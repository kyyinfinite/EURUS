/** DataTable — a typed, consistent table surface for module pages.
 *  Dense rows, hairline dividers, sticky header inside a scroll area. */

import type { ReactNode } from "react";
import { Empty } from "./States";

export interface DataTableColumn<Row> {
  key: string;
  header: ReactNode;
  render: (row: Row, index: number) => ReactNode;
  className?: string;
  headerClassName?: string;
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  empty,
  onRowClick,
  maxHeight,
  className = "",
}: {
  columns: DataTableColumn<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  empty?: ReactNode;
  onRowClick?: (row: Row) => void;
  maxHeight?: string;
  className?: string;
}) {
  if (rows.length === 0) {
    return <div className={className}>{empty ?? <Empty>No records yet.</Empty>}</div>;
  }
  return (
    <div className={`overflow-x-auto rounded-xl border border-line bg-surface ${className}`}>
      <div className={maxHeight} style={maxHeight ? undefined : undefined}>
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-line bg-raised-soft/60">
              {columns.map((column) => (
                <th
                  key={column.key}
                  className={`px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-[0.1em] text-muted ${column.headerClassName ?? ""}`}
                >
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr
                key={rowKey(row, index)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-line-soft last:border-b-0 ${
                  onRowClick ? "cursor-pointer transition-colors hover:bg-copper-tint" : ""
                }`}
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={`px-4 py-3 align-middle ${column.className ?? ""}`}
                  >
                    {column.render(row, index)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
