import React from 'react';
import CountUp from './CountUp';
import { cn } from './utils';

/** Compact stat tile: label + animated value, used in KPI/summary rows. */
const StatChip: React.FC<{
  label: string;
  value: number | string;
  icon?: React.ReactNode;
  accent?: string;
  decimals?: number;
  suffix?: string;
  className?: string;
}> = ({ label, value, icon, accent, decimals = 0, suffix, className }) => (
  <div
    className={cn(
      'glass rounded-2xl px-4 py-3 flex items-center gap-3 border border-slate-200/70 dark:border-slate-700/70',
      className
    )}
  >
    {icon && (
      <span
        className="grid place-items-center w-10 h-10 rounded-xl shrink-0"
        style={{ background: `${accent || '#6366f1'}1a`, color: accent || '#6366f1' }}
      >
        {icon}
      </span>
    )}
    <div className="min-w-0">
      <div className="text-xl font-bold text-slate-800 dark:text-slate-100 leading-tight">
        {typeof value === 'number' ? <CountUp to={value} decimals={decimals} suffix={suffix} /> : value}
      </div>
      <div className="text-xs text-slate-500 dark:text-slate-400 truncate">{label}</div>
    </div>
  </div>
);

export default StatChip;
