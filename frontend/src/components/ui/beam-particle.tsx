// SPDX-License-Identifier: Apache-2.0

/**
 * Animated SVG beam particle for force-directed graph visualizations.
 *
 * Uses CSS `offset-path` + `animation` instead of SVG SMIL to prevent
 * animation restarts caused by React re-renders during polling cycles.
 * SMIL `<animateMotion>` elements restart whenever the browser detects DOM
 * changes in sibling SVG elements (e.g. node attribute updates from polling).
 * CSS animations are tied to the DOM element lifecycle and are immune to
 * sibling DOM reconciliation.
 *
 * The parent SVG must include a `<style>` element with
 * {@link BEAM_PARTICLE_KEYFRAMES} for the animation to work.
 *
 * Wrapped in React.memo so that parent re-renders (e.g. polling updates to
 * running-agent state) do not recreate the CSS-animated SVG elements.
 */

import { memo, type CSSProperties } from "react";

interface BeamParticleProps {
  path: string;
  dur: number;
  delay: number;
  color: string;
  /** Core particle radius. @default 1.5 */
  size?: number;
  /** Multiplier for the outer glow circle radius. @default 2 */
  glowScale?: number;
  /** Peak opacity for the outer glow circle. @default 0.15 */
  glowOpacity?: number;
  /** Peak opacity for the inner core circle. @default 0.85 */
  coreOpacity?: number;
}

/**
 * Animated SVG particle that travels along a path with a glow effect.
 *
 * Uses CSS `offset-path` for motion and CSS `animation` for opacity,
 * eliminating SMIL animations that restart on React DOM reconciliation.
 */
export const BeamParticle = memo(function BeamParticle({
  path, dur, delay, color,
  size = 1.5,
  glowScale = 2,
  glowOpacity = 0.15,
  coreOpacity = 0.85,
}: BeamParticleProps) {
  const anim = `bp-travel ${dur}s linear ${delay}s infinite,bp-fade ${dur}s linear ${delay}s infinite`;
  const base: CSSProperties = {
    offsetPath: `path('${path}')`,
    offsetRotate: "0deg",
  };

  // Decoration only: a particle crossing a resting pointer must not take the
  // hover from the edge it travels on (#9106).
  return (
    <g pointerEvents="none">
      {/* Outer glow */}
      <circle r={size * glowScale} fill={color} opacity={0}
        style={{ ...base, animation: anim, "--bp-peak": glowOpacity } as CSSProperties}
      />
      {/* Core */}
      <circle r={size} fill={color} opacity={0}
        style={{ ...base, animation: anim, "--bp-peak": coreOpacity } as CSSProperties}
      />
    </g>
  );
});
