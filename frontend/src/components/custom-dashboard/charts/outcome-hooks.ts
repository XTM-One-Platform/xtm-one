/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The hooks the validation views (`OutcomeViews.tsx`) share: a figure counting
 * up, a drawing that fills once mounted, and the words of the validation
 * vocabulary the data carries in English.
 */
import { useEffect, useRef, useState } from "react";

import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";

import { countUpValue } from "./formats";

/** How a view with no count yet shows its sample: greyed, desaturated, or not at all. */
export type SampleDisplay = "full" | "subtle" | "off";

/** A figure counting up to `target` over `duration` ms, easing out; at once when motion is reduced. */
export function useCountUp(target: number, duration: number): number {
  const reduced = usePrefersReducedMotion();
  const [value, setValue] = useState(reduced ? target : 0);
  const from = useRef(0);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame === "undefined") {
      from.current = target;
      setValue(target);
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const tick = (now: number) => {
      const elapsed = now - start;
      setValue(countUpValue(origin, target, elapsed, duration));
      if (elapsed < duration) frame = requestAnimationFrame(tick);
      else from.current = target;
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration, reduced]);
  return value;
}

/** A progress (0..1) set 60 ms after it changes, so a drawing fills from empty. */
export function useDrawn(target: number): number {
  const reduced = usePrefersReducedMotion();
  const [progress, setProgress] = useState(reduced ? target : 0);
  useEffect(() => {
    const timer = setTimeout(() => setProgress(target), reduced ? 0 : 60);
    return () => clearTimeout(timer);
  }, [target, reduced]);
  return progress;
}

/**
 * The validation vocabulary the data carries in English - expectation types,
 * what a success is for each, statuses, the security domains - read in the
 * viewer's language. @i18n-keys
 */
const OUTCOME_WORDS = {
  Prevention: "Prevention",
  Detection: "Detection",
  Vulnerability: "Vulnerability",
  Manual: "Manual",
  Challenge: "Challenge",
  Article: "Article",
  prevented: "prevented",
  detected: "detected",
  "not vulnerable": "not vulnerable",
  validated: "validated",
  stopped: "stopped",
  Prevented: "Prevented",
  "Not Prevented": "Not Prevented",
  Detected: "Detected",
  "Not Detected": "Not Detected",
  "Not vulnerable": "Not vulnerable",
  Vulnerable: "Vulnerable",
  "Partially Prevented": "Partially Prevented",
  "Partially Detected": "Partially Detected",
  "Partially Vulnerable": "Partially Vulnerable",
  Success: "Success",
  Failed: "Failed",
  Pending: "Pending",
  Endpoint: "Endpoint",
  Network: "Network",
  "Web App": "Web App",
  "E-mail Infiltration": "E-mail Infiltration",
  "Data Exfiltration": "Data Exfiltration",
  "URL Filtering": "URL Filtering",
  Cloud: "Cloud",
  "Artificial Intelligence": "Artificial Intelligence",
  Tabletop: "Tabletop",
} as const;

/** A word of the data in the viewer's language when it is of the vocabulary, else as written. */
export function useOutcomeWords(): (text: string) => string {
  const { t_i18n } = useFormatter();
  return (text) => (Object.prototype.hasOwnProperty.call(OUTCOME_WORDS, text) ? t_i18n(OUTCOME_WORDS[text as keyof typeof OUTCOME_WORDS]) : text);
}
