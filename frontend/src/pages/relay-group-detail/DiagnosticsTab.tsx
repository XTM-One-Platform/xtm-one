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
import { AlertCircle, CheckCircle2, Stethoscope, Terminal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useGroupRelays, useTestRelayDestination } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { RelayGroup } from "@/types";
import { CopyBlock } from "./EnrollDialog";
import { relayErrorLabel } from "./relay-labels";
import { Spinner } from "@filigran/design-system";

const ANY = "__any__";

export function DiagnosticsTab({ group, canManage }: { group: RelayGroup; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { data: relays } = useGroupRelays(group.id);
  const test = useTestRelayDestination(group.id);
  const [url, setUrl] = useState("");
  const [relayId, setRelayId] = useState(ANY);
  const online = (relays ?? []).filter((r) => r.online);
  const result = test.data;

  const run = () => {
    if (!url.trim()) return;
    test.mutate({ url: url.trim(), relay_id: relayId === ANY ? undefined : relayId });
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 content-base">
            <Stethoscope className="h-4 w-4 text-accent" /> {t_i18n("Test a destination")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="content-compact text-default-secondary">
            {t_i18n("Sends one GET from the customer's network through a relay of this group, with the same checks as a routed integration. Nothing of the response is kept.")}
          </p>
          <Input aria-label={t_i18n("Test a destination")}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            placeholder="https://splunk.corp.local:8089/services/server/info"
            className="font-mono-plex content-base"
            disabled={!canManage}
          />
          <div className="flex flex-wrap items-center gap-2">
            <SearchableSelect
              aria-label={t_i18n("Relay")}
              className="w-60"
              options={online.map((r) => ({ value: r.id, label: r.name }))}
              pinned={[{ value: ANY, label: t_i18n("Any online relay") }]}
              value={relayId}
              onValueChange={setRelayId}
              disabled={!canManage}
            />
            <Button onClick={run} disabled={!canManage || !url.trim() || test.isPending} aria-busy={test.isPending || undefined}>
              {test.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Run the test")}
            </Button>
          </div>
          {!canManage && (
            <p className="text-[11px] text-default-secondary">{t_i18n("Only someone who manages this group can test from its network.")}</p>
          )}
          {result && (
            <div
              className={cn(
                "rounded-sm p-3 content-compact",
                result.ok ? "bg-feedback-success-primary/10 text-feedback-success-primary" : "bg-feedback-error-primary/10 text-feedback-error-primary",
              )}
            >
              <p className="flex items-center gap-1.5 font-medium">
                {result.ok ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                {result.ok
                  ? t_i18n("Reached: HTTP {status} in {ms} ms", { status: result.status_code ?? "-", ms: result.duration_ms ?? "-" })
                  : t_i18n("Not reached: {code}", { code: result.error_code ?? "error" })}
              </p>
              {!result.ok && (
                <p className="mt-1 text-[11px] opacity-90">
                  {relayErrorLabel(result.error_code) ? t_i18n(relayErrorLabel(result.error_code)!) : result.message}
                </p>
              )}
              {result.ok && (result.status_code ?? 0) >= 400 && (
                <p className="mt-1 text-[11px] opacity-90">
                  {t_i18n("The relay reached the service, which answered with an error: the network path works.")}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 content-base">
            <Terminal className="h-4 w-4 text-accent" /> {t_i18n("On the relay's machine")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 content-compact">
          <p className="text-default-secondary">{t_i18n("Whether it is enrolled and connected (exit code 0 when online):")}</p>
          <CopyBlock text="xtm-one-relay status" />
          <p className="text-default-secondary">{t_i18n("The configuration it runs with and its effective allow-list:")}</p>
          <CopyBlock text="xtm-one-relay check-config" />
          <p className="text-default-secondary">{t_i18n("The same request from the machine itself, through the relay's own checks:")}</p>
          <CopyBlock text="xtm-one-relay test-destination https://splunk.corp.local:8089/" />
          <p className="text-default-secondary">
            {t_i18n("Its configuration lives in relay.toml: /etc/xtm-one-relay on Linux, /Library/Application Support/XTM One Relay on macOS, %ProgramData%\\XTM One Relay on Windows. It reloads the file when it changes.")}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
