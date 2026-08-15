import React from 'react';
import { Database } from 'lucide-react';

type Evidence = Record<string, unknown>;

const record = (value: unknown): Evidence | undefined => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Evidence
    : undefined
);

const text = (value: unknown): string | undefined => (
  typeof value === 'string' && value.trim() ? value.trim() : undefined
);

const humanize = (value: string): string => {
  const words = value
    .replace(/^metric__/, '')
    .replace(/^field__/, '')
    .replace(/__/g, ' ')
    .replace(/[_.]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : value;
};

const outputLabel = (column: string, evidence?: Evidence): string => {
  const plan = record(evidence?.query_plan);
  const output = (Array.isArray(plan?.outputs) ? plan.outputs : [])
    .map(record)
    .find((candidate) => candidate?.alias === column);
  const outputId = text(output?.id);
  const metric = (Array.isArray(evidence?.metric_definitions) ? evidence.metric_definitions : [])
    .map(record)
    .find((candidate) => candidate?.metric_id === outputId);
  const outputParts = outputId?.split('.') ?? [];
  return text(metric?.label)
    ?? (outputId ? humanize(outputParts[outputParts.length - 1] ?? outputId) : humanize(column));
};

const queryDetails = (evidence?: Evidence, fallbackSql?: string) => {
  const plan = record(evidence?.query_plan);
  return {
    logicalSql: text(plan?.logical_sql) ?? text(fallbackSql),
  };
};

export const BridgetteResultTable: React.FC<{
  rows?: unknown[][];
  columns?: string[];
  evidence?: Evidence;
  compact?: boolean;
}> = ({ rows, columns, evidence, compact = false }) => {
  if (!rows || !columns) return null;
  const limit = compact ? 25 : 100;
  return (
    <section aria-label="Query result" className="overflow-hidden rounded-xl border border-slate-200/80 bg-white dark:border-slate-700/80 dark:bg-slate-900">
      <div className={`${compact ? 'px-2 py-1.5' : 'px-3 py-2'} flex items-center justify-between border-b border-slate-200/70 dark:border-slate-700/70`}>
        <span className="inline-flex items-center gap-1.5 font-semibold text-slate-700 dark:text-slate-200"><Database className="h-3.5 w-3.5 text-brand-500" /> Result</span>
        <span className="text-slate-400">{rows.length} row{rows.length === 1 ? '' : 's'}</span>
      </div>
      <div className={`${compact ? 'max-h-44' : 'max-h-64'} overflow-auto`}>
        <table className="w-full">
          <thead className="sticky top-0 bg-slate-100 dark:bg-slate-800">
            <tr>{columns.map((column) => <th key={column} className={`${compact ? 'px-2 py-1.5' : 'px-3 py-2'} text-left font-semibold text-slate-600 dark:text-slate-300`}>{outputLabel(column, evidence)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.slice(0, limit).map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-slate-200/60 dark:border-slate-700/60">
                {columns.map((column, columnIndex) => <td key={column} className={`${compact ? 'px-2 py-1.5' : 'px-3 py-2'} text-slate-700 dark:text-slate-300`}>{String(row[columnIndex] ?? '')}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
};

export const BridgetteQueryDetails: React.FC<{
  evidence?: Evidence;
  sql?: string;
  rows?: unknown[][];
  columns?: string[];
  stale?: boolean;
  compact?: boolean;
}> = ({ evidence, sql, rows, columns, stale = false, compact = false }) => {
  const details = queryDetails(evidence, sql);
  if (!details.logicalSql && (!rows || !columns)) return null;
  const rowCount = rows?.length ?? 0;
  return (
    <details className={`${compact ? 'text-[11px]' : 'text-xs'} text-slate-400`}>
      <summary className="inline-flex cursor-pointer list-none select-none items-center gap-1.5 hover:text-brand-500 [&::-webkit-details-marker]:hidden">
        <Database className="h-3.5 w-3.5" />
        <span>SQL · {rowCount} row(s)</span>
      </summary>
      <div className="mt-2 space-y-2">
        {details.logicalSql && <pre className={`${compact ? 'max-h-36 p-2' : 'p-3'} overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 text-slate-200`}><code>{details.logicalSql}</code></pre>}
        <BridgetteResultTable rows={rows} columns={columns} evidence={evidence} compact={compact} />
      </div>
    </details>
  );
};
