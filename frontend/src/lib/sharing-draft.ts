// SPDX-License-Identifier: Apache-2.0

/**
 * The sharing state a share dialog edits, and what it says about it.
 *
 * Shared by the dialogs that share an entity from a card or a toolbar rather
 * than from a configuration page - a presentation, a workspace:
 * whether saving would change anything, and who the DRAFT lets in,
 * said in words so nobody has to infer it from two switches.
 *
 * Pure (no React, no `@/` import) so `sharing-draft.test.ts` runs under
 * `node --test`; the translator is passed in.
 */

export interface SharingDraft {
  companyManaged: boolean;
  accessRestricted: boolean;
  groupIds: string[] | null;
  groupPermissions: Record<string, string> | null;
  userIds: string[] | null;
  userPermissions: Record<string, string> | null;
}

/**
 * The sharing state in the shape a save sends it — what "no change" has to be
 * measured against. `[]` and `null` both mean "no grants" to the backend (the
 * payload normalises them the same way), and a permission left behind for a
 * principal no longer listed is dropped on save, so neither may count as a
 * pending change. An unrestricted company-managed entity keeps no grants.
 */
export function canonicalSharing(state: SharingDraft): string {
  const restricted = state.companyManaged && state.accessRestricted;
  const keepGrants = !state.companyManaged || restricted;
  const grants = (ids: string[] | null, perms: Record<string, string> | null) => {
    if (!keepGrants || !ids || ids.length === 0) return [];
    return [...ids].sort().map((id) => [id, perms?.[id] || "read_only"]);
  };
  return JSON.stringify({
    company: state.companyManaged,
    restricted,
    groups: grants(state.groupIds, state.groupPermissions),
    users: grants(state.userIds, state.userPermissions),
  });
}

/** Whether saving *draft* would change what *stored* holds. */
export function sharingChanged(draft: SharingDraft, stored: SharingDraft): boolean {
  return canonicalSharing(draft) !== canonicalSharing(stored);
}

/** Which icon the audience line wears: a lock, the company, or people. */
type SharingAudienceKind = "private" | "restricted" | "company" | "shared";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * Who the draft lets in, in words — the same rule the backend's three-tier
 * visibility applies.
 */
export function sharingAudience(
  state: SharingDraft,
  t_i18n: Translate,
): { kind: SharingAudienceKind; text: string } {
  const people = state.userIds?.length ?? 0;
  const groups = state.groupIds?.length ?? 0;
  if (state.companyManaged) {
    return state.accessRestricted
      ? {
          kind: "restricted",
          text: t_i18n("Admins, the owner, and the people and groups listed above."),
        }
      : { kind: "company", text: t_i18n("Everyone in the company.") };
  }
  if (people + groups === 0) {
    // Nobody listed: either sharing is off (no group marker, no one stored)
    // or it was just switched on and no one is chosen yet. An entity shared
    // with people only carries no group list at all — that is why the person
    // count decides this branch, not the group list.
    return {
      kind: "private",
      text:
        state.groupIds === null
          ? t_i18n("Only you, until you share it above.")
          : t_i18n("Only you: no one is listed above yet."),
    };
  }
  if (people > 0 && groups > 0) {
    return {
      kind: "shared",
      text: t_i18n(
        "You, {people, plural, one {# person} other {# people}} and {groups, plural, one {# group} other {# groups}} listed above.",
        { people, groups },
      ),
    };
  }
  if (people > 0) {
    return {
      kind: "shared",
      text: t_i18n("You and {people, plural, one {# person} other {# people}} listed above.", {
        people,
      }),
    };
  }
  return {
    kind: "shared",
    text: t_i18n("You and {groups, plural, one {# group} other {# groups}} listed above.", {
      groups,
    }),
  };
}

/** The group and person fields of an update, empty lists sent as `null`. */
export function sharingPayloadGroups(draft: SharingDraft): {
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
} {
  const has = !!draft.groupIds && draft.groupIds.length > 0;
  return {
    shared_group_ids: has ? draft.groupIds : null,
    shared_group_permissions: has ? draft.groupPermissions : null,
  };
}
