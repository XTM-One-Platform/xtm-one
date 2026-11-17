/**
 * Unit tests for the sharing / manage-right helpers - `yarn test:unit`.
 *
 * A read-only share used to open the whole edit form: `canManageEntity`
 * treated any grant as a manage right. The server now says, per entity, what
 * the requesting user may do (`can_manage`, computed by the backend's own
 * `can_manage()`), and that flag wins; a response that does not carry it only
 * opens the edit surfaces to the owner and admins.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canManageEntity,
  canViewAgentChats,
  configLockReason,
  isEntityShared,
  type ShareableEntity,
} from "./entity-permissions.ts";

const ME = "user-me";
const OWNER = "user-owner";

const entity = (overrides: Partial<ShareableEntity> = {}): ShareableEntity => ({
  is_company_managed: false,
  user_id: OWNER,
  shared_group_ids: null,
  shared_user_ids: null,
  ...overrides,
});

test("a missing entity is neither shared nor manageable", () => {
  assert.equal(isEntityShared(null, ME), false);
  assert.equal(isEntityShared(undefined, ME), false);
  assert.equal(canManageEntity(null, ME, true), false);
  assert.equal(canManageEntity(undefined, ME, true), false);
});

test("isEntityShared: another user's entity carrying a group or a personal grant", () => {
  assert.equal(isEntityShared(entity({ shared_group_ids: ["g1"] }), ME), true);
  assert.equal(isEntityShared(entity({ shared_user_ids: [ME] }), ME), true);
  assert.equal(isEntityShared(entity(), ME), false, "no grant at all");
  assert.equal(isEntityShared(entity({ user_id: ME, shared_group_ids: ["g1"] }), ME), false, "my own entity");
  assert.equal(isEntityShared(entity({ user_id: null, shared_group_ids: ["g1"] }), ME), false, "no owner");
  assert.equal(
    isEntityShared(entity({ is_company_managed: true, shared_group_ids: ["g1"] }), ME),
    false,
    "company-managed is not a share",
  );
});

test("the server flag answers when present: a read-only share cannot manage", () => {
  // The case: shared through a group (or directly) with the read_only
  // tier. The fallback rule would say true; the server says false.
  assert.equal(canManageEntity(entity({ shared_group_ids: ["g1"], can_manage: false }), ME, false), false);
  assert.equal(canManageEntity(entity({ shared_user_ids: [ME], can_manage: false }), ME, false), false);
});

test("the server flag answers when present: a config share can manage", () => {
  assert.equal(canManageEntity(entity({ shared_group_ids: ["g1"], can_manage: true }), ME, false), true);
  assert.equal(canManageEntity(entity({ shared_user_ids: [ME], can_manage: true }), ME, false), true);
});

test("the server flag is not second-guessed by the local role or ownership", () => {
  // The backend already folds admin, owner and company-managed into the flag,
  // so the browser never re-derives any of it once the flag is there.
  assert.equal(canManageEntity(entity({ is_company_managed: true, can_manage: true }), ME, false), true);
  assert.equal(canManageEntity(entity({ is_company_managed: true, can_manage: false }), ME, true), false);
  assert.equal(canManageEntity(entity({ user_id: ME, can_manage: false }), ME, false), false);
  assert.equal(canManageEntity(entity({ can_manage: true }), ME, false), true);
});

test("without the flag, only the owner and admins may manage", () => {
  // Company-managed: admins only.
  assert.equal(canManageEntity(entity({ is_company_managed: true, user_id: null }), ME, true), true);
  assert.equal(canManageEntity(entity({ is_company_managed: true, user_id: null }), ME, false), false);
  assert.equal(canManageEntity(entity({ is_company_managed: true, user_id: ME }), ME, false), false);
  // Personal: the owner or an admin.
  assert.equal(canManageEntity(entity({ user_id: ME }), ME, false), true);
  assert.equal(canManageEntity(entity(), ME, true), true);
  assert.equal(canManageEntity(entity(), ME, false), false);
});

test("without the flag, a share never opens the edit form", () => {
  // The payload does not say which tier reaches the viewer, and a read-only
  // grantee must never be offered an edit the API refuses: a share
  // alone grants nothing until the server says so.
  assert.equal(canManageEntity(entity({ shared_group_ids: ["g1"] }), ME, false), false);
  assert.equal(canManageEntity(entity({ shared_user_ids: [ME] }), ME, false), false);
  assert.equal(canManageEntity(entity({ shared_group_ids: ["g1"], shared_user_ids: [ME] }), ME, false), false);
});

test("canViewAgentChats: the server flag answers when present", () => {
  const agent = { user_id: OWNER, can_view_chats: true };
  assert.equal(canViewAgentChats(agent, ME, false), true, "a config_with_chats grantee");
  assert.equal(canViewAgentChats({ ...agent, can_view_chats: false }, ME, false), false, "a read-only grantee");
  // Not second-guessed by ownership or role either.
  assert.equal(canViewAgentChats({ user_id: ME, can_view_chats: false }, ME, true), false);
});

test("canViewAgentChats: without the flag, only the owner and admins see the chats", () => {
  assert.equal(canViewAgentChats(null, ME, true), false);
  assert.equal(canViewAgentChats(undefined, ME, true), false);
  assert.equal(canViewAgentChats({ user_id: ME }, ME, false), true, "the owner");
  assert.equal(canViewAgentChats({ user_id: OWNER }, ME, true), true, "an admin");
  assert.equal(canViewAgentChats({ user_id: OWNER }, ME, false), false, "anyone else, whatever it is shared with");
  assert.equal(canViewAgentChats({ user_id: null }, undefined, false), false);
});

const card = (overrides: Partial<Parameters<typeof configLockReason>[0]> = {}) =>
  configLockReason({
    isShared: false,
    isCompanyManaged: false,
    isBuiltin: false,
    isPackageOrigin: false,
    canManage: true,
    canEdit: true,
    ...overrides,
  });

test("configLockReason: nothing to explain when the viewer may edit", () => {
  assert.equal(card(), null);
  assert.equal(card({ isShared: true }), null, "a config share");
  assert.equal(card({ isCompanyManaged: true }), null, "an admin on a company-managed entity");
});

test("configLockReason: a read-only share is explained as one", () => {
  assert.equal(card({ isShared: true, canManage: false }), "read_only_share");
});

test("configLockReason: a lock that is not about the viewer is never called a read-only share", () => {
  // A registration-only integration shared with a config
  // grantee - the viewer HAS the right, the page locks it for another reason.
  assert.equal(card({ isShared: true, canEdit: false }), "locked");
  // An admin on a company-managed entity the page locks is not told that only
  // admins may edit it.
  assert.equal(card({ isCompanyManaged: true, canEdit: false }), "locked");
  assert.equal(card({ canEdit: false }), "locked");
});

test("configLockReason: company-managed for a viewer without the right", () => {
  assert.equal(card({ isCompanyManaged: true, canManage: false }), "company_managed");
  assert.equal(card({ isCompanyManaged: true, canManage: false, canEdit: false }), "company_managed");
});

test("configLockReason: a package-installed entity is locked by its skill package, for everyone", () => {
  // The write routes refuse it whoever asks, so the owner and an admin
  // (who keep their manage right) are told why, and never offered an Edit.
  assert.equal(card({ isPackageOrigin: true }), "package_origin");
  assert.equal(card({ isPackageOrigin: true, isCompanyManaged: true }), "package_origin");
  // A read-only grantee is told about the package too: that is the lock that
  // would remain even with configuration access.
  assert.equal(card({ isPackageOrigin: true, isShared: true, canManage: false }), "package_origin");
  assert.equal(card({ isPackageOrigin: true, canEdit: false }), "package_origin");
  assert.equal(card({ isPackageOrigin: false }), null);
});

test("configLockReason: a built-in outranks a package origin", () => {
  assert.equal(card({ isBuiltin: true, isPackageOrigin: true }), "builtin");
});

test("configLockReason: a built-in is read-only for everyone, whatever the rest says", () => {
  assert.equal(card({ isBuiltin: true }), "builtin", "an admin, who otherwise may manage it");
  assert.equal(card({ isBuiltin: true, canManage: false, isCompanyManaged: true }), "builtin");
  assert.equal(card({ isBuiltin: true, canManage: false, isShared: true }), "builtin");
});
