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
 * How a flow's lifecycle status is shown on its card.
 *
 * Keyed by `FlowStatus`, so a status added to the type without an entry here
 * fails the build instead of being drawn as something else. A value the map
 * does not know (a newer backend, a hand-edited payload) is shown AS ITSELF,
 * the way `StatusBadge` degrades - never as "Draft", which would misstate it.
 */

import type { FlowStatus } from "@/types";

interface FlowStatusDisplay {
  /** The known status, or null for a value outside `FlowStatus`. */
  status: FlowStatus | null;
  /** The tooltip / aria-label: an i18n key for a known status, the raw value otherwise. */
  label: string;
  className: string;
}

export const FLOW_STATUS_DISPLAY: Record<FlowStatus, Omit<FlowStatusDisplay, "status">> = {
  draft: { label: "Draft", className: "text-default-secondary" },
  active: { label: "Active", className: "text-feedback-success-primary" },
  archived: { label: "Archived", className: "text-feedback-warning-primary" },
};

export function isFlowStatus(value: string): value is FlowStatus {
  return Object.prototype.hasOwnProperty.call(FLOW_STATUS_DISPLAY, value);
}

export function flowStatusDisplay(value: string): FlowStatusDisplay {
  if (isFlowStatus(value)) {
    return { status: value, ...FLOW_STATUS_DISPLAY[value] };
  }
  return { status: null, label: value, className: "text-default-secondary" };
}
