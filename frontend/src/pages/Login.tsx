// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Shield, LogIn, AlertCircle, Clock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardDescription } from "@/components/ui/card";
import { useLogin, useRegister, useAuthConfig } from "@/hooks/api/useAuthApi";
import { MfaCodeStep } from "@/components/auth/MfaCodeStep";
import { BrowserLoginStep, DesktopHandoffStep } from "@/components/auth/DesktopSignIn";
import { clearAllTokens, getToken, setToken } from "@/lib/api";
import { DESKTOP_PRODUCT_NAME } from "@/lib/desktop";
import type { DesktopBrowserLoginProvider } from "@/types";
import { useDesktopBridge } from "@/hooks/useDesktop";
import { validatePassword, MIN_PASSWORD_LENGTH } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import {
  SESSION_ENDED_PARAM,
  SESSION_RETURN_PATH_KEY,
  readSessionEndedParam,
  safeReturnPath,
  signedInUrlSwitchesSession,
  type SessionEndReason,
} from "@/lib/session";
import { usePlatformName, usePlatformLogoUrl, usePlatformLoginMessage } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";


/**
 * Microsoft logo SVG (inline, minimal). Used for the OIDC button when the
 * provider display name includes "Microsoft".
 */
function MicrosoftIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 21 21" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="1" y="1" width="9" height="9" fill="#F25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
      <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
      <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
    </svg>
  );
}

/** Simple email format check (no native browser popups). */
function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

/**
 * Read (and clear) the MCP OAuth consent return URL stashed by the consent
 * page before it redirected here for SSO login. Returns the URL only when it
 * is a same-origin link back to the MCP consent endpoint, so a poisoned
 * sessionStorage value can never be used to open-redirect the user after a
 * successful login.
 */
function consumeMcpOauthReturn(): string | null {
  let raw: string | null;
  try {
    raw = sessionStorage.getItem("mcp_oauth_return");
    if (raw) sessionStorage.removeItem("mcp_oauth_return");
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const url = new URL(raw, window.location.origin);
    // Only allow the exact GET consent page — NOT a subpath like
    // ``/mcp/oauth/authorize/session`` (a POST-only endpoint that would 405),
    // so a poisoned sessionStorage value can't bounce the user anywhere else.
    const consentPath = withBasePath("/mcp/oauth/authorize");
    if (
      url.origin === window.location.origin &&
      (url.pathname === consentPath || url.pathname === `${consentPath}/`)
    ) {
      return url.toString();
    }
  } catch {
    // Malformed URL — ignore.
  }
  return null;
}

/** What an SSO callback handed back: a finished session, or a pending factor. */
type SsoCallback =
  | { kind: "session"; token: string; desktopLogin?: string }
  | { kind: "mfa"; mfaToken: string; desktopLogin?: string };

/**
 * Read the SSO callback credential from the URL and scrub it after consumption.
 *
 * The backend delivers it in the URL FRAGMENT (`/login#token=...`, or
 * `/login#mfa_token=...` when the account has two-factor enabled): fragments
 * are processed client-side only and are never sent to servers, proxy/access
 * logs, or `Referer` headers. The legacy query-string form (`/login?token=...`)
 * is still honoured as a fallback so older bookmarks and in-flight flows
 * started before the backend change keep working.
 *
 * `mfa_token` is a bearer credential too — a 5-minute challenge that completes
 * a login — so it is scrubbed on exactly the same terms as a session token.
 *
 * Whichever form carried the credential, it is scrubbed from BOTH the query
 * string and the fragment via `history.replaceState` — a copied/crafted URL
 * could carry `?token=` and `#token=` at the same time, and leaving either
 * one in place would keep a JWT in the address bar and browser history.
 * Only the part(s) that actually carried a credential are re-serialized; an
 * untouched part is preserved VERBATIM — round-tripping an opaque anchor
 * like `#help` through `URLSearchParams.toString()` would mangle it into
 * `#help=` (it parses `help` as a key with an empty value), breaking
 * anchor/hash-router fragments on legacy `?token=` URLs. A part that did
 * carry one is by definition param-shaped, so re-serializing it is
 * safe, and its unrelated params survive the rewrite. The existing
 * `history.state` is passed through unchanged — React Router stores its own
 * navigation state (`{ usr, key, idx }`) there, and replacing it with `null`
 * would desynchronize the router's back/forward tracking.
 */
function consumeSsoCallback(): SsoCallback | null {
  const { hash, search, pathname } = window.location;
  // `desktop_login` is not a credential but it belongs to the same
  // round-trip: the SSO callback emits it next to the token when the sign-in
  // was started from the XTM One Desktop app, and it is scrubbed with it.
  const KEYS = ["token", "mfa_token", "desktop_login"] as const;

  const fragmentParams = new URLSearchParams(hash.slice(1));
  const queryParams = new URLSearchParams(search);

  const found = Object.fromEntries(
    KEYS.map((key) => [key, fragmentParams.get(key) || queryParams.get(key)]),
  ) as Record<(typeof KEYS)[number], string | null>;
  if (!found.token && !found.mfa_token) return null;
  const desktopLogin = found.desktop_login && /^[A-Za-z0-9_-]{32,64}$/.test(found.desktop_login)
    ? found.desktop_login
    : undefined;

  const scrubbedFromQuery = KEYS.some((key) => queryParams.has(key));
  const scrubbedFromFragment = KEYS.some((key) => fragmentParams.has(key));

  let newSearch = search;
  if (scrubbedFromQuery) {
    KEYS.forEach((key) => queryParams.delete(key));
    const remaining = queryParams.toString();
    newSearch = remaining ? `?${remaining}` : "";
  }
  let newHash = hash;
  if (scrubbedFromFragment) {
    KEYS.forEach((key) => fragmentParams.delete(key));
    const remaining = fragmentParams.toString();
    newHash = remaining ? `#${remaining}` : "";
  }
  window.history.replaceState(
    window.history.state,
    "",
    pathname + newSearch + newHash,
  );

  // A session token wins if a crafted URL somehow carries both: it is the
  // stricter reading only in the sense that it is what the backend issues when
  // no factor is owed, and the backend never emits the two together.
  if (found.token) return { kind: "session", token: found.token, desktopLogin };
  return { kind: "mfa", mfaToken: found.mfa_token as string, desktopLogin };
}

/**
 * Read and scrub an SSO refusal marker from `/login?sso_error=...`.
 *
 * The backend redirects here when an identity provider authenticated someone
 * successfully but this instance declined to admit them (the XTM Hub
 * entitlement gate; no account while on-the-fly creation is off; a
 * deactivated account). It arrives as a query parameter rather than a
 * fragment on purpose: unlike a token this is not a credential, and it must
 * survive being read by the page it lands on.
 *
 * The marker is opaque by design. The backend deliberately sends no reason —
 * the criteria describe how the organisation's identity provider is configured,
 * and the person standing here is unauthenticated to us — so the wording below
 * is all the page has, and all it should have. The reason is in the server log.
 *
 * Scrubbed on the same terms as a token, for a different reason: left in place
 * it would survive a reload and re-accuse someone who has since signed in
 * perfectly well. `history.state` is passed through unchanged so React Router's
 * back/forward tracking stays in sync, and only the query string is
 * re-serialized — an untouched fragment is preserved verbatim.
 */
function consumeSsoError(): string | null {
  const { hash, search, pathname } = window.location;
  const queryParams = new URLSearchParams(search);
  const code = queryParams.get("sso_error");
  if (!code) return null;

  queryParams.delete("sso_error");
  const remaining = queryParams.toString();
  window.history.replaceState(
    window.history.state,
    "",
    pathname + (remaining ? `?${remaining}` : "") + hash,
  );
  return code;
}

/**
 * Read and scrub why the session ended, from `/login?session_ended=...`.
 * Scrubbed like `sso_error`: a reload must not say it again.
 */
function consumeSessionEnded(): SessionEndReason | null {
  const { hash, search, pathname } = window.location;
  const reason = readSessionEndedParam(search);
  if (!reason) return null;
  const queryParams = new URLSearchParams(search);
  queryParams.delete(SESSION_ENDED_PARAM);
  const remaining = queryParams.toString();
  window.history.replaceState(window.history.state, "", pathname + (remaining ? `?${remaining}` : "") + hash);
  return reason;
}

/** Read (and clear) the page a session ended on, to land there again once signed in. */
function consumeSessionReturnPath(): string | null {
  try {
    const raw = sessionStorage.getItem(SESSION_RETURN_PATH_KEY);
    sessionStorage.removeItem(SESSION_RETURN_PATH_KEY);
    return safeReturnPath(raw);
  } catch {
    return null;
  }
}

/**
 * Minimal Markdown renderer for the login subtitle. Supports links (opened in
 * a new tab), emphasis, and lists so deployers can point users at an internal
 * policy / help page. Styling matches ``CardDescription`` (muted, centered).
 */
const loginMessageComponents: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2 hover:text-accent/80"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-default-secondary">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => <ul className="my-2 list-disc pl-5 text-left">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal pl-5 text-left">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  code: ({ children }) => (
    <code className="rounded-sm bg-elevation-hover px-1 py-0.5 text-[13px] font-mono-plex text-accent/80">{children}</code>
  ),
};

/**
 * Whitelist of Markdown elements the login subtitle may render. The login page
 * is public/unauthenticated, so this deliberately excludes images: a Markdown
 * image (``![](https://...)``) would make every visitor's browser fetch an
 * arbitrary external URL before authentication (a privacy/SSRF-adjacent
 * footgun) and could break the layout. Headings, blockquotes, tables, ``hr``,
 * etc. are likewise dropped to keep this a compact subtitle. Anything outside
 * this set is unwrapped (its text content is preserved) via
 * ``unwrapDisallowed`` below.
 */
const LOGIN_MESSAGE_ALLOWED_ELEMENTS = [
  "p", "a", "strong", "em", "del", "code", "ul", "ol", "li", "br",
];

function LoginMessage({ content }: { content: string }) {
  if (!content.trim()) return null;
  return (
    <div className="content-base text-default-secondary">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={loginMessageComponents}
        allowedElements={LOGIN_MESSAGE_ALLOWED_ELEMENTS}
        unwrapDisallowed
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

/**
 * Inline field error displayed below an input. `id` is what the input names in
 * `aria-describedby` while the error shows, so a screen reader reads the reason
 * with "invalid" (WCAG 3.3.1).
 */
function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="mt-1.5 flex items-center gap-1.5 content-compact text-feedback-error-primary animate-in fade-in slide-in-from-top-1 duration-200">
      <AlertCircle className="h-3 w-3 shrink-0" />
      {message}
    </p>
  );
}

export default function Login() {
  const { t_i18n } = useFormatter();
  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");

  // --- Second factor ---
  // Holding a challenge token IS the "show the code step" condition: the token
  // is the only proof the password half succeeded, so deriving the step from it
  // means the UI can never show the code form without something to submit it
  // with, nor lose the token while still showing the form.
  const [mfaToken, setMfaToken] = useState<string | null>(null);

  // Per-field validation errors
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string; fullName?: string }>({});
  // Top-level error (bad credentials, server errors)
  const [formError, setFormError] = useState("");
  // A federated sign-in that did not complete. Deliberately NOT
  // `formError`: that banner lives inside the local-auth form, which an
  // SSO-only deployment does not render at all — so a refused person would have
  // had the message set and nothing to show it in. The two are different events
  // anyway. A credential mistake is fixed in the field below it; an SSO refusal
  // is someone else's decision and there is nothing on this page to correct.
  const [ssoError, setSsoError] = useState("");
  // Why the previous session ended. With SSO the person signs in again
  // through their identity provider by choosing it below: an unattended
  // browser is never signed back in on its own.
  const [sessionEnded, setSessionEnded] = useState<SessionEndReason | null>(null);
  // Track whether the user has attempted to submit (show errors only after first attempt)
  const [submitted, setSubmitted] = useState(false);

  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const login = useLogin();
  const register = useRegister();
  /* `isError` matters here: `useAuthConfig` no longer throws (that would take
     the whole app down from the header), so a failed fetch leaves `authConfig`
     undefined and silently falls back to "local auth, no SSO". On an SSO-only
     deployment that renders an email/password form which can never succeed,
     with nothing on screen saying why — strictly worse than the error screen
     it used to get. The banner below is that missing surface.

     `&& !authConfig` because v5's `isError` covers refetch failures too: with
     a config already cached, a failed background refetch would otherwise post
     "sign-in options could not be loaded" above a correct, working SSO list. */
  const { data: authConfig, isError: authConfigFetchError } = useAuthConfig();
  const authConfigError = authConfigFetchError && !authConfig;
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  // A refused SSO sign-in comes back here with a marker instead of a token.
  // Its own effect rather than a branch of the callback one below: that effect
  // returns early when there is no credential, which is exactly the case a
  // refusal is.
  useEffect(() => {
    const ssoError = consumeSsoError();
    if (!ssoError) return;
    // "cancelled" is not a failure — the person declined the consent screen and
    // is told so plainly, with no administrator to contact and nothing to fix.
    const messages: Record<string, string> = {
      not_authorised: t_i18n("Your account is not authorised to sign in to this instance. Contact your administrator."),
      not_provisioned: t_i18n("No account exists for your identity, and this instance does not create one for it automatically. Ask your administrator to create your account first."),
      // Only an administrator can reactivate the account; retrying changes nothing.
      account_deactivated: t_i18n("This account has been deactivated. Contact your administrator."),
      cancelled: t_i18n("Sign-in was cancelled. You can try again when you are ready."),
      // The discovery document is unusable; only an administrator can fix the URL.
      misconfigured: t_i18n("Single sign-on is not configured correctly. Contact your administrator."),
    };
    // `hasOwnProperty`, not `messages[ssoError] ?? fallback`: the marker comes
    // straight off the query string, so `?sso_error=toString` (or `valueOf`,
    // `constructor`) would resolve to an inherited *function*. `??` never fires
    // for one, and React treats a function passed to a setter as a state
    // updater rather than a value — the banner would render "[object Undefined]".
    const message = Object.prototype.hasOwnProperty.call(messages, ssoError)
      ? messages[ssoError]
      : t_i18n("Single sign-on failed. Contact your administrator.");
    setSsoError(message);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const reason = consumeSessionEnded();
    if (reason) setSessionEnded(reason);
  }, []);

  // XTM One Desktop. Two different pages, one file:
  //   - INSIDE the app (`desktopBridge`), an SSO button runs the sign-in in the
  //     system browser and this page waits (`browserLogin`);
  //   - IN THE BROWSER, an SSO callback carrying `desktop_login` is a session
  //     to hand to the app, not to this tab (`handoff`).
  const desktopBridge = useDesktopBridge();
  const [browserLogin, setBrowserLogin] = useState<DesktopBrowserLoginProvider | null>(null);
  // `token` is the session to hand over - held here, never stored by this
  // tab, EXCEPT when a second factor is owed: the code step stores the
  // verified session over whatever this browser held (that is how it works
  // everywhere), so `priorToken` remembers the previous session to put back.
  const [handoff, setHandoff] = useState<{
    loginId: string;
    token: string | null;
    priorToken?: string | null;
  } | null>(null);
  const [handoffDeclined, setHandoffDeclined] = useState(false);

  // Handle SSO callback: the credential arrives in the URL fragment (preferred
  // — never sent to servers, logs, or Referer headers), with a legacy
  // query-string fallback. consumeSsoCallback() scrubs it from the address bar.
  // A signed-in URL opened in a tab that already sits on /login changes the
  // fragment alone: the document is not loaded again and this page stays
  // mounted, so the callback is also read on every `hashchange`.
  useEffect(() => {
    const handleCallback = () => {
      const callback = consumeSsoCallback();
      if (!callback) return;

      // The IdP proved the first factor but the account is enrolled in XTM One's
      // own second factor: show the code step instead of signing in. The
      // MCP consent return is deliberately left in sessionStorage — the user is
      // not signed in yet, and completeSignIn picks it up once they are.
      if (callback.kind === "mfa") {
        // A desktop handoff waits for the code first; the verified session is
        // then moved out of storage into the handoff (see completeSignIn), and
        // whatever session this browser already held is put back.
        if (callback.desktopLogin) {
          setHandoff({ loginId: callback.desktopLogin, token: null, priorToken: getToken() });
        }
        setMfaToken(callback.mfaToken);
        return;
      }

      // The sign-in was started from the Desktop app: this browser hands the
      // session over instead of keeping it (the person asked to sign in on the
      // desktop, not here).
      if (callback.desktopLogin) {
        setHandoff({ loginId: callback.desktopLogin, token: callback.token });
        return;
      }

      const switchesSession = signedInUrlSwitchesSession(getToken(), callback.token);
      setToken(callback.token);
      // Another session than the stored one: what the shell cached (the
      // `["me"]` profile and role above all) belongs to the previous identity,
      // so it is forgotten and the page loaded afresh, as at the end of an
      // impersonation. A first sign-in keeps the client-side navigation.
      if (switchesSession) queryClient.clear();
      // If the user was redirected here from the MCP OAuth consent page for
      // SSO login, return them there now that they hold a session. Use
      // replace() so the /login callback URL never lingers in browser
      // history.
      const mcpReturn = consumeMcpOauthReturn();
      if (mcpReturn) {
        window.location.replace(mcpReturn);
      } else if (switchesSession) {
        window.location.replace(withBasePath(consumeSessionReturnPath() ?? "/"));
      } else {
        navigate(consumeSessionReturnPath() ?? "/", { replace: true });
      }
    };
    handleCallback();
    window.addEventListener("hashchange", handleCallback);
    return () => window.removeEventListener("hashchange", handleCallback);
  }, [searchParams, navigate, queryClient]);

  /** Validate fields and return true if the form is valid. */
  const validate = (): boolean => {
    const errors: typeof fieldErrors = {};

    if (!email.trim()) {
      errors.email = t_i18n("Email is required");
    } else if (!isValidEmail(email)) {
      errors.email = t_i18n("Please enter a valid email address");
    }

    if (!password) {
      errors.password = t_i18n("Password is required");
    } else if (isRegister) {
      const pwErr = validatePassword(password, minPasswordLength);
      if (pwErr) errors.password = pwErr;
    }

    if (isRegister && !fullName.trim()) {
      errors.fullName = t_i18n("Full name is required");
    }

    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  /** Land the user wherever they were headed, now that a session exists. */
  const completeSignIn = () => {
    // A desktop handoff that owed a second factor: the code step stored the
    // verified session, and this tab's job is to hand it over, not to use it.
    // Move the session out of storage and into this render at once, so the
    // browser never keeps a signed-in tab behind the confirmation (a closed
    // tab, a "Not my machine") - the person asked to sign in on the desktop.
    // The code step wrote it over the session this browser held before (the
    // web app open in another tab): that one goes back, or storage is cleared
    // when there was none.
    if (handoff && handoff.token === null) {
      const verified = getToken();
      if (handoff.priorToken) setToken(handoff.priorToken);
      else clearAllTokens();
      setMfaToken(null);
      setHandoff({ loginId: handoff.loginId, token: verified });
      return;
    }
    // Return to the MCP OAuth consent page if we arrived from there, else to
    // the page an ended session left.
    const mcpReturn = consumeMcpOauthReturn();
    if (mcpReturn) {
      window.location.replace(mcpReturn);
    } else {
      navigate(consumeSessionReturnPath() ?? "/");
    }
  };

  /**
   * The handoff step finished (either way). This tab keeps no session from the
   * handoff: the token only ever lived in `handoff.token` (the SSO callback's
   * fragment, or the MFA step's session moved out of storage in
   * `completeSignIn`), so nothing is cleared here - a session the browser held
   * BEFORE the handoff (the person already signed in to the web app in this
   * browser) is left exactly as it was.
   */
  const finishHandoff = (outcome: "completed" | "declined") => {
    if (outcome === "declined") {
      setHandoff(null);
      setHandoffDeclined(true);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setFormError("");

    if (!validate()) return;

    try {
      if (isRegister) {
        await register.mutateAsync({ email, password, full_name: fullName });
      } else {
        const result = await login.mutateAsync({ email, password });
        if (result.mfa_required && result.mfa_token) {
          // Password proven, but no session yet. Drop the password from state:
          // the challenge token now stands in for it, and there is no reason to
          // keep the plaintext around for the rest of the flow.
          setPassword("");
          setMfaToken(result.mfa_token);
          setSubmitted(false);
          return;
        }
      }
      completeSignIn();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Something went wrong";
      setFormError(message);
    }
  };

  /** Abandon the challenge and go back to email + password. */
  const cancelMfa = () => {
    setMfaToken(null);
    setFormError("");
  };

  /** Clear the field error for a given key when the user types. */
  const clearFieldError = (key: keyof typeof fieldErrors) => {
    if (fieldErrors[key]) {
      setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
    }
    // Also clear top-level form error when user starts correcting
    if (formError) setFormError("");
  };

  // Inside XTM One Desktop the SSO runs in the system browser: an
  // identity provider inside the app's webview is where corporate sign-ins
  // break (conditional access, passkeys, "use the browser you are signed in
  // to"). In a browser the buttons navigate as they always did.
  const handleOIDC = () => {
    if (desktopBridge) {
      setBrowserLogin("oidc");
      return;
    }
    window.location.href = withBasePath("/api/auth/oidc/login");
  };

  const handleSAML = () => {
    if (desktopBridge) {
      setBrowserLogin("saml");
      return;
    }
    window.location.href = withBasePath("/api/auth/saml/login");
  };

  /** The browser handed the session back to the app: sign this window in. */
  const onBrowserSignedIn = (accessToken: string) => {
    setBrowserLogin(null);
    setToken(accessToken);
    completeSignIn();
  };

  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();
  const loginMessage = usePlatformLoginMessage();

  const hasSso = authConfig?.oidc_enabled || authConfig?.saml_enabled;
  const isLoading = login.isPending || register.isPending;

  return (
    <div className="flex min-h-screen items-center justify-center bg-elevation-default p-4">
      {/* Background glow */}
      <div className="pointer-events-none fixed inset-0">
        <div className="absolute left-1/2 top-1/3 h-96 w-96 -translate-x-1/2 -translate-y-1/2 rounded-full bg-elevation-highlight/5 blur-3xl" />
      </div>

      <Card className="relative z-10 w-full max-w-md border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs">
        <CardHeader className="text-center">
          <div className="mx-auto mb-6">
            <img src={logoSrc} alt={platformName} className="h-10" />
          </div>
          {mfaToken ? (
            <CardDescription>{t_i18n("Two-factor authentication")}</CardDescription>
          ) : handoff || browserLogin ? (
            <CardDescription>{DESKTOP_PRODUCT_NAME}</CardDescription>
          ) : isRegister ? (
            <CardDescription>{t_i18n("Create your account")}</CardDescription>
          ) : (
            <LoginMessage content={loginMessage} />
          )}
          {/* FR-011 / SC-007: state the condition *before* authentication.
              This is the one screen with no request to refuse, so it has to
              come from the unauthenticated platform config. */}
          <AiDisabledNotice />
        </CardHeader>
        <CardContent className="space-y-6">
          {/* ===== Second factor =====
              Replaces the whole sign-in surface rather than appearing beside
              it: the password step is already behind us, and leaving the SSO
              buttons or the local form on screen would offer a way to sidestep
              the code that is currently being asked for. */}
          {mfaToken ? (
            <MfaCodeStep
              mfaToken={mfaToken}
              email={email}
              onVerified={completeSignIn}
              onCancel={cancelMfa}
            />
          ) : handoff ? (
            /* In the browser: the SSO callback carried a desktop
               handoff id - confirm the machine, hand the session over, keep
               none here. Replaces the whole surface like the MFA step: the
               sign-in is already done, only the destination is in question. */
            <DesktopHandoffStep
              key={handoff.loginId}
              loginId={handoff.loginId}
              token={handoff.token}
              onDone={finishHandoff}
            />
          ) : browserLogin ? (
            /* Inside the app: the system browser has the sign-in,
               this window waits for the session to come back. */
            <BrowserLoginStep
              provider={browserLogin}
              providerLabel={
                browserLogin === "oidc"
                  ? authConfig?.oidc_display_name ?? "OIDC"
                  : authConfig?.saml_display_name ?? "SAML"
              }
              onSignedIn={onBrowserSignedIn}
              onCancel={() => setBrowserLogin(null)}
            />
          ) : (
          <>
          {handoffDeclined && (
            <div className="mb-4 flex items-center gap-2.5 rounded-sm bg-elevation-hover px-4 py-3">
              <AlertCircle className="h-4 w-4 shrink-0 text-default-secondary" />
              <p className="content-base text-default-secondary">
                {t_i18n("Nothing was signed in. If that request did not come from your own machine, tell your administrator.")}
              </p>
            </div>
          )}
          {/* A failed federated sign-in, shown ABOVE the SSO buttons and outside
              the `local_enabled` gate below — an SSO-only deployment renders no
              local form, and this message must survive that. It sits next to the
              button the person just came back from, which is where they are
              looking. */}
          {authConfigError && (
            <div className="mb-4 flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3">
              <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
              <p className="content-base text-feedback-error-primary">
                {t_i18n("The available sign-in options could not be loaded, so what is shown below may be incomplete.")}{" "}
                {t_i18n("Reload the page to see the current state.")}
              </p>
            </div>
          )}

          {ssoError && (
            <div className="mb-4 flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3 animate-in fade-in slide-in-from-top-2 duration-300">
              <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
              <p className="content-base text-feedback-error-primary">{ssoError}</p>
            </div>
          )}

          {/* Outside the `local_enabled` gate too: an SSO-only deployment says it as well. */}
          {sessionEnded && !ssoError && (
            <div className="mb-4 flex items-center gap-2.5 rounded-sm bg-elevation-hover px-4 py-3">
              <Clock className="h-4 w-4 shrink-0 text-default-secondary" />
              <p className="content-base text-default-secondary">
                {sessionEnded === "idle"
                  ? t_i18n("You were signed out after a period of inactivity. Sign in again to continue.")
                  : sessionEnded === "pin"
                    ? t_i18n("You were signed out after too many wrong PINs. Sign in again to continue.")
                    : t_i18n("Your session has expired. Sign in again to continue.")}
              </p>
            </div>
          )}

          {/* ===== SSO Buttons ===== */}
          {hasSso && (
            <div className="space-y-3">
              {authConfig?.oidc_enabled && (
                <Button
                  type="button"
                  variant="outline"
                  className="w-full gap-3 h-11 content-base font-medium"
                  onClick={handleOIDC}
                >
                  {authConfig.oidc_display_name.toLowerCase().includes("microsoft") ? (
                    <MicrosoftIcon className="h-5 w-5" />
                  ) : (
                    <LogIn className="h-4 w-4" />
                  )}
                  {t_i18n("Continue with {provider}", { provider: authConfig.oidc_display_name })}
                </Button>
              )}

              {authConfig?.saml_enabled && (
                <Button
                  type="button"
                  variant="outline"
                  className="w-full gap-3 h-11 content-base font-medium"
                  onClick={handleSAML}
                >
                  <Shield className="h-4 w-4" />
                  {t_i18n("Continue with {provider}", { provider: authConfig.saml_display_name })}
                </Button>
              )}

              {/* Divider */}
              {authConfig?.local_enabled && (
                <div className="relative my-4">
                  <div className="absolute inset-0 flex items-center">
                    <div className="w-full border-t border-elevation-subtle-soft" />
                  </div>
                  <div className="relative flex justify-center content-compact">
                    <span className="layer-1 bg-elevation-default px-3 text-default-secondary">
                      {t_i18n("or continue with email")}
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ===== Local Auth Form ===== */}
          {(authConfig?.local_enabled !== false) && (
            <form onSubmit={handleSubmit} noValidate className="space-y-4">
              {isRegister && (
                <div className="space-y-2">
                  <label className="content-base font-medium text-default-secondary">{t_i18n("Full Name")}</label>
                  <Input aria-label={t_i18n("Full Name")}
                    value={fullName}
                    onChange={(e) => { setFullName(e.target.value); clearFieldError("fullName"); }}
                    placeholder={t_i18n("Your full name")}
                    aria-invalid={(submitted && Boolean(fieldErrors.fullName)) || undefined}
                    aria-describedby={submitted && fieldErrors.fullName ? "login-full-name-error" : undefined}
                  />
                  <FieldError id="login-full-name-error" message={submitted ? fieldErrors.fullName : undefined} />
                </div>
              )}
              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Email")}</label>
                <Input aria-label={t_i18n("Email")}
                  type="text"
                  inputMode="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => { setEmail(e.target.value); clearFieldError("email"); }}
                  placeholder="user@mail.com"
                  aria-invalid={(submitted && Boolean(fieldErrors.email)) || undefined}
                  aria-describedby={submitted && fieldErrors.email ? "login-email-error" : undefined}
                />
                <FieldError id="login-email-error" message={submitted ? fieldErrors.email : undefined} />
              </div>
              <div className="space-y-2">
                <label className="content-base font-medium text-default-secondary">{t_i18n("Password")}</label>
                <Input aria-label={t_i18n("Password")}
                  type="password"
                  autoComplete={isRegister ? "new-password" : "current-password"}
                  value={password}
                  onChange={(e) => { setPassword(e.target.value); clearFieldError("password"); }}
                  placeholder="••••••••"
                  aria-invalid={(submitted && Boolean(fieldErrors.password)) || undefined}
                  aria-describedby={submitted && fieldErrors.password ? "login-password-error" : undefined}
                />
                <FieldError id="login-password-error" message={submitted ? fieldErrors.password : undefined} />
              </div>

              {!isRegister && (
                <div className="-mt-1 flex justify-end">
                  <button
                    type="button"
                    onClick={() => navigate("/forgot-password")}
                    className="content-compact text-default-secondary hover:text-default-primary hover:underline"
                  >
                    {t_i18n("Forgot password?")}
                  </button>
                </div>
              )}

              {/* Domain restriction hint */}
              {isRegister && authConfig?.allowed_domains && authConfig.allowed_domains.length > 0 && (
                <p className="content-compact text-default-secondary">
                  {t_i18n("Registration restricted to: {domains}", { domains: authConfig.allowed_domains.join(", ") })}
                </p>
              )}

              {/* Top-level error banner (bad credentials, server errors) */}
              {formError && (
                <div className="flex items-center gap-2.5 rounded-sm bg-feedback-error-primary/10 px-4 py-3 animate-in fade-in slide-in-from-top-2 duration-300">
                  <AlertCircle className="h-4 w-4 shrink-0 text-feedback-error-primary" />
                  <p className="content-base text-feedback-error-primary">{formError}</p>
                </div>
              )}

              <Button type="submit" className="w-full" disabled={isLoading}>
                {isLoading ? (
                  <span className="flex items-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    {isRegister ? t_i18n("Creating account...") : t_i18n("Signing in...")}
                  </span>
                ) : (
                  isRegister ? t_i18n("Create Account") : t_i18n("Sign In")
                )}
              </Button>

              {authConfig?.registration_enabled === true && (
                <p className="text-center content-base text-default-secondary">
                  {isRegister ? t_i18n("Already have an account?") : t_i18n("Don't have an account?")}{" "}
                  <button
                    type="button"
                    onClick={() => { setIsRegister(!isRegister); setFormError(""); setFieldErrors({}); setSubmitted(false); }}
                    className="text-accent hover:underline"
                  >
                    {isRegister ? t_i18n("Sign in") : t_i18n("Create one")}
                  </button>
                </p>
              )}
            </form>
          )}
          </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
