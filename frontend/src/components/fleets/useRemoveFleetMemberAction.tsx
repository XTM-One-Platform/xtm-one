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
 * Remove a fleet member after a confirmation: the one remove action of
 * the members panel and of the graph's node menu, so removing asks first
 * wherever it starts. Mount `confirmElement` once in the surface.
 */
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useRemoveFleetMember, type FleetMember } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { removeMemberCopy } from "./fleet-edition";
import { fleetRefusalMessage } from "./fleet-refusals";

export function useRemoveFleetMemberAction(fleetId: string): {
  confirmElement: React.ReactNode;
  removeMember: (member: FleetMember) => Promise<void>;
} {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [confirmElement, confirm] = useConfirm();
  const remove = useRemoveFleetMember(fleetId);

  const removeMember = async (member: FleetMember) => {
    const copy = removeMemberCopy(member);
    const description = copy.description ? t_i18n(copy.description) : undefined;
    const ok = await confirm({
      title: t_i18n(copy.title, copy.titleValues),
      description,
      confirmLabel: t_i18n("Remove"),
      variant: "danger",
    });
    if (!ok) return;
    remove.mutate(member.id, {
      onSuccess: () => toast({ title: t_i18n("Removed from the fleet"), description, variant: "success" }),
      onError: (err) =>
        toast({
          title: t_i18n("The member was not removed"),
          description: fleetRefusalMessage(err, t_i18n),
          variant: "error",
        }),
    });
  };

  return { confirmElement, removeMember };
}
