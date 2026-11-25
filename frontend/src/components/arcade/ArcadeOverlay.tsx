/**
 * The waiting arcade's full-screen game - a lazy chunk.
 *
 * Hosts the engine over the whole app and everything around it: the game
 * session (scores are only recorded against one the server issued), the HUD,
 * banners and achievement toasts, the pause and game-over screens, and the
 * rule that matters most - the agent's answer always wins. When the answer
 * has arrived, an idle player is sent back to it automatically, an
 * active one may keep playing or leave with Esc. Leaving records the run (a
 * game left untouched has none), rebuilds the interface the game damaged and
 * gives focus back to where it was, or to the composer once that is gone.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CheckCircle2,
  ChevronsUp,
  CloudOff,
  Heart,
  LogOut,
  Pause,
  Play,
  Rocket,
  RotateCcw,
  Shield,
  Trophy,
  Users,
  Volume2,
  VolumeX,
  Wrench,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useArcadeHeartbeat, useFinishArcadeSession, useStartArcadeSession } from "@/hooks/api/useArcadeApi";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ArcadeFinishResult, ArcadeLivePlayer, ArcadeSessionStart } from "@/types";
import type { ArcadeLaunchOptions } from "./arcade-context";
import { escapeAction, type ArcadeStage } from "./arcade-escape";
import { ArcadeLeaderboardPanel } from "./ArcadeLeaderboard";
import { BOSS_NAMES, POWER_UP_NAMES } from "./display";
import { achievementById, type AchievementId } from "./engine/achievements";
import { PAUSE_TIPS, seasonalTheme } from "./engine/easter-eggs";
import { ArcadeGame } from "./engine/game";
import type { ArcadeCanvasLabels, ArcadeEvent, HudSnapshot, RunResult } from "./engine/types";
import { heartbeatIntervalMs, startHeartbeat } from "./heartbeat";
import { followAccent, resolveArcadeTheme } from "./theme";
import type { ArcadeAnswer } from "./wait-sources";
import { Spinner } from "@filigran/design-system";

const MUTE_KEY = "xtmArcadeMuted";
const HINT_MS = 7000;
const BANNER_MS = 2400;
const ACHIEVEMENT_TOAST_MS = 4200;
/** Idle this long after the answer arrived and the countdown back to it starts. */
const ANSWER_IDLE_MS = 2500;
const ANSWER_COUNTDOWN_S = 6;
const GAME_OVER_COUNTDOWN_S = 10;
const PATIENCE_MS = 180_000;
const SESSION_MAX_WAIT_MS = 2500;
const INTRO_MS = 1100;

type Stage = ArcadeStage;

interface Banner {
  id: number;
  text: string;
  tone: "info" | "good" | "bad" | "gold";
}

interface AchievementToast {
  id: number;
  achievement: AchievementId;
}

type SaveState =
  | { status: "idle" | "saving" | "offline" }
  | { status: "saved"; data: ArcadeFinishResult }
  | { status: "error"; message: string };

interface Props {
  options: ArcadeLaunchOptions;
  /** Whether the answer this game waits for has arrived (`WaitSources`). */
  answer: ArcadeAnswer;
  getWaitStartedAt: () => number | null;
  onClose: () => void;
  onReplay: () => void;
}

function readMuted(): boolean {
  try {
    return window.localStorage.getItem(MUTE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeMuted(muted: boolean): void {
  try {
    window.localStorage.setItem(MUTE_KEY, muted ? "1" : "0");
  } catch {
    /* in-memory only */
  }
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function ArcadeOverlay({ options, answer, getWaitStartedAt, onClose, onReplay }: Props) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const reducedMotion = usePrefersReducedMotion();
  const startSession = useStartArcadeSession();
  const heartbeat = useArcadeHeartbeat();
  const finishSession = useFinishArcadeSession();

  const rootRef = useRef<HTMLDivElement>(null);
  // The overlay's tooltips portal into it: portaled to the body, they would be
  // painted under it.
  const [tipContainer, setTipContainer] = useState<HTMLDivElement | null>(null);
  const attachRoot = useCallback((el: HTMLDivElement | null) => {
    rootRef.current = el;
    setTipContainer(el);
  }, []);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const gameRef = useRef<ArcadeGame | null>(null);
  const sessionRef = useRef<ArcadeSessionStart | null>(null);
  // The live room's cadence, as the server last said it (the session, then each beat).
  const heartbeatSecondsRef = useRef<number | null>(null);
  const hudRef = useRef<HudSnapshot | null>(null);
  const leavingRef = useRef(false);
  const nextId = useRef(1);

  const [stage, setStage] = useState<Stage>("intro");
  const stageRef = useRef<Stage>("intro");
  const [hud, setHud] = useState<HudSnapshot | null>(null);
  const [banners, setBanners] = useState<Banner[]>([]);
  const [achievementToasts, setAchievementToasts] = useState<AchievementToast[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [save, setSave] = useState<SaveState>({ status: "idle" });
  const [offline, setOffline] = useState(false);
  const [muted, setMuted] = useState(readMuted);
  const mutedRef = useRef(muted);
  const [answerReady, setAnswerReady] = useState(false);
  const answerReadyRef = useRef(false);
  const [overtime, setOvertime] = useState(false);
  const overtimeRef = useRef(false);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [live, setLive] = useState<ArcadeLivePlayer[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const [showHint, setShowHint] = useState(true);
  const [showBoard, setShowBoard] = useState(false);
  // The engine's loop threw: rethrown while rendering, so `ArcadeBoundary`
  // closes the game and the unmount repairs the interface.
  const [crash, setCrash] = useState<{ error: unknown } | null>(null);
  const [tip] = useState(() => PAUSE_TIPS[Math.floor(Math.random() * PAUSE_TIPS.length)]);
  // A game launched while a chat was waiting stays a waiting game even if the
  // answer began while this chunk loaded: the answer-ready flow must still run.
  // A game opened from a link is a free game, whatever a chat is doing.
  const startedWhileWaiting = useRef(
    options.source === "waiting" || (options.source !== "link" && answer !== "none"),
  );
  const agentWorkingRef = useRef(answer === "waiting");
  const playStartedAt = useRef<number | null>(null);
  const answerAt = useRef<number | null>(null);
  const coarsePointer = useMemo(() => window.matchMedia?.("(pointer: coarse)").matches ?? false, []);

  // Latest mutation functions for callbacks created once.
  const startRef = useRef(startSession.mutateAsync);
  startRef.current = startSession.mutateAsync;
  const heartbeatRef = useRef(heartbeat.mutateAsync);
  heartbeatRef.current = heartbeat.mutateAsync;
  const finishRef = useRef(finishSession.mutateAsync);
  finishRef.current = finishSession.mutateAsync;

  const go = useCallback((next: Stage) => {
    stageRef.current = next;
    setStage(next);
  }, []);

  const announce = useCallback((text: string) => setAnnouncement(text), []);

  const pushBanner = useCallback((text: string, tone: Banner["tone"] = "info") => {
    const id = nextId.current++;
    setBanners((current) => [...current.slice(-2), { id, text, tone }]);
    window.setTimeout(() => setBanners((current) => current.filter((b) => b.id !== id)), BANNER_MS);
  }, []);

  const labels = useMemo<ArcadeCanvasLabels>(
    () => ({
      destroyed: {
        button: t_i18n("BUTTON DESTROYED"),
        link: t_i18n("LINK SEVERED"),
        icon: t_i18n("ICON VAPORIZED"),
        image: t_i18n("IMAGE SHATTERED"),
        text: t_i18n("TEXT SHREDDED"),
        input: t_i18n("INPUT OBLITERATED"),
        message: t_i18n("MESSAGE OBLITERATED"),
        navigation: t_i18n("NAVIGATION DOWN"),
        panel: t_i18n("PANEL WRECKED"),
        logo: t_i18n("LOGO DEFACED"),
        composer: t_i18n("COMPOSER SILENCED"),
      },
      fake: t_i18n("FAKE!"),
      payload: t_i18n("IGNORE PREVIOUS INSTRUCTIONS"),
      sudo: t_i18n("SUDO MODE"),
      combo: t_i18n("COMBO"),
      legendary: t_i18n("LEGENDARY SHOT"),
    }),
    [t_i18n],
  );

  // ── Leaving ────────────────────────────────────────────────────────────────

  const leave = useCallback(async () => {
    if (leavingRef.current) return;
    leavingRef.current = true;
    go("leaving");
    const game = gameRef.current;
    if (game) {
      try {
        await game.repair(!reducedMotion);
      } catch {
        /* destroy() on unmount cancels whatever is left */
      }
    }
    onClose();
  }, [go, onClose, reducedMotion]);

  const savedToast = useCallback(
    (run: RunResult, data: ArcadeFinishResult) => {
      const score = intl.formatNumber(run.score);
      const week = data.ranks.week;
      toast({
        title: data.personal_best ? t_i18n("Arcade: new personal best!") : t_i18n("Arcade"),
        description: !data.ranked
          ? t_i18n("{score} points. Unranked run.", { score })
          : week
            ? t_i18n("{score} points, #{rank} this week.", { score, rank: week })
            : t_i18n("{score} points recorded.", { score }),
        variant: "success",
      });
    },
    [intl, t_i18n, toast],
  );

  // The game-over screen shows why a score was not recorded; a player who
  // already left has no screen, and must not lose the reason with it.
  const notSavedToast = useCallback(
    (message: string) => {
      toast({ title: t_i18n("Arcade: score not recorded"), description: message, variant: "error" });
    },
    [t_i18n, toast],
  );

  const submit = useCallback(
    (run: RunResult) => {
      const session = sessionRef.current;
      if (!session) {
        setSave({ status: "offline" });
        return;
      }
      setSave({ status: "saving" });
      finishRef
        .current({
          sessionId: session.session_id,
          run: {
            score: run.score,
            wave: run.wave,
            kills: run.kills,
            shots: run.shots,
            hits: run.hits,
            ui_destroyed: run.uiDestroyed,
            bosses: run.bosses,
            max_combo: run.maxCombo,
            ufo_kills: run.ufoKills,
            duration_ms: run.durationMs,
            cheat: run.cheat,
            achievements: run.achievements,
          },
        })
        .then((data) => {
          setSave({ status: "saved", data });
          if (leavingRef.current) savedToast(run, data);
        })
        .catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          setSave({ status: "error", message });
          if (leavingRef.current) notSavedToast(message);
        });
    },
    [notSavedToast, savedToast],
  );

  const exitToAnswer = useCallback(() => {
    const game = gameRef.current;
    if (!game || game.isOver || stageRef.current === "over" || stageRef.current === "intro") {
      void leave();
      return;
    }
    const start = Math.max(playStartedAt.current ?? Date.now(), getWaitStartedAt() ?? 0);
    const end = answerAt.current ?? Date.now();
    game.missionComplete(Math.max(0, (end - start) / 1000));
    if (stageRef.current === "paused") {
      go("playing");
    }
  }, [getWaitStartedAt, go, leave]);

  const exitRef = useRef(exitToAnswer);
  exitRef.current = exitToAnswer;

  // ── Engine callbacks ───────────────────────────────────────────────────────

  const onEvent = useCallback(
    (event: ArcadeEvent) => {
      switch (event.type) {
        case "wave": {
          const text = event.boss
            ? t_i18n("Boss incoming: {boss}", { boss: t_i18n(BOSS_NAMES[event.boss]) })
            : t_i18n("Wave {wave}", { wave: event.wave });
          pushBanner(text, event.boss ? "bad" : "info");
          announce(text);
          break;
        }
        case "extraLife":
          pushBanner(t_i18n("Extra life!"), "good");
          break;
        case "lifeLost":
          announce(
            event.lives > 0
              ? t_i18n("Hit! {count, plural, one {# life} other {# lives}} left.", { count: event.lives })
              : t_i18n("Game over."),
          );
          break;
        case "powerUp":
          pushBanner(t_i18n(POWER_UP_NAMES[event.kind]), "good");
          break;
        case "achievement": {
          const achievement = achievementById(event.id);
          if (!achievement) break;
          const id = nextId.current++;
          setAchievementToasts((current) => [...current.slice(-2), { id, achievement: event.id }]);
          window.setTimeout(
            () => setAchievementToasts((current) => current.filter((toastItem) => toastItem.id !== id)),
            ACHIEVEMENT_TOAST_MS,
          );
          announce(t_i18n("Achievement unlocked: {name}", { name: t_i18n(achievement.title) }));
          break;
        }
        case "rivalPassed":
          pushBanner(
            t_i18n("You passed {callsign}. Now #{rank} this week!", { callsign: event.rival.callsign, rank: event.rank }),
            "gold",
          );
          break;
        case "personalBest":
          pushBanner(t_i18n("New personal best!"), "gold");
          break;
        case "cheat":
          pushBanner(
            event.code === "iddqd"
              ? t_i18n("God mode. This run will not be ranked.")
              : t_i18n("Full arsenal. This run will not be ranked."),
            "bad",
          );
          break;
        case "secret": {
          const texts: Record<typeof event.code, string> = {
            konami: t_i18n("Rainbow mode!"),
            hello: t_i18n("The invaders wave back."),
            answer: t_i18n("The answer is 42. The question is still loading."),
            xyzzy: t_i18n("Nothing happens."),
            sudo: t_i18n("sudo: this incident will be reported."),
          };
          pushBanner(texts[event.code], "gold");
          break;
        }
        case "nukeIncoming":
          pushBanner(t_i18n("Tactical nuke incoming!"), "bad");
          break;
        case "bossDefeated":
          pushBanner(t_i18n("{boss} defeated!", { boss: t_i18n(BOSS_NAMES[event.kind]) }), "gold");
          break;
        case "autoPaused":
          if (stageRef.current === "playing") go("paused");
          break;
      }
    },
    [announce, go, pushBanner, t_i18n],
  );

  const onGameOver = useCallback(
    (run: RunResult) => {
      if (run.untouched) {
        // Opened and left without playing: there is no run to record.
        void leave();
        return;
      }
      setResult(run);
      submit(run);
      if (run.reason === "dead") {
        setShowBoard(false);
        go("over");
        announce(t_i18n("Game over. {score} points.", { score: intl.formatNumber(run.score) }));
      } else {
        void leave();
      }
    },
    [announce, go, intl, leave, submit, t_i18n],
  );

  const eventRef = useRef(onEvent);
  eventRef.current = onEvent;
  const gameOverRef = useRef(onGameOver);
  gameOverRef.current = onGameOver;

  // ── Boot: session, then the game ───────────────────────────────────────────

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    previous?.blur();
    rootRef.current?.focus({ preventScroll: true });
    let cancelled = false;
    let stopFollowingAccent: () => void = () => {};
    // A game is a waiting game when an agent was working as it started,
    // whatever opened it (the panel, the palette, the Konami code, a replay),
    // and always when the waiting panel opened it.
    const sessionPromise = startRef
      .current({ mode: startedWhileWaiting.current ? "wait" : "free" })
      .then((session) => {
        if (!cancelled) {
          sessionRef.current = session;
          heartbeatSecondsRef.current = session.heartbeat_seconds;
        }
        return session;
      })
      .catch(() => {
        if (!cancelled) setOffline(true);
        return null;
      });

    void (async () => {
      await Promise.all([Promise.race([sessionPromise, sleep(SESSION_MAX_WAIT_MS)]), sleep(reducedMotion ? 150 : INTRO_MS)]);
      const canvas = canvasRef.current;
      const root = rootRef.current;
      if (cancelled || !canvas || !root) return;
      const session = sessionRef.current;
      let game: ArcadeGame;
      try {
        game = new ArcadeGame({
          canvas,
          overlayRoot: root,
          theme: resolveArcadeTheme(canvas),
          labels,
          reducedMotion,
          seasonal: seasonalTheme(new Date()),
          origin: options.origin ?? null,
          personalBest: session?.personal_best ?? 0,
          rivals: (session?.ladder ?? [])
            .filter((rival) => !rival.is_self)
            .map((rival) => ({ callsign: rival.callsign, score: rival.score, rank: rival.rank })),
          unlocked: new Set(session?.achievements ?? []),
          initialAchievements: options.source === "konami" ? ["konami_kid"] : [],
          callbacks: {
            onHud: (next) => {
              hudRef.current = next;
              setHud(next);
            },
            onEvent: (event) => eventRef.current(event),
            onGameOver: (run) => gameOverRef.current(run),
            onCrash: (error) => setCrash({ error }),
          },
        });
      } catch {
        onClose();
        return;
      }
      gameRef.current = game;
      // The platform mode can be set (or switched) after the game booted.
      stopFollowingAccent = followAccent(canvas, () => game.setTheme(resolveArcadeTheme(canvas)));
      try {
        game.audio.setMuted(mutedRef.current);
        game.audio.unlock();
        playStartedAt.current = Date.now();
        game.start();
      } catch (error) {
        setCrash({ error });
        return;
      }
      go("playing");
      window.setTimeout(() => setShowHint(false), HINT_MS);
    })();

    return () => {
      cancelled = true;
      stopFollowingAccent();
      gameRef.current?.destroy();
      gameRef.current = null;
      // The waiting panel's Take control button is gone once the answer
      // streams (and a click on its canvas left the focus on the page body):
      // the composer takes the focus back instead of the page body.
      const target =
        previous?.isConnected && previous !== document.body
          ? previous
          : document.querySelector<HTMLElement>('[data-arcade-target="composer"] textarea');
      target?.focus({ preventScroll: true });
    };
    // The game boots once per overlay; a relaunch remounts the overlay.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Resize ─────────────────────────────────────────────────────────────────

  useEffect(() => {
    const onResize = () => gameRef.current?.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // ── The agent's answer ─────────────────────────────────────────────────────

  useEffect(() => {
    agentWorkingRef.current = answer === "waiting";
    if (answer !== "ready" || !startedWhileWaiting.current || answerReadyRef.current) return;
    answerAt.current = Date.now();
    answerReadyRef.current = true;
    setAnswerReady(true);
    gameRef.current?.audio.play("answerReady");
    announce(t_i18n("Your answer is ready. Press Escape to read it."));
  }, [answer, announce, t_i18n]);

  const keepPlaying = useCallback(() => {
    overtimeRef.current = true;
    setOvertime(true);
    setCountdown(null);
    gameRef.current?.unlock("overtime");
    if (stageRef.current === "paused") {
      gameRef.current?.setPaused(false);
      go("playing");
    }
  }, [go]);

  // Countdown back to the answer while idle; patience while waiting.
  useEffect(() => {
    let deadline: number | null = null;
    let patienceMs = 0;
    const id = window.setInterval(() => {
      const game = gameRef.current;
      const current = stageRef.current;
      if (game && startedWhileWaiting.current && agentWorkingRef.current && current === "playing") {
        patienceMs += 250;
        if (patienceMs >= PATIENCE_MS) game.unlock("patience");
      }
      if (!answerReadyRef.current || overtimeRef.current || leavingRef.current) {
        deadline = null;
        setCountdown(null);
        return;
      }
      const idle = current === "paused" || current === "over" || !game || game.idleMs > ANSWER_IDLE_MS;
      if (!idle) {
        deadline = null;
        setCountdown(null);
        return;
      }
      const total = current === "over" ? GAME_OVER_COUNTDOWN_S : ANSWER_COUNTDOWN_S;
      if (deadline === null) deadline = Date.now() + total * 1000;
      const remaining = Math.ceil((deadline - Date.now()) / 1000);
      if (remaining <= 0) {
        deadline = null;
        setCountdown(null);
        exitRef.current();
        return;
      }
      setCountdown(remaining);
    }, 250);
    return () => window.clearInterval(id);
  }, []);

  // ── Live room ──────────────────────────────────────────────────────────────

  useEffect(() => {
    const beat = () => {
      const session = sessionRef.current;
      const game = gameRef.current;
      // Not live before the first input: an untouched game never reaches the
      // finish that takes the player out of the room.
      if (!session || !game || !game.isEngaged || stageRef.current !== "playing" || game.isPaused) return undefined;
      const snapshot = hudRef.current;
      // Returned: an answer that changes the cadence paces the next beat.
      return heartbeatRef
        .current({ sessionId: session.session_id, score: snapshot?.score ?? 0, wave: snapshot?.wave ?? 1 })
        .then((response) => {
          if (response.heartbeat_seconds !== undefined) heartbeatSecondsRef.current = response.heartbeat_seconds;
          setLive(response.live.filter((player) => !player.is_self));
        })
        // A refused or failed beat is a missed beat; a 429's Retry-After holds the next one.
        .catch((error: unknown) => ({ retryAfterSeconds: (error as { retryAfter?: number } | null)?.retryAfter }));
    };
    // The server sets the cadence; until it has said, its default.
    return startHeartbeat(beat, () => heartbeatIntervalMs(heartbeatSecondsRef.current));
  }, []);

  // ── Controls ───────────────────────────────────────────────────────────────

  const toggleMute = useCallback(() => {
    setMuted((current) => {
      const next = !current;
      mutedRef.current = next;
      writeMuted(next);
      gameRef.current?.audio.setMuted(next);
      return next;
    });
  }, []);

  const pause = useCallback(() => {
    const game = gameRef.current;
    if (!game || game.isOver || stageRef.current !== "playing") return;
    game.setPaused(true);
    go("paused");
  }, [go]);

  const resume = useCallback(() => {
    const game = gameRef.current;
    if (!game || stageRef.current !== "paused") return;
    setShowBoard(false);
    game.setPaused(false);
    go("playing");
    rootRef.current?.focus({ preventScroll: true });
  }, [go]);

  const quitGame = useCallback(() => {
    const game = gameRef.current;
    if (!game || game.isOver) {
      void leave();
      return;
    }
    game.quit();
  }, [leave]);

  const replay = useCallback(async () => {
    if (leavingRef.current) return;
    leavingRef.current = true;
    go("leaving");
    try {
      await gameRef.current?.repair(!reducedMotion);
    } catch {
      /* the remount destroys the rest */
    }
    onReplay();
  }, [go, onReplay, reducedMotion]);

  useEffect(() => {
    const trapFocus = (event: KeyboardEvent) => {
      const root = rootRef.current;
      if (!root) return;
      const focusables = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!root.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) {
        // The app's shortcuts (the palette...) must not act under the game.
        event.stopPropagation();
        return;
      }
      const game = gameRef.current;
      const current = stageRef.current;
      const key = event.key;
      const lower = key.toLowerCase();
      if (key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (event.repeat) return;
        const action = escapeAction(current, answerReadyRef.current);
        if (action === "pause") pause();
        else if (action === "quit") quitGame();
        else if (action === "answer") exitRef.current();
        else if (action === "leave") void leave();
        return;
      }
      if (current !== "playing") {
        // Menus: Tab, Enter and Space reach the buttons; nothing leaks to the app.
        event.stopPropagation();
        // A key still held from the game (autofire on Space) must not press
        // the button the menu just focused ("Play again", "Resume").
        if (event.repeat) {
          event.preventDefault();
          return;
        }
        if (key === "Tab") trapFocus(event);
        else if (lower === "p" && current === "paused") {
          event.preventDefault();
          resume();
        } else if (lower === "m") {
          event.preventDefault();
          toggleMute();
        } else if (lower === "k" && answerReadyRef.current && !overtimeRef.current) {
          event.preventDefault();
          keepPlaying();
        }
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (!game) return;
      game.audio.unlock();
      if (!event.repeat) game.typeKey(key);
      switch (lower) {
        case "arrowleft":
        case "a":
        case "q":
          game.setKey("left", true);
          break;
        case "arrowright":
        case "d":
          game.setKey("right", true);
          break;
        case " ":
        case "arrowup":
        case "w":
        case "z":
          game.setKey("fire", true);
          break;
        case "x":
        case "b":
          if (!event.repeat) game.launchMissile(null);
          break;
        case "p":
          pause();
          break;
        case "m":
          toggleMute();
          break;
        case "k":
          if (answerReadyRef.current && !overtimeRef.current) keepPlaying();
          break;
      }
    };

    const onKeyUp = (event: KeyboardEvent) => {
      const game = gameRef.current;
      if (!game) return;
      switch (event.key.toLowerCase()) {
        case "arrowleft":
        case "a":
        case "q":
          game.setKey("left", false);
          break;
        case "arrowright":
        case "d":
          game.setKey("right", false);
          break;
        case " ":
        case "arrowup":
        case "w":
        case "z":
          game.setKey("fire", false);
          break;
      }
    };

    const onBlur = () => {
      const game = gameRef.current;
      if (!game) return;
      game.setKey("left", false);
      game.setKey("right", false);
      game.setKey("fire", false);
      game.setPointerFire(false);
      pause();
    };

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", onBlur);
    };
  }, [keepPlaying, leave, pause, quitGame, resume, toggleMute]);

  const isPlayfield = (target: EventTarget) => target === canvasRef.current;

  // ── Render ─────────────────────────────────────────────────────────────────

  if (crash) throw crash.error instanceof Error ? crash.error : new Error(String(crash.error));

  const format = (n: number) => intl.formatNumber(n);

  const overlay = (
    <div
      ref={attachRoot}
      role="dialog"
      aria-modal="true"
      aria-label={t_i18n("Space Invaders arcade")}
      tabIndex={-1}
      className={cn(
        "fixed inset-0 z-[80] select-none touch-none overflow-hidden outline-none",
        stage === "playing" ? "cursor-crosshair" : "cursor-default",
      )}
      onPointerMove={(event) => {
        if (stageRef.current === "playing") gameRef.current?.setPointer(event.clientX);
      }}
      onPointerDown={(event) => {
        const game = gameRef.current;
        if (!game || stageRef.current !== "playing" || !isPlayfield(event.target)) return;
        game.audio.unlock();
        if (event.button === 2) {
          game.launchMissile({ x: event.clientX, y: event.clientY });
          return;
        }
        game.setPointer(event.clientX);
        game.setPointerFire(true);
        try {
          (event.target as Element).setPointerCapture(event.pointerId);
        } catch {
          /* capture is a nicety */
        }
      }}
      onPointerUp={() => gameRef.current?.setPointerFire(false)}
      onPointerCancel={() => gameRef.current?.setPointerFire(false)}
      onContextMenu={(event) => event.preventDefault()}
    >
      <canvas ref={canvasRef} aria-hidden className="absolute inset-0 h-full w-full" />

      {stage === "intro" && (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[color-mix(in_srgb,var(--bg-elevation-default)_40%,transparent)]">
          <p
            className={cn(
              "font-mono-plex text-3xl font-black tracking-[0.35em] text-accent drop-shadow-[0_0_18px_color-mix(in_srgb,_var(--color-accent)_calc(0.7_*_100%),_transparent)] sm:text-5xl",
              !reducedMotion && "animate-pulse",
            )}
          >
            {t_i18n("INVASION DETECTED")}
          </p>
          <p className="font-mono-plex content-compact uppercase tracking-[0.3em] text-default-secondary">{t_i18n("Taking control")}</p>
        </div>
      )}

      {hud && stage !== "intro" && (
        <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex h-12 items-center gap-4 border-b border-accent/15 bg-elevation-default px-4 font-mono-plex content-base backdrop-blur-sm">
          <div className="flex min-w-0 items-center gap-4">
            <span>
              <span className="content-caption text-default-secondary">{t_i18n("SCORE")} </span>
              <span className="font-bold tabular-nums text-accent">{format(hud.score)}</span>
            </span>
            <span className="hidden sm:inline">
              <span className="content-caption text-default-secondary">{t_i18n("HI")} </span>
              <span className="tabular-nums">{format(hud.hiScore)}</span>
            </span>
            {hud.multiplier > 1 && (
              <span className="rounded-sm bg-feedback-alert-primary/15 px-1.5 content-compact font-bold text-feedback-warning-tertiary light:text-feedback-warning-tertiary">
                x{hud.multiplier}
              </span>
            )}
          </div>

          <div className="flex flex-1 items-center justify-center">
            {hud.boss ? (
              <div className="flex w-full max-w-sm items-center gap-2">
                <span className="truncate content-compact font-bold uppercase text-feedback-error-primary">{t_i18n(BOSS_NAMES[hud.boss.kind])}</span>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-elevation-hover">
                  <div
                    className="h-full rounded-full bg-feedback-error-primary transition-[width] duration-200"
                    style={{ width: `${(hud.boss.hp / hud.boss.maxHp) * 100}%` }}
                  />
                </div>
              </div>
            ) : (
              <span className="content-compact tracking-widest text-default-secondary">
                {t_i18n("WAVE")} <span className="font-bold text-default-primary">{hud.wave}</span>
              </span>
            )}
          </div>

          <div className="flex items-center gap-3">
            <span className="flex items-center gap-0.5" aria-label={t_i18n("Lives")}>
              {Array.from({ length: Math.min(hud.lives, 6) }).map((_, i) => (
                <Heart key={i} className="h-3.5 w-3.5 fill-current text-feedback-error-primary" />
              ))}
            </span>
            <span className="hidden items-center gap-1 content-compact md:flex" aria-label={t_i18n("Missiles")}>
              <Rocket className="h-3.5 w-3.5 text-feedback-warning-primary light:text-feedback-warning-primary" />
              {hud.missiles}
            </span>
            {hud.shield && <Shield className="h-3.5 w-3.5 text-accent" aria-label={t_i18n("Shield")} />}
            {hud.powerUps.map((power) => (
              <span key={power.kind} className="hidden flex-col content-caption leading-tight lg:flex">
                <span className="uppercase text-default-secondary">{t_i18n(POWER_UP_NAMES[power.kind])}</span>
                <span className="h-0.5 w-full overflow-hidden rounded-sm bg-elevation-hover">
                  <span className="block h-full bg-accent" style={{ width: `${(power.remainingMs / power.totalMs) * 100}%` }} />
                </span>
              </span>
            ))}
            {hud.throttled && <span className="content-caption font-bold text-feedback-warning-primary light:text-feedback-warning-primary">429</span>}
            {hud.cheat && (
              <span className="rounded-sm bg-feedback-error-primary/15 px-1.5 content-caption font-bold uppercase text-feedback-error-primary">
                {t_i18n("Unranked")}
              </span>
            )}
            {hud.rival && (
              <span className="hidden items-center gap-1 content-compact xl:flex">
                <ChevronsUp className="h-3.5 w-3.5 text-feedback-warning-primary light:text-feedback-warning-primary" />
                <span className="text-default-secondary">{t_i18n("Next")}</span>
                <span className="font-bold text-accent">{hud.rival.callsign}</span>
                <span className="tabular-nums">{format(hud.rival.score)}</span>
              </span>
            )}
            {live.length > 0 && (
              <Tip container={tipContainer} content={live.map((p) => `${p.callsign}: ${format(p.score)}`).join(" · ")}>
                <span className="pointer-events-auto flex items-center gap-1 content-compact text-accent">
                  <Users className="h-3.5 w-3.5" />
                  {live.length}
                </span>
              </Tip>
            )}
            {offline && (
              <Tip container={tipContainer} content={t_i18n("The arcade could not reach the server: this game will not be saved.")}>
                <span className="pointer-events-auto flex items-center gap-1 content-caption text-default-secondary">
                  <CloudOff className="h-3.5 w-3.5" />
                  {t_i18n("Offline")}
                </span>
              </Tip>
            )}
            <div className="pointer-events-auto flex items-center gap-1">
              <Tip container={tipContainer} content={muted ? t_i18n("Sound on (M)") : t_i18n("Mute (M)")}>
                <button
                  type="button"
                  onClick={toggleMute}
                  aria-label={muted ? t_i18n("Sound on (M)") : t_i18n("Mute (M)")}
                  className="rounded-sm p-1.5 text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary"
                >
                  {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
                </button>
              </Tip>
              <Tip container={tipContainer} content={t_i18n("Pause (P)")}>
                <button
                  type="button"
                  onClick={() => (stageRef.current === "paused" ? resume() : pause())}
                  aria-label={t_i18n("Pause (P)")}
                  className="rounded-sm p-1.5 text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary"
                >
                  <Pause className="h-4 w-4" />
                </button>
              </Tip>
              <Tip container={tipContainer} content={t_i18n("Leave the arcade (Esc twice)")}>
                <button
                  type="button"
                  onClick={quitGame}
                  aria-label={t_i18n("Leave the arcade (Esc twice)")}
                  className="rounded-sm p-1.5 text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary"
                >
                  <LogOut className="h-4 w-4" />
                </button>
              </Tip>
            </div>
          </div>
        </div>
      )}

      {answerReady && (stage === "playing" || stage === "paused") && (
        <div className="absolute left-1/2 top-16 z-40 -translate-x-1/2">
          {overtime ? (
            <button
              type="button"
              onClick={exitToAnswer}
              className="flex items-center gap-2 rounded-sm border border-accent/40 bg-elevation-default px-3 py-1 content-compact text-accent shadow-overlay backdrop-blur-sm hover:bg-accent/10"
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              {t_i18n("Answer ready: Esc to read it")}
            </button>
          ) : (
            <div className="flex items-center gap-3 rounded-sm border border-accent/40 bg-elevation-default px-4 py-2.5 shadow-glow-accent-strong backdrop-blur-sm animate-fade-in">
              <CheckCircle2 className="h-5 w-5 shrink-0 text-accent" />
              <div className="content-base">
                <p className="font-semibold">{t_i18n("Your answer is ready")}</p>
                <p className="content-compact text-default-secondary">
                  {countdown !== null
                    ? t_i18n("Back to it in {seconds}s...", { seconds: countdown })
                    : t_i18n("Keep playing or go read it.")}
                </p>
              </div>
              <Button size="sm" onClick={exitToAnswer}>
                {t_i18n("Read it")} <kbd className="ml-1.5 content-caption opacity-70">Esc</kbd>
              </Button>
              <Button size="sm" variant="outline" onClick={keepPlaying}>
                {t_i18n("Keep playing")} <kbd className="ml-1.5 content-caption opacity-70">K</kbd>
              </Button>
            </div>
          )}
        </div>
      )}

      <div className="pointer-events-none absolute inset-x-0 top-[22%] z-10 flex flex-col items-center gap-2">
        {banners.map((banner) => (
          <p
            key={banner.id}
            className={cn(
              "animate-fade-in rounded-sm bg-elevation-default px-4 py-1 text-center font-mono-plex text-lg font-black uppercase tracking-[0.2em] backdrop-blur-sm sm:text-2xl",
              banner.tone === "info" && "text-accent",
              banner.tone === "good" && "text-feedback-success-primary light:text-feedback-success-primary",
              banner.tone === "bad" && "text-feedback-error-primary",
              banner.tone === "gold" && "text-feedback-warning-primary light:text-feedback-warning-primary",
            )}
          >
            {banner.text}
          </p>
        ))}
      </div>

      <div className="pointer-events-none absolute right-4 top-16 z-20 flex w-72 flex-col gap-2">
        {achievementToasts.map((item) => {
          const achievement = achievementById(item.achievement);
          if (!achievement) return null;
          return (
            <div
              key={item.id}
              className="flex items-center gap-3 rounded-sm border border-feedback-alert-primary/40 bg-elevation-default p-3 shadow-overlay backdrop-blur-sm animate-fade-in"
            >
              <Trophy className="h-5 w-5 shrink-0 text-feedback-warning-primary light:text-feedback-warning-primary" />
              <div className="min-w-0">
                <p className="content-caption font-semibold uppercase tracking-wider text-feedback-warning-primary light:text-feedback-warning-primary">
                  {t_i18n("Achievement unlocked")}
                </p>
                <p className="truncate content-base font-semibold">{t_i18n(achievement.title)}</p>
                <p className="truncate content-compact text-default-secondary">{t_i18n(achievement.description)}</p>
              </div>
            </div>
          );
        })}
      </div>

      {showHint && stage === "playing" && (
        <div className="pointer-events-none absolute inset-x-0 bottom-10 z-10 flex justify-center">
          <p className="rounded-sm bg-elevation-default px-4 py-1.5 font-mono-plex text-[11px] text-default-secondary backdrop-blur-sm animate-fade-in">
            {coarsePointer
              ? t_i18n("Drag to move and fire - tap the rocket for a missile")
              : t_i18n("Arrows or A/D to move - Space to fire - right-click or X for a missile - P or Esc to pause - Esc again to leave")}
          </p>
        </div>
      )}

      {hud?.awaitingPlayer && stage === "playing" && (
        <div className="pointer-events-none absolute inset-x-0 top-[46%] z-10 flex justify-center">
          <p
            className={cn(
              "rounded-full bg-elevation-default px-5 py-2 font-mono-plex content-base font-bold uppercase tracking-[0.3em] text-accent backdrop-blur-sm",
              !reducedMotion && "animate-pulse",
            )}
          >
            {coarsePointer ? t_i18n("Touch to start") : t_i18n("Move or fire to start")}
          </p>
        </div>
      )}

      {coarsePointer && stage === "playing" && (
        <div className="absolute bottom-16 right-4 z-10 flex flex-col gap-3">
          <button
            type="button"
            aria-label={t_i18n("Missile")}
            onClick={() => gameRef.current?.launchMissile(null)}
            className="flex h-14 w-14 items-center justify-center rounded-full border border-feedback-alert-primary/50 bg-elevation-default text-feedback-warning-primary backdrop-blur-sm light:text-feedback-warning-primary"
          >
            <Rocket className="h-6 w-6" />
          </button>
          <button
            type="button"
            aria-label={t_i18n("Pause (P)")}
            onClick={pause}
            className="flex h-12 w-12 items-center justify-center self-end rounded-full border border-elevation-subtle bg-elevation-default text-default-secondary backdrop-blur-sm"
          >
            <Pause className="h-5 w-5" />
          </button>
        </div>
      )}

      {stage === "paused" && (
        // A dimming scrim over the frozen playfield, never the opaque page surface.
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-scrim p-4 backdrop-blur-[2px]">
          <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-sm border border-accent/25 layer-1 bg-elevation-default p-6 shadow-glow-accent">
            <h2 className="text-center font-mono-plex text-3xl font-black tracking-[0.3em] text-accent">{t_i18n("PAUSED")}</h2>
            <p className="mt-2 text-center content-base text-default-secondary">{t_i18n(tip)}</p>
            <div className="mt-5 grid gap-2">
              <Button onClick={resume} autoFocus>
                <Play className="mr-2 h-4 w-4" />
                {t_i18n("Resume")} <kbd className="ml-auto content-caption opacity-70">P</kbd>
              </Button>
              <Button variant="secondary" onClick={() => setShowBoard((current) => !current)}>
                <Trophy className="mr-2 h-4 w-4" />
                {showBoard ? t_i18n("Hide the leaderboard") : t_i18n("Leaderboard")}
              </Button>
              <Button variant="secondary" onClick={toggleMute}>
                {muted ? <VolumeX className="mr-2 h-4 w-4" /> : <Volume2 className="mr-2 h-4 w-4" />}
                {muted ? t_i18n("Sound on") : t_i18n("Mute")} <kbd className="ml-auto content-caption opacity-70">M</kbd>
              </Button>
              <Button variant="outline" onClick={quitGame}>
                <LogOut className="mr-2 h-4 w-4" />
                {t_i18n("End the game and save the score")}
                {!answerReady && <kbd className="ml-auto content-caption opacity-70">Esc</kbd>}
              </Button>
            </div>
            {showBoard && (
              <div className="mt-5">
                <ArcadeLeaderboardPanel compact tooltipContainer={tipContainer} />
              </div>
            )}
            <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-1 content-compact text-default-secondary">
              <dt>{t_i18n("Move")}</dt>
              <dd className="font-mono-plex text-default-secondary">← → · A D · Q D</dd>
              <dt>{t_i18n("Fire (hold for autofire)")}</dt>
              <dd className="font-mono-plex text-default-secondary">Space · ↑ · W · Z</dd>
              <dt>{t_i18n("Missile")}</dt>
              <dd className="font-mono-plex text-default-secondary">{t_i18n("Right-click to aim")} · X</dd>
              <dt>{t_i18n("Pause / mute / leave")}</dt>
              <dd className="font-mono-plex text-default-secondary">P · M · Esc</dd>
            </dl>
          </div>
        </div>
      )}

      {stage === "over" && result && (
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-scrim p-4 backdrop-blur-[2px]">
          <div className="max-h-[88vh] w-full max-w-xl overflow-y-auto rounded-sm border border-accent/25 layer-1 bg-elevation-default p-6 shadow-glow-accent">
            <h2 className="text-center font-mono-plex text-3xl font-black tracking-[0.3em] text-feedback-error-primary">{t_i18n("GAME OVER")}</h2>
            <p className="mt-3 text-center font-mono-plex text-5xl font-black tabular-nums text-accent">{format(result.score)}</p>
            <div className="mt-2 flex min-h-6 items-center justify-center gap-2 content-base">
              {save.status === "saving" && (
                <span className="flex items-center gap-1.5 text-default-secondary">
                  <Spinner size="sm" tone="inherit" />
                  {t_i18n("Saving your score...")}
                </span>
              )}
              {save.status === "saved" && save.data.personal_best && (
                <span className="rounded-sm border border-transparent bg-feedback-alert-primary/15 px-2 py-0.5 content-compact font-bold uppercase text-feedback-warning-tertiary light:text-feedback-warning-tertiary">
                  {t_i18n("New personal best!")}
                </span>
              )}
              {save.status === "saved" && !save.data.ranked && (
                <span className="content-compact text-default-secondary">{t_i18n("Unranked run: a cheat code was used.")}</span>
              )}
              {save.status === "saved" && save.data.ranked && (
                <span className="flex gap-3 content-compact text-default-secondary">
                  {save.data.ranks.day && <span>{t_i18n("#{rank} today", { rank: save.data.ranks.day })}</span>}
                  {save.data.ranks.week && <span>{t_i18n("#{rank} this week", { rank: save.data.ranks.week })}</span>}
                  {save.data.ranks.all && <span>{t_i18n("#{rank} all time", { rank: save.data.ranks.all })}</span>}
                </span>
              )}
              {save.status === "offline" && (
                <span className="content-compact text-default-secondary">{t_i18n("Offline game: this score was not saved.")}</span>
              )}
              {save.status === "error" && <span className="content-compact text-feedback-error-primary">{save.message}</span>}
            </div>

            <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
              {(
                [
                  ["Wave", String(result.wave)],
                  ["Invaders", format(result.kills)],
                  ["Accuracy", result.shots ? intl.formatNumber(result.hits / result.shots, { style: "percent" }) : "-"],
                  ["Interface destroyed", format(result.uiDestroyed)],
                  ["Best combo", format(result.maxCombo)],
                  ["Saucers", format(result.ufoKills)],
                ] as [string, string][]
              ).map(([label, value]) => (
                <div key={label} className="rounded-sm bg-elevation-hover p-2">
                  <dt className="content-caption uppercase tracking-wider text-default-secondary">{t_i18n(label)}</dt>
                  <dd className="font-mono-plex text-lg font-bold">{value}</dd>
                </div>
              ))}
            </dl>

            {save.status === "saved" && save.data.new_achievements.length > 0 && (
              <div className="mt-4">
                <p className="content-compact font-semibold uppercase tracking-wider text-feedback-warning-primary light:text-feedback-warning-primary">
                  {t_i18n("Achievements unlocked")}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {save.data.new_achievements.map((id) => {
                    const achievement = achievementById(id);
                    return achievement ? (
                      <Tip key={id} container={tipContainer} content={t_i18n(achievement.description)}>
                        <span className="rounded-sm border border-feedback-alert-primary/40 px-2 py-0.5 content-compact">{t_i18n(achievement.title)}</span>
                      </Tip>
                    ) : null;
                  })}
                </div>
              </div>
            )}

            {answerReady && (
              <p className="mt-4 flex items-center justify-center gap-2 rounded-sm border border-accent/30 bg-accent/[0.06] p-2 content-base text-accent">
                <CheckCircle2 className="h-4 w-4" />
                {countdown !== null
                  ? t_i18n("Your answer is ready. Back to it in {seconds}s...", { seconds: countdown })
                  : t_i18n("Your answer is ready.")}
              </p>
            )}

            <div className="mt-5 grid gap-2 sm:grid-cols-3">
              <Button onClick={() => void replay()} autoFocus={!answerReady}>
                <RotateCcw className="mr-2 h-4 w-4" />
                {t_i18n("Play again")}
              </Button>
              <Button variant="secondary" onClick={() => setShowBoard((current) => !current)}>
                <Trophy className="mr-2 h-4 w-4" />
                {t_i18n("Leaderboard")}
              </Button>
              <Button variant={answerReady ? "default" : "outline"} onClick={() => void leave()} autoFocus={answerReady}>
                <LogOut className="mr-2 h-4 w-4" />
                {answerReady ? t_i18n("Read your answer") : t_i18n("Back to work")}
              </Button>
            </div>
            {showBoard && (
              <div className="mt-5">
                <ArcadeLeaderboardPanel compact tooltipContainer={tipContainer} />
              </div>
            )}
          </div>
        </div>
      )}

      {stage === "leaving" && (
        <div className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center">
          <p className="flex items-center gap-2 rounded-sm bg-elevation-default px-4 py-2 font-mono-plex content-base text-accent shadow-overlay backdrop-blur-sm">
            <Wrench className="h-4 w-4 animate-pulse" />
            {t_i18n("Repairing the interface...")}
          </p>
        </div>
      )}

      <span className="sr-only" aria-live="polite">
        {announcement}
      </span>
    </div>
  );

  return createPortal(overlay, document.body);
}
