// SPDX-License-Identifier: Apache-2.0

import { AuditLogList } from "@/components/audit-logs/AuditLogList";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useObservabilityConfig } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";

/**
 * The platform audit trail (admin). The list is the shared audit log table on the
 * DataTable (#9378 slice 6y): search, Action, Entity, User and Time range pills,
 * server sort, the total and one pager, the state in the URL keys the page always had.
 */
export default function AuditLogs() {
  const { t_i18n } = useFormatter();
  const { data: observability } = useObservabilityConfig();

  return (
    <div className="space-y-6">
      {/* One header row (#10215): the page's help and its retention rule are the title's "i". */}
      <ListPageHeader
        title={t_i18n("Audit logs")}
        infoLabel={t_i18n("About audit logs")}
        info={
          <span className="space-y-1">
            <span className="block">
              {t_i18n("Track all user actions across the platform: creation, updates, deletions, configuration changes, and more.")}
            </span>
            {/* A space between the two sentences, so the tip's text reads as two sentences to a screen reader (UX r1 N1). */}
            {observability ? " " : null}
            {observability ? (
              <span className="block" data-testid="audit-logs-retention-hint">
                {t_i18n("Entries older than {count, plural, one {# day} other {# days}} are deleted automatically.", {
                  count: observability.audit_log_retention_days,
                })}
              </span>
            ) : null}
          </span>
        }
      />
      <AuditLogList prefix="" storageKey="admin-audit-logs" emptyLabel={t_i18n("No audit logs yet.")} />
    </div>
  );
}
