// SPDX-License-Identifier: Apache-2.0

import { useUsers } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";

/**
 * The name a background pass reads a module as (#2739): the user's name, else the email; `owner` adds
 * "(integration owner)" when the pass falls back to the integration's owner. `ownerName` is the integration's
 * own `owner_name`, read first so a reader without the users list still sees who.
 */
export function useRunAsUserName(userId: string | null, { owner = false, ownerName }: { owner?: boolean; ownerName?: string | null } = {}): string {
  const { t_i18n } = useFormatter();
  const { data: users } = useUsers(Boolean(userId) && !(owner && ownerName));
  if (!userId) return t_i18n("Not set");
  const user = users?.find((u) => u.id === userId);
  const name = (owner && ownerName?.trim()) || user?.full_name?.trim() || user?.email || t_i18n("A specific user");
  return owner ? t_i18n("{name} (integration owner)", { name }) : name;
}
