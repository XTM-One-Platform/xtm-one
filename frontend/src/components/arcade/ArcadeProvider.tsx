/**
 * The waiting arcade's host, mounted once above the routes.
 *
 * It holds what outlives any single screen: whether a game is on, whether a
 * chat surface is still waiting on its agent (the chat's waiting panel comes
 * and goes as reasoning streams, a game must not), and the hidden code that
 * opens the arcade from anywhere. The game itself and the leaderboard are
 * separate lazy chunks, kept out of the first paint and fetched once the
 * browser is idle: a deploy replaces every hashed chunk, and a tab that had
 * not loaded them by then could never play again.
 */
import { Component, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useToast } from "@/components/ui/toast";
import { useAuthConfig, useMeAboveRoutes } from "@/hooks/api/useAuthApi";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { getToken, isImpersonating } from "@/lib/api";
import { isChunkLoadError, warmChunksWhenIdle } from "@/lib/lazyWithRetry";
import { termsOfUsePending } from "@/lib/terms-of-use";
import { ArcadeContext, type ArcadeContextValue, type ArcadeLaunchOptions } from "./arcade-context";
import { arcadeGate } from "./arcade-gate";
import {
  type ArcadeLinkStorage,
  type ArcadeRequestTarget,
  arcadeLinkVerdict,
  arcadeRequestUrl,
  captureArcadeRequest,
  forgetArcadeRequest,
  requestsArcade,
  withoutArcadeRequest,
} from "./arcade-link";
import { KONAMI_CODE, SecretCodeListener, isEditableTarget } from "./engine/easter-eggs";
import { WaitSources, type ArcadeAnswer, type SurfaceWait } from "./wait-sources";

/** The tab's session storage; reading it throws in a sandboxed frame. */
function tabStorage(): ArcadeLinkStorage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/**
 * The link request of the URL this page loaded with (`arcade-link.ts`), read
 * as the app boots: before this provider first renders (the app waits for the
 * platform config), a stale session's first request may already have sent the
 * tab to `/login`, and a signed-out visit is sent there by the route guard,
 * both dropping the query string. Honoured or dropped once: a remount of the
 * provider must not replay it.
 */
let landingLinkRequest = captureArcadeRequest(window.location.search, tabStorage(), Date.now());
const pendingLandingLinkRequest = () => landingLinkRequest;
const settleLandingLinkRequest = () => {
  landingLinkRequest = null;
};

const loadOverlay = () => import("./ArcadeOverlay");
const loadLeaderboard = () => import("./ArcadeLeaderboard");
const ArcadeOverlay = lazy(loadOverlay);
const ArcadeLeaderboardDialog = lazy(loadLeaderboard);

/** How long the offer to reload stays up: long enough to reach the button. */
const RELOAD_OFFER_MS = 15_000;

/** One offer on screen, however many times Play is clicked while the chunk
 *  still fails: `React.lazy` rethrows the same error on every mount. The
 *  game's and the leaderboard's share it - the latest request wins. */
const RELOAD_OFFER_TAG = "arcade-reload";

/** Reload asking for what this page's build could not load (`arcade-link.ts`):
 *  the build that answers opens it. */
function reloadInto(target: ArcadeRequestTarget) {
  const url = arcadeRequestUrl(window.location.href, target);
  // Already asked for in the address bar: assigning the same URL would not
  // even leave the page when it carries a hash.
  if (url === window.location.href) window.location.reload();
  else window.location.assign(url);
}

/** A game that crashes closes itself; it never takes the page with it. */
class ArcadeBoundary extends Component<{ onError: (error: unknown) => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error("[arcade]", error);
    this.props.onError(error);
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

interface GameSession {
  key: number;
  options: ArcadeLaunchOptions;
}

export function ArcadeProvider({ children }: { children: ReactNode }) {
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const platformName = usePlatformName("XTM One");
  const { data: authConfig, isPending: authConfigPending } = useAuthConfig();
  // Mounted above the routes, this provider re-renders on nothing the sign-in
  // changes; a navigation follows every sign-in and sign-out, so the token is
  // re-read on each one.
  const location = useLocation();
  const navigate = useNavigate();
  const signedIn = !!getToken();
  const { data: me } = useMeAboveRoutes(signedIn);
  // Whether the arcade may open at all: the one answer every way in reads.
  // The last `/auth/me` answer holds while a refetch runs or fails: a gate
  // that closed then would end a running game on a window-focus refetch.
  const gate = arcadeGate({
    signedIn,
    impersonating: isImpersonating(),
    termsPending: me ? termsOfUsePending(me.terms_of_use) : undefined,
    arcadeEnabled: authConfig?.arcade_enabled,
    configPending: authConfigPending,
  });
  const enabled = gate === "open";
  const [linkRequest, setLinkRequest] = useState(pendingLandingLinkRequest);
  const [session, setSession] = useState<GameSession | null>(null);
  const sessionRef = useRef<GameSession | null>(null);
  const [leaderboardOpen, setLeaderboardOpen] = useState(false);
  // Whether the answer the running game waits for has arrived. Kept out of
  // the context value: a turn starting or ending must not re-render every
  // consumer of the arcade.
  const [answer, setAnswer] = useState<ArcadeAnswer>("none");
  const waits = useRef(new WaitSources());
  const waitStartedAt = useRef<number | null>(null);

  const syncWaits = useCallback(() => setAnswer(waits.current.answer), []);

  const setWaitSource = useCallback(
    (id: string, wait: SurfaceWait | null) => {
      if (wait === null) waits.current.drop(id);
      else waits.current.report(id, wait);
      const any = waits.current.any;
      if (any && waitStartedAt.current === null) waitStartedAt.current = Date.now();
      if (!any) waitStartedAt.current = null;
      syncWaits();
    },
    [syncWaits],
  );

  const startGame = useCallback(
    (options: ArcadeLaunchOptions) => {
      waits.current.watch(options.waitSource);
      const next = { key: Date.now(), options };
      sessionRef.current = next;
      setSession(next);
      syncWaits();
    },
    [syncWaits],
  );

  const launch = useCallback(
    (options: ArcadeLaunchOptions) => {
      if (!enabled || sessionRef.current) return;
      setLeaderboardOpen(false);
      startGame(options);
    },
    [enabled, startGame],
  );

  const close = useCallback(() => {
    sessionRef.current = null;
    waits.current.unwatch();
    setSession(null);
    syncWaits();
  }, [syncWaits]);

  // A gate that closes mid-game (a sign-out, Terms of Use to accept again)
  // ends the game; it must not come back when the gate reopens.
  useEffect(() => {
    if (enabled) return;
    close();
    setLeaderboardOpen(false);
  }, [enabled, close]);

  const replay = useCallback(() => startGame({ source: "replay", origin: null }), [startGame]);

  const openLeaderboard = useCallback(() => {
    if (enabled) setLeaderboardOpen(true);
  }, [enabled]);

  const prefetch = useCallback(() => {
    // A stale chunk after a deploy is not an error yet: `lazy()` reports it
    // to the boundary if someone actually plays.
    if (enabled) loadOverlay().catch(() => undefined);
  }, [enabled]);

  // Imported while the build that served this page is still the deployed one,
  // once the gate is open: the session confirmed, no Terms of Use consent owed
  // and `/auth/config` answered with the arcade on - an import already started
  // cannot be called back when it says off.
  useEffect(() => {
    if (!enabled) return;
    return warmChunksWhenIdle([loadOverlay, loadLeaderboard]);
  }, [enabled]);

  // A chunk that does not load belongs to the build this page was opened
  // with, replaced by a deploy since: only a reload reaches the current one.
  // Any other failure is a crash, closed without a word.
  const offerReload = useCallback(
    (target: ArcadeRequestTarget) =>
      toast({
        title: target === "game" ? t_i18n("Reload to play") : t_i18n("Reload to open the leaderboard"),
        description: t_i18n("{name} was updated since this page was opened.", { name: platformName }),
        variant: "info",
        durationMs: RELOAD_OFFER_MS,
        tag: RELOAD_OFFER_TAG,
        action: { label: t_i18n("Reload"), onClick: () => reloadInto(target) },
      }),
    [toast, t_i18n, platformName],
  );

  const onGameError = useCallback(
    (error: unknown) => {
      close();
      if (isChunkLoadError(error)) offerReload("game");
    },
    [close, offerReload],
  );

  const onLeaderboardError = useCallback(
    (error: unknown) => {
      setLeaderboardOpen(false);
      if (isChunkLoadError(error)) offerReload("leaderboard");
    },
    [offerReload],
  );

  // A link asked for a game or the leaderboard (`arcade-link.ts`): decided by
  // the gate - it waits for the sign-in, while the gate is pending and through
  // the Terms of Use (`consent`), is honoured once it opens, and is dropped
  // when it closes or the request expired. Every input is this render's: a
  // sign-in stores its token in an effect that runs before this one.
  useEffect(() => {
    if (linkRequest === null) return;
    const verdict = arcadeLinkVerdict({ requestedAt: linkRequest.requestedAt, now: Date.now(), signedIn, gate });
    if (verdict === "wait") return;
    settleLandingLinkRequest();
    setLinkRequest(null);
    forgetArcadeRequest(tabStorage());
    if (requestsArcade(location.search)) {
      navigate(
        { pathname: location.pathname, search: withoutArcadeRequest(location.search), hash: location.hash },
        { replace: true, state: location.state },
      );
    }
    if (verdict !== "play") return;
    if (linkRequest.target === "leaderboard") openLeaderboard();
    else launch({ source: "link", origin: null });
  }, [linkRequest, signedIn, gate, launch, openLeaderboard, location, navigate]);

  // The code everyone knows, typed anywhere but in a text field.
  useEffect(() => {
    if (!enabled || session) return;
    const listener = new SecretCodeListener({ konami: KONAMI_CODE });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isEditableTarget(event.target)) {
        listener.reset();
        return;
      }
      if (listener.push(event.key) === "konami") launch({ source: "konami", origin: null });
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled, session, launch]);

  const active = !!session;
  const value = useMemo<ArcadeContextValue>(
    () => ({ enabled, active, launch, openLeaderboard, setWaitSource, prefetch }),
    [enabled, active, launch, openLeaderboard, setWaitSource, prefetch],
  );

  return (
    <ArcadeContext.Provider value={value}>
      {children}
      {session && enabled && (
        <ArcadeBoundary key={session.key} onError={onGameError}>
          <Suspense fallback={null}>
            <ArcadeOverlay
              options={session.options}
              answer={answer}
              getWaitStartedAt={() => waitStartedAt.current}
              onClose={close}
              onReplay={replay}
            />
          </Suspense>
        </ArcadeBoundary>
      )}
      {leaderboardOpen && enabled && (
        <ArcadeBoundary onError={onLeaderboardError}>
          <Suspense fallback={null}>
            <ArcadeLeaderboardDialog
              open={leaderboardOpen}
              onOpenChange={setLeaderboardOpen}
              onPlay={() => launch({ source: "leaderboard", origin: null })}
            />
          </Suspense>
        </ArcadeBoundary>
      )}
    </ArcadeContext.Provider>
  );
}
