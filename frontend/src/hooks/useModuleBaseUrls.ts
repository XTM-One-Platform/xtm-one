// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { useCtemPosture } from "@/hooks/api/useCtemMetricsApi";
import { moduleBaseUrlsOf, type ModuleBaseUrls } from "@/lib/module-pivot";

/**
 * The base URL of each connected module (OpenCTI, OpenAEV, OpenCRQ), read from
 * the CTEM posture's `products`, for the "Open in <Module>" pivots (#1357).
 * The posture is cached and shared with the CTEM pages, so this adds no read.
 */
export function useModuleBaseUrls(): ModuleBaseUrls {
  const { data } = useCtemPosture();
  return useMemo(() => moduleBaseUrlsOf(data?.products), [data?.products]);
}
