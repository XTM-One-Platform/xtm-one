// SPDX-License-Identifier: Apache-2.0

/**
 * Warning text inside a row that fills on hover (`hover:bg-elevation-hover` on a
 * `group/hover-row` element): the schedule row, the run card and its lightweight
 * row (with the approval prompt of `action-proposal.tsx` they render), the runner
 * activity header button.
 *
 * On the light hover fill (`--bg-elevation-hover-layer-1`) the warning primary
 * falls to 3.84:1, so a hovered row in the light theme steps to the tertiary
 * (9.58:1); the dark theme keeps the primary, 5.45:1 on its hover fill.
 * The row carries the named group `group/hover-row`, never a bare
 * `group`: a bare `group-hover` also matches an outer `group` (the chat message
 * bubble), so a label would change colour on rows that are not hovered.
 * `tests_unit/warning-text-contrast.test.ts`
 * measures both themes and keeps this string out of the components, so every
 * hover row uses this one class instead of a copy.
 */
export const WARNING_ON_HOVER_TEXT = "text-feedback-warning-primary light:group-hover/hover-row:text-feedback-warning-tertiary";

/**
 * Warning text on a control drawn over a filled bar: the "Select the N awaiting
 * approval" button of the list control bar, whose bar paints `bg-elevation-hover`
 * at rest and whose ghost button adds its own fill on hover and focus. The light
 * theme takes the tertiary in every state, the primary read 2.98:1 there; the
 * dark theme keeps the primary. The `hover:` and `focus-visible:` forms
 * restate the colour because the design system's button sets its own text colour
 * in those states. `tests_unit/warning-text-contrast.test.ts` measures it.
 */
export const WARNING_ON_FILL_TEXT =
  "text-feedback-warning-primary hover:text-feedback-warning-primary focus-visible:text-feedback-warning-primary light:text-feedback-warning-tertiary light:hover:text-feedback-warning-tertiary light:focus-visible:text-feedback-warning-tertiary";
