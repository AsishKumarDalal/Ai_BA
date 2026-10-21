/**
 * Turning a result table into rows the chart compiler understands.
 *
 * A table artifact stores its rows as arrays, with the column names beside
 * it. The charts want objects. That conversion lives here, on its own, so the
 * end-to-end tests can run the *same* code the browser runs instead of a copy
 * of it.
 */

import type { Row } from "./compile";

/** Zip a table's column names back onto its rows. */
export function rowsAsObjects(columns: string[], rows: unknown[][]): Row[] {
  return (rows ?? []).map((row) => {
    const object: Row = {};
    columns.forEach((column, index) => {
      object[column] = row[index];
    });
    return object;
  });
}
