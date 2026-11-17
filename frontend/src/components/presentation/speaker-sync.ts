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
 * The bridge between a presented deck (the STAGE) and its speaker window.
 *
 * The speaker view is its own route in its own window — never the viewer page
 * loaded into an iframe, which is what showed the studio rail and the toolbar
 * in the "next slide" pane. The two windows talk over one `BroadcastChannel`
 * per deck (`xtm-deck:<presentation id>`), same origin, no parent/child
 * relationship required:
 *
 * - the stage posts its `state` (slide index, total, revision) on every move,
 *   on every `hello` it hears, and as a heartbeat, so a speaker window opened
 *   late syncs at once and one whose stage vanished without a `bye` (a killed
 *   tab) notices within `STAGE_TIMEOUT_MS`; every frame says WHY it was sent
 *   (`reason`), because a heartbeat and a move must not be read the same way;
 * - the speaker window posts `command`s (`prev` / `next` / `goto`) addressed to
 *   the stage it FOLLOWS — the one that MOVED last — so when the same deck is
 *   open in a chat card and in the viewer, the one the presenter is using is
 *   the one shown and driven, and the two never fight over the keys. Only a
 *   move switches the followed stage: were a heartbeat enough, two idle stages
 *   would swap the window's slide every few seconds. The other stages are
 *   remembered, so closing the followed one hands over to the freshest of them
 *   instead of dropping into rehearsal;
 * - with no stage connected the speaker window runs in REHEARSAL mode: the same
 *   navigation, notes and timer, driving its own position.
 *
 * `useSpeakerStage` is mounted by every `DeckStage` (chat card and viewer
 * alike); `useSpeakerFollower` by the speaker view. Both degrade to no-ops
 * where `BroadcastChannel` is missing.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { randomId } from "@/lib/random-id";

import type { DeckController } from "./deck-controller";

const SPEAKER_CHANNEL_PREFIX = "xtm-deck:";
/** How often a stage re-posts its state with nothing changed.
 * @public
 */
export const STAGE_HEARTBEAT_MS = 4000;
/** Silence after which the speaker window considers its stage gone.
 */
const STAGE_TIMEOUT_MS = 11000;

/** @public */
export function speakerChannelName(presentationId: string): string {
  return `${SPEAKER_CHANNEL_PREFIX}${presentationId}`;
}

interface StageState {
  index: number;
  total: number;
  revision: number;
  blackout: boolean;
}

/**
 * Why a stage posted its state: it `move`d (slide or blackout changed — the
 * presenter acted on THAT stage), it answered a `hello` or just appeared, it
 * re-synced something else (revision, readiness, length), or it is the
 * heartbeat. Only a `move` makes a speaker window switch stages.
 */
type StateReason = "move" | "hello" | "sync" | "heartbeat";

/** What the speaker window may ask of the stage: move, or black the room's screen. */
type SpeakerCommand = "prev" | "next" | "goto" | "blackout";

/**
 * What a stage is: the deck as staged anywhere (`stage`: the viewer, a chat
 * card) or the AUDIENCE window of "Present to a meeting" (xtm-one #10552,
 * slice A2). The audience window mirrors every move of the deck's other
 * stages and they mirror its moves, so the room's share and the presenter's
 * viewer never drift whichever window the speaker view drives.
 */
export type StageRole = "stage" | "audience";

type SpeakerMessage =
  | ({ type: "state"; stageId: string; reason: StateReason; role?: StageRole } & StageState)
  | { type: "hello"; from: "stage" | "speaker"; id: string }
  | { type: "bye"; from: "stage" | "speaker"; id: string }
  | { type: "command"; to: string; speakerId: string; command: SpeakerCommand; index?: number };

function newId(): string {
  return randomId();
}

function openChannel(presentationId: string): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(speakerChannelName(presentationId));
  } catch {
    return null;
  }
}

function isSpeakerMessage(data: unknown): data is SpeakerMessage {
  return typeof data === "object" && data !== null && typeof (data as { type?: unknown }).type === "string";
}

type StateFrame = Extract<SpeakerMessage, { type: "state" }>;

/**
 * A `state` frame the follower can use. The channel is same-origin and keyed on
 * the deck, so the sender is our own code — but a stage from another build of
 * the app can be open in a stale tab, and a slide index that is not a number
 * would become a `NaN` position and an empty speaker window.
 */
function isStateFrame(data: SpeakerMessage): data is StateFrame {
  if (data.type !== "state") return false;
  const frame = data as Partial<StateFrame>;
  return (
    typeof frame.stageId === "string" &&
    typeof frame.index === "number" &&
    Number.isFinite(frame.index) &&
    typeof frame.total === "number" &&
    Number.isFinite(frame.total) &&
    typeof frame.revision === "number" &&
    typeof frame.blackout === "boolean"
  );
}

// ── Stage side ────────────────────────────────────────────────────────────────

interface SpeakerStageOptions {
  presentationId: string | undefined;
  controller: DeckController;
  revision: number;
  /** Off for a page (portrait document): nothing to present, nothing to follow. */
  enabled?: boolean;
  /** `audience` for the audience window (see `StageRole`). */
  role?: StageRole;
}

/**
 * Whether a stage of role `self` takes over the move another stage announced:
 * the audience window follows every stage, a stage follows the audience
 * window only (two viewers of one deck keep their own position, as before).
 */
export function mirrorsMove(self: StageRole, sender: StageRole | undefined): boolean {
  return self === "audience" || sender === "audience";
}

/**
 * Publish a deck's position to its speaker window(s) and obey their commands.
 * A stage never learns whether anyone listens — it announces and answers, and
 * the window that follows it is the one that decides whom to drive.
 */
export function useSpeakerStage({
  presentationId,
  controller,
  revision,
  enabled = true,
  role = "stage",
}: SpeakerStageOptions): void {
  const [stageId] = useState(newId);
  const channelRef = useRef<BroadcastChannel | null>(null);
  const controllerRef = useRef(controller);
  controllerRef.current = controller;
  // Read through a ref so `postState` stays stable: were it rebuilt on every
  // revision, the channel effect below would close and reopen the channel on
  // each in-place edit — a `bye` then a `hello` the speaker window shows as a
  // blink of rehearsal mode.
  const revisionRef = useRef(revision);
  revisionRef.current = revision;
  const { index, total, blackout, ready } = controller;

  const post = useCallback((message: SpeakerMessage) => {
    try {
      channelRef.current?.postMessage(message);
    } catch {
      // A closed channel throws; the effect below is about to replace it.
    }
  }, []);

  const postState = useCallback(
    (reason: StateReason) => {
      const current = controllerRef.current;
      if (!current.ready) return;
      post({
        type: "state",
        stageId,
        reason,
        index: current.index,
        total: current.total,
        revision: revisionRef.current,
        blackout: current.blackout,
        role,
      });
    },
    [post, role, stageId],
  );

  useEffect(() => {
    if (!enabled || !presentationId) return;
    const channel = openChannel(presentationId);
    if (!channel) return;
    channelRef.current = channel;
    const onMessage = (event: MessageEvent) => {
      const data: unknown = event.data;
      if (!isSpeakerMessage(data)) return;
      if (data.type === "hello" && data.from === "speaker") {
        postState("hello");
        return;
      }
      if (
        data.type === "state" &&
        data.stageId !== stageId &&
        data.reason === "move" &&
        isStateFrame(data) &&
        mirrorsMove(role, data.role)
      ) {
        const current = controllerRef.current;
        if (current.ready && current.index !== data.index) current.goTo(data.index);
        if (current.ready && current.blackout !== data.blackout) current.toggleBlackout();
        return;
      }
      if (data.type === "command" && data.to === stageId) {
        const current = controllerRef.current;
        if (data.command === "next") current.next();
        else if (data.command === "prev") current.prev();
        else if (data.command === "goto" && typeof data.index === "number") current.goTo(data.index);
        else if (data.command === "blackout") current.toggleBlackout();
      }
    };
    channel.addEventListener("message", onMessage);
    // A closed tab or a full navigation unloads the document without running
    // this effect's cleanup, so the `bye` is also sent on `pagehide` — the
    // speaker window then hands over (or drops to rehearsal) at once instead of
    // waiting out `STAGE_TIMEOUT_MS`. A second `bye` on unmount is harmless.
    const bye = () => post({ type: "bye", from: "stage", id: stageId });
    window.addEventListener("pagehide", bye);
    post({ type: "hello", from: "stage", id: stageId });
    postState("hello");
    const heartbeat = window.setInterval(() => postState("heartbeat"), STAGE_HEARTBEAT_MS);
    return () => {
      window.clearInterval(heartbeat);
      window.removeEventListener("pagehide", bye);
      bye();
      channel.removeEventListener("message", onMessage);
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, [enabled, presentationId, post, postState, role, stageId]);

  // Every move, blackout toggle, in-place edit (revision) or readiness change
  // is announced at once — the heartbeat only covers silence. A move is a
  // slide or blackout that differs from what this stage last announced; the
  // first frame of a stage that just became ready, a new revision or a new
  // length are a `sync`, which never steals a speaker window from the stage
  // the presenter is actually using.
  const announcedRef = useRef<{ index: number; blackout: boolean } | null>(null);
  useEffect(() => {
    const before = announcedRef.current;
    const moved = before !== null && (before.index !== index || before.blackout !== blackout);
    if (ready) announcedRef.current = { index, blackout };
    postState(moved ? "move" : "sync");
  }, [index, total, blackout, ready, revision, postState]);
}

// ── Speaker side ──────────────────────────────────────────────────────────────

interface SpeakerFollower {
  /** Zero-based index of the slide being talked over. */
  index: number;
  /** True while a stage is heard from; false is rehearsal mode. */
  connected: boolean;
  /**
   * True while an AUDIENCE window of the deck ("Present to a meeting") is heard
   * from, whichever stage the window follows: the state of the link the speaker
   * view shows next to its "Audience window" control (xtm-one #10552, A3).
   */
  audienceConnected: boolean;
  /** The revision the connected stage renders (`null` in rehearsal). */
  stageRevision: number | null;
  blackout: boolean;
  prev: () => void;
  next: () => void;
  goTo: (index: number) => void;
  /** Black the room's screen (and back). A no-op in rehearsal: no room. */
  toggleBlackout: () => void;
}

/**
 * Follow the stage presenting `presentationId`; drive it back through the
 * returned actions. `total` is the deck length as THIS window knows it, used to
 * clamp rehearsal-mode navigation.
 */
export function useSpeakerFollower(presentationId: string | undefined, total: number): SpeakerFollower {
  const [speakerId] = useState(newId);
  const channelRef = useRef<BroadcastChannel | null>(null);
  const [stage, setStage] = useState<{ id: string; state: StageState } | null>(null);
  const [localIndex, setLocalIndex] = useState(0);
  // The followed stage, mirrored in refs the message handler and the watchdog
  // read synchronously; `lastHeardRef` is when THAT stage last spoke — another
  // stage's heartbeat must not keep a dead one alive. Its last frame is kept
  // too (`followedStateRef`), so a stage the window switches AWAY from can be
  // filed among the others with the frame it was last seen with.
  const followedRef = useRef<string | null>(null);
  const followedStateRef = useRef<StageState | null>(null);
  const lastHeardRef = useRef(0);
  // The other live stages of this deck, freshest frame each: the hand-over
  // list when the followed stage says bye or falls silent.
  const othersRef = useRef(new Map<string, { state: StageState; heard: number }>());
  // The audience windows heard from, by stage id, with when they last spoke.
  const audienceRef = useRef(new Map<string, number>());
  const [audienceConnected, setAudienceConnected] = useState(false);

  const post = useCallback((message: SpeakerMessage) => {
    try {
      channelRef.current?.postMessage(message);
    } catch {
      // Closed channel — nothing to reach.
    }
  }, []);

  useEffect(() => {
    if (!presentationId) return;
    const channel = openChannel(presentationId);
    if (!channel) return;
    channelRef.current = channel;
    // One Map for the hook's lifetime; read once so the cleanup clears the
    // same instance it filled.
    const others = othersRef.current;
    const audiences = audienceRef.current;
    const syncAudience = () => setAudienceConnected(audiences.size > 0);

    const follow = (id: string, state: StageState, heard: number) => {
      const previous = followedRef.current;
      // Switching on a move: the stage left behind is still live, so it joins
      // the hand-over list with its last frame rather than being forgotten
      // until its next heartbeat re-lists it — were the new stage to close
      // inside that gap, the window would fall to rehearsal beside a live deck.
      // (A stage that is GONE never reaches this branch: `dropFollowed` clears
      // the refs before it picks the successor.)
      if (previous !== null && previous !== id && followedStateRef.current) {
        others.set(previous, { state: followedStateRef.current, heard: lastHeardRef.current });
      }
      followedRef.current = id;
      followedStateRef.current = state;
      lastHeardRef.current = heard;
      others.delete(id);
      setStage({ id, state });
      // The stage's position becomes the rehearsal position too, so losing
      // the stage mid-talk keeps the presenter on the slide they were on.
      setLocalIndex(state.index);
    };
    // The followed stage is gone: the freshest other stage still within the
    // heartbeat window takes over, otherwise rehearsal.
    const dropFollowed = () => {
      followedRef.current = null;
      followedStateRef.current = null;
      const now = Date.now();
      let successor: { id: string; state: StageState; heard: number } | null = null;
      for (const [id, entry] of others) {
        if (now - entry.heard > STAGE_TIMEOUT_MS) others.delete(id);
        else if (!successor || entry.heard > successor.heard) successor = { id, ...entry };
      }
      if (successor) follow(successor.id, successor.state, successor.heard);
      else setStage(null);
    };

    const onMessage = (event: MessageEvent) => {
      const data: unknown = event.data;
      if (!isSpeakerMessage(data)) return;
      if (data.type === "state") {
        if (!isStateFrame(data)) return;
        const now = Date.now();
        if (data.role === "audience") {
          audiences.set(data.stageId, now);
          syncAudience();
        }
        const state: StageState = {
          index: data.index,
          total: data.total,
          revision: data.revision,
          blackout: data.blackout,
        };
        const followed = followedRef.current;
        // A stage without a `reason` is an older build: read as a move, the
        // one reading every frame was.
        const moved = typeof data.reason !== "string" || data.reason === "move";
        if (followed === null || followed === data.stageId || moved) {
          follow(data.stageId, state, now);
        } else {
          others.set(data.stageId, { state, heard: now });
        }
      } else if (data.type === "bye" && data.from === "stage") {
        others.delete(data.id);
        if (audiences.delete(data.id)) syncAudience();
        if (followedRef.current === data.id) dropFollowed();
      }
    };
    channel.addEventListener("message", onMessage);
    post({ type: "hello", from: "speaker", id: speakerId });
    const watchdog = window.setInterval(() => {
      const now = Date.now();
      let pruned = false;
      for (const [id, heard] of audiences) {
        if (now - heard > STAGE_TIMEOUT_MS) pruned = audiences.delete(id) || pruned;
      }
      if (pruned) syncAudience();
      if (followedRef.current !== null && Date.now() - lastHeardRef.current > STAGE_TIMEOUT_MS) {
        dropFollowed();
      }
    }, 1000);
    return () => {
      window.clearInterval(watchdog);
      post({ type: "bye", from: "speaker", id: speakerId });
      channel.removeEventListener("message", onMessage);
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
      followedRef.current = null;
      followedStateRef.current = null;
      others.clear();
      audiences.clear();
    };
  }, [presentationId, post, speakerId]);

  const clamp = useCallback(
    (value: number) => Math.max(0, Math.min(Math.max(0, total - 1), value)),
    [total],
  );

  const command = useCallback(
    (name: SpeakerCommand, index?: number) => {
      if (stage) {
        post({ type: "command", to: stage.id, speakerId, command: name, index });
        return;
      }
      if (name === "blackout") return;
      setLocalIndex((current) => {
        if (name === "next") return clamp(current + 1);
        if (name === "prev") return clamp(current - 1);
        return clamp(index ?? current);
      });
    },
    [clamp, post, speakerId, stage],
  );

  return {
    index: stage ? clamp(stage.state.index) : clamp(localIndex),
    connected: stage !== null,
    audienceConnected,
    stageRevision: stage ? stage.state.revision : null,
    blackout: stage ? stage.state.blackout : false,
    prev: useCallback(() => command("prev"), [command]),
    next: useCallback(() => command("next"), [command]),
    goTo: useCallback((index: number) => command("goto", index), [command]),
    toggleBlackout: useCallback(() => command("blackout"), [command]),
  };
}
