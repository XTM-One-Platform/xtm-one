// SPDX-License-Identifier: Apache-2.0

// An element that is not a `<button>` but acts as one: a card whose
// paragraphs and badges a `<button>` cannot hold (its content model is phrasing
// content only). Tab reaches it, Enter and Space activate it as a click does,
// and assistive technology announces it as a button.

import type { KeyboardEvent } from "react";

/** The focus ring of an element given `buttonRoleProps()`: it shows where the
 *  keyboard is, and only for keyboard focus. It is the ring the design-system
 *  Button and the shared Tip wrapper draw (2 px offset plus 2 px `ring-focus`),
 *  so Tab moving from a button to a clickable card keeps one ring style.
 *  Gate: button-role-focus-ring.test.ts. */
export const BUTTON_ROLE_FOCUS_RING =
  "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus";

export interface ButtonRoleProps {
  role: "button";
  tabIndex: 0;
  onClick: () => void;
  /** Any element, HTML or SVG (an edge of the agent graph). */
  onKeyDown: (e: KeyboardEvent<Element>) => void;
}

/** The props that make an element act as a button calling `onActivate`, or
 *  none when there is nothing to activate (the element stays inert to the
 *  keyboard and to assistive technology). Only the element's own key presses
 *  activate it: Enter or Space bubbling up from a control inside it (an info
 *  button, a link) is that control's, not a request to activate the element. */
export function buttonRoleProps(onActivate: () => void): ButtonRoleProps;
export function buttonRoleProps(onActivate: (() => void) | undefined): ButtonRoleProps | Record<string, never>;
export function buttonRoleProps(onActivate: (() => void) | undefined): ButtonRoleProps | Record<string, never> {
  if (!onActivate) return {};
  return {
    role: "button",
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate();
      }
    },
  };
}
