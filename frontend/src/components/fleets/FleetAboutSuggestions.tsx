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
 * The About hero's "Suggestions" chip (#10246, #10536): what the AI found wrong
 * with the fleet's configuration while writing its documentation, as actions,
 * never as prose. Shown to the people who manage the fleet only (the API sends
 * no finding to anyone else).
 *
 * - At least one visible finding: one chip counting them, opening a dialog.
 * - None visible but some dismissed: "No suggestions" with "N dismissed" as a
 *   link to the dismissed view, never a stale count.
 * - Neither: nothing.
 *
 * The dialog lists each finding with its member, the suggested fix and three
 * actions (Fix with AI, Open, Dismiss with an optional reason), and in its
 * header Fix all with AI (one chat with every visible finding as one numbered
 * request; agentic AI only) and Dismiss all. "Show dismissed (N)" lists the
 * dismissals that still hold, with who, when and why, and Restore / Restore
 * all. A dismissal holds while the member it names is unchanged, at most 90
 * days (`fleet_doc_findings.holding_dismissals`). Reading starts no run.
 */
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { Check, ExternalLink, EyeOff, Lightbulb, RotateCcw, Sparkles, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  useApplyFleetPracticeSuggestion,
  useDismissFleetDocFindings,
  useRestoreFleetDocFindings,
  type FleetDismissedFinding,
  type FleetDocFinding,
} from "@/hooks/api/useFleetApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useAskAssistant } from "@/lib/ask-assistant";
import { formatCompactNumber } from "@/lib/utils";

import { applyText, DISMISS_REASONS, dismissedText, dismissReasonLabel, findingKindLabel, findingRequest, findingsRequest, findingTexts } from "./fleet-documentation";

type View = "suggestions" | "dismissed";

export function FleetAboutSuggestions({
  fleetId,
  findings,
  dismissed = [],
  agentIdOf,
  onOpenMembers,
}: {
  fleetId: string;
  findings: readonly FleetDocFinding[];
  /** The dismissals that still hold (`documentation.dismissed_findings`). */
  dismissed?: readonly FleetDismissedFinding[];
  /** The live agent id of a member named in a finding, if any: Open goes to its page. */
  agentIdOf: (memberName: string) => string | undefined;
  /** The members view, where Open goes when the finding names no live member. */
  onOpenMembers: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>("suggestions");
  if (!findings.length && !dismissed.length) return null;
  const openOn = (next: View) => {
    setView(next);
    setOpen(true);
  };
  // Nothing left to show in the suggestions view: the dismissed view is the only one.
  const shown: View = findings.length ? view : "dismissed";
  return (
    <>
      {findings.length ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 gap-1.5"
          onClick={() => openOn("suggestions")}
          aria-haspopup="dialog"
          data-testid="about-suggestions-chip"
        >
          <Lightbulb className="h-4 w-4" aria-hidden />
          {t_i18n("{count, plural, one {# suggestion} other {# suggestions}}", { count: findings.length })}
        </Button>
      ) : (
        <span className="flex items-center gap-1.5 content-compact text-default-secondary" data-testid="about-suggestions-none">
          {t_i18n("No suggestions")}
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-8 px-1"
            onClick={() => openOn("dismissed")}
            aria-haspopup="dialog"
            data-testid="about-suggestions-dismissed-link"
          >
            {t_i18n("{count, plural, one {# dismissed} other {# dismissed}}", { count: dismissed.length })}
          </Button>
        </span>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xl" data-testid="about-suggestions-dialog">
          <DialogHeader>
            <DialogTitle>{t_i18n("Suggestions for this fleet")}</DialogTitle>
            <DialogDescription>
              {t_i18n(
                "What the fleet's configuration could do better: good practices checked each time this page opens, and what the AI noted while writing the documentation. Fix one, open what it concerns, or dismiss it: a dismissal holds while the member concerned is unchanged, for 90 days at most.",
              )}
            </DialogDescription>
          </DialogHeader>
          {shown === "suggestions" ? (
            <SuggestionList
              fleetId={fleetId}
              findings={findings}
              dismissedCount={dismissed.length}
              agentIdOf={agentIdOf}
              onShowDismissed={() => setView("dismissed")}
              onOpenMembers={() => {
                setOpen(false);
                onOpenMembers();
              }}
            />
          ) : (
            <DismissedList
              fleetId={fleetId}
              dismissed={dismissed}
              canGoBack={findings.length > 0}
              onBack={() => setView("suggestions")}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Which finding a reason form is open for: one id, or every visible one. */
type DismissTarget = { kind: "one"; id: string } | { kind: "all" };

function SuggestionList({
  fleetId,
  findings,
  dismissedCount,
  agentIdOf,
  onShowDismissed,
  onOpenMembers,
}: {
  fleetId: string;
  findings: readonly FleetDocFinding[];
  dismissedCount: number;
  agentIdOf: (memberName: string) => string | undefined;
  onShowDismissed: () => void;
  onOpenMembers: () => void;
}) {
  const { t_i18n } = useFormatter();
  const dismiss = useDismissFleetDocFindings(fleetId);
  const apply = useApplyFleetPracticeSuggestion(fleetId);
  const ask = useAskAssistant();
  const agentic = useAgenticAIEnabled();
  const [target, setTarget] = useState<DismissTarget | null>(null);
  const [applying, setApplying] = useState<string | null>(null);

  const submit = (reason: string) => {
    if (!target || dismiss.isPending) return;
    const ids = target.kind === "one" ? [target.id] : null;
    dismiss.mutate({ ids, reason }, { onSuccess: () => setTarget(null) });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2" data-testid="about-suggestions-toolbar">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="gap-1.5"
          disabled={dismissedCount === 0}
          onClick={onShowDismissed}
          data-testid="about-suggestions-show-dismissed"
        >
          <EyeOff className="h-4 w-4" aria-hidden />
          {t_i18n("Show dismissed ({count})", { count: formatCompactNumber(dismissedCount) })}
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          {agentic && (
            <Button
              type="button"
              size="sm"
              className="gap-1.5"
              onClick={() => ask(findingsRequest(fleetId, findings, t_i18n))}
              data-testid="about-suggestions-fix-all"
            >
              <Sparkles className="h-4 w-4" aria-hidden />
              {t_i18n("Fix all with AI")}
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="gap-1.5"
            onClick={() => setTarget({ kind: "all" })}
            data-testid="about-suggestions-dismiss-all"
          >
            <X className="h-4 w-4" aria-hidden />
            {t_i18n("Dismiss all")}
          </Button>
        </div>
      </div>
      {target?.kind === "all" && (
        <DismissReasonForm
          confirmLabel={t_i18n("{count, plural, one {Dismiss # suggestion} other {Dismiss # suggestions}}", { count: findings.length })}
          busy={dismiss.isPending}
          onCancel={() => setTarget(null)}
          onConfirm={submit}
        />
      )}
      {dismiss.isError && (
        <RefusalAlert message={t_i18n("This suggestion could not be dismissed. Try again in a moment.")} detail={dismiss.error?.message} />
      )}
      {apply.isError && (
        <RefusalAlert message={t_i18n("This suggestion could not be applied. Try again in a moment.")} detail={apply.error?.message} />
      )}
      <ul className="divide-y divide-[var(--border-elevation-subtle-soft)] rounded-sm border border-elevation-subtle" data-testid="about-suggestions-list">
        {findings.map((f) => {
          const agentId = f.agent_id ?? (f.member ? agentIdOf(f.member) : undefined);
          const words = findingTexts(f, t_i18n);
          const openTarget = agentId ? (
            <Button asChild size="sm" variant="outline" className="gap-1.5">
              <Link to={`/agents/${agentId}`} data-testid="about-suggestion-open">
                <ExternalLink className="h-4 w-4" aria-hidden />
                {t_i18n("Open")}
              </Link>
            </Button>
          ) : (
            <Button type="button" size="sm" variant="outline" className="gap-1.5" onClick={onOpenMembers} data-testid="about-suggestion-open">
              <ExternalLink className="h-4 w-4" aria-hidden />
              {t_i18n("Open")}
            </Button>
          );
          const asking = target?.kind === "one" && target.id === f.id;
          return (
            <li key={f.id} className="space-y-2 p-4 content-compact" data-testid="about-suggestion">
              <p className="text-default-secondary">
                {f.member
                  ? t_i18n("{kind}, {member}", { kind: t_i18n(findingKindLabel(f.kind)), member: f.member })
                  : t_i18n(findingKindLabel(f.kind))}
              </p>
              <p className="font-medium text-default-primary">{words.finding}</p>
              {words.fix && (
                <p className="text-default-secondary">{t_i18n("Suggested fix: {fix}", { fix: words.fix })}</p>
              )}
              {applying === f.id ? (
                <div className="space-y-2 rounded-sm border border-elevation-subtle p-3" data-testid="about-suggestion-apply-confirm">
                  <p className="text-default-primary">
                    {applyText(f, t_i18n)}
                  </p>
                  <InlineConfirmActions
                    confirmLabel={t_i18n("Apply")}
                    busy={apply.isPending}
                    onCancel={() => setApplying(null)}
                    onConfirm={() => apply.mutate({ id: f.id, agentId }, { onSuccess: () => setApplying(null) })}
                    testId="about-suggestion-apply-confirm-button"
                  />
                </div>
              ) : asking ? (
                <DismissReasonForm
                  confirmLabel={t_i18n("Dismiss suggestion")}
                  busy={dismiss.isPending}
                  onCancel={() => setTarget(null)}
                  onConfirm={submit}
                />
              ) : (
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  {f.applicable && (
                    <Button
                      type="button"
                      size="sm"
                      className="gap-1.5"
                      onClick={() => setApplying(f.id)}
                      data-testid="about-suggestion-apply"
                    >
                      <Check className="h-4 w-4" aria-hidden />
                      {t_i18n("Apply")}
                    </Button>
                  )}
                  {agentic && !f.applicable && (
                    <Button
                      type="button"
                      size="sm"
                      className="gap-1.5"
                      onClick={() => ask(findingRequest(fleetId, f, t_i18n))}
                      data-testid="about-suggestion-fix-with-ai"
                    >
                      <Sparkles className="h-4 w-4" aria-hidden />
                      {t_i18n("Fix with AI")}
                    </Button>
                  )}
                  {openTarget}
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="gap-1.5"
                    onClick={() => setTarget({ kind: "one", id: f.id })}
                    data-testid="about-suggestion-dismiss"
                  >
                    <X className="h-4 w-4" aria-hidden />
                    {t_i18n("Dismiss suggestion")}
                  </Button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const NO_REASON = "none";
const OTHER_REASON = "other";

/** The optional reason of a dismissal: none, a standing decision, a false positive or one line of free text. */
function DismissReasonForm({
  confirmLabel,
  busy,
  onCancel,
  onConfirm,
}: {
  confirmLabel: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const [choice, setChoice] = useState<string>(NO_REASON);
  const [text, setText] = useState("");
  const reason =
    choice === OTHER_REASON
      ? text.trim()
      : choice === NO_REASON
        ? ""
        : (DISMISS_REASONS[choice as keyof typeof DISMISS_REASONS] ?? "");
  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle p-3" data-testid="about-suggestion-reason">
      <Label htmlFor={fieldId}>{t_i18n("Reason (optional)")}</Label>
      <Select value={choice} onValueChange={setChoice} disabled={busy}>
        <SelectTrigger id={fieldId} className="w-full" aria-label={t_i18n("Reason (optional)")} data-testid="about-suggestion-reason-select">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_REASON}>{t_i18n("No reason")}</SelectItem>
          {(Object.keys(DISMISS_REASONS) as (keyof typeof DISMISS_REASONS)[]).map((key) => (
            <SelectItem key={key} value={key}>
              {t_i18n(DISMISS_REASONS[key])}
            </SelectItem>
          ))}
          <SelectItem value={OTHER_REASON}>{t_i18n("Other reason")}</SelectItem>
        </SelectContent>
      </Select>
      {choice === OTHER_REASON && (
        <Input
          aria-label={t_i18n("Other reason")}
          value={text}
          maxLength={200}
          disabled={busy}
          placeholder={t_i18n("One line, kept with the dismissal")}
          onChange={(e) => setText(e.target.value)}
          data-testid="about-suggestion-reason-text"
        />
      )}
      <InlineConfirmActions
        confirmLabel={confirmLabel}
        busy={busy}
        onCancel={onCancel}
        onConfirm={() => onConfirm(reason)}
        testId="about-suggestion-reason-confirm"
      />
    </div>
  );
}

/**
 * The Cancel and confirm buttons of an inline confirmation in a suggestion row (Apply, Dismiss): one
 * block, so both read the same, at the default 36 px button height (UX r1 B3 on #11961).
 */
function InlineConfirmActions({
  confirmLabel,
  busy,
  onCancel,
  onConfirm,
  testId,
}: {
  confirmLabel: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap justify-end gap-2 pt-1" data-testid="about-suggestion-inline-confirm">
      <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
        {t_i18n("Cancel")}
      </Button>
      <Button
        type="button"
        className="gap-1.5"
        aria-disabled={busy || undefined}
        aria-busy={busy || undefined}
        onClick={() => {
          if (!busy) onConfirm();
        }}
        data-testid={testId}
      >
        {busy && <Spinner size="sm" tone="inherit" />}
        {confirmLabel}
      </Button>
    </div>
  );
}

function DismissedList({
  fleetId,
  dismissed,
  canGoBack,
  onBack,
}: {
  fleetId: string;
  dismissed: readonly FleetDismissedFinding[];
  canGoBack: boolean;
  onBack: () => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const restore = useRestoreFleetDocFindings(fleetId);
  const restoring = restore.isPending ? restore.variables?.ids : undefined;
  const absolute = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
  const restoreIds = (ids: string[] | null) => {
    if (!restore.isPending) restore.mutate({ ids });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2" data-testid="about-dismissed-toolbar">
        {canGoBack ? (
          <Button type="button" variant="ghost" size="sm" onClick={onBack} data-testid="about-dismissed-back">
            {t_i18n("Back to suggestions")}
          </Button>
        ) : (
          <p className="content-compact text-default-secondary">{t_i18n("No suggestions")}</p>
        )}
        {dismissed.length > 0 && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="gap-1.5"
            aria-disabled={restore.isPending || undefined}
            aria-busy={restoring === null || undefined}
            onClick={() => restoreIds(null)}
            data-testid="about-dismissed-restore-all"
          >
            {restoring === null && <Spinner size="sm" tone="inherit" />}
            {restoring !== null && <RotateCcw className="h-4 w-4" aria-hidden />}
            {t_i18n("Restore all")}
          </Button>
        )}
      </div>
      {restore.isError && (
        <RefusalAlert message={t_i18n("This suggestion could not be restored. Try again in a moment.")} detail={restore.error?.message} />
      )}
      {dismissed.length === 0 ? (
        <p className="content-compact text-default-secondary" data-testid="about-dismissed-empty">
          {t_i18n("No dismissed suggestion.")}
        </p>
      ) : (
        <ul className="divide-y divide-[var(--border-elevation-subtle-soft)] rounded-sm border border-elevation-subtle" data-testid="about-dismissed-list">
          {dismissed.map((d) => (
            <li key={d.id} className="flex items-start justify-between gap-3 p-4 content-compact" data-testid="about-dismissed">
              <div className="min-w-0 space-y-1">
                <p className="text-default-secondary">
                  {d.member
                    ? t_i18n("{kind}, {member}", { kind: t_i18n(findingKindLabel(d.kind)), member: d.member })
                    : t_i18n(findingKindLabel(d.kind))}
                </p>
                <p className="font-medium text-default-primary">{dismissedText(d, t_i18n)}</p>
                <p className="text-default-secondary">
                  {t_i18n("Dismissed by {name} on {date}", {
                    name: d.by_name || t_i18n("an agent"),
                    date: d.at ? absolute.format(new Date(d.at)) : "",
                  })}
                </p>
                {d.reason && <p className="text-default-secondary">{t_i18n("Reason: {reason}", { reason: dismissReasonLabel(d.reason, t_i18n) })}</p>}
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="shrink-0 gap-1.5"
                aria-disabled={restore.isPending || undefined}
                aria-busy={restoring?.[0] === d.id || undefined}
                onClick={() => restoreIds([d.id])}
                data-testid="about-dismissed-restore"
              >
                {restoring?.[0] === d.id && <Spinner size="sm" tone="inherit" />}
                {restoring?.[0] !== d.id && <RotateCcw className="h-4 w-4" aria-hidden />}
                {t_i18n("Restore")}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
