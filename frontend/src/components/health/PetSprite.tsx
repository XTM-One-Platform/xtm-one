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
 * One pet of the Concierge companion (#8261), playing a row of its atlas. Under
 * `prefers-reduced-motion`, or with `still`, it shows the row's first frame.
 */
import { useEffect, useState } from "react";

import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { PET_CELL_HEIGHT, PET_CELL_WIDTH, PET_COLUMNS, PET_ROWS, PET_STATES, framePosition, type PetState } from "@/lib/health-pet";

interface Props {
  url: string | null;
  state: PetState;
  version: 1 | 2;
  /** The cell's size against the format's 192x208. */
  scale?: number;
  still?: boolean;
  /** Called once a loop of the row ended (a greeting that plays once). */
  onLoop?: () => void;
  label: string;
}

export function PetSprite({ url, state, version, scale = 0.5, still = false, onLoop, label }: Props) {
  const reducedMotion = usePrefersReducedMotion();
  const animate = !still && !reducedMotion;
  const [clock, setClock] = useState({ state, frame: 0 });
  // A new state starts at its first frame, in the very render that shows it.
  const frame = clock.state === state ? clock.frame : 0;
  useEffect(() => {
    if (!animate) return undefined;
    const durations = PET_STATES[state].durations;
    const timer = window.setTimeout(() => {
      const next = (frame + 1) % durations.length;
      if (next === 0) onLoop?.();
      setClock({ state, frame: next });
    }, durations[frame]);
    return () => window.clearTimeout(timer);
  }, [animate, frame, state, onLoop]);
  return (
    <div
      role="img"
      aria-label={label}
      style={{
        width: PET_CELL_WIDTH * scale,
        height: PET_CELL_HEIGHT * scale,
        backgroundImage: url ? `url("${url}")` : undefined,
        backgroundRepeat: "no-repeat",
        backgroundSize: `${PET_COLUMNS * PET_CELL_WIDTH * scale}px ${PET_ROWS[version] * PET_CELL_HEIGHT * scale}px`,
        backgroundPosition: framePosition(state, animate ? frame : 0, scale),
        imageRendering: "pixelated",
      }}
    />
  );
}
