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
 * The About page's prerequisites (#10258 S2): the checklist derived from the
 * fleet's configuration and checked live for the reader (`live_prerequisites`).
 * What needs a look comes first, one line each; the met lines fold into
 * "N met" behind a toggle. Every line wears its integration's logo or its
 * kind's icon in the same slot, names the members that need it, and says why
 * in the reader's language (the backend sends a reason code, never a status).
 * A line not met carries a Fix link to the exact page when the reader may open
 * it, else says only an administrator can fix it. "Fix with AI" opens a new
 * chat whose composer holds the request and the lines the reader can fix, for
 * the person to read and send; without agentic AI it falls back to the first
 * such line's Fix. Reading the page starts no run and calls no model.
 *
 * A documented manual line (what no system can read, #10469) is never a grey
 * line nobody can act on: a fleet manager confirms it (who and when, stored on
 * the fleet, Undo beside it), asks the AI to check and confirm it, and the
 * status band counts it as met once confirmed or says "N to confirm" with the
 * action while it is not.
 */
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRightLeft,
  BookOpen,
  Building2,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  EyeOff,
  Mail,
  Plug,
  Server,
  Sparkles,
  UserCheck,
  XCircle,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { useConfirmFleetPrerequisite, type FleetLivePrerequisite } from "@/hooks/api/useFleetApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useAskAssistant } from "@/lib/ask-assistant";

import {
  failingLinesText,
  prerequisiteGroups,
  prerequisiteLabel,
  prerequisiteReason,
  prerequisiteSummary,
} from "./fleet-prerequisites";

/** The icon a line without a provider logo wears, by its kind: every line has one, in the same slot. */
const KIND_ICONS: Record<string, LucideIcon> = {
  integration: Plug,
  agent_account: Mail,
  handover: ArrowRightLeft,
  knowledge_base: BookOpen,
  mcp_server: Server,
  automations: Zap,
  profile: Building2,
  manual: UserCheck,
};

/** The reader's date format for an ISO date the backend sends (a token expiry). */
function useFormatIsoDate(): (iso: string) => string {
  const { intl } = useFormatter();
  return (iso: string) => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : intl.formatDate(date, { dateStyle: "medium", timeZone: "UTC" });
  };
}

/** What Fix with AI does: open a new chat with the request, or go to the first fixable line's page. */
type FixWithAi = { route: string } | { onClick: () => void };

/** Fix with AI over the lines the reader can fix: the request in a new chat, or the first one's page without agentic AI. */
function useFixWithAi(fleetId: string, fixable: readonly FleetLivePrerequisite[]): FixWithAi | null {
  const { t_i18n } = useFormatter();
  const formatDate = useFormatIsoDate();
  const ask = useAskAssistant();
  const agentic = useAgenticAIEnabled();
  if (!fixable.length) return null;
  if (!agentic) return { route: fixable[0].fix_route };
  return {
    onClick: () =>
      ask(
        t_i18n(
          "Help me fulfil the prerequisites of fleet {fleetId}. Read them with get_fleet_prerequisites, then take the failing lines one by one: say what is missing, do what your tools allow, give me the link for what only I can do, and check again before moving on.",
          { fleetId },
        ) +
          "\n\n" +
          failingLinesText(fixable, t_i18n, formatDate),
      ),
  };
}

function FixWithAiButton({ fleetId, fixable }: { fleetId: string; fixable: readonly FleetLivePrerequisite[] }) {
  const { t_i18n } = useFormatter();
  const fix = useFixWithAi(fleetId, fixable);
  if (!fix) return null;
  const content = (
    <>
      <Sparkles className="h-4 w-4" aria-hidden />
      {t_i18n("Fix with AI")}
    </>
  );
  if ("route" in fix) {
    return (
      <Button asChild size="sm" variant="outline" className="gap-1.5">
        <Link to={fix.route} data-testid="prerequisites-fix-with-ai">
          {content}
        </Link>
      </Button>
    );
  }
  return (
    <Button type="button" size="sm" variant="outline" className="gap-1.5" onClick={fix.onClick} data-testid="prerequisites-fix-with-ai">
      {content}
    </Button>
  );
}

/** Confirm (or Undo) one manual line for a fleet manager: one request, the page redrawn from the answer. */
function useConfirmLine(fleetId: string) {
  const mutation = useConfirmFleetPrerequisite(fleetId);
  return {
    mutation,
    pending: (check: string) => mutation.isPending && mutation.variables?.check === check,
    set: (check: string, confirmed: boolean) => mutation.mutate({ check, confirmed }),
  };
}

/**
 * The status band's prerequisites chip: "Prerequisites 7/9" with Fix with AI
 * while a line the reader can fix fails, "N to confirm" with Confirm (one line)
 * while a manual line waits for a fleet manager, "All prerequisites met" and
 * nothing else once all are (a confirmed manual line counts as met).
 */
export function FleetPrerequisitesChip({
  fleetId,
  lines,
  readOnly = false,
}: {
  fleetId: string;
  lines: readonly FleetLivePrerequisite[];
  /** A fleet nobody may write (Platform Health): the counts alone, no Confirm and no Fix with AI. */
  readOnly?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const confirm = useConfirmLine(fleetId);
  const { met, total, failing, fixable, toConfirm } = prerequisiteSummary(lines);
  if (!total) return null;
  if (met === total) {
    return (
      <Badge variant="success" data-testid="about-prerequisites-chip">
        {t_i18n("All prerequisites met")}
      </Badge>
    );
  }
  const only = !readOnly && toConfirm.length === 1 && toConfirm[0].can_confirm ? toConfirm[0] : null;
  return (
    <span className="flex flex-wrap items-center gap-2">
      <Badge variant={failing.length ? "warning" : "secondary"} data-testid="about-prerequisites-chip">
        {t_i18n("Prerequisites {met}/{total}", { met, total })}
      </Badge>
      {toConfirm.length > 0 && (
        <Badge variant="secondary" data-testid="about-prerequisites-to-confirm">
          {t_i18n("{count, plural, one {# to confirm} other {# to confirm}}", { count: toConfirm.length })}
        </Badge>
      )}
      {only && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="gap-1.5"
          disabled={confirm.pending(only.check)}
          onClick={() => confirm.set(only.check, true)}
          data-testid="prerequisites-chip-confirm"
        >
          <CheckCircle2 className="h-4 w-4" aria-hidden />
          {t_i18n("Confirm")}
        </Button>
      )}
      {!readOnly && <FixWithAiButton fleetId={fleetId} fixable={fixable} />}
    </span>
  );
}

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The request "Fix with AI" sends for one manual line: check it with the tools, confirm it only once it holds. */
function confirmRequest(fleetId: string, line: FleetLivePrerequisite, t: Translate): string {
  return t(
    "Help me confirm the prerequisite \"{label}\" of fleet {fleetId}. Check it with what your tools can read and tell me what you found; confirm it with confirm_fleet_prerequisite (check {check}) only once it holds.",
    { label: line.label, fleetId, check: line.check },
  );
}

/**
 * The actions of a manual line for a fleet manager: Confirm and Fix with AI
 * while it is to confirm, Undo once confirmed. A reader who does not manage the
 * fleet gets the sentence of `manualNote`, never a line with no way out.
 */
function ManualLineActions({
  fleetId,
  line,
  confirm,
}: {
  fleetId: string;
  line: FleetLivePrerequisite;
  confirm: ReturnType<typeof useConfirmLine>;
}) {
  const ask = useAskAssistant();
  const agentic = useAgenticAIEnabled();
  const { t_i18n } = useFormatter();
  if (!line.can_confirm) return null;
  const busy = confirm.pending(line.check);
  if (line.state === "met") {
    return (
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="shrink-0"
        disabled={busy}
        onClick={() => confirm.set(line.check, false)}
        data-testid="about-prerequisite-undo"
      >
        {t_i18n("Undo")}
      </Button>
    );
  }
  return (
    <span className="flex shrink-0 flex-wrap items-center gap-2">
      {agentic && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={() => ask(confirmRequest(fleetId, line, t_i18n))}
          data-testid="about-prerequisite-confirm-with-ai"
        >
          <Sparkles className="h-4 w-4" aria-hidden />
          {t_i18n("Fix with AI")}
        </Button>
      )}
      <Button
        type="button"
        size="sm"
        className="gap-1.5"
        disabled={busy}
        onClick={() => confirm.set(line.check, true)}
        data-testid="about-prerequisite-confirm"
      >
        <CheckCircle2 className="h-4 w-4" aria-hidden />
        {t_i18n("Confirm")}
      </Button>
    </span>
  );
}

/** What a manual line says under its label: who confirmed it and when, an expired confirmation, or why a person confirms it. */
function manualNote(line: FleetLivePrerequisite, t: Translate, formatDate: (iso: string) => string): string {
  if (line.state === "met" && line.confirmed_at) {
    return t("Confirmed by {name} on {date}", { name: line.confirmed_by || "", date: formatDate(line.confirmed_at) });
  }
  if (line.confirmed_at) return t("Confirmed on {date}, over 90 days ago: confirm it again", { date: formatDate(line.confirmed_at) });
  if (!line.can_confirm) return t("A fleet manager confirms this: no system can read it");
  return t("A person confirms this: no system can read it");
}

function StateIcon({ state }: { state: FleetLivePrerequisite["state"] }) {
  if (state === "met") return <CheckCircle2 className="h-5 w-5 shrink-0 text-feedback-success-primary" aria-hidden />;
  if (state === "not_met") return <XCircle className="h-5 w-5 shrink-0 text-feedback-warning-tertiary" aria-hidden />;
  if (state === "unreadable") return <EyeOff className="h-5 w-5 shrink-0 text-default-secondary" aria-hidden />;
  return <CircleHelp className="h-5 w-5 shrink-0 text-default-secondary" aria-hidden />;
}

/** The logo slot: the integration's logo, else the kind's icon, so every label starts in one column. */
function KindMark({ line }: { line: FleetLivePrerequisite }) {
  if (line.provider) return <ProviderLogo provider={line.provider} decorative className="h-5 w-5 shrink-0" />;
  const Icon = KIND_ICONS[line.kind] ?? CircleHelp;
  return <Icon className="h-5 w-5 shrink-0 text-default-secondary" aria-hidden data-testid="about-prerequisite-kind" />;
}

function Line({
  fleetId,
  line,
  memberName,
  confirm,
  readOnly,
}: {
  fleetId: string;
  line: FleetLivePrerequisite;
  memberName: (id: string) => string;
  confirm: ReturnType<typeof useConfirmLine>;
  readOnly: boolean;
}) {
  const { t_i18n } = useFormatter();
  const formatDate = useFormatIsoDate();
  const manual = line.kind === "manual";
  const members = line.member_ids.map(memberName).filter(Boolean);
  const reason = manual
    ? manualNote(line, t_i18n, formatDate)
    : line.state === "met"
      ? ""
      : prerequisiteReason(line, t_i18n, formatDate);
  const stateText = {
    met: t_i18n("Met"),
    not_met: t_i18n("Not met"),
    unreadable: t_i18n("Not shared with you"),
    to_confirm: t_i18n("To confirm"),
  }[line.state];
  // Platform Health off is fixed in Settings even on its read-only fleet (UX r1 B3 on #980): the one line with a way out.
  const settingsFix = line.reason_code === "platform_health_off";
  const fixable = (!readOnly || settingsFix) && line.state === "not_met" && line.can_fix && line.fix_route;
  return (
    <li className="flex items-center gap-3 p-4 content-compact" data-testid="about-prerequisite" data-state={line.state}>
      <StateIcon state={line.state} />
      <KindMark line={line} />
      <span className="min-w-0 flex-1 space-y-0.5">
        <span className="block font-medium">
          {prerequisiteLabel(line, t_i18n)}
          <span className="sr-only"> {stateText}</span>
        </span>
        {members.length > 0 && (
          <span className="block text-default-secondary">{t_i18n("Needed by {members}", { members: members.join(", ") })}</span>
        )}
        {reason && <span className="block text-default-secondary">{reason}</span>}
        {line.state === "not_met" && !fixable && (
          <span
            className="block text-default-secondary"
            data-testid={readOnly ? "about-prerequisite-read-only" : "about-prerequisite-admin-only"}
          >
            {readOnly ? t_i18n("Platform Health is built in and read-only.") : t_i18n("Only an administrator can fix this")}
          </span>
        )}
      </span>
      {fixable && (
        <Link to={line.fix_route} className="shrink-0 text-accent underline-offset-2 hover:underline" data-testid="about-prerequisite-fix">
          {t_i18n("Fix it")}
        </Link>
      )}
      {manual && !readOnly && <ManualLineActions fleetId={fleetId} line={line} confirm={confirm} />}
    </li>
  );
}

/** The Prerequisites section's body: what needs a look (manual lines to confirm included, with their action), the met lines folded. */
export function FleetAboutPrerequisites({
  fleetId,
  lines,
  memberName,
  readOnly = false,
}: {
  fleetId: string;
  lines: readonly FleetLivePrerequisite[];
  /** A member's display name from its agent id (the fleet's health rows). */
  memberName: (agentId: string) => string;
  /** A fleet nobody may write (Platform Health): no Fix with AI, no Fix it, no confirm action, the lock stated (UX r2 B1 on #464). */
  readOnly?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [showMet, setShowMet] = useState(false);
  const metListId = useId();
  const { open, met } = prerequisiteGroups(lines);
  const { fixable } = prerequisiteSummary(lines);
  const confirm = useConfirmLine(fleetId);
  return (
    <div className="space-y-3">
      {confirm.mutation.isError && (
        <RefusalAlert
          message={t_i18n("This prerequisite could not be saved. Try again in a moment.")}
          detail={confirm.mutation.error?.message}
        />
      )}
      {lines.length > 0 && (
        <Card>
          <CardContent className="p-0">
            {!readOnly && fixable.length > 0 && (
              <div className="flex items-center justify-end border-b border-[var(--border-elevation-subtle-soft)] p-3">
                <FixWithAiButton fleetId={fleetId} fixable={fixable} />
              </div>
            )}
            <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]" data-testid="about-checklist">
              {open.map((line) => (
                <Line key={line.check} fleetId={fleetId} line={line} memberName={memberName} confirm={confirm} readOnly={readOnly} />
              ))}
              {met.length > 0 && (
                <li>
                  <button
                    type="button"
                    className="flex min-h-9 w-full items-center gap-3 p-4 text-left content-compact"
                    aria-expanded={showMet}
                    aria-controls={metListId}
                    onClick={() => setShowMet((v) => !v)}
                    data-testid="about-prerequisites-met-toggle"
                  >
                    <CheckCircle2 className="h-5 w-5 shrink-0 text-feedback-success-primary" aria-hidden />
                    <span className="flex-1 font-medium">{t_i18n("{count, plural, one {# met} other {# met}}", { count: met.length })}</span>
                    <ChevronDown className={`h-4 w-4 shrink-0 transition-transform ${showMet ? "rotate-180" : ""}`} aria-hidden />
                  </button>
                  {showMet && (
                    <ul id={metListId} className="divide-y divide-[var(--border-elevation-subtle-soft)] border-t border-[var(--border-elevation-subtle-soft)]">
                      {met.map((line) => (
                        <Line key={line.check} fleetId={fleetId} line={line} memberName={memberName} confirm={confirm} readOnly={readOnly} />
                      ))}
                    </ul>
                  )}
                </li>
              )}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
