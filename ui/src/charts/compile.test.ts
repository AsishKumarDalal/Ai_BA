import { describe, expect, it } from "vitest";
import { compileToVegaLite, type ChartSpec, type Row } from "./compile";

const rows: Row[] = [
  { region: "East", revenue: 1652.5 },
  { region: "West", revenue: 2533.5 },
  { region: "North", revenue: 868 },
];

function spec(overrides: Partial<ChartSpec> = {}): ChartSpec {
  return {
    chart_type: "bar",
    data_ref: "tbl_1",
    encodings: { x: "region", y: "revenue" },
    semantic_types: { region: "Category", revenue: "Currency" },
    ...overrides,
  };
}

describe("compileToVegaLite", () => {
  it("emits a bar chart with the right channels", () => {
    const out = compileToVegaLite(spec(), rows);
    expect(out.mark.type).toBe("bar");
    expect(out.encoding.x.field).toBe("region");
    expect(out.encoding.x.type).toBe("nominal");
    expect(out.encoding.y.field).toBe("revenue");
    expect(out.encoding.y.type).toBe("quantitative");
  });

  it("forces a zero baseline for a currency measure", () => {
    const out = compileToVegaLite(spec(), rows);
    expect(out.encoding.y.scale).toEqual({ zero: true });
  });

  it("does not force a zero baseline for a percent", () => {
    const out = compileToVegaLite(
      spec({
        encodings: { x: "region", y: "share" },
        semantic_types: { region: "Category", share: "Percent" },
      }),
      rows,
    );
    expect(out.encoding.y.scale).toEqual({ zero: false });
    expect(out.encoding.y.format).toBe(".1%");
  });

  it("formats a quantity with a thousands separator", () => {
    const out = compileToVegaLite(
      spec({
        encodings: { x: "region", y: "units" },
        semantic_types: { region: "Category", units: "Quantity" },
      }),
      rows,
    );
    expect(out.encoding.y.format).toBe(",.0f");
  });

  it("swaps the axes for a horizontal bar chart", () => {
    const out = compileToVegaLite(spec({ chart_type: "hbar" }), rows);
    expect(out.encoding.x.field).toBe("revenue");
    expect(out.encoding.y.field).toBe("region");
  });

  it("turns a bar chart horizontal when the labels are long", () => {
    const longLabels = [
      { region: "North America", revenue: 10 },
      { region: "South America", revenue: 20 },
    ];
    const out = compileToVegaLite(spec(), longLabels);
    expect(out.encoding.x.field).toBe("revenue");
  });

  it("keeps a bar chart vertical when the labels are short", () => {
    const out = compileToVegaLite(spec(), rows);
    expect(out.encoding.x.field).toBe("region");
  });

  it("treats a year-month field as ordinal so it sorts and renders", () => {
    const months = [
      { month: "2024-01", revenue: 10 },
      { month: "2024-02", revenue: 20 },
    ];
    const out = compileToVegaLite(
      spec({
        chart_type: "line",
        encodings: { x: "month", y: "revenue" },
        semantic_types: { month: "YearMonth", revenue: "Currency" },
      }),
      months,
    );
    expect(out.encoding.x.type).toBe("ordinal");
    expect(out.encoding.x.sort).toBe("month");
    expect(out.mark.type).toBe("line");
  });

  it("adds a colour channel for a series", () => {
    const out = compileToVegaLite(
      spec({ encodings: { x: "region", y: "revenue", series: "segment" } }),
      rows,
    );
    expect(out.encoding.color.field).toBe("segment");
  });

  it("stacks a stacked bar chart", () => {
    const out = compileToVegaLite(spec({ chart_type: "stacked_bar" }), rows);
    expect(out.encoding.y.stack).toBe("zero");
  });

  it("gives a donut an inner radius", () => {
    const out = compileToVegaLite(spec({ chart_type: "donut" }), rows);
    expect(out.mark.innerRadius).toBe(60);
  });

  it("carries the title through", () => {
    const out = compileToVegaLite(spec({ title: "Revenue by Region" }), rows);
    expect(out.title).toBe("Revenue by Region");
  });

  it("sizes the chart to the number of categories", () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      region: `R${i}`,
      revenue: i,
    }));
    const wide = compileToVegaLite(spec(), many);
    const narrow = compileToVegaLite(spec(), rows);
    expect(wide.width!).toBeGreaterThan(narrow.width!);
    expect(wide.width!).toBeLessThanOrEqual(760);
  });

  it("passes the rows through to the spec", () => {
    const out = compileToVegaLite(spec(), rows);
    expect(out.data.values).toBe(rows);
  });
});
