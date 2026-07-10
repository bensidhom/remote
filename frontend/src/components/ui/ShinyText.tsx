import React from 'react';
import { cn } from './utils';

/** Sweeping shine across text (reactbits ShinyText). */
const ShinyText: React.FC<{ text: string; className?: string }> = ({ text, className }) => (
  <span className={cn('shiny-text', className)}>{text}</span>
);

export default ShinyText;
