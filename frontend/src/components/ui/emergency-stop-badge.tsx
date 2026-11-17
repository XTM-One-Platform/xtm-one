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
 * The one label for anything an emergency stop switched off.
 *
 * A paused assignment normally shows a grey "Paused"; one paused by the panic
 * button shows this instead, wherever the assignment appears (detail header,
 * agent overview list, status tile), so nobody has to open the audit trail to
 * know why an automation went quiet.
 */
import { OctagonX } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export function EmergencyStopBadge({ className, details }: { className?: string; details?: string | null }) {
  const { t_i18n } = useFormatter();
  const badge = (
    <Badge
      variant="outline"
      className={cn("gap-1 border-feedback-error-primary/50 bg-feedback-error-primary/15 text-feedback-error-primary font-semibold", className)}
      data-testid="emergency-stop-badge"
    >
      <OctagonX className="h-2.5 w-2.5" />
      {t_i18n("Emergency stop")}
    </Badge>
  );
  return details ? <Tip content={details}>{badge}</Tip> : badge;
}
