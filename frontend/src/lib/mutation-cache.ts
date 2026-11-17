// SPDX-License-Identifier: Apache-2.0

import { MutationCache, type QueryClient } from "@tanstack/react-query";
import { globalToast } from "@/lib/global-toast";

/**
 * The app's MutationCache (`main.tsx`): what every write does after it settles.
 * A component test that checks a mutation's error toast mounts it too
 * (`testQueryClient({ appMutationCache: true })`), so a toast it adds shows up there.
 *
 * `client` is read when a write settles: the client is built with this cache.
 */
export function createAppMutationCache(client: () => QueryClient): MutationCache {
  return new MutationCache({
    // Who a lent resource reaches depends on nearly every entity (shares,
    // bindings, owners, groups, bots, settings): after any write, the mounted
    // "Sharing and access" cards and "Shared - N ways" badges read it again.
    onSuccess: (_data, _variables, _context, mutation) => {
      if ((mutation.options.meta as { readOnly?: boolean } | undefined)?.readOnly) return;
      void client().invalidateQueries({ queryKey: ["exposure"] });
    },
    onError: (error, _variables, _context, mutation) => {
      const suppressGlobalErrorToast =
        (mutation.options.meta as { suppressGlobalErrorToast?: boolean } | undefined)
          ?.suppressGlobalErrorToast === true;
      if (suppressGlobalErrorToast) return;

      /* An AI-disabled refusal is a platform state, not a fault. FR-009 adds
         ``detail.code`` precisely so a client can render the state instead of
         parsing prose, and this global handler is the last client that still
         showed the raw message — which is how a refusal surfaced as
         "Error [Error]" from a third-party panel rendering inside our tree. */
      const detail = (error as { detail?: { code?: string } } | null)?.detail;
      if (detail?.code === "ai_disabled") {
        /* A group's switch is worded by the server for the person; the
           platform's points the administrator at Settings → Platform. */
        const refusal = detail as { scope?: string; message?: string };
        globalToast({
          title: "AI is disabled",
          description:
            refusal.scope === "group" && refusal.message
              ? refusal.message
              : "AI is disabled on this platform. An administrator can enable it in Settings → Platform.",
          variant: "error",
        });
        return;
      }

      globalToast({
        title: "Error",
        description: error instanceof Error ? error.message : "An unexpected error occurred",
        variant: "error",
      });
    },
  });
}
