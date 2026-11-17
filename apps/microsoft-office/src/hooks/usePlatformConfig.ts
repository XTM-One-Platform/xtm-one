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

import { useEffect, useState } from "react";
import { getPlatformConfig } from "@/lib/api";
import type { PlatformConfig } from "@/lib/types";

/**
 * Fetch the platform config from the configured backend on mount.
 *
 * Mirrors the main frontend's ``usePlatformName`` so the Office add-in
 * (which also renders product branding before the user has authenticated)
 * picks up the admin-editable ``PlatformSettings.platform_name`` value
 * exposed by ``/api/v1/platform/config`` instead of hardcoding the
 * deployment-mode default.
 *
 * Loading-state semantics (kept identical to the main frontend hook):
 *
 * - The default fallback is the empty string — explicitly brand-neutral
 *   so the hook never renders the wrong deployment mode's product name
 *   during the brief load window or permanently if the request fails.
 *   Copilot-mode and custom-branded deployments no longer see another
 *   brand flash on the Office add-in.
 * - Consumers that need a non-empty default during loading should pass
 *   an explicit ``fallback`` (e.g. ``usePlatformName(baseUrl, "Filigran")``).
 * - Refetches when ``baseUrl`` changes (the unauthenticated login screen
 *   lets users edit the instance URL before signing in). The cached
 *   config is cleared at the start of each refetch so the ``fallback``
 *   is rendered while the new instance's config is in flight — without
 *   this, the previous instance's brand would briefly leak through to
 *   the new URL (or stay forever if the new URL is slow / unreachable).
 *
 * Returns the resolved platform name (string).
 */
export function usePlatformName(
  baseUrl?: string,
  fallback: string = "",
): string {
  const [config, setConfig] = useState<PlatformConfig | null>(null);

  useEffect(() => {
    let cancelled = false;
    setConfig(null);
    getPlatformConfig(baseUrl)
      .then((cfg) => {
        if (!cancelled) setConfig(cfg);
      })
      .catch(() => {
        if (!cancelled) setConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl]);

  return config?.platform_name ?? fallback;
}
