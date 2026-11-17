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
 * "Draft with AI" on the tracked organizations (#9484 S-c2, dev-docs/ctem-fleets.md
 * section 6.1): a centred dialog for the inputs only. Names one per line, or a
 * supplier or customer file (CSV or XLSX, first column read), the count and the
 * quota line before Draft. One metered call per batch of 10 on the click; the
 * rows open as the review page `/settings/organization/tracked/ai-draft/<id>`,
 * where nothing is created before an explicit accept.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Textarea, Spinner } from "@filigran/design-system";

import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DocumentsPicker } from "@/components/ui/documents-picker";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Tip } from "@/components/ui/tooltip";
import { type OrganizationRelationship, useDraftTrackedOrganizations } from "@/hooks/api/useOrganizationsApi";
import { useFormatter } from "@/i18n";
import { runEstimate, trackedDraftPath, typedNames } from "@/lib/tracked-organizations-ai-draft";

/** The backend reads at most 2 MB of a list file. */
const FILE_MAX_BYTES = 2 * 1024 * 1024;

/** @i18n-keys */
const RELATIONSHIP_LABELS = { provider: "Provider", customer: "Customer", partner: "Partner", subsidiary: "Subsidiary" } as const;

/** One line per relationship, shown under its option. @i18n-keys */
const RELATIONSHIP_MEANINGS = {
  provider: "Sells you a product or a service: a supplier, a SaaS, an MSSP",
  customer: "Buys from you",
  partner: "Works with you without buying or selling: a reseller, an integrator",
  subsidiary: "Owned by your organization or by its group",
} as const;

interface TrackedOrganizationsAiDraftDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Names the field opens with: the names of a failed batch, for Retry on the review page. */
  initialNames?: readonly string[];
}

export function TrackedOrganizationsAiDraftDialog({ open, onOpenChange, initialNames = [] }: TrackedOrganizationsAiDraftDialogProps) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const drafting = useDraftTrackedOrganizations();
  const [names, setNames] = useState(() => initialNames.join("\n"));
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [relationship, setRelationship] = useState<OrganizationRelationship>("provider");
  const [failure, setFailure] = useState<{ quota: boolean; message: string } | null>(null);

  const typed = typedNames(names);
  const file = files[0] ?? null;
  const estimate = runEstimate(typed.length);
  const canDraft = (typed.length > 0 || !!file) && !drafting.isPending && !failure?.quota;

  const close = (next: boolean) => {
    if (!next) {
      setFailure(null);
      drafting.reset();
    }
    onOpenChange(next);
  };

  const runDraft = () => {
    setFailure(null);
    drafting.mutate(
      { names: typed.join("\n"), file, relationship },
      {
        onSuccess: (response) => {
          onOpenChange(false);
          navigate(trackedDraftPath(response.proposal_id));
        },
        onError: (err) => setFailure({ quota: (err as { status?: number }).status === 429, message: err.message }),
      },
    );
  };

  const countLine = file
    ? typed.length > 0
      ? t_i18n("{count} names typed, plus the names of {file}. At most 50 names per run, one agentic request of your quota per 10 names.", {
          count: typed.length,
          file: file.name,
        })
      : t_i18n("The names of {file} are read on Draft. At most 50 names per run, one agentic request of your quota per 10 names.", { file: file.name })
    : typed.length > 0
      ? t_i18n(
          "{count, plural, one {# name} other {# names}}, {requests, plural, one {# agentic request} other {# agentic requests}} of your quota",
          { count: estimate.drafted, requests: estimate.requests },
        )
      : null;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Draft organizations with AI")}</DialogTitle>
          <DialogDescription>{t_i18n("Proposes each organization's details from its name; nothing is created until you accept.")}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">
          <SettingsField
            id="tracked-ai-draft-names"
            label={t_i18n("Organization names")}
            helper={t_i18n("One name per line. Example: Acme Cloud")}
            info={t_i18n("A name already tracked is shown as a conflict and costs no request.")}
          >
            <Textarea
              id="tracked-ai-draft-names"
              aria-labelledby={settingsFieldLabelId("tracked-ai-draft-names")}
              aria-describedby={settingsFieldHintId("tracked-ai-draft-names")}
              rows={6}
              value={names}
              onChange={(e) => setNames(e.target.value)}
            />
          </SettingsField>

          <SettingsField
            id="tracked-ai-draft-file"
            label={t_i18n("Supplier or customer file")}
            helper={fileError ?? t_i18n("CSV or XLSX, 2 MB at most. The first column is read as names; a header row is skipped.")}
            info={t_i18n("One file, 2 MB at most, for example a supplier export. Only the first column is read, one name per row; the other columns are ignored. Empty: only the names typed above are drafted.")}
          >
            <DocumentsPicker
              id="tracked-ai-draft-file"
              files={files}
              onChange={setFiles}
              onError={setFileError}
              maxFiles={1}
              maxBytes={FILE_MAX_BYTES}
              accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              aria-labelledby={settingsFieldLabelId("tracked-ai-draft-file")}
              aria-describedby={settingsFieldHintId("tracked-ai-draft-file")}
            />
          </SettingsField>

          <SettingsField
            id="tracked-ai-draft-relationship"
            label={t_i18n("Relationship when unsure")}
            helper={t_i18n("Used for a name the model cannot place.")}
            info={t_i18n("Sets the relationship of a drafted organization when the model cannot tell it, for example Provider for a supplier list. You can change it on the review page. Provider is used when you do not pick one.")}
          >
            <SearchableSelect
              id="tracked-ai-draft-relationship"
              aria-labelledby={settingsFieldLabelId("tracked-ai-draft-relationship")}
              aria-describedby={settingsFieldHintId("tracked-ai-draft-relationship")}
              searchable="auto"
              sort={false}
              options={(Object.keys(RELATIONSHIP_LABELS) as OrganizationRelationship[]).map((value) => ({
                value,
                label: t_i18n(RELATIONSHIP_LABELS[value]),
                description: t_i18n(RELATIONSHIP_MEANINGS[value]),
              }))}
              value={relationship}
              onValueChange={(value) => setRelationship(value as OrganizationRelationship)}
            />
          </SettingsField>

          {countLine && (
            <p className="content-compact text-default-secondary" data-testid="tracked-ai-draft.estimate">
              {countLine}
            </p>
          )}
          {estimate.overLimit && (
            <p role="status" className="content-compact text-feedback-warning-primary">
              {t_i18n("{drafted} of {count} names drafted; run again for the next {next}.", {
                drafted: estimate.drafted,
                count: typed.length,
                next: Math.min(typed.length - estimate.drafted, 50),
              })}
            </p>
          )}
          {failure && (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {failure.quota ? t_i18n("Your agentic quota is spent.") : t_i18n("The draft did not run.")}{" "}
              {failure.message && (
                <Tip content={<p className="max-w-xs content-compact">{failure.message}</p>}>
                  <BadgeFocusTrigger aria-label={t_i18n("Details")}>
                    <Badge variant="outline" className="whitespace-nowrap">
                      {t_i18n("Details")}
                    </Badge>
                  </BadgeFocusTrigger>
                </Tip>
              )}{" "}
              {failure.quota && (
                <a className="text-accent underline" href="/settings/usage">
                  {t_i18n("Settings > Usage and quotas")}
                </a>
              )}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={runDraft} disabled={!canDraft} aria-busy={drafting.isPending || undefined}>
            {drafting.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Draft (verb)")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
