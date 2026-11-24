// SPDX-License-Identifier: Apache-2.0

/**
 * Amber warning banner for company-managed entities in read-only mode.
 *
 * Usage:
 *   <CompanyManagedBanner entityLabel="integration" />
 *   <CompanyManagedBanner entityLabel="MCP server" />
 */

import { Lock, Package, ShieldCheck } from "lucide-react";
import { useFormatter } from "@/i18n";

interface CompanyManagedBannerProps {
  entityLabel: string;
  isBuiltin?: boolean;
  /** Installed by a skill package: the package, not this page, changes it. */
  isPackageOrigin?: boolean;
  /** One sentence naming what stays editable on a built-in entity (e.g. its ROI outcomes). */
  exception?: string;
  /**
   * A built-in whose customer layer stays editable (#914): the banner names what
   * is locked and what stays yours instead of saying it cannot be modified.
   */
  hasCustomerLayer?: boolean;
}

export function CompanyManagedBanner({ entityLabel, isBuiltin, isPackageOrigin, exception, hasCustomerLayer }: CompanyManagedBannerProps) {
  const { t_i18n } = useFormatter();
  if (isPackageOrigin) {
    return (
      <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2.5">
        <Package className="h-3.5 w-3.5 text-accent shrink-0 mt-0.5" />
        <p className="content-compact text-accent/90 leading-relaxed">
          {t_i18n("This {entity} was installed by a skill package and cannot be modified here. Update or remove the skill package to change it.", { entity: t_i18n(entityLabel) })}
        </p>
      </div>
    );
  }
  if (isBuiltin) {
    return (
      <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2.5">
        <ShieldCheck className="h-3.5 w-3.5 text-accent shrink-0 mt-0.5" />
        <p className="content-compact text-accent/90 leading-relaxed">
          {hasCustomerLayer
            ? t_i18n("This is a built-in {entity} pre-configured by the platform. Its persona, handovers and gates are locked; its organization notes and integrations stay yours.", { entity: t_i18n(entityLabel) })
            : t_i18n("This is a built-in {entity} pre-configured by the platform. It cannot be modified.", { entity: t_i18n(entityLabel) })}
          {exception && <> {exception}</>}
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
      <Lock className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
      <p className="content-compact text-feedback-warning-primary leading-relaxed">
        {t_i18n("This is a company-managed {entity}. Only admins can edit the configuration.", { entity: t_i18n(entityLabel) })}
      </p>
    </div>
  );
}
