// SPDX-License-Identifier: Apache-2.0

/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE } from "@/lib/api";
import { withBasePath } from "@/lib/base-path";
import { buildCssVarsBlock } from "@/lib/color-utils";
import { useColorMode } from "@/hooks/useColorMode";
import { useAiDisabledGroup } from "@/hooks/useAiDisabledGroup";

interface PlatformFeatures {
  ai_adoption: boolean;
  all_integrations: boolean;
  all_templates: boolean;
  has_ee_platform: boolean;
  /** Number of active platform registrations (any contract). Lets the
   *  CE-only splash distinguish "no platforms yet" from "platforms
   *  connected but only CE" without re-fetching the registrations API. */
  active_registration_count: number;
}

/** Connectivity of one product (OpenCTI / OpenAEV) with XTM One.
 *
 *  * ``connected``      — registered, active, recent heartbeat
 *  * ``disconnected``   — registered but deactivated or heartbeat stale
 *  * ``not_registered`` — never registered
 */
type PlatformLinkState = "connected" | "disconnected" | "not_registered";

interface PlatformLinkStatus {
  status: PlatformLinkState;
  connected: boolean;
  /** When connectivity was last verified (ISO 8601, UTC) — the product's
   *  last heartbeat. `null` when it has never registered. */
  last_checked_at: string | null;
}

/** OpenCTI ↔ OpenAEV integration state, as of the last verification.
 *  Shipped in the (unauthenticated) platform config so the link can be
 *  checked without a session — see ``integration_status`` in
 *  ``backend/app/api/v1/platform.py``. */
interface PlatformIntegrationStatus {
  opencti: PlatformLinkStatus;
  openaev: PlatformLinkStatus;
  /** True only when BOTH products are currently connected. */
  linked: boolean;
  /** Freshest of the two verification timestamps, or `null`. */
  last_checked_at: string | null;
}

/** XTM One deployment edition — the ONE licence answer, mirrored from
 *  ``EDITION_*`` in ``backend/app/api/v1/platform.py``.
 *
 *  * ``enterprise`` — a valid Filigran license OR a connected Enterprise Edition
 *    platform. Everything is on; ``agentic_source`` says where the acts
 *    ceilings come from.
 *  * ``community``  — no Filigran license and no EE platform. AI is off.
 *  * ``""``         — Copilot mode (uses ``license_required`` instead)
 *
 *  There is deliberately no third value: the former three-tier model
 *  (Community / Starter Pack / Enterprise) is retired — a license carries a
 *  quota, not a feature switch.
 */
export type Edition = "enterprise" | "community" | "";

/** Badge label + Tailwind classes per edition — shared by every surface
 *  that renders the edition badge (Parameters license card, Quotas license
 *  banner, CTEM command center) so copy and colors never drift. */
export const EDITION_BADGES: Record<Exclude<Edition, "">, { label: string; className: string }> = {
  enterprise: {
    label: "Enterprise Edition",
    className: "bg-feedback-alert-primary/20 text-feedback-warning-tertiary border-feedback-alert-primary/40",
  },
  community: {
    label: "Community Edition",
    className: "bg-accent/15 text-accent border-accent/30",
  },
};

/** Platform-wide agentic ceilings per period: `null` is uncapped, a number is
 *  the ceiling, and `0` means no allowance at all (Community Edition). */
export interface AgenticQuotas {
  daily: number | null;
  monthly: number | null;
  yearly: number | null;
}

interface SubLicenses {
  opencti_ids: string[];
  openaev_ids: string[];
  opencrq_ids: string[];
}

export interface XTMLicense {
  valid: boolean;
  customer: string | null;
  license_type: string | null;
  expiration: string | null;
  expired: boolean;
  expiration_prevention: boolean;
  extra_expiration: boolean;
  extra_expiration_days: number;
  /** The acts ceilings the certificate carries — what a license DECIDES,
   *  with the module sub-licenses below. The legacy capability flags are
   *  no longer on the wire: nothing gates on them. */
  agentic_quotas: AgenticQuotas;
  license_source: "env_var" | "database" | "none";
  sub_licenses: SubLicenses;
}

export interface PlatformConfig {
  /** Anonymous per-install platform ID (the ``platform_settings`` row UUID).
   *  This is the ID required when generating a Filigran license certificate. */
  instance_id: string;
  platform_mode: string;
  platform_name: string;
  /** Branding logo — binary asset endpoint path, bundled static asset path,
   *  or legacy http(s) URL fallback. Never a raw base64 data URL; pair with
   *  `platform_logo_hash` as a `?v=` cache-buster when set. */
  platform_logo: string | null;
  /** Content hash for an uploaded logo, or `null` when unset/not an upload. */
  platform_logo_hash: string | null;
  /** The bundled logo's light-theme variant (dark wordmark); `null` once an
   *  operator sets a logo of their own. Absent from an older backend. */
  platform_logo_light?: string | null;
  /** The product mark alone, for the collapsed navigation rail: the bundled
   *  emblem, or beside an operator's own logo their uploaded favicon (cache-
   *  busted by `platform_emblem_hash`); `null` when they uploaded none. */
  platform_emblem?: string | null;
  platform_emblem_hash?: string | null;
  /** The bundled emblem's light-theme variant; `null` once an operator sets a
   *  logo of their own, whose mark is drawn in both themes. */
  platform_emblem_light?: string | null;
  platform_description: string;
  /** Markdown login-screen subtitle. Always carries a value (admin override
   *  or mode default), resolved server-side. Rendered as Markdown on login. */
  platform_login_message: string;
  /** Platform-wide UI language: "auto" (browser detection) or a locale code
   *  (e.g. "fr-fr"). Users can override it in their profile. */
  platform_language: string;
  platform_theme_color: string;
  platform_accent_color: string | null;
  platform_background_color: string | null;
  platform_default_accent_color: string;
  platform_default_background_color: string;
  platform_favicon: string | null;
  /** Content hash for an uploaded favicon, or `null` when unset. */
  platform_favicon_hash: string | null;
  platform_og_image: string;
  /** Content hash for an uploaded OG image, or `null` when unset/not an upload. */
  platform_og_image_hash: string | null;
  platform_version: string;
  license_required: boolean;
  license_valid: boolean;
  license_customer: string | null;
  license_type: string | null;
  license_expiration: string | null;
  features: PlatformFeatures;
  /** OpenCTI / OpenAEV connectivity + when it was last verified. Always
   *  present — both products default to `not_registered`. */
  integration_status: PlatformIntegrationStatus;
  xtm_license: XTMLicense | null;
  /** Whether AI is LICENSED here — the one licence answer every surface
   *  gates on (creation flows, composer, nav locks, upsell dialogs). `true`
   *  on Enterprise Edition (XTM One: a valid Filigran license OR a connected EE
   *  platform; Copilot: a valid license), `false` on Community Edition.
   *  Read it through `useAgenticAIEnabled()`. */
  agentic_enabled: boolean;
  /** The ceilings in force and where they come from: `xtm_license`,
   *  `ee_platform` (the included allowance — Enterprise Edition inherited
   *  from a connected EE platform, no Filigran license yet) or `none`. */
  agentic_quotas: AgenticQuotas;
  agentic_source: string;
  /** XTM One deployment edition — see ``Edition`` docs. */
  edition: Edition;
  /** Whether chat APIs and UI are available. Mirrors the backend
   *  ``is_chat_enabled`` resolution: `agentic_enabled` AND `ai_enabled`. */
  chat_enabled: boolean;
  disable_code_interpreter: boolean;
  disable_custom_tools: boolean;
  disable_custom_variables: boolean;
  ai_catalog_enabled: boolean;
  /** Whether AI is switched on here. Independent of
   *  `agentic_enabled`, which answers whether the licence permits AI —
   *  `ai_enabled: false` with `agentic_enabled: true` is a licensed
   *  platform deliberately running without AI. Absent on older backends,
   *  so read it through `useAiEnabled()`, which defaults it to enabled. */
  ai_enabled?: boolean;
}

const PlatformContext = createContext<PlatformConfig | null>(null);

/** How often the platform config is asked again while the app has none yet (ms). */
const PLATFORM_CONFIG_BOOT_POLL_MS = 3_000;

async function fetchPlatformConfig(): Promise<PlatformConfig> {
  const res = await fetch(`${API_BASE}/platform/config`);
  if (!res.ok) throw new Error("Failed to fetch platform config");
  return res.json();
}

/**
 * A branding asset URL ready for `src` / `href`: under the URL path prefix the
 * platform is served from (the payload's paths are root-relative, an admin's
 * legacy absolute URL is left alone) and with a `?v=<hash>` cache-buster when
 * the asset has one.
 */
function withVersion(url: string, hash: string | null): string {
  const resolved = withBasePath(url);
  if (!hash) return resolved;
  return `${resolved}${resolved.includes("?") ? "&" : "?"}v=${hash}`;
}

function replaceHeadLink(rel: string, attrs: Record<string, string>) {
  document.head
    .querySelectorAll<HTMLLinkElement>(`link[rel="${rel}"]`)
    .forEach((el) => el.remove());

  const el = document.createElement("link");
  el.setAttribute("rel", rel);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  document.head.appendChild(el);
}

export function PlatformProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery<PlatformConfig>({
    queryKey: ["platform-config"],
    queryFn: fetchPlatformConfig,
    /* 30s, not 5min. This payload carries `ai_enabled`, which drives the nav
       locks, the header badge and the landing screen, and the AI toggle is
       expected to take effect "within a few seconds" (FR-003a). At five minutes
       a tab that did not make the change — a non-administrator's, say — kept
       showing the pre-toggle platform long after it had switched. The endpoint
       is unauthenticated and already hit on every page load, so the extra
       traffic is a refetch on mount or focus rather than a new cost. */
    staleTime: 30 * 1000,
    retry: 3,
    /* The app waits on this payload behind its boot spinner (`App.tsx`): a page
       opened or reloaded while the backend is down (a restart, a deploy) asks
       again every few seconds until it answers, then boots by itself (#10910). */
    refetchInterval: (query) => (query.state.data === undefined ? PLATFORM_CONFIG_BOOT_POLL_MS : false),
    throwOnError: false,
  });

  // Only <html data-theme> cannot be expressed declaratively — React cannot
  // target the root <html> element from within the component tree.
  useEffect(() => {
    if (isXtmOneMode(data?.platform_mode)) {
      document.documentElement.dataset.theme = "xtm-one";
    } else {
      delete document.documentElement.dataset.theme;
    }
  }, [data?.platform_mode]);

  useEffect(() => {
    if (!data) return;

    const isXtmOne = isXtmOneMode(data.platform_mode);
    const faviconHref = data.platform_favicon
      ? withVersion(data.platform_favicon, data.platform_favicon_hash)
      : withBasePath(isXtmOne ? "/xtp-one-icon.svg" : "/favicon.ico");
    const appleTouchHref = data.platform_favicon
      ? withVersion(data.platform_favicon, data.platform_favicon_hash)
      : withBasePath(isXtmOne ? "/xtp-one-icon.svg" : "/apple-touch-icon.png");

    replaceHeadLink("icon", { href: faviconHref, sizes: "any" });
    replaceHeadLink("apple-touch-icon", { href: appleTouchHref, sizes: "180x180" });
  }, [data]);

  // Build :root CSS variable overrides from branding config.
  // <style precedence> is hoisted to <head> by React 19 after the bundled sheets.
  const cssVarsBlock = buildCssVarsBlock(
    data?.platform_accent_color,
    data?.platform_background_color,
  );

  return (
    <PlatformContext.Provider value={data ?? null}>
      {data && (
        <>
          <title>{data.platform_name}</title>
          <meta property="og:title" content={data.platform_name} />
          <meta name="description" content={data.platform_description} />
          <meta property="og:description" content={data.platform_description} />
          <meta name="theme-color" content={data.platform_theme_color} />
          <meta property="og:image" content={withVersion(data.platform_og_image, data.platform_og_image_hash)} />
          <link rel="manifest" href={`${API_BASE}/platform/manifest.webmanifest`} />
          {cssVarsBlock && (
            <style precedence="override">{cssVarsBlock}</style>
          )}
        </>
      )}
      {children}
    </PlatformContext.Provider>
  );
}

export function usePlatformConfig(): PlatformConfig | null {
  return useContext(PlatformContext);
}

/**
 * Resolved platform name from `PlatformConfig`.
 *
 * Returns the loaded ``platform_name`` (which already prefers the
 * admin-editable ``PlatformSettings.platform_name`` over the mode default
 * on the backend), or the caller-supplied ``fallback`` while the config
 * request is still in flight (or if it has failed permanently).
 *
 * The default fallback is the empty string — explicitly brand-neutral so
 * the hook never renders the wrong deployment mode's product name during
 * the brief load window. Consumers that need a non-empty default during
 * loading (e.g. ``Sidebar``'s "is an agentic assistant" copy) should pass
 * an explicit ``fallback``; consumers that interpolate the name into a
 * sentence ("Powered by …") generally render the empty space briefly and
 * then settle on the real name once the config resolves.
 */
/** Whether this is an XTM One *product*, AI on or off.
 *
 * Mirrors the backend's ``is_xtm_one`` (`core/config.py`), which counts
 * `ai_disabled` as XTM One with AI switched off. Comparing the mode to
 * `"xtm_one"` directly reads the enum as a closed set of two, and an AI-disabled
 * deployment then renders as Copilot — wrong branding, wrong dashboard, and
 * Copilot-only nav entries. The backend hit the same bug and fixed it
 * the same way; this is the client-side twin nobody audited.
 */
export function isXtmOneMode(mode: string | undefined | null): boolean {
  return mode === "xtm_one" || mode === "ai_disabled";
}

export function usePlatformName(fallback: string = ""): string {
  const config = usePlatformConfig();
  if (config?.platform_name) return config.platform_name;
  if (config?.platform_mode === "copilot") return "Filigran Copilot";
  if (isXtmOneMode(config?.platform_mode)) return "XTM One";
  return fallback;
}

/**
 * The product logo to display, honouring an operator's uploaded one.
 *
 * The bundled logos paint their wordmark white, for a dark UI. On a light
 * background that made the product name invisible — a defect that predates the
 * design-system migration but became easy to hit once light mode was exercised.
 * Each bundled logo therefore has a `-light` variant whose wordmark is the
 * design system's light-theme primary text colour, and the mode picks between
 * them.
 *
 * `currentColor` would be the tidier fix and does not work here: these are
 * served as `<img src>`, and an image has no colour context to inherit from.
 * Making it work would mean inlining both logos as components, at every call
 * site.
 *
 * The backend always fills `platform_logo` - with the bundled white wordmark
 * when no operator set one - so the light variant has to come from it too:
 * `platform_logo_light`, set only while the bundled logo is in use. Reading
 * the mode only when `platform_logo` was empty left the light theme with the
 * white wordmark on every deployment.
 *
 * An operator's own logo is returned untouched in both themes - its colours
 * are theirs. Recorded in fds-migration/LIBRARY-FEEDBACK.md as a branding
 * follow-up.
 */
export function platformLogoUrl(
  config: Pick<PlatformConfig, "platform_mode" | "platform_logo" | "platform_logo_hash" | "platform_logo_light"> | null,
  mode: "light" | "dark",
): string {
  if (!config) return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E";
  const light = mode === "light";
  if (light && config.platform_logo_light) return withBasePath(config.platform_logo_light);
  if (config.platform_logo) return withVersion(config.platform_logo, config.platform_logo_hash);
  /* A config with no logo at all (an older backend): the bundled pair, picked
     by mode. The path prefix is the deployment's, whichever is picked. */
  if (isXtmOneMode(config.platform_mode)) {
    return withBasePath(light ? "/xtp-one-light.svg" : "/xtp-one.svg");
  }
  return withBasePath(light ? "/filigran-copilot-light.svg" : "/filigran-copilot.svg");
}

export function usePlatformLogoUrl(): string {
  return platformLogoUrl(usePlatformConfig(), useColorMode());
}

/**
 * The product mark the collapsed navigation rail shows in place of the
 * wordmark, in the theme's variant - or `null` when there is none (an
 * operator's logo with no favicon, a backend older than the field), for the
 * caller to crop the logo instead. Which mark applies is the backend's call,
 * as for the light logo.
 */
export function platformEmblemUrl(
  config: Pick<PlatformConfig, "platform_emblem" | "platform_emblem_hash" | "platform_emblem_light"> | null,
  mode: "light" | "dark",
): string | null {
  if (!config) return null;
  if (mode === "light" && config.platform_emblem_light) return withBasePath(config.platform_emblem_light);
  if (config.platform_emblem) return withVersion(config.platform_emblem, config.platform_emblem_hash ?? null);
  return null;
}

export function usePlatformEmblemUrl(): string | null {
  return platformEmblemUrl(usePlatformConfig(), useColorMode());
}

export function useIsXtmOne(): boolean {
  const config = usePlatformConfig();
  return isXtmOneMode(config?.platform_mode);
}

/**
 * Per-mode default login subtitles.
 *
 * Mirror ``BrandingDefaults.login_message`` in
 * ``backend/app/services/branding.py`` — the backend is the source of truth
 * (``/platform/config`` already resolves the default server-side); these
 * client-side copies cover the brief in-flight window and the Settings
 * placeholder, where the *unconfigured* default must be shown regardless of
 * any current override.
 */
const LOGIN_MESSAGE_DEFAULTS: Record<string, string> = {
  xtm_one: "Autonomous AI agents for threat exposure management",
  copilot: "Your open agentic AI platform",
};

/**
 * Mode default login subtitle, ignoring any admin override.
 *
 * Use this for the Settings placeholder so an admin always sees the fallback
 * that will apply once they clear the field — not their current override,
 * which is what the resolved ``/platform/config`` value (and
 * :func:`usePlatformLoginMessage`) carries.
 */
export function usePlatformLoginMessageDefault(): string {
  const config = usePlatformConfig();
  return (config && LOGIN_MESSAGE_DEFAULTS[config.platform_mode]) || "";
}

/**
 * Resolved Markdown login-screen message.
 *
 * Prefers the admin-configured ``platform_login_message`` (already resolved
 * server-side with the mode-aware default applied). While the config request
 * is in flight ``config`` is ``null`` and the deployment mode is unknown, so
 * this returns an empty string — the login subtitle stays blank for that brief
 * window rather than flashing the wrong product's copy, and fills in once the
 * config resolves. The per-mode default is a defensive fallback for the
 * unlikely case the server returns an empty message after loading.
 */
export function usePlatformLoginMessage(): string {
  const config = usePlatformConfig();
  if (config?.platform_login_message) return config.platform_login_message;
  return (config && LOGIN_MESSAGE_DEFAULTS[config.platform_mode]) || "";
}

export function useXTMLicense(): XTMLicense | null {
  const config = usePlatformConfig();
  if (!config) return null;
  return config.xtm_license ?? null;
}

/**
 * Whether the chat **route** is reachable.
 *
 * Unlike :func:`useChatSendEnabled` (which controls the composer and the
 * "new conversation" button), the route stays available even on
 * Community Edition so users can browse their existing conversation history
 * and the platform feels explorable. The Copilot mode keeps its
 * original behaviour — chat route is gated on a valid license.
 */
export function useChatEnabled(): boolean {
  const config = usePlatformConfig();
  if (!config) return true;
  // Copilot mode: keep the historical license-required gate.
  if (!isXtmOneMode(config.platform_mode)) return config.chat_enabled ?? true;
  // XTM One: always allow navigating to /chat. The composer disables
  // itself based on ``useChatSendEnabled`` below — the spec is
  // "everything accessible but read-only to encourage exploration".
  return true;
}

/**
 * Whether starting a new conversation / sending a message is allowed.
 *
 * Drives the disabled state on:
 *   - the chat composer textarea
 *   - the "send" button
 *   - the "new conversation" button
 *   - any agent "Chat now" / "Start chat" CTAs that route into /chat
 *
 * The matching server-side gate is :func:`require_chat_enabled` — both
 * must be kept in sync so the UI never optimistically opens a
 * conversation that the backend will refuse.
 *
 * Covers the AI-disabled mode without a check of its own: the backend
 * `is_chat_enabled` short-circuits on the AI state, so `chat_enabled` already
 * arrives `false`. Use `AiDisabledTip` to explain *why* on those surfaces.
 */
export function useChatSendEnabled(): boolean {
  const config = usePlatformConfig();
  // Default to ``true`` while loading so the UI doesn't briefly flicker
  // as disabled when the config request hasn't settled yet.
  if (!config) return true;
  if (typeof config.chat_enabled === "boolean") return config.chat_enabled;
  if (!isXtmOneMode(config.platform_mode)) return true;
  return config.agentic_enabled;
}

/** Return the resolved XTM One deployment edition. Empty string in Copilot mode. */
export function useEdition(): Edition {
  const config = usePlatformConfig();
  return config?.edition ?? "";
}

/**
 * ``true`` when XTM One runs Community Edition (no Filigran license, no EE
 * platform). Community Edition does not switch AI off: whether AI runs
 * follows the platform mode (backend ``ai_state.py``, read here through
 * ``useAiEnabled``), and AI is on by default in Community Edition. What CE
 * locks are the Enterprise features (quotas, licensed write surfaces), which
 * lead to the upgrade path. ``false`` in Copilot mode, whose unlicensed state
 * is the licence wall rather than an explorable edition.
 */
export function useIsCommunityEdition(): boolean {
  return useEdition() === "community";
}

/**
 * Whether AI is switched on for this deployment (FR-011).
 *
 * Gate affordances on this, never on a caught error: a disabled button beats a
 * refused click, and the sign-in screen has no request to refuse.
 *
 * Defaults to **enabled** in the two cases where the answer is not yet known —
 * config still loading, or a backend older than the field. The mode is opt-in,
 * so the safe frontend default is the ordinary platform; failing closed here
 * would flash "AI disabled" on every load of every normal install. The backend
 * gate is what actually refuses work, and it fails closed.
 *
 * Distinct from `useAgenticAIEnabled()`, which asks whether the licence permits
 * AI. Neither implies the other.
 */
export function useAiEnabled(): boolean {
  const platformOn = usePlatformAiEnabled();
  const group = useAiDisabledGroup();
  return platformOn && group === null;
}

/** The platform-wide switch alone, whatever the person's groups say. */
export function usePlatformAiEnabled(): boolean {
  const config = usePlatformConfig();
  return config?.ai_enabled ?? true;
}


/**
 * Whether AI is LICENSED on this deployment — Enterprise Edition (XTM One: a
 * valid Filigran license or a connected EE platform; Copilot: a valid license).
 *
 * The one licence answer: chat, creation flows, KB sources, the MCP agent
 * endpoints and A2A all follow it, so no surface can be "on" while another
 * is gated. ``useAgenticQuotas()`` tells the ceilings apart on its own - an
 * uncapped period is ``null``, a Community Edition deployment reports 0/0/0 -
 * but only this says whether agentic execution runs at all. Fails closed
 * while the config loads.
 *
 * Distinct from :func:`useAiEnabled`, the administrator's switch. Neither
 * implies the other.
 */
export function useAgenticAIEnabled(): boolean {
  const config = usePlatformConfig();
  return config?.agentic_enabled ?? false;
}

export function useAgenticQuotas(): AgenticQuotas {
  const config = usePlatformConfig();
  return config?.agentic_quotas ?? { daily: 0, monthly: 0, yearly: 0 };
}

export function useAgenticSource(): string {
  const config = usePlatformConfig();
  return config?.agentic_source ?? "none";
}

// ── Security feature flags — fail-closed defaults ─────────────────────────
//
// These three hooks gate sandbox/runtime surfaces (code interpreter,
// custom JS tools, custom template variables) behind platform-level
// security flags.  All three return ``false`` while ``PlatformConfig``
// is still loading so the UI never briefly renders the gated surface
// (or fires the gated network request) before the flag value is known.
// Strict ``=== false`` is intentional: if the backend is missing a flag
// (e.g. staggered deploy, partial response), ``undefined === false`` is
// ``false``, so the feature stays disabled (fail-closed).  The previous
// ``!config.disable_*`` form returned ``!undefined === true``, which made
// the flags fail-open when the field was absent from the response.

export function useCodeInterpreterEnabled(): boolean {
  const config = usePlatformConfig();
  if (!config) return false;
  return config.disable_code_interpreter === false;
}

export function useCustomToolsEnabled(): boolean {
  const config = usePlatformConfig();
  if (!config) return false;
  return config.disable_custom_tools === false;
}

export function useCustomVariablesEnabled(): boolean {
  const config = usePlatformConfig();
  if (!config) return false;
  return config.disable_custom_variables === false;
}

export function useAICatalogEnabled(): boolean {
  return usePlatformConfig()?.ai_catalog_enabled ?? false;
}
