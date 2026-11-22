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
 * The two layers of a built-in agent on its Configuration > General section
 * (#914, slice 4): "Locked by the product" beside "Yours", the organization
 * notes appended after the seeded persona, and the agent's learned skill with
 * its lessons and the run behind each change. No new menu entry: the card
 * sits on the page that already configures the agent.
 *
 * An administrator edits the notes here; the learned skill is edited, frozen,
 * cleared or rolled back on its own skill page (Versioning tab), which this
 * card links to. Everyone else reads both.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Icon, Textarea } from "@filigran/design-system";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useSkillVersions, useSkills } from "@/hooks/api/useSkillApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import {
  CUSTOMER_NOTES_MAX_CHARS,
  LOCKED_ITEMS,
  YOURS_ITEMS,
  learnedLessonTitles,
  learnedSkillFrozen,
  learnedSkillOf,
  versionRunId,
  yoursItemSet,
} from "@/lib/builtin-layers";
import type { Agent } from "@/types";

/** Versions of the learned skill the card lists; the skill page lists them all. */
const HISTORY_LIMIT = 5;

export function BuiltinLayersCard({ agent, canEditCustomerLayer }: { agent: Agent; canEditCustomerLayer: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <Card data-testid="builtin-layers-card">
      {/* The section-card header of General and Pipeline: one left edge and one title style on the page (UX r1 B4, A5). */}
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          {t_i18n("What you can change on this built-in agent")}
          <InfoTip
            label={t_i18n("What each layer means")}
            content={t_i18n("Each release rewrites what the product locks and keeps what is yours. To change the locked part, duplicate the fleet.")}
          />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 px-4 pb-4 pt-0">
        <div className="grid gap-3 md:grid-cols-2">
          <section aria-labelledby="builtin-locked" className="rounded-md border border-elevation-subtle p-3">
            <h3 id="builtin-locked" className="flex items-center gap-1.5 content-compact font-medium">
              <Icon name="lock" size={16} className="text-default-secondary" aria-hidden /> {t_i18n("Locked by the product")}
            </h3>
            <ul className="mt-2 space-y-1 content-compact text-default-secondary">
              {LOCKED_ITEMS.map((item) => (
                <li key={item}>{t_i18n(item)}</li>
              ))}
            </ul>
          </section>
          <section aria-labelledby="builtin-yours" className="rounded-md border border-elevation-subtle p-3">
            <h3 id="builtin-yours" className="flex items-center gap-1.5 content-compact font-medium">
              <Icon name="users" size={16} className="text-accent" aria-hidden /> {t_i18n("Yours")}
            </h3>
            <ul className="mt-2 space-y-1 content-compact">
              {YOURS_ITEMS.map((item) => (
                <li key={item} className="flex items-center gap-2">
                  <span className="min-w-0 truncate">{t_i18n(item)}</span>
                  {yoursItemSet(item, agent) ? (
                    <Badge variant="secondary" className="shrink-0">{t_i18n("Set")}</Badge>
                  ) : (
                    <span className="shrink-0 content-caption text-default-secondary">{t_i18n("Not set")}</span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        </div>
        <OrganizationNotes agent={agent} canEdit={canEditCustomerLayer} />
        <LearnedSkill agentId={agent.id} />
      </CardContent>
    </Card>
  );
}

function OrganizationNotes({ agent, canEdit }: { agent: Agent; canEdit: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAgent();
  // PUT /agents/{id} sits behind the AI gate: with AI off a save would only refuse.
  const aiOff = useAiActionDisabled();
  const stored = agent.customer_notes ?? "";
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? stored;
  const changed = draft !== null && draft.trim() !== stored.trim();

  const save = () =>
    update.mutate(
      { id: agent.id, customer_notes: value.trim() || null },
      {
        onSuccess: () => {
          setDraft(null);
          toast({ title: t_i18n("Organization notes saved") });
        },
      },
    );

  return (
    <section aria-labelledby="builtin-notes" className="space-y-1.5">
      {/* The body-size field label with its InfoTip (UX r1 B2). */}
      <div className="flex items-center gap-1">
        <Label id="builtin-notes" htmlFor={canEdit ? "builtin-notes-input" : undefined}>
          {t_i18n("Organization notes")}
        </Label>
        <InfoTip
          label={t_i18n("What organization notes are")}
          content={t_i18n("Every run of this agent reads them after the product's persona, never in its place: who owns which asset, how tickets are routed, what to leave alone. Up to 4,000 characters. Empty, the agent runs on the persona alone.")}
        />
      </div>
      <p id="builtin-notes-help" className="content-compact text-default-secondary">
        {t_i18n("Added after the product's persona on every run, never in its place.")}
      </p>
      {canEdit ? (
        <>
          <Textarea
            id="builtin-notes-input"
            aria-describedby="builtin-notes-help"
            value={value}
            maxLength={CUSTOMER_NOTES_MAX_CHARS}
            rows={4}
            placeholder={t_i18n("Who owns which asset, how tickets are routed, what to leave alone")}
            onChange={(event) => setDraft(event.target.value)}
          />
          {changed && (
            <div className="flex justify-end gap-2">
                <Button type="button" variant="secondary" size="sm" onClick={() => setDraft(null)} disabled={update.isPending}>
                  {t_i18n("Cancel")}
                </Button>
                <AiDisabledTip>
                  <Button type="button" size="sm" onClick={save} disabled={aiOff || update.isPending}>
                    {t_i18n("Save")}
                  </Button>
                </AiDisabledTip>
            </div>
          )}
        </>
      ) : stored ? (
        <p className="whitespace-pre-wrap content-compact">{stored}</p>
      ) : (
        <p className="content-compact text-default-secondary">{t_i18n("No organization notes yet.")}</p>
      )}
    </section>
  );
}

function LearnedSkill({ agentId }: { agentId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data: skills, isLoading } = useSkills({ scope: "all" });
  const skill = learnedSkillOf(skills, agentId);
  const { data: versions } = useSkillVersions(skill?.id ?? "", { limit: HISTORY_LIMIT });
  const lessons = learnedLessonTitles(skill?.content);

  return (
    <section aria-labelledby="builtin-learned" className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="builtin-learned" className="flex items-center gap-1.5 content-compact font-medium">
          <Icon name="graduation-cap" size={16} className="text-accent" aria-hidden /> {t_i18n("Learned skill")}
          {learnedSkillFrozen(skill) && <Badge variant="outline">{t_i18n("Frozen")}</Badge>}
        </h3>
        {skill && (
          <Button asChild variant="ghost" size="sm">
            <Link to={`/skills/${skill.id}/versioning`}>{t_i18n("Open its history")}</Link>
          </Button>
        )}
      </div>
      {isLoading ? null : !skill ? (
        <p className="content-compact text-default-secondary">
          {t_i18n("The agent creates it with its first lesson: an asset owner, a false positive, a ticket routing rule.")}
        </p>
      ) : (
        <>
          <p className="content-compact text-default-secondary">
            {lessons.length === 0
              ? t_i18n("No lesson yet. The agent reads this skill on every run.")
              : t_i18n("{count, plural, one {# lesson, read on every run} other {# lessons, read on every run}}", { count: lessons.length })}
          </p>
          {lessons.length > 0 && (
            <ul className="flex flex-wrap gap-1.5" aria-label={t_i18n("Lessons")}>
              {lessons.map((title) => (
                <li key={title}>
                  <Badge variant="secondary" className="max-w-[240px] truncate" title={title}>
                    {title}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
          {versions && versions.items.length > 0 && (
            <ol className="space-y-1 border-l border-elevation-subtle pl-3" aria-label={t_i18n("Latest changes")}>
              {versions.items.map((version) => {
                const run = versionRunId(version.commit_message);
                return (
                  <li key={version.id} className="flex flex-wrap items-baseline gap-x-2 content-compact">
                    <span className="font-mono-plex text-default-secondary">v{version.version_number}</span>
                    <span className="min-w-0 truncate" title={version.commit_message ?? ""}>
                      {(version.commit_message ?? "").replace(/\s*\(run [^)]+\)\s*$/, "")}
                    </span>
                    {run && (
                      <span className="content-caption text-default-secondary" title={run}>
                        {t_i18n("run {id}", { id: run.slice(0, 8) })}
                      </span>
                    )}
                    <span className="content-caption text-default-secondary">{intl.formatDate(version.created_at, { dateStyle: "medium", timeStyle: "short" })}</span>
                  </li>
                );
              })}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
