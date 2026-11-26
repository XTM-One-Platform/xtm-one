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

import { Zap } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import type { ProviderCatalogItem } from "@/types";

export function EventTriggersCard({ catalogEntry }: { catalogEntry: ProviderCatalogItem }) {
  const { t_i18n, t_text } = useFormatter();
  if (!catalogEntry.available_events || catalogEntry.available_events.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Zap className="h-4 w-4 text-accent" />
          {t_i18n("Event Triggers")}
          <Badge variant="cyan" className="content-caption ml-auto">{t_i18n("{count} events", { count: catalogEntry.available_events.length })}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {catalogEntry.available_events.map((event) => (
          <div key={event.event} className="rounded-sm bg-elevation-hover p-3 space-y-1.5">
            <div className="flex items-center gap-2">
              <Zap className="h-3 w-3 text-accent" />
              <span className="content-base font-medium">{t_text(event.label)}</span>
              <Badge variant="outline" className="text-[9px] ml-auto">
                {event.mechanism ?? "polling"}
              </Badge>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed pl-5">{t_text(event.description)}</p>
            <p className="content-caption text-default-secondary pl-5">
              {event.mechanism === "webhook"
                ? t_i18n("Triggered via webhook when the event occurs in the external service.")
                : event.mechanism === "hybrid"
                  ? t_i18n("Supports real-time webhook delivery with polling fallback.")
                  : event.mechanism === "stream"
                    ? t_i18n("Read in real time from an event stream the platform consumes.")
                    : t_i18n("Detected via periodic polling by the agent's assignment scheduler.")}
            </p>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
