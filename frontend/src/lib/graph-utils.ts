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
 * Shared graph utilities: colors and the beam particle keyframes.
 *
 * For the BeamParticle React component, see beam-particle.tsx.
 */

// ─── Shared graph colors ────────────────────────────────────
export const GRAPH_COLORS = {
  CYAN: "var(--color-accent)",
  CYAN_B: "hsl(197,100%,65%)",
  CYAN_DIM: "hsl(197,40%,35%)",
  GREEN: "var(--color-feedback-success-primary)",
  GREEN_B: "hsl(160,84%,55%)",
  AMBER: "var(--color-feedback-alert-primary)",
  AMBER_B: "var(--color-feedback-alert-primary)",
  NODE_BG: "hsl(220,25%,10%)",
  GRID: "hsl(220,25%,13%)",
  /** Muted label color for secondary nodes (KB, MCP). */
  LABEL_MUTED: "hsl(220,10%,30%)",
} as const;

// ─── Shared CSS keyframes for beam particles ─────────────────
/**
 * CSS @keyframes shared by all BeamParticle instances.
 * Must be injected once into each parent SVG via a `<style>` element.
 *
 * Uses CSS `offset-path` for motion and CSS `animation` for opacity,
 * eliminating SMIL animations that restart on React DOM reconciliation.
 */
export const BEAM_PARTICLE_KEYFRAMES = [
  "@keyframes bp-travel{from{offset-distance:0%}to{offset-distance:100%}}",
  "@keyframes bp-fade{0%,100%{opacity:0}10%,90%{opacity:var(--bp-peak,0.85)}}",
].join("\n");
