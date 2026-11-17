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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import { runErrorText } from "@/lib/run-error-text";

/**
 * The run page's reason card for a run someone cancelled. A cancel is a deliberate act, not a failure: the card reads
 * neutral, like the interrupted run's card beside it, with the reason
 * in the app language ("Cancelled by <actor>" from a bulk cancel). Every other
 * error keeps the red Error card.
 */
export function RunCancelledCard({ run }: { run: { status?: string | null; outcome?: string | null; error: string | null } }) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="border-elevation-subtle" data-testid="run-cancelled-card" data-tone="neutral">
      <CardHeader>
        <CardTitle className="content-base flex items-center gap-2 text-default-primary">
          <Icon name="ban" size={16} className="text-default-secondary" /> {t_i18n("Cancelled")}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="content-compact text-default-secondary whitespace-pre-wrap">{runErrorText(run, t_i18n)}</p>
      </CardContent>
    </Card>
  );
}
