// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization, tracked organizations (#9157, slice M2): the
 * organizations the platform's own one (the primary, whose profile is the
 * section above) depends on or serves. An administrator adds, edits and
 * archives them here; the first-login setup only sets up the primary
 * (dev-docs/ctem-fleets.md section 9.8). Every tracked organization starts
 * with monitoring off: nothing is provisioned in OpenCTI and no run is
 * metered until a later slice lets an administrator enable it (section 9.7).
 *
 * Add and edit open the centred dialog (#9375 slice 3, the "Forms" standard of
 * dev-docs/reference/frontend-design-system.md): full-size fields with a help
 * line and an info bubble, a one-line meaning per relationship and
 * criticality, and the OpenCTI Organization picked by search, never typed as
 * a raw id. The list is the shared DataTable: search on the name, the table's
 * filter pills on relationship, criticality and OpenCTI link (gate 3 of
 * dev-docs/reference/data-tables.md), every column sorted, a count (UX round 1
 * B4 of #9443).
 */
import { type FormEvent, type HTMLAttributes, useEffect, useId, useState } from "react";
import { Archive, Check, Lock, Network, Pencil, Plus, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { AgenticGateDialogControlled } from "@/components/ui/license-gate-dialog";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { SettingsCard, SettingsField, SettingsFieldGrid, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import {
  type Organization,
  type OrganizationCriticality,
  type OrganizationInput,
  type OrganizationRelationship,
  useArchiveOrganization,
  useCreateOrganization,
  useOpenCtiIdentities,
  useOpenCtiIdentity,
  useOrganizations,
  useUpdateOrganization,
} from "@/hooks/api/useOrganizationsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useDebounce } from "@/hooks/useDebounce";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";

import { TrackedOrganizationsAiDraftDialog } from "./TrackedOrganizationsAiDraftDialog";
import { Spinner } from "@filigran/design-system";

/** @i18n-keys */
const RELATIONSHIP_LABELS = { provider: "Provider", customer: "Customer", partner: "Partner", subsidiary: "Subsidiary" } as const;

/** What each relationship means, one line under its name in the picker. @i18n-keys */
const RELATIONSHIP_MEANINGS = {
  provider: "Supplies a product or service your organization depends on",
  customer: "Buys from your organization or is served by it",
  partner: "Works with your organization under an agreement",
  subsidiary: "Owned or controlled by your organization",
} as const;

/** What each criticality means, one line under its name in the picker. @i18n-keys */
const CRITICALITY_MEANINGS = {
  low: "A breach there barely affects your organization",
  medium: "A breach there slows part of your business",
  high: "A breach there stops one of your business services",
  critical: "A breach there stops your core business",
} as const;

const EMPTY: OrganizationInput = { name: "", relationship: "provider", criticality: "medium", opencti_organization_id: null };

/** The longest name the API accepts; typing stops there (no library counter under the field: B2 of #9443). */
const NAME_MAX = 200;

const CRITICALITY_RANK: Record<OrganizationCriticality, number> = { low: 0, medium: 1, high: 2, critical: 3 };

/** The form state of the organization being added (id "new") or edited (its id), with what it opened with. */
type Editing = { id: string; initial: OrganizationInput; value: OrganizationInput };

function toInput(o: Organization): OrganizationInput {
  return {
    name: o.name,
    relationship: o.relationship ?? "provider",
    criticality: o.criticality,
    opencti_organization_id: o.opencti_organization_id,
  };
}

const sameInput = (a: OrganizationInput, b: OrganizationInput) =>
  a.name === b.name && a.relationship === b.relationship && a.criticality === b.criticality && (a.opencti_organization_id ?? null) === (b.opencti_organization_id ?? null);

/**
 * The card's root attributes a host page sets: a landing anchor (id, scroll margin, `data-anchor`,
 * `tabIndex`) goes on the card itself, never on a wrapper, so its focus ring keeps the card's radius.
 */
type TrackedOrganizationsProps = Omit<HTMLAttributes<HTMLDivElement>, "title" | "children"> & {
  "data-anchor"?: string;
  /** A reader's view on CTEM > Inventory (#1690): the list at rest, no Add, Draft, Edit or Archive (administrators only). */
  readOnly?: boolean;
};

export function TrackedOrganizations({ readOnly = false, ...cardProps }: TrackedOrganizationsProps = {}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data, isLoading } = useOrganizations();
  const create = useCreateOrganization();
  const updateOrg = useUpdateOrganization();
  const archive = useArchiveOrganization();
  const [editing, setEditing] = useState<Editing | null>(null);
  /** Set by a Save refused for an empty name, cleared by the next change of the name. */
  const [nameError, setNameError] = useState(false);
  /** What is typed in the OpenCTI picker; the server searches it once typing pauses. */
  const [openctiQuery, setOpenctiQuery] = useState("");
  /** The name of the OpenCTI Organization picked in this dialog, so the field shows it once the results change. */
  const [pickedIdentity, setPickedIdentity] = useState<SelectOption | null>(null);
  const settledQuery = useDebounce(openctiQuery, 300);
  const identities = useOpenCtiIdentities(settledQuery, !!editing);
  /**
   * Where focus goes once an archived row has left the list (A3 of #9227): the Edit button of the row
   * that took its place (the next one, else the previous one), or "Add an organization" when none is left.
   */
  const [focusAfterArchive, setFocusAfterArchive] = useState<{ archivedId: string; nextId: string | null } | null>(null);
  /** "Draft with AI" (#9484 S-c2): the dialog when AI is licensed and on, else the single CTA system of #9173. */
  const agenticEnabled = useAgenticAIEnabled();
  const aiActionDisabled = useAiActionDisabled();
  const aiAvailable = agenticEnabled && !aiActionDisabled;
  const [aiDraftOpen, setAiDraftOpen] = useState(false);
  const [aiGateOpen, setAiGateOpen] = useState(false);

  const tracked = (data ?? []).filter((o) => o.role === "tracked" && !o.archived_at);
  const pending = create.isPending || updateOrg.isPending;
  const editButtonId = (id: string) => `${fieldId}-edit-${id}`;
  const addButtonId = `${fieldId}-add`;
  const nameId = `${fieldId}-name`;
  const relationshipId = `${fieldId}-relationship`;
  const criticalityId = `${fieldId}-criticality`;
  const openctiId = `${fieldId}-opencti`;

  useEffect(() => {
    if (!focusAfterArchive || archive.isPending) return;
    if (tracked.some((o) => o.id === focusAfterArchive.archivedId)) return;
    const next = focusAfterArchive.nextId ? document.getElementById(editButtonId(focusAfterArchive.nextId)) : null;
    (next ?? document.getElementById(addButtonId))?.focus();
    setFocusAfterArchive(null);
  });
  /** Opens (or closes, with null) the form dialog; a refusal or a search from an earlier one never carries over. */
  const openForm = (next: { id: string; value: OrganizationInput } | null) => {
    setNameError(false);
    setOpenctiQuery("");
    setPickedIdentity(null);
    setEditing(next ? { id: next.id, initial: next.value, value: next.value } : null);
  };
  const set = (patch: Partial<OrganizationInput>) => setEditing((prev) => (prev ? { ...prev, value: { ...prev.value, ...patch } } : prev));

  const submit = () => {
    if (!editing) return;
    const value = { ...editing.value, name: editing.value.name.trim(), opencti_organization_id: editing.value.opencti_organization_id?.trim() || null };
    if (!value.name) {
      setNameError(true);
      document.getElementById(nameId)?.focus();
      return;
    }
    const onError = (err: Error) => toast({ title: t_i18n("The organization was not saved"), description: err.message, variant: "error" });
    const onSuccess = () => {
      setEditing(null);
      toast({ title: t_i18n("Organization saved"), variant: "success" });
    };
    if (editing.id === "new") create.mutate(value, { onSuccess, onError });
    else updateOrg.mutate({ id: editing.id, ...value }, { onSuccess, onError });
  };

  const onArchive = async (o: Organization) => {
    const ok = await confirm({
      title: t_i18n("Archive {name}?", { name: o.name }),
      description: t_i18n("It leaves the list and is no longer read by agents. Its profile versions are kept."),
      confirmLabel: t_i18n("Archive"),
      variant: "warning",
    });
    if (!ok) return;
    const index = tracked.findIndex((t) => t.id === o.id);
    const neighbour = tracked[index + 1] ?? tracked[index - 1] ?? null;
    archive.mutate(o.id, {
      onSuccess: () => {
        setFocusAfterArchive({ archivedId: o.id, nextId: neighbour?.id ?? null });
        toast({ title: t_i18n("Organization archived"), variant: "success" });
      },
      onError: (err) => toast({ title: t_i18n("The organization was not archived"), description: err.message, variant: "error" }),
    });
  };

  const relationshipOptions: SelectOption[] = (Object.keys(RELATIONSHIP_LABELS) as OrganizationRelationship[]).map((value) => ({
    value,
    label: t_i18n(RELATIONSHIP_LABELS[value]),
    description: t_i18n(RELATIONSHIP_MEANINGS[value]),
  }));
  const criticalityOptions: SelectOption[] = (Object.keys(CRITICALITY_LABELS) as OrganizationCriticality[]).map((value) => ({
    value,
    label: t_i18n(CRITICALITY_LABELS[value]),
    description: t_i18n(CRITICALITY_MEANINGS[value]),
  }));

  // The OpenCTI picker's options: the search results, plus the linked one so the field always names it.
  const linkedId = editing?.value.opencti_organization_id ?? null;
  // The organization the dialog opened linked to is read by id, so its name shows before any search (B5 of #9443).
  const openedLinkedId = editing?.initial.opencti_organization_id ?? null;
  const linked = useOpenCtiIdentity(openedLinkedId, !!editing && linkedId === openedLinkedId);
  const identityOptions: SelectOption[] = (identities.data ?? []).map((identity) => ({
    value: identity.id,
    label: identity.name,
    description: [identity.organization_type, ...identity.aliases].filter(Boolean).join(", ") || undefined,
  }));
  if (linkedId && !identityOptions.some((option) => option.value === linkedId)) {
    let linkedOption: SelectOption;
    if (pickedIdentity?.value === linkedId) linkedOption = pickedIdentity;
    else if (linked.data?.id === linkedId) linkedOption = { value: linkedId, label: linked.data.name };
    else if (linked.isError) linkedOption = { value: linkedId, label: t_i18n("Linked OpenCTI organization, name not available") };
    else linkedOption = { value: linkedId, label: t_i18n("Loading...") };
    identityOptions.unshift(linkedOption);
  }
  const filters: DataTableFilter<Organization>[] = [
    {
      key: "relationship",
      label: "Relationship",
      values: (o) => [o.relationship],
      options: relationshipOptions.map(({ value, label }) => ({ value, label })),
    },
    {
      key: "criticality",
      label: "Criticality",
      values: (o) => [o.criticality],
      options: criticalityOptions.map(({ value, label }) => ({ value, label })),
      // Ordinal scale: low to critical, never alphabetical (#12289).
      ordered: true,
    },
    {
      key: "opencti",
      label: "OpenCTI",
      values: (o) => [o.opencti_organization_id ? "linked" : "unlinked"],
      options: [
        { value: "linked", label: t_i18n("Linked to OpenCTI") },
        { value: "unlinked", label: t_i18n("Not linked to OpenCTI") },
      ],
    },
  ];
  const openctiError = identities.isError ? t_i18n("OpenCTI did not answer. Check the OpenCTI integration, then search again.") : undefined;

  const allColumns: DataTableColumn<Organization>[] = [
    {
      key: "name",
      header: "Name",
      sortValue: (o) => o.name.toLowerCase(),
      className: "max-w-[280px]",
      // A long name is cut to its column; the title carries it whole (A1 of #9227).
      cell: (o) => (
        <span title={o.name} className="block truncate font-medium">
          {o.name}
        </span>
      ),
    },
    {
      key: "relationship",
      header: "Relationship",
      hideable: true,
      sortValue: (o) => (o.relationship ? t_i18n(RELATIONSHIP_LABELS[o.relationship]) : ""),
      cell: (o) => (o.relationship ? <Badge variant="secondary">{t_i18n(RELATIONSHIP_LABELS[o.relationship])}</Badge> : null),
    },
    {
      key: "criticality",
      header: "Criticality",
      hideable: true,
      sortValue: (o) => CRITICALITY_RANK[o.criticality],
      firstSort: "desc",
      cell: (o) => <Badge variant="outline">{t_i18n("Criticality: {criticality}", { criticality: t_i18n(CRITICALITY_LABELS[o.criticality]) })}</Badge>,
    },
    {
      key: "opencti",
      header: "OpenCTI",
      hideable: true,
      sortValue: (o) => !!o.opencti_organization_id,
      hideBelow: "3xl",
      cell: (o) => <span className="text-default-secondary">{o.opencti_organization_id ? t_i18n("Linked to OpenCTI") : t_i18n("Not linked to OpenCTI")}</span>,
    },
    {
      key: "monitoring",
      header: "Monitoring",
      hideable: true,
      sortValue: (o) => o.monitoring,
      hideBelow: "4xl",
      cell: (o) => <span className="text-default-secondary">{o.monitoring === "off" ? t_i18n("Monitoring off") : t_i18n("Monitoring on")}</span>,
    },
    {
      key: "actions",
      header: "Actions",
      align: "right",
      className: "whitespace-nowrap",
      cell: (o) => (
        <span className="inline-flex gap-1">
          <Button
            id={editButtonId(o.id)}
            variant="ghost"
            size="icon"
            aria-label={t_i18n("Edit {name}", { name: o.name })}
            disabled={!!editing}
            onClick={() => openForm({ id: o.id, value: toInput(o) })}
          >
            <Pencil className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" aria-label={t_i18n("Archive {name}", { name: o.name })} disabled={!!editing || archive.isPending} onClick={() => onArchive(o)}>
            <Archive className="h-4 w-4" />
          </Button>
        </span>
      ),
    },
  ];
  // A reader has no action on a row: the Actions column is left out rather than drawn empty.
  const columns = readOnly ? allColumns.filter((column) => column.key !== "actions") : allColumns;

  const isNew = editing?.id === "new";
  const dirty = !!editing && !sameInput(editing.initial, editing.value);
  const formId = `${fieldId}-form`;
  const onFormSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!pending) submit();
  };
  // Add and Edit open the centred dialog (Sam, 7 Nov 2026: no drawer in the app);
  // the body is a <form> so Enter submits, the actions sit in the dialog footer.
  const formDialog = (
    <Dialog open={!!editing} onOpenChange={(open) => !open && openForm(null)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{isNew ? t_i18n("Add an organization") : t_i18n("Edit {name}", { name: editing?.initial.name ?? "" })}</DialogTitle>
          <DialogDescription>{t_i18n("A provider, customer, partner or subsidiary your organization depends on or serves.")}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 p-1">
          <form id={formId} data-testid="settings.organizations.form" noValidate onSubmit={onFormSubmit} className="space-y-4">
      {editing && (
        <>
          <SettingsCard title={t_i18n("Identity")}>
            <SettingsFieldGrid>
              <SettingsField
                id={nameId}
                wide
                required
                label={t_i18n("Name")}
                helper={t_i18n("The name agents use for it in briefs and reports.")}
                info={t_i18n("Its legal or usual name, as agents write it. Example: Cloudy SaaS. Required: an organization without a name is not saved.")}
                error={nameError ? t_i18n("A name is required") : undefined}
              >
                <Input
                  id={nameId}
                  aria-labelledby={settingsFieldLabelId(nameId)}
                  value={editing.value.name}
                  aria-required
                  aria-invalid={nameError || undefined}
                  aria-describedby={settingsFieldHintId(nameId)}
                  onChange={(e) => {
                    setNameError(false);
                    set({ name: e.target.value.slice(0, NAME_MAX) });
                  }}
                />
              </SettingsField>
              <SettingsField
                id={relationshipId}
                label={t_i18n("Relationship")}
                helper={t_i18n("How your organization depends on it or serves it.")}
                info={t_i18n("Read by the third-party fleets to word their briefs: a provider's breach can reach you, a customer's is your exposure.")}
              >
                <SearchableSelect
                  id={relationshipId}
                  aria-labelledby={settingsFieldLabelId(relationshipId)}
                  aria-describedby={settingsFieldHintId(relationshipId)}
                  options={relationshipOptions}
                  sort={false}
                  searchable={false}
                  value={editing.value.relationship}
                  onValueChange={(v) => set({ relationship: v as OrganizationRelationship })}
                />
              </SettingsField>
              <SettingsField
                id={criticalityId}
                label={t_i18n("Criticality")}
                helper={t_i18n("How hard a breach at this organization would hit yours.")}
                info={t_i18n("High and critical organizations get a priority intelligence requirement of their own in OpenCTI; low and medium ones share one.")}
              >
                <SearchableSelect
                  id={criticalityId}
                  aria-labelledby={settingsFieldLabelId(criticalityId)}
                  aria-describedby={settingsFieldHintId(criticalityId)}
                  options={criticalityOptions}
                  sort={false}
                  searchable={false}
                  value={editing.value.criticality}
                  onValueChange={(v) => set({ criticality: v as OrganizationCriticality })}
                />
              </SettingsField>
            </SettingsFieldGrid>
          </SettingsCard>

          <SettingsCard title={t_i18n("OpenCTI")}>
            <SettingsField
              id={openctiId}
              label={t_i18n("OpenCTI organization")}
              helper={t_i18n("Search the connected OpenCTI by name or alias. Optional.")}
              error={openctiError}
              info={t_i18n("The OpenCTI Organization it is linked to: threats and reports about it are read from there. Empty: not linked, nothing is read from OpenCTI for it.")}
            >
              <SearchableSelect
                id={openctiId}
                aria-labelledby={settingsFieldLabelId(openctiId)}
                aria-describedby={settingsFieldHintId(openctiId)}
                aria-invalid={identities.isError}
                options={identityOptions}
                sort={false}
                clearable
                onSearchChange={setOpenctiQuery}
                placeholder={t_i18n("Search OpenCTI organizations...")}
                emptyMessage={
                  identities.isError
                    ? t_i18n("OpenCTI did not answer")
                    : settledQuery.trim()
                      ? t_i18n("No OpenCTI organization matches")
                      : t_i18n("Type a name to search OpenCTI")
                }
                value={linkedId}
                onValueChange={(v) => {
                  setPickedIdentity(identityOptions.find((option) => option.value === v) ?? null);
                  set({ opencti_organization_id: v || null });
                }}
              />
            </SettingsField>
          </SettingsCard>

          <SettingsCard title={t_i18n("Monitoring")}>
            <p className="content-compact text-default-secondary">
              {(tracked.find((o) => o.id === editing.id)?.monitoring ?? "off") === "off"
                ? t_i18n("Off. Nothing is watched in OpenCTI and no run is counted until an administrator enables it.")
                : t_i18n("Monitoring on")}
            </p>
          </SettingsCard>
        </>
      )}
          </form>
        </DialogBody>
        <DialogFooter>
          {dirty && <span className="mr-auto content-compact text-default-secondary">{t_i18n("Unsaved changes")}</span>}
          <Button type="button" variant="secondary" onClick={() => openForm(null)} disabled={pending}>
            {t_i18n("Cancel")}
          </Button>
          <Button type="submit" form={formId} className="gap-1.5" disabled={pending} aria-busy={pending || undefined}>
            {pending && <Spinner size="sm" tone="inherit" />}{!pending && <Check className="h-4 w-4" />}
            {isNew ? t_i18n("Create") : t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return (
    <SettingsCard
      {...cardProps}
      data-testid="settings.organizations"
      icon={Network}
      title={t_i18n("Tracked organizations")}
      description={t_i18n(
        "The providers, customers, partners and subsidiaries your organization depends on or serves. Monitoring starts off: nothing is watched in OpenCTI and no run is counted until you enable it.",
      )}
      action={
        readOnly ? undefined : (
        <span className="inline-flex gap-2">
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={isLoading || !!editing}
            onClick={() => (aiAvailable ? setAiDraftOpen(true) : setAiGateOpen(true))}
          >
            {aiAvailable ? <Sparkles className="h-4 w-4" /> : <Lock className="h-4 w-4" />} {t_i18n("Draft with AI")}
          </Button>
          <Button id={addButtonId} className="gap-1.5" disabled={isLoading || !!editing} onClick={() => openForm({ id: "new", value: EMPTY })}>
            <Plus className="h-4 w-4" /> {t_i18n("Add an organization")}
          </Button>
        </span>
        )
      }
    >
      {ConfirmEl}
      {formDialog}
      {/* Both mounted only once opened: the draft dialog navigates (it needs the router), the CTA reads the licence. */}
      {aiDraftOpen && <TrackedOrganizationsAiDraftDialog open={aiDraftOpen} onOpenChange={setAiDraftOpen} />}
      {aiGateOpen && <AgenticGateDialogControlled open={aiGateOpen} onOpenChange={setAiGateOpen} feature={t_i18n("Draft with AI")} />}
      {!isLoading && tracked.length === 0 ? (
        <EmptyState
          variant="inset"
          icon={Network}
          title="No tracked organization yet"
          description="Add a key provider or customer to follow the threats that reach your organization through it."
        />
      ) : (
        <DataTable
          rows={tracked}
          columns={columns}
          rowKey={(o) => o.id}
          storageKey="settings-tracked-organizations"
          rowTestId="settings.organizations.row"
          defaultSort={{ key: "name", direction: "asc" }}
          searchFields={(o) => [o.name]}
          searchPlaceholder={t_i18n("Search organizations...")}
          filters={filters}
          isLoading={isLoading}
          emptyLabel={t_i18n("No results")}
          noResultsLabel={t_i18n("No organization matches these filters")}
          toolbar={
            <span className="content-compact text-default-secondary" data-testid="settings.organizations.count">
              {t_i18n("{count, plural, =0 {No organization} one {# organization} other {# organizations}}", { count: tracked.length })}
            </span>
          }
        />
      )}
    </SettingsCard>
  );
}
