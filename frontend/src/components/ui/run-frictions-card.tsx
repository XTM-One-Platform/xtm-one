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
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useFormatter } from "@/i18n";
import type { RunFriction } from "@/types";

/**
 * The run page's Frictions card: the friction ledger of the run (#8528), what
 * it struggled with (a tool error, a retry, an approval pause, a sandbox or
 * runner failure...) and what carries each fix. The kind, the tool, the class
 * and the disposition are platform identifiers, shown as written. Rendered
 * only for a run that recorded at least one friction.
 */
export function RunFrictionsCard({
  frictions,
  undisposed,
}: {
  frictions: RunFriction[];
  undisposed: number | null | undefined;
}) {
  const { t_i18n } = useFormatter();
  const open = undisposed ?? frictions.filter((f) => !f.disposition).length;
  return (
    <Card data-testid="run-frictions-card">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="content-base flex items-center gap-2">
            <Icon name="circle-alert" size={16} className="text-default-secondary" />
            {t_i18n("Frictions ({count})", { count: frictions.length })}
          </CardTitle>
          {open > 0 && (
            <Badge variant="outline" data-testid="run-frictions-undisposed">
              {t_i18n("{count} undisposed", { count: open })}
            </Badge>
          )}
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n("What this run struggled with, and what carries each fix.")}
        </p>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-[var(--border-elevation-subtle)]">
          {frictions.map((f) => (
            <li key={f.id} className="py-2 space-y-1" data-testid="run-friction-row">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="font-mono-plex">{f.kind}</Badge>
                {f.tool && <span className="content-compact font-mono-plex text-default-primary">{f.tool}</span>}
                {f.count > 1 && (
                  <span className="content-compact text-default-secondary">
                    {t_i18n("Met {count} times", { count: f.count })}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-2">
                  {f.class && f.class !== "unclassified" && (
                    <Badge variant="outline" className="font-mono-plex">{f.class}</Badge>
                  )}
                  {f.disposition ? (
                    <span className="content-compact font-mono-plex text-default-secondary">
                      {f.disposition.ref ? `${f.disposition.kind}: ${f.disposition.ref}` : f.disposition.kind}
                    </span>
                  ) : (
                    <span className="content-compact text-default-secondary italic">{t_i18n("Undisposed")}</span>
                  )}
                </span>
              </div>
              {f.evidence && (
                <details>
                  <DisclosureSummary>{t_i18n("Evidence")}</DisclosureSummary>
                  <pre className="mt-1.5 content-compact text-default-secondary whitespace-pre-wrap bg-elevation-hover rounded-sm p-3">
                    {f.evidence}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
