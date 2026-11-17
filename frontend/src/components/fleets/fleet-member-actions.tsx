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
 * The "Change parent" dialog the fleet graph's node menu and the members panel
 * share, so the two surfaces never drift. It saves through
 * `useReparentFleetMember`, the re-parent of the members panel's drag and of
 * the fleet graph's drag too. The remove action they share is
 * `useRemoveFleetMemberAction`.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import type { FleetMember } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { parentCandidates } from "./fleet-edition";
import { useReparentFleetMember } from "./useReparentFleetMember";

/**
 * Who a home member reports to: the parent picker, then the update. Opened
 * from the members panel's row menu and from the graph's node menu.
 */
export function ChangeParentDialog({
  fleetId,
  members,
  member,
  onClose,
}: {
  fleetId: string;
  members: readonly FleetMember[];
  /** The member whose parent changes; null keeps the dialog closed. */
  member: FleetMember | null;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const reparent = useReparentFleetMember(fleetId, members);
  const [parentId, setParentId] = useState<string | null>(null);
  const [openedFor, setOpenedFor] = useState<string | null>(null);

  // Start from the member's current parent each time the dialog opens on a member.
  if (member && openedFor !== member.id) {
    setOpenedFor(member.id);
    setParentId(member.parent_member_id);
  } else if (!member && openedFor !== null) {
    setOpenedFor(null);
  }

  const save = () => {
    if (member && member.parent_member_id !== parentId) reparent(member, parentId);
    onClose();
  };

  return (
    <Dialog open={!!member} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Change parent")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Who {name} reports to in this fleet. Its handovers do not change.", { name: member?.name ?? "" })}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="fleet-change-parent-field" className="text-default-secondary">
              {t_i18n("Parent")}
            </Label>
            <SearchableSelect
              id="fleet-change-parent-field"
              aria-label={t_i18n("Parent")}
              options={member ? parentCandidates(members, member.id).map((m) => ({ value: m.id, label: m.name })) : []}
              value={parentId}
              onValueChange={(value) => setParentId(value || null)}
              placeholder={t_i18n("Top level")}
              clearable
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              {t_i18n("Cancel")}
            </Button>
            <Button type="submit">{t_i18n("Save")}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
