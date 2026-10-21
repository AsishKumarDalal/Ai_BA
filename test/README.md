# End-to-end tests

These scripts drive the **real** system against the **live model endpoint** and
check the results against the **database**.

That is the point. A model answer is only right if the number in it is in the
database — so the scripts compute the truth from SQLite and compare. It is how
the `SUM(amount * quantity)` double-count was caught in the first place, and
it is how a lost dashboard panel was found.

```bash
bash test/run_all.sh              # everything, about four minutes
.venv/bin/python test/test_02_dashboard.py   # one script
```

Every script is a `unittest` file and can be run on its own. They exit
non-zero on failure.

Set `TSQL_TEST_OFFLINE=1` to skip the parts that need the endpoint (the
offline checks still run).

## What each one covers

| script | what it proves |
|---|---|
| `test_01_tool_calling.py` | the endpoint returns usable `tool_calls`; a real question goes through the whole harness and the number in the answer is the number in the database |
| `test_02_dashboard.py` | the model builds three panels; each panel's data is real, the revenue panels add up to the revenue in the database, and every dollar figure in the insights exists in the database |
| `test_03_stall_recovery.py` | the "Let me take a look…" stall is recovered from; a truncated reply is an error, not an answer; live, the same question comes back answered |
| `test_04_server_api.py` | the server the UI talks to: the UI is served, the health and catalog are right, a turn streams over SSE, and the dashboard reaches the canvas |
| `test_05_ui_charts.py` | the live panels compile through the real UI compiler into valid Vega-Lite, with the right data and the right design rules |

## The checks that matter

**Figures are checked against the database.** `helpers.known_figures()` is the
set of numbers that legitimately exist in the demo database. A figure in an
answer that is not in that set is a failure — the model believed its own wrong
number once, and said so in a chart insight.

**Panels are checked against each other.** The revenue panels must add up to
the total paid revenue. A panel that does not is double-counted or missing a
filter.

**The stream is checked as it arrives.** A turn has to start, call tools,
answer, and end with a `result` event. The server closes the connection when
the turn is done, and the script checks that it does.

## Requirements

* the endpoint configuration in `config.local.json` (or `TSQL_TEST_CONFIG`)
* the demo database — `harness/examples/make_demo_db.py /tmp/demo.db`; the
  scripts create it if it is missing
* `ui/node_modules` for the chart compiler test
