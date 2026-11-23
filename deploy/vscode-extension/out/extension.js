"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.WebPlayerWebviewViewProvider = exports.WebPlayerWebviewPanelManager = exports.WebPlayerWebviewBridge = exports.WebPlayerMcpServerDefinitionProvider = exports.McpClientService = void 0;
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const WEBPLAYER_MCP_SERVER_DEFINITION_PROVIDER_ID = 'webplayer-mcp-ui.webplayer-mcp';
/**
 * Domain Service: McpClientService
 * Handles HTTP JSON-RPC communication with the WebPlayer MCP Server.
 */
class McpClientService {
    endpoint;
    constructor(endpoint) {
        this.endpoint = endpoint || this.loadEndpointFromConfig();
    }
    getEndpoint() {
        return this.endpoint;
    }
    setEndpoint(endpoint) {
        this.endpoint = endpoint;
    }
    loadEndpointFromConfig() {
        const config = vscode.workspace.getConfiguration('webplayerMcp');
        const configuredEndpoint = config.get('endpoint');
        return (configuredEndpoint && configuredEndpoint.trim()) || 'http://192.168.0.47:8766/mcp';
    }
    async sendJsonRpc(method, params = {}) {
        const protocolVersion = '2026-07-28';
        const requestParams = {
            ...params,
            _meta: {
                ...(typeof params._meta === 'object' && params._meta !== null ? params._meta : {}),
                'io.modelcontextprotocol/protocolVersion': protocolVersion,
            },
        };
        const payload = {
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
        const data = (await response.json());
        if (data.error) {
            throw new Error(`MCP JSON-RPC error (${data.error.code}): ${data.error.message}`);
        }
        if (data.result === undefined) {
            throw new Error('MCP server returned empty result');
        }
        return data.result;
    }
    async initialize() {
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
    async readUiResource(uri = 'ui://Mpc.App.ui') {
        const result = await this.sendJsonRpc('resources/read', { uri });
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
    async callTool(name, args = {}) {
        return this.sendJsonRpc('tools/call', {
            name,
            arguments: args,
        });
    }
}
exports.McpClientService = McpClientService;
/**
 * Publishes the existing WebPlayer MCP endpoint to VS Code's MCP tool registry.
 */
class WebPlayerMcpServerDefinitionProvider {
    mcpService;
    definitionsChangedEmitter = new vscode.EventEmitter();
    onDidChangeMcpServerDefinitions = this.definitionsChangedEmitter.event;
    constructor(mcpService) {
        this.mcpService = mcpService;
    }
    provideMcpServerDefinitions(_token) {
        return [
            new vscode.McpHttpServerDefinition('WebPlayer MCP', vscode.Uri.parse(this.mcpService.getEndpoint())),
        ];
    }
    refreshDefinitions() {
        this.definitionsChangedEmitter.fire();
    }
    dispose() {
        this.definitionsChangedEmitter.dispose();
    }
}
exports.WebPlayerMcpServerDefinitionProvider = WebPlayerMcpServerDefinitionProvider;
/**
 * Domain Object: WebPlayerWebviewBridge
 * Connects either VS Code webview surface to WebPlayer MCP tools.
 */
class WebPlayerWebviewBridge {
    mcpService;
    constructor(mcpService) {
        this.mcpService = mcpService;
    }
    attachWebview(webview) {
        return vscode.Disposable.from(webview.onDidReceiveMessage(async (message) => {
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
                }
                catch (err) {
                    await webview.postMessage({
                        type: 'webplayer-tool-result',
                        requestId: message.requestId,
                        tool: message.name,
                        error: err instanceof Error ? err.message : String(err),
                    });
                }
            }
            else if (message.type === 'webplayer-ui-ready') {
                await this.syncHostContext(webview);
            }
            else if (message.type === 'webplayer-open-link') {
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
                }
                catch {
                    void vscode.window.showWarningMessage('The TIDAL link is invalid.');
                }
            }
        }), vscode.window.onDidChangeActiveColorTheme(() => {
            void this.syncHostContext(webview);
        }));
    }
    async syncHostContext(webview) {
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
exports.WebPlayerWebviewBridge = WebPlayerWebviewBridge;
/**
 * Domain Object: WebPlayerWebviewPanelManager
 * Manages creation, rendering, and event listeners for the WebviewPanel.
 */
class WebPlayerWebviewPanelManager {
    panel;
    mcpService;
    bridge;
    constructor(mcpService, bridge = new WebPlayerWebviewBridge(mcpService)) {
        this.mcpService = mcpService;
        this.bridge = bridge;
    }
    async showOrFocusPanel(extensionUri) {
        if (this.panel) {
            this.panel.reveal(vscode.ViewColumn.One);
            return this.panel;
        }
        this.panel = vscode.window.createWebviewPanel('webplayerMcpUi', 'TIDAL MCP WebPlayer', vscode.ViewColumn.One, {
            enableScripts: true,
            retainContextWhenHidden: true,
            localResourceRoots: [extensionUri],
        });
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
    async loadAndRenderUi() {
        if (!this.panel) {
            return;
        }
        try {
            await this.mcpService.initialize();
            const markup = await this.mcpService.readUiResource('ui://Mpc.App.ui');
            this.panel.webview.html = markup;
            void this.syncHostContext();
        }
        catch (error) {
            const errorMessage = error instanceof Error ? error.message : String(error);
            vscode.window.showErrorMessage(`Failed to load WebPlayer MCP App UI: ${errorMessage}`);
            this.panel.webview.html = this.buildErrorHtml(errorMessage);
        }
    }
    syncHostContext() {
        if (!this.panel) {
            return;
        }
        void this.bridge.syncHostContext(this.panel.webview);
    }
    buildErrorHtml(errorMessage) {
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
exports.WebPlayerWebviewPanelManager = WebPlayerWebviewPanelManager;
/**
 * Domain Object: WebPlayerWebviewViewProvider
 * Provides sidebar embedding for WebPlayer MCP UI.
 */
class WebPlayerWebviewViewProvider {
    extensionUri;
    mcpService;
    bridge;
    static viewType = 'webplayerMcpView';
    view;
    bridgeSubscription;
    constructor(extensionUri, mcpService, bridge = new WebPlayerWebviewBridge(mcpService)) {
        this.extensionUri = extensionUri;
        this.mcpService = mcpService;
        this.bridge = bridge;
    }
    async resolveWebviewView(webviewView, _context, _token) {
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
        }
        catch (error) {
            const errorMessage = error instanceof Error ? error.message : String(error);
            webviewView.webview.html = `<p style="color:red;padding:10px;">${errorMessage}</p>`;
        }
    }
}
exports.WebPlayerWebviewViewProvider = WebPlayerWebviewViewProvider;
/**
 * Extension Entry Points: activate & deactivate
 */
function activate(context) {
    const mcpService = new McpClientService();
    const mcpServerDefinitionProvider = new WebPlayerMcpServerDefinitionProvider(mcpService);
    const mcpServerDefinitionRegistration = vscode.lm.registerMcpServerDefinitionProvider(WEBPLAYER_MCP_SERVER_DEFINITION_PROVIDER_ID, mcpServerDefinitionProvider);
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
        }
        catch (error) {
            vscode.window.showErrorMessage(`Play Next failed: ${error}`);
        }
    });
    const viewProvider = new WebPlayerWebviewViewProvider(context.extensionUri, mcpService, webviewBridge);
    const sidebarViewDisposable = vscode.window.registerWebviewViewProvider(WebPlayerWebviewViewProvider.viewType, viewProvider);
    context.subscriptions.push(openUiCommand, refreshUiCommand, playNextCommand, sidebarViewDisposable, endpointConfigurationChange, mcpServerDefinitionProvider, mcpServerDefinitionRegistration);
}
function deactivate() {
    // Clean up resources if necessary.
}
//# sourceMappingURL=extension.js.map