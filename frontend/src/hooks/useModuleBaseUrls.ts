// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { moduleBaseUrlsOf, type ModuleBaseUrls } from "@/lib/module-pivot";

/**
 * The base URL of each connected module (OpenCTI, OpenAEV, OpenCRQ), read from
 * the platform registrations, for the "Open in <Module>" pivots (#1357). The
 * registrations are a light read shared with the sidebar and the case page;
 * never the CTEM posture, whose cold build took seconds on a case page (#1986).
 */
export function useModuleBaseUrls(): ModuleBaseUrls {
  const { data } = usePlatformRegistrations();
  return useMemo(() => moduleBaseUrlsOf(data), [data]);
}
