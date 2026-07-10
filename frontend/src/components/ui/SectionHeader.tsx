import React from 'react';
import GradientText from './GradientText';
import { cn } from './utils';

/** Consistent page/modal heading: icon + title + optional actions.
 * The `subtitle` prop is accepted for back-compat but intentionally NOT rendered
 * — the app keeps headings to a plain label, no descriptive sub-text. */
const SectionHeader: React.FC<{
  icon?: React.ReactNode;
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  className?: string;
}> = ({ icon, title, actions, className }) => (
  <div className={cn('flex items-start justify-between gap-4 mb-6', className)}>
    <div className="flex items-center gap-3 min-w-0">
      {icon && (
        <span className="grid place-items-center w-11 h-11 rounded-2xl bg-brand-500/10 text-brand-500 dark:text-brand-300 shrink-0">
          {icon}
        </span>
      )}
      <div className="min-w-0">
        <GradientText as="h2" className="text-2xl leading-tight tracking-tight">
          {title}
        </GradientText>
      </div>
    </div>
    {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
  </div>
);

export default SectionHeader;
