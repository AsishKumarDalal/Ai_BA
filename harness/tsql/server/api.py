"""The HTTP + SSE API that the web UI talks to.

Standard library only — no web framework dependency. The UI is a renderer:
it streams events, reads artifacts, and renders them. It never executes
anything itself.

    POST /api/chat            -> Server-Sent Events: the agent's turn, live
    GET  /api/sessions        -> past sessions
    GET  /api/sessions/{id}   -> one session's transcript
    GET  /api/artifacts       -> a session's artifacts
    GET  /api/artifacts/{id}  -> one artifact, with its data
    GET  /api/catalog         -> the connected schema
    GET  /api/health          -> resolved configuration
    GET  /                     -> the built UI
"""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..agent.events import AgentEvent
from ..harness import AnalystHarness

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class AnalystServer:
    """Serves the UI and the agent API."""

    def __init__(
        self,
        harness: AnalystHarness,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        ui_dir: str | Path | None = None,
    ):
        self.harness = harness
        self.host = host
        self.port = port
        self.ui_dir = Path(ui_dir).expanduser() if ui_dir else None
        self._httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ serve
    def serve_forever(self) -> None:
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        print(f"text_sql serving on http://{self.host}:{self.port}")
        if self.ui_dir and (self.ui_dir / "index.html").exists():
            print(f"  ui: {self.ui_dir}")
        else:
            print("  ui: not built — run `npm run build` in ui/, or use the dev server")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        # Clear the reference first: shutdown() may be called twice (the
        # serve_forever loop and the owner), and shutdown() blocks.
        self._httpd = None
        httpd.shutdown()
        httpd.server_close()

    # ---------------------------------------------------------------- handler
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # ---------------------------------------------------------- helpers
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # quiet by default; the CLI owns the output

            def _send_json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _send_error_json(self, message: str, status: int = 400) -> None:
                self._send_json({"error": message}, status=status)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}

            # ------------------------------------------------------------ GET
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path == "/api/health":
                    self._send_json(server.harness.describe())
                    return
                if path == "/api/catalog":
                    self._send_json({
                        "label": server.harness.catalog.label,
                        "dialect": server.harness.db.dialect,
                        "read_only": server.harness.db.read_only,
                        "tables": [
                            {"name": t.name, "kind": t.kind, "row_estimate": t.row_estimate,
                             "columns": t.columns}
                            for t in server.harness.catalog.tables
                        ],
                    })
                    return
                if path == "/api/sessions":
                    limit = int((query.get("limit") or ["50"])[0])
                    self._send_json({"sessions": server.harness.state.list_sessions(limit=limit)})
                    return
                if path.startswith("/api/sessions/"):
                    session_id = path.rsplit("/", 1)[-1]
                    if path.endswith("/messages"):
                        messages = server.harness.state.get_messages(session_id)
                        self._send_json({"messages": messages})
                        return
                    session = server.harness.state.get_session(session_id)
                    if not session:
                        self._send_error_json("session not found", 404)
                        return
                    self._send_json({"session": session})
                    return
                if path == "/api/artifacts":
                    session_id = (query.get("session") or [server.harness.session_id])[0]
                    artifacts = server.harness.state.list_artifacts(session_id)
                    self._send_json({"artifacts": artifacts})
                    return
                if path.startswith("/api/artifacts/"):
                    artifact_id = path.rsplit("/", 1)[-1]
                    artifact = server.harness.state.get_artifact(artifact_id)
                    if not artifact:
                        self._send_error_json("artifact not found", 404)
                        return
                    self._send_json({"artifact": artifact})
                    return
                if path == "/api/audit":
                    entries = server.harness.state.list_audit(limit=200)
                    verification = server.harness.state.verify_audit_chain()
                    self._send_json({"entries": entries, "verification": verification})
                    return

                self._serve_static(path)

            # ----------------------------------------------------------- POST
            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/api/chat":
                    self._handle_chat()
                    return
                self._send_error_json("not found", 404)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            # ------------------------------------------------------------ chat
            def _handle_chat(self) -> None:
                payload = self._read_json()
                message = str(payload.get("message") or "").strip()
                if not message:
                    self._send_error_json("message is required")
                    return

                session_id = payload.get("session_id")
                if session_id:
                    server.harness.resume_session(session_id)

                events: queue.Queue = queue.Queue()
                done: queue.Queue = queue.Queue()

                def on_event(event: AgentEvent) -> None:
                    events.put(event)

                def run() -> None:
                    try:
                        result = server.harness.agent.run_conversation(message, task_id=None)
                        events.put(AgentEvent(type="result", data=result.to_dict()))
                    except Exception as exc:  # noqa: BLE001
                        events.put(AgentEvent(type="error", data={
                            "message": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(limit=6),
                        }))
                    finally:
                        done.put(True)

                # Subscribe to the agent's events for the duration of this turn.
                server.harness.add_event_listener(on_event)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                # The response has no Content-Length, so the client reads until
                # EOF. Close the connection when the turn is done.
                self.close_connection = True

                worker = threading.Thread(target=run, daemon=True)
                worker.start()

                try:
                    while True:
                        try:
                            event = events.get(timeout=0.25)
                        except queue.Empty:
                            if done.empty():
                                continue
                            break
                        self._write_sse(event)
                        if event.type == "result":
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    server.harness.remove_event_listener(on_event)
                    worker.join(timeout=0.1)

            def _write_sse(self, event: AgentEvent) -> None:
                frame = f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()

            # ---------------------------------------------------------- static
            def _serve_static(self, path: str) -> None:
                ui_dir = server.ui_dir
                if not ui_dir or not ui_dir.exists():
                    self._send_error_json(
                        "the ui is not built. run `npm install && npm run build` in ui/",
                        404,
                    )
                    return

                relative = path.lstrip("/") or "index.html"
                target = (ui_dir / relative).resolve()
                # Path traversal guard.
                if not str(target).startswith(str(ui_dir.resolve())):
                    self._send_error_json("forbidden", 403)
                    return
                if target.is_dir():
                    target = target / "index.html"
                if not target.exists():
                    # Single-page app: unknown paths fall back to index.html.
                    target = ui_dir / "index.html"
                    if not target.exists():
                        self._send_error_json("not found", 404)
                        return

                content_type, _ = mimetypes.guess_type(str(target))
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def serve(harness: AnalystHarness, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          ui_dir: str | Path | None = None) -> None:
    AnalystServer(harness, host=host, port=port, ui_dir=ui_dir).serve_forever()
