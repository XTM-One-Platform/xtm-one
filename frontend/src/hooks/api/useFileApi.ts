// SPDX-License-Identifier: Apache-2.0

/**
 * Files repository hooks (Work > Files) — `GET /files`, `DELETE /files/{id}`.
 *
 * The list is the cross-context view over every file the user can download:
 * conversation uploads and agent outputs, assignment-run artifacts, dataset and
 * tool-test inputs. Per-conversation listing stays in `useChatApi.ts`
 * (`useConversationFiles`) because it carries the admin break-glass gate this
 * cross-context list deliberately does not offer.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RepositoryFilesPage, RepositoryFilesQuery } from "@/types";

export const FILES_KEY = ["files"] as const;

function buildQuery(query: RepositoryFilesQuery): string {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  if (query.search) params.set("search", query.search);
  if (query.context) params.set("context", query.context);
  if (query.origin) params.set("origin", query.origin);
  if (query.vis) params.set("vis", query.vis);
  if (query.conversation_id) params.set("conversation_id", query.conversation_id);
  if (query.assignment_run_id) params.set("assignment_run_id", query.assignment_run_id);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** Newest-first page of files, `total` being the size of the filtered set. */
export function useFiles(query: RepositoryFilesQuery = {}, options: { enabled?: boolean } = {}) {
  return useQuery<RepositoryFilesPage>({
    queryKey: [...FILES_KEY, "list", query],
    queryFn: () => api.get(`/files${buildQuery(query)}`),
    enabled: options.enabled ?? true,
  });
}

/**
 * Delete one file (row + object). The server admits the file's owner, the
 * owner of its conversation, or whoever may access its run — the list marks
 * each row with `can_delete` so the UI never offers what the server refuses.
 */
export function useDeleteFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) => api.delete<void>(`/files/${fileId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: FILES_KEY });
      // The same rows back the per-conversation panel and the run detail's
      // attachments card.
      void queryClient.invalidateQueries({ queryKey: ["conversation-files"] });
    },
  });
}
