// SPDX-License-Identifier: Apache-2.0

/**
 * Who besides the person can see a workspace (#6533, spec 032 FR-022). Its
 * members can search a conversation filed there, so filing one asks first
 * (`components/chat/useConfirmSharedFiling.ts`) and the project page says so.
 *
 * Pure, so `workspace-sharing.test.ts` runs under `node --test`.
 */

/** The sharing fields `workspaceReachesOthers` reads. */
interface SharedWorkspace {
  user_id: string | null;
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_user_ids?: string[] | null;
}

/**
 * Whether people other than *userId* can see a workspace: someone else owns
 * it, it is company-managed, or it is shared with a person or a group. The
 * backend's `workspaces.workspace_reaches_others`.
 */
export function workspaceReachesOthers(workspace: SharedWorkspace, userId: string | undefined): boolean {
  const others = (workspace.shared_user_ids ?? []).filter((id) => id !== userId);
  return (
    !!workspace.is_company_managed ||
    !!workspace.shared_group_ids?.length ||
    others.length > 0 ||
    (workspace.user_id !== null && workspace.user_id !== userId)
  );
}
