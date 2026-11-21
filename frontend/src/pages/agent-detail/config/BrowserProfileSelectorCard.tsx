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

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Globe, KeyRound, Lock, Plus } from "lucide-react";

import { SESSION_STATUS_LABELS, SESSION_VARIANTS } from "@/components/browser-profiles/browser-profile-format";
import { Badge } from "@/components/ui/badge";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useBrowserProfiles } from "@/hooks/api/useBrowserProfilesApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Agent } from "@/types";

/**
 * The browser profiles an agent's browser tool may open.
 *
 * Binding is explicit: an agent never reaches every login its users can see.
 * A run opens a bound profile only for a person who may use it, so the list
 * offers the profiles the viewer may use; a bound one the viewer cannot see
 * stays bound and is shown as such. A read-only binding refuses clicking,
 * typing, key presses and scripts: the agent navigates to URLs and reads.
 * Appears only while the agent carries the `browser` function: the `web`
 * group (or its former `browser` id) without a scope that drops it.
 */
export function BrowserProfileSelectorCard({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { carries } = useBuiltinTools();
  const webScope = agent.builtin_allowed_tools?.web;
  const browserOn =
    (agent.builtin_tool_ids || []).includes("browser") ||
    (carries(agent.builtin_tool_ids, "web") && (webScope == null || webScope.includes("browser")));
  const { data: profiles } = useBrowserProfiles({ enabled: browserOn });
  const update = useUpdateAgent();
  const aiOff = useAiActionDisabled();

  const [editing, setEditing] = useState(false);
  const [draftIds, setDraftIds] = useState<string[]>([]);
  const [draftReadOnly, setDraftReadOnly] = useState<string[]>([]);

  const boundIds = useMemo(() => agent.browser_profile_ids || [], [agent.browser_profile_ids]);
  const readOnlyIds = useMemo(() => agent.browser_profile_read_only || [], [agent.browser_profile_read_only]);
  const byId = useMemo(() => new Map((profiles ?? []).map((p) => [p.id, p])), [profiles]);

  if (!browserOn) return null;

  const start = () => {
    setDraftIds([...boundIds]);
    setDraftReadOnly([...readOnlyIds]);
    setEditing(true);
  };
  const toggle = (id: string) => {
    setDraftIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    setDraftReadOnly((prev) => prev.filter((x) => x !== id));
  };
  const setReadOnly = (id: string, on: boolean) =>
    setDraftReadOnly((prev) => (on ? [...prev.filter((x) => x !== id), id] : prev.filter((x) => x !== id)));
  const save = () => {
    update.mutate(
      {
        id: agent.id,
        browser_profile_ids: draftIds,
        browser_profile_read_only: draftReadOnly.filter((id) => draftIds.includes(id)),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Browser profiles saved"), variant: "success" });
          setEditing(false);
        },
        onError: (e: Error) =>
          toast({ title: t_i18n("Could not save the browser profiles"), description: e.message, variant: "error" }),
      },
    );
  };

  const hidden = boundIds.filter((id) => !byId.has(id));

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <div>
          <CardTitle className="content-base flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-accent" /> {t_i18n("Browser profiles")}
            {boundIds.length > 0 && !editing && (
              <Badge variant="secondary" className="text-[9px] h-4 px-1.5">
                {t_i18n("{count, plural, one {# profile} other {# profiles}}", { count: boundIds.length })}
              </Badge>
            )}
          </CardTitle>
          <p className="content-caption text-default-secondary mt-0.5">
            {t_i18n("The saved sign-ins this agent's browser opens, for the people who may use them.")}
          </p>
        </div>
        <CardEditActions editing={editing} canEdit={canManage} onEdit={start} onCancel={() => setEditing(false)} onSave={save} saveDisabled={update.isPending || aiOff} aiGated />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        {editing ? (
          <div className="space-y-3">
            <p className="content-caption text-default-secondary">
              {t_i18n(
                "Pick the profiles the agent may sign in with. A run opens one only for a person who may use it. Read-only lets it navigate and read, never click, type or run a script.",
              )}
            </p>
            {profiles && profiles.length > 0 ? (
              <div className="grid gap-2 sm:grid-cols-2">
                {profiles.map((p) => {
                  const selected = draftIds.includes(p.id);
                  return (
                    <div
                      key={p.id}
                      className={cn(
                        "rounded-sm border p-3 transition-all",
                        selected ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover",
                      )}
                    >
                      <button type="button" onClick={() => toggle(p.id)} className="flex w-full items-center gap-3 text-left">
                        <Globe className={cn("h-4 w-4 shrink-0", selected ? "text-accent" : "text-default-disabled")} />
                        <div className="min-w-0 flex-1">
                          <p className={cn("content-base font-medium truncate", selected && "text-accent")}>{p.name}</p>
                          <p className="content-caption text-default-secondary truncate">{p.start_url}</p>
                        </div>
                        {selected ? (
                          <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                        ) : (
                          <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                        )}
                      </button>
                      {selected && (
                        <label className="mt-2 flex items-center justify-between gap-2 content-caption text-default-secondary">
                          {t_i18n("Read-only")}
                          <Switch
                            checked={draftReadOnly.includes(p.id)}
                            onCheckedChange={(on) => setReadOnly(p.id, on)}
                            aria-label={t_i18n("Read-only")}
                          />
                        </label>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="content-caption text-default-secondary italic">
                {t_i18n("No browser profile you may use yet.")}{" "}
                <Link to="/connections?new=1" className="text-accent hover:underline">
                  {t_i18n("New browser profile")}
                </Link>
              </p>
            )}
            {hidden.length > 0 && (
              <p className="content-caption text-default-secondary">
                {t_i18n("{count, plural, one {# bound profile you cannot see stays bound.} other {# bound profiles you cannot see stay bound.}}", {
                  count: hidden.length,
                })}
              </p>
            )}
          </div>
        ) : boundIds.length === 0 ? (
          <p className="content-base text-default-secondary italic py-2">
            {t_i18n("None: the agent browses without a sign-in.")}
          </p>
        ) : (
          <div className="space-y-1.5">
            {boundIds.map((id) => {
              const p = byId.get(id);
              return (
                <div key={id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                  <Globe className="h-4 w-4 text-accent shrink-0" />
                  <p className="content-base font-medium flex-1 truncate">{p ? p.name : t_i18n("A profile you cannot see")}</p>
                  {readOnlyIds.includes(id) && (
                    <Badge variant="outline" className="gap-1">
                      <Lock className="h-3 w-3" /> {t_i18n("Read-only")}
                    </Badge>
                  )}
                  {p && <Badge variant={SESSION_VARIANTS[p.session_status]}>{t_i18n(SESSION_STATUS_LABELS[p.session_status])}</Badge>}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
