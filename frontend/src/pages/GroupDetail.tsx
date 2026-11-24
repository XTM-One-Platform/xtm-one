// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import {
  UsersRound, Trash2, X, Users, Pencil, FileText, Gauge, Save, Zap, Key, UserCog, Sparkles,
} from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { NotFoundState } from "@/components/ui/not-found-state";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import {
  useAddGroupManagers,
  useAddGroupMembers,
  useDeleteGroup,
  useGroup,
  useGroupManagers,
  useGroupMembers,
  useRemoveGroupManager,
  useRemoveGroupMember,
  useUpdateGroup,
} from "@/hooks/api/useGroupApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useAvailableModels, usePlatformQuotas } from "@/hooks/api/useSettingsApi";
import { QuotaEditor } from "@/components/ui/quota-editor";
import { useToast } from "@/components/ui/toast";
import { isUnlimitedOverride, normalizeQuotaOverride } from "@/components/ui/quota-override";
import type { Group, GroupCtemAccess, GroupKind, GroupMemberItem, QuotaTypeConfig } from "@/types";
import { useIsXtmOne, useAgenticQuotas, useIsCommunityEdition } from "@/hooks/usePlatformConfig";
import { useTabParam } from "@/hooks/useTabParam";
import { BACKEND_FETCH_LIMIT } from "@/lib/constants";
import { GROUP_KIND_LABELS } from "@/lib/group-kinds";
import { roleLabelKey } from "@/lib/role-labels";
import { cn, formatCompactNumber, QUOTA_NOT_APPLICABLE_LABEL } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Tab definition
// ---------------------------------------------------------------------------

type Tab = "overview" | "members" | "managers" | "quotas";

const TABS: { key: Tab; label: string; icon: React.ElementType }[] = [
  { key: "overview", label: "Overview", icon: FileText },
  { key: "members", label: "Members", icon: Users },
  { key: "managers", label: "Managers", icon: UserCog },
  { key: "quotas", label: "Quotas", icon: Gauge },
];

const MEMBERS_PAGE_SIZE = 25;

export default function GroupDetail() {
  const { t_i18n } = useFormatter();
  const { id = "" } = useParams<{ id: string; tab?: string }>();
  const navigate = useNavigate();
  const [tab] = useTabParam<Tab>("overview");
  const { data: group, isLoading } = useGroup(id);
  const deleteGroup = useDeleteGroup();
  const [ConfirmEl, confirm] = useConfirm();

  if (isLoading) return <DetailPageSkeleton backTo="/users-and-groups/groups" backLabel="Back to Groups" tabs={TABS.length} descriptionLines={1} actions={1} />;
  if (!group) return (
    <NotFoundState
      icon={UsersRound}
      title="Group not found"
      description="This group may have been deleted or doesn't exist."
      actionLabel="View All Groups"
      onAction={() => navigate("/users-and-groups/groups")}
      backTo="/users-and-groups/groups"
      backLabel="Back to Groups"
    />
  );

  const handleDelete = async () => {
    const ok = await confirm({
      title: "Delete Group",
      description: t_i18n('Are you sure you want to delete "{name}"? This will remove all members and clean up shared references.', { name: group.name }),
      confirmLabel: "Delete",
    });
    if (ok) {
      await deleteGroup.mutateAsync(id);
      navigate("/users-and-groups/groups");
    }
  };

  return (
    <div className="space-y-6">
      {ConfirmEl}

      <EntityPageHeader
        backTo="/users-and-groups/groups"
        backLabel="Back to Groups"
        icon={UsersRound}
        title={group.name}
        description={t_i18n("{count, plural, one {# member} other {# members}}", { count: group.member_count })}
        actions={
          <Button variant="ghost" size="sm" className="text-feedback-error-primary hover:text-feedback-error-primary" onClick={handleDelete}>
            <Trash2 className="h-4 w-4 mr-1" /> {t_i18n("Delete")}
          </Button>
        }
      />

      {/* Tabs */}
      <DetailTabs tabs={TABS} active={tab} />

      {/* Tab content */}
      {tab === "overview" && <OverviewTab group={group} groupId={id} />}
      {tab === "members" && <MembersSection groupId={id} />}
      {tab === "managers" && <ManagersSection groupId={id} />}
      {tab === "quotas" && <GroupQuotasTab group={group} groupId={id} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overview Tab
// ---------------------------------------------------------------------------

function OverviewTab({ group, groupId }: { group: NonNullable<ReturnType<typeof useGroup>["data"]>; groupId: string }) {
  return (
    <div className="space-y-6">
      <ProfileCard group={group} groupId={groupId} />
      <GroupAiAccessCard group={group} groupId={groupId} />
      <GroupCtemAccessCard group={group} groupId={groupId} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// CTEM access — the right the group grants on system signals and cases (#1748)
// ---------------------------------------------------------------------------

/** @i18n-keys */
const CTEM_ACCESS_LABELS = {
  none: "No CTEM access",
  view: "View signals and cases",
  decide: "View and decide",
} as const;

const CTEM_ACCESS_CHOICES = ["none", "view", "decide"] as const;

/** @i18n-keys */
const CTEM_ACCESS_HINTS = {
  none: "Members see only the signals they record themselves.",
  view: "Members read every signal and case of the organization, read only.",
  decide: "Members also triage signals, change cases (owner, due date, state, accepted risk) and answer the case decisions. Their agents get the same reach.",
} as const;

function GroupCtemAccessCard({ group, groupId }: { group: NonNullable<ReturnType<typeof useGroup>["data"]>; groupId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateGroup = useUpdateGroup();
  const right: GroupCtemAccess | "none" = group.ctem_access ?? "none";
  const notify = group.ctem_notify !== false;
  const save = (data: { ctem_access?: GroupCtemAccess | "none"; ctem_notify?: boolean }) =>
    updateGroup.mutate(
      { groupId, data },
      {
        onSuccess: () => toast({ title: t_i18n("CTEM access updated"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("CTEM access could not be updated"), description: err.message, variant: "error" }),
      },
    );
  return (
    <Card data-testid="group-ctem-access">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Icon name="shield-check" size={16} aria-hidden />
          {t_i18n("CTEM access")}
        </CardTitle>
        <CardDescription>
          {t_i18n("What the members may do on the signals and cases the platform records. Administrators keep full access.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 space-y-1">
            <span className="content-compact text-default-primary">{t_i18n("Access")}</span>
            <p className="content-compact text-default-secondary" data-testid="group-ctem-access-hint">
              {t_i18n(CTEM_ACCESS_HINTS[right])}
            </p>
          </div>
          <Select
            value={right}
            disabled={updateGroup.isPending}
            onValueChange={(value) => save({ ctem_access: value as GroupCtemAccess | "none" })}
          >
            <SelectTrigger className="w-56 shrink-0" aria-label={t_i18n("CTEM access")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CTEM_ACCESS_CHOICES.map((value) => (
                <SelectItem key={value} value={value}>{t_i18n(CTEM_ACCESS_LABELS[value])}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {right === "decide" && (
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0 space-y-1">
              <span className="content-compact text-default-primary">{t_i18n("Notify the members of case decisions")}</span>
              <p className="content-compact text-default-secondary">
                {t_i18n("Off: the members can still answer from the case page, without a notification.")}
              </p>
            </div>
            <Switch
              aria-label={t_i18n("Notify the members of case decisions")}
              checked={notify}
              disabled={updateGroup.isPending}
              onCheckedChange={(checked) => save({ ctem_notify: checked })}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// AI access — the admin's decision to switch AI off for the group's members
// ---------------------------------------------------------------------------

function GroupAiAccessCard({ group, groupId }: { group: NonNullable<ReturnType<typeof useGroup>["data"]>; groupId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateGroup = useUpdateGroup();
  const aiOff = group.ai_enabled === false;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-4 w-4" />
          {t_i18n("AI access")}
        </CardTitle>
        <CardDescription>
          {t_i18n(
            "Switch AI off for the members of this group. They get no chat, no agents and no automation running a model on their behalf, whatever their other groups say. Everything that does not need a model keeps working.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center justify-between gap-4">
          <span className="content-compact text-default-primary">{t_i18n("AI disabled for this group")}</span>
          <Switch
            aria-label={t_i18n("AI disabled for this group")}
            checked={aiOff}
            disabled={updateGroup.isPending}
            onCheckedChange={(checked) =>
              updateGroup.mutate(
                { groupId, data: { ai_enabled: !checked } },
                {
                  onSuccess: () => toast({ title: t_i18n("AI access updated"), variant: "success" }),
                  onError: (err) =>
                    toast({ title: t_i18n("AI access could not be updated"), description: err.message, variant: "error" }),
                },
              )
            }
          />
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Profile Card — group metadata with inline edit
// ---------------------------------------------------------------------------

function ProfileCard({ group, groupId }: { group: NonNullable<ReturnType<typeof useGroup>["data"]>; groupId: string }) {
  const { t_i18n, intl } = useFormatter();
  const updateGroup = useUpdateGroup();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(group.name);
  const [description, setDescription] = useState(group.description || "");
  const [kind, setKind] = useState<GroupKind>(group.kind ?? "auto");

  const handleSave = async () => {
    await updateGroup.mutateAsync({
      groupId,
      data: { name: name.trim(), description: description.trim() || null, kind },
    });
    setEditing(false);
  };

  const handleCancel = () => {
    setName(group.name);
    setDescription(group.description || "");
    setKind(group.kind ?? "auto");
    setEditing(false);
  };

  return (
    <Card>
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base">{t_i18n("Group Information")}</CardTitle>
        {!editing && (
          <Button variant="secondary" className="gap-1.5 shrink-0" onClick={() => setEditing(true)}>
            <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {editing ? (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="content-base font-medium">{t_i18n("Name")}</label>
              <Input aria-label={t_i18n("Name")}
                value={name}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value)}
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <label className="content-base font-medium">{t_i18n("Description")}</label>
              <textarea
                className="flex min-h-[80px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                value={description}
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setDescription(e.target.value)}
                rows={3}
                placeholder={t_i18n("Optional description...")}
              />
            </div>
            <div className="space-y-2">
              <label id={`group-kind-${groupId}`} className="content-base font-medium">{t_i18n("Kind")}</label>
              <Select value={kind} onValueChange={(value) => setKind(value as GroupKind)}>
                <SelectTrigger aria-labelledby={`group-kind-${groupId}`} className="w-64">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(GROUP_KIND_LABELS) as GroupKind[]).map((value) => (
                    <SelectItem key={value} value={value}>{t_i18n(GROUP_KIND_LABELS[value])}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="content-compact text-default-secondary">
                {t_i18n("Analytics lists org units apart from access and distribution lists. A department is set in Settings > Organization > Departments.")}
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={handleCancel}>{t_i18n("Cancel")}</Button>
              <Button onClick={handleSave} disabled={updateGroup.isPending || !name.trim()}>
                {updateGroup.isPending ? t_i18n("Saving...") : t_i18n("Save changes")}
              </Button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 content-base">
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Name")}</p>
              <p>{group.name}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Description")}</p>
              <p>{group.description || "—"}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Members")}</p>
              <p>{group.member_count}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Created")}</p>
              <p>{group.created_at ? formatTimeAgo(intl, group.created_at) : "—"}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Kind")}</p>
              <p>{t_i18n(GROUP_KIND_LABELS[group.kind ?? "auto"])}</p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// Members Section: the shared DataTable in server mode (#9378 slice 6z, #11940)
// ---------------------------------------------------------------------------

/** The members table's keys in the URL (`gm_q`, `gm_sort`, `gm_page`, `gm_f_role`...). */
const MEMBERS_URL_KEY = "gm_";
const MEMBERS_STORAGE_KEY = "group-members";
const MEMBERS_DEFAULT_SORT = { key: "name", direction: "asc" } as const;

function MembersSection({ groupId }: { groupId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data: allUsers } = useUsers();
  const addMembers = useAddGroupMembers();
  const removeMember = useRemoveGroupMember();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();

  // The table's state lives in the URL (`gm_` keys): the API searches, filters, sorts and
  // pages the members, and its total is the count under the same filters.
  const [searchParams] = useSearchParams();
  const table = readTableState(searchParams, MEMBERS_URL_KEY, {
    sort: MEMBERS_DEFAULT_SORT,
    pageSize: storedPageSize(MEMBERS_STORAGE_KEY, MEMBERS_PAGE_SIZE),
  });
  const sort = table.sort ?? MEMBERS_DEFAULT_SORT;
  const managerPicked = table.filters.manager ?? [];
  const { data, isLoading, isError, refetch } = useGroupMembers(groupId, {
    search: table.search.trim() || undefined,
    role: table.filters.role?.length ? table.filters.role.join(",") : undefined,
    // Both values picked filter nothing, as no value does.
    manager: managerPicked.length === 1 ? managerPicked[0] : undefined,
    sort: sort.key,
    direction: sort.direction,
    limit: table.pageSize,
    offset: table.page * table.pageSize,
  });

  // Fetch all members (not just this page) to filter the add picker; its total is the
  // group's size under no filter, so a filtered total reads "n of N" (UX r1 B1 on #11941).
  const { data: allMembersData } = useGroupMembers(groupId, { limit: BACKEND_FETCH_LIMIT, offset: 0 });
  const allMemberIds = useMemo(() => {
    const ids = new Set<string>();
    if (allMembersData?.items) {
      for (const m of allMembersData.items) ids.add(m.user_id);
    }
    return ids;
  }, [allMembersData]);

  const candidates = useMemo(
    () => (allUsers ?? []).filter((u) => !allMemberIds.has(u.id)).map((u) => userSelectOption(u)),
    [allUsers, allMemberIds],
  );

  // A member uses what is shared with the group, and whatever carries it.
  const handleAdd = async (userId: string) => {
    if (!userId) return;
    if (!(await confirmSharing({ entity_type: "group", entity_id: groupId, change: { add_user_ids: [userId] } }, { confirmLabel: t_i18n("Add") }))) return;
    await addMembers.mutateAsync({ groupId, userIds: [userId] });
  };

  const handleRemove = async (userId: string) => {
    if (!(await confirmSharing({ entity_type: "group", entity_id: groupId, change: { remove_user_ids: [userId] } }, { mode: "unshare", confirmLabel: t_i18n("Remove") }))) return;
    await removeMember.mutateAsync({ groupId, userId });
  };

  const columns: DataTableColumn<GroupMemberItem>[] = [
    {
      key: "name",
      header: "Name",
      cell: (m) => <TruncatedText text={m.full_name || m.email} className="font-medium" />,
      sortValue: (m) => m.full_name.toLowerCase(),
      csv: (m) => m.full_name,
    },
    {
      key: "email",
      header: "Email",
      cell: (m) => <TruncatedText text={m.email} className="text-default-secondary" />,
      sortValue: (m) => m.email.toLowerCase(),
      csv: (m) => m.email,
    },
    {
      key: "role",
      header: "Role",
      width: 224,
      cell: (m) => (
        <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
          <Badge variant={m.role === "admin" ? "cyan" : "outline"} className="whitespace-nowrap">{t_i18n(roleLabelKey(m.role))}</Badge>
          {m.is_manager && (
            <Badge variant="secondary" className="gap-1 whitespace-nowrap">
              <UserCog className="h-3 w-3" aria-hidden />
              {t_i18n("Manager")}
            </Badge>
          )}
        </span>
      ),
      sortValue: (m) => m.role,
      csv: (m) => (m.is_manager ? `${m.role}, manager` : m.role),
    },
    {
      key: "added",
      header: "Added",
      width: 160,
      cell: (m) => <span className="whitespace-nowrap text-default-secondary">{m.added_at ? intl.formatDate(m.added_at, { dateStyle: "medium", timeStyle: "short" }) : ""}</span>,
      sortValue: (m) => m.added_at,
      csv: (m) => m.added_at,
      firstSort: "desc",
      hideBelow: "xl",
      hideable: true,
    },
  ];

  // Add Member above the table, as the section's action: the design-system picker the
  // Managers tab uses, searched by name or email, listing the users not in the group yet.
  // It stays out of the table toolbar, whose right group is total, Columns and page size
  // only, all 32 px (UX r1 B2 on #11941).
  const addMember = (
    <div data-testid="group-detail.add-member" className="flex justify-end">
      <SearchableSelect
        aria-label={t_i18n("Add a member")}
        className="w-72"
        options={candidates}
        value=""
        onValueChange={(userId) => void handleAdd(userId)}
        placeholder={t_i18n("Add a member...")}
        emptyMessage={t_i18n("No users to add")}
        disabled={addMembers.isPending}
      />
    </div>
  );

  return (
    <div className="space-y-4">
      {consentElement}
      {addMember}
      {/* One table (dev-docs/reference/data-tables.md, slice 6z): search, Role and Manager
          pills, sort, Columns, total, page size and one pager are the table's; Add Member
          is the section's action above it and each row keeps its link and its remove action. */}
      <DataTable<GroupMemberItem>
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(m) => m.id}
        rowHref={(m) => `/users-and-groups/users/${m.user_id}`}
        layout="fixed"
        urlKey={MEMBERS_URL_KEY}
        storageKey={MEMBERS_STORAGE_KEY}
        defaultSort={MEMBERS_DEFAULT_SORT}
        defaultPageSize={MEMBERS_PAGE_SIZE}
        server={{ total: data?.total ?? 0 }}
        totalAll={allMembersData?.total}
        searchPlaceholder={t_i18n("Search members...")}
        filters={[
          {
            key: "role",
            label: "Role",
            options: [
              { value: "admin", label: t_i18n(roleLabelKey("admin")) },
              { value: "user", label: t_i18n(roleLabelKey("user")) },
            ],
          },
          {
            key: "manager",
            label: "Manager",
            options: [
              { value: "yes", label: t_i18n("Managers") },
              { value: "no", label: t_i18n("Other members") },
            ],
          },
        ]}
        exportName="group-members"
        rowActions={(m) => (
          <Tip content={t_i18n("Remove from group")}>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t_i18n("Remove from group")}
              className="text-default-secondary hover:text-feedback-error-primary"
              onClick={() => void handleRemove(m.user_id)}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </Tip>
        )}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No members yet. Add users to this group.")}
        noResultsLabel={t_i18n("No members match your filters.")}
        rowTestId="group-members-row"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Managers Section — the people who follow the group's AI usage
// ---------------------------------------------------------------------------

function ManagersSection({ groupId }: { groupId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: managers = [], isLoading } = useGroupManagers(groupId);
  const { data: allUsers = [] } = useUsers();
  const addManagers = useAddGroupManagers();
  const removeManager = useRemoveGroupManager();

  const managerIds = useMemo(() => new Set(managers.map((manager) => manager.user_id)), [managers]);
  const candidates = useMemo(
    () => allUsers.filter((candidate) => !managerIds.has(candidate.id)).map((candidate) => userSelectOption(candidate)),
    [allUsers, managerIds],
  );

  const handleAdd = (userId: string) => {
    if (!userId) return;
    addManagers.mutate(
      { groupId, userIds: [userId] },
      {
        onSuccess: () => toast({ title: t_i18n("Manager added"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("The manager could not be added"), description: err.message, variant: "error" }),
      },
    );
  };

  const handleRemove = async (userId: string, name: string) => {
    const ok = await confirm({
      title: "Remove manager",
      description: t_i18n("{name} will no longer see the AI usage of this group's members.", { name }),
      confirmLabel: "Remove",
    });
    if (!ok) return;
    removeManager.mutate(
      { groupId, userId },
      {
        onError: (err) => toast({ title: t_i18n("The manager could not be removed"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <div className="space-y-4">
      {ConfirmEl}
      <Card>
        <CardContent className="flex flex-wrap items-start gap-4 p-4">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <UserCog className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            <p className="content-base font-medium">{t_i18n("What a manager sees")}</p>
            <p className="content-compact text-default-secondary">
              {t_i18n(
                "A manager follows the AI usage of the group's current members in Analytics, Traces and Sessions, and through an agent with the platform insights tools: the agents they use, the costs, the errors, exported as CSV. A manager does not have to be a member and gets no other right.",
              )}
            </p>
          </div>
          <SearchableSelect
            aria-label={t_i18n("Add a manager")}
            className="w-72"
            options={candidates}
            value=""
            onValueChange={handleAdd}
            placeholder={t_i18n("Add a manager...")}
            emptyMessage={t_i18n("No users to add")}
            disabled={addManagers.isPending}
          />
        </CardContent>
      </Card>

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : managers.length === 0 ? (
        <div className="rounded-sm border border-dashed border-elevation-subtle-soft layer-1 bg-elevation-default p-8 text-center">
          <UserCog className="mx-auto mb-2 h-8 w-8 text-default-disabled" />
          <p className="content-base text-default-secondary">{t_i18n("No manager yet. Add one to let them follow this group's AI usage.")}</p>
        </div>
      ) : (
        <div className="space-y-2">
          {managers.map((manager) => (
            <div
              key={manager.user_id}
              className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-4 py-2.5"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate content-base font-medium">{manager.full_name || manager.email}</p>
                <p className="content-caption text-default-secondary">{manager.email}</p>
              </div>
              <Badge variant={manager.is_member ? "secondary" : "outline"} className="content-caption">
                {manager.is_member ? t_i18n("Member") : t_i18n("Not a member")}
              </Badge>
              {manager.assigned_at && (
                <span className="hidden content-caption text-default-secondary sm:inline">
                  {t_i18n("since {time}", { time: formatTimeAgo(intl, manager.assigned_at) })}
                </span>
              )}
              <Tip content={t_i18n("Remove manager")}>
                <button
                  aria-label={t_i18n("Remove manager")}
                  className="text-default-secondary transition-colors hover:text-feedback-error-primary"
                  onClick={() => handleRemove(manager.user_id, manager.full_name || manager.email)}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </Tip>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// GroupQuotasTab
// ---------------------------------------------------------------------------

const GROUP_PERIOD_LABELS: Record<string, string> = { daily: "/ day", monthly: "/ month", yearly: "/ year" };

function GroupQuotasTab({ group, groupId }: { group: Pick<Group, "quota_config" | "quota_notices">; groupId: string }) {
  const { t_i18n } = useFormatter();
  const updateGroup = useUpdateGroup();
  const { data: models } = useAvailableModels({ scope: "all" });
  const { data: platformQuotas } = usePlatformQuotas();
  const isXtmOne = useIsXtmOne();
  const platformAgenticQuotas = useAgenticQuotas();
  // Community Edition runs no agentic execution: lock the overrides and say
  // why, exactly like the user quota tab.
  const isCeOnly = useIsCommunityEdition();

  const agenticMaxLimits = isXtmOne
    ? { daily: platformAgenticQuotas.daily, monthly: platformAgenticQuotas.monthly, yearly: platformAgenticQuotas.yearly }
    : undefined;

  const platformAgenticCap = platformQuotas?.quota_config?.agentic?.platform_limit ?? null;
  const platformAgenticCapPeriod = platformQuotas?.quota_config?.agentic?.period ?? "monthly";
  const platformLLMProxyCap = platformQuotas?.quota_config?.llm_proxy?.platform_limit ?? null;
  const platformLLMProxyCapPeriod = platformQuotas?.quota_config?.llm_proxy?.period ?? "monthly";

  const [llmProxy, setLLMProxy] = useState<QuotaTypeConfig | null>(
    (group.quota_config?.llm_proxy as QuotaTypeConfig) ?? null,
  );
  const [agentic, setAgentic] = useState<QuotaTypeConfig | null>(
    (group.quota_config?.agentic as QuotaTypeConfig) ?? null,
  );
  const [dirty, setDirty] = useState(false);

  const handleSave = () => {
    // An emptied type inherits and is sent as absent. With both emptied
    // the route needs `{}` to clear: `null` leaves the override as it is.
    const llm = isXtmOne ? null : normalizeQuotaOverride(llmProxy);
    const agenticOverride = normalizeQuotaOverride(agentic);
    const quota_config = (llm || agenticOverride)
      ? { llm_proxy: llm ?? undefined, agentic: agenticOverride ?? undefined }
      : {};
    updateGroup.mutate(
      { groupId, data: { quota_config } },
      { onSuccess: () => setDirty(false) },
    );
  };

  const QUOTA_TYPES: { key: string; label: string; icon: React.ElementType; color: string }[] = [
    { key: "agentic", label: "Agentic Executions", icon: Zap, color: "text-accent" },
    ...(!isXtmOne ? [{ key: "llm_proxy", label: "LLM Proxy Requests", icon: Key, color: "text-feedback-success-primary" }] : []),
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="content-base text-default-secondary">
            {t_i18n("Configure quota overrides for all members of this group. These take priority over platform defaults but can be overridden by user-specific quotas. Platform-wide quotas still apply as a hard cap.")}
          </p>
        </div>
        <Tip content={isCeOnly ? t_i18n("Activate a license or register an EE platform to configure quotas.") : ""} wrap={isCeOnly}>
          <Button
            onClick={handleSave}
            disabled={isCeOnly || !dirty || updateGroup.isPending}
            className="gap-2"
          >
            <Save className="h-4 w-4" />
            {t_i18n("Save")}
          </Button>
        </Tip>
      </div>

      {/* Configured quota summary */}
      <div className="space-y-4">
        {QUOTA_TYPES.map(({ key, label, icon: QIcon, color }) => {
          const cfg = key === "agentic"
            ? (group.quota_config?.agentic as QuotaTypeConfig | undefined)
            : (group.quota_config?.llm_proxy as QuotaTypeConfig | undefined);
          const unlimited = isUnlimitedOverride(cfg);
          const limit = unlimited ? null : (cfg?.default ?? null);
          const period = cfg?.period ?? "monthly";
          // A 0 is a limit - a hard stop, as the backend enforces it -
          // not an absent override.
          const hasLimit = limit != null;
          // Per-model limits alone keep the override: members then have no
          // overall limit, they do not inherit one.
          const hasPerModel = cfg?.per_model != null && Object.keys(cfg.per_model).length > 0;

          return (
            <Card key={key} className="transition-all duration-300 hover:bg-elevation-hover">
              <CardContent className="pt-5 pb-4 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <QIcon className={cn("h-4 w-4", color)} />
                    <span className="content-base font-semibold">{t_i18n(label)}</span>
                  </div>
                  <Badge variant="outline" className="content-caption font-normal">
                    {unlimited ? t_i18n("Unlimited") : hasLimit || hasPerModel ? t_i18n("Configured") : t_i18n("Not set")}
                  </Badge>
                </div>

                {isCeOnly ? (
                  <p className="content-compact text-default-secondary">
                    {QUOTA_NOT_APPLICABLE_LABEL} &mdash; {t_i18n("agentic execution is disabled until a license is activated or an EE platform is registered.")}
                  </p>
                ) : unlimited ? (
                  <p className="content-compact text-default-secondary">{t_i18n("Unlimited: members have no limit, whatever the platform default.")}</p>
                ) : hasLimit ? (
                  <p className="content-compact text-default-secondary">
                    {t_i18n("Each member gets")} <span className={cn("font-semibold", color)}>{formatCompactNumber(limit!)}</span> {t_i18n(label).toLowerCase()} {t_i18n(GROUP_PERIOD_LABELS[period] ?? period)}.
                  </p>
                ) : hasPerModel ? (
                  <p className="content-compact text-default-secondary">{t_i18n("No overall limit")}</p>
                ) : (
                  <p className="content-compact text-default-secondary">{t_i18n("No group override - members inherit the platform default. Turn on Unlimited to lift the limit.")}</p>
                )}

                {key === "llm_proxy" && cfg?.per_model && Object.keys(cfg.per_model).length > 0 && (
                  <div className="space-y-1 pt-1">
                    <p className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Per-model limits")}</p>
                    {Object.entries(cfg.per_model).map(([model, modelLimit]) => (
                      <div key={model} className="flex items-center justify-between">
                        <span className="text-[11px] font-medium truncate max-w-[60%]">{model}</span>
                        <span className="text-[11px] text-default-secondary tabular-nums">{formatCompactNumber(modelLimit)} {t_i18n(GROUP_PERIOD_LABELS[period] ?? period)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      <QuotaEditor
        llmProxy={llmProxy}
        agentic={agentic}
        onChangeLLMProxy={(v) => { setLLMProxy(v); setDirty(true); }}
        onChangeAgentic={(v) => { setAgentic(v); setDirty(true); }}
        availableModels={models}
        hideLLMProxy={isXtmOne}
        agenticMaxLimits={agenticMaxLimits}
        platformAgenticCap={platformAgenticCap}
        platformAgenticCapPeriod={platformAgenticCapPeriod}
        platformLLMProxyCap={platformLLMProxyCap}
        platformLLMProxyCapPeriod={platformLLMProxyCapPeriod}
        llmProxyTitle={t_i18n("LLM Proxy Quota Override")}
        llmProxyDescription={t_i18n("Set the maximum number of LLM proxy requests each member of this group can make. Leave empty to inherit the platform default; turn on Unlimited to lift the limit.")}
        agenticTitle={t_i18n("Agentic Execution Quota Override")}
        agenticDescription={t_i18n("Set the maximum number of agentic executions (chat messages + assignment runs) each member of this group can make. Leave empty to inherit the platform default; turn on Unlimited to lift the limit.")}
        limitLabel={t_i18n("Limit per member")}
        limitHint={t_i18n("Maximum requests per group member per period")}
        clearLabel={t_i18n("Clear (inherit from platform)")}
        disabled={isCeOnly}
        ceOnly={isCeOnly}
        unlimitedLabel={t_i18n("Unlimited")}
        unlimitedHint={t_i18n("Members have no limit, whatever the platform default.")}
        inheritPlaceholder={t_i18n("Inherit")}
        notices={group.quota_notices}
      />
    </div>
  );
}
