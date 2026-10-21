# Ai_BA — AI Business Analyst

An analyst agent over a SQL database. Ask it a question, or ask it to build a sales report
with multiple dashboards. It explores the schema, writes and validates SQL, runs it safely, and
emits **declarative dashboard artifacts** rather than free-form HTML.

```
text_sql/
├── plan.md      # the full implementation plan — read this first
├── harness/     # Phase 1: the agent runtime (Python)
└── ui/          # Phase 2: the web client (React + TypeScript)
```

## The shape of it

```
you:   "Q3 sales report — revenue trend, top regions, top products"
agent: introspects schema → writes SQL → runs it safely → builds chart panels
you:   dashboard on screen, every panel traceable back to its SQL
```

Three ideas carry the design:

| Idea | Why it matters |
|---|---|
| **Result handles** — the transcript stores `tbl_a91f`, not 40k rows | Long multi-panel report sessions fit in context |
| **Charts as data** — `{chart_type, encodings, semantic_types}` | Small, editable, no generated HTML |
| **Panels as artifacts** — each written independently | One bad panel is retried, not the whole dashboard |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e harness
```

Configure the model endpoint and the database:

```bash
cp .env.example .env      # then fill it in
export TSQL_BASE_URL=...  TSQL_MODEL=...  TSQL_API_KEY=...
export TSQL_DB_DIALECT=sqlite
export TSQL_DB_PATH=./path/to/your.db
```

Then:

```bash
tsql doctor    # verify the endpoint and the database connection
tsql chat      # talk to the analyst
tsql serve     # the web UI on http://127.0.0.1:8000
```

### The web UI

```bash
cd ui && npm install && npm run build && cd ..
tsql --db-path /tmp/demo.db serve
```

Without a model endpoint, `python harness/examples/run_demo_server.py` runs
the harness with a scripted analyst so the UI can be tried offline.

## Safety

The agent cannot write to your database. Five layers:

1. **The database role is the guarantee** — read-only user, read-only transaction, statement timeout.
2. **AST guard** — every statement is parsed; only a single read-only `SELECT` over allow-listed
   tables survives, with a forced and capped `LIMIT`.
3. **Result guard** — row caps, byte caps, cell truncation, PII masking by role.
4. **Prompt-injection defence** — database text is treated as data, never as instructions.
5. **Approval + audit** — gated actions need a human; every decision is hash-chained.

`"only run SELECT"` in a prompt is a request, not a guardrail. The enforcement is in the
permissions and the parser.

## Status

See [`plan.md`](plan.md) for the phase-by-phase plan and current progress.

## License

MIT
