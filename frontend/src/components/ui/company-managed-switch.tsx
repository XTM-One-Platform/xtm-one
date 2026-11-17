// SPDX-License-Identifier: Apache-2.0

/**
 * CompanyManagedSwitch — the company-managed toggle row shared by every
 * entity settings surface (agent Configuration > General, EntityConfigCard,
 * the flow builder's Owner and access drawer), and by the admin switch rows
 * of the same card (fleet "Hide activity from non-admins", through `icon`).
 *
 * Presentational only: the caller gates it on `isAdmin` (turning an entity
 * company-managed is an admin action) and decides what a change does.
 */

import type { ComponentProps, ReactNode } from "react";
import { Icon } from "@filigran/design-system";
import { Switch } from "@/components/ui/switch";

export function CompanyManagedSwitch({
  label,
  description,
  checked,
  onCheckedChange,
  disabled = false,
  icon = "building-2",
}: {
  /** Translated title, e.g. "Company-managed agent". Also the switch's accessible name. */
  label: string;
  /** Translated helper text under the title. */
  description: ReactNode;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Design-system icon of the row; the company-managed building by default. */
  icon?: ComponentProps<typeof Icon>["name"];
}) {
  return (
    <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
      <div className="flex items-center gap-2">
        <span className="flex shrink-0" aria-hidden>
          <Icon name={icon} size={16} className="text-accent" />
        </span>
        <div>
          <p className="content-base font-medium">{label}</p>
          <p className="content-compact text-default-secondary">{description}</p>
        </div>
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-label={label}
      />
    </div>
  );
}
