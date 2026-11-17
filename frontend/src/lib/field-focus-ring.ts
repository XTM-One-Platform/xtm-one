// SPDX-License-Identifier: Apache-2.0

/**
 * The keyboard focus ring of a page-local text field.
 *
 * The design-system `Input` draws its focus as a 2 px `ring-focus` with a 2 px
 * offset. A `<textarea>` or a bare `<input>` written in a page used to draw the
 * same token at the same width flush on its border, so one focus token had two
 * geometries depending on which field had focus. Every hand-written text field
 * takes this class instead of spelling the ring out, so the geometry matches
 * the `Input` everywhere.
 */
export const FIELD_FOCUS_RING =
  "ring-offset-focus focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";
