import React from 'react';
import { cn } from './utils';

/**
 * Aurora — animated, blurred color-field background (reactbits-style).
 * Drop inside a `relative overflow-hidden` container; it sits behind content.
 */
const Aurora: React.FC<{ className?: string; intensity?: 'soft' | 'normal' | 'bold' }> = ({
  className,
  intensity = 'normal',
}) => {
  const opacity = intensity === 'soft' ? 0.5 : intensity === 'bold' ? 1 : 0.75;
  return (
    <div aria-hidden className={cn('pointer-events-none absolute inset-0 overflow-hidden', className)}>
      <div className="aurora-bg" style={{ opacity }} />
    </div>
  );
};

export default Aurora;
