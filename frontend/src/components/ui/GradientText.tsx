import React from 'react';
import { cn } from './utils';

/** Animated gradient text (reactbits GradientText). */
const GradientText: React.FC<{
  children: React.ReactNode;
  className?: string;
  as?: keyof JSX.IntrinsicElements;
}> = ({ children, className, as: Tag = 'span' }) => {
  return React.createElement(
    Tag,
    { className: cn('text-gradient font-semibold', className) },
    children
  );
};

export default GradientText;
