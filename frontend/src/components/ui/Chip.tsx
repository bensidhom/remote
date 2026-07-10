import React from 'react';
import { motion } from 'framer-motion';
import { cn } from './utils';

/**
 * Chip — the floating action pill used on the map shell (and elsewhere).
 * Magnetic-ish hover lift with optional accent color + active glow.
 */
const Chip: React.FC<{
  icon?: React.ReactNode;
  label: string;
  onClick?: () => void;
  active?: boolean;
  accent?: string; // hex/rgb for a colored chip (e.g. Vision purple)
  title?: string;
  className?: string;
}> = ({ icon, label, onClick, active, accent, title, className }) => (
  <motion.button
    type="button"
    onClick={onClick}
    title={title || label}
    whileHover={{ y: -2 }}
    whileTap={{ scale: 0.96 }}
    transition={{ type: 'spring', stiffness: 420, damping: 24 }}
    style={accent ? { boxShadow: `0 8px 28px -6px ${accent}55` } : undefined}
    className={cn(
      'inline-flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium whitespace-nowrap',
      'border backdrop-blur-md transition-colors',
      active
        ? 'bg-brand-600 text-white border-brand-500 shadow-glow'
        : 'bg-white/80 text-slate-700 border-slate-200 hover:bg-white dark:bg-slate-800/70 dark:text-slate-200 dark:border-slate-700 dark:hover:bg-slate-800',
      className
    )}
  >
    {icon && <span className="shrink-0" style={accent ? { color: accent } : undefined}>{icon}</span>}
    {label}
  </motion.button>
);

export default Chip;
