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
 * What an enrollment key gives the machine it enrolls, preset by preset.
 * Pure, so `yarn test:unit` can check it without a browser.
 */

import type { RunnerCapability, RunnerPermissionMode } from "@/types";

export type EnrollmentPreset = "desktop" | "locked" | "custom";

/** The Locked down preset: screenshots and browser control only. */
export const LOCKED_DOWN_CAPS: RunnerCapability[] = ["screenshot", "browser_control"];

/** The key form's controls, as the person left them. */
export interface EnrollmentControls {
  /** Every capability this instance offers. */
  availableCaps: RunnerCapability[];
  /** Custom only: the permission mode and the capabilities picked. */
  mode: RunnerPermissionMode;
  selectedCaps: RunnerCapability[];
  /** The "Shell commands need approval" switch. */
  requireApproval: boolean;
}

/** The permission model a key carries into the machines it enrolls. */
interface EnrollmentPermissions {
  mode: RunnerPermissionMode;
  caps: RunnerCapability[];
  approval: boolean;
}

/** Whether the form offers the approval switch for this preset and mode. */
export function offersApprovalSwitch(preset: EnrollmentPreset, mode: RunnerPermissionMode): boolean {
  return preset === "desktop" || (preset === "custom" && mode === "restricted");
}

/**
 * The permission model a preset resolves to (Custom = the controls as set).
 * Shell approval is the person's switch on every preset that can hold a
 * command, never implied by the preset itself.
 */
export function resolveEnrollmentPermissions(
  preset: EnrollmentPreset,
  controls: EnrollmentControls,
): EnrollmentPermissions {
  if (preset === "desktop") {
    return { mode: "restricted", caps: controls.availableCaps, approval: controls.requireApproval };
  }
  if (preset === "locked") {
    return { mode: "restricted", caps: LOCKED_DOWN_CAPS, approval: false };
  }
  return {
    mode: controls.mode,
    caps: controls.mode === "full_control" ? [] : controls.selectedCaps,
    approval: controls.mode === "restricted" && controls.requireApproval,
  };
}
