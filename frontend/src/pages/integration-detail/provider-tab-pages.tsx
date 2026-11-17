// SPDX-License-Identifier: Apache-2.0

/**
 * The pages of `provider-tabs.tsx`, each loaded on first open.
 *
 * The registry is read by the sidebar on every page, so it must not import a
 * tab's component: that would put every provider page in the entry bundle.
 */
import { lazy } from "react";

export const NovaRulesTab = lazy(() => import("./NovaRulesTab").then((m) => ({ default: m.NovaRulesTab })));
export const OpenAPISpecTab = lazy(() => import("./OpenAPISpecTab").then((m) => ({ default: m.OpenAPISpecTab })));
export const OpenRouterUsageTab = lazy(() =>
  import("./openrouter/OpenRouterUsageTab").then((m) => ({ default: m.OpenRouterUsageTab })),
);
export const OpenRouterModelsTab = lazy(() =>
  import("./openrouter/OpenRouterModelsTab").then((m) => ({ default: m.OpenRouterModelsTab })),
);
export const OpenRouterKeysTab = lazy(() =>
  import("./openrouter/OpenRouterKeysTab").then((m) => ({ default: m.OpenRouterKeysTab })),
);
export const OpenRouterPlatformProviderCard = lazy(() =>
  import("./openrouter/OpenRouterPlatformProviderCard").then((m) => ({ default: m.OpenRouterPlatformProviderCard })),
);
export const LMStudioModelsTab = lazy(() =>
  import("./lmstudio/LMStudioModelsTab").then((m) => ({ default: m.LMStudioModelsTab })),
);
export const LMStudioPlatformProviderCard = lazy(() =>
  import("./lmstudio/LMStudioPlatformProviderCard").then((m) => ({ default: m.LMStudioPlatformProviderCard })),
);
export const OpenCrqVersionCard = lazy(() =>
  import("./opencrq/OpenCrqVersionCard").then((m) => ({ default: m.OpenCrqVersionCard })),
);
export const SupabaseDatabaseAccessTab = lazy(() =>
  import("./supabase/SupabaseDatabaseAccessTab").then((m) => ({ default: m.SupabaseDatabaseAccessTab })),
);
