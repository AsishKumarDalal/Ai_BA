import { useEffect, useRef } from "react";
import type { ChartSpec, Row, VegaLiteSpec } from "../charts/compile";
import { compileToVegaLite } from "../charts/compile";

interface Props {
  spec: ChartSpec;
  rows: Row[];
  columns: string[];
}

/** Renders one panel: a chart, a table, or a big number. */
export function ChartRenderer({ spec, rows, columns }: Props) {
  if (spec.chart_type === "table") {
    return <DataTable columns={columns} rows={rows} />;
  }
  if (spec.chart_type === "metric") {
    return <Metric spec={spec} rows={rows} />;
  }
  return <VegaChart spec={spec} rows={rows} />;
}

function VegaChart({ spec, rows }: { spec: ChartSpec; rows: Row[] }) {
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    const node = container.current;
    if (!node) return;

    const vegaSpec: VegaLiteSpec = compileToVegaLite(spec, rows);
    node.innerHTML = "";

    // Vega is ~1MB. It is loaded on demand, so a session with no chart never
    // pays for it.
    void import("vega-embed")
      .then(({ default: vegaEmbed }) => {
        if (cancelled) return null;
        return vegaEmbed(node, vegaSpec as never, {
          actions: false,
          renderer: "svg",
          config: {
            background: "transparent",
            view: { stroke: "transparent" },
            axis: {
              labelColor: "#98a1b3",
              titleColor: "#98a1b3",
              gridColor: "#232833",
              domainColor: "#2a2f3a",
              tickColor: "#2a2f3a",
            },
            legend: { labelColor: "#98a1b3", titleColor: "#98a1b3" },
            title: { color: "#e6e9ef", fontSize: 13, anchor: "start" },
            range: {
              category: ["#6ea8fe", "#5fd08a", "#f0c674", "#f2777a", "#c792ea", "#4dd0e1"],
            },
          },
        });
      })
      .then((result) => {
        if (!result || cancelled) {
          result?.view.finalize();
          return;
        }
        result.view.resize();
      })
      .catch((error) => {
        if (!cancelled) {
          node.innerHTML = `<div class="note">chart failed: ${String(error)}</div>`;
        }
      });

    return () => {
      cancelled = true;
    };
  }, [spec, rows]);

  return <div ref={container} style={{ width: "100%", overflow: "auto" }} />;
}

function Metric({ spec, rows }: { spec: ChartSpec; rows: Row[] }) {
  const field = spec.encodings.y ?? spec.encodings.x;
  const first = rows[0];
  const raw = field && first ? first[field] : undefined;
  const semantic = field ? spec.semantic_types?.[field] : undefined;
  const value = typeof raw === "number" ? formatValue(raw, semantic) : String(raw ?? "—");
  return (
    <div className="metric">
      <div className="value">{value}</div>
      {spec.title && <div className="label">{spec.title}</div>}
    </div>
  );
}

function formatValue(value: number, semantic?: string): string {
  switch (semantic) {
    case "Currency":
      return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    case "Percent":
      return `${(value * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
    default:
      return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
}

function DataTable({ columns, rows }: { columns: string[]; rows: Row[] }) {
  return (
    <div style={{ maxHeight: 320, overflow: "auto" }}>
      <table className="data">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{formatCell(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return String(value);
}
