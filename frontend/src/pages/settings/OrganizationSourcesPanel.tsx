// SPDX-License-Identifier: Apache-2.0

/**
 * The Sources panel at the top of the organization profile (#1134 S2 part 2): who feeds the
 * profile, before what it holds. One row per source (each connected platform, then the Typed,
 * CSV, API and Agent rows), its syncs counted, and one chip per section it feeds with the number
 * of rows; a chip opens the section's tab filtered on that source. "Add a sync" opens the import
 * stepper (pick a platform, pick sections, preview, keep in sync). With no source yet, the panel
 * teaches that flow.
 */
import { useNavigate } from "react-router-dom";
import { Chip, Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SettingsCard } from "@/components/ui/settings-form";
import { useOrganizationSyncs, type OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { IMPORT_SOURCE_NAMES, IMPORT_SOURCE_PROVIDER } from "@/lib/organization-profile";
import { SOURCE_KIND_LABELS, type Provenance } from "@/lib/organization-provenance";
import { SOURCED_SECTION_LABELS, sourceSectionHref, summarizeSources, type SourceSummary } from "@/lib/organization-sources";

interface Props {
  profile: Partial<Record<"crown_jewels" | "tech_stack" | "ai_systems" | "assets_exposure" | "asset_groups", readonly Provenance[] | undefined>>;
  /** The source keys with an integration (opencti, openaev, opencrq; intune and defender through a Microsoft integration). */
  connected: readonly string[];
  /** Opens the import stepper; disabled while the form is in edit. */
  onAddSync: () => void;
  disabled?: boolean;
}

export function OrganizationSourcesPanel({ profile, connected, onAddSync, disabled }: Props) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data } = useOrganizationSyncs();
  const syncs = Array.isArray(data) ? data : [];
  const sources = summarizeSources(profile, connected);
  const label = (source: SourceSummary) =>
    source.kind === "platform" ? (IMPORT_SOURCE_NAMES[source.key] ?? source.key) : t_i18n(SOURCE_KIND_LABELS[source.kind as keyof typeof SOURCE_KIND_LABELS]);
  const syncsOf = (key: string): OrganizationSync[] => syncs.filter((sync) => sync.source === key && sync.section !== "all");

  const addSync = (
    <Button variant="secondary" className="gap-1.5" onClick={onAddSync} disabled={disabled} data-testid="settings.organization.sources.add">
      <Icon name="plus" size={16} aria-hidden /> {t_i18n("Add a sync")}
    </Button>
  );

  return (
    <SettingsCard
      data-testid="settings.organization.sources"
      title={t_i18n("Sources")}
      description={t_i18n("Who feeds the profile: each platform's rows are kept by its sync and read-only here; typed, CSV, API and agent rows stay yours.")}
      action={sources.length > 0 ? addSync : undefined}
    >
      {sources.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3" data-testid="settings.organization.sources.empty">
          <div className="space-y-0.5">
            <p className="font-medium">{t_i18n("Start with your sources")}</p>
            <p className="text-default-secondary">{t_i18n("Pick a platform, pick the sections it fills, preview the rows, then keep them in sync.")}</p>
          </div>
          {addSync}
        </div>
      ) : (
        // One grid for every row (UX r1 B2 on #1232): name with a 16 px logo slot, sync status,
        // then the chips, so each column starts at the same x on every row.
        <ul className="grid grid-cols-[max-content_max-content_minmax(0,1fr)] items-center gap-x-4 gap-y-2" aria-label={t_i18n("Sources")}>
          {sources.map((source) => {
            const name = label(source);
            const sourceSyncs = source.kind === "platform" ? syncsOf(source.key) : [];
            return (
              <li key={source.key} className="col-span-3 grid min-h-6 grid-cols-subgrid items-center" data-testid="settings.organization.sources.row">
                <span className="flex items-center gap-2 font-medium">
                  {source.kind === "platform" ? (
                    <ProviderLogo provider={IMPORT_SOURCE_PROVIDER[source.key] ?? source.key} decorative className="h-4 w-4 shrink-0" />
                  ) : (
                    <span className="h-4 w-4 shrink-0" aria-hidden />
                  )}
                  {name}
                </span>
                <span className="content-compact text-default-secondary">
                  {source.kind === "platform"
                    ? sourceSyncs.length > 0
                      ? t_i18n("{count, plural, one {# sync} other {# syncs}}", { count: sourceSyncs.length })
                      : t_i18n("No sync yet")
                    : null}
                </span>
                <span className="flex min-w-0 flex-wrap gap-1.5">
                  {source.sections.length === 0 ? (
                    <span className="content-compact text-default-secondary">{t_i18n("Feeds no section yet")}</span>
                  ) : (
                    source.sections.map(({ section, count }) => {
                      const sectionLabel = t_i18n(SOURCED_SECTION_LABELS[section]);
                      return (
                        <Chip
                          key={section}
                          label={`${sectionLabel} ${count}`}
                          onClick={() => navigate(sourceSectionHref(section, name))}
                          data-testid="settings.organization.sources.chip"
                          aria-label={t_i18n("{section} from {source}: {count, plural, one {# row} other {# rows}}", { section: sectionLabel, source: name, count })}
                        />
                      );
                    })
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </SettingsCard>
  );
}
