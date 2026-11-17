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
 * A fleet module's mark: the provider logo for OpenCTI / OpenAEV / OpenCRQ and
 * for every integration provider (Outlook, Teams, GitHub, Notion..., #9979), an
 * icon for the platform's own modules. Shared by the About page (flow, gates,
 * catalog) and the autonomy raise dialog, so one module always wears one mark.
 */
import { CalendarClock, ClipboardList, Gauge, Hand, Webhook } from "lucide-react";

import { ProviderLogo } from "@/components/ui/provider-logo";
import type { FleetDocModule } from "@/hooks/api/useFleetApi";
import { cn } from "@/lib/utils";

import { ICON_MODULES } from "./fleet-about-showcase";

const ICONS = { schedule: CalendarClock, work_item: ClipboardList, webhook: Webhook, manual: Hand } as const;

export function FleetModuleMark({ module, className = "h-5 w-5" }: { module: FleetDocModule; className?: string }) {
  const key = (module || "platform").toLowerCase();
  if (!ICON_MODULES.has(key)) return <ProviderLogo provider={key} decorative className={className} />;
  const Icon = ICONS[key as keyof typeof ICONS] ?? Gauge;
  return <Icon className={cn(className, "shrink-0 text-default-secondary")} aria-hidden />;
}

/**
 * A gate's mark (UX round 1 B1 on #10034): the integration's logo for a gate
 * proposed from an integration's write tools, the module's mark otherwise.
 */
export function FleetGateMark({
  gate,
  className = "h-4 w-4",
}: {
  gate: { module: FleetDocModule; provider?: string };
  className?: string;
}) {
  if (gate.provider) return <ProviderLogo provider={gate.provider} decorative className={cn(className, "shrink-0")} />;
  return <FleetModuleMark module={gate.module} className={className} />;
}
