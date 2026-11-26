// SPDX-License-Identifier: Apache-2.0

/**
 * The Intelligence policy (#2435): the PIRs of every connected OpenCTI, stored in XTM One by an hourly refresh
 * (no sync to configure), with the organization's policy on each: the question it answers, its owner, its review
 * cadence, the primary assets it serves, its own minimum PIR score and whether it drives signals.
 *
 * The PIR playbook and the PIR filters read the enabled PIRs with their own minimum (one `pir_score` filter per
 * threshold); a PIR disabled here leaves the playbook at the next provisioning pass, its row stays. A PIR OpenCTI
 * no longer lists is shown as gone, never removed from the table. An administrator edits; anyone else reads.
 */

import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsCard, type SettingsCardFold, SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import {
  type IntelligencePolicyPir,
  useIntelligencePolicy,
  useOrganizationContext,
  usePatchIntelligencePolicyPir,
  useRefreshIntelligencePolicy,
} from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { pirDetailProblem, REFERENCE_BOUNDS, REVIEW_CADENCE_LABELS, REVIEW_CADENCES, type ReviewCadence } from "@/lib/organization-reference";

const NO_CADENCE = "__none__";
const FIELD = "org-intelligence-policy";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="list-checks" size={16} aria-hidden />
    </span>
  );
}

/** The draft of the edit dialog: the policy fields of one PIR, the minimum as typed. */
export interface PirPolicyDraft {
  question: string;
  owner: string;
  review_cadence: ReviewCadence;
  primary_assets: string[];
  min_score: string;
  enabled: boolean;
}

export function draftOf(pir: IntelligencePolicyPir): PirPolicyDraft {
  const cadence = (REVIEW_CADENCES as readonly string[]).includes(pir.review_cadence) ? (pir.review_cadence as ReviewCadence) : "";
  return {
    question: pir.question,
    owner: pir.owner,
    review_cadence: cadence,
    primary_assets: [...pir.primary_assets],
    min_score: String(pir.min_score),
    enabled: pir.enabled,
  };
}

/** The minimum as a whole number from 0 to 100, or `null` when what is typed is not one. */
export function parseMinScore(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d{1,3}$/.test(text)) return null;
  const value = Number(text);
  return value <= 100 ? value : null;
}

/** Why the draft cannot be saved (an English message), or `null`. */
export function draftProblem(draft: PirPolicyDraft): string | null {
  if (parseMinScore(draft.min_score) === null) return "Type a whole number from 0 to 100.";
  return pirDetailProblem(draft);
}

/** The PATCH body: only the fields the draft changed, so a field left alone is kept as stored. */
export function draftPatch(pir: IntelligencePolicyPir, draft: PirPolicyDraft): Record<string, unknown> {
  const before = draftOf(pir);
  const patch: Record<string, unknown> = {};
  if (draft.question.trim() !== before.question.trim()) patch.question = draft.question.trim();
  if (draft.owner.trim() !== before.owner.trim()) patch.owner = draft.owner.trim();
  if (draft.review_cadence !== before.review_cadence) patch.review_cadence = draft.review_cadence;
  if (JSON.stringify(draft.primary_assets) !== JSON.stringify(before.primary_assets)) patch.primary_assets = draft.primary_assets;
  const min = parseMinScore(draft.min_score);
  if (min !== null && min !== pir.min_score) patch.min_score = min;
  if (draft.enabled !== before.enabled) patch.enabled = draft.enabled;
  return patch;
}

interface Props {
  /** The landing anchor of a `?card=intelligence_policy` link. */
  id: string;
  fold?: SettingsCardFold;
}

export function OrganizationIntelligencePolicyCard({ id: anchorId, fold }: Props) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const policy = useIntelligencePolicy();
  const patch = usePatchIntelligencePolicyPir();
  const refresh = useRefreshIntelligencePolicy();
  const { data: integrations = [] } = useIntegrations();
  const [openId, setOpenId] = useState<string | null>(null);

  const rows = policy.data?.pirs ?? [];
  const open = rows.find((pir) => pir.id === openId) ?? null;
  const sourceOf = (pir: IntelligencePolicyPir) =>
    pir.integration_id ? (integrations.find((integration) => integration.id === pir.integration_id)?.name ?? t_i18n("Integration not found")) : "";
  const dateText = (iso: string | null) => (iso ? intl.formatDate(iso, { dateStyle: "medium", timeStyle: "short" }) : "");
  const cadenceText = (cadence: string) => {
    const known = (REVIEW_CADENCES as readonly string[]).includes(cadence) ? (cadence as ReviewCadence) : "";
    return known ? t_i18n(REVIEW_CADENCE_LABELS[known]) : "";
  };

  const toggle = (pir: IntelligencePolicyPir, enabled: boolean) =>
    patch.mutate(
      { id: pir.id, patch: { enabled } },
      {
        onError: (err) => toast({ title: t_i18n("The PIR policy was not saved"), description: err.message, variant: "error" }),
      },
    );

  const columns: DataTableColumn<IntelligencePolicyPir>[] = [
    {
      key: "name",
      header: "Name",
      cell: (pir) => (
        <span className="flex min-w-0 items-center gap-2">
          <span className="min-w-0 break-words">{pir.name}</span>
          {pir.gone_at && (
            <Badge variant="warning" className="shrink-0 whitespace-nowrap">
              {t_i18n("No longer in OpenCTI")}
            </Badge>
          )}
        </span>
      ),
      sortValue: (pir) => pir.name.toLowerCase(),
      csv: (pir) => pir.name,
    },
    { key: "source", header: "Source", cell: (pir) => sourceOf(pir), sortValue: (pir) => sourceOf(pir), csv: (pir) => sourceOf(pir), hideBelow: "6xl", hideable: true },
    { key: "owner", header: "Owner", cell: (pir) => pir.owner, sortValue: (pir) => pir.owner.toLowerCase(), csv: (pir) => pir.owner, hideable: true },
    {
      key: "review_cadence",
      header: "Review cadence",
      cell: (pir) => cadenceText(pir.review_cadence),
      csv: (pir) => cadenceText(pir.review_cadence),
      hideBelow: "4xl",
      hideable: true,
    },
    {
      key: "primary_assets",
      header: "Primary assets served",
      cell: (pir) => pir.primary_assets.join(", "),
      csv: (pir) => pir.primary_assets.join(", "),
      hideBelow: "7xl",
      hideable: true,
    },
    {
      key: "min_score",
      header: "Minimum score",
      hint: t_i18n("The PIR score at or above which an entity flagged in this PIR starts a signal, 0 to 100."),
      align: "right",
      cell: (pir) => <span className="font-mono">{pir.min_score}</span>,
      sortValue: (pir) => pir.min_score,
      csv: (pir) => pir.min_score,
    },
    {
      key: "enabled",
      header: "Enabled in signals",
      interactive: isAdmin,
      cell: (pir) =>
        isAdmin ? (
          <Switch
            checked={pir.enabled}
            onCheckedChange={(enabled) => toggle(pir, enabled)}
            disabled={patch.isPending}
            aria-label={t_i18n("Enable {name} in signals", { name: pir.name })}
          />
        ) : pir.enabled ? (
          t_i18n("Yes")
        ) : (
          t_i18n("No")
        ),
      sortValue: (pir) => (pir.enabled ? 1 : 0),
      csv: (pir) => (pir.enabled ? "yes" : "no"),
    },
    {
      key: "last_seen_at",
      header: "Last refreshed",
      cell: (pir) => dateText(pir.last_seen_at),
      sortValue: (pir) => pir.last_seen_at ?? "",
      csv: (pir) => pir.last_seen_at ?? "",
      hideBelow: "8xl",
      hideable: true,
    },
  ];

  const enabledCount = rows.filter((pir) => pir.enabled).length;
  return (
    <SettingsCard
      id={anchorId}
      className={SECTION_BAR_ANCHOR_CLASS}
      data-anchor="card"
      tabIndex={-1}
      {...fold}
      data-testid="settings.organization.intelligence-policy"
      icon={CardIcon}
      title={t_i18n("Intelligence policy")}
      description={t_i18n(
        "The PIRs of every connected OpenCTI, refreshed every hour. Each one has its own minimum score; only the PIRs enabled in signals start fleet runs.",
      )}
      action={
        isAdmin ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={refresh.isPending}
            aria-busy={refresh.isPending || undefined}
            onClick={() =>
              refresh.mutate(undefined, {
                onError: (err) => toast({ title: t_i18n("The PIRs were not refreshed"), description: err.message, variant: "error" }),
              })
            }
            data-testid="settings.organization.intelligence-policy.refresh"
          >
            {refresh.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Refresh now")} /> : <Icon name="refresh-cw" size={16} aria-hidden />}
            {t_i18n("Refresh now")}
          </Button>
        ) : undefined
      }
    >
      {policy.isError ? (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The PIRs could not be read: {reason}", { reason: policy.error.message })}
        </p>
      ) : (
        <div className="space-y-2">
          {rows.length > 0 && (
            <p className="content-compact text-default-secondary" data-testid="settings.organization.intelligence-policy.count">
              {t_i18n("{enabled, plural, one {# PIR enabled in signals} other {# PIRs enabled in signals}} of {total}", { enabled: enabledCount, total: rows.length })}
            </p>
          )}
          <DataTable
            rows={rows}
            columns={columns}
            rowKey={(pir) => pir.id}
            searchFields={(pir) => [pir.name, pir.owner, pir.question, pir.pir_id]}
            filters={[
              {
                key: "enabled",
                label: "Enabled in signals",
                values: (pir) => [pir.enabled ? "yes" : "no"],
                options: [
                  { value: "yes", label: t_i18n("Yes") },
                  { value: "no", label: t_i18n("No") },
                ],
              },
              {
                key: "in_opencti",
                label: "In OpenCTI",
                values: (pir) => [pir.gone_at ? "gone" : "listed"],
                options: [
                  { value: "listed", label: t_i18n("Listed") },
                  { value: "gone", label: t_i18n("No longer in OpenCTI") },
                ],
              },
            ]}
            searchPlaceholder={t_i18n("Search a PIR")}
            exportName="intelligence-policy"
            urlKey="oip_"
            storageKey="settings-organization-intelligence-policy"
            isLoading={policy.isLoading}
            emptyLabel={t_i18n("No PIR yet. The PIRs of every connected OpenCTI appear here after the next refresh.")}
            rowTestId="settings.organization.intelligence-policy.row"
            onRowClick={(pir) => setOpenId(pir.id)}
          />
        </div>
      )}
      <PirPolicyDialog pir={open} canEdit={isAdmin} onOpenChange={(next) => !next && setOpenId(null)} />
    </SettingsCard>
  );
}

function PirPolicyDialog({ pir, canEdit, onOpenChange }: { pir: IntelligencePolicyPir | null; canEdit: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={pir !== null} onOpenChange={onOpenChange}>
      <DialogContent size="md" data-testid="settings.organization.intelligence-policy.dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("PIR policy: {name}", { name: pir?.name ?? "" })}</DialogTitle>
          <DialogDescription>
            {pir?.description || t_i18n("The question this PIR answers, who owns it, its minimum score and whether it drives signals. OpenCTI keeps the PIR itself.")}
          </DialogDescription>
        </DialogHeader>
        {/* Keyed by the PIR, so a second PIR opens on its own stored values, never the previous draft. */}
        {pir && <PirPolicyBody key={pir.id} pir={pir} canEdit={canEdit} onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function PirPolicyBody({ pir, canEdit, onClose }: { pir: IntelligencePolicyPir; canEdit: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data } = useOrganizationContext();
  const save = usePatchIntelligencePolicyPir();
  const [draft, setDraft] = useState<PirPolicyDraft>(() => draftOf(pir));
  const primaryAssets = (data?.profile.primary_assets ?? []).map((pa) => pa.name).filter(Boolean);
  const problem = draftProblem(draft);
  const minInvalid = parseMinScore(draft.min_score) === null;
  const body = draftPatch(pir, draft);
  const dirty = Object.keys(body).length > 0;
  const set = (next: Partial<PirPolicyDraft>) => setDraft((prev) => ({ ...prev, ...next }));
  const cadenceText = (cadence: ReviewCadence) => (cadence ? t_i18n(REVIEW_CADENCE_LABELS[cadence]) : "");

  const onSave = () => {
    if (problem || !dirty) return;
    save.mutate(
      { id: pir.id, patch: body },
      {
        onSuccess: () => {
          toast({ title: t_i18n("PIR policy saved"), variant: "success" });
          onClose();
        },
      },
    );
  };

  if (!canEdit) {
    return (
      <>
        <DialogBody className="space-y-3">
          <SettingsReadGrid data-testid="settings.organization.intelligence-policy.read">
            <SettingsReadField label={t_i18n("Question")} value={pir.question} wide />
            <SettingsReadField label={t_i18n("Owner")} value={pir.owner} />
            <SettingsReadField label={t_i18n("Review cadence")} value={cadenceText(draft.review_cadence)} />
            <SettingsReadField label={t_i18n("Primary assets served")} value={pir.primary_assets.join(", ")} wide />
            <SettingsReadField label={t_i18n("Minimum score")} value={String(pir.min_score)} mono />
            <SettingsReadField label={t_i18n("Enabled in signals")} value={pir.enabled ? t_i18n("Yes") : t_i18n("No")} />
            {pir.criteria_summary && <SettingsReadField label={t_i18n("OpenCTI criteria")} value={pir.criteria_summary} wide />}
          </SettingsReadGrid>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Close")}
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <>
      <DialogBody className="-m-1 space-y-4 p-1">
        <SettingsSwitchField
          id={`${FIELD}-enabled`}
          label={t_i18n("Enabled in signals")}
          helper={t_i18n("The PIR playbook listens to this PIR and its flags start fleet runs.")}
          info={t_i18n("On: an entity flagged in this PIR at or above its minimum score records a signal. Off: the playbook stops listening to it at the next provisioning pass; the PIR stays in this table.")}
          checked={draft.enabled}
          onCheckedChange={(enabled) => set({ enabled })}
        />
        <SettingsField
          id={`${FIELD}-min-score`}
          label={t_i18n("Minimum score")}
          helper={t_i18n("0 to 100. A flag in this PIR at or above this score starts a signal.")}
          info={t_i18n("The live OpenCTI PIR score of a flagged entity, a whole number from 0 to 100. Example: 80: an entity flagged at 85 starts a signal, one at 75 does not. Each PIR has its own minimum.")}
          error={minInvalid ? t_i18n("Type a whole number from 0 to 100.") : undefined}
        >
          <Input
            id={`${FIELD}-min-score`}
            inputMode="numeric"
            aria-labelledby={settingsFieldLabelId(`${FIELD}-min-score`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-min-score`)}
            aria-invalid={minInvalid ? true : undefined}
            className="font-mono"
            value={draft.min_score}
            onChange={(e) => set({ min_score: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-question`}
          label={t_i18n("Question")}
          helper={t_i18n("The intelligence question this PIR answers.")}
          info={t_i18n("Example: Which ransomware groups target European banks this quarter? Agents read it with the PIR in the organization context. Left empty, they read the PIR by its name alone.")}
          error={!minInvalid && problem ? t_i18n(problem) : undefined}
        >
          <Input
            id={`${FIELD}-question`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-question`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-question`)}
            maxLength={REFERENCE_BOUNDS.question}
            value={draft.question}
            onChange={(e) => set({ question: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-owner`}
          label={t_i18n("Owner")}
          helper={t_i18n("The team or person who answers for this PIR.")}
          info={t_i18n("Example: CTI team. Agents read it with the PIR in the organization context. Left empty, the PIR names no owner.")}
        >
          <Input
            id={`${FIELD}-owner`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-owner`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-owner`)}
            maxLength={REFERENCE_BOUNDS.owner}
            value={draft.owner}
            onChange={(e) => set({ owner: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-cadence`}
          label={t_i18n("Review cadence")}
          helper={t_i18n("How often the question is reviewed.")}
          info={t_i18n("Example: Quarterly. Agents read it with the PIR in the organization context. Left empty, the PIR has no review cadence.")}
        >
          <SearchableSelect
            id={`${FIELD}-cadence`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-cadence`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-cadence`)}
            pinned={[{ value: NO_CADENCE, label: t_i18n("Not set") }]}
            options={REVIEW_CADENCES.filter(Boolean).map((cadence) => ({ value: cadence, label: t_i18n(REVIEW_CADENCE_LABELS[cadence]) }))}
            sort={false}
            value={draft.review_cadence || NO_CADENCE}
            onValueChange={(value) => set({ review_cadence: (value === NO_CADENCE ? "" : value) as ReviewCadence })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-primary-assets`}
          label={t_i18n("Primary assets served")}
          helper={t_i18n("The primary assets of the inventory this PIR protects.")}
          info={t_i18n("Example: Payment platform, picked from the primary assets of the inventory. Agents read them with the PIR in the organization context. Left empty, the PIR names no primary asset.")}
        >
          <MultiSelect
            id={`${FIELD}-primary-assets`}
            aria-label={t_i18n("Primary assets served")}
            aria-describedby={settingsFieldHintId(`${FIELD}-primary-assets`)}
            options={[...new Set([...primaryAssets, ...draft.primary_assets])].map((name) => ({ value: name, label: name }))}
            selected={draft.primary_assets}
            onToggle={(name) =>
              set({ primary_assets: draft.primary_assets.includes(name) ? draft.primary_assets.filter((n) => n !== name) : [...draft.primary_assets, name] })
            }
            placeholder={t_i18n("None")}
            searchPlaceholder={t_i18n("Search a primary asset")}
            emptyOptionsLabel={t_i18n("No primary asset declared")}
            noMatchLabel={t_i18n("No match")}
          />
        </SettingsField>
        {save.isError && (
          <p role="alert" className="content-compact text-feedback-error-primary" data-testid="settings.organization.intelligence-policy.error">
            {t_i18n("The PIR policy was not saved: {reason}", { reason: save.error.message })}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="secondary" onClick={onClose}>
          {t_i18n("Cancel")}
        </Button>
        <Button onClick={onSave} disabled={Boolean(problem) || !dirty || save.isPending} aria-busy={save.isPending || undefined} data-testid="settings.organization.intelligence-policy.save">
          {save.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Save")}
        </Button>
      </DialogFooter>
    </>
  );
}
