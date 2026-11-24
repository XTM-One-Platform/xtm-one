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
 * Settings > Agents and automations > Platform Health, the module thresholds
 * (#101 S10c): the editable (E) figures of the OpenCTI and OpenAEV checks
 * (`services/health/settings.py`, dev-docs/platform-health.md 4.3 and 4.4).
 * Same draft, same Edit / Save as the page's other blocks; the next pass of the
 * engine reads them. Shown in the XTM One modes only: Copilot mode has no module.
 */
import { Icon } from "@filigran/design-system";

import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { HealthOpenAEVThresholds, HealthOpenCTIThresholds, HealthSettings } from "@/types";

import { BlockTitle, type BlockProps } from "./health-block";
import { NumberField } from "./HealthTriggerBlocks";

const ThresholdsGlyph = ({ className }: { className?: string }) => <Icon name="activity" size={16} className={className} aria-hidden />;

/** The bounds the server holds (`HealthOpenCTIThresholds`, `HealthOpenAEVThresholds`); a value outside them is refused (422). */
const OPENCTI_BOUNDS: Record<keyof HealthOpenCTIThresholds, { min: number; max: number }> = {
  connector_stale_minutes: { min: 5, max: 10080 },
  connector_errors_max: { min: 0, max: 1000000 },
  queue_depth_max: { min: 0, max: 1000000000 },
  search_disk_max_percent: { min: 1, max: 99 },
  ingestion_lag_minutes: { min: 1, max: 10080 },
};

const OPENAEV_BOUNDS: Record<keyof HealthOpenAEVThresholds, { min: number; max: number }> = {
  integration_silent_minutes: { min: 5, max: 10080 },
  agents_offline_max_percent: { min: 0, max: 99 },
};

const within = (value: number, { min, max }: { min: number; max: number }) => Number.isFinite(value) && value >= min && value <= max;

/** Whether every module threshold of the draft is a number within its bounds (Save stays off otherwise). */
export function moduleThresholdsValid(draft: HealthSettings): boolean {
  return (
    (Object.keys(OPENCTI_BOUNDS) as (keyof HealthOpenCTIThresholds)[]).every((key) => within(draft.opencti[key], OPENCTI_BOUNDS[key])) &&
    (Object.keys(OPENAEV_BOUNDS) as (keyof HealthOpenAEVThresholds)[]).every((key) => within(draft.openaev[key], OPENAEV_BOUNDS[key]))
  );
}

export function ModuleThresholdsBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const { exactCount } = useNumberFormat();
  const isXtmOne = useIsXtmOne();
  if (!isXtmOne) return null;
  const cti = draft.opencti;
  const aev = draft.openaev;
  const setCti = (key: keyof HealthOpenCTIThresholds) => (value: number) => onChange({ ...draft, opencti: { ...cti, [key]: value } });
  const setAev = (key: keyof HealthOpenAEVThresholds) => (value: number) => onChange({ ...draft, openaev: { ...aev, [key]: value } });
  return (
    <div data-testid="settings.health.thresholds">
      <BlockTitle
        icon={ThresholdsGlyph}
        title={t_i18n("Module thresholds")}
        hint={t_i18n("The figures the OpenCTI and OpenAEV checks compare against. The next pass, within 5 minutes, reads them; no model call.")}
      />
      <div className="space-y-2">
        <div className="rounded-sm bg-elevation-default p-3.5" data-testid="settings.health.thresholds.opencti">
          <p className="mb-2 content-base font-medium">{t_i18n("OpenCTI")}</p>
          <div className="flex flex-wrap items-start gap-4">
            <NumberField
              label={t_i18n("Connector silent after")}
              info={t_i18n("An active connector that has not reported for this long raises a warning, e.g. 30.")}
              unit={t_i18n("minutes")}
              value={cti.connector_stale_minutes}
              {...OPENCTI_BOUNDS.connector_stale_minutes}
              editing={editing}
              onChange={setCti("connector_stale_minutes")}
            />
            <NumberField
              label={t_i18n("Connector errors above")}
              info={t_i18n("A connector with more works in error than this raises a warning, e.g. 10.")}
              unit={t_i18n("works")}
              value={cti.connector_errors_max}
              {...OPENCTI_BOUNDS.connector_errors_max}
              editing={editing}
              onChange={setCti("connector_errors_max")}
              display={exactCount}
            />
            <NumberField
              label={t_i18n("Queue depth above")}
              info={t_i18n("More messages waiting in the broker than this raises a warning, e.g. 100000.")}
              unit={t_i18n("messages")}
              value={cti.queue_depth_max}
              {...OPENCTI_BOUNDS.queue_depth_max}
              editing={editing}
              onChange={setCti("queue_depth_max")}
              display={exactCount}
            />
            <NumberField
              label={t_i18n("Search engine disk above")}
              info={t_i18n("A search engine disk used over this share raises a warning, e.g. 85.")}
              unit="%"
              value={cti.search_disk_max_percent}
              {...OPENCTI_BOUNDS.search_disk_max_percent}
              editing={editing}
              onChange={setCti("search_disk_max_percent")}
            />
            <NumberField
              label={t_i18n("Ingestion lag above")}
              info={t_i18n("Ingestion lagging by more than this raises a warning, e.g. 15.")}
              unit={t_i18n("minutes")}
              value={cti.ingestion_lag_minutes}
              {...OPENCTI_BOUNDS.ingestion_lag_minutes}
              editing={editing}
              onChange={setCti("ingestion_lag_minutes")}
            />
          </div>
        </div>
        <div className="rounded-sm bg-elevation-default p-3.5" data-testid="settings.health.thresholds.openaev">
          <p className="mb-2 content-base font-medium">{t_i18n("OpenAEV")}</p>
          <div className="flex flex-wrap items-start gap-4">
            <NumberField
              label={t_i18n("Integration silent after")}
              info={t_i18n("An injector, collector or executor that has not reported for this long raises a warning, e.g. 60.")}
              unit={t_i18n("minutes")}
              value={aev.integration_silent_minutes}
              {...OPENAEV_BOUNDS.integration_silent_minutes}
              editing={editing}
              onChange={setAev("integration_silent_minutes")}
            />
            <NumberField
              label={t_i18n("Agents offline above")}
              info={t_i18n("More agents offline than this share raises a warning, e.g. 50.")}
              unit="%"
              value={aev.agents_offline_max_percent}
              {...OPENAEV_BOUNDS.agents_offline_max_percent}
              editing={editing}
              onChange={setAev("agents_offline_max_percent")}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
