# text_sql — the web client

A minimal UI for the analyst harness: chat on the left, a live dashboard
canvas on the right.

The UI is a **renderer**. It never executes anything — it streams the
harness's events over SSE, reads the artifacts the agent produced, and draws
them.

## Run it

```bash
# 1. build the UI
cd ui
npm install
npm run build          # -> ui/dist

# 2. serve it with the harness
cd ..
source .venv/bin/activate
tsql --db-path /tmp/demo.db serve
# open http://127.0.0.1:8000
```

### Without a model endpoint

`harness/examples/run_demo_server.py` runs the harness with a scripted
analyst, so the UI can be tried before an endpoint is configured:

```bash
python harness/examples/run_demo_server.py --db /tmp/demo.db
```

### Development

```bash
cd ui
npm run dev            # vite on :5173, proxying /api to :8000
```

Run `tsql serve` in another terminal for the API.

## Layout

```
src/
├── api.ts                  # the harness API + the SSE client
├── App.tsx                 # the shell
├── charts/
│   ├── compile.ts          # ChartSpec -> Vega-Lite  ★
│   └── compile.test.ts
└── components/
    ├── ChatPanel.tsx       # the conversation
    ├── ArtifactCanvas.tsx  # dashboards, charts, result tables
    └── ChartRenderer.tsx   # one panel: a chart, a table, or a metric
```

## The chart compiler

`charts/compile.ts` turns the agent's compact chart spec into a Vega-Lite
spec. The agent supplies **intent**; the compiler supplies **design**:

| Semantic type | What the compiler decides |
|---|---|
| `Quantity` | zero baseline, `,.0f` format |
| `Currency` | zero baseline, `$,.0f` format |
| `Percent` | no forced zero, `.1%` format |
| `YearMonth` | an ordinal axis — `"2024-01"` is not ISO-8601, so it would fail to parse as a date |
| `Category` | a nominal axis, sorted by the measure |

It also decides the layout: how wide the chart is for the number of
categories, and when a bar chart should become a horizontal one (long labels,
or more than eight categories).

The shape deliberately mirrors Microsoft Flint's `ChartAssemblyInput`, so the
compiler can later be replaced with `assembleVegaLite()` without changing the
agent's output.

## Tests

```bash
npm test        # the chart compiler
npm run build   # typecheck + bundle
```
