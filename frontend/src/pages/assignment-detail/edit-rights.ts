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
 * What the viewer may do on an assignment page.
 *
 * Built-in agents (and their assignments) are immutable in XTM One mode -
 * mirroring the backend `writable_assignment_error` guard so the UI disables
 * edits (run-as, hide-runs, prompt, ...) instead of letting them 403 on save.
 * Two openings on a built-in, both an administrator's:
 * - a seeded opt-in assignment: its switch and run-as account, and
 *   nothing else (the backend's `ADMIN_OPT_IN_WRITABLE_FIELDS`);
 * - starting a run, which is not editing the built-in: the backend lets an
 *   administrator trigger any assignment they can see (`manual_trigger_refusal`).
 */
interface AssignmentEditRights {
  /** Full edit: prompt, trigger, channels, delete. */
  canManage: boolean;
  /** Pause / resume. */
  canToggle: boolean;
  /** The run-as account alone, on a built-in's opt-in assignment. */
  canChooseRunAs: boolean;
  /** Run Now. */
  canRunNow: boolean;
}

export function assignmentEditRights({
  agentIsBuiltin,
  isXtmOne,
  hasManageRights,
  isAdmin,
  builtinAdminOptIn,
}: {
  agentIsBuiltin: boolean;
  isXtmOne: boolean;
  hasManageRights: boolean;
  isAdmin: boolean;
  builtinAdminOptIn: boolean;
}): AssignmentEditRights {
  const readOnlyBuiltin = agentIsBuiltin && isXtmOne;
  const canManage = hasManageRights && !readOnlyBuiltin;
  const adminOptIn = readOnlyBuiltin && isAdmin && builtinAdminOptIn;
  return {
    canManage,
    canToggle: canManage || adminOptIn,
    canChooseRunAs: adminOptIn,
    canRunNow: canManage || (readOnlyBuiltin && isAdmin),
  };
}
