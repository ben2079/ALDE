import * as vscode from 'vscode';

/**
 * Interface representing a JSON-RPC 2.0 Request Payload.
 */
interface JsonRpcRequest {
    jsonrpc: '2.0';
    id: number | string;
    method: string;
    params?: Record<string, unknown>;
}

/**
 * Interface representing a JSON-RPC 2.0 Response Payload.

 */
interface JsonRpcResponse<T = unknown> {
    jsonrpc: '2.0';
    id: number | string;
    result?: T;
    error?: {
        code: number;
        message: string;
        data?: unknown;
    };
}

const WEBPLAYER_MCP_SERVER_DEFINITION_PROVIDER_ID = 'webplayer-mcp-ui.webplayer-mcp';

/**
 * Domain Service: McpClientService
 * Handles HTTP JSON-RPC communication with the WebPlayer MCP Server.
 */
export class McpClientService {
    private endpoint: string;

    constructor(endpoint?: string) {
        this.endpoint = endpoint || this.loadEndpointFromConfig();
    }

    public getEndpoint(): string {
        return this.endpoint;
    }

    public setEndpoint(endpoint: string): void {
        this.endpoint = endpoint;
    }

    public loadEndpointFromConfig(): string {
        const config = vscode.workspace.getConfiguration('webplayerMcp');
        const configuredEndpoint = config.get<string>('endpoint');
        return (configuredEndpoint && configuredEndpoint.trim()) || 'http://192.168.0.47:8766/mcp';
    }

    public async sendJsonRpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
        const protocolVersion = '2026-07-28';
        const requestParams = {
            ...params,
            _meta: {
                ...(typeof params._meta === 'object' && params._meta !== null ? params._meta : {}),
                'io.modelcontextprotocol/protocolVersion': protocolVersion,
            },
        };
        const payload: JsonRpcRequest = {
            jsonrpc: '2.0',
            id: Date.now(),
            method,
            params: requestParams,
        };

        const name = method === 'resources/read' ? String(params.uri || '')
            : method === 'tools/call' || method === 'prompts/get' ? String(params.name || '') : '';

        const response = await fetch(this.getEndpoint(), {
            method: 'POST',
            signal: AbortSignal.timeout(20000),
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'MCP-Protocol-Version': protocolVersion,
                'Mcp-Method': method,
                ...(name ? { 'Mcp-Name': name } : {}),
            },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            throw new Error(`MCP server HTTP error (${response.status}): ${response.statusText}`);
        }

        const data = (await response.json()) as JsonRpcResponse<T>;
        if (data.error) {
            throw new Error(`MCP JSON-RPC error (${data.error.code}): ${data.error.message}`);
        }

        if (data.result === undefined) {
            throw new Error('MCP server returned empty result');
        }

        return data.result;
    }

    public async initialize(): Promise<unknown> {
        return this.sendJsonRpc('initialize', {
            protocolVersion: '2026-07-28',
            capabilities: {
                extensions: {
                    'io.modelcontextprotocol/ui': {
                        mimeTypes: ['text/html;profile=mcp-app'],
                    },
                },
            },
            clientInfo: {
                name: 'vscode-webplayer-extension',
                version: '1.0.1',
            },
        });
    }

    public async readUiResource(uri = 'ui://Mpc.App.ui'): Promise<string> {
        const result = await this.sendJsonRpc<{
            contents?: Array<{ text?: string; blob?: string; mimeType?: string }>;
        }>('resources/read', { uri });

        const contents = result?.contents || [];
        const uiContent = contents.find((c) => c.text || c.blob);
        if (!uiContent) {
            throw new Error(`UI resource empty or not found: ${uri}`);
        }

        if (uiContent.text) {
            return uiContent.text;
        }

        if (uiContent.blob) {
            if (typeof Buffer !== 'undefined') {
                return Buffer.from(uiContent.blob, 'base64').toString('utf-8');
            }
            return atob(uiContent.blob);
        }

        throw new Error('Resource content contains neither text nor blob');
    }

    public async callTool(name: string, args: Record<string, unknown> = {}): Promise<unknown> {
        return this.sendJsonRpc('tools/call', {
            name,
            arguments: args,
        });
    }
}

/**
 * Publishes the existing WebPlayer MCP endpoint to VS Code's MCP tool registry.
 */
export class WebPlayerMcpServerDefinitionProvider
    implements vscode.McpServerDefinitionProvider<vscode.McpHttpServerDefinition>, vscode.Disposable {
    private readonly definitionsChangedEmitter = new vscode.EventEmitter<void>();
    public readonly onDidChangeMcpServerDefinitions: vscode.Event<void> = this.definitionsChangedEmitter.event;

    constructor(private readonly mcpService: McpClientService) { }

    public provideMcpServerDefinitions(
        _token: vscode.CancellationToken
    ): vscode.ProviderResult<vscode.McpHttpServerDefinition[]> {
        return [
            new vscode.McpHttpServerDefinition(
                'WebPlayer MCP',
                vscode.Uri.parse(this.mcpService.getEndpoint())
            ),
        ];
    }

    public refreshDefinitions(): void {
        this.definitionsChangedEmitter.fire();
    }

    public dispose(): void {
        this.definitionsChangedEmitter.dispose();
    }
}

/**
 * Domain Object: WebPlayerWebviewBridge
 * Connects either VS Code webview surface to WebPlayer MCP tools.
 */
export class WebPlayerWebviewBridge {
    constructor(private readonly mcpService: McpClientService) { }

    public attachWebview(webview: vscode.Webview): vscode.Disposable {
        return vscode.Disposable.from(
            webview.onDidReceiveMessage(async (message) => {
                if (!message || typeof message !== 'object') {
                    return;
                }

                if (message.type === 'webplayer-call-tool') {
                    try {
                        const result = await this.mcpService.callTool(message.name, message.arguments);
                        await webview.postMessage({
                            type: 'webplayer-tool-result',
                            requestId: message.requestId,
                            tool: message.name,
                            result,
                        });
                    } catch (err) {
                        await webview.postMessage({
                            type: 'webplayer-tool-result',
                            requestId: message.requestId,
                            tool: message.name,
                            error: err instanceof Error ? err.message : String(err),
                        });
                    }
                } else if (message.type === 'webplayer-ui-ready') {
                    await this.syncHostContext(webview);
                } else if (message.type === 'webplayer-open-link') {
                    const candidate = typeof message.url === 'string' ? message.url.trim() : '';
                    try {
                        const parsedUrl = new URL(candidate);
                        if (!['http:', 'https:'].includes(parsedUrl.protocol) || !/(^|\.)tidal\.com$/i.test(parsedUrl.hostname)) {
                            return;
                        }
                        const opened = await vscode.env.openExternal(vscode.Uri.parse(parsedUrl.toString()));
                        if (!opened) {
                            void vscode.window.showWarningMessage('VS Code could not open the TIDAL link.');
                        }
                    } catch {
                        void vscode.window.showWarningMessage('The TIDAL link is invalid.');
                    }
                }
            }),
            vscode.window.onDidChangeActiveColorTheme(() => {
                void this.syncHostContext(webview);
            })
        );
    }

    public async syncHostContext(webview: vscode.Webview): Promise<void> {
        const theme = vscode.window.activeColorTheme.kind === vscode.ColorThemeKind.Dark ? 'dark' : 'light';
        await webview.postMessage({
            type: 'vscode-host-context',
            context: {
                theme,
                mcpEndpoint: this.mcpService.getEndpoint(),
                styles: {
                    variables: {
                        '--fontFamilyBase': 'var(--vscode-font-family)',
                    },
                },
            },
        });
    }
}

/**
 * Domain Object: WebPlayerWebviewPanelManager
 * Manages creation, rendering, and event listeners for the WebviewPanel.
 */
export class WebPlayerWebviewPanelManager {
    private panel: vscode.WebviewPanel | undefined;
    private mcpService: McpClientService;
    private bridge: WebPlayerWebviewBridge;

    constructor(mcpService: McpClientService, bridge = new WebPlayerWebviewBridge(mcpService)) {
        this.mcpService = mcpService;
        this.bridge = bridge;
    }

    public async showOrFocusPanel(extensionUri: vscode.Uri): Promise<vscode.WebviewPanel> {
        if (this.panel) {
            this.panel.reveal(vscode.ViewColumn.One);
            return this.panel;
        }

        this.panel = vscode.window.createWebviewPanel(
            'webplayerMcpUi',
            'TIDAL MCP WebPlayer',
            vscode.ViewColumn.One,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [extensionUri],
            }
        );

        const bridgeSubscription = this.bridge.attachWebview(this.panel.webview);
        const panel = this.panel;
        panel.onDidDispose(() => {
            bridgeSubscription.dispose();
            if (this.panel === panel) {
                this.panel = undefined;
            }
        });

        await this.loadAndRenderUi();
        return this.panel;
    }

    public async loadAndRenderUi(): Promise<void> {
        if (!this.panel) {
            return;
        }

        try {
            await this.mcpService.initialize();
            const markup = await this.mcpService.readUiResource('ui://Mpc.App.ui');
            this.panel.webview.html = markup;
            void this.syncHostContext();
        } catch (error) {
            const errorMessage = error instanceof Error ? error.message : String(error);
            vscode.window.showErrorMessage(`Failed to load WebPlayer MCP App UI: ${errorMessage}`);
            this.panel.webview.html = this.buildErrorHtml(errorMessage);
        }
    }

    public syncHostContext(): void {
        if (!this.panel) {
            return;
        }
        void this.bridge.syncHostContext(this.panel.webview);
    }

    private buildErrorHtml(errorMessage: string): string {
        return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    body { font-family: var(--vscode-font-family, sans-serif); padding: 20px; color: var(--vscode-errorForeground, #ff6b6b); background: var(--vscode-editor-background, #1e1e1e); }
    h3 { margin-top: 0; }
    button { padding: 8px 14px; background: var(--vscode-button-background, #0e639c); color: var(--vscode-button-foreground, #fff); border: none; border-radius: 4px; cursor: pointer; }
    button:hover { background: var(--vscode-button-hoverBackground, #1177bb); }
  </style>
</head>
<body>
  <h3>WebPlayer MCP UI Error</h3>
  <p>${errorMessage}</p>
  <p><button onclick="location.reload()">Retry Connection</button></p>
</body>
</html>`;
    }
}

/**
 * Domain Object: WebPlayerWebviewViewProvider
 * Provides sidebar embedding for WebPlayer MCP UI.
 */
export class WebPlayerWebviewViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'webplayerMcpView';
    private view: vscode.WebviewView | undefined;
    private bridgeSubscription: vscode.Disposable | undefined;

    constructor(
        private readonly extensionUri: vscode.Uri,
        private readonly mcpService: McpClientService,
        private readonly bridge = new WebPlayerWebviewBridge(mcpService)
    ) { }

    public async resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): Promise<void> {
        this.view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.extensionUri],
        };
        this.bridgeSubscription?.dispose();
        const bridgeSubscription = this.bridge.attachWebview(webviewView.webview);
        this.bridgeSubscription = bridgeSubscription;
        webviewView.onDidDispose(() => {
            bridgeSubscription.dispose();
            if (this.bridgeSubscription === bridgeSubscription) {
                this.bridgeSubscription = undefined;
            }
            if (this.view === webviewView) {
                this.view = undefined;
            }
        });

        try {
            await this.mcpService.initialize();
            const markup = await this.mcpService.readUiResource('ui://Mpc.App.ui');
            webviewView.webview.html = markup;
            void this.bridge.syncHostContext(webviewView.webview);
        } catch (error) {
            const errorMessage = error instanceof Error ? error.message : String(error);
            webviewView.webview.html = `<p style="color:red;padding:10px;">${errorMessage}</p>`;
        }
    }
}

/**
 * Extension Entry Points: activate & deactivate
 */
export function activate(context: vscode.ExtensionContext): void {
    const mcpService = new McpClientService();
    const mcpServerDefinitionProvider = new WebPlayerMcpServerDefinitionProvider(mcpService);
    const mcpServerDefinitionRegistration = vscode.lm.registerMcpServerDefinitionProvider(
        WEBPLAYER_MCP_SERVER_DEFINITION_PROVIDER_ID,
        mcpServerDefinitionProvider
    );
    const endpointConfigurationChange = vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration('webplayerMcp.endpoint')) {
            mcpService.setEndpoint(mcpService.loadEndpointFromConfig());
            mcpServerDefinitionProvider.refreshDefinitions();
        }
    });
    const webviewBridge = new WebPlayerWebviewBridge(mcpService);
    const panelManager = new WebPlayerWebviewPanelManager(mcpService, webviewBridge);

    const openUiCommand = vscode.commands.registerCommand('webplayer.openUI', async () => {
        await panelManager.showOrFocusPanel(context.extensionUri);
    });

    const refreshUiCommand = vscode.commands.registerCommand('webplayer.refreshUI', async () => {
        await panelManager.loadAndRenderUi();
        vscode.window.showInformationMessage('WebPlayer MCP App UI refreshed.');
    });

    const playNextCommand = vscode.commands.registerCommand('webplayer.playNext', async () => {
        try {
            await mcpService.callTool('webplayer_forward', {
                playback_backend: 'browser',
                player_selector: 'chromium',
            });
            vscode.window.showInformationMessage('Skipped to next track.');
        } catch (error) {
            vscode.window.showErrorMessage(`Play Next failed: ${error}`);
        }
    });

    const viewProvider = new WebPlayerWebviewViewProvider(context.extensionUri, mcpService, webviewBridge);
    const sidebarViewDisposable = vscode.window.registerWebviewViewProvider(
        WebPlayerWebviewViewProvider.viewType,
        viewProvider
    );

    context.subscriptions.push(
        openUiCommand,
        refreshUiCommand,
        playNextCommand,
        sidebarViewDisposable,
        endpointConfigurationChange,
        mcpServerDefinitionProvider,
        mcpServerDefinitionRegistration
    );
}

export function deactivate(): void {
    // Clean up resources if necessary.
}
