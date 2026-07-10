import React, { useRef } from 'react';
import { cn } from './utils';

/**
 * SpotlightCard (reactbits) — a glass card with a cursor-following glow.
 * Tracks the pointer and feeds --mx/--my to the `.spotlight-card` CSS.
 */
const SpotlightCard: React.FC<
  React.HTMLAttributes<HTMLDivElement> & { as?: 'div' | 'button' }
> = ({ className, children, onMouseMove, ...props }) => {
  const ref = useRef<HTMLDivElement>(null);

  const handleMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (el) {
      const r = el.getBoundingClientRect();
      el.style.setProperty('--mx', `${e.clientX - r.left}px`);
      el.style.setProperty('--my', `${e.clientY - r.top}px`);
    }
    onMouseMove?.(e);
  };

  return (
    <div
      ref={ref}
      onMouseMove={handleMove}
      className={cn(
        'spotlight-card glass rounded-2xl border border-slate-200/70 dark:border-slate-700/70',
        'shadow-card dark:shadow-card-dark transition-transform duration-300',
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
};

export default SpotlightCard;
