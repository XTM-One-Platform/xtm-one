// SPDX-License-Identifier: Apache-2.0

/**
 * The invalid border of a raw `<input>`, `<textarea>` or `<select>`, one that
 * does not go through the `Input` adapter (components/ui/input.tsx draws the
 * same two tones for its fields). The class sits on the element itself and is
 * drawn from its own `aria-invalid`, so a raw field sets `aria-invalid` and
 * takes this class, never a tone of its own ("Form fields: error state" in
 * dev-docs/reference/frontend-design-system.md, input-feedback-border.test.ts).
 * Amber is drawn with the warning token, the one that reaches 3:1 on the card
 * in both themes.
 */
export const INVALID_FIELD_CLASS = {
  error: "aria-[invalid=true]:border-feedback-error-primary",
  alert: "aria-[invalid=true]:border-feedback-warning-primary",
} as const;

/**
 * The invalid border of a combobox field (SearchableSelect): the visible border
 * is the wrapper's, around the input, so it is drawn from the input's own
 * `aria-invalid`, at rest and on hover, the way components/ui/input.tsx keys
 * its INVALID_INPUT_CLASS. Never a rest border repeated as its hover border
 * and switched by a prop (the "identical rest and hover surface"
 * design-system guard).
 */
export const COMBOBOX_INVALID_FIELD_CLASS =
  "[&:has(input[aria-invalid=true])]:border-feedback-error-primary [&:has(input[aria-invalid=true]):hover]:border-feedback-error-primary";
