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
 * "Draft" on the organization profile (#9484, #1991, dev-docs/ctem-fleets.md
 * section 6.1): a centred dialog, the inputs first, then the review diff in the
 * same dialog. One pass on the Draft click: the connected platforms (free, always)
 * and, when a model is available, one metered model call on the description, the
 * documents and the website. The platform values come first, the AI completes the
 * fields they left; only the fields of the two Settings tabs are proposed. Nothing
 * is saved before an explicit accept. Opened from read mode, "Save accepted (n)" writes one version
 * with source "ai_draft" and the accepted rows' provenance; opened from the
 * form, "Apply accepted (n)" fills the form and the form's own Save writes it.
 */
import { useState } from "react";
import { Textarea, Spinner } from "@filigran/design-system";

import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { DocumentsPicker } from "@/components/ui/documents-picker";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useAiDraftOrganizationContext,
  useDraftOrganizationContext,
  useSaveOrganizationContext,
  type AiDraftProposal,
  type AiDraftResponse,
  type OrganizationProfile,
} from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  AMOUNT_PROPOSAL_FIELDS,
  acceptAll,
  acceptedSources,
  applyAccepted,
  formatProposalAmount,
  formatProposalValue,
  isScalarProposal,
  isSettingsDraftField,
  mergeDraftProposals,
  parseProposalAmount,
  platformProposals,
  sameValue,
} from "@/lib/organization-ai-draft";
import { CURRENCIES, FIELD_LABELS, PLATFORM_NAMES, SIZE_BANDS, unreadablePlatform } from "@/lib/organization-profile";
import { PROFILE_TAB_LABELS, groupByTab } from "@/lib/organization-profile-tabs";

const DESCRIPTION_MAX = 4000;
const MAX_DOCUMENTS = 5;
const DOCUMENT_MAX_BYTES = 10 * 1024 * 1024;

/** @i18n-keys */
const CONFIDENCE_LABELS = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
} as const satisfies Record<AiDraftProposal["confidence"], string>;

/** @i18n-keys */
const AMOUNT_LABELS = {
  "risk_appetite.annual_loss_threshold": "Annual loss threshold",
  "risk_appetite.approval_threshold": "Approval threshold",
} as const satisfies Record<string, string>;

interface OrganizationAiDraftDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The values the proposals are reviewed against: the saved profile, or the open form's. */
  profile: OrganizationProfile;
  /** The connected platforms the draft reads (opencti, openaev, opencrq). */
  platforms: readonly string[];
  /** A model is available: the description, the documents and the website are read too; else the platforms alone. */
  aiAvailable: boolean;
  /** Set when a form is open: the accepted values fill it and nothing is saved here. */
  onApply?: (profile: OrganizationProfile) => void;
  /** After a save from read mode: the chat onboarding takes over for the fields left (#11224). */
  onSaved?: () => void;
}

type Failure = { kind: "quota"; message: string } | { kind: "model"; message: string };

function sourceLabel(source: string): string {
  if (source.startsWith("document:")) return source.slice("document:".length);
  if (source.startsWith("website:")) return source.slice("website:".length);
  return PLATFORM_NAMES[source] ?? source;
}

/** The id of a review row's editor; its field name is the label pointing at it (UX round 2, B1). */
function editorId(field: string): string {
  return `ai-draft-edit-${field.replace(/\./g, "-")}`;
}

export function OrganizationAiDraftDialog({ open, onOpenChange, profile, platforms, aiAvailable, onApply, onSaved }: OrganizationAiDraftDialogProps) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const aiDrafting = useAiDraftOrganizationContext();
  const platformDrafting = useDraftOrganizationContext();
  const save = useSaveOrganizationContext();
  const pending = aiDrafting.isPending || platformDrafting.isPending;

  const [description, setDescription] = useState("");
  const [documents, setDocuments] = useState<File[]>([]);
  const [website, setWebsite] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [result, setResult] = useState<AiDraftResponse | null>(null);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [rejected, setRejected] = useState<Set<string>>(new Set());
  const [edited, setEdited] = useState<Record<string, unknown>>({});
  const [editing, setEditing] = useState<string | null>(null);
  /** The text typed in an amount editor, kept as typed so a non-number stays visible and marked invalid. */
  const [amountText, setAmountText] = useState<Record<string, string>>({});
  const [skippedNote, setSkippedNote] = useState<string | null>(null);

  const fieldLabel = (path: string) =>
    t_i18n((FIELD_LABELS as Record<string, string>)[path] ?? (AMOUNT_LABELS as Record<string, string>)[path] ?? path);
  const anyPlatform = platforms.length > 0;
  const websiteInvalid = aiAvailable && website.trim() !== "" && !/^https?:\/\/[^\s/]+\.[^\s]+$/i.test(website.trim());
  const hasAiInput = aiAvailable && (description.trim() !== "" || documents.length > 0 || website.trim() !== "");
  const hasInput = hasAiInput || anyPlatform;
  const canDraft = hasInput && !websiteInvalid && !pending && failure?.kind !== "quota";

  const reset = () => {
    setResult(null);
    setAccepted(new Set());
    setRejected(new Set());
    setEdited({});
    setEditing(null);
    setAmountText({});
    setSkippedNote(null);
  };
  const close = (next: boolean) => {
    if (!next) {
      reset();
      setFailure(null);
      aiDrafting.reset();
      platformDrafting.reset();
    }
    onOpenChange(next);
  };

  /** One pass (#1991): the platforms and, with a model, the AI run together; one review of both. */
  const runDraft = async () => {
    setFailure(null);
    reset();
    const [fromPlatforms, fromAi] = await Promise.allSettled([
      anyPlatform ? platformDrafting.mutateAsync() : Promise.resolve(null),
      aiAvailable && hasInput
        ? aiDrafting.mutateAsync({ description: description.trim(), documents, website: website.trim(), usePlatforms: anyPlatform })
        : Promise.resolve(null),
    ]);
    if (fromAi.status === "rejected") {
      const err = fromAi.reason as Error & { status?: number };
      setFailure(err.status === 429 ? { kind: "quota", message: err.message } : { kind: "model", message: err.message });
      return;
    }
    if (fromPlatforms.status === "rejected" && !fromAi.value) {
      setFailure({ kind: "model", message: t_i18n("Try again in a moment.") });
      return;
    }
    const det = fromPlatforms.status === "fulfilled" ? fromPlatforms.value : null;
    const ai = fromAi.value;
    setResult({
      based_on_version: ai?.based_on_version ?? det?.based_on_version ?? null,
      proposals: mergeDraftProposals(det ? platformProposals(det, profile) : [], ai?.proposals ?? []),
      not_supported: (ai?.not_supported ?? []).filter(isSettingsDraftField),
      suggested_pirs: [],
      unavailable: [...new Set([...(det?.unavailable ?? []), ...(ai?.unavailable ?? [])])],
      notes: ai?.notes ?? [],
      usage: ai?.usage ?? {},
    });
  };

  const rows = (result?.proposals ?? []).filter((proposal) => !sameValue(proposal.value, proposal.current));
  const unchanged = (result?.proposals ?? []).length - rows.length;
  const acceptedCount = rows.filter((row) => accepted.has(row.field)).length;

  const decide = (field: string, accept: boolean) => {
    setAccepted((prev) => {
      const next = new Set(prev);
      if (accept) next.add(field);
      else next.delete(field);
      return next;
    });
    setRejected((prev) => {
      const next = new Set(prev);
      if (accept) next.delete(field);
      else next.add(field);
      return next;
    });
  };
  const onAcceptAll = () => {
    const { accepted: next, skipped } = acceptAll(rows.filter((row) => !rejected.has(row.field)), accepted);
    setAccepted(next);
    setSkippedNote(t_i18n("Accepted {accepted}, skipped {skipped} to review", { accepted: next.size, skipped }));
  };

  const finish = () => {
    const merged = applyAccepted(profile, rows, accepted, edited);
    if (onApply) {
      onApply(merged);
      close(false);
      return;
    }
    save.mutate(
      { profile: merged, source: "ai_draft", sources: acceptedSources(rows, accepted, edited, user?.email ?? "an administrator") },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Organization profile saved"), variant: "success" });
          close(false);
          onSaved?.();
        },
      },
    );
  };

  const sources = [
    ...platforms.map((platform) => PLATFORM_NAMES[platform] ?? platform),
    ...(aiAvailable && description.trim() ? [t_i18n("Description")] : []),
    ...(aiAvailable ? documents.map((file) => file.name) : []),
    ...(aiAvailable && website.trim() ? [website.trim()] : []),
  ];

  const platformsField = (
    <SettingsField
      id="ai-draft-platforms"
      label={t_i18n("Platform facts")}
      helper={t_i18n("What the connected platforms already know, read on every draft.")}
      info={t_i18n("None connected: connect OpenCTI, OpenAEV or OpenCRQ in Settings > Integrations.")}
    >
      {platforms.length === 0 ? (
        <p id="ai-draft-platforms" className="content-compact text-default-secondary">
          {t_i18n("No platform is connected.")}{" "}
          <a className="text-accent underline" href="/settings/integrations">
            {t_i18n("Settings > Integrations")}
          </a>
        </p>
      ) : (
        <p id="ai-draft-platforms" className="content-compact">
          {platforms.map((platform) => PLATFORM_NAMES[platform] ?? platform).join(", ")}
        </p>
      )}
    </SettingsField>
  );

  const inputs = (
    <div className="space-y-4">
      {platformsField}
      {!aiAvailable && (
        <p data-testid="ai-draft.no-model" className="content-compact text-default-secondary">
          {t_i18n("Documents and AI need a model: this draft reads the connected platforms only.")}
        </p>
      )}
      {aiAvailable && (
      <>
      <SettingsField
        id="ai-draft-description"
        label={t_i18n("Description")}
        helper={t_i18n("What the company does, where, its main systems. Example: insurer, 2,000 staff, France and Spain, Guidewire.")}
        info={t_i18n("Read as typed, up to 4,000 characters. Empty: not used.")}
      >
        <Textarea
          id="ai-draft-description"
          aria-labelledby={settingsFieldLabelId("ai-draft-description")}
          aria-describedby={settingsFieldHintId("ai-draft-description")}
          rows={4}
          maxLength={DESCRIPTION_MAX}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </SettingsField>
      <p className="-mt-3 text-right content-caption text-default-secondary">
        {t_i18n("{count} of {max} characters", { count: description.length, max: DESCRIPTION_MAX })}
      </p>

      <SettingsField
        id="ai-draft-documents"
        label={t_i18n("Documents")}
        helper={fileError ?? t_i18n("Used for this draft only, not stored.")}
        info={t_i18n("5 files, 10 MB each, 60,000 characters in total. A file that cannot be read is skipped with its reason. Empty: not used.")}
      >
        <DocumentsPicker
          id="ai-draft-documents"
          files={documents}
          onChange={setDocuments}
          onError={setFileError}
          maxFiles={MAX_DOCUMENTS}
          maxBytes={DOCUMENT_MAX_BYTES}
          aria-labelledby={settingsFieldLabelId("ai-draft-documents")}
          aria-describedby={settingsFieldHintId("ai-draft-documents")}
        />
      </SettingsField>

      <SettingsField
        id="ai-draft-website"
        label={t_i18n("Website")}
        helper={t_i18n("Example: https://www.example.com")}
        info={t_i18n("One page and at most 4 links of the same site. Private hosts are refused. Empty: nothing is fetched.")}
        error={websiteInvalid ? t_i18n("Enter a public https URL") : undefined}
      >
        <Input
          id="ai-draft-website"
          type="url"
          inputMode="url"
          aria-labelledby={settingsFieldLabelId("ai-draft-website")}
          aria-describedby={settingsFieldHintId("ai-draft-website")}
          placeholder="https://www.example.com"
          value={website}
          onChange={(e) => setWebsite(e.target.value)}
        />
      </SettingsField>

      <p className="content-compact text-default-secondary">{t_i18n("About one chat turn of your quota")}</p>
      </>
      )}

      {pending && (
        <ul aria-live="polite" data-testid="ai-draft.progress" className="space-y-1 content-compact">
          {sources.map((name) => (
            <li key={name} className="flex items-center gap-2">
              <Spinner size="sm" tone="inherit" />
              {t_i18n("Reading {source}", { source: name })}
            </li>
          ))}
        </ul>
      )}

      {failure?.kind === "quota" && (
        <div role="alert" className="space-y-1 rounded-sm border border-alert-warning bg-alert-warning p-3 content-compact">
          <p>{failure.message}</p>
          <a className="text-accent underline" href="/settings/usage">
            {t_i18n("Settings > Usage and quotas")}
          </a>
        </div>
      )}
      {failure?.kind === "model" && (
        <div role="alert" className="flex items-start justify-between gap-3 rounded-sm border border-alert-error bg-alert-error p-3 content-compact">
          <p>{t_i18n("The draft could not be made: {reason}", { reason: failure.message })}</p>
          <Button variant="secondary" size="sm" onClick={() => void runDraft()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      )}
    </div>
  );

  /** The currency the amounts are read in: a reviewed currency row (edited, else proposed), else the profile's. */
  const currencyRow = rows.find((row) => row.field === "currency");
  const currency = String((currencyRow && ("currency" in edited ? edited.currency : currencyRow.value)) ?? profile.currency ?? "");
  const amountInvalid = (field: string) => AMOUNT_PROPOSAL_FIELDS.has(field) && field in amountText && parseProposalAmount(amountText[field]) === undefined;
  const anyAcceptedInvalid = rows.some((row) => accepted.has(row.field) && amountInvalid(row.field));
  /** An amount always carries its unit, and says when no currency is set (UX round 2, B2). */
  const displayAmount = (field: string, value: unknown) => {
    const amount = formatProposalAmount(value, currency, locale);
    if (!amount) return amount;
    const hourly = field === "loaded_hourly_rate";
    if (currency) return hourly ? t_i18n("{amount} per hour", { amount }) : amount;
    return hourly ? t_i18n("{amount} per hour, currency not set", { amount }) : t_i18n("{amount}, currency not set", { amount });
  };
  const display = (field: string, value: unknown) =>
    AMOUNT_PROPOSAL_FIELDS.has(field)
      ? displayAmount(field, value)
      : field === "size" && typeof value === "string" && value
        ? t_i18n("{band} employees", { band: value })
        : formatProposalValue(value);
  const setValue = (field: string, value: unknown) => setEdited((prev) => ({ ...prev, [field]: value }));

  /** The control the profile form uses for that field (UX round 1, B2): an amount with its currency, the size bands, the currency picker, else text. */
  const editor = (proposal: AiDraftProposal, value: unknown) => {
    const id = editorId(proposal.field);
    const labelledBy = `${id}-label`;
    if (AMOUNT_PROPOSAL_FIELDS.has(proposal.field)) {
      const text = amountText[proposal.field] ?? (typeof value === "number" ? String(value) : "");
      const invalid = amountInvalid(proposal.field);
      const errorId = `${id}-error`;
      return (
        <div className="space-y-1">
          <div className="relative">
            <Input
              id={id}
              inputMode="decimal"
              aria-labelledby={labelledBy}
              aria-invalid={invalid || undefined}
              aria-describedby={invalid ? errorId : undefined}
              className="[&_input]:pr-16"
              value={text}
              onChange={(e) => {
                const typed = e.target.value;
                setAmountText((prev) => ({ ...prev, [proposal.field]: typed }));
                const parsed = parseProposalAmount(typed);
                if (parsed !== undefined) setValue(proposal.field, parsed);
              }}
            />
            {currency && (
              <span aria-hidden className="pointer-events-none absolute inset-y-0 right-3 flex items-center content-compact text-default-secondary">
                {currency}
              </span>
            )}
          </div>
          {invalid && (
            <p id={errorId} className="content-compact text-feedback-error-primary">
              {t_i18n("Not a valid amount: type a number, without letters, 0 or more.")}
            </p>
          )}
        </div>
      );
    }
    if (proposal.field === "size") {
      return (
        <Select value={typeof value === "string" ? value : ""} onValueChange={(next) => setValue("size", next)}>
          <SelectTrigger id={id} aria-labelledby={labelledBy}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SIZE_BANDS.map((band) => (
              <SelectItem key={band} value={band}>
                {t_i18n("{band} employees", { band })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    if (proposal.field === "currency") {
      const code = typeof value === "string" ? value : "";
      const options = code && !(CURRENCIES as readonly string[]).includes(code) ? [code, ...CURRENCIES] : [...CURRENCIES];
      return (
        <SearchableSelect
          id={id}
          aria-labelledby={labelledBy}
          options={options.map((option) => ({ value: option, label: option }))}
          sort={false}
          value={code}
          onValueChange={(next) => setValue("currency", next)}
        />
      );
    }
    return <Input id={id} aria-labelledby={labelledBy} value={String(value ?? "")} onChange={(e) => setValue(proposal.field, e.target.value)} />;
  };

  const tag = (proposal: AiDraftProposal) => (
    <Tip
      content={
        <div className="max-w-xs space-y-1 content-compact">
          <p>{t_i18n("Source: {source}", { source: sourceLabel(proposal.source) })}</p>
          {proposal.basis && <p>{proposal.basis}</p>}
        </div>
      }
    >
      <BadgeFocusTrigger aria-label={t_i18n("Source of {field}", { field: fieldLabel(proposal.field) })}>
        <Badge variant={proposal.confidence === "low" ? "warning" : "outline"}>{t_i18n(CONFIDENCE_LABELS[proposal.confidence])}</Badge>
      </BadgeFocusTrigger>
    </Tip>
  );

  const review = result && (
    <div className="space-y-3" data-testid="ai-draft.review">
      {rows.length === 0 ? (
        <div className="space-y-2 content-base">
          <p className="font-medium">{t_i18n("Nothing in what you gave supports a field")}</p>
          <p className="text-default-secondary">{t_i18n("Add a document or a description, then draft again.")}</p>
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between gap-3">
            <p className="font-medium content-base">{t_i18n("Review: {count} proposals", { count: rows.length })}</p>
            <Button variant="secondary" size="sm" onClick={onAcceptAll}>
              {t_i18n("Accept all")}
            </Button>
          </div>
          {skippedNote && (
            <p role="status" className="content-compact text-default-secondary">
              {skippedNote}
            </p>
          )}
          <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]">
            {/* Grouped by the profile tab that holds each field (#12329), with the count per tab. */}
            {groupByTab(rows, (row) => row.field).map((group) => (
              <li key={group.tab ?? "other"} data-testid={`ai-draft.group.${group.tab ?? "other"}`} className="py-2">
                <p className="font-medium content-base">
                  {group.tab ? t_i18n(PROFILE_TAB_LABELS[group.tab]) : t_i18n("Other")}{" "}
                  <span className="font-normal content-compact text-default-secondary">
                    {t_i18n("{count, plural, one {# proposal} other {# proposals}}", { count: group.items.length })}
                  </span>
                </p>
                <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]">
            {group.items.map((proposal) => {
              const isAccepted = accepted.has(proposal.field);
              const isRejected = rejected.has(proposal.field);
              const value = proposal.field in edited ? edited[proposal.field] : proposal.value;
              return (
                <li key={proposal.field} data-testid={`ai-draft.row.${proposal.field}`} className="space-y-1.5 py-2 content-compact">
                  <div className="flex flex-wrap items-center gap-2">
                    {editing === proposal.field ? (
                      <label id={`${editorId(proposal.field)}-label`} htmlFor={editorId(proposal.field)} className="font-medium content-base">
                        {fieldLabel(proposal.field)}
                      </label>
                    ) : (
                      <span className="font-medium content-base">{fieldLabel(proposal.field)}</span>
                    )}
                    {tag(proposal)}
                    {proposal.conflict && <Badge variant="warning">{t_i18n("Replaces your value")}</Badge>}
                    {proposal.check && <Badge variant="warning">{t_i18n("Check this")}</Badge>}
                    {isAccepted && <Badge variant="success">{t_i18n("Accepted")}</Badge>}
                    {isRejected && <Badge variant="secondary">{t_i18n("Rejected")}</Badge>}
                  </div>
                  {proposal.conflict && (
                    <p className="text-default-secondary">{t_i18n("Current: {value}", { value: display(proposal.field, proposal.current) })}</p>
                  )}
                  {editing === proposal.field ? (
                    editor(proposal, value)
                  ) : (
                    <p>{t_i18n("Proposed: {value}", { value: display(proposal.field, value) })}</p>
                  )}
                  <div className="flex justify-end gap-2">
                    {isScalarProposal(proposal) && (
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={editing === proposal.field && amountInvalid(proposal.field)}
                        onClick={() => setEditing(editing === proposal.field ? null : proposal.field)}
                      >
                        {editing === proposal.field ? t_i18n("Done") : t_i18n("Edit")}
                      </Button>
                    )}
                    <Button
                      variant="secondary"
                      size="sm"
                      aria-label={t_i18n("Accept {field}", { field: fieldLabel(proposal.field) })}
                      aria-pressed={isAccepted}
                      disabled={amountInvalid(proposal.field)}
                      onClick={() => decide(proposal.field, true)}
                    >
                      {t_i18n("Accept")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={t_i18n("Reject {field}", { field: fieldLabel(proposal.field) })}
                      aria-pressed={isRejected}
                      onClick={() => decide(proposal.field, false)}
                    >
                      {t_i18n("Reject")}
                    </Button>
                  </div>
                </li>
              );
            })}
                </ul>
              </li>
            ))}
          </ul>
        </>
      )}
      {unchanged > 0 && (
        <details className="content-compact">
          <DisclosureSummary>{t_i18n("Unchanged ({count})", { count: unchanged })}</DisclosureSummary>
          <ul className="list-disc pl-5 text-default-secondary">
            {(result.proposals ?? [])
              .filter((proposal) => sameValue(proposal.value, proposal.current))
              .map((proposal) => (
                <li key={proposal.field}>{fieldLabel(proposal.field)}</li>
              ))}
          </ul>
        </details>
      )}
      {result.not_supported.length > 0 && (
        <p className="content-compact text-default-secondary">
          {t_i18n("Not supported: {fields}", { fields: result.not_supported.map(fieldLabel).join(", ") })}
        </p>
      )}
      {result.unavailable.map((line) => (
        <p key={line} className="content-compact text-default-secondary">
          {unreadablePlatform(line) ? t_i18n("{platform} could not be read", { platform: unreadablePlatform(line) }) : t_i18n("A platform could not be read")}{" "}
          <a className="text-accent underline" href="/settings/integrations">
            {t_i18n("Settings > Integrations")}
          </a>
        </p>
      ))}
      {save.isError && (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The organization profile was not saved: {reason}", { reason: save.error.message })}
        </p>
      )}
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Draft")}</DialogTitle>
          <DialogDescription>{t_i18n("Draft reads your platforms and documents and proposes values to review. Nothing is saved until you click Save.")}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">{result ? review : inputs}</DialogBody>
        <DialogFooter>
          {!result && !hasInput && (
            <p className="mr-auto content-compact text-default-secondary">
              {aiAvailable ? t_i18n("Add a description, a document or a website, or connect a platform") : t_i18n("Connect a platform to draft without a model")}
            </p>
          )}
          <Button variant="secondary" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          {result ? (
            rows.length === 0 ? (
              <Button onClick={reset}>{t_i18n("Back to inputs")}</Button>
            ) : (
              <Button onClick={finish} disabled={acceptedCount === 0 || anyAcceptedInvalid || save.isPending} aria-busy={save.isPending || undefined}>
                {onApply ? t_i18n("Apply accepted ({count})", { count: acceptedCount }) : t_i18n("Save accepted ({count})", { count: acceptedCount })}
              </Button>
            )
          ) : (
            <Button onClick={() => void runDraft()} disabled={!canDraft} aria-busy={pending || undefined}>
              {pending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Draft")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
