"""The ``tsql`` command line.

    tsql doctor                 verify the endpoint and the database
    tsql chat                   talk to the analyst
    tsql ask "question"         one question, one answer
    tsql schema                 print the connected schema
    tsql sessions               list past sessions
    tsql resume <session-id>    continue a past session
    tsql artifacts [session]    list the artifacts a session produced
    tsql audit                  show and verify the audit chain
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agent.events import AgentEvent, EventType
from .config import Config
from .errors import TsqlError
from .harness import AnalystHarness
from .providers.base import ProviderRequest
from .tools.approval import ApprovalRequest

# --------------------------------------------------------------------------- ui

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _use_colour() -> bool:
    return sys.stdout.isatty()


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _use_colour() else text


def _print_header(title: str) -> None:
    print()
    print(_paint(title, BOLD))
    print(_paint("-" * len(title), DIM))


def _print_kv(key: str, value: Any) -> None:
    print(f"  {_paint(key + ':', DIM):<28} {value}")


# ------------------------------------------------------------------- rendering


class ConsoleRenderer:
    """Renders agent events to the terminal."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def __call__(self, event: AgentEvent) -> None:
        if event.type == EventType.TOOL_START:
            data = event.data
            args = data.get("args") or {}
            detail = ", ".join(f"{k}={_short(v)}" for k, v in list(args.items())[:3])
            print(f"  {_paint('▸', CYAN)} {data.get('name')} {DIM if False else ''}"
                  f"{_paint(detail, DIM)}")
        elif event.type == EventType.TOOL_END and self.verbose:
            print(f"    {_paint(data_preview(event.data), DIM)}")
        elif event.type == EventType.TOOL_ERROR:
            print(f"    {_paint('error:', RED)} {event.data.get('preview', '')[:300]}")
        elif event.type == EventType.ARTIFACT_CREATED:
            print(f"  {_paint('◆', GREEN)} panel {event.data.get('panel_id')} "
                  f"{event.data.get('title')}")
        elif event.type == EventType.CONTEXT_COMPRESSION:
            print(f"  {_paint('⟳', YELLOW)} compressed "
                  f"{event.data.get('messages_before')} → {event.data.get('messages_after')} "
                  f"messages")
        elif event.type == EventType.NUDGE:
            print(f"  {_paint('↻', YELLOW)} the model described what it was about to "
                  f"do and stopped — asking it to act")


def _short(value: Any, limit: int = 60) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def data_preview(data: dict) -> str:
    return str(data.get("preview", ""))[:200]


# ------------------------------------------------------------------- commands


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    if getattr(args, "db_path", None):
        config.db.path = args.db_path
    if getattr(args, "db_dialect", None):
        config.db.dialect = args.db_dialect
    if getattr(args, "model", None):
        config.model.model = args.model
    if getattr(args, "base_url", None):
        config.model.base_url = args.base_url
    if getattr(args, "api_key", None):
        config.model.api_key = args.api_key
    if getattr(args, "state_db", None):
        config.state_db_path = args.state_db
    return config


def _approval_callback(request: ApprovalRequest) -> str:
    """Ask a human on the terminal. An empty answer is a denial."""
    if not sys.stdin.isatty():
        print(f"  {_paint('approval required:', YELLOW)} {request.reason}")
        print(f"    {request.reason}: {json.dumps(request.detail)[:200]}")
        print("    no interactive channel — denying")
        return "deny"
    print()
    print(f"  {_paint('⚠ approval required', YELLOW)} — {request.reason}")
    for key, value in request.detail.items():
        print(f"    {key}: {value}")
    try:
        answer = input("    approve? [y]es / [n]o / [a]lways / [d]eny > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "deny"
    if answer in ("a", "always"):
        return "always"
    if answer in ("y", "yes", "ok", "approve"):
        return "approve"
    return "deny"


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load_config(args)
    print()
    print(_paint("text_sql doctor", BOLD))
    print(_paint("=" * 16, DIM))

    _print_header("model")
    _print_kv("provider", config.model.provider)
    _print_kv("model", config.model.model or _paint("(not set)", YELLOW))
    _print_kv("base url", config.model.base_url or "(default)")
    _print_kv("api key", "set" if config.model.api_key else _paint("not set", YELLOW))
    _print_kv("context length", config.model.resolved_context_length())

    ok = True
    model_ok = True
    if config.model.model and config.model.api_key:
        try:
            from .providers import build_provider

            provider = build_provider(config.model)
            response = provider.complete(
                ProviderRequest(
                    messages=[{"role": "user", "content": "Reply with the single word OK."}],
                    model=config.model.model,
                    # A reasoning model spends the first part of its budget
                    # thinking. Ask for enough to leave room for the answer.
                    max_tokens=min(1024, max(256, config.model.max_tokens // 4)),
                    temperature=0.0,
                )
            )
            answer = (response.text or "").strip()
            if answer:
                print(f"  {_paint('✓', GREEN)} live round trip: {answer[:60]!r}")
            elif response.reasoning:
                print(f"  {_paint('!', YELLOW)} the model replied but spent its whole "
                      f"budget thinking — raise max_tokens")
            else:
                print(f"  {_paint('✗', RED)} the endpoint answered with no content "
                      f"(finish_reason: {response.finish_reason})")
                ok = False
                model_ok = False
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            print(f"  {_paint('✗', RED)} round trip failed: {exc}")
            ok = False
            model_ok = False
    else:
        print(f"  {_paint('!', YELLOW)} set TSQL_MODEL and TSQL_API_KEY to test the endpoint")
        model_ok = False

    _print_header("database")
    try:
        from .db.connection import connect_database

        db = connect_database(config.db)
        tables = db.list_tables()
        _print_kv("dialect", db.dialect)
        _print_kv("path", config.db.path or config.db.database)
        _print_kv("read only", db.read_only)
        _print_kv("tables", len(tables))
        for table in tables[:12]:
            _print_kv(f"  {table['name']}", f"{table['kind']}, ~{table['row_estimate']:,} rows")
        db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  {_paint('✗', RED)} {exc}")
        ok = False

    _print_header("state")
    _print_kv("database", config.resolved_state_db_path)

    print()
    if ok and model_ok:
        print(_paint("all checks passed", GREEN))
    elif ok:
        print(_paint("the database is ready; the model endpoint is not configured yet", YELLOW))
    else:
        print(_paint("some checks failed — see above", YELLOW))
    return 0 if ok else 1


def cmd_schema(args: argparse.Namespace) -> int:
    config = _load_config(args)
    from .db.catalog import CatalogBuilder
    from .db.connection import connect_database

    db = connect_database(config.db)
    catalog = CatalogBuilder(db).get()
    print()
    print(_paint(catalog.label or config.db.path, BOLD))
    print()
    print(catalog.to_prompt_block())
    print()
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    config = _load_config(args)
    from .state.store import SessionDB

    state = SessionDB(config.resolved_state_db_path)
    sessions = state.list_sessions(limit=args.limit)
    print()
    if not sessions:
        print("  no sessions yet")
        return 0
    print(f"  {_paint('SESSION', DIM):<26} {'MESSAGES':>8}  {'STARTED':<20} TITLE")
    for session in sessions:
        started = _format_time(session["started_at"])
        print(f"  {session['id']:<26} {session.get('active_messages', 0):>8}  "
              f"{started:<20} {session.get('title') or session.get('preview') or ''}")
    print()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    config = _load_config(args)
    renderer = ConsoleRenderer(verbose=args.verbose)
    with AnalystHarness.create(
        config, on_event=renderer, approval_callback=_approval_callback
    ) as harness:
        if args.session:
            harness.resume_session(args.session)
        result = harness.ask(" ".join(args.question))
        if result.error:
            print(_paint(f"\nerror: {result.error}", RED))
            return 1
        print()
        print(result.final_response)
        return 0


def cmd_chat(args: argparse.Namespace) -> int:
    config = _load_config(args)
    renderer = ConsoleRenderer(verbose=args.verbose)
    with AnalystHarness.create(
        config, on_event=renderer, approval_callback=_approval_callback
    ) as harness:
        print()
        print(_paint("text_sql — the analyst", BOLD))
        print(_paint(f"database: {harness.catalog.label}  "
                     f"({len(harness.catalog.table_names())} tables, read-only)", DIM))
        print(_paint("Ask a question, or ask for a report. Ctrl-D to quit.", DIM))
        print()
        while True:
            try:
                question = input(_paint("you › ", CYAN)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            if question in ("/quit", "/exit", "/q"):
                break
            try:
                result = harness.ask(question)
            except TsqlError as exc:
                print(_paint(f"error: {exc.message}", RED))
                continue
            print()
            print(_paint("analyst ›", GREEN))
            print(result.final_response or _paint("(no answer)", YELLOW))
            print()
    return 0


def cmd_artifacts(args: argparse.Namespace) -> int:
    config = _load_config(args)
    from .state.store import SessionDB

    state = SessionDB(config.resolved_state_db_path)
    session_id = args.session
    if not session_id:
        sessions = state.list_sessions(limit=1)
        if not sessions:
            print("  no sessions yet")
            return 0
        session_id = sessions[0]["id"]
    artifacts = state.list_artifacts(session_id)
    print()
    if not artifacts:
        print("  no artifacts")
        return 0
    for artifact in artifacts:
        print(f"  {_paint(artifact['id'], CYAN):<28} {artifact['kind']:<10} "
              f"{artifact.get('title') or ''}")
    print()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    config = _load_config(args)
    from .state.store import SessionDB

    state = SessionDB(config.resolved_state_db_path)
    verification = state.verify_audit_chain()
    entries = state.list_audit(limit=args.limit)
    print()
    print(_paint("audit chain", BOLD))
    status = (_paint("valid", GREEN) if verification["valid"]
              else _paint(f"BROKEN at id {verification.get('broken_at_id')}", RED))
    print(f"  {verification.get('checked', 0)} entries — {status}")
    print()
    for entry in reversed(entries):
        detail = json.dumps(entry["detail"])
        print(f"  {_paint(entry['event'], DIM):<20} {detail[:120]}")
    print()
    return 0 if verification["valid"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(args)
    from .server.api import AnalystServer

    ui_dir = args.ui_dir or _default_ui_dir()
    with AnalystHarness.create(config, approval_callback=_approval_callback) as harness:
        server = AnalystServer(harness, host=args.host, port=args.port, ui_dir=ui_dir)
        try:
            server.serve_forever()
        finally:
            harness.close()
    return 0


def _default_ui_dir() -> str | None:
    """The built UI, if it has been built.

    The UI lives at the repository root (``ui/``), but a copy inside
    ``harness/ui/`` is also accepted so the harness can be packaged alone.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "ui" / "dist",   # <repo>/ui/dist
        here.parents[2] / "ui" / "dist",   # <repo>/harness/ui/dist
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------- utils


def _format_time(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsql",
        description="An analyst agent over your SQL database.",
    )
    parser.add_argument("--config", help="path to a config file (JSON or YAML)")
    parser.add_argument("--db-path", dest="db_path", help="override the database path")
    parser.add_argument("--db-dialect", dest="db_dialect",
                        choices=["sqlite", "duckdb", "postgres", "mysql"])
    parser.add_argument("--model", help="override the model id")
    parser.add_argument("--base-url", dest="base_url", help="override the endpoint base URL")
    parser.add_argument("--api-key", dest="api_key", help="override the API key")
    parser.add_argument("--state-db", dest="state_db", help="override the state database path")
    parser.add_argument("-v", "--verbose", action="store_true", help="show tool output")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="verify the endpoint and the database").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("schema", help="print the connected schema").set_defaults(func=cmd_schema)
    sub.add_parser("chat", help="talk to the analyst").set_defaults(func=cmd_chat)
    sub.add_parser("sessions", help="list past sessions").set_defaults(func=cmd_sessions)
    sub.add_parser("artifacts", help="list a session's artifacts").set_defaults(
        func=cmd_artifacts
    )

    ask = sub.add_parser("ask", help="ask one question")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--session", help="continue an existing session")
    ask.set_defaults(func=cmd_ask)

    resume = sub.add_parser("resume", help="continue a past session")
    resume.add_argument("session_id")
    resume.set_defaults(func=cmd_chat)

    audit = sub.add_parser("audit", help="show and verify the audit chain")
    audit.add_argument("--limit", type=int, default=30)
    audit.set_defaults(func=cmd_audit)

    serve = sub.add_parser("serve", help="serve the web UI and the agent API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--ui-dir", dest="ui_dir", help="the built ui directory")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "resume":
        args.session = args.session_id
    try:
        return int(args.func(args))
    except TsqlError as exc:
        print(_paint(f"error: {exc.message}", RED), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(_paint(f"error: {exc.filename} not found", RED), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI should not dump a traceback
        print(_paint(f"error: {type(exc).__name__}: {exc}", RED), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
