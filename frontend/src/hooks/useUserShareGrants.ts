// SPDX-License-Identifier: Apache-2.0

/**
 * Edit-form state for per-user share grants and company-managed access
 * restriction.
 *
 * Every entity config page already juggles two pieces of sharing state
 * (`shared_group_ids` + `shared_group_permissions`) by hand. Rather than have
 * each of the nine surfaces grow three more `useState` calls, three lines in
 * its `startEditing` reset and three in its save payload — thirty-odd chances
 * to diverge — the new fields live here as one controller.
 *
 * Deliberately scoped to the *new* fields: the group ones are left exactly as
 * they are so this change cannot alter existing sharing behaviour.
 */

import { useCallback, useState } from "react";

/** The subset of an entity this hook reads its initial values from. */
interface UserShareGrantSource {
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

/** What the entity's update payload needs to carry. */
interface UserShareGrantPayload {
  shared_user_ids: string[] | null;
  shared_user_permissions: Record<string, string> | null;
  access_restricted: boolean;
}

/** Props consumed by `EntityConfigCard` / `PrincipalSharingCard`. */
export interface UserSharingProps {
  userIds: string[] | null;
  onUserIdsChange: (ids: string[] | null) => void;
  userPermissions: Record<string, string> | null;
  onUserPermissionsChange: (perms: Record<string, string> | null) => void;
  accessRestricted: boolean;
  onAccessRestrictedChange: (value: boolean) => void;
}

interface UserShareGrantsController {
  /** Pass straight to `EntityConfigCard`'s `userSharing` prop. */
  props: UserSharingProps;
  /** Re-seed from the entity — call when an edit session starts. */
  reset: () => void;
  /**
   * Build the payload fields for a save.
   *
   * `isCompanyManaged` is the value being saved, not the stored one: a
   * personal entity cannot carry `access_restricted` (the API rejects it), and
   * an unrestricted company-managed entity has no use for grants because
   * everyone can already see it. Normalising here means no caller has to
   * remember either rule.
   */
  toPayload: (isCompanyManaged: boolean) => UserShareGrantPayload;
}

export function useUserShareGrants(
  entity: UserShareGrantSource | null | undefined,
): UserShareGrantsController {
  const [userIds, setUserIds] = useState<string[] | null>(
    entity?.shared_user_ids ?? null,
  );
  const [userPermissions, setUserPermissions] = useState<Record<string, string> | null>(
    entity?.shared_user_permissions ?? null,
  );
  const [accessRestricted, setAccessRestricted] = useState<boolean>(
    entity?.access_restricted ?? false,
  );

  const reset = useCallback(() => {
    setUserIds(entity?.shared_user_ids ?? null);
    setUserPermissions(entity?.shared_user_permissions ?? null);
    setAccessRestricted(entity?.access_restricted ?? false);
  }, [entity]);

  const toPayload = useCallback(
    (isCompanyManaged: boolean): UserShareGrantPayload => {
      const restricted = isCompanyManaged && accessRestricted;
      // Grants only mean something when the entity is personal, or when it is
      // company-managed AND narrowed to them.
      const keepGrants = !isCompanyManaged || restricted;
      const ids = keepGrants && userIds && userIds.length > 0 ? userIds : null;
      const perms =
        ids && userPermissions && Object.keys(userPermissions).length > 0
          ? userPermissions
          : null;
      return {
        shared_user_ids: ids,
        shared_user_permissions: perms,
        access_restricted: restricted,
      };
    },
    [accessRestricted, userIds, userPermissions],
  );

  return {
    props: {
      userIds,
      onUserIdsChange: setUserIds,
      userPermissions,
      onUserPermissionsChange: setUserPermissions,
      accessRestricted,
      onAccessRestrictedChange: setAccessRestricted,
    },
    reset,
    toPayload,
  };
}
