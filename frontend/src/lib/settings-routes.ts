// SPDX-License-Identifier: Apache-2.0

/**
 * Settings destinations that a surface outside Settings links to.
 *
 * One constant per target so a move of a Settings section (the 7-section
 * consolidation of #9173) changes one line here, never a literal scattered
 * over the cards that send an administrator there.
 */

/** Anchor of the "Artificial Intelligence" card on the Platform section. */
export const AI_ENABLEMENT_ANCHOR = "ai-enablement";

/** The AI on/off switch: the Platform section, scrolled to its AI card. */
export const SETTINGS_AI_ENABLEMENT_PATH = `/settings/platform#${AI_ENABLEMENT_ANCHOR}`;

/** Where the default model is configured. */
export const SETTINGS_MODELS_PATH = "/settings/ai/models";
