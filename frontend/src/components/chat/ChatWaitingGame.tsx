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

import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, IconButton } from "@filigran/design-system";
// Trophy: the design system has no trophy glyph (ICON_NAMES 1.5.0); kept until its
// design arbitration (audit table of xtm-one #10911, issue #10910).
import { Trophy } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";
import { useArcade } from "@/components/arcade/arcade-context";
import { waitingMessage } from "@/components/arcade/engine/easter-eggs";
import { followAccent, resolveAccent } from "@/components/arcade/theme";
import { useArcadeLeaderboard } from "@/hooks/api/useArcadeApi";
import { useFormatter } from "@/i18n";
import { useWaitingArcade } from "./waiting-arcade";
import {
  OFFER_ACCENT_INTERACTIVE,
  OFFER_ACCENT_TEXT,
  OFFER_ICON_REST,
  OFFER_PLATE,
  OFFER_TEXT_REST,
} from "./waiting-offer-style";

/** Plain (no-game) message rotation cadence. */
const PLAIN_ROTATE_MS = 2600;

/** Canvas height in CSS px — room for the target row + the gliding ship. */
const GAME_HEIGHT = 70;

/** Chunky monospace for the retro arcade feel + even letter spacing. */
const arcadeFont = (px: number): string =>
  `700 ${px}px 'IBM Plex Mono', 'Courier New', ui-monospace, monospace`;

/**
 * Classic 11x8 "crab" invader, two leg frames toggled while it glides — the
 * silhouette reads instantly as a Space Invader. `1` = filled pixel.
 */
const INVADER_FRAMES: string[][] = [
  [
    "00100000100",
    "00010001000",
    "00111111100",
    "01101110110",
    "11111111111",
    "10111111101",
    "10100000101",
    "00011011000",
  ],
  [
    "00100000100",
    "10010001001",
    "10111111101",
    "11101110111",
    "11111111111",
    "00111111100",
    "00100000100",
    "01000000010",
  ],
];

interface Letter {
  char: string;
  x: number;
  w: number;
  alive: boolean;
}

interface Bullet {
  x: number;
  y: number;
}

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
}

interface AttractGame {
  destroy: () => void;
  /** The little invader's position in the viewport - where the arcade breaks out from. */
  shipPosition: () => { x: number; y: number };
}

/**
 * Self-contained canvas attract mode: the invader shoots the waiting message
 * away letter by letter, then the next message fades in. Owns its own rAF loop
 * and resize handling.
 */
function createInvaderGame(canvas: HTMLCanvasElement, nextMessage: () => string): AttractGame {
  const maybeCtx = canvas.getContext("2d");
  if (!maybeCtx) return { destroy: () => {}, shipPosition: () => ({ x: 0, y: 0 }) };
  const ctx: CanvasRenderingContext2D = maybeCtx;

  let raf = 0;
  let cssW = 0;
  const cssH = GAME_HEIGHT;
  let fontPx = 13;
  let accent = resolveAccent(canvas);
  const stopFollowingAccent = followAccent(canvas, (value) => {
    accent = value;
  });

  let text = nextMessage();
  let letters: Letter[] = [];
  let targetIndex = -1;
  let bullets: Bullet[] = [];
  let particles: Particle[] = [];
  let shipX = 0;
  let cooldown = 0;
  let clearedAt = 0;
  let legFrame = 0;
  let legTimer = 0;
  let last = performance.now();

  const PX = 2;
  const SPRITE_W = 11 * PX;
  const SPRITE_H = 8 * PX;
  const shipTop = cssH - SPRITE_H - 4;
  const letterY = Math.round(cssH * 0.42);

  function firstAlive(from: number): number {
    for (let i = from; i < letters.length; i++) {
      if (letters[i].alive) return i;
    }
    return -1;
  }

  function layout(): void {
    if (cssW <= 0) return;

    // Shrink the font until the message fits the available width.
    fontPx = 13;
    for (; fontPx >= 8; fontPx--) {
      ctx.font = arcadeFont(fontPx);
      if (ctx.measureText(text).width <= cssW - 16) break;
    }
    ctx.font = arcadeFont(fontPx);

    const widths = Array.from(text).map((ch) => ctx.measureText(ch).width);
    const total = widths.reduce((a, b) => a + b, 0);
    let x = (cssW - total) / 2;
    letters = Array.from(text).map((ch, i) => {
      const lx = x;
      x += widths[i];
      return { char: ch, x: lx, w: widths[i], alive: ch.trim().length > 0 };
    });
    targetIndex = firstAlive(0);
    bullets = [];
    particles = [];
    cooldown = 0;
    clearedAt = 0;
    if (shipX <= 0) shipX = cssW / 2;
  }

  function letterCenter(i: number): number {
    return letters[i].x + letters[i].w / 2;
  }

  function update(dt: number): void {
    const dtf = Math.min(dt / 16.6667, 3);

    legTimer += dt;
    if (legTimer > 320) {
      legTimer = 0;
      legFrame ^= 1;
    }

    if (targetIndex === -1) {
      // Message fully cleared — brief pause, then load the next one.
      if (clearedAt === 0) clearedAt = performance.now();
      else if (performance.now() - clearedAt > 650) {
        text = nextMessage();
        layout();
      }
    } else {
      const targetX = letterCenter(targetIndex);
      shipX += (targetX - shipX) * Math.min(0.16 * dtf, 1);
      cooldown -= dt;
      if (
        bullets.length === 0 &&
        cooldown <= 0 &&
        Math.abs(shipX - targetX) < 4
      ) {
        bullets.push({ x: shipX, y: shipTop });
        cooldown = 130;
      }
    }

    for (let i = bullets.length - 1; i >= 0; i--) {
      bullets[i].y -= 2.6 * dtf;
      if (bullets[i].y <= letterY) {
        // Hit: detonate the current target letter and advance.
        if (targetIndex !== -1) {
          const cx = letterCenter(targetIndex);
          letters[targetIndex].alive = false;
          for (let p = 0; p < 7; p++) {
            const ang = (Math.PI * 2 * p) / 7 + Math.random();
            const spd = 0.6 + Math.random() * 1.4;
            particles.push({
              x: cx,
              y: letterY,
              vx: Math.cos(ang) * spd,
              vy: Math.sin(ang) * spd,
              life: 1,
            });
          }
          targetIndex = firstAlive(targetIndex + 1);
        }
        bullets.splice(i, 1);
      }
    }

    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x += p.vx * dtf;
      p.y += p.vy * dtf;
      p.life -= 0.045 * dtf;
      if (p.life <= 0) particles.splice(i, 1);
    }
  }

  /** The accent at an opacity: globalAlpha, since canvas fillStyle does not
   *  parse color-mix() consistently across browsers. */
  function withAlpha(a: number): string {
    ctx.globalAlpha = a;
    return accent;
  }

  function draw(): void {
    ctx.clearRect(0, 0, cssW, cssH);

    ctx.font = arcadeFont(fontPx);
    ctx.textBaseline = "middle";
    ctx.fillStyle = withAlpha(0.85);
    for (const l of letters) {
      if (l.alive) ctx.fillText(l.char, l.x, letterY);
    }

    ctx.globalAlpha = 1;
    ctx.fillStyle = accent;
    for (const b of bullets) {
      ctx.fillRect(b.x - 1, b.y, 2, 7);
    }

    for (const p of particles) {
      ctx.fillStyle = withAlpha(Math.max(p.life, 0) * 0.9);
      ctx.fillRect(p.x - 1.5, p.y - 1.5, 3, 3);
    }

    const frame = INVADER_FRAMES[legFrame];
    const ox = Math.round(shipX - SPRITE_W / 2);
    ctx.globalAlpha = 1;
    ctx.fillStyle = accent;
    for (let r = 0; r < frame.length; r++) {
      const row = frame[r];
      for (let c = 0; c < row.length; c++) {
        if (row[c] === "1") ctx.fillRect(ox + c * PX, shipTop + r * PX, PX, PX);
      }
    }
  }

  // Scrolled out of view, the canvas is not updated or drawn: a transcript
  // being read while the agent works keeps this panel far above the fold.
  let onScreen = true;
  let io: IntersectionObserver | null = null;
  if (typeof IntersectionObserver !== "undefined") {
    io = new IntersectionObserver((entries) => {
      onScreen = entries.some((entry) => entry.isIntersecting);
    });
    io.observe(canvas);
  }

  function loop(now: number): void {
    const dt = now - last;
    last = now;
    if (!document.hidden && onScreen && cssW > 0) {
      update(dt);
      draw();
    }
    raf = requestAnimationFrame(loop);
  }

  function applySize(): void {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cssW = rect.width;
    canvas.width = Math.max(1, Math.round(cssW * dpr));
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layout();
  }

  // ResizeObserver isn't available in every environment (older browsers, some
  // test runners); fall back to a window resize listener so constructing the
  // game never throws and breaks the waiting panel.
  let ro: ResizeObserver | null = null;
  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(applySize);
    ro.observe(canvas);
  } else if (typeof window !== "undefined") {
    window.addEventListener("resize", applySize);
  }
  applySize();
  raf = requestAnimationFrame(loop);

  return {
    destroy: () => {
      cancelAnimationFrame(raf);
      io?.disconnect();
      stopFollowingAccent();
      if (ro) {
        ro.disconnect();
      } else if (typeof window !== "undefined") {
        window.removeEventListener("resize", applySize);
      }
    },
    shipPosition: () => {
      const rect = canvas.getBoundingClientRect();
      return { x: rect.left + shipX, y: rect.top + shipTop };
    },
  };
}

interface ChatWaitingGameProps {
  /** The chat surface waiting here (`useArcadeWaitSignal`): a game taken over
   *  from this panel waits for that surface's answer. */
  waitSource?: string;
}

/**
 * Waiting experience shown below the status bubble during longer waits:
 * dynamic rotating messages with an optional Space Invader that shoots the
 * message letters away one by one. When the OS requests reduced motion the
 * messages simply rotate as plain dimmed text, so the "dynamic loading
 * messages" feedback always stands on its own. There is no per-user switch to
 * hide the game: the gamepad icon means play, and only play.
 *
 * The self-playing invader is the attract mode of the waiting arcade:
 * clicking it (or its gamepad Take control button) takes control and opens the full-screen
 * game over the whole app, with a cross-user leaderboard.
 *
 * It is the content `XtmChatPanel` hands the `@filigran/chatbot` waiting panel
 * (`renderWaitingPanel`), which indents it under the status bubble and keeps
 * the thread's end in view while the reader follows it.
 */
export function ChatWaitingGame({ waitSource }: ChatWaitingGameProps) {
  const { t_i18n, intl } = useFormatter();
  const arcade = useArcade();
  // The offer (gate, reduced motion) and the launch (`useWaitingArcade`).
  const { reducedMotion, playMode, canTakeControl, takeControl } = useWaitingArcade({ waitSource });
  const [message, setMessage] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const attractRef = useRef<AttractGame | null>(null);
  const indexRef = useRef(0);

  const { data: board } = useArcadeLeaderboard("week", { enabled: canTakeControl, limit: 1 });
  const leader = board?.entries[0] ?? null;
  const livePlayers = (board?.live ?? []).filter((player) => !player.is_self).length;

  const nextMessage = useCallback(() => {
    const text = t_i18n(waitingMessage(indexRef.current, Math.random()));
    indexRef.current += 1;
    return text;
  }, [t_i18n]);

  // Warm the arcade's chunk while the player makes up their mind.
  useEffect(() => {
    if (canTakeControl) arcade.prefetch();
  }, [canTakeControl, arcade]);

  // Plain-text rotation under reduced motion.
  useEffect(() => {
    if (playMode) return;
    setMessage(nextMessage());
    const id = window.setInterval(() => setMessage(nextMessage()), PLAIN_ROTATE_MS);
    return () => window.clearInterval(id);
  }, [playMode, nextMessage]);

  // Canvas attract mode when the game is on - paused while the arcade runs.
  useEffect(() => {
    if (!playMode || arcade.active) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const game = createInvaderGame(canvas, nextMessage);
    attractRef.current = game;
    return () => {
      game.destroy();
      attractRef.current = null;
    };
  }, [playMode, arcade.active, nextMessage]);

  // The invaders break out from where the attract-mode ship stands.
  const takeControlFromShip = useCallback(
    () => takeControl(attractRef.current?.shipPosition() ?? null),
    [takeControl],
  );

  return (
    // `relative` is load-bearing, not cosmetic: the `sr-only` span below is
    // `position: absolute` (Tailwind's sr-only recipe). Without a positioned
    // ancestor here it resolves against the nearest positioned ancestor — the
    // chat message list's `relative` wrapper, which sits OUTSIDE the
    // `overflow-y-auto` scroll viewport. At the bottom of a long thread the
    // span then lands far down that wrapper's coordinate space, inflating its
    // scrollHeight and bleeding past the fixed-height chat shell into a
    // page-level scrollbar (the "empty space below the composer" bug). Keeping
    // it `relative` traps the span inside this self-contained, scroll-clipped
    // block so the waiting panel can never grow the page.
    <div className="relative">
      {/* Announce the waiting state to assistive tech exactly once. The
          rotating messages are decorative (aria-hidden below) so a polite
          live region would otherwise re-announce every ~2.6s rotation. */}
      <span className="sr-only" role="status">
        {t_i18n("Still working on your request")}
      </span>
      <div
        className={cn(
          "group relative overflow-hidden rounded-sm bg-accent/[0.03]",
          !reducedMotion && "animate-fade-in",
        )}
      >
        <div aria-hidden>
          {playMode ? (
            <canvas
              ref={canvasRef}
              onClick={takeControlFromShip}
              className={cn("block w-full", canTakeControl && "cursor-pointer")}
              style={{ height: GAME_HEIGHT }}
            />
          ) : (
            <div className="flex items-center" style={{ height: GAME_HEIGHT }}>
              <span
                key={message}
                className={cn(
                  "px-3 content-compact text-default-secondary",
                  !reducedMotion && "animate-fade-in",
                )}
              >
                {message}
                <span
                  className={cn(
                    "ml-0.5 inline-block w-1 h-3 align-middle bg-accent/60",
                    !reducedMotion && "animate-pulse",
                  )}
                />
              </span>
            </div>
          )}
        </div>
        {canTakeControl && (
          <div
            className={cn(
              "pointer-events-none absolute bottom-1 left-2 flex items-center gap-2 px-1 font-mono-plex content-caption",
              OFFER_PLATE,
              OFFER_TEXT_REST,
            )}
          >
            {leader && (
              <span>
                {t_i18n("HI")} {intl.formatNumber(leader.best_score)} · <span className={OFFER_ACCENT_TEXT}>{leader.callsign}</span>
              </span>
            )}
            {livePlayers > 0 && (
              <span className={cn("flex items-center gap-0.5", OFFER_ACCENT_TEXT)}>
                <Icon name="users" size={16} />
                {t_i18n("{count, plural, one {# playing} other {# playing}}", { count: livePlayers })}
              </span>
            )}
          </div>
        )}
        {canTakeControl && (
          <div className={cn("absolute top-1 right-1 flex items-center gap-0.5", OFFER_PLATE)}>
            <Tip content={t_i18n("Play Space Invaders while the agent works")}>
              {/* data-ds-exempt: the arcade plate's label button, drawn in the game's
                  own offer colours over the canvas (waiting-offer-style.ts). */}
              <button
                type="button"
                data-ds-exempt="arcade"
                onClick={takeControlFromShip}
                aria-label={t_i18n("Take control of the waiting game")}
                className={cn(
                  "flex items-center gap-1 rounded-sm px-1.5 py-0.5 font-mono-plex content-caption font-bold uppercase tracking-wider",
                  OFFER_TEXT_REST,
                  OFFER_ACCENT_INTERACTIVE,
                  // A click on the canvas takes control too: pointing at the panel lights this button.
                  "group-hover:text-[color:color-mix(in_srgb,var(--color-accent)_60%,var(--text-default-primary))]",
                )}
              >
                <Icon name="gamepad-2" size={16} />
                {t_i18n("Take control")}
              </button>
            </Tip>
            <Tip content={t_i18n("Arcade leaderboard")}>
              <IconButton
                type="button"
                variant="default"
                priority="tertiary"
                size="sm"
                onClick={arcade.openLeaderboard}
                aria-label={t_i18n("Arcade leaderboard")}
                className={cn(OFFER_ICON_REST, OFFER_ACCENT_INTERACTIVE)}
                icon={<Trophy className="h-3.5 w-3.5" aria-hidden />}
              />
            </Tip>
          </div>
        )}
      </div>
    </div>
  );
}
