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

import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useApproveByTool } from "@/hooks/api/useAssignmentApi";
import type { PendingByToolResponse } from "@/types";

/**
 * Answer every paused run of the chosen tools, across every page.
 *
 * The one approval path that reaches past the rows on screen, and safe for the
 * same reason "Yes, always" is: the reviewer judges a **tool**, not each call's
 * arguments. It is the weaker form of that decision — it answers what is
 * waiting and grants nothing durable.
 *
 * Selecting several tools is not a convenience. A run cannot be half-answered,
 * so a run calling two of them is covered only once both are ticked; until then
 * it is counted as held back rather than silently missing.
 */
export function ApproveByToolDialog({
  open,
  onOpenChange,
  data,
  isLoading,
  agentId,
  agentName,
  ownOnly,
  approveOnly,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /* Set on an agent's activity list, where the counts and the sweep are scoped
     to that agent. Note "always" is NOT scoped by it and cannot be: a standing
     approval is per user and per tool, so it reaches every run carrying the
     reviewer's identity. The disclosure below already says so. */
  agentId?: string;
  agentName?: string;
  /* Scopes counts and sweep to the caller's own runs, for the offer that
     follows switching allow-all on. */
  ownOnly?: boolean;
  /* Drops reject and "approve always": the caller has just granted the durable
     form wholesale, so offering it again would be a second grant, and rejecting
     is not what somebody who switched every tool on came here to do. */
  approveOnly?: boolean;
  /* Passed in rather than fetched here. Two observers of one query with
     different `enabled` values is an ambiguity about who drives the polling,
     and the page needs the data anyway to decide whether to offer this at
     all. */
  data?: PendingByToolResponse;
  isLoading?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const approveByTool = useApproveByTool();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const tools = data?.tools ?? [];
  const groups = data?.mixed ?? [];
  const chosen = tools.filter((t) => selected.has(t.tool_name));

  /* A mixed run is answered once EVERY tool it calls is ticked — the same rule
     the endpoint applies. Summing `tools` alone therefore disclosed less than
     the sweep performs: with A-only, B-only and A+B paused, ticking both said
     two runs and answered three. */
  const covered = groups.filter((g) => g.tool_names.every((n) => selected.has(n)));
  const runCount =
    chosen.reduce((n, t) => n + t.run_count, 0) +
    covered.reduce((n, g) => n + g.run_count, 0);
  const callCount =
    chosen.reduce((n, t) => n + t.call_count, 0) +
    covered.reduce((n, g) => n + g.call_count, 0);

  /* What the current selection still does not reach. Live, so it is what tells
     the reviewer that ticking one more combination releases the stragglers. */
  const heldBack = groups
    .filter((g) => !covered.includes(g))
    .reduce((n, g) => n + g.run_count, 0);

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (!next.delete(name)) next.add(name);
      return next;
    });

  /* A combination is one decision, not several: its run is answerable only as a
     whole, so ticking it selects every tool it calls.
     Unticking drops its names unless another still-covered combination needs
     them. It does not remember that a name was also ticked on its own row —
     tracking why each name is selected would be state for a corner case, and
     the counts above update live, so re-ticking it is one click. */
  const toggleGroup = (names: string[]) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (!names.every((n) => next.has(n))) {
        for (const n of names) next.add(n);
        return next;
      }
      const needed = new Set(
        groups
          .filter(
            (g) =>
              g.tool_names !== names &&
              g.tool_names.every((n) => next.has(n)),
          )
          .flatMap((g) => g.tool_names),
      );
      for (const n of names) if (!needed.has(n)) next.delete(n);
      return next;
    });

  const submit = (decision: "approve" | "approve_always" | "reject") => {
    /* FR-032b asks that an "always" disclosure enumerate every tool it
       un-gates. Here the reviewer named them, so the list is theirs — but the
       reach is the part they have not stated, and it outlives every run on
       screen. */
    if (decision === "approve_always") {
      const names = [...selected].sort();
      const ok = window.confirm(
        t_i18n(
          "Stop asking for {n, plural, one {this tool} other {these # tools}} on every run carrying your identity, including scheduled ones nobody is watching?",
          { n: names.length },
        ) +
          "\n\n" +
          names.join("\n") +
          "\n\n" +
          /* The grant is global, the sweep beside it is not: the gate has
             already run for another agent's paused runs, so nothing here
             releases them. Say it, or "always" reads as covering them. */
          (agentId
            ? t_i18n(
                "Runs of other agents already waiting on {n, plural, one {it} other {them}} stay paused: answer those from their own pages.",
                { n: names.length },
              ) + "\n\n"
            : "") +
          t_i18n("You can revoke this at any time in your profile."),
      );
      if (!ok) return;
    }
    let reason: string | undefined;
    if (decision === "reject") {
      const entered = window.prompt(
        t_i18n(
          "Why are these being rejected? The agents receive this and will otherwise just ask you what to do instead.",
        ),
        "",
      );
      if (entered === null) return;
      reason = entered.trim() || undefined;
    }
    approveByTool.mutate(
      {
        toolNames: [...selected],
        decision,
        rejectionReason: reason,
        agentId,
        ownOnly,
      },
      {
        onSuccess: (res) => {
          const refused = res.results.filter((r) => r.status === "refused");
          setSelected(new Set());
          onOpenChange(false);
          toast(
            refused.length === 0
              ? {
                  title: t_i18n("Decisions recorded: assignments resuming"),
                  variant: "success",
                }
              : {
                  title: t_i18n("{n} of {total} could not be answered", {
                    n: refused.length,
                    total: res.results.length,
                  }),
                  description: refused[0]?.detail ?? undefined,
                  variant: "info",
                },
          );
        },
        onError: (err) =>
          toast({
            title: t_i18n("Could not submit decisions"),
            description: err.message,
            variant: "error",
          }),
      },
    );
  };

  const busy = approveByTool.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <HitlIcon state="topic" className="size-4" decorative />
            {t_i18n("Approve by tool")}
          </DialogTitle>
          <DialogDescription>
            {approveOnly
              ? t_i18n(
                  "Nothing will pause from now on, but these runs were already waiting when you turned that on. Tick the tools whose runs should resume now.",
                )
              : agentName
              ? t_i18n(
                  "Answers every paused run of {agent} that uses only the tools you pick: on every page of its activity, not just this one.",
                  { agent: agentName },
                )
              : t_i18n(
                  "Answers every paused run that uses only the tools you pick: on every page, not just this one.",
                )}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center gap-2 py-6 content-compact text-default-secondary">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Loading…")}
          </div>
        ) : tools.length === 0 && groups.length === 0 ? (
          <p className="py-6 content-compact text-default-secondary">
            {t_i18n("Nothing is waiting for a decision.")}
          </p>
        ) : (
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {tools.map((tool) => (
              <label
                key={tool.tool_name}
                className="flex cursor-pointer items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 hover:bg-elevation-highlight"
              >
                <Switch
                  checked={selected.has(tool.tool_name)}
                  onCheckedChange={() => toggle(tool.tool_name)}
                />
                <span className="min-w-0 flex-1 truncate font-mono-plex content-compact">
                  {tool.tool_name}
                </span>
                <span className="shrink-0 content-caption tabular-nums text-default-secondary">
                  {t_i18n("{runs, plural, one {# run} other {# runs}} · {calls, plural, one {# call} other {# calls}}", {
                    runs: tool.run_count,
                    calls: tool.call_count,
                  })}
                </span>
              </label>
            ))}

            {/* Its own section, not rows in the list above. A run calling two
                tools is answerable only as a whole, so it is a different unit
                of decision — and listing its tools among the single-tool rows
                would need a "0 runs" count beside a name that does have runs
                waiting, which is one screen contradicting itself (FR-053). */}
            {groups.length > 0 && (
              <>
                <p className="px-1 pt-3 pb-1 content-caption uppercase tracking-wide text-default-secondary">
                  {t_i18n("Runs calling more than one tool")}
                </p>
                {groups.map((group) => {
                  const key = group.tool_names.join("\u0000");
                  const checked = group.tool_names.every((n) => selected.has(n));
                  return (
                    <label
                      key={key}
                      className="flex cursor-pointer items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 hover:bg-elevation-highlight"
                    >
                      <Switch
                        checked={checked}
                        onCheckedChange={() => toggleGroup(group.tool_names)}
                      />
                      <span className="min-w-0 flex-1 truncate font-mono-plex content-compact">
                        {group.tool_names.join(" + ")}
                      </span>
                      <span className="shrink-0 content-caption tabular-nums text-default-secondary">
                        {t_i18n("{runs, plural, one {# run} other {# runs}} · {calls, plural, one {# call} other {# calls}}", {
                          runs: group.run_count,
                          calls: group.call_count,
                        })}
                      </span>
                    </label>
                  );
                })}
              </>
            )}
          </div>
        )}

        {heldBack > 0 && (
          <p className="flex items-start gap-1.5 text-[11px] text-default-secondary">
            <AlertTriangle className="mt-0.5 size-3 shrink-0" />
            {t_i18n(
              "{n, plural, one {# further run stays paused until every tool it calls is ticked.} other {# further runs stay paused until every tool they call is ticked.}}",
              { n: heldBack },
            )}
          </p>
        )}

        {(chosen.length > 0 || covered.length > 0) && (
          <>
            {/* The counterpart to FR-015c's disclosure: this decides a tool,
                not the arguments of each call, so say so before it fires. */}
            <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.05] px-3 py-2 content-caption text-feedback-warning-tertiary">
              {approveOnly
                ? t_i18n(
                    "This answers {calls, plural, one {# call} other {# calls}} across {runs, plural, one {# run} other {# runs}} of your own, which you have not read individually.",
                    { calls: callCount, runs: runCount },
                  )
                : agentName
                ? t_i18n(
                    "This answers {calls, plural, one {# call} other {# calls}} across {runs, plural, one {# run} other {# runs}} of {agent}, on every page of its activity, not only this one, which you have not read individually.",
                    { calls: callCount, runs: runCount, agent: agentName },
                  )
                : t_i18n(
                    "This answers {calls, plural, one {# call} other {# calls}} across {runs, plural, one {# run} other {# runs}}, on every page, not only this one, which you have not read individually.",
                    { calls: callCount, runs: runCount },
                  )}
            </p>
            <div className="flex justify-end gap-2">
              {!approveOnly && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 content-compact"
                  disabled={busy}
                  onClick={() => submit("reject")}
                >
                  {t_i18n("Reject {n}", { n: callCount })}
                </Button>
              )}
              <Button
                size="sm"
                className="h-7 content-compact"
                disabled={busy}
                aria-busy={busy || undefined}
                onClick={() => submit("approve")}
              >
                {busy && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Approve {n}", { n: callCount })}
              </Button>
              {/* Kept distinct from the one-off approve rather than folded into
                  a menu beside it (FR-032b): this one outlives every run on
                  screen. */}
              {!approveOnly && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 border-feedback-alert-primary/40 content-compact text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
                  disabled={busy}
                  onClick={() => submit("approve_always")}
                >
                  {t_i18n("Approve always")}
                </Button>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
