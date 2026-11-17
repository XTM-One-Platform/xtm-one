// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import { Input as FdsInput } from "@filigran/design-system";
import { cn } from "@/lib/utils";

type FdsInputProps = React.ComponentProps<typeof FdsInput>;

/**
 * The library's props, with the two attributes it re-declares more narrowly than
 * the element it renders widened back to the element's own types:
 *
 *   type  — text | password | number | email upstream, where this app also uses
 *           `date`
 *   value — string upstream, where the element (and this app) also allows a
 *           number
 *
 * Everything else stays typed as the library declares it, `label`,
 * `helperText`, `error` and the icon slots included. An earlier version typed
 * the whole component as `InputHTMLAttributes` and cast the entire props object
 * at the spread, which did fix these two but also silenced every other type
 * error at the single point 232 call sites funnel through — a typo'd prop or a
 * wrong handler signature would have compiled.
 */
type InputProps = Omit<FdsInputProps, "type" | "value"> &
  Pick<React.InputHTMLAttributes<HTMLInputElement>, "type" | "value"> & {
    /** A short field that fits a list row. */
    compact?: boolean;
    /**
     * The tone of the invalid border, drawn from `aria-invalid` on the field:
     * `error` (default) for a value that cannot be saved, `alert` (amber) for a
     * value that is accepted but deserves a second look (a reserved tag key, an
     * unknown icon name).
     */
    invalidTone?: "error" | "alert";
  };

/**
 * The compact field's styling. The library has no compact size, and its
 * `className` lands on the wrapper, so the field is reached through it
 * (LIBRARY-FEEDBACK F35).
 */
const COMPACT_INPUT_CLASS = "[&_input]:block [&_input]:h-6 [&_input]:px-1 [&_input]:text-center [&_input]:content-caption";

/**
 * The helper row under the field: the help or error line and the `N/max`
 * character counter the library draws for `maxLength`. The library sets both
 * at its `text-1` step (10 px), under the 12 px floor of the design standards
 * ("[UX] Design standards", type scale). The row carries the id
 * `<input id>-helper`, so it is reached through the wrapper by that id and
 * both lines are set at the compact body step (12 px) on every Input, once,
 * here (LIBRARY-FEEDBACK: helper row at text-1).
 */
const HELPER_ROW_CLASS = "[&_[id$=-helper]>span]:content-compact";

/**
 * The invalid border, the ONE place it is drawn ("Form fields: error state" in
 * dev-docs/reference/frontend-design-system.md). The library's
 * `className` lands on the wrapper, so the field is reached through it, and
 * only while the field itself carries `aria-invalid="true"`: a call site sets
 * `aria-invalid` and never a tone of its own (input-feedback-border.test.ts).
 * Full tone, the strength that reaches 3:1 on the card in both themes (a /60
 * border measured 2.57:1 on dark). The amber variant is
 * drawn with the warning token: the alert token (#F2BE3A) measured 1.72:1 on
 * the light card, the warning one 4.84:1 on light and 5.70:1 on dark. The hover state keeps the tone. The focus ring stays
 * the shared `ring-focus`: the border alone carries the error.
 */
const INVALID_INPUT_CLASS = {
  error:
    "[&_input[aria-invalid=true]]:border-feedback-error-primary [&_input[aria-invalid=true]:hover]:border-feedback-error-primary",
  alert:
    "[&_input[aria-invalid=true]]:border-feedback-warning-primary [&_input[aria-invalid=true]:hover]:border-feedback-warning-primary",
} as const;

/**
 * Input — an adapter over the design system's `Input`.
 *
 * A pass-through, with two corrections.
 *
 * Both widened attributes are forwarded to a real `<input>`, so both work at
 * runtime and only the types disagree; the two casts below keep that where it
 * belongs instead of pushing a library type problem out to every call site.
 *
 * `aria-invalid` and `aria-required` are the other. The library writes both
 * AFTER spreading the caller's props, from its own `error` and `required`
 * props alone, so a caller's value is silently replaced by `undefined`: an
 * invalid field is announced as valid, a field whose requirement depends on
 * another choice loses it. The library's props are no substitute: `error` also
 * draws an icon inside the field and a message under it, and `required` an
 * asterisk beside a label the caller may not render through the library. So
 * the caller's value is applied to the element itself once the library has
 * rendered, exactly as React would have written it (`false` included); React
 * leaves an attribute alone while the prop it renders stays the same, so it is
 * not undone. Where the library's own prop is set, its value wins.
 *
 * Both are filed in fds-migration/LIBRARY-FEEDBACK.md (F10 and F26), and this
 * file goes back to a plain re-export when they land. `compact` adds a size
 * the library lacks (F35), and `invalidTone` names the invalid border the
 * adapter draws from `aria-invalid` on every field.
 */
const Input = React.forwardRef<HTMLInputElement, InputProps>(({ type, value, compact, invalidTone = "error", className, ...rest }, ref) => {
  const element = React.useRef<HTMLInputElement | null>(null);
  const setRefs = React.useCallback(
    (node: HTMLInputElement | null) => {
      element.current = node;
      if (typeof ref === "function") ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );
  const ariaInvalid = rest["aria-invalid"];
  const ariaRequired = rest["aria-required"];
  const invalidDecided = Boolean(rest.error);
  const requiredDecided = Boolean(rest.required);
  React.useLayoutEffect(() => {
    const node = element.current;
    if (!node) return;
    const keep = (name: string, value: unknown, libraryDecides: boolean) => {
      if (libraryDecides) return;
      if (value === undefined || value === null) node.removeAttribute(name);
      else node.setAttribute(name, String(value));
    };
    keep("aria-invalid", ariaInvalid, invalidDecided);
    keep("aria-required", ariaRequired, requiredDecided);
  }, [ariaInvalid, ariaRequired, invalidDecided, requiredDecided]);
  return (
    <FdsInput
      ref={setRefs}
      {...rest}
      className={cn(INVALID_INPUT_CLASS[invalidTone], HELPER_ROW_CLASS, compact && COMPACT_INPUT_CLASS, className)}
      type={type as FdsInputProps["type"]}
      value={value as FdsInputProps["value"]}
    />
  );
});
Input.displayName = "Input";

export { Input };
