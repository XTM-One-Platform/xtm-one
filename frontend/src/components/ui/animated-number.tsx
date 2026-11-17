// SPDX-License-Identifier: Apache-2.0

/** Smoothly animates a number from 0 to the target value with ease-out cubic. */

import { useRef, useEffect, useCallback } from "react";
import { formatCompactNumber } from "@/lib/utils";

export function AnimatedNumber({
  value,
  duration = 800,
  formatter,
}: {
  value: number;
  duration?: number;
  formatter?: (value: number) => string;
}) {
  const ref = useRef<HTMLSpanElement>(null);

  const format = useCallback(
    (next: number) => (formatter ? formatter(next) : formatCompactNumber(Math.round(next))),
    [formatter],
  );

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (value === 0) {
      el.textContent = format(0);
      return;
    }
    const start = performance.now();
    let raf: number;
    const step = (now: number) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = format(value * eased);
      if (progress < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration, format]);
  const initialValue = formatter ? formatter(0) : "0";
  return <span ref={ref}>{initialValue}</span>;
}
