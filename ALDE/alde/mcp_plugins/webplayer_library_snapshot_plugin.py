"""WebPlayer MCP plugin for snapshot-based library extraction.

The plugin encapsulates command construction and stdout payload parsing for
the ``webplayer_library_snapshot`` action so the WebPlayer MCP service can keep
its core dispatch logic independent from action-specific details.
"""

from __future__ import annotations

import base64
import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class WPlibSnaPlugin:
    """Plugin implementation for the ``webplayer_library_snapshot`` action."""

    object_name: str = "webplayer_library_snapshot"

    def load_object_command(
        self,
        *,
        arguments: dict[str, Any],
        library_section_urls: Mapping[str, str],
        load_validated_tidal_url: Callable[[str], str | None],
        load_bounded_int: Callable[..., int],
        load_browser_open_command: Callable[..., str],
    ) -> str:
        library_url = str(arguments.get("library_url") or "").strip()
        if library_url:
            target_url = load_validated_tidal_url(library_url)
            if target_url is None:
                return "echo 'error=invalid_library_url'; exit 1;"
        else:
            section = str(arguments.get("section") or "favorites_tracks").strip().casefold()
            target_url = library_section_urls.get(section)
            if target_url is None:
                supported_sections = ",".join(sorted(str(key) for key in library_section_urls.keys()))
                return f"echo 'error=invalid_library_section'; echo 'supported_sections={supported_sections}'; exit 1;"

        wait_for_page_s = load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        cdp_port = load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        max_items = load_bounded_int(
            arguments.get("max_items"),
            default_value=200,
            minimum=1,
            maximum=2000,
        )
        scroll_steps = load_bounded_int(
            arguments.get("scroll_steps"),
            default_value=24,
            minimum=1,
            maximum=500,
        )
        scroll_pause_ms = load_bounded_int(
            arguments.get("scroll_pause_ms"),
            default_value=250,
            minimum=10,
            maximum=5000,
        )

        cdp_snapshot_command = self._load_cdp_library_snapshot_command(
            cdp_port=cdp_port,
            max_items=max_items,
            scroll_steps=scroll_steps,
            scroll_pause_ms=scroll_pause_ms,
            target_url=target_url,
        )

        return (
            load_browser_open_command(
                target_url=target_url,
                log_file_path="/tmp/webplayer_mcp_snapshot.log",
                allow_xdg_open_fallback=False,
            )
            + f" sleep {wait_for_page_s}; "
            + cdp_snapshot_command
            + " snapshot_exit_code=$?; if [ \"$snapshot_exit_code\" -ne 0 ]; then echo 'error=library_snapshot_failed'; exit 3; fi; "
            + "echo section="
            + shlex.quote(str(arguments.get("section") or "favorites_tracks").strip().casefold() or "favorites_tracks")
            + ";"
        )

    def load_result_payload(self, stdout_payload: str) -> dict[str, Any]:
        metadata: dict[str, str] = {}
        for line in str(stdout_payload or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            metadata[str(key).strip()] = str(value).strip()

        track_object_list: list[dict[str, Any]] = []
        snapshot_json_base64 = str(metadata.get("snapshot_json_base64") or "").strip()
        if snapshot_json_base64:
            try:
                decoded_payload = base64.b64decode(snapshot_json_base64.encode("ascii"))
                loaded_payload = json.loads(decoded_payload.decode("utf-8", errors="replace"))
                if isinstance(loaded_payload, list):
                    for item in loaded_payload:
                        if isinstance(item, dict):
                            track_object_list.append(dict(item))
            except Exception:
                track_object_list = []

        snapshot_anchor_count: int | None = None
        raw_anchor_count = str(metadata.get("snapshot_anchor_count") or "").strip()
        if raw_anchor_count:
            try:
                snapshot_anchor_count = int(raw_anchor_count)
            except Exception:
                snapshot_anchor_count = None

        snapshot_data_node_count: int | None = None
        raw_data_node_count = str(metadata.get("snapshot_data_node_count") or "").strip()
        if raw_data_node_count:
            try:
                snapshot_data_node_count = int(raw_data_node_count)
            except Exception:
                snapshot_data_node_count = None

        raw_login_hint = str(metadata.get("snapshot_login_hint") or "").strip().casefold()
        snapshot_login_hint = raw_login_hint in {"1", "true", "yes", "on"}
        raw_shell_fallback_hint = str(metadata.get("snapshot_shell_fallback_hint") or "").strip().casefold()
        snapshot_shell_fallback_hint = raw_shell_fallback_hint in {"1", "true", "yes", "on"}
        raw_recovery_attempted = str(metadata.get("snapshot_recovery_attempted") or "").strip().casefold()
        snapshot_recovery_attempted = raw_recovery_attempted in {"1", "true", "yes", "on"}
        raw_recovery_succeeded = str(metadata.get("snapshot_recovery_succeeded") or "").strip().casefold()
        snapshot_recovery_succeeded = raw_recovery_succeeded in {"1", "true", "yes", "on"}
        snapshot_source_url = str(metadata.get("snapshot_source_url") or "").strip()
        source_looks_like_tidal_shell = snapshot_source_url.startswith("https://tidal.com/")
        snapshot_error_class = ""
        if not track_object_list:
            if snapshot_shell_fallback_hint or (source_looks_like_tidal_shell and snapshot_data_node_count == 0):
                snapshot_error_class = "shell_fallback_unextractable"
            elif snapshot_login_hint:
                snapshot_error_class = "login_or_session_blocked"
            else:
                snapshot_error_class = "empty_snapshot"

        return {
            "snapshot_ok": bool(track_object_list),
            "snapshot_count": len(track_object_list),
            "tracks": track_object_list,
            "snapshot_source_url": snapshot_source_url or None,
            "snapshot_anchor_count": snapshot_anchor_count,
            "snapshot_data_node_count": snapshot_data_node_count,
            "snapshot_page_title": str(metadata.get("snapshot_page_title") or "").strip() or None,
            "snapshot_ready_state": str(metadata.get("snapshot_ready_state") or "").strip() or None,
            "snapshot_login_hint": snapshot_login_hint,
            "snapshot_shell_fallback_hint": snapshot_shell_fallback_hint,
            "snapshot_recovery_attempted": snapshot_recovery_attempted,
            "snapshot_recovery_succeeded": snapshot_recovery_succeeded,
            "snapshot_error_class": snapshot_error_class or None,
        }

    def _load_cdp_library_snapshot_command(
        self,
        *,
        cdp_port: int,
        max_items: int,
        scroll_steps: int,
        scroll_pause_ms: int,
        target_url: str,
    ) -> str:
        env_prefix = (
            f"CDP_PORT={int(cdp_port)} "
            f"CDP_MAX_ITEMS={int(max_items)} "
            f"CDP_SCROLL_STEPS={int(scroll_steps)} "
            f"CDP_SCROLL_PAUSE_MS={int(scroll_pause_ms)} "
            + "CDP_TARGET_URL="
            + shlex.quote(str(target_url or "").strip())
            + " "
        )
        cdp_script = r'''python3 - <<'PY'
import asyncio
import base64
import json
import os
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
max_items = int(str(os.environ.get('CDP_MAX_ITEMS') or '200'))
scroll_steps = int(str(os.environ.get('CDP_SCROLL_STEPS') or '24'))
scroll_pause_ms = int(str(os.environ.get('CDP_SCROLL_PAUSE_MS') or '250'))
target_url_hint = str(os.environ.get('CDP_TARGET_URL') or '').strip()


def load_target_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))

    page_targets = [target for target in targets if str(target.get('type') or '') == 'page']
    if not page_targets:
        raise RuntimeError('cdp_no_page_target')

    def normalize(value: object) -> str:
        return str(value or '').strip().casefold()

    normalized_hint = normalize(target_url_hint)
    hint_targets = [
        target
        for target in page_targets
        if normalized_hint and normalized_hint in normalize(target.get('url'))
    ]
    if hint_targets:
        chosen_target = hint_targets[-1]
        return str(chosen_target.get('webSocketDebuggerUrl') or '')

    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    if not tidal_targets:
        raise RuntimeError('cdp_no_tidal_target')

    favorite_targets = [target for target in tidal_targets if '/favorites/' in str(target.get('url') or '')]
    chosen_target = favorite_targets[-1] if favorite_targets else tidal_targets[-1]
    return str(chosen_target.get('webSocketDebuggerUrl') or '')


async def run() -> int:
    ws_url = load_target_url()
    if not ws_url:
        print('error=cdp_no_ws_url')
        return 11

    async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
        message_id = 0

        async def send_method(method_name: str, params: dict | None = None) -> dict:
            nonlocal message_id
            message_id += 1
            payload = {'id': message_id, 'method': method_name}
            if params:
                payload['params'] = params
            await websocket.send(json.dumps(payload))
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get('id') == message_id:
                    return message

        await send_method('Page.enable')
        await send_method('Runtime.enable')

        if target_url_hint:
            await send_method('Page.navigate', {'url': target_url_hint})
            await asyncio.sleep(1.2)

        expression = """
(async function () {
    var maxItems = %(max_items)d;
    var scrollSteps = %(scroll_steps)d;
    var scrollPauseMs = %(scroll_pause_ms)d;
    var itemMap = Object.create(null);
    var itemList = [];
    var lastDataNodeCount = 0;

    function toText(value) {
        return String(value || '').replace(/\\s+/g, ' ').replace(/^\\s+|\\s+$/g, '');
    }

    function loadTrackIdFromText(rawValue) {
        var text = toText(rawValue);
        if (!text) return '';
        var patternList = [
            /\\/tracks?\\/([A-Za-z0-9_-]{4,})/i,
            /\"track(?:Id|ID|_id)\"\\s*:\\s*\"?([A-Za-z0-9_-]{4,})\"?/i,
            /\\btrackId=([A-Za-z0-9_-]{4,})/i,
            /\"id\"\\s*:\\s*\"?([0-9]{4,})\"?\\s*,\\s*\"type\"\\s*:\\s*\"track\"/i,
            /\"type\"\\s*:\\s*\"track\"\\s*,\\s*\"id\"\\s*:\\s*\"?([0-9]{4,})\"?/i
        ];
        for (var i = 0; i < patternList.length; i += 1) {
            var match = text.match(patternList[i]);
            if (match && match[1]) {
                return toText(match[1]);
            }
        }
        return '';
    }

    function pushTrack(trackId, sourceUrl, titleText, artistText, albumText) {
        var normalizedId = toText(trackId);
        if (!normalizedId) return;
        if (itemMap[normalizedId]) return;
        itemMap[normalizedId] = true;
        itemList.push({
            tidal_track_id: normalizedId,
            title: toText(titleText),
            artist: toText(artistText),
            album: toText(albumText),
            liked: true,
            source_url: toText(sourceUrl || ('https://listen.tidal.com/track/' + normalizedId))
        });
    }

    function collectFromHtml() {
        var htmlNode = document.documentElement;
        var html = String((htmlNode && htmlNode.innerHTML) || '');
        if (!html) return;
        var patternList = [
            /\\/track\\/(\\d{4,})/gi,
            /\"track(?:Id|ID|_id)\"\\s*:\\s*\"?(\\d{4,})\"?/gi,
            /trackId=(\\d{4,})/gi,
            /\"id\"\\s*:\\s*\"?(\\d{4,})\"?\\s*,\\s*\"type\"\\s*:\\s*\"track\"/gi,
            /\"type\"\\s*:\\s*\"track\"\\s*,\\s*\"id\"\\s*:\\s*\"?(\\d{4,})\"?/gi
        ];
        for (var i = 0; i < patternList.length; i += 1) {
            var pattern = patternList[i];
            var match = null;
            while ((match = pattern.exec(html)) !== null) {
                if (match && match[1]) {
                    pushTrack(String(match[1]), 'https://listen.tidal.com/track/' + String(match[1]), '', '', '');
                    if (itemList.length >= maxItems) return;
                }
            }
            if (itemList.length >= maxItems) return;
        }
    }

    function collectFromAnchors() {
        var anchors = document.querySelectorAll('a[href*="/track/"],a[href*="/tracks/"]');
        for (var i = 0; i < anchors.length; i += 1) {
            if (itemList.length >= maxItems) break;
            var anchor = anchors[i];
            var href = toText(anchor.getAttribute('href') || anchor.href || '');
            var extractedTrackId = loadTrackIdFromText(href);
            if (!extractedTrackId) continue;
            var row = anchor.closest ? (anchor.closest('tr,li,article,div') || anchor) : anchor;
            var rowText = toText((row && (row.innerText || row.textContent || '')) || '');
            var lines = rowText.split('\\n');
            var titleText = toText(anchor.innerText || (lines.length > 0 ? lines[0] : ''));
            var artistText = toText(lines.length > 1 ? lines[1] : '');
            var albumText = toText(lines.length > 2 ? lines[2] : '');
            pushTrack(extractedTrackId, href, titleText, artistText, albumText);
        }
    }

    function collectFromDataNodes() {
        var selector = '[data-track-id],[data-trackid],[data-testid*="track"],[data-test-id*="track"],[data-id]';
        var nodeList = document.querySelectorAll(selector);
        lastDataNodeCount = nodeList.length;
        for (var i = 0; i < nodeList.length; i += 1) {
            if (itemList.length >= maxItems) break;
            var node = nodeList[i];
            if (!node) continue;

            var attributeTextList = [];
            if (node.attributes && node.attributes.length) {
                for (var attrIndex = 0; attrIndex < node.attributes.length; attrIndex += 1) {
                    var attr = node.attributes[attrIndex];
                    if (!attr) continue;
                    var entry = toText((attr.name || '') + '=' + (attr.value || ''));
                    if (entry) attributeTextList.push(entry);
                }
            }

            var nodeText = toText((node.textContent || node.innerText || ''));
            var candidateText = toText(attributeTextList.join(' ') + ' ' + nodeText);
            var extractedTrackId = loadTrackIdFromText(candidateText);
            if (!extractedTrackId) continue;

            var anchor = node.querySelector ? node.querySelector('a[href*="/track/"],a[href*="/tracks/"]') : null;
            if (!anchor && node.closest) {
                anchor = node.closest('a[href*="/track/"],a[href*="/tracks/"]');
            }
            var href = anchor ? toText(anchor.getAttribute('href') || anchor.href || '') : '';
            var lines = nodeText.split('\\n');
            var titleText = toText(lines.length > 0 ? lines[0] : '');
            var artistText = toText(lines.length > 1 ? lines[1] : '');
            var albumText = toText(lines.length > 2 ? lines[2] : '');
            pushTrack(extractedTrackId, href, titleText, artistText, albumText);
        }
    }

    function loadScrollTargets() {
        var targetList = [window];
        var nodeList = document.querySelectorAll('main,section,div,[role="grid"],[role="table"],[role="list"]');
        for (var i = 0; i < nodeList.length; i += 1) {
            var node = nodeList[i];
            if (!node || !node.style) continue;
            try {
                var style = window.getComputedStyle(node);
                var overflowY = String(style.overflowY || '').toLowerCase();
                var isScrollable = overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay';
                if (!isScrollable) continue;
                if ((node.scrollHeight || 0) <= ((node.clientHeight || 0) + 80)) continue;
                targetList.push(node);
            } catch (_error) {
            }
            if (targetList.length >= 8) break;
        }
        return targetList;
    }

    collectFromAnchors();
    collectFromDataNodes();
    collectFromHtml();

    var scrollTargets = loadScrollTargets();
    for (var step = 0; step < scrollSteps; step += 1) {
        if (itemList.length >= maxItems) break;
        var deltaY = Math.max(360, Math.floor(window.innerHeight * 0.85));
        for (var targetIndex = 0; targetIndex < scrollTargets.length; targetIndex += 1) {
            var target = scrollTargets[targetIndex];
            if (target === window) {
                window.scrollBy(0, deltaY);
            } else {
                target.scrollTop = (target.scrollTop || 0) + deltaY;
            }
        }
        collectFromAnchors();
        collectFromDataNodes();
        collectFromHtml();
        if (itemList.length < maxItems && scrollPauseMs > 0) {
            await new Promise(function (resolve) { setTimeout(resolve, scrollPauseMs); });
            collectFromAnchors();
            collectFromDataNodes();
            collectFromHtml();
        }
    }

    var bodyText = toText((document.body && (document.body.innerText || document.body.textContent || '')) || '').toLowerCase();
    var locationHost = toText((window.location && window.location.host) || '').toLowerCase();
    var locationPath = toText((window.location && window.location.pathname) || '').toLowerCase();
    var shellFallbackHint = locationHost === 'tidal.com' && lastDataNodeCount === 0 && itemList.length === 0;
    var loginHint = bodyText.indexOf('log in') >= 0 || bodyText.indexOf('sign up') >= 0 || bodyText.indexOf('anmelden') >= 0 || shellFallbackHint;

    return {
        source_url: String((window.location && window.location.href) || ''),
        anchor_count: document.querySelectorAll('a[href*="/track/"],a[href*="/tracks/"]').length,
        data_node_count: lastDataNodeCount,
        page_title: toText(document.title || ''),
        ready_state: toText(document.readyState || ''),
        login_hint: loginHint,
        shell_fallback_hint: shellFallbackHint,
        tracks: itemList.slice(0, maxItems)
    };
})()
""" % {
            'max_items': int(max_items),
            'scroll_steps': int(scroll_steps),
            'scroll_pause_ms': int(scroll_pause_ms),
        }

        async def load_snapshot_eval_result() -> tuple[list[dict], dict]:
            response = await send_method(
                'Runtime.evaluate',
                {
                    'expression': expression,
                    'returnByValue': True,
                    'awaitPromise': True,
                },
            )

            runtime_result = response.get('result') if isinstance(response.get('result'), dict) else {}
            exception_details = runtime_result.get('exceptionDetails') if isinstance(runtime_result, dict) else None
            if isinstance(exception_details, dict):
                exception_text = str(exception_details.get('text') or '').strip()
                if exception_text:
                    print('snapshot_eval_error=' + exception_text.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))
                line_number = exception_details.get('lineNumber')
                column_number = exception_details.get('columnNumber')
                if isinstance(line_number, int) and isinstance(column_number, int):
                    print('snapshot_eval_location=' + str(line_number) + ':' + str(column_number))
                    expression_lines = str(expression or '').splitlines()
                    if 0 <= line_number < len(expression_lines):
                        print('snapshot_eval_line=' + expression_lines[line_number].replace('=', '-'))
                exception_object = exception_details.get('exception') if isinstance(exception_details.get('exception'), dict) else {}
                exception_description = str(exception_object.get('description') or '').strip()
                if exception_description:
                    print('snapshot_eval_description=' + exception_description.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))

            value = ((runtime_result.get('result') or {}).get('value') if isinstance(runtime_result, dict) else [])
            print('snapshot_value_type=' + type(value).__name__)

            snapshot_result_payload = {
                'source_url': '',
                'anchor_count': None,
                'data_node_count': None,
                'page_title': '',
                'ready_state': '',
                'login_hint': False,
                'shell_fallback_hint': False,
            }
            track_object_list = []

            if isinstance(value, dict):
                candidate_list = value.get('tracks')
                if isinstance(candidate_list, list):
                    track_object_list = [dict(item) for item in candidate_list if isinstance(item, dict)]
                snapshot_result_payload['source_url'] = str(value.get('source_url') or '').strip()
                snapshot_result_payload['anchor_count'] = value.get('anchor_count')
                snapshot_result_payload['data_node_count'] = value.get('data_node_count')
                snapshot_result_payload['page_title'] = str(value.get('page_title') or '').strip()
                snapshot_result_payload['ready_state'] = str(value.get('ready_state') or '').strip()
                snapshot_result_payload['login_hint'] = bool(value.get('login_hint'))
                snapshot_result_payload['shell_fallback_hint'] = bool(value.get('shell_fallback_hint'))
            elif isinstance(value, list):
                track_object_list = [dict(item) for item in value if isinstance(item, dict)]

            return track_object_list, snapshot_result_payload

        track_object_list, snapshot_result_payload = await load_snapshot_eval_result()
        recovery_attempted = False
        recovery_succeeded = False

        if (not track_object_list) and bool(snapshot_result_payload.get('shell_fallback_hint')) and target_url_hint:
            recovery_attempted = True
            recovery_url_candidates = [
                target_url_hint,
                'https://listen.tidal.com/browse/favorites/tracks',
                'https://listen.tidal.com/favorites/tracks',
                'https://listen.tidal.com/browse/collection/tracks',
            ]
            normalized_recovery_url_list = []
            for candidate_url in recovery_url_candidates:
                normalized_candidate_url = str(candidate_url or '').strip()
                if not normalized_candidate_url:
                    continue
                if normalized_candidate_url in normalized_recovery_url_list:
                    continue
                normalized_recovery_url_list.append(normalized_candidate_url)

            for recovery_url in normalized_recovery_url_list:
                await send_method('Page.navigate', {'url': recovery_url})
                await asyncio.sleep(1.8)
                track_object_list, snapshot_result_payload = await load_snapshot_eval_result()
                if track_object_list:
                    recovery_succeeded = True
                    break

        print('snapshot_recovery_attempted=' + ('true' if recovery_attempted else 'false'))
        print('snapshot_recovery_succeeded=' + ('true' if recovery_succeeded else 'false'))

        snapshot_source_url = str(snapshot_result_payload.get('source_url') or '').strip()
        snapshot_anchor_count = snapshot_result_payload.get('anchor_count')
        if isinstance(snapshot_anchor_count, (int, float, str)):
            try:
                print('snapshot_anchor_count=' + str(int(snapshot_anchor_count)))
            except Exception:
                pass
        snapshot_data_node_count = snapshot_result_payload.get('data_node_count')
        if isinstance(snapshot_data_node_count, (int, float, str)):
            try:
                print('snapshot_data_node_count=' + str(int(snapshot_data_node_count)))
            except Exception:
                pass
        snapshot_page_title = str(snapshot_result_payload.get('page_title') or '').strip()
        if snapshot_page_title:
            print('snapshot_page_title=' + snapshot_page_title.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))
        snapshot_ready_state = str(snapshot_result_payload.get('ready_state') or '').strip()
        if snapshot_ready_state:
            print('snapshot_ready_state=' + snapshot_ready_state.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))
        print('snapshot_login_hint=' + ('true' if bool(snapshot_result_payload.get('login_hint')) else 'false'))
        print('snapshot_shell_fallback_hint=' + ('true' if bool(snapshot_result_payload.get('shell_fallback_hint')) else 'false'))

        if snapshot_source_url:
            print('snapshot_source_url=' + snapshot_source_url)

        encoded_payload = base64.b64encode(
            json.dumps(track_object_list, ensure_ascii=False).encode('utf-8')
        ).decode('ascii')
        print('snapshot_count=' + str(len(track_object_list)))
        print('snapshot_json_base64=' + encoded_payload)
        return 0


try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_snapshot_exception:' + str(exc))
    raise SystemExit(13)
PY
'''
        return env_prefix + cdp_script + " "