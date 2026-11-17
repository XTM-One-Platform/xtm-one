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

import { Icon } from "@filigran/design-system";
import { useId, useMemo, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";

/**
 * Sentinel for "no credential": a picker option cannot hold an empty value, so
 * the public/anonymous choice needs its own token.
 */
export const NO_SOURCE_CREDENTIAL = "none";

/**
 * Credential + tracked-branch fields for a URL-based skill.
 *
 * Selecting a connected GitHub integration makes the platform authenticate the
 * SKILL.md fetch with that integration's stored credential, which is what lets
 * a skill live in a **private** repository. Leaving it on the public option
 * keeps the historical anonymous fetch.
 *
 * Shared by the create form and the catalog edit view so both stay in sync on
 * the wording and on the "ref only matters with a credential" rule.
 */
export function PrivateSourceFields({
  integrationId,
  onIntegrationIdChange,
  sourceRef,
  onSourceRefChange,
  compact = false,
}: {
  integrationId: string;
  onIntegrationIdChange: (value: string) => void;
  sourceRef: string;
  onSourceRefChange: (value: string) => void;
  /** Denser sizing for the inline edit view. */
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const baseId = useId();
  const accessId = `${baseId}-access`;
  const accessHelpId = `${accessId}-help`;
  const refId = `${baseId}-ref`;
  const refHelpId = `${refId}-help`;
  // The viewer's integrations plus the one the skill already fetches through,
  // so an admin editing a colleague's skill keeps its credential.
  const { data: visibleIntegrations } = useIntegrations();
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const integrations = useMemo(
    () => withBoundEntities(
      visibleIntegrations,
      allIntegrations,
      integrationId && integrationId !== NO_SOURCE_CREDENTIAL ? [integrationId] : [],
    ),
    [visibleIntegrations, allIntegrations, integrationId],
  );

  const githubIntegrations = (integrations ?? []).filter(
    (i) => i.provider === "github" && i.status === "connected",
  );
  const hasCredential = integrationId !== NO_SOURCE_CREDENTIAL && integrationId !== "";
  const fieldHeight = compact ? "h-8 content-base" : "";
  // The create form uses the design-system Label (14 px); the inline edit view keeps its denser label.
  const fieldLabel = (text: ReactNode, htmlFor?: string) =>
    compact ? (
      <label htmlFor={htmlFor} className="content-compact text-default-secondary">{text}</label>
    ) : (
      <Label htmlFor={htmlFor} className="block text-default-primary">{text}</Label>
    );

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        {fieldLabel(t_i18n("Private repository access (optional)"), githubIntegrations.length > 0 ? accessId : undefined)}
        {githubIntegrations.length > 0 ? (
          <>
            <SearchableSelect
              id={accessId}
              aria-describedby={accessHelpId}
              aria-label={t_i18n("Private repository access (optional)")}
              options={githubIntegrations.map((int) => ({
                value: int.id,
                label: int.name,
                description: int.is_company_managed ? t_i18n("(company)") : undefined,
              }))}
              pinned={[{ value: NO_SOURCE_CREDENTIAL, label: t_i18n("Public repository (no credential)") }]}
              value={integrationId || NO_SOURCE_CREDENTIAL}
              onValueChange={onIntegrationIdChange}
            />
            <p id={accessHelpId} className="content-compact text-default-secondary">
              {t_i18n("Select a connected GitHub integration to read a SKILL.md from a private repository. Its credentials authenticate every refresh; nothing is copied onto the skill.")}
            </p>
          </>
        ) : (
          <div className="flex items-start gap-1.5 rounded-sm bg-elevation-hover p-2.5">
            <Icon name="info" size={16} className="mt-px shrink-0 text-default-secondary" />
            <p className="content-compact text-default-secondary">
              {t_i18n("To use a SKILL.md from a private GitHub repository, first connect a GitHub integration (personal access token or GitHub App) in Integrations.")}
            </p>
          </div>
        )}
      </div>

      {hasCredential && (
        <div className="space-y-1.5">
          {fieldLabel(t_i18n("Tracked branch (optional)"), refId)}
          <Input id={refId} aria-describedby={refHelpId} aria-label={t_i18n("Tracked branch (optional)")}
            value={sourceRef}
            onChange={(e) => onSourceRefChange(e.target.value)}
            className={`font-mono-plex ${fieldHeight}`}
            placeholder={t_i18n("Default branch")}
          />
          <p id={refHelpId} className="content-compact text-default-secondary">
            {t_i18n("Leave empty to always serve the latest commit merged to the repository's default branch. Set a branch to follow it instead, or a commit SHA to pin a fixed version.")}
          </p>
        </div>
      )}
    </div>
  );
}
