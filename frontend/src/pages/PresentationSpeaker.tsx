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

/**
 * Speaker view — `/presentations/:id/speaker`.
 *
 * The window a presenter reads while the deck is on the room's screen. It is
 * a page of its own, NOT the viewer loaded into a popup: reveal's notes plugin
 * did that, and the "next slide" pane came up with the studio rail, the
 * toolbar and a chat conversation in it. Nothing here is the app shell
 * either — no sidebar, no header — because the window is glanced at in a dark
 * room from a metre away.
 *
 * What it shows, and why in this order:
 *   - the CURRENT slide, so the presenter knows what the room sees without
 *     turning round (and a "screen is black" notice when the deck is paused);
 *   - the speaker NOTES in large type the presenter sets once (`+` / `-`),
 *     the deck's inline markup rendered, bullets as bullets — this is the
 *     reason the window exists, so it takes the floor;
 *   - the NEXT slide, so the transition is spoken before it appears;
 *   - the STOPWATCH (auto-started on the first slide change, pausable), the
 *     wall clock, and PACING against the notes' own estimate — how far ahead or
 *     behind the plan the talk is, on this slide, with the remaining spoken
 *     time — optionally re-scaled to a target duration the presenter types in;
 *   - the transport: previous / next / go to slide / black screen, every one
 *     with a key, the same keys as the deck.
 *
 * The deck is FOLLOWED over `speaker-sync.ts`: any stage of this deck (the
 * viewer, a chat card) announces its position and this window drives it back,
 * so the presenter's clicker on the laptop moves both, and the keys pressed
 * here move the room's screen. With no stage connected it runs in REHEARSAL
 * mode — same notes, same timer, its own position — and says so.
 *
 * The deck is polled while open (`live`), and re-fetched at once when the
 * stage reports a newer revision, so an in-place edit made mid-rehearsal from
 * the studio lands in the notes and the previews without a reload.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Keyboard,
  Minus,
  MonitorOff,
  Pause,
  Play,
  Plus,
  Printer,
  Radio,
  RotateCcw,
  ScreenShare,
  X,
} from "lucide-react";

import { SpeakerClickerControl } from "@/components/presentation/ClickerControls";
import { deckMayHandleKey } from "@/components/presentation/deck-keys";
import { slideHeading } from "@/components/presentation/slide-label";
import { PollControls } from "@/components/presentation/PollControls";
import { DeckPollsContext, specHasPoll } from "@/components/presentation/poll-context";
import { SlidePreview } from "@/components/presentation/SlidePreview";
import { SpeakerNotes } from "@/components/presentation/SpeakerNotes";
import { useSpeakerFollower } from "@/components/presentation/speaker-sync";
import {
  audienceFeaturesOnOtherScreen,
  openAudienceWindow,
  openNotesHandout,
} from "@/components/presentation/speaker-url";
import {
  deckSpeakingSeconds,
  estimateSpeakingSeconds,
  speakingSecondsBefore,
} from "@/components/presentation/speaking-time";
import { useSpeakerTimer } from "@/components/presentation/useSpeakerTimer";
import { Chip, Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { PRESENTATIONS_KEY, useDeckPolls, usePresentation } from "@/hooks/api/usePresentationApi";
import type { ClickerCommandFrame } from "@/hooks/api/usePresentationClicker";
import { useFormatter } from "@/i18n";
import { cn, formatCountdown, formatDurationShort } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import "@/components/presentation/presentation.css";
import "@/components/presentation/speaker.css";

/** Notes type sizes the presenter steps through with `+` / `-` (x 22px). */
const NOTES_SCALES = [0.8, 0.9, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.4];
const NOTES_SCALE_KEY = "xtm-speaker-notes-scale";
/** The presenter's target length, per deck, in minutes (`""` = none). */
const targetKey = (id: string) => `xtm-speaker-target:${id}`;

/**
 * How far outside the current slide's planned window still counts as "on
 * pace" — a talk is not a metronome. The window itself already absorbs the
 * time the slide is planned to take; this is the slack on top of it.
 */
const ON_PACE_SECONDS = 30;

function readNotesScale(): number {
  try {
    const stored = Number(localStorage.getItem(NOTES_SCALE_KEY));
    return NOTES_SCALES.includes(stored) ? stored : 1;
  } catch {
    return 1;
  }
}

function readTarget(id: string): number | null {
  try {
    const raw = localStorage.getItem(targetKey(id));
    const minutes = raw ? Number(raw) : NaN;
    return Number.isFinite(minutes) && minutes > 0 ? minutes : null;
  } catch {
    return null;
  }
}

const TYPING_TARGETS = "input,textarea,select,[contenteditable]";

export default function PresentationSpeaker() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { t_i18n, intl } = useFormatter();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { data, isLoading, isError } = usePresentation(id, { live: true });
  const slides = useMemo(() => data?.spec.slides ?? [], [data]);
  const total = slides.length;

  const follower = useSpeakerFollower(id, total);
  const timer = useSpeakerTimer(id);
  // The timer object is rebuilt on every tick and the follower on every
  // render; the effects below depend on the stable pieces only, or the key
  // listener would re-subscribe four times a second.
  const { untouched: timerUntouched, start: startTimer, toggle: toggleTimer } = timer;
  const { next: goNext, prev: goPrev, goTo, toggleBlackout } = follower;
  const [notesScale, setNotesScale] = useState(readNotesScale);
  const [target, setTarget] = useState<number | null>(() => (id ? readTarget(id) : null));
  const [keysOpen, setKeysOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const index = Math.min(follower.index, Math.max(0, total - 1));
  const slide = slides[index];
  const next = slides[index + 1];

  // Live poll results on the current slide (#10157): the same 2 s query as the
  // deck, one cache entry, read only while the deck holds a poll block.
  const hasPoll = useMemo(() => specHasPoll(slides), [slides]);
  const polls = useDeckPolls(id, hasPoll);
  const pollsValue = useMemo(
    () => (hasPoll && id ? { presentationId: id, data: polls.data } : null),
    [hasPoll, id, polls.data],
  );

  // The tab title names the window on the presenter's second screen.
  useEffect(() => {
    if (!data?.title) return;
    const previous = document.title;
    document.title = `${data.title} - ${t_i18n("Speaker view")}`;
    return () => {
      document.title = previous;
    };
  }, [data?.title, t_i18n]);

  // Wall clock, once a second (the stopwatch keeps its own cadence).
  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, []);

  // The stage renders a revision this window has not seen: fetch it now
  // rather than on the next poll, so the notes match the slide on screen.
  useEffect(() => {
    if (!data || follower.stageRevision === null || follower.stageRevision <= data.revision) return;
    void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id] });
  }, [data, follower.stageRevision, id, queryClient]);

  // Auto-start: the clock starts on the first slide CHANGE — the presenter
  // moved, so the talk began. Not on the first sync with the stage, which also
  // changes the index (from this window's 0 to wherever the deck is), and not
  // on a deck edit that removes slides and shifts the index with them. Every
  // way of moving from this window lands here — a command the stage obeyed, a
  // rehearsal move of the local index — so nothing else starts the clock, and
  // a move that goes nowhere (Left on the first slide, Right on the last)
  // changes nothing and starts nothing.
  const previous = useRef<{ index: number; total: number; connected: boolean } | null>(null);
  useEffect(() => {
    const before = previous.current;
    previous.current = { index, total, connected: follower.connected };
    if (!before || before.index === index) return;
    if (before.connected !== follower.connected || before.total !== total) return;
    if (timerUntouched) startTimer();
  }, [follower.connected, index, startTimer, timerUntouched, total]);

  useEffect(() => {
    try {
      localStorage.setItem(NOTES_SCALE_KEY, String(notesScale));
    } catch {
      // Preference only.
    }
  }, [notesScale]);

  useEffect(() => {
    if (!id) return;
    try {
      if (target === null) localStorage.removeItem(targetKey(id));
      else localStorage.setItem(targetKey(id), String(target));
    } catch {
      // Preference only.
    }
  }, [id, target]);

  const stepNotes = useCallback((direction: 1 | -1) => {
    setNotesScale((current) => {
      const at = NOTES_SCALES.indexOf(current);
      const nextAt = Math.min(NOTES_SCALES.length - 1, Math.max(0, (at < 0 ? 2 : at) + direction));
      return NOTES_SCALES[nextAt];
    });
  }, []);

  // The keys of the deck, redirected at the stage — plus the window's own.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target instanceof Element && target.closest(TYPING_TARGETS)) return;
      if (!deckMayHandleKey(event)) return;
      const key = event.key;
      const handled = (() => {
        switch (key) {
          case "ArrowRight":
          case "PageDown":
          case "n":
          case "N":
            goNext();
            return true;
          case " ":
            if (event.shiftKey) goPrev();
            else goNext();
            return true;
          case "ArrowLeft":
          case "PageUp":
          case "p":
          case "P":
            goPrev();
            return true;
          case "Home":
            goTo(0);
            return true;
          case "End":
            goTo(total - 1);
            return true;
          case "b":
          case "B":
          case ".":
            toggleBlackout();
            return true;
          case "t":
          case "T":
            toggleTimer();
            return true;
          case "+":
          case "=":
            stepNotes(1);
            return true;
          case "-":
          case "_":
            stepNotes(-1);
            return true;
          case "?":
          case "F1":
            setKeysOpen((open) => !open);
            return true;
          default:
            return false;
        }
      })();
      if (handled) event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [goNext, goPrev, goTo, stepNotes, toggleBlackout, toggleTimer, total]);

  if (isLoading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-elevation-default">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !data || !slide) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base font-medium">{t_i18n("This presentation is not available.")}</p>
        <p className="max-w-md content-base text-default-secondary">
          {t_i18n(
            "It may have been deleted, or it has not been shared with you. The link alone does not grant access. Ask whoever sent it to share the presentation with you.",
          )}
        </p>
      </div>
    );
  }

  // A page has no speaker view — no bridge, no `S`, no entry point to this
  // URL (see `kind`); a typed URL lands on the page's own reader rather than
  // on a "deck" of pages in rehearsal mode.
  if (data.kind !== "deck") {
    return <Navigate to={`/presentations/${data.id}`} replace />;
  }

  // ── Pacing ────────────────────────────────────────────────────────────────
  // The plan is the notes' own estimate; a target duration re-scales it (a
  // 34-minute plan in a 45-minute slot is allowed to run 32% slower). The plan
  // gives the CURRENT slide a window — from the spoken time of the slides
  // before it to that plus its own — and the talk is on pace while the clock
  // is inside that window: reaching a slide before its window opens is being
  // ahead, still being on it after the window closed is being behind. Measured
  // against the window's start alone, every second spent on a slide would
  // count as falling behind — which is what speaking is.
  const planned = deckSpeakingSeconds(slides);
  const plannedBudget = target !== null ? target * 60 : planned;
  const ratio = planned > 0 ? plannedBudget / planned : 1;
  const thisSlide = estimateSpeakingSeconds(slide.notes) * ratio;
  const windowStart = planned > 0 ? speakingSecondsBefore(slides, index) * ratio : null;
  const windowEnd = windowStart === null ? null : windowStart + thisSlide;
  const remainingPlan = planned > 0 ? deckSpeakingSeconds(slides.slice(index)) * ratio : 0;
  const aheadBy = windowStart === null || timer.untouched ? null : windowStart - timer.elapsed;
  const behindBy = windowEnd === null || timer.untouched ? null : timer.elapsed - windowEnd;
  const paceState: "ahead" | "on" | "behind" | "over" | null =
    aheadBy === null || behindBy === null
      ? null
      : plannedBudget > 0 && timer.elapsed > plannedBudget
        ? "over"
        : behindBy > ON_PACE_SECONDS
          ? "behind"
          : aheadBy > ON_PACE_SECONDS
            ? "ahead"
            : "on";
  const paceLabel =
    paceState === null
      ? planned > 0
        ? t_i18n("Pacing starts with the clock")
        : t_i18n("Add speaker notes to get a pacing estimate")
      : paceState === "over"
        ? t_i18n("Over time by {time}", { time: formatDurationShort(timer.elapsed - plannedBudget) })
        : paceState === "behind"
          ? t_i18n("Behind by {time}", { time: formatDurationShort(behindBy ?? 0) })
          : paceState === "ahead"
            ? t_i18n("Ahead by {time}", { time: formatDurationShort(aheadBy ?? 0) })
            : t_i18n("On pace");
  // What a screen reader hears: the STATE, when it changes — never the ticking
  // seconds of the label above, which a live region would read out all talk.
  const paceAnnouncement =
    paceState === "over"
      ? t_i18n("Over time")
      : paceState === "behind"
        ? t_i18n("Behind the plan")
        : paceState === "ahead"
          ? t_i18n("Ahead of the plan")
          : paceState === "on"
            ? t_i18n("On pace")
            : "";
  const progress = plannedBudget > 0 ? Math.min(1, timer.elapsed / plannedBudget) : 0;
  // The current slide's window on the same track: the fill should sit in it.
  const planWindow =
    plannedBudget > 0 && windowStart !== null && windowEnd !== null
      ? { start: Math.min(1, windowStart / plannedBudget), end: Math.min(1, windowEnd / plannedBudget) }
      : null;
  const remainingBudget = plannedBudget > 0 ? plannedBudget - timer.elapsed : null;

  const clock = intl.formatTime(now, { hour: "2-digit", minute: "2-digit" });
  // Both header status tokens are the design system's Chip (UX round 1 of
  // xtm-one #10645, B1 and B2): a live link reads as a low-severity chip with
  // its icon, no link as a neutral chip with no dot; the Chip pairs its label
  // and wash at 4.5:1 in both themes.
  const status = follower.connected ? (
    <Chip
      label={t_i18n("Following the presentation")}
      severity="low"
      startIcon={<Radio size={16} aria-hidden="true" />}
    />
  ) : (
    <Chip label={t_i18n("Rehearsal - no presentation window connected")} severity="neutral" />
  );

  // The state of the audience link (xtm-one #10552, slice A3): the presenter
  // sees whether the window they share is live before the meeting sees it.
  const audienceStatus = follower.audienceConnected ? (
    <Chip
      label={t_i18n("Audience window live")}
      severity="low"
      startIcon={<ScreenShare size={16} aria-hidden="true" />}
      data-testid="presentation.speaker.audience-status"
    />
  ) : (
    <Chip
      label={t_i18n("No audience window")}
      severity="neutral"
      data-testid="presentation.speaker.audience-status"
    />
  );

  // With two screens and the Window Management permission, the audience window
  // is placed over the other screen; on one screen it is the plain window to
  // share. A pop-up blocked after the permission prompt is reported as blocked.
  const openAudience = async () => {
    const features = await audienceFeaturesOnOtherScreen();
    const opened = features ? openAudienceWindow(data.id, features) : openAudienceWindow(data.id);
    if (!opened) {
      toast({
        title: t_i18n("Audience window blocked"),
        description: t_i18n("The browser blocked the audience window. Allow pop-ups for this site and try again."),
        variant: "error",
      });
    }
  };

  // A co-presenter's command relayed by the server ("Hand the clicker",
  // xtm-one #10552 B2) moves the deck exactly like this window's own keys.
  const onClickerCommand = (frame: ClickerCommandFrame) => {
    if (frame.command === "next") goNext();
    else if (frame.command === "prev") goPrev();
    else if (frame.index !== null) goTo(Math.min(Math.max(0, frame.index), Math.max(0, total - 1)));
  };

  const openDeckWindow = () => window.open(withBasePath(`/presentations/${data.id}`), "_blank", "noopener");
  // A window a script opened (the popup the stage or the library opens) closes
  // itself; the same-tab fallback and a typed URL are not such a window, and
  // the browser refuses `close()` without a word. Then the button goes to the
  // deck instead — a Close that does nothing is a broken button.
  const closeSpeaker = () => {
    window.close();
    if (!window.closed) navigate(`/presentations/${data.id}`);
  };

  return (
    <div className="speaker-root" data-testid="presentation.speaker">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className="flex items-center gap-3 px-5 py-3">
        <div className="min-w-0 flex-1">
          <h1 className="truncate font-heading content-base font-medium text-default-primary">{data.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {status}
            {audienceStatus}
            {!follower.connected && (
              <button
                type="button"
                onClick={openDeckWindow}
                className="inline-flex items-center gap-1 text-[11px] text-accent underline-offset-2 hover:underline"
              >
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
                {t_i18n("Open the presentation to drive it from here")}
              </button>
            )}
          </div>
        </div>
        <div className="speaker-clock text-default-secondary" aria-label={t_i18n("Current time")}>
          {clock}
        </div>
        <div className="flex items-center gap-1">
          {/* The hub of "Present to a meeting" (xtm-one #10552): the audience
              window to share is opened, or brought back at the current slide,
              from here, so the presenter never switches windows to restart. */}
          <Tip content={t_i18n("Open or bring back the slides-only window to share in a meeting")}>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 gap-1.5"
              onClick={() => void openAudience()}
            >
              <ScreenShare className="h-4 w-4 shrink-0" aria-hidden="true" />
              {follower.audienceConnected ? t_i18n("Bring back audience window") : t_i18n("Open audience window")}
            </Button>
          </Tip>
          <SpeakerClickerControl presentationId={data.id} slideIndex={index} onCommand={onClickerCommand} />
          <Tip content={t_i18n("Notes handout (print)")}>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 px-0"
              onClick={() => openNotesHandout(data.id)}
              aria-label={t_i18n("Notes handout (print)")}
            >
              <Printer className="h-4 w-4" />
            </Button>
          </Tip>
          <Tip content={t_i18n("Keyboard shortcuts")}>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 px-0"
              onClick={() => setKeysOpen(true)}
              aria-label={t_i18n("Keyboard shortcuts")}
            >
              <Keyboard className="h-4 w-4" />
            </Button>
          </Tip>
          <Tip content={t_i18n("Close the speaker view")}>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 px-0"
              onClick={closeSpeaker}
              aria-label={t_i18n("Close the speaker view")}
            >
              <X className="h-4 w-4" />
            </Button>
          </Tip>
        </div>
      </header>

      {/* ── Body ───────────────────────────────────────────────────────────── */}
      <main className="speaker-main">
        <section className="speaker-column" aria-label={t_i18n("Current slide and notes")}>
          <div className="speaker-current">
            <DeckPollsContext.Provider value={pollsValue}>
            <SlidePreview
              presentation={data}
              slide={slide}
              index={index}
              fit="contain"
              overlay={
                follower.blackout ? (
                  <div className="speaker-blackout" role="status">
                    <MonitorOff className="mr-2 h-4 w-4" aria-hidden="true" />
                    {t_i18n("Screen is black")}
                  </div>
                ) : null
              }
            />
            </DeckPollsContext.Provider>
          </div>
          {id ? <PollControls presentationId={id} slide={slide} /> : null}
          <div className="flex items-center justify-between gap-3 px-0.5">
            <div className="min-w-0">
              <p className="truncate text-[11px] uppercase tracking-wider text-default-secondary">
                {t_i18n("Slide {current} of {total}", { current: index + 1, total })}
              </p>
              <p className="truncate content-base text-default-secondary">{slideHeading(slide) || t_i18n("Untitled slide")}</p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <span className="mr-1 text-[11px] text-default-secondary">
                {thisSlide > 0
                  ? t_i18n("About {time} for this slide", { time: formatDurationShort(thisSlide) })
                  : t_i18n("No notes")}
              </span>
              <Tip content={t_i18n("Smaller notes")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 w-7 px-0"
                  onClick={() => stepNotes(-1)}
                  disabled={notesScale === NOTES_SCALES[0]}
                  aria-label={t_i18n("Smaller notes")}
                >
                  <Minus className="h-3.5 w-3.5" />
                </Button>
              </Tip>
              <Tip content={t_i18n("Larger notes")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 w-7 px-0"
                  onClick={() => stepNotes(1)}
                  disabled={notesScale === NOTES_SCALES[NOTES_SCALES.length - 1]}
                  aria-label={t_i18n("Larger notes")}
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </Tip>
            </div>
          </div>
          <div
            className="speaker-notes-well"
            data-testid="presentation.speaker.notes"
            // The presenter's type size, as a CSS variable the notes inherit.
            style={{ "--notes-scale": notesScale } as CSSProperties}
          >
            <SpeakerNotes notes={slide.notes} empty={t_i18n("No speaker notes for this slide.")} />
          </div>
        </section>

        <aside className="speaker-column speaker-side" aria-label={t_i18n("Next slide and timer")}>
          <div className="speaker-next">
            <p className="mb-1.5 text-[11px] uppercase tracking-wider text-default-secondary">{t_i18n("Next")}</p>
            {next ? (
              <>
                {/* The next slide reads the same live poll state as the current one:
                    without it a poll one slide ahead would show no voters (#10157). */}
                <DeckPollsContext.Provider value={pollsValue}>
                  <SlidePreview presentation={data} slide={next} index={index + 1} />
                </DeckPollsContext.Provider>
                <p className="mt-1.5 truncate content-compact text-default-secondary">{slideHeading(next) || t_i18n("Untitled slide")}</p>
              </>
            ) : (
              <div className="flex aspect-video items-center justify-center rounded-sm border border-dashed border-elevation-subtle-soft content-compact text-default-secondary">
                {t_i18n("End of presentation")}
              </div>
            )}
          </div>

          <div className="speaker-timer rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[11px] uppercase tracking-wider text-default-secondary">{t_i18n("Elapsed")}</p>
                <p
                  className="speaker-stopwatch mt-1 text-default-primary"
                  data-paused={!timer.running && !timer.untouched ? "true" : undefined}
                  aria-live="off"
                >
                  {formatCountdown(timer.elapsed)}
                </p>
              </div>
              <div className="flex items-center gap-1">
                <Tip content={timer.running ? t_i18n("Pause the clock") : t_i18n("Start the clock")}>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-8 w-8 px-0"
                    onClick={timer.toggle}
                    aria-label={timer.running ? t_i18n("Pause the clock") : t_i18n("Start the clock")}
                    aria-pressed={timer.running}
                  >
                    {timer.running ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                  </Button>
                </Tip>
                <Tip content={t_i18n("Reset the clock")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 w-8 px-0"
                    onClick={timer.reset}
                    disabled={timer.untouched}
                    aria-label={t_i18n("Reset the clock")}
                  >
                    <RotateCcw className="h-4 w-4" />
                  </Button>
                </Tip>
              </div>
            </div>

            <div className="mt-4 space-y-2">
              <div className="speaker-pace-track" aria-hidden="true">
                {/* The widths and offsets are live data, hence inline. */}
                <div
                  className="speaker-pace-fill"
                  data-state={paceState === "behind" ? "behind" : paceState === "over" ? "over" : undefined}
                  style={{ width: `${progress * 100}%` }}
                />
                {planWindow !== null && (
                  <div
                    className="speaker-pace-window"
                    style={{
                      left: `${planWindow.start * 100}%`,
                      width: `${Math.max(0, planWindow.end - planWindow.start) * 100}%`,
                    }}
                  />
                )}
              </div>
              <div className="flex items-center justify-between gap-3 content-compact">
                <span className="sr-only" role="status">
                  {paceAnnouncement}
                </span>
                <span
                  className={cn(
                    "font-medium",
                    paceState === "on" && "text-feedback-success-primary",
                    paceState === "ahead" && "text-accent",
                    paceState === "behind" && "text-feedback-warning-primary",
                    paceState === "over" && "text-feedback-error-primary",
                    paceState === null && "text-default-secondary",
                  )}
                >
                  {paceLabel}
                </span>
                {remainingBudget !== null && !timer.untouched && (
                  <span className="tabular-nums text-default-secondary">
                    {remainingBudget >= 0
                      ? t_i18n("{time} left", { time: formatDurationShort(remainingBudget) })
                      : t_i18n("{time} over", { time: formatDurationShort(-remainingBudget) })}
                  </span>
                )}
              </div>
            </div>

            <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 content-compact">
              <div>
                <dt className="text-[11px] uppercase tracking-wider text-default-secondary">{t_i18n("Notes left")}</dt>
                <dd className="mt-0.5 tabular-nums text-default-secondary">
                  {remainingPlan > 0 ? formatDurationShort(remainingPlan) : "-"}
                </dd>
              </div>
              <div>
                <dt className="text-[11px] uppercase tracking-wider text-default-secondary">
                  {t_i18n("Planned")}
                </dt>
                <dd className="mt-0.5 tabular-nums text-default-secondary">{planned > 0 ? formatDurationShort(planned) : "-"}</dd>
              </div>
              <div className="col-span-2">
                <dt className="text-[11px] uppercase tracking-wider text-default-secondary">
                  <label htmlFor="speaker-target">{t_i18n("Target length (minutes)")}</label>
                </dt>
                <dd className="mt-1 flex items-center gap-2">
                  <Input aria-label={t_i18n("Target length (minutes)")}
                    id="speaker-target"
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={600}
                    step={1}
                    placeholder={planned > 0 ? String(Math.ceil(planned / 60)) : "45"}
                    value={target ?? ""}
                    onChange={(e) => {
                      const minutes = Number(e.target.value);
                      setTarget(e.target.value && Number.isFinite(minutes) && minutes > 0 ? minutes : null);
                    }}
                    className="h-8 w-24 content-compact"
                  />
                  <span className="text-[11px] text-default-secondary">
                    {target !== null
                      ? t_i18n("Pacing is scaled to your target.")
                      : t_i18n("Pacing follows the notes' estimate.")}
                  </span>
                </dd>
              </div>
            </dl>
          </div>
        </aside>
      </main>

      {/* ── Transport ──────────────────────────────────────────────────────── */}
      <footer className="flex items-center gap-2 border-t border-elevation-subtle-soft px-5 py-2.5">
        <Tip content={t_i18n("Previous slide")}>
          <Button
            variant="outline"
            size="sm"
            className="h-9 gap-1 px-3"
            onClick={follower.prev}
            disabled={index <= 0}
            aria-label={t_i18n("Previous slide")}
          >
            <ChevronLeft className="h-4 w-4" />
            <Kbd>←</Kbd>
          </Button>
        </Tip>
        <Tip content={t_i18n("Next slide")}>
          <Button
            size="sm"
            className="h-9 gap-1 px-4"
            onClick={follower.next}
            disabled={index >= total - 1}
            aria-label={t_i18n("Next slide")}
            data-testid="presentation.speaker.next"
          >
            {t_i18n("Next")}
            <ChevronRight className="h-4 w-4" />
          </Button>
        </Tip>

        <div className="mx-2 min-w-0 flex-1">
          <SearchableSelect
            aria-label={t_i18n("Go to slide")}
            className="max-w-md"
            sort={false}
            options={slides.map((entry, at) => ({
              value: String(at),
              label: `${at + 1}. ${slideHeading(entry) || t_i18n("Untitled slide")}`,
            }))}
            value={String(index)}
            onValueChange={(value) => follower.goTo(Number(value))}
          />
        </div>

        <Tip
          content={
            follower.connected
              ? follower.blackout
                ? t_i18n("Show the slide again")
                : t_i18n("Black screen")
              : t_i18n("Black screen (needs a connected presentation window)")
          }
        >
          <Button
            variant="ghost"
            size="sm"
            className={cn("h-9 gap-1.5 px-3", follower.blackout && "bg-accent/15 text-accent")}
            onClick={follower.toggleBlackout}
            disabled={!follower.connected}
            aria-pressed={follower.blackout}
            aria-label={t_i18n("Black screen")}
          >
            <MonitorOff className="h-4 w-4" />
            <Kbd>B</Kbd>
          </Button>
        </Tip>
      </footer>

      <Dialog open={keysOpen} onOpenChange={setKeysOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t_i18n("Keyboard shortcuts")}</DialogTitle>
            <DialogDescription>
              {t_i18n("The deck's keys work here too: what you press in this window moves the presentation.")}
            </DialogDescription>
          </DialogHeader>
          <dl className="divide-y divide-[var(--border-elevation-subtle)] content-base">
            {[
              { label: t_i18n("Next slide"), keys: [["→"], ["Space"], ["N"], ["PgDn"]] },
              { label: t_i18n("Previous slide"), keys: [["←"], ["P"], ["PgUp"], ["Shift", "Space"]] },
              { label: t_i18n("First / last slide"), keys: [["Home"], ["End"]] },
              { label: t_i18n("Black screen"), keys: [["B"], ["."]] },
              { label: t_i18n("Start / pause the clock"), keys: [["T"]] },
              { label: t_i18n("Larger / smaller notes"), keys: [["+"], ["-"]] },
              { label: t_i18n("Keyboard shortcuts"), keys: [["?"], ["F1"]] },
            ].map((row) => (
              <div key={row.label} className="flex items-center justify-between gap-4 py-2">
                <dt className="text-default-primary">{row.label}</dt>
                <dd className="flex flex-wrap items-center justify-end gap-1.5">
                  {row.keys.map((combo, i) => (
                    <span key={combo.join("+")} className="inline-flex items-center gap-1">
                      {i > 0 && <span className="content-caption text-default-secondary">{t_i18n("or")}</span>}
                      <span className="inline-flex items-center gap-0.5">
                        {combo.map((key) => (
                          <Kbd key={key}>{key}</Kbd>
                        ))}
                      </span>
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </DialogContent>
      </Dialog>
    </div>
  );
}
