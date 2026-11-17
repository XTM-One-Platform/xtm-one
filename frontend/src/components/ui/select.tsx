// SPDX-License-Identifier: Apache-2.0

/**
 * Select — re-exported from the design system.
 *
 * A clean swap: the library wraps the same `@radix-ui/react-select` primitives
 * this file wrapped by hand, with the same compound anatomy, so the 77 call
 * sites keep working and every trigger, chevron, content surface and item state
 * is the library's now.
 *
 * Two differences from the old surface, both deliberate:
 *
 *  - `SelectScrollUpButton` / `SelectScrollDownButton` are gone. The library does
 *    not expose them and nothing outside this file imported them.
 *  - `SelectHelperText` is new, and available to call sites that want it.
 */
export {
  Select,
  SelectContent,
  SelectGroup,
  SelectHelperText,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@filigran/design-system";
