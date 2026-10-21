/**
 * ChartSpec -> Vega-Lite.
 *
 * The agent writes a compact *semantic* chart request; the compiler turns it
 * into a backend-native spec. The model supplies intent, the compiler
 * supplies design: which scale, whether the baseline is zero, how numbers are
 * formatted, how wide the chart is, when a bar chart should become a
 * horizontal one.
 *
 * The three-stage structure mirrors Microsoft Flint:
 *   1. frontend  - resolve channel semantics
 *   2. optimizer - fit the layout to the data
 *   3. codegen   - emit the native spec
 */

export type SemanticType =
  | "Quantity"
  | "Currency"
  | "Percent"
  | "YearMonth"
  | "Category"
  | "Date"
  | "Text";

export interface ChartSpec {
  chart_type: string;
  data_ref: string;
  encodings: { x?: string; y?: string; series?: string; size?: string };
  semantic_types: Record<string, SemanticType>;
  title?: string;
  insight?: string;
  layout?: { row?: number; col?: number; w?: number; h?: number };
}

export interface Row {
  [column: string]: unknown;
}

/** A minimal Vega-Lite spec type — we only use a fraction of the language. */
export interface VegaLiteSpec {
  $schema: string;
  title?: string;
  data: { values: Row[] };
  mark: Record<string, unknown>;
  encoding: Record<string, Record<string, unknown>>;
  width?: number;
  height?: number;
  autosize?: Record<string, unknown>;
  [key: string]: unknown;
}

const SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json";

/** Chart types that are not charts. The UI renders these itself. */
export const NON_CHART_TYPES = new Set(["table", "metric"]);

// ---------------------------------------------------------------- stage 1

interface Channel {
  field: string;
  type: "quantitative" | "nominal" | "ordinal" | "temporal";
  format?: string;
  zero?: boolean;
  title?: string;
}

function channelFor(field: string, semantic: SemanticType | undefined): Channel {
  switch (semantic) {
    case "Quantity":
      return { field, type: "quantitative", format: ",.0f", zero: true };
    case "Currency":
      return { field, type: "quantitative", format: "$,.0f", zero: true };
    case "Percent":
      return { field, type: "quantitative", format: ".1%", zero: false };
    case "Date":
      return { field, type: "temporal", format: "%b %d, %Y" };
    case "YearMonth":
      // "2024-01" is not ISO-8601, so Date.parse rejects it. An ordinal axis
      // with a natural string sort renders correctly and sorts correctly.
      return { field, type: "ordinal" };
    case "Text":
      return { field, type: "nominal" };
    case "Category":
    default:
      return { field, type: "nominal" };
  }
}

function semanticOf(spec: ChartSpec, field: string): SemanticType | undefined {
  return spec.semantic_types?.[field];
}

// ---------------------------------------------------------------- stage 2

interface Layout {
  width: number;
  height: number;
  orient: "vertical" | "horizontal";
}

function layoutFor(spec: ChartSpec, rows: Row[]): Layout {
  const x = spec.encodings.x;
  const categories = x ? rows.map((r) => String(r[x] ?? "")) : [];
  const longest = categories.reduce((max, c) => Math.max(max, c.length), 0);
  const count = categories.length;

  const horizontal =
    spec.chart_type === "hbar" ||
    (spec.chart_type === "bar" && (longest > 11 || count > 8));

  if (horizontal) {
    const width = Math.min(760, Math.max(260, longest * 8 + 140));
    const height = Math.min(560, Math.max(180, count * 26 + 60));
    return { width, height, orient: "horizontal" };
  }

  const width = Math.min(760, Math.max(240, count * 44 + 80));
  const height = 300;
  return { width, height, orient: "vertical" };
}

// ---------------------------------------------------------------- stage 3

function axisTitle(field: string): string {
  return field.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function compileToVegaLite(spec: ChartSpec, rows: Row[]): VegaLiteSpec {
  const { encodings } = spec;
  const layout = layoutFor(spec, rows);

  const xField = encodings.x;
  const yField = encodings.y;
  const seriesField = encodings.series;

  const x = xField ? channelFor(xField, semanticOf(spec, xField)) : undefined;
  const y = yField ? channelFor(yField, semanticOf(spec, yField)) : undefined;
  const series = seriesField
    ? channelFor(seriesField, semanticOf(spec, seriesField))
    : undefined;

  const encoding: Record<string, Record<string, unknown>> = {};

  if (x && y) {
    if (layout.orient === "horizontal") {
      // In a horizontal bar chart the roles swap: the measure goes on x.
      encoding.x = {
        field: y.field,
        type: y.type,
        ...(y.format ? { format: y.format } : {}),
        ...(y.zero !== undefined ? { scale: { zero: y.zero } } : {}),
        title: axisTitle(y.field),
      };
      encoding.y = {
        field: x.field,
        type: x.type,
        sort: "-x",
        title: axisTitle(x.field),
      };
    } else {
      encoding.x = {
        field: x.field,
        type: x.type,
        ...(x.type === "ordinal" ? { sort: x.field } : {}),
        title: axisTitle(x.field),
      };
      encoding.y = {
        field: y.field,
        type: y.type,
        ...(y.format ? { format: y.format } : {}),
        ...(y.zero !== undefined ? { scale: { zero: y.zero } } : {}),
        title: axisTitle(y.field),
      };
    }
  } else if (y) {
    // A metric: one number.
    encoding.y = {
      field: y.field,
      type: y.type,
      ...(y.format ? { format: y.format } : {}),
    };
  }

  if (series) {
    encoding.color = { field: series.field, type: "nominal", title: axisTitle(series.field) };
  }

  const stacked = spec.chart_type === "stacked_bar";
  if (stacked && encoding.y) {
    encoding.y.stack = "zero";
  }

  const markType: Record<string, string> = {
    bar: "bar",
    hbar: "bar",
    stacked_bar: "bar",
    line: "line",
    area: "area",
    scatter: "point",
    pie: "arc",
    donut: "arc",
    heatmap: "rect",
    waterfall: "bar",
    funnel: "bar",
    gauge: "bar",
    treemap: "rect",
  };

  const mark: Record<string, unknown> = { type: markType[spec.chart_type] ?? "bar" };
  if (spec.chart_type === "line") {
    mark.point = true;
    mark.strokeWidth = 2;
  }
  if (spec.chart_type === "area") {
    mark.opacity = 0.6;
  }
  if (spec.chart_type === "donut") {
    mark.innerRadius = 60;
  }
  if (spec.chart_type === "scatter") {
    mark.size = 80;
  }

  const out: VegaLiteSpec = {
    $schema: SCHEMA,
    data: { values: rows },
    mark,
    encoding,
    width: layout.width,
    height: layout.height,
    autosize: { type: "fit", contains: "padding" },
  };
  if (spec.title) out.title = spec.title;
  return out;
}

/** A short, human description of what the chart will show. */
export function describeSpec(spec: ChartSpec): string {
  const { encodings } = spec;
  const parts: string[] = [];
  if (encodings.x) parts.push(encodings.x);
  if (encodings.y) parts.push(encodings.y);
  if (encodings.series) parts.push(`by ${encodings.series}`);
  return `${spec.chart_type} · ${parts.join(" · ")}`;
}
