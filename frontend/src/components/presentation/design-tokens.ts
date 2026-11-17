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
 * A template's design tokens on the slide root.
 *
 * The deck response carries `template_design` (backend
 * `presentation_design.TemplateDesign.as_dict()`): how flat the template's
 * blocks are drawn and its radius and spacing scale. A template that declares
 * nothing gets no attribute and no variable, so the stylesheet draws it as
 * before (THREAD, XTM One Brand Deck, user and video templates). The seeded
 * Filigran Brand Deck declares the flat design (spec "PPT Design System" 3.0,
 * 3.4, 3.5, 3.6).
 *
 * The attributes key the flat rules of `presentation.css` (section "Template
 * design tokens"); the variables feed the existing radius and margin tokens,
 * so every block that already reads `--deck-radius` or `--deck-pad-x` follows.
 */
import type { CSSProperties } from "react";
import type { TemplateCoverEmblem, TemplateDesign } from "@/types";

/** canvas px -> CSS length */
const px = (value: number): string => `${value}px`;

/** Radius role -> the stylesheet variable it sets. */
const RADIUS_VARS: Record<string, string> = {
  card: "--deck-radius",
  inner: "--deck-radius-sm",
  chip: "--deck-radius-chip",
  table: "--deck-radius-table",
};

/** Spacing role -> the stylesheet variable it sets. */
const SPACING_VARS: Record<string, string> = {
  margin_x: "--deck-pad-x",
  margin_top: "--deck-pad-top",
  footer: "--deck-pad-bottom",
  title_body: "--deck-title-body-gap",
  title_subtitle: "--deck-title-subtitle-gap",
  tile_gap: "--deck-tile-gap",
  block_gap: "--deck-block-gap",
  block_gap_max: "--deck-block-gap-max",
  card_padding: "--deck-card-padding",
  kpi_padding: "--deck-kpi-padding",
  banner_padding: "--deck-banner-padding",
};

export interface DesignRootAttributes {
  /** Present when the template declares any token: keys the radius and spacing rules. */
  "data-design"?: "true";
  "data-design-shadows"?: "false";
  "data-design-glow"?: "false";
  "data-design-eyebrows"?: "false";
  "data-design-gradient-strokes"?: "never";
  "data-design-body-align"?: "top" | "center";
  /** Slice 4: the slide compositions. */
  "data-design-title-underline"?: "false";
  "data-design-title-ink"?: "brand";
  "data-design-cover"?: "brand";
  "data-design-section"?: "outline";
  "data-design-agenda"?: "plain";
  /** The brand cover emblem anchored inside the right edge. */
  "data-design-cover-emblem-anchor"?: "right";
  style: CSSProperties;
}

/** The bounds the backend accepts (`presentation_design.COVER_EMBLEM_*`). */
export const COVER_EMBLEM_SCALE = { min: 20, max: 140 } as const;
export const COVER_EMBLEM_OPACITY = { min: 1, max: 15 } as const;

const wholeIn = (value: unknown, bounds: { min: number; max: number }): number | undefined =>
  typeof value === "number" && Number.isInteger(value) && value >= bounds.min && value <= bounds.max
    ? value
    : undefined;

/** The readable keys of a `cover_emblem` declaration, `undefined` when none is. */
export function coverEmblemOf(raw: unknown): TemplateCoverEmblem | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const declaration = raw as Record<string, unknown>;
  const out: TemplateCoverEmblem = {};
  const scale = wholeIn(declaration.scale, COVER_EMBLEM_SCALE);
  const opacity = wholeIn(declaration.opacity, COVER_EMBLEM_OPACITY);
  if (scale !== undefined) out.scale = scale;
  if (declaration.anchor === "bottom_right" || declaration.anchor === "right") out.anchor = declaration.anchor;
  if (opacity !== undefined) out.opacity = opacity;
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * The attributes and inline variables a slide root carries for *design*.
 * Only what the template turned OFF (or set) is published: an absent switch
 * is today's rendering.
 */
export function designRootAttributes(design: TemplateDesign | null | undefined): DesignRootAttributes {
  const style: Record<string, string> = {};
  if (!design) return { style };
  for (const [role, value] of Object.entries(design.radius ?? {})) {
    const variable = RADIUS_VARS[role];
    if (variable && typeof value === "number") style[variable] = px(value);
  }
  for (const [role, value] of Object.entries(design.spacing ?? {})) {
    const variable = SPACING_VARS[role];
    if (variable && typeof value === "number") style[variable] = px(value);
  }
  // The brand cover emblem: read by `.deck-brand-emblem` on the cover
  // and the closing; an undeclared key keeps the stylesheet's value.
  const emblem = coverEmblemOf(design.cover_emblem);
  if (emblem?.scale !== undefined) style["--deck-cover-emblem-scale"] = `${emblem.scale}%`;
  if (emblem?.opacity !== undefined) style["--deck-cover-emblem-opacity"] = String(emblem.opacity / 100);
  return {
    "data-design": "true",
    "data-design-shadows": design.shadows === false ? "false" : undefined,
    "data-design-glow": design.glow === false ? "false" : undefined,
    "data-design-eyebrows": design.eyebrows === false ? "false" : undefined,
    "data-design-gradient-strokes": design.gradient_strokes === "never" ? "never" : undefined,
    "data-design-body-align": design.body_align,
    "data-design-title-underline": design.title_underline === false ? "false" : undefined,
    "data-design-title-ink": design.title_ink === "brand" ? "brand" : undefined,
    "data-design-cover": design.cover === "brand" ? "brand" : undefined,
    "data-design-section": design.section === "outline" ? "outline" : undefined,
    "data-design-agenda": design.agenda === "plain" ? "plain" : undefined,
    "data-design-cover-emblem-anchor": emblem?.anchor === "right" ? "right" : undefined,
    style: style as CSSProperties,
  };
}

/**
 * Whether the slide field *key* of *layout* is offered under the template's
 * *design*: the plain agenda draws every item alike, so its
 * `current` field changes nothing there and the inspector does not offer it.
 * Every other field, and every field under any other template, is offered.
 */
export function slideFieldOffered(layout: string, key: string, design: TemplateDesign | null | undefined): boolean {
  return !(layout === "agenda" && key === "current" && design?.agenda === "plain");
}

/**
 * The design the Studio DRAFT renders with, resolved like its chrome: none
 * without a template, the stored deck's `template_design` while the template
 * is unchanged, and the tried (or picked) template's declaration once it
 * changed, so the stage and the filmstrip show what a save would store.
 */
export function draftTemplateDesign({
  templateContainerId,
  templateChanged,
  stored,
  tried,
}: {
  templateContainerId: string | null | undefined;
  templateChanged: boolean;
  stored: TemplateDesign | null | undefined;
  tried: { extra_metadata?: Record<string, unknown> | null } | null | undefined;
}): TemplateDesign | null {
  if (!templateContainerId) return null;
  if (!templateChanged) return stored ?? null;
  return templateDesignOf(tried);
}

/** The template's design read off a template row the way the deck response serves it. */
export function templateDesignOf(
  template: { extra_metadata?: Record<string, unknown> | null } | null | undefined,
): TemplateDesign | null {
  const raw = template?.extra_metadata?.design;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const declaration = raw as Record<string, unknown>;
  const out: TemplateDesign = {};
  for (const key of ["shadows", "glow", "eyebrows"] as const) {
    if (typeof declaration[key] === "boolean") out[key] = declaration[key] as boolean;
  }
  if (declaration.gradient_strokes === "allowed" || declaration.gradient_strokes === "never") {
    out.gradient_strokes = declaration.gradient_strokes;
  }
  if (declaration.body_align === "top" || declaration.body_align === "center") out.body_align = declaration.body_align;
  if (typeof declaration.title_underline === "boolean") out.title_underline = declaration.title_underline;
  if (declaration.title_ink === "brand") out.title_ink = "brand";
  if (declaration.cover === "brand") out.cover = "brand";
  if (declaration.section === "outline") out.section = "outline";
  if (declaration.agenda === "plain") out.agenda = "plain";
  const emblem = coverEmblemOf(declaration.cover_emblem);
  if (emblem) out.cover_emblem = emblem;
  const scale = (value: unknown, keys: readonly string[], limit: number): Record<string, number> | undefined => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
    const read: Record<string, number> = {};
    for (const key of keys) {
      const pixel = (value as Record<string, unknown>)[key];
      if (typeof pixel === "number" && Number.isInteger(pixel) && pixel >= 0 && pixel <= limit) read[key] = pixel;
    }
    return Object.keys(read).length > 0 ? read : undefined;
  };
  const radius = scale(declaration.radius, Object.keys(RADIUS_VARS), 48);
  const spacing = scale(declaration.spacing, Object.keys(SPACING_VARS), 240);
  if (radius) out.radius = radius;
  if (spacing) out.spacing = spacing;
  return Object.keys(out).length > 0 ? out : null;
}
