"""Shared helpers for the end-to-end tests.

These scripts drive the harness against the **real** model endpoint and check
the results against the **database**, not against a fixed expected string. A
model answer is only right if the number in it is in the database — that is the
whole point of the check, and it is how the ``SUM(amount * quantity)``
double-count bug was caught in the first place.

    python test/test_01_tool_calling.py       # one script
    bash test/run_all.sh                       # all of them

Every script exits non-zero on failure. Set ``TSQL_TEST_OFFLINE=1`` to skip
the parts that need the endpoint.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "harness"
CONFIG_PATH = Path(os.environ.get("TSQL_TEST_CONFIG", REPO / "config.local.json"))
DEMO_DB = Path(os.environ.get("TSQL_TEST_DB", "/tmp/demo.db"))
DEMO_STATE = Path(os.environ.get("TSQL_TEST_STATE", "/tmp/demo-e2e-state.db"))
VENV_PY = REPO / ".venv" / "bin" / "python"

OFFLINE = os.environ.get("TSQL_TEST_OFFLINE") == "1"

if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from tsql.config import Config  # noqa: E402
from tsql.harness import AnalystHarness  # noqa: E402

#: The figures the demo database actually contains.
TRUTH: dict[str, dict[str, float]] = {}


# --------------------------------------------------------------------- database
def ensure_demo_db() -> Path:
    """Create the demo database if it is not there yet."""
    if not DEMO_DB.exists():
        subprocess.run(
            [str(VENV_PY), str(HARNESS / "examples" / "make_demo_db.py"), str(DEMO_DB)],
            check=True,
            capture_output=True,
        )
    return DEMO_DB


def db() -> sqlite3.Connection:
    ensure_demo_db()
    connection = sqlite3.connect(DEMO_DB)
    connection.row_factory = sqlite3.Row
    return connection


def truth() -> dict[str, dict[str, float]]:
    """The aggregates the model is expected to reproduce, computed from SQLite.

    Deliberately written the *right* way: ``amount`` is already the line
    total, so revenue is ``SUM(amount)`` over the paid orders.
    """
    global TRUTH
    if TRUTH:
        return TRUTH
    connection = db()
    TRUTH = {
        "region": {
            row["region"]: row["revenue"]
            for row in connection.execute(
                "SELECT c.region AS region, SUM(o.amount) AS revenue "
                "FROM orders o JOIN customers c ON c.id = o.customer_id "
                "WHERE o.status = 'paid' GROUP BY c.region"
            )
        },
        "month": {
            row["month"]: row["revenue"]
            for row in connection.execute(
                "SELECT substr(ordered_at, 1, 7) AS month, SUM(amount) AS revenue "
                "FROM orders WHERE status = 'paid' GROUP BY 1"
            )
        },
        "product": {
            row["name"]: row["revenue"]
            for row in connection.execute(
                "SELECT p.name AS name, SUM(o.amount) AS revenue "
                "FROM orders o JOIN products p ON p.id = o.product_id "
                "WHERE o.status = 'paid' GROUP BY 1"
            )
        },
    }
    return TRUTH


def total_revenue() -> float:
    connection = db()
    row = connection.execute(
        "SELECT SUM(amount) FROM orders WHERE status = 'paid'"
    ).fetchone()
    return float(row[0])


# ---------------------------------------------------------------------- figures
FIGURE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")

#: A number that looks like money even without the sign: "6,330.0" or "2533.50".
#: Counts ("24 orders") and ratios ("3.5x") are deliberately not matched.
BARE_AMOUNT_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\b")


def figures(text: str) -> list[float]:
    """Every dollar figure in a piece of text, as floats."""
    out: list[float] = []
    for raw in FIGURE_RE.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def amounts(text: str) -> list[float]:
    """Every figure in a piece of text that could be an amount.

    A model writes the total as "$6,330.00" or as "6,330.0" depending on how
    it feels that day; both are amounts, and both have to be in the database.
    """
    out: list[float] = []
    for raw in FIGURE_RE.findall(text or "") + BARE_AMOUNT_RE.findall(text or ""):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if value >= 100 and not any(abs(value - seen) < 0.001 for seen in out):
            out.append(value)
    return out


def known_figures() -> set[float]:
    """Every figure that legitimately appears in this database.

    The revenue aggregates, plus the raw numbers a model might quote when it
    explains the data (a product price, a line total).
    """
    global TRUTH
    values: set[float] = {total_revenue()}
    for dimension in truth().values():
        values.update(round(v, 2) for v in dimension.values())
    connection = db()
    for query in (
        "SELECT price FROM products",
        "SELECT amount FROM orders",
        "SELECT quantity FROM orders",
    ):
        for row in connection.execute(query):
            values.add(round(float(row[0]), 2))
    return values


# ----------------------------------------------------------------- the harness
def config(state_db: Path = DEMO_STATE) -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"no endpoint config at {CONFIG_PATH}. "
            "copy config.local.json or set TSQL_TEST_CONFIG."
        )
    cfg = Config.load(CONFIG_PATH)
    cfg.db.path = str(DEMO_DB)
    cfg.state_db_path = str(state_db)
    return cfg


def reset_state_db(path: Path) -> None:
    """Remove a state database — and the sidecar files SQLite leaves behind.

    Deleting only the ``.db`` leaves a stale write-ahead log behind, and the
    next open fails with "disk I/O error".
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def make_harness(state_db: Path = DEMO_STATE) -> AnalystHarness:
    return AnalystHarness.create(config(state_db))


def provider_from_config() -> Any:
    from tsql.providers import build_provider

    return build_provider(config().model)


# -------------------------------------------------------------------- the API
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str, timeout: float = 20.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        # The server is not up yet.
        return 0, str(exc)


def post_sse(
    url: str, payload: dict, timeout: float = 900.0
) -> Iterator[tuple[str, dict]]:
    """POST to the chat endpoint and yield each SSE frame as (type, data)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buffer = ""
        while True:
            chunk = response.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                event_type = None
                data = ""
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data += line[6:]
                if event_type is None:
                    continue
                try:
                    parsed = json.loads(data) if data else {}
                except json.JSONDecodeError:
                    parsed = {"raw": data}
                yield event_type, parsed


# ------------------------------------------------------------------- artifacts
def artifacts(state_db: Path = DEMO_STATE) -> list[dict]:
    if not state_db.exists():
        return []
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute("SELECT id, kind, title, payload FROM artifacts ORDER BY id")
        )
    except sqlite3.OperationalError:
        # The database exists but has no schema yet.
        return []
    finally:
        connection.close()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = None
        out.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "payload": payload,
            }
        )
    return out


def dashboard_artifact(state_db: Path = DEMO_STATE) -> dict | None:
    for artifact in artifacts(state_db):
        if artifact["kind"] == "dashboard" and artifact["payload"]:
            return artifact
    return None


def build_dashboard(request: str, state_db: Path = DEMO_STATE) -> dict:
    """Ask the model for a dashboard and return the stored artifact."""
    harness = make_harness(state_db)
    try:
        result = harness.agent.run_conversation(request)
    finally:
        harness.close()
    if result.stop_reason != "completed":
        raise RuntimeError(f"the turn did not complete: {result.error}")
    dashboard = dashboard_artifact(state_db)
    if dashboard is None:
        raise RuntimeError("the model did not store a dashboard")
    return dashboard


def table_artifacts(state_db: Path = DEMO_STATE) -> dict[str, dict]:
    """Table artifacts, keyed by id, for resolving a panel's ``data_ref``."""
    return {a["id"]: a for a in artifacts(state_db) if a["kind"] == "table"}


# ---------------------------------------------------------------------- output
def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")
    print("-" * len(text))


def ok(text: str) -> None:
    print(f"  \033[32m✓\033[0m {text}")


def bad(text: str) -> None:
    print(f"  \033[31m✗\033[0m {text}")


def info(text: str) -> None:
    print(f"    {text}")


def skip(reason: str) -> None:
    print(f"  \033[33m—\033[0m skipped: {reason}")
