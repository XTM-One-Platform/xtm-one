// SPDX-License-Identifier: Apache-2.0

/**
 * Who a shareable entity is shared with, and who may edit it, as the UI reads
 * it. Re-exported by `lib/utils.ts`, which is where pages import them from.
 *
 * No runtime imports: `yarn test:unit` loads this file under plain
 * `node --test`, which does not resolve the `@/` alias.
 */

/** The sharing columns every shareable entity response carries. */
export interface ShareableEntity {
  is_company_managed: boolean;
  user_id: string | null;
  shared_group_ids: string[] | null;
  shared_user_ids?: string[] | null;
  /** The requesting user's manage right, computed server-side. */
  can_manage?: boolean;
}

/**
 * Determine whether an entity is shared with the current user.
 * An entity is "shared" when it is NOT company-managed, belongs to another
 * user, and carries at least one grant — to a group or to a person directly.
 * Both principal kinds count: someone reached by a personal grant is
 * just as much a guest on another user's entity as a group member is.
 *
 * This is the single source of truth — do NOT inline the
 * `!entity.is_company_managed && !!entity.user_id && entity.user_id !== userId && !!entity.shared_group_ids?.length`
 * expression in page files.
 */
export function isEntityShared(
  entity: ShareableEntity | null | undefined,
  currentUserId: string | undefined,
): boolean {
  if (!entity) return false;
  if (entity.is_company_managed || !entity.user_id || entity.user_id === currentUserId) {
    return false;
  }
  return !!entity.shared_group_ids?.length || !!entity.shared_user_ids?.length;
}

/**
 * Whether the current user can manage (edit, delete, share) an entity.
 *
 * When the response carries `can_manage`, that flag is the answer: the backend
 * computes it for the requesting user with the same `can_manage()` helper that
 * guards every write, share tiers included — a `read_only` grantee sees the
 * entity but cannot change it, a `config` / `config_with_chats` grantee can.
 * A response without the flag only opens the edit surfaces to an
 * admin or the owner (company-managed: admins only): which tier reaches a
 * grantee is the server's to say, and a share alone must never offer an edit
 * the API may refuse.
 *
 * Locks that are not about who you are — built-ins, system-managed knowledge
 * bases, registration-only integrations, a custom tool or knowledge base a
 * skill package installed (`is_package_origin`) — stay the caller's to add on
 * top (`EntityConfigCard` takes them as `canEdit`, `isBuiltin` and
 * `isPackageOrigin`, see `configLockReason`).
 */
export function canManageEntity(
  entity: ShareableEntity | null | undefined,
  currentUserId: string | undefined,
  isAdmin: boolean,
): boolean {
  if (!entity) return false;
  if (typeof entity.can_manage === "boolean") return entity.can_manage;
  if (entity.is_company_managed) return isAdmin;
  return isAdmin || (!!entity.user_id && entity.user_id === currentUserId);
}

/** The agent fields the Chats tab gate reads. */
interface ChatViewableAgent {
  user_id: string | null;
  /** Whether the requesting user may read the agent's conversations,
   *  computed server-side. */
  can_view_chats?: boolean;
}

/**
 * Whether the current user may read the conversations other people had with
 * an agent — the agent's Chats tab.
 *
 * The server's `can_view_chats` answers when present: it is the rule the chats
 * API enforces (`helpers.can_view_agent_chats()` — an admin or the owner,
 * otherwise a `config_with_chats` grant, direct or through a group the user
 * belongs to). Without it, only the owner and admins.
 */
export function canViewAgentChats(
  agent: ChatViewableAgent | null | undefined,
  currentUserId: string | undefined,
  isAdmin: boolean,
): boolean {
  if (!agent) return false;
  if (typeof agent.can_view_chats === "boolean") return agent.can_view_chats;
  return isAdmin || (!!agent.user_id && agent.user_id === currentUserId);
}

/** Why a Configuration card offers no Edit button. */
type ConfigLockReason =
  | "builtin"
  | "package_origin"
  | "company_managed"
  | "read_only_share"
  | "locked";

/**
 * The reason an entity's configuration is not editable by the viewer, so the
 * card names the real cause instead of guessing it from one boolean.
 *
 * `canManage` is the viewer's own right (`canManageEntity()`); `canEdit` is
 * the page's locks that are not about the viewer (a registration-only
 * integration, a system-managed knowledge base). A lock of the second kind is
 * `"locked"` and the page explains it: it is never presented as a read-only
 * share, and an admin is never told that only admins may edit. A built-in is
 * read-only for everyone, and so is an entity a skill package installed
 * (`is_package_origin`, the write routes' own answer).
 */
export function configLockReason({
  isShared,
  isCompanyManaged,
  isBuiltin,
  isPackageOrigin = false,
  canManage,
  canEdit,
}: {
  isShared: boolean;
  isCompanyManaged: boolean;
  isBuiltin: boolean;
  isPackageOrigin?: boolean;
  canManage: boolean;
  canEdit: boolean;
}): ConfigLockReason | null {
  if (isBuiltin) return "builtin";
  if (isPackageOrigin) return "package_origin";
  if (!canManage) {
    if (isCompanyManaged) return "company_managed";
    if (isShared) return "read_only_share";
    return "locked";
  }
  return canEdit ? null : "locked";
}
