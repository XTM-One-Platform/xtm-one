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

/** The `paused_reason` the backend stamps on assignments an emergency stop paused. */
const PANIC_STOP_REASON = "panic_stop";

/** Whether an assignment is paused BY the emergency stop (not by hand, not by a quarantine). */
export function isEmergencyStopped(assignment: { enabled: boolean; paused_reason?: string | null }): boolean {
  return !assignment.enabled && assignment.paused_reason === PANIC_STOP_REASON;
}
