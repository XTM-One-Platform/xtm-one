// SPDX-License-Identifier: Apache-2.0

/**
 * The sentence a deleted file reads as (#396): when and why its content went.
 * The same wording the backend gives the agent (`file_retention.deleted_notice`).
 * Shared by the chat file card, the conversation files panel and the Files page;
 * kept out of the component files so they export components only.
 */
import { useCallback } from "react";

import { useFormatter } from "@/i18n";

export function useDeletedFileNotice() {
  const { t_i18n, intl } = useFormatter();
  return useCallback(
    (deletedAt: string, reason: string | null | undefined) => {
      const date = intl.formatDate(new Date(deletedAt), { dateStyle: "medium" });
      if (reason === "owner_deleted") return t_i18n("Deleted on {date} with what it belonged to", { date });
      if (reason === "admin") return t_i18n("Deleted on {date} by an administrator", { date });
      return t_i18n("Deleted on {date} by the retention policy", { date });
    },
    [intl, t_i18n],
  );
}
