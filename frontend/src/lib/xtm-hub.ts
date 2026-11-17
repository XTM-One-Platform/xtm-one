// SPDX-License-Identifier: Apache-2.0

/**
 * The XTM Hub as the home dashboard shows it, next to OpenCTI, OpenAEV and
 * OpenCRQ, and the registration of XTM One with it (Settings > Platform).
 * Those three products register themselves inbound; XTM One registers itself
 * on the Hub, from a Hub page opened in a popup that posts the platform token
 * back. The pill reads that registration first, then the `xtm_hub`
 * integrations the user can see. Kept free of the `@/` alias and of Vite
 * globals so `yarn test:unit` (Node, no DOM) can import it.
 *
 * The default XTM Hub is the backend's `XTM_HUB_DEFAULT_URL`, served as
 * `default_hub_url` by `GET /xtm-hub/registration/status`: every function
 * here that falls back to it takes it as an argument, `undefined` until that
 * answer arrives.
 */

/** The integration provider key of the XTM Hub. */
export const XTM_HUB_PROVIDER = "xtm_hub";

/** The fields of an integration the pill reads. */
interface XtmHubIntegrationLike {
  provider: string;
  status: string;
  extra_config: Record<string, unknown> | null;
}

type XtmHubPill =
  | { present: false }
  | { present: true; healthy: boolean; url: string | undefined };

/** This platform's registration with the XTM Hub, as `GET /xtm-hub/registration/status` serves it. */
type XtmHubRegistrationStatus =
  | "unregistered"
  | "registered"
  | "lost_connectivity"
  | "needs_reregistration";

export interface XtmHubRegistrationLike {
  status: XtmHubRegistrationStatus;
  hub_url: string | null;
  /** The backend's `XTM_HUB_DEFAULT_URL`. */
  default_hub_url: string;
}

/**
 * The XTM Hub pill. The platform's registration decides first, like the
 * suite products' registrations do: registered is healthy, a lost or stale
 * registration is shown unhealthy. Unregistered, the `xtm_hub` integrations
 * the user can see decide: absent when none is an XTM Hub, otherwise the
 * first connected one (else the first one), healthy only when connected. A
 * URL that is not an absolute http(s) URL never becomes the link: the pill
 * falls back to the default hub (the registration's `default_hub_url`)
 * rather than to a value someone typed.
 */
export function xtmHubPill(
  integrations: readonly XtmHubIntegrationLike[] | null | undefined,
  registration?: XtmHubRegistrationLike | null,
): XtmHubPill {
  const defaultUrl = registration?.default_hub_url;
  if (registration && registration.status !== "unregistered") {
    return {
      present: true,
      healthy: registration.status === "registered",
      url: xtmHubUrl({ base_url: registration.hub_url }, defaultUrl),
    };
  }
  const hubs = (integrations ?? []).filter((i) => i.provider === XTM_HUB_PROVIDER);
  if (hubs.length === 0) return { present: false };
  const hub = hubs.find((i) => i.status === "connected") ?? hubs[0];
  return { present: true, healthy: hub.status === "connected", url: xtmHubUrl(hub.extra_config, defaultUrl) };
}

/** *raw* as an absolute http(s) URL without credentials and trailing slash, else `null`. */
function httpUrl(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const parsed = new URL(raw.trim());
    if ((parsed.protocol === "http:" || parsed.protocol === "https:") && !parsed.username && !parsed.password) {
      return parsed.href.replace(/\/+$/, "");
    }
  } catch {
    /* not an absolute URL */
  }
  return null;
}

/** The hub an integration's `extra_config` names, else *defaultUrl* (the default hub). */
export function xtmHubUrl(
  extraConfig: Record<string, unknown> | null | undefined,
  defaultUrl: string | undefined,
): string | undefined {
  return httpUrl(extraConfig?.base_url) ?? defaultUrl;
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * The Hub a registration may be made with, or `null` when the input is
 * refused. Unlike `xtmHubUrl` (a display link), never falls back: a typo must
 * be reported, not silently registered against the default Hub (the caller
 * passes the default Hub for an empty input). Same rule as the backend's
 * `registration_hub_url`: an absolute https URL without credentials, query or
 * fragment, http only for a Hub on this machine.
 */
export function xtmHubRegistrationUrl(raw: string): string | null {
  const value = raw.trim();
  if (!value) return null;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) return null;
  const secure = parsed.protocol === "https:";
  const local = parsed.protocol === "http:" && LOOPBACK_HOSTS.has(parsed.hostname);
  if (!secure && !local) return null;
  return parsed.href.replace(/\/+$/, "");
}

// ---------------------------------------------------------------------------
// Registration popup
// ---------------------------------------------------------------------------

/** What the Hub's register page is opened with. */
interface XtmHubRegistrationParams {
  hubUrl: string;
  platformId: string;
  platformUrl: string;
  platformTitle: string;
  platformContract: "EE" | "CE";
  platformVersion: string;
  state: string;
}

/**
 * The Hub page that registers (or unregisters) this platform. The
 * `/redirect/...` route sends a signed-out user through the Hub's login and
 * back with every parameter. `state` is echoed back in the Hub's message.
 */
export function xtmHubRegistrationPageUrl(
  action: "register" | "unregister",
  params: XtmHubRegistrationParams,
): string {
  const query = new URLSearchParams(
    action === "register"
      ? {
          platform_id: params.platformId,
          platform_url: params.platformUrl,
          platform_title: params.platformTitle,
          platform_contract: params.platformContract,
          platform_version: params.platformVersion,
          state: params.state,
        }
      : { platform_id: params.platformId, state: params.state },
  );
  return `${registrationHub(params.hubUrl)}/redirect/${action}-xtmone?${query.toString()}`;
}

/** The origin a Hub's messages must come from. */
export function xtmHubOrigin(hubUrl: string): string {
  return new URL(registrationHub(hubUrl)).origin;
}

/** A Hub the caller validated (`xtmHubRegistrationUrl`); anything else is a bug, never a page to open. */
function registrationHub(hubUrl: string): string {
  const url = httpUrl(hubUrl);
  if (!url) throw new Error("Not an XTM Hub URL");
  return url;
}

type XtmHubRegistrationMessage =
  | { action: "register"; token: string }
  | { action: "unregister" }
  | { action: "cancel" };

const PLATFORM_TOKEN_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether `value` reads as an XTM Hub platform token (a UUID). */
export function isXtmHubPlatformToken(value: string): boolean {
  return PLATFORM_TOKEN_RE.test(value.trim());
}

/**
 * A message from the Hub's registration popup, or `null` for anything else.
 *
 * Only the popup this page opened (`source`), only from the Hub's exact
 * origin, only with the `state` this page generated (a stale document in the
 * reused popup cannot answer for a new request; a Hub that echoes no state
 * leaves the paste fallback), only the three actions the Hub sends, and a
 * register only with a platform token.
 */
export function readXtmHubRegistrationMessage(
  event: { data: unknown; origin: string; source: unknown },
  expected: { hubOrigin: string; popup: unknown; state: string },
): XtmHubRegistrationMessage | null {
  if (!expected.popup || event.source !== expected.popup) return null;
  if (event.origin !== expected.hubOrigin) return null;
  const data = event.data;
  if (!data || typeof data !== "object") return null;
  const { action, token, state } = data as Record<string, unknown>;
  if (typeof state !== "string" || !expected.state || state !== expected.state) return null;
  if (action === "register") {
    return typeof token === "string" && isXtmHubPlatformToken(token)
      ? { action, token: token.trim() }
      : null;
  }
  if (action === "unregister" || action === "cancel") return { action };
  return null;
}
