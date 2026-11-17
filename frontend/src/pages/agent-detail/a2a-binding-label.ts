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

/** The badge per direction, read by `t_i18n(a2aLabel)` in the Overview row,
 *  so every value is a catalog key `validate-i18n` checks. @i18n-keys */
const A2A_BINDING_LABELS = {
  inbound: "Exposes agent",
  outbound: "Delegates to partner",
} as const;

/** What an agent's A2A binding badge says, or `null` for no badge.
 *
 * Binding an inbound registration EXPOSES the agent to the partner; binding an
 * outbound one gives it a delegation tool - opposite consequences of one
 * action, so the badge follows the registration's direction. No badge while
 * the direction is unknown - still loading, or a lookup that failed - (never
 * state the wrong one first) or while A2A is not offered: neither is in effect
 * then, and a direction cached before AI was turned off must not keep showing
 * where a fresh load shows nothing.
 *
 * Returns the i18n message id; the caller translates it. */
export function a2aBindingLabel(
  offered: boolean,
  direction: keyof typeof A2A_BINDING_LABELS | undefined,
): (typeof A2A_BINDING_LABELS)[keyof typeof A2A_BINDING_LABELS] | null {
  if (!offered || direction === undefined) return null;
  return A2A_BINDING_LABELS[direction];
}
