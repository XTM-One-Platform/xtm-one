// SPDX-License-Identifier: Apache-2.0

/**
 * The "Runs as" row of the OpenCRQ evidence health pass in a path's sources (#2739), the same `RunAsRow` as the
 * sources XTM One provisions (`PlatformSourceControls`), placed in the pass's grid under Runs on and Schedule (UX
 * round 4 on #2743, B1): who the pass reads OpenCRQ as, the integration's owner
 * until an administrator picks a user. Change opens the same "Act in OpenCRQ as" field, administrators only, and
 * saves `evidence_health.run_as_user_id` through the path panel's profile save.
 */
import { useState } from "react";
import { ModuleRunAsField } from "@/components/module-run-as/ModuleRunAsField";
import { RunAsRow } from "@/components/module-run-as/RunAsRow";
import { useRunAsUserName } from "@/components/module-run-as/useRunAsUserName";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { type EvidenceHealthPass, evidenceHealthRunAs, evidenceHealthSavedRunAs } from "@/lib/organization-profile";
import type { Integration } from "@/types";

export function EvidenceHealthRunAsRow({
  pass,
  integration,
  isAdmin,
  saving,
  onSave,
}: {
  pass: EvidenceHealthPass;
  /** The OpenCRQ integration the pass reads, when there is one. */
  integration: Integration | undefined;
  isAdmin: boolean;
  saving: boolean;
  onSave: (next: Partial<OrganizationProfile>, done: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const runAs = evidenceHealthRunAs(pass, integration);
  const name = useRunAsUserName(runAs.userId, { owner: runAs.owner, ownerName: integration?.owner_name });
  const [picked, setPicked] = useState<string | null>(null);

  const save = () => {
    if (picked === null) return;
    onSave(
      { evidence_health: { ...pass, run_as_user_id: evidenceHealthSavedRunAs(picked, pass.run_as_user_id, integration?.user_id ?? null) } },
      t_i18n("Scheduled pass saved"),
    );
    setPicked(null);
  };

  return (
    <>
      {/* The row of the provisioned sources (UX round 4 on #2743, B1): one component, its Change named. */}
      <RunAsRow
        name={name}
        changeLabel={t_i18n("Change the run-as user of {source}", { source: t_i18n("Evidence health") })}
        onChange={isAdmin && integration ? () => setPicked(runAs.userId ?? "") : undefined}
        testId="ctem-path-source-evidence-health-run-as"
      />
      <Dialog open={picked !== null} onOpenChange={(open) => !open && setPicked(null)}>
        <DialogContent size="md" data-testid="ctem-path-source-run-as-dialog">
          <DialogHeader>
            <DialogTitle>{t_i18n("Change the run-as user of {source}", { source: t_i18n("Evidence health") })}</DialogTitle>
            <DialogDescription>{t_i18n("The user the pass reads OpenCRQ as. Default: the integration's owner.")}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <ModuleRunAsField module="opencrq" value={picked ?? ""} onChange={(id) => setPicked(id)} active={picked !== null} />
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPicked(null)} disabled={saving}>
              {t_i18n("Cancel")}
            </Button>
            <Button onClick={save} disabled={saving || !picked || picked === (runAs.userId ?? "")} data-testid="ctem-path-source-run-as-save">
              {t_i18n("Save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
