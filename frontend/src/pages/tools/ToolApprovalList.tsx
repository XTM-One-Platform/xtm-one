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
 * Which tools ask first, and the control for each.
 *
 * Rendered in two places, one per decision, and never mixed:
 *
 *   platform — Settings > Access and security > Tool approvals (administrators): the
 *              platform's rules. Every tool this platform can name, with
 *              "Let it run without asking" / "Make it ask first" for everyone.
 *   personal — Profile > Tool approvals: only the tools that ask first, with
 *              "Always allow for me" for the reader alone.
 *
 * Tools that are neither builtin nor custom get their own groups: they can
 * never be on the shipped list, because we do not know what they do. They are
 * also the only ones a user cannot always allow before a first run — an MCP or
 * integration tool has no definition until its server is bound, and a standing
 * approval with no schema to fingerprint would ask again on the first real call.
 */

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import {
  useAddToWhitelist,
  usePlatformReadBulk,
  useToolGateCatalog,
  type ToolGateEntry,
} from "@/hooks/api/useHitlWhitelist";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { GateRow, type ToolApprovalScope } from "@/pages/tools/ToolGateRow";
import {
  CHANNEL_TYPE_LABELS,
  INTEGRATION_PROVIDER_NAMES,
} from "@/lib/constants";
import { onPersonalList } from "@/lib/hitl";
import { cn } from "@/lib/utils";

/* Which groups a reader left open, kept per group and per list the way
   `useViewMode` keeps a grid/list choice: the biggest group is sixty-odd rows
   on a deployment with OpenCTI connected, so they start collapsed. Scoped by a
   stable id rather than the label, so a translation does not lose it. */
const GROUP_STORAGE_PREFIX = "copilot-hitl-group:";

function readGroupOpen(id: string): boolean {
  try {
    return localStorage.getItem(GROUP_STORAGE_PREFIX + id) === "open";
  } catch {
    // Private browsing / storage disabled — collapsed, like the default.
    return false;
  }
}

/**
 * Verbs that suggest a tool changes something or reaches outside the platform.
 *
 * A hint for the human, never a gate: the API refuses nothing, so a tool this
 * misses is still allowed if an administrator asks for it. The point is that
 * the consequential cases are hard to skim past — and this is the one control
 * that takes a free-text name, so it has no row, no description and no
 * classification to go on.
 */
const CONSEQUENTIAL = [
  "delete",
  "remove",
  "create",
  "update",
  "write",
  "send",
  "post",
  "trigger",
  "execute",
  "run",
  "purge",
  "revoke",
  "grant",
  "publish",
];

/**
 * Let a tool the list cannot name run without asking (FR-003).
 *
 * Everything here comes from what is connected or has run, so a tool whose
 * integration is not bound yet has no row to act on — and that is exactly when
 * an operator wants to rule on it, ahead of connecting it. Typed names are
 * accepted whether or not this deployment recognises them; an unknown name can
 * never exempt a real tool, and refusing would make the case above impossible.
 */
function AllowByName({ suspended }: { suspended?: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const add = useAddToWhitelist();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  /* Names this deployment can vouch for, offered rather than left to memory —
     the field accepts anything, so this narrows nothing. */
  const { data: catalog } = useToolGateCatalog();
  const known = useMemo(
    () => (catalog?.tools ?? []).filter((t) => t.gates).map((t) => t.tool_key),
    [catalog?.tools],
  );

  const submit = async () => {
    const toolKey = name.trim();
    if (!toolKey) return;
    const consequential = CONSEQUENTIAL.some((verb) =>
      toolKey.toLowerCase().includes(verb),
    );
    const ok = await confirm({
      title: t_i18n("Let {tool} run without asking anyone?", { tool: toolKey }),
      description: consequential
        ? t_i18n(
            "This name looks like a tool that changes something or acts outside the platform. No agent and no person here will be asked before it runs, scheduled runs nobody is watching included. The change is recorded in the audit log, and you can make it ask first again.",
          )
        : t_i18n(
            "No agent and no person on this platform will be asked before it runs, scheduled runs nobody is watching included. The change is recorded in the audit log, and you can make it ask first again here.",
          ),
      confirmLabel: t_i18n("Let it run without asking"),
      variant: consequential ? "danger" : "warning",
    });
    if (!ok) return;
    add.mutate(
      { toolKey },
      {
        onSuccess: (result) => {
          setName("");
          setOpen(false);
          /* An unrecognised name is accepted and says so. Silence here is what
             makes a typo invisible: the entry never matches, the tool keeps
             asking, and nothing explains why. */
          toast({
            title: result.recognised
              ? t_i18n("It runs without asking now")
              : t_i18n("Saved, but nothing here answers to that name"),
            description: result.recognised
              ? undefined
              : result.suggestion
              ? t_i18n("Did you mean {suggestion}?", {
                  suggestion: result.suggestion,
                })
              : t_i18n(
                  "It will apply if a tool by that name appears: check the spelling if you expected it to already.",
                ),
            variant: result.recognised ? "success" : "info",
          });
        },
        onError: (err) =>
          toast({
            title: t_i18n("Could not apply that"),
            description: err.message,
            variant: "error",
          }),
      },
    );
  };

  if (!open) {
    return (
      <Button
        size="sm"
        variant="ghost"
        className="h-8 shrink-0 text-[11px]"
        disabled={suspended}
        onClick={() => setOpen(true)}
      >
        <Icon name="plus" size={16} className="mr-1" />
        {t_i18n("Add a tool by name")}
      </Button>
    );
  }

  return (
    <div className="flex shrink-0 items-center gap-1">
      {ConfirmEl}
      <datalist id="allow-known-tools">
        {known.map((n) => (
          <option key={n} value={n} />
        ))}
      </datalist>
      <Input aria-label={t_i18n("Exact tool name")}
        autoFocus
        list="allow-known-tools"
        value={name}
        placeholder={t_i18n("Exact tool name")}
        className="h-8 w-56 font-mono-plex text-[11px]"
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") setOpen(false);
        }}
      />
      <Button
        size="sm"
        variant="outline"
        className="h-8 text-[11px]"
        disabled={!name.trim() || add.isPending}
        aria-busy={add.isPending}
        onClick={submit}
      >
        {add.isPending && <Spinner size="sm" tone="inherit" />}
        {t_i18n("Let it run without asking")}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="h-8 text-[11px]"
        onClick={() => setOpen(false)}
      >
        {t_i18n("Cancel")}
      </Button>
    </div>
  );
}

/**
 * Make every read tool one registered platform contributes ask first, or put
 * them back to running without asking.
 *
 * The derived list arrives sixty names at a time, so ruling on it has to be one
 * action. Offered beside the provider whose reads it governs, because that is
 * where the rows it changes are. Only for registered platforms: there is no
 * equivalent for an ordinary integration's tools.
 */
function PlatformReadBulk({
  provider,
  suspended,
}: {
  provider: string;
  suspended?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const bulk = usePlatformReadBulk();
  /* Read from the catalogue, NOT from the rows the group renders: those are
     filtered by the search box, and the endpoint acts on every read of the
     provider regardless. With a `?tool=` deep link seeding the search with one
     name, a filtered count would have offered "all 1" and then changed sixty. */
  const { data } = useToolGateCatalog();
  const reads = useMemo(
    () =>
      (data?.tools ?? []).filter(
        (t) => t.source === "platform" && t.provider === provider,
      ),
    [data?.tools, provider],
  );
  if (reads.length === 0) return null;

  // Every one asking is exactly the state in which the button that puts them
  // back is needed, so the control flips rather than disappearing.
  const allRegated = reads.every((r) => r.regated);
  const affected = reads.filter((r) => (allRegated ? r.regated : !r.regated));

  const run = async () => {
    /* Both directions confirm, and the one that stops asking is the loud one. */
    const ok = await confirm(
      allRegated
        ? {
            title: t_i18n("Let all its read tools run without asking?"),
            description: t_i18n(
              "All {count} of this platform's read tools will run without a person approving them, on every agent and every scheduled run. They are read-only, so they cannot change anything, but nobody will be asked.",
              { count: affected.length },
            ),
            confirmLabel: t_i18n("Let them run without asking"),
            variant: "warning" as const,
          }
        : {
            title: t_i18n("Make all its read tools ask first?"),
            description: t_i18n(
              "All {count} of this platform's read tools will wait for a person on every agent that asks before running tools. Reading data is most of what agents do here, so expect many more requests.",
              { count: affected.length },
            ),
            confirmLabel: t_i18n("Make them ask first"),
            variant: "default" as const,
          },
    );
    if (!ok) return;
    bulk.mutate(
      { provider, action: allRegated ? "restore-default" : "require-approval" },
      {
        onSuccess: (result) =>
          toast({
            title: allRegated
              ? t_i18n("Its read tools run without asking again")
              : t_i18n("Its read tools now ask first"),
            description: t_i18n("{count} tools", { count: result.changed }),
            variant: "success",
          }),
        onError: (err) =>
          toast({
            title: t_i18n("Could not apply that"),
            description: err.message,
            variant: "error",
          }),
      },
    );
  };

  return (
    <>
      {ConfirmEl}
      <Button
        size="sm"
        variant="ghost"
        className="h-5 px-1.5 content-caption font-normal normal-case tracking-normal"
        disabled={suspended || bulk.isPending}
        onClick={run}
      >
        {allRegated
          ? t_i18n("Let its reads run without asking")
          : t_i18n("Make all its reads ask first")}
      </Button>
    </>
  );
}

function Group({
  id,
  label,
  hint,
  entries,
  approvals,
  scope,
  suspended,
  allowAll,
  searching,
}: {
  /** Stable key for the remembered open/closed state. */
  id: string;
  label: string;
  hint: string;
  entries: ToolGateEntry[];
  approvals: Map<string, string>;
  scope: ToolApprovalScope;
  suspended?: boolean;
  /** The reader allows every tool, so nothing asks them (personal list only). */
  allowAll?: boolean;
  /** A search is active, so what matched is shown rather than hidden. */
  searching?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const storageId = `${scope}:${id}`;
  const [open, setOpen] = useState(() => readGroupOpen(storageId));

  /* Searching opens what matched, and closing one while searching is a
     transient act — it must not overwrite what the reader chose to keep open
     for their next visit. */
  const [searchOpen, setSearchOpen] = useState(true);
  useEffect(() => setSearchOpen(true), [searching]);

  const shown = searching ? searchOpen : open;

  const toggle = () => {
    if (searching) {
      setSearchOpen((was) => !was);
      return;
    }
    setOpen((was) => {
      const next = !was;
      try {
        localStorage.setItem(GROUP_STORAGE_PREFIX + storageId, next ? "open" : "shut");
      } catch {
        // Nothing to remember it with; the session's own state still holds.
      }
      return next;
    });
  };
  /* The platform list counts the platform's rule; the personal list counts
     what asks the reader, which also folds in their own approvals. */
  const asks = (e: ToolGateEntry) =>
    scope === "platform"
      ? e.gates
      : e.gates && !suspended && !allowAll && !approvals.has(e.tool_key);
  const asking = entries.filter(asks).length;

  /* Grouped by what contributes them — a platform, an MCP server, a provider —
     so a group of 60 OpenCTI reads and 4 OpenAEV ones reads as two lists and
     not as one wall. */
  const byProvider = useMemo(() => {
    const map = new Map<string | null, ToolGateEntry[]>();
    for (const entry of entries) {
      const key = entry.provider ?? null;
      const rows = map.get(key);
      if (rows) rows.push(entry);
      else map.set(key, [entry]);
    }
    // Within a provider, what asks first leads.
    for (const rows of map.values()) {
      rows.sort(
        (a, b) =>
          Number(b.gates) - Number(a.gates) ||
          a.tool_key.localeCompare(b.tool_key),
      );
    }
    return [...map.entries()].sort(([a], [b]) =>
      a === null ? -1 : b === null ? 1 : a.localeCompare(b),
    );
  }, [entries]);

  if (entries.length === 0) return null;

  return (
    <div className="space-y-1.5" data-testid="tool-approval-group" data-group={id}>
      <button
        type="button"
        className="flex w-full items-center gap-2 text-left"
        aria-expanded={shown}
        onClick={toggle}
      >
        {shown ? (
          <Icon name="chevron-down" size={16} className="shrink-0 text-default-secondary" />
        ) : (
          <Icon name="chevron-right" size={16} className="shrink-0 text-default-secondary" />
        )}
        <span className="content-compact font-medium">{label}</span>
        <span className="text-[11px] tabular-nums text-default-secondary">
          {scope === "platform"
            ? t_i18n("{asking} of {total} ask first", {
                asking,
                total: entries.length,
              })
            : t_i18n("{asking} of {total} ask you first", {
                asking,
                total: entries.length,
              })}
        </span>
      </button>
      {shown && (
        <>
          <p className="pl-5 text-[11px] text-default-secondary">{hint}</p>
          <div className="space-y-2 pl-5">
            {byProvider.map(([provider, rows]) => (
              <div key={provider ?? "_"} className="space-y-1">
                {provider && (
                  <p className="flex items-center gap-1.5 pt-1 content-caption uppercase tracking-wider text-default-secondary">
                    {INTEGRATION_PROVIDER_NAMES[provider] ??
                      CHANNEL_TYPE_LABELS[provider.replace(/_bot$/, "")] ??
                      provider}
                    <span className="tabular-nums text-default-secondary">
                      {t_i18n("{asking}/{total}", {
                        asking: rows.filter(asks).length,
                        total: rows.length,
                      })}
                    </span>
                    {scope === "platform" && (
                      <PlatformReadBulk provider={provider} suspended={suspended} />
                    )}
                  </p>
                )}
                {rows.map((entry) => (
                  <GateRow
                    key={entry.tool_key}
                    entry={entry}
                    approvedId={approvals.get(entry.tool_key)}
                    scope={scope}
                    suspended={suspended}
                  />
                ))}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function ToolApprovalList({ scope }: { scope: ToolApprovalScope }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useToolGateCatalog();
  const { data: mine } = useToolApprovals();
  /* `?tool=<name>` seeds the filter, so a surface that can only *report* a
     tool's state — an integration's tool table, a tool's own page — hands the
     reader straight to the row that changes it. Dropped once applied: it is a
     way in, not a state to keep. */
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(() => params.get("tool") ?? "");

  useEffect(() => {
    const wanted = params.get("tool");
    if (!wanted) return;
    setSearch(wanted);
    const next = new URLSearchParams(params);
    next.delete("tool");
    setParams(next, { replace: true });
  }, [params, setParams]);

  /* Keyed by tool, because that is how a row asks: "do I already allow this?"
     Revoking needs the preference id, so the map carries it. */
  const approvals = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of mine?.preferences ?? []) map.set(p.tool_key, p.id);
    return map;
  }, [mine?.preferences]);

  const [mineOnly, setMineOnly] = useState(false);

  const groups = useMemo(() => {
    const term = search.trim().toLowerCase();
    /* The personal list holds what is the reader's to decide: the tools that
       ask first, and the ones they already always allow (so they can take an
       approval back even once the tool stopped asking). Matches the provider
       as well as the name: "opencti" is what a reader types for that group. */
    const all = (data?.tools ?? []).filter(
      (t) =>
        (scope === "platform" || onPersonalList(t, approvals)) &&
        (!mineOnly || approvals.has(t.tool_key)) &&
        (!term ||
          t.tool_key.toLowerCase().includes(term) ||
          (t.provider ?? "").toLowerCase().includes(term)),
    );
    return {
      builtin: all.filter((t) => t.source === "builtin"),
      custom: all.filter((t) => t.source === "custom"),
      mcp: all.filter((t) => t.source === "mcp"),
      connected: all.filter(
        (t) => t.source === "integration" || t.source === "platform",
      ),
      channel: all.filter((t) => t.source === "channel"),
      unattributed: all.filter((t) => t.source === "other"),
    };
  }, [data?.tools, search, mineOnly, approvals, scope]);

  const allowAll = mine?.approve_all_tools ?? false;
  const suspended = mine?.tool_approvals_enabled === false;
  /* The effective rules cannot be computed while a registered platform is
     unreadable, so `gates` reads TRUE for tools that are in fact exempt as
     platform reads. Editing the platform rules has to stop: an admin letting
     such a row run without asking would pin exempt a tool they only believed
     asked first. A personal approval is unaffected. */
  const readsUnknown = data?.platform_reads_available === false;
  const editsRefused =
    scope === "platform" ? suspended || readsUnknown : suspended;
  const searching = !!search.trim();
  const groupProps = { approvals, scope, suspended: editsRefused, allowAll, searching };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-[12rem] flex-1">
          <SearchInput
            placeholder="Search by tool or provider…"
            value={search}
            onChange={setSearch}
          />
        </div>
        {scope === "personal" ? (
          <Button
            size="sm"
            variant={mineOnly ? "default" : "ghost"}
            className="h-8 shrink-0 gap-1.5 text-[11px]"
            aria-pressed={mineOnly}
            onClick={() => setMineOnly((was) => !was)}
          >
            <HitlIcon
              state={mineOnly ? "open" : "unknown"}
              className={cn("size-3", mineOnly && "text-current")}
              decorative
            />
            {t_i18n("Only the ones I always allow")}
            <span className="tabular-nums opacity-70">{approvals.size}</span>
          </Button>
        ) : (
          <AllowByName suspended={editsRefused} />
        )}
      </div>

      {suspended && (
        <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2 content-caption text-feedback-warning-tertiary">
          {t_i18n(
            "Approvals are suspended for the whole platform, so nothing below asks anyone right now. Everything here applies again when they are resumed.",
          )}
        </p>
      )}
      {scope === "platform" && readsUnknown && (
        <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2 content-caption text-feedback-warning-tertiary">
          {t_i18n(
            "A connected platform's tools could not be read just now, so this list is incomplete and cannot be edited: a tool shown as asking first may in fact run without asking. Retry in a moment.",
          )}
        </p>
      )}
      {scope === "personal" && !suspended && allowAll && (
        <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2 content-caption text-feedback-warning-tertiary">
          {t_i18n(
            "You allow every tool, so none of these asks you. They still ask everyone else first.",
          )}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 content-compact text-default-secondary">
          <Spinner size="sm" />
          {t_i18n("Loading…")}
        </div>
      ) : (
        <div className="space-y-3">
          <Group
            id="builtin"
            label={t_i18n("Built-in")}
            hint={t_i18n("Product tools. Read-only ones run without asking.")}
            entries={groups.builtin}
            {...groupProps}
          />
          <Group
            id="custom"
            label={t_i18n("Custom")}
            hint={t_i18n("This platform's own sandboxed scripts. They ask first until an administrator rules otherwise.")}
            entries={groups.custom}
            {...groupProps}
          />
          <Group
            id="mcp"
            label={t_i18n("MCP servers")}
            hint={t_i18n(
              "Tools from the MCP servers bound here. Read-only ones run without asking; they appear once a server is connected.",
            )}
            entries={groups.mcp}
            {...groupProps}
          />
          <Group
            id="connected"
            label={t_i18n("Integrations and connected platforms")}
            hint={t_i18n(
              "Every tool the connected providers contribute. Read-only ones run without asking; the ones that change something ask first.",
            )}
            entries={groups.connected}
            {...groupProps}
          />
          <Group
            id="channel"
            label={t_i18n("Channels and bots")}
            hint={t_i18n(
              "Tools the connected bots contribute: reading, sending, editing and deleting messages in Slack, Teams or Discord. None runs without asking by default, since they act in people's conversations. Each bot also has its own switch under Settings > Integrations > Channels & Bots.",
            )}
            entries={groups.channel}
            {...groupProps}
          />
          <Group
            id="unattributed"
            label={t_i18n("No longer connected")}
            hint={t_i18n(
              "Nothing connected right now provides these: a server since removed, a provider disconnected, or a name added before the tool existed.",
            )}
            entries={groups.unattributed}
            {...groupProps}
          />
          {scope === "personal" &&
            mineOnly &&
            (() => {
              const shown = Object.values(groups).reduce(
                (n, g) => n + g.length,
                0,
              );
              const missing = approvals.size - shown;
              if (searching || missing <= 0) return null;
              return (
                <p className="text-[11px] text-default-secondary">
                  {t_i18n(
                    "{n, plural, one {# of your approvals names a tool} other {# of your approvals name tools}} nothing here can list: a server since removed, or a provider disconnected. They are in the list above.",
                    { n: missing },
                  )}
                </p>
              );
            })()}
          {Object.values(groups).every((g) => g.length === 0) &&
            (searching || mineOnly ? (
              <p className="py-6 text-center content-compact text-default-secondary">
                {t_i18n("No tools matching your filters")}
              </p>
            ) : (
              scope === "personal" && (
                <p className="py-6 text-center content-compact text-default-secondary">
                  {t_i18n("No tool asks first on this platform.")}
                </p>
              )
            ))}
        </div>
      )}
    </div>
  );
}
