// SPDX-License-Identifier: Apache-2.0

/**
 * OwnerCard — admin-only owner (`user_id`) transfer control for ownable entities.
 *
 * Lets an admin reassign an object (agent, integration, knowledge base, MCP
 * server, custom tool, prompt, skill) to another active user. Renders as an
 * embedded rounded section (drop it inside an existing Card's CardContent,
 * alongside the company-managed toggle / group sharing). Saves immediately on
 * change via the `onTransfer` callback — it does not participate in any
 * batched edit/save flow.
 *
 * The user-list endpoint is admin-only, so the control (and its data fetch)
 * are gated on `isAdmin`; non-admins render nothing.
 */

import { UserCog } from "lucide-react";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";

export function OwnerCard({
  userId,
  ownerName,
  onTransfer,
  disabled = false,
  entityLabel = "object",
}: {
  /** The entity's current owner user id. */
  userId: string | null;
  /** The current owner's display name (shown as the placeholder before the user list loads). */
  ownerName?: string | null;
  /** Called with the selected user id when the admin picks a new owner. */
  onTransfer: (userId: string) => void;
  /** Disable the picker while a save is in flight. */
  disabled?: boolean;
  /** Entity type label used in the helper text (e.g. "agent", "knowledge base"). */
  entityLabel?: string;
}) {
  const { isAdmin } = useAuth();
  const { t_i18n } = useFormatter();
  // The `/users` endpoint is admin-only — gate the fetch on isAdmin to avoid a
  // 403 that would surface through the query ErrorBoundary for non-admins.
  const { data: users } = useUsers(isAdmin);

  // Owner transfer is an admin-only operation; hide the control otherwise.
  if (!isAdmin) return null;

  const selectableUsers = (users ?? []).filter((u) => u.is_active);
  // Only bind the picker to ``userId`` when that owner is in the loaded, active
  // options. A value matching no option leaves the field blank - which is
  // exactly the state before the user list loads, or when the current owner is
  // deactivated and thus filtered out. Falling back to "" lets the
  // ``ownerName`` placeholder surface the current owner instead of a blank box.
  const ownerInList = !!userId && selectableUsers.some((u) => u.id === userId);

  return (
    <div className="rounded-sm bg-elevation-hover p-3 space-y-2.5">
      <div className="flex items-center gap-2">
        <UserCog className="h-4 w-4 text-accent" />
        <div>
          <p className="content-base font-medium">{t_i18n("Owner")}</p>
          <p className="text-[11px] text-default-secondary">
            {t_i18n("Transfer this {entity} to another user. Only admins can change the owner.", {
              entity: t_i18n(entityLabel),
            })}
          </p>
        </div>
      </div>
      <SearchableSelect
        aria-label={t_i18n("Owner")}
        options={selectableUsers.map((u) => userSelectOption(u))}
        value={ownerInList && userId ? userId : ""}
        onValueChange={(value) => {
          if (value && value !== userId) onTransfer(value);
        }}
        disabled={disabled}
        placeholder={ownerName ?? t_i18n("Select an owner")}
      />
    </div>
  );
}
