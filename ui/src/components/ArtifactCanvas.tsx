import { useEffect, useState } from "react";
import type { Artifact, DashboardPayload, Panel, TablePayload } from "../api";
import { api } from "../api";
import { ChartRenderer } from "./ChartRenderer";
import type { Row } from "../charts/compile";

interface Props {
  artifacts: Artifact[];
}

/** The artifact canvas: dashboards, charts and result tables. */
export function ArtifactCanvas({ artifacts }: Props) {
  const dashboards = artifacts.filter((a) => a.kind === "dashboard");
  const charts = artifacts.filter((a) => a.kind === "chart");
  const tables = artifacts.filter((a) => a.kind === "table");

  if (!artifacts.length) {
    return (
      <div className="empty">
        <h3>Nothing here yet</h3>
        <p>
          Ask a question, or ask for a report. The analyst's charts and result
          tables appear here as it works.
        </p>
      </div>
    );
  }

  return (
    <>
      {dashboards.map((artifact) => (
        <DashboardCard key={artifact.id} artifact={artifact} />
      ))}
      {charts.length > 0 && (
        <div className="canvas-head" style={{ marginTop: 18 }}>
          <h2>Charts</h2>
        </div>
      )}
      {charts.map((artifact) => (
        <ChartCard key={artifact.id} artifact={artifact} />
      ))}
      {tables.length > 0 && (
        <div className="canvas-head" style={{ marginTop: 18 }}>
          <h2>Query results</h2>
        </div>
      )}
      {tables.map((artifact) => (
        <TableCard key={artifact.id} artifact={artifact} />
      ))}
    </>
  );
}

function DashboardCard({ artifact }: { artifact: Artifact }) {
  const payload = artifact.payload as DashboardPayload;
  const panels = payload?.panels ?? [];

  return (
    <section style={{ marginBottom: 22 }}>
      <div className="canvas-head">
        <h2>{payload?.title || artifact.title || "Dashboard"}</h2>
        <span className="sub">
          {panels.length} panel{panels.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="grid">
        {panels.map((panel) => (
          <PanelCard key={panel.id} panel={panel} />
        ))}
      </div>
      {payload?.narrative && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="card-head">
            <h3>Summary</h3>
          </div>
          <div className="card-body" style={{ whiteSpace: "pre-wrap" }}>
            {payload.narrative}
          </div>
          {payload.key_findings?.length > 0 && (
            <ul className="insight" style={{ listStyle: "disc", paddingLeft: 28 }}>
              {payload.key_findings.map((finding, index) => (
                <li key={index}>{finding}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function PanelCard({ panel }: { panel: Panel }) {
  const [result, setResult] = useState<TablePayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .artifact(panel.data_ref)
      .then(({ artifact }) => {
        if (!cancelled) setResult(artifact.payload as TablePayload);
      })
      .catch(() => {
        if (!cancelled) setResult(null);
      });
    return () => {
      cancelled = true;
    };
  }, [panel.data_ref]);

  const columns = result?.columns?.map((c) => c.name) ?? [];
  const rows: Row[] = (result?.rows ?? []).map((row) => {
    const object: Row = {};
    columns.forEach((column, index) => {
      object[column] = row[index];
    });
    return object;
  });

  return (
    <article className="card">
      <div className="card-head">
        <h3>{panel.title}</h3>
        <span className="kind">{panel.chart_type}</span>
      </div>
      <div className="card-body chart">
        {result ? (
          <ChartRenderer spec={panel} rows={rows} columns={columns} />
        ) : (
          <span className="spinner" />
        )}
      </div>
      {panel.insight && <div className="insight">{panel.insight}</div>}
    </article>
  );
}

function ChartCard({ artifact }: { artifact: Artifact }) {
  const [result, setResult] = useState<TablePayload | null>(null);
  const spec = artifact.payload as Panel;

  useEffect(() => {
    let cancelled = false;
    api
      .artifact(spec.data_ref)
      .then(({ artifact: found }) => {
        if (!cancelled) setResult(found.payload as TablePayload);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [spec.data_ref]);

  const columns = result?.columns?.map((c) => c.name) ?? [];
  const rows: Row[] = (result?.rows ?? []).map((row) => {
    const object: Row = {};
    columns.forEach((column, index) => {
      object[column] = row[index];
    });
    return object;
  });

  return (
    <article className="card" style={{ marginBottom: 14 }}>
      <div className="card-head">
        <h3>{artifact.title || spec.title}</h3>
        <span className="kind">{spec.chart_type}</span>
      </div>
      <div className="card-body chart">
        {result ? (
          <ChartRenderer spec={spec} rows={rows} columns={columns} />
        ) : (
          <span className="spinner" />
        )}
      </div>
    </article>
  );
}

function TableCard({ artifact }: { artifact: Artifact }) {
  const [result, setResult] = useState<TablePayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .artifact(artifact.id)
      .then(({ artifact: found }) => {
        if (!cancelled) setResult(found.payload as TablePayload);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [artifact.id]);

  if (!result) return null;
  const columns = result.columns?.map((c) => c.name) ?? [];
  const rows: Row[] = (result.rows ?? []).map((row) => {
    const object: Row = {};
    columns.forEach((column, index) => {
      object[column] = row[index];
    });
    return object;
  });

  return (
    <article className="card" style={{ marginBottom: 14 }}>
      <div className="card-head">
        <h3>{artifact.title || "Result"}</h3>
        <span className="kind">{result.row_count} rows</span>
      </div>
      <div className="card-body">
        <ChartRenderer
          spec={{ chart_type: "table", data_ref: artifact.id, encodings: {}, semantic_types: {} }}
          rows={rows}
          columns={columns}
        />
      </div>
      {typeof artifact.meta?.sql === "string" && (
        <div className="sql">{artifact.meta.sql}</div>
      )}
    </article>
  );
}
