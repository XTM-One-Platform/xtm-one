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
 * What starts a fleet and the modules it covers, shared by the Fleets list
 * card and the fleet About page (#2194), so both say the same thing with the
 * same keys: the starts line ("Events + 3 schedules, next in 2 hours"), the
 * Platform Health settings link (#974) and the modules logo row ("+N" with
 * the full list on hover and focus).
 */
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Tip } from "@/components/ui/tooltip";
import type { FleetHealthStarts } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { moduleDisplayName, type FleetStarts } from "@/lib/fleet-card-facts";
import { cn, timeUntil } from "@/lib/utils";

/** The logos one line holds at the narrowest card (1024 px, two columns) before "+N". */
const LOGOS_SHOWN = 6;

/** "Events + 3 schedules", "2 schedules", "Events", or nothing starts it. */
function useStartsBase(starts: FleetStarts): string {
  const { t_i18n } = useFormatter();
  if (starts.events && starts.schedules > 0) {
    return t_i18n("Events + {count, plural, one {# schedule} other {# schedules}}", { count: starts.schedules });
  }
  if (starts.schedules > 0) return t_i18n("{count, plural, one {# schedule} other {# schedules}}", { count: starts.schedules });
  if (starts.events) return t_i18n("Events");
  return t_i18n("Nothing starts it yet");
}

/**
 * The starts line of a fleet: its counted starts, and for the Platform Health
 * fleet "off in Settings" when it is off, else its next start (UX r1 B2 on #980).
 */
export function useFleetStartsLine(starts: FleetStarts, healthStarts: FleetHealthStarts | null | undefined): string {
  const { t_i18n, locale } = useFormatter();
  const base = useStartsBase(starts);
  if (healthStarts && healthStarts.active === false) return t_i18n("{starts}, off in Settings", { starts: base });
  if (healthStarts?.next_run_at) {
    return t_i18n("{starts}, next {time}", { starts: base, time: timeUntil(healthStarts.next_run_at, locale) });
  }
  return base;
}

/** Where the Platform Health fleet's schedules change: Settings > Platform Health, the one place (#974). */
export function FleetHealthSettingsLink({ healthStarts, testId, className }: { healthStarts: FleetHealthStarts; testId: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Link
      to={healthStarts.settings_route}
      className={cn(
        "shrink-0 rounded-sm text-accent underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        className,
      )}
      data-testid={testId}
    >
      {t_i18n("Platform Health settings")}
    </Link>
  );
}

/** The modules a fleet covers as one line of logos, "+N" past what fits, every name on hover and focus. */
export function FleetModuleLogos({ modules, testId }: { modules: readonly string[]; testId: string }) {
  const { t_i18n } = useFormatter();
  const names = modules.map((m) => moduleDisplayName(m));
  const shown = modules.slice(0, modules.length > LOGOS_SHOWN ? LOGOS_SHOWN - 1 : LOGOS_SHOWN);
  const hiddenNames = names.slice(shown.length);
  return (
    <>
      {shown.map((provider, index) => (
        <Tip key={provider} content={names[index]}>
          <span className="flex h-6 w-6 shrink-0 items-center justify-center">
            <ProviderLogo provider={provider} className="h-5 w-5" decorative />
          </span>
        </Tip>
      ))}
      {hiddenNames.length > 0 && (
        <Tip content={hiddenNames.join(", ")}>
          {/* Focusable: the hidden names open on focus and are read by its label (UX r1 B4 on #10010). */}
          <span
            tabIndex={0}
            role="img"
            aria-label={t_i18n("{count} more modules: {names}", { count: hiddenNames.length, names: hiddenNames.join(", ") })}
            className="shrink-0 rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            data-testid={`${testId}-more`}
          >
            <Badge variant="outline" className="tabular-nums" aria-hidden>
              {`+${hiddenNames.length}`}
            </Badge>
          </span>
        </Tip>
      )}
    </>
  );
}
