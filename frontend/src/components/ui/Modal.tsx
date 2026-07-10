import React, { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { X } from 'lucide-react';
import { cn } from './utils';

type Size = 'md' | 'lg' | 'xl' | 'full';

const SIZES: Record<Size, string> = {
  md: 'max-w-2xl',
  lg: 'max-w-4xl',
  xl: 'max-w-6xl',
  full: 'max-w-[98vw] h-[94vh]',
};

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  title?: React.ReactNode;
  icon?: React.ReactNode;
  size?: Size;
  /** When false, the modal body is not padded (e.g. for full-bleed tools). */
  padded?: boolean;
  className?: string;
  /** Render children inline (no portal/backdrop) — for embedding as a tab panel. */
  inline?: boolean;
  /**
   * Keep children mounted when closed (hidden via display:none) instead of
   * unmounting them. Used by the tool host so switching tools or closing back to
   * the map preserves each tool's state (conversations, in-progress edits).
   * Trades the open/close animation for state persistence.
   */
  keepMounted?: boolean;
}

/**
 * Modal — animated, portalled floating window used by every tool surface.
 * Backdrop blur + spring scale-in (framer-motion), Esc to close, click-outside to close.
 */
const Modal: React.FC<ModalProps> = ({
  open,
  onClose,
  children,
  title,
  icon,
  size = 'xl',
  padded = true,
  className,
  inline = false,
  keepMounted = false,
}) => {
  useEffect(() => {
    if (inline || !open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, inline]);

  // Embedded mode: render the body in place (used as a tab panel).
  if (inline) {
    return <div className={cn('h-full', padded && 'p-1', className)}>{children}</div>;
  }

  // The visible panel (header, close button, scrollable body).
  const panel = (
    <div
      className={cn(
        'relative w-full glass-strong rounded-3xl shadow-2xl my-auto',
        'border border-slate-200/80 dark:border-slate-700/80',
        SIZES[size],
        size === 'full' ? 'flex flex-col overflow-hidden' : 'max-h-[calc(100vh-4rem)] overflow-hidden flex flex-col',
        className
      )}
    >
      {(title || icon) && (
        <div className="flex items-center gap-3 px-6 py-4 border-b border-slate-200/70 dark:border-slate-700/70 shrink-0">
          {icon && (
            <span className="grid place-items-center w-9 h-9 rounded-xl bg-brand-500/10 text-brand-500 dark:text-brand-300">{icon}</span>
          )}
          <div className="text-lg font-semibold text-slate-800 dark:text-slate-100">{title}</div>
        </div>
      )}
      <button
        onClick={onClose}
        aria-label="Close"
        className="absolute top-3.5 right-3.5 z-10 grid place-items-center w-9 h-9 rounded-full bg-slate-100/80 text-slate-500 hover:bg-red-500 hover:text-white dark:bg-slate-800/80 dark:text-slate-300 transition-colors"
      >
        <X className="w-4 h-4" />
      </button>
      <div className={cn('flex-1 overflow-auto ui-scroll', padded && 'p-6')}>{children}</div>
    </div>
  );

  // keepMounted: always render the frame; toggle visibility so children keep
  // their state when closed. (No open/close animation in this mode.)
  if (keepMounted) {
    return createPortal(
      <div
        className="fixed inset-0 z-[4600] flex items-start justify-center p-4 sm:p-8 overflow-y-auto"
        style={{ background: 'rgba(2,6,23,0.55)', backdropFilter: 'blur(6px)', display: open ? undefined : 'none' }}
        aria-hidden={!open}
        onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
      >
        {panel}
      </div>,
      document.body
    );
  }

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[4600] flex items-start justify-center p-4 sm:p-8 overflow-y-auto"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          style={{ background: 'rgba(2,6,23,0.55)', backdropFilter: 'blur(6px)' }}
          onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ type: 'spring', stiffness: 320, damping: 30 }}
            className={cn(
              'relative w-full glass-strong rounded-3xl shadow-2xl my-auto',
              'border border-slate-200/80 dark:border-slate-700/80',
              SIZES[size],
              size === 'full' ? 'flex flex-col overflow-hidden' : 'max-h-[calc(100vh-4rem)] overflow-hidden flex flex-col',
              className
            )}
          >
            {(title || icon) && (
              <div className="flex items-center gap-3 px-6 py-4 border-b border-slate-200/70 dark:border-slate-700/70 shrink-0">
                {icon && (
                  <span className="grid place-items-center w-9 h-9 rounded-xl bg-brand-500/10 text-brand-500 dark:text-brand-300">
                    {icon}
                  </span>
                )}
                <div className="text-lg font-semibold text-slate-800 dark:text-slate-100">{title}</div>
              </div>
            )}
            <button
              onClick={onClose}
              aria-label="Close"
              className="absolute top-3.5 right-3.5 z-10 grid place-items-center w-9 h-9 rounded-full bg-slate-100/80 text-slate-500 hover:bg-red-500 hover:text-white dark:bg-slate-800/80 dark:text-slate-300 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
            <div className={cn('flex-1 overflow-auto ui-scroll', padded && 'p-6')}>{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
  );
};

export default Modal;
