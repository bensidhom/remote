import React, { useEffect, useRef, useState } from 'react';
import { useInView, useMotionValue, useSpring } from 'framer-motion';
import { cn } from './utils';

/**
 * CountUp — animates a number from 0 → `to` when it scrolls into view
 * (reactbits CountUp). Uses a framer-motion spring for smooth easing.
 */
const CountUp: React.FC<{
  to: number;
  decimals?: number;
  className?: string;
  prefix?: string;
  suffix?: string;
  duration?: number;
}> = ({ to, decimals = 0, className, prefix = '', suffix = '', duration = 1.4 }) => {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: '0px' });
  const mv = useMotionValue(0);
  const spring = useSpring(mv, { duration: duration * 1000, bounce: 0 });
  const [display, setDisplay] = useState('0');

  useEffect(() => {
    if (inView) mv.set(to);
  }, [inView, to, mv]);

  useEffect(() => {
    const unsub = spring.on('change', (v) => {
      setDisplay(
        Number(v).toLocaleString(undefined, {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
        })
      );
    });
    return () => unsub();
  }, [spring, decimals]);

  return (
    <span ref={ref} className={cn('tabular-nums', className)}>
      {prefix}
      {display}
      {suffix}
    </span>
  );
};

export default CountUp;
