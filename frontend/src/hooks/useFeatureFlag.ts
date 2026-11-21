// SPDX-License-Identifier: Apache-2.0

/**
 * Read a platform feature flag (`ENABLED_FEATURES` on the backend).
 *
 * The resolved id list arrives with `GET /auth/config`, so this is one shared
 * query away — no extra request per call site.
 *
 * **This is presentation, never enforcement**, and how much the server backs it
 * up is per-flag — do not assume. `A2A_SERVER` is enforced independently (its
 * routers are not even mounted when it is off). `FLOW_BUILDER` is deliberately
 * UI-only: the `/api/v1/flows` routes still answer with it off, because the
 * agent-creation wizard reads the flow node catalog — only the UI, the OpenAPI
 * schema and the docs are gated (see `dev-docs/flow-builder-feature-flag.md`).
 * So hiding a control here is not an authorization decision. Anything that
 * must actually be refused needs a server-side gate of its own, whether or not
 * a flag hides its button.
 *
 * Fail-closed while the config is in flight: an unresolved flag reads as OFF,
 * which is right for a nav entry (it appears when the answer arrives) and
 * wrong for a route guard (it would bounce a hard refresh home before the
 * answer arrives) — that is what `useFeatureFlagResolved` is for.
 */
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { isFeatureEnabled, type FeatureFlag } from "@/lib/utils";

export function useFeatureFlag(flag: FeatureFlag): boolean {
  const { data } = useAuthConfig();
  return isFeatureEnabled(data?.enabled_features, flag);
}

/**
 * As `useFeatureFlag`, plus whether `/auth/config` actually answered.
 *
 * Three states, because a route guard has to tell them apart:
 *
 * - `isResolved` — the flag is known. Act on `enabled`.
 * - neither — still in flight. Wait; redirecting on `enabled === false` here
 *   turns every deep link into a bounce to the dashboard.
 * - `isError` — the config could not be fetched, so the flag is **unknown**.
 *
 * `isError` is deliberately *not* folded into `isResolved`. Treating an
 * unfetchable config as "resolved, and off" makes a guard redirect as though
 * the feature did not exist, which on a flag-ON instance hides a real outage
 * behind a plausible-looking bounce to the dashboard. What the error means is
 * the caller's call: a guard on a feature route surfaces it, a page whose other
 * tabs work regardless carries on with the feature hidden.
 *
 * Both flags are derived from **whether a config is in hand**, not from the
 * query's status. TanStack Query v5's `isError` is
 * `isLoadingError || isRefetchError`, so a *background* refetch failure flips
 * it to `true` while `data` stays cached and perfectly usable — and the query
 * has a 60s `staleTime` with `refetchOnWindowFocus` left on, so alt-tabbing
 * away and back is enough to trigger one. Reading `isError` raw meant a
 * transient 5xx during that refetch tore down whatever the flag was gating:
 * on `/flows/library/<id>` that unmounted the canvas and took unsaved graph
 * edits with it, over a flag that was known and enabled the whole time.
 *
 * So `isError` here means *only* "no config and the fetch failed", and
 * `isResolved` means "a config is in hand", cached or fresh.
 */
export function useFeatureFlagResolved(
  flag: FeatureFlag,
): { enabled: boolean; isResolved: boolean; isError: boolean } {
  const { data, isError } = useAuthConfig();
  const hasConfig = data !== undefined;
  return {
    enabled: isFeatureEnabled(data?.enabled_features, flag),
    isResolved: hasConfig,
    isError: isError && !hasConfig,
  };
}
