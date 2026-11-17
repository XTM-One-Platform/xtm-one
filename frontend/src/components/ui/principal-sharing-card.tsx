// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo, useRef } from "react";
import { UsersRound, User as UserIcon, X, Search, ChevronDown, Lock } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useGroups } from "@/hooks/api/useGroupApi";
import { useUserDirectory, useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import type { UserSharingProps } from "@/hooks/useUserShareGrants";
import {
  SHARE_PERMISSIONS,
  SHARE_PERMISSION_LABELS,
  SHARE_PERMISSION_DESCRIPTIONS,
} from "@/lib/constants";

/**
 * PrincipalSharingCard — share an entity with individual people *and* groups.
 *
 * Replaces the group-only card. Sharing something sensitive with one
 * colleague previously meant sharing it with a whole group, or asking an admin
 * to create a two-person group; both hand access to people who were never
 * meant to have it.
 *
 * Both principal kinds are rendered in one list and carry the same permission
 * tiers, because they mean the same thing to the backend: a user grant is not
 * a weaker concept than a group grant, and the strongest of the two wins when
 * someone is reached by both.
 *
 * For company-managed entities the card switches role entirely — see
 * `accessRestricted`. There, grants do not *widen* access (everyone already
 * has it); they *narrow* it.
 */
export function PrincipalSharingCard({
  sharedGroupIds,
  onChange,
  sharedGroupPermissions,
  onPermissionsChange,
  userSharing,
  entityType,
  isCompanyManaged = false,
  disabled = false,
  permissionLabels = SHARE_PERMISSION_LABELS,
  permissionDescriptions = SHARE_PERMISSION_DESCRIPTIONS,
  withPermissions = true,
  restrictionCopy,
}: {
  sharedGroupIds: string[] | null;
  onChange: (ids: string[] | null) => void;
  sharedGroupPermissions?: Record<string, string> | null;
  onPermissionsChange?: (perms: Record<string, string> | null) => void;
  /** Per-user grants + the restriction flag. Omitted = groups only. */
  userSharing?: UserSharingProps;
  entityType?: string;
  /** Flips the card from "who else can see this" to "who may see this". */
  isCompanyManaged?: boolean;
  disabled?: boolean;
  /**
   * Wording of the permission tiers, keyed by tier id. The defaults speak of
   * "configuration", which fits an agent or an integration; an entity whose
   * tiers mean something else (a presentation: viewer vs editor) passes its own
   * maps so the picker says what the grant actually does there.
   */
  permissionLabels?: Record<string, string>;
  permissionDescriptions?: Record<string, string>;
  /** False for an access list with no tiers (an LLM provider: one may use it or not). */
  withPermissions?: boolean;
  /**
   * The restriction's wording on a company-wide resource, untranslated (the
   * card translates it): what "Restrict access" means there and what an
   * empty list leaves.
   */
  restrictionCopy?: { title: string; restricted: string; open: string; empty: string };
}) {
  const { t_i18n } = useFormatter();
  const { data: groups } = useGroups();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const dropdownRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // The directory endpoint requires a search term, so people only appear once
  // the user has typed. Groups are already fully loaded, so they filter locally.
  const { data: directoryUsers, isFetching: isSearchingUsers } = useUserDirectory(search);

  useClickOutside(dropdownRef, () => { setOpen(false); setSearch(""); }, open);

  const restricted = userSharing?.accessRestricted ?? false;
  // On a company-managed entity the grants only apply while the restriction is
  // on; on a personal one the toggle is what turns sharing on at all. While
  // editing, the group list is the on/off marker (`[]` = on, nothing chosen
  // yet) — but the backend stores no empty list, so an entity shared with
  // people only comes back with `shared_group_ids = null`. A stored grant of
  // EITHER kind therefore means sharing is on; reading the group list alone
  // reopened such an entity as "private" and hid the very people it lists.
  const enabled = isCompanyManaged
    ? restricted
    : sharedGroupIds !== null || (userSharing?.userIds?.length ?? 0) > 0;

  const selectedGroupIds = useMemo(() => new Set(sharedGroupIds ?? []), [sharedGroupIds]);
  const selectedUserIds = useMemo(
    () => new Set(userSharing?.userIds ?? []),
    [userSharing?.userIds],
  );
  const groupPerms = sharedGroupPermissions ?? {};
  const userPerms = userSharing?.userPermissions ?? {};

  // The strongest tier is offered where it means more than editing: an agent's chats, or an entity whose own
  // labels name it (a dashboard's "can manage").
  const visiblePermissions = useMemo(() => {
    if (entityType === "agent" || (permissionLabels !== SHARE_PERMISSION_LABELS && "config_with_chats" in permissionLabels)) {
      return [...SHARE_PERMISSIONS];
    }
    return SHARE_PERMISSIONS.filter((p) => p !== "config_with_chats");
  }, [entityType, permissionLabels]);
  // The select shows the tier's name at full size; what it allows is the 12px line under the person or group (UX #9752 B3).
  const permissionLabel = (perm: string) => t_i18n(permissionLabels[perm] ?? SHARE_PERMISSION_LABELS[perm]);
  const permissionMeaning = (perm: string) => t_i18n(permissionDescriptions[perm] ?? SHARE_PERMISSION_DESCRIPTIONS[perm]);

  const filteredGroups = useMemo(() => {
    if (!groups) return [];
    if (!search) return groups;
    const q = search.toLowerCase();
    return groups.filter(
      (g) => g.name.toLowerCase().includes(q) || (g.description || "").toLowerCase().includes(q),
    );
  }, [groups, search]);

  const selectedGroups = useMemo(
    () => (groups ?? []).filter((g) => selectedGroupIds.has(g.id)),
    [groups, selectedGroupIds],
  );

  // Selected people must stay listed even when the search box is empty or has
  // moved on to another term — the search query only ever returns matches for
  // the *current* term. Labels come from two places: ids chosen in this session
  // are remembered here, and ids already stored on the entity are resolved by
  // id, which also covers grantees who have since been deactivated (they are
  // absent from search results but must not degrade to a bare UUID).
  const [userLabels, setUserLabels] = useState<Record<string, string>>({});
  const { data: resolvedUsers } = useUserDirectoryByIds(userSharing?.userIds);
  const selectedUsers = useMemo(() => {
    const resolved = new Map((resolvedUsers ?? []).map((u) => [u.id, u]));
    return (userSharing?.userIds ?? []).map((id) => {
      const hit = resolved.get(id);
      const label = hit
        ? hit.full_name?.trim()
          ? `${hit.full_name} (${hit.email})`
          : hit.email
        : userLabels[id];
      return {
        id,
        // Falling back to the raw id is deliberate: an id with no matching
        // account is a stale grant, and showing it is what lets someone
        // remove it.
        label: label ?? id,
        isInactive: hit ? hit.is_active === false : false,
      };
    });
  }, [userSharing?.userIds, userLabels, resolvedUsers]);

  const handleToggle = (checked: boolean) => {
    if (isCompanyManaged) {
      userSharing?.onAccessRestrictedChange(checked);
      if (checked) {
        onChange(sharedGroupIds ?? []);
        onPermissionsChange?.(sharedGroupPermissions ?? {});
        setOpen(true);
      }
      return;
    }
    if (checked) {
      onChange(sharedGroupIds ?? []);
      onPermissionsChange?.(sharedGroupPermissions ?? {});
      setOpen(true);
    } else {
      onChange(null);
      onPermissionsChange?.(null);
      userSharing?.onUserIdsChange(null);
      userSharing?.onUserPermissionsChange(null);
      setOpen(false);
      setSearch("");
    }
  };

  const handleSelectGroup = (groupId: string) => {
    const current = sharedGroupIds ?? [];
    if (selectedGroupIds.has(groupId)) {
      const next = current.filter((id) => id !== groupId);
      onChange(next.length > 0 ? next : []);
      const nextPerms = { ...groupPerms };
      delete nextPerms[groupId];
      onPermissionsChange?.(Object.keys(nextPerms).length > 0 ? nextPerms : {});
    } else {
      onChange([...current, groupId]);
      onPermissionsChange?.({ ...groupPerms, [groupId]: "read_only" });
    }
    setSearch("");
    inputRef.current?.focus();
  };

  const handleSelectUser = (userId: string, label: string) => {
    if (!userSharing) return;
    const current = userSharing.userIds ?? [];
    if (selectedUserIds.has(userId)) {
      const next = current.filter((id) => id !== userId);
      // When the card is on because of people alone, removing the last one
      // must not flip it off under the user's hand: leave the group marker
      // in place (an empty list, exactly what a fresh toggle-on sets) so the
      // picker stays open until sharing is turned off deliberately.
      if (next.length === 0 && sharedGroupIds === null) {
        onChange([]);
        onPermissionsChange?.(sharedGroupPermissions ?? {});
      }
      userSharing.onUserIdsChange(next.length > 0 ? next : []);
      const nextPerms = { ...userPerms };
      delete nextPerms[userId];
      userSharing.onUserPermissionsChange(Object.keys(nextPerms).length > 0 ? nextPerms : {});
    } else {
      setUserLabels((prev) => ({ ...prev, [userId]: label }));
      userSharing.onUserIdsChange([...current, userId]);
      userSharing.onUserPermissionsChange({ ...userPerms, [userId]: "read_only" });
    }
    setSearch("");
    inputRef.current?.focus();
  };

  const description = isCompanyManaged
    ? enabled
      ? t_i18n(restrictionCopy?.restricted ?? "Only the people and groups below can see this. Everyone else on the platform cannot.")
      : t_i18n(restrictionCopy?.open ?? "Visible to everyone on the platform. Toggle on to limit it to specific people and groups.")
    : enabled
      ? t_i18n("Visible to the people and groups below.")
      : t_i18n("Private to you. Toggle on to share with specific people or groups.");

  return (
    <div className="flex flex-col gap-3 rounded-sm bg-elevation-hover p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {isCompanyManaged ? (
            <Lock className="h-4 w-4 text-accent" />
          ) : (
            <UsersRound className="h-4 w-4 text-accent" />
          )}
          <div>
            <p className="content-base font-medium">
              {isCompanyManaged
                ? t_i18n(restrictionCopy?.title ?? "Restrict access")
                : t_i18n("Share with people and groups")}
            </p>
            <p className="content-compact text-default-secondary">{description}</p>
          </div>
        </div>
        <Switch
          aria-label={
            isCompanyManaged
              ? t_i18n(restrictionCopy?.title ?? "Restrict access")
              : t_i18n("Share with people and groups")
          }
          checked={enabled}
          onCheckedChange={handleToggle}
          disabled={disabled || (isCompanyManaged && !userSharing)}
        />
      </div>

      {enabled && (
        <div className="relative" ref={dropdownRef}>
          {/* Selected principals — people first, then groups */}
          {(selectedUsers.length > 0 || selectedGroups.length > 0) && (
            <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden mb-2">
              {selectedUsers.map((u, i) => (
                <div
                  key={`user-${u.id}`}
                  className={`flex items-center gap-3 px-3 py-2 ${
                    i > 0 ? "border-t border-elevation-subtle-soft" : ""
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 min-w-0">
                      <UserIcon className="h-3 w-3 text-default-secondary shrink-0" />
                      <Tip content={u.label}>
                        <span className="content-compact font-medium truncate">{u.label}</span>
                      </Tip>
                      {u.isInactive && (
                        <span className="shrink-0 rounded-sm bg-feedback-alert-primary/10 px-1.5 py-0.5 content-caption text-feedback-warning-tertiary">
                          {t_i18n("Deactivated")}
                        </span>
                      )}
                    </div>
                    {userSharing && withPermissions && (
                      <p className="content-compact text-default-secondary">
                        {permissionMeaning(userPerms[u.id] || "read_only")}
                      </p>
                    )}
                  </div>
                  {userSharing && withPermissions && (
                    <Select
                      value={userPerms[u.id] || "read_only"}
                      onValueChange={(v) =>
                        userSharing.onUserPermissionsChange({ ...userPerms, [u.id]: v })
                      }
                      disabled={disabled}
                    >
                      <SelectTrigger aria-label={t_i18n("Permission of {name}", { name: u.label })} className="w-[200px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {visiblePermissions.map((perm) => (
                          <SelectItem key={perm} value={perm}>
                            {permissionLabel(perm)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                  <button
                    aria-label={t_i18n("Remove user")}
                    className="shrink-0 rounded-sm p-0.5 text-default-secondary hover:text-feedback-error-primary hover:bg-elevation-hover transition-colors"
                    onClick={() => handleSelectUser(u.id, u.label)}
                    disabled={disabled}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
              {selectedGroups.map((g, i) => (
                <div
                  key={`group-${g.id}`}
                  className={`flex items-center gap-3 px-3 py-2 ${
                    i > 0 || selectedUsers.length > 0
                      ? "border-t border-elevation-subtle-soft"
                      : ""
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 min-w-0">
                      <UsersRound className="h-3 w-3 text-default-secondary shrink-0" />
                      <Tip content={g.name}>
                        <span className="content-compact font-medium truncate">{g.name}</span>
                      </Tip>
                    </div>
                    {onPermissionsChange && withPermissions && (
                      <p className="content-compact text-default-secondary">
                        {permissionMeaning(groupPerms[g.id] || "read_only")}
                      </p>
                    )}
                  </div>
                  {onPermissionsChange && withPermissions && (
                    <Select
                      value={groupPerms[g.id] || "read_only"}
                      onValueChange={(v) => onPermissionsChange({ ...groupPerms, [g.id]: v })}
                      disabled={disabled}
                    >
                      <SelectTrigger aria-label={t_i18n("Permission of {name}", { name: g.name })} className="w-[200px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {visiblePermissions.map((perm) => (
                          <SelectItem key={perm} value={perm}>
                            {permissionLabel(perm)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                  <button
                    aria-label={t_i18n("Remove group")}
                    className="shrink-0 rounded-sm p-0.5 text-default-secondary hover:text-feedback-error-primary hover:bg-elevation-hover transition-colors"
                    onClick={() => handleSelectGroup(g.id)}
                    disabled={disabled}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Search input */}
          <div className="relative">
            <Input
              ref={inputRef}
              aria-label={userSharing ? t_i18n("Search people or groups…") : t_i18n("Search groups...")}
              className="w-full"
              startIcon={<Search className="h-4 w-4" />}
              endIcon={{ type: "icon", icon: <ChevronDown className="h-4 w-4" /> }}
              placeholder={
                userSharing
                  ? t_i18n("Search people or groups…")
                  : t_i18n("Search groups...")
              }
              value={search}
              onChange={(e) => { setSearch(e.target.value); setOpen(true); }}
              onFocus={() => setOpen(true)}
              disabled={disabled}
            />
          </div>

          {/* Dropdown */}
          {open && (
            <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-48 overflow-y-auto">
              {userSharing && (
                <>
                  {search.trim().length < 2 ? (
                    <p className="px-3 py-2 content-caption uppercase tracking-wide text-default-secondary">
                      {t_i18n("Type at least 2 characters to find people")}
                    </p>
                  ) : (
                    (directoryUsers ?? []).map((u) => {
                      const label = u.full_name?.trim() ? `${u.full_name} (${u.email})` : u.email;
                      const selected = selectedUserIds.has(u.id);
                      return (
                        <button
                          key={`u-${u.id}`}
                          className={`flex items-center gap-2 w-full px-3 py-2 text-left content-base transition-colors ${
                            selected ? "bg-accent/10 text-accent" : "hover:bg-elevation-hover"
                          }`}
                          onClick={() => handleSelectUser(u.id, label)}
                        >
                          <UserIcon className="h-3 w-3 shrink-0" />
                          <span className="truncate flex-1">{label}</span>
                          {selected && <span className="content-caption text-accent">&#10003;</span>}
                        </button>
                      );
                    })
                  )}
                  {search.trim().length >= 2
                    && !isSearchingUsers
                    && (directoryUsers ?? []).length === 0 && (
                    <p className="px-3 py-2 content-compact text-default-secondary">
                      {t_i18n("No matching people.")}
                    </p>
                  )}
                </>
              )}

              {filteredGroups.length > 0 ? (
                filteredGroups.map((g) => {
                  const selected = selectedGroupIds.has(g.id);
                  return (
                    <button
                      key={`g-${g.id}`}
                      className={`flex items-center gap-2 w-full px-3 py-2 text-left content-base transition-colors ${
                        selected ? "bg-accent/10 text-accent" : "hover:bg-elevation-hover"
                      }`}
                      onClick={() => handleSelectGroup(g.id)}
                    >
                      <UsersRound className="h-3 w-3 shrink-0" />
                      <span className="truncate flex-1">{g.name}</span>
                      <span className="content-caption text-default-secondary shrink-0">
                        {t_i18n("{count, plural, one {# member} other {# members}}", { count: g.member_count })}
                      </span>
                      {selected && (
                        <span className="content-caption text-accent">&#10003;</span>
                      )}
                    </button>
                  );
                })
              ) : (
                <p className="px-3 py-2 content-compact text-default-secondary">
                  {groups && groups.length === 0 ? t_i18n("No groups created yet.") : t_i18n("No matching groups.")}
                </p>
              )}
            </div>
          )}

          {isCompanyManaged && selectedUsers.length === 0 && selectedGroups.length === 0 && (
            <p className="mt-2 content-caption text-feedback-warning-primary">
              {t_i18n(restrictionCopy?.empty ?? "No one is listed yet, so only admins and the owner can reach this.")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
