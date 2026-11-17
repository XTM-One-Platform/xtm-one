// SPDX-License-Identifier: Apache-2.0

/**
 * Fixed heights of the app chrome, in pixels.
 *
 * The chrome is sized *from* these constants (inline `style`, since a Tailwind
 * class cannot be computed at runtime), so the impersonation banner and the
 * padding that clears it cannot drift apart.
 */

/**
 * Height of the top bar.
 *
 * No longer applied to the bar: the design system's `Header` owns its own
 * height, and publishes it as the `--fds-header-height` custom property so a
 * consumer offsetting content does not have to hard-code the number a second
 * time. This constant is the JavaScript half of that same value — `AppShell`
 * publishes it INTO the property, so the bar, the content offset and the
 * assistant panel's `topOffset` all still move together from here.
 *
 * 68px is the library's own default (Figma 5376-2034), up from the 56px this app
 * used before the migration. Everything that positions against the bar reads it
 * from here or from the property, so the change is one edit, not a hunt.
 */
export const HEADER_HEIGHT = 68;

/**
 * Height of the impersonation banner.
 *
 * Applied explicitly rather than left to emerge from the banner's padding and
 * font size: the offset maths depends on it, and content-derived height is
 * exactly the kind of value that shifts under an unrelated change.
 */
export const IMPERSONATION_BANNER_HEIGHT = 36;

/**
 * Width of the navigation rail, expanded and collapsed, in pixels.
 *
 * The design system's `Navbar` owns these widths internally (`w-45` / `w-12`,
 * Figma 5376-2034). They are mirrored here because the content column has to be
 * offset by exactly the same amount, and `LAYOUT.md` records that the two ends
 * of that pair drifting apart is a live risk in the library's own docs shell.
 * Declared as pixels rather than Tailwind classes so a change is checkable
 * against the library in one place.
 */
export const NAVBAR_WIDTH_EXPANDED = 180;
export const NAVBAR_WIDTH_COLLAPSED = 48;
