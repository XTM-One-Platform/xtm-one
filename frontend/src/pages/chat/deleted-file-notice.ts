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
