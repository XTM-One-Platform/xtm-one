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
 * "Improve with Concierge" (#8261): asks the Concierge to investigate one agent
 * or flow the viewer manages. Shown while the viewer may manage the target
 * (built in since #101 S1, no flag); disabled, with the reason, while agentic AI is
 * off; a refusal (another cycle open, outside the scope, budget reached) is
 * said as such.
 */
import { Compass } from "lucide-react";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useConciergeName, useRequestConciergeCycle } from "@/hooks/api/useConciergeApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { conciergeRefusalKey } from "@/lib/concierge";
import { Spinner } from "@filigran/design-system";

interface Props {
  targetType: "agent" | "flow";
  targetId: string;
  canManage: boolean;
  size?: "sm" | "default";
}

export function ImproveWithConciergeButton({ targetType, targetId, canManage, size = "sm" }: Props) {
  const { t_i18n } = useFormatter();
  const conciergeName = useConciergeName();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const request = useRequestConciergeCycle();
  if (!canManage) return null;

  const ask = () =>
    request.mutate(
      { target_type: targetType, target_id: targetId },
      {
        onSuccess: () =>
          toast({
            title: t_i18n("{name} is on it", { name: conciergeName }),
            description: t_i18n("Its findings and proposals will appear under Improvements."),
            variant: "success",
          }),
        onError: (error) =>
          toast({ title: t_i18n("Not started"), description: t_i18n(conciergeRefusalKey(error), { name: conciergeName }), variant: "error" }),
      },
    );

  return (
    <AiDisabledTip>
      <Button variant="outline" size={size} className="gap-1.5" onClick={ask} disabled={request.isPending || aiOff} aria-busy={request.isPending || undefined}>
        {request.isPending && <Spinner size="sm" tone="inherit" />}{!request.isPending && <Compass className="h-3.5 w-3.5" />}
        {t_i18n("Improve with {name}", { name: conciergeName })}
      </Button>
    </AiDisabledTip>
  );
}
