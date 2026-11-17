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
 * The one re-parent of the fleet page: the members panel's drag, the
 * change-parent dialog (`ChangeParentDialog`) and a drop on the fleet graph
 * all go through it, so the rule and the toasts never drift.
 */
import { useToast } from "@/components/ui/toast";
import { useUpdateFleetMember, type FleetMember } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { canReparent } from "./fleet-edition";
import { fleetRefusalMessage } from "./fleet-refusals";

/**
 * Make `parentId` (a member id, null for the top level) the parent of a home
 * member: checked with `canReparent` (a refused move writes nothing), then one
 * `PATCH`, its toast naming who the member now reports to. Resolves whether
 * the move was saved (false when refused or when the `PATCH` fails), so the
 * fleet graph rings the new parent only after a save.
 */
export function useReparentFleetMember(
  fleetId: string,
  members: readonly FleetMember[],
): (member: FleetMember, parentId: string | null) => Promise<boolean> {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateMember = useUpdateFleetMember(fleetId);
  return (member, parentId) => new Promise<boolean>((resolve) => {
    if (!canReparent(members, member.id, parentId)) {
      resolve(false);
      return;
    }
    const parentName = parentId ? members.find((m) => m.id === parentId)?.name ?? "" : "";
    updateMember.mutate(
      { memberId: member.id, patch: { parent_member_id: parentId } },
      {
        onSuccess: () => {
          resolve(true);
          toast({
            title: t_i18n("Hierarchy updated"),
            description: parentId
              ? t_i18n("{name} now reports to {parent}.", { name: member.name, parent: parentName })
              : t_i18n("{name} is now at the top level.", { name: member.name }),
            variant: "success",
          });
        },
        onError: (err) => {
          resolve(false);
          toast({
            title: t_i18n("The hierarchy was not updated"),
            description: fleetRefusalMessage(err, t_i18n),
            variant: "error",
          });
        },
      },
    );
  });
}
