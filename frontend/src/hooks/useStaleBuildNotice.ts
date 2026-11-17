// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from "react";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { isStaleBuild, readPageBuildId } from "@/lib/build-version";

/** The build this page booted: read once, it never changes without a reload. */
const PAGE_BUILD_ID = readPageBuildId();

/**
 * Offer a reload once the server serves another frontend build than the one
 * this tab runs.
 *
 * The server's build arrives with `/auth/config`, which is refetched when the
 * window regains focus - after an upgrade, the moment the user comes back to
 * the tab. The notice stays until it is acted on or closed, and is offered
 * once per server build.
 */
export function useStaleBuildNotice(): void {
  const serverBuildId = useAuthConfig().data?.build_id;
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const offeredFor = useRef<string | null>(null);

  useEffect(() => {
    if (!isStaleBuild(PAGE_BUILD_ID, serverBuildId) || offeredFor.current === serverBuildId) return;
    offeredFor.current = serverBuildId ?? null;
    toast({
      title: t_i18n("A new version is available"),
      description: t_i18n("This tab still runs the previous version. Reload it to use the new one."),
      variant: "info",
      durationMs: Number.POSITIVE_INFINITY,
      tag: "stale-build",
      action: { label: t_i18n("Reload"), onClick: () => window.location.reload() },
    });
  }, [serverBuildId, toast, t_i18n]);
}
