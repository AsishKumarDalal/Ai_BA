import { describe, expect, it } from "vitest";
import { rowsAsObjects } from "./rows";

describe("rowsAsObjects", () => {
  it("zips the column names back onto the rows", () => {
    const rows = rowsAsObjects(["region", "revenue"], [
      ["West", 2533.5],
      ["North", 868],
    ]);
    expect(rows).toEqual([
      { region: "West", revenue: 2533.5 },
      { region: "North", revenue: 868 },
    ]);
  });

  it("keeps a null cell as a null", () => {
    const rows = rowsAsObjects(["name", "note"], [["Acme", null]]);
    expect(rows[0].note).toBeNull();
  });

  it("ignores extra columns the table does not have", () => {
    const rows = rowsAsObjects(["a"], [[1, 2, 3]]);
    expect(Object.keys(rows[0])).toEqual(["a"]);
  });

  it("survives a table with no rows", () => {
    expect(rowsAsObjects(["a"], [])).toEqual([]);
  });
});
