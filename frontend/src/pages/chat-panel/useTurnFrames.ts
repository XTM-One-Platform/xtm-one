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
import { useQueryClient } from "@tanstack/react-query";
import type { ChatStreamEventInfo } from "@filigran/chatbot";
import {
  RUNNER_APPROVAL_REQUIRED_EVENT,
  RUNNER_APPROVAL_RESOLVED_EVENT,
  TURN_END_REFETCH_DELAY_MS,
  handleToolProgress,
  refreshAfterToolDone,
  refreshAfterPlatformTool,
  type ToolProgressFrame,
} from "@/hooks/api/useChatApi";
import type { UserQuotaInfo } from "@/types";
import {
  AGENTIC_ACTION_FRAME,
  NO_TURN_CHARGES,
  addTurnCharge,
  agenticUsagePeriod,
  chargeFromFrame,
  refetchAfterCharge,
  type TurnCharges,
} from "@/lib/agentic-usage";
import { readRunnerApprovalRequest } from "@/lib/desktop";
import {
  closeLiveRunnerActivity,
  readRunnerActivityFrame,
  unwrapConsultFrame,
  upsertRunnerActivity,
} from "@/lib/runner-activity";
import { readResumingFrame } from "@/lib/resumed-turn";
import { readWaitItems } from "@/lib/wait-items";
import type {
  ConversationDetail,
  ResumedFromNote,
  RunnerActivity,
  RunnerApprovalRequest,
  WaitItem,
  WakeNote,
} from "@/types";
import { readWakeNote } from "@/lib/wake-turn";

/** Opens a turn the platform resumed by itself after a cut (#7441, `cut_turn_resume.RESUMING_FRAME_TYPE`). */
const TURN_RESUMING_FRAME = "turn_resuming";

const TOOL_PROGRESS_STATUS = "tool_progress";

/**
 * The statuses of a pause on a `wait` call: `wait_start` names what it
 * watches, `wait_status` says one of them changed state. The pause lasts
 * through the statuses a running tool sends; any other status ends it.
 */
const WAIT_START_STATUS = "wait_start";
const WAIT_STATUS_STATUS = "wait_status";
const STATUSES_DURING_WAIT = new Set([WAIT_STATUS_STATUS, "tool_heartbeat", TOOL_PROGRESS_STATUS]);

/**
 * What the chat page does with the frames of a turn that the panel does not
 * render itself (`onStreamEvent`): the commands the agent runs on a machine and
 * the ones held for approval, the agentic actions the turn is charged, the
 * investigation it runs, what a pause waits on, whether the platform resumed
 * the turn by itself after a cut, and the caches the turn's
 * tools changed. The frames of a turn this tab did not start (`relayed`) count
 * the same: it is the turn on screen. A host following a tool's own reports
 * (a studio's stage following the agent's pen) passes `onToolProgress`.
 *
 * Whether a turn runs (`working`) is the panel's own word
 * (`onTurnRunningChange`, handed to the panel with `onStreamEvent`): its end
 * drops the turn's state and refreshes what the turn changed.
 */
export function useTurnFrames(
  conversationId: string | null,
  { onToolProgress: hostToolProgress }: { onToolProgress?: (frame: ToolProgressFrame) => void } = {},
) {
  const qc = useQueryClient();
  const [working, setWorking] = useState(false);
  const [turnCharges, setTurnCharges] = useState<TurnCharges>(NO_TURN_CHARGES);
  const [runnerActivity, setRunnerActivity] = useState<RunnerActivity[]>([]);
  const [runnerApprovals, setRunnerApprovals] = useState<RunnerApprovalRequest[]>([]);
  const [activeInvestigationId, setActiveInvestigationId] = useState<string | null>(null);
  const [waitItems, setWaitItems] = useState<WaitItem[]>([]);
  // The `turn_resuming` note of a turn, kept with the turn it names: the
  // panel's running flag can blink off and on while a new conversation takes
  // its id (#10910), and a replay re-sends the note of the same turn, so
  // neither drops it; a frame of another turn, the turn's stored end or a
  // switch to another conversation does.
  // `read` turns true once the thread is read back after the frame: until
  // then the transcript may not hold the cut yet, and the line under it said
  // "resumed by itself" for a moment before the cut card took over with
  // "resuming by itself" (#10910).
  const [resuming, setResuming] = useState<{ turn: string | null; note: ResumedFromNote; read: boolean } | null>(null);
  // The work that woke the turn in flight (its `thinking` ack's `wake`,
  // #11796). Unlike `resuming` it never outlives its turn: the next turn's
  // ack replaces it (a plain ack drops it), and the turn's end on screen, its
  // stored end or a switch to another conversation drops it, so a finished
  // task is never announced again on a later turn. A wake turn runs in a
  // conversation that already exists, so the running flag's blink while a
  // new conversation takes its id (#10910) never concerns it.
  const [waking, setWaking] = useState<{ turn: string | null; note: WakeNote } | null>(null);
  const [liveTurn, setLiveTurn] = useState<string | null>(null);
  // Whether the turn is in a pause: `wait_status` only updates a pause on screen.
  const waitingRef = useRef(false);
  const runningRef = useRef(false);
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  const shownConversationRef = useRef(conversationId);

  // A conversation's turn state is its own (whether one runs is the panel's to say).
  // A new chat taking the id its first send created keeps the turn it runs.
  useEffect(() => {
    const left = shownConversationRef.current;
    shownConversationRef.current = conversationId;
    if (left === null && conversationId !== null) return;
    setTurnCharges(NO_TURN_CHARGES);
    setRunnerActivity([]);
    setRunnerApprovals([]);
    setActiveInvestigationId(null);
    setWaitItems([]);
    setResuming(null);
    setWaking(null);
    setLiveTurn(null);
    waitingRef.current = false;
  }, [conversationId]);

  const hostToolProgressRef = useRef(hostToolProgress);
  hostToolProgressRef.current = hostToolProgress;
  const onToolProgress = useCallback((frame: ToolProgressFrame) => {
    hostToolProgressRef.current?.(frame);
    if (frame.event === RUNNER_APPROVAL_REQUIRED_EVENT) {
      const request = readRunnerApprovalRequest(frame);
      if (!request) return;
      setRunnerApprovals((prev) => (prev.some((p) => p.command_id === request.command_id) ? prev : [...prev, request]));
    } else if (frame.event === RUNNER_APPROVAL_RESOLVED_EVENT) {
      const commandId = typeof frame.command_id === "string" ? frame.command_id : null;
      if (commandId) setRunnerApprovals((prev) => prev.filter((p) => p.command_id !== commandId));
    }
  }, []);

  const onStreamEvent = useCallback(
    (frame: Record<string, unknown>, info: ChatStreamEventInfo) => {
      const type = frame.type;
      const cid = conversationIdRef.current;
      if (typeof type !== "string") return;
      const turn = typeof frame.turn_id === "string" && frame.turn_id ? frame.turn_id : null;
      if (turn) {
        setLiveTurn(turn);
        // A frame of another turn: the resume note of the turn before is over.
        setResuming((prev) => (prev && prev.turn !== null && prev.turn !== turn ? null : prev));
      }
      if (type === AGENTIC_ACTION_FRAME) {
        const charge = chargeFromFrame(
          frame.actions,
          Date.now(),
          typeof frame.period === "string" && frame.period
            ? frame.period
            : agenticUsagePeriod(qc.getQueryData<UserQuotaInfo>(["my-quotas"])?.usage),
          info.relayed || frame.relayed === true,
        );
        if (!charge) return;
        setTurnCharges((prev) => addTurnCharge(prev, charge));
        void refetchAfterCharge(qc, ["my-quotas"]);
        return;
      }
      if (info.replayed) {
        // A turn replayed from its start (the panel's stream dropped and the
        // event stream took it over): the charges that follow are its totals
        // so far, which replace the ones counted, never add to them.
        setTurnCharges(NO_TURN_CHARGES);
        // The turn's `turn_resuming` note stays: the replay is the same turn.
        return;
      }
      if (type === TURN_RESUMING_FRAME) {
        const note = readResumingFrame(frame);
        // A replay of the same turn keeps the read-back already made.
        setResuming((prev) =>
          note ? { turn, note, read: !cid || (prev !== null && prev.turn === turn && prev.read) } : null,
        );
        // A resume stored the reply its cut left (and the cut card draws it):
        // the thread is read back now, not at the turn's end, and the resume
        // is said only once it is.
        if (cid && note) {
          void qc
            .invalidateQueries({ queryKey: ["conversation", cid] })
            .finally(() => setResuming((prev) => (prev && prev.turn === turn ? { ...prev, read: true } : prev)));
        }
        return;
      }
      if (type === "thinking") {
        // Each turn opens with one ack: its own `wake`, or none for a plain turn.
        // A stream taking the same turn over re-sends a `resumed` ack without
        // it (`resume_frames`): the turn's note stays.
        const note = frame.wake !== undefined ? readWakeNote(frame.wake) : null;
        setWaking((prev) =>
          note ? { turn, note } : frame.resumed === true && prev !== null && prev.turn === turn ? prev : null,
        );
        return;
      }
      if (type === "conversation_title") {
        void qc.invalidateQueries({ queryKey: ["conversations"] });
        if (cid) void qc.invalidateQueries({ queryKey: ["conversation", cid] });
        return;
      }
      if (info.end === "stored") {
        // The turn's reply is stored: a Stop or a failure stores it well after
        // the turn stopped running on screen, past the refetch its end made.
        // A turn still running on screen ends with that refetch, after this.
        // The stored reply carries its own `resumed_from`.
        if (turn) setResuming((prev) => (prev?.turn === turn ? null : prev));
        // The stored reply carries its own `wake` marker.
        if (turn) setWaking((prev) => (prev?.turn === turn ? null : prev));
        if (runningRef.current) return;
        void qc.invalidateQueries({ queryKey: ["conversations"] });
        if (cid) void qc.invalidateQueries({ queryKey: ["conversation", cid] });
        return;
      }
      if (type !== "status") {
        if (type === "stream" && waitingRef.current) {
          waitingRef.current = false;
          setWaitItems([]);
        }
        return;
      }
      const status = frame.status;
      if (status === WAIT_START_STATUS) {
        waitingRef.current = true;
        setWaitItems(readWaitItems(frame.items) ?? []);
      } else if (status === WAIT_STATUS_STATUS) {
        const items = readWaitItems(frame.items);
        if (items && waitingRef.current) setWaitItems(items);
      } else if (waitingRef.current && typeof status === "string" && !STATUSES_DURING_WAIT.has(status)) {
        waitingRef.current = false;
        setWaitItems([]);
      }
      if (status === "tool_done") {
        refreshAfterToolDone(qc, frame.tool, cid);
      } else if (status === TOOL_PROGRESS_STATUS) {
        const activity = readRunnerActivityFrame(frame);
        if (activity) setRunnerActivity((prev) => upsertRunnerActivity(prev, activity));
        handleToolProgress(qc, frame, onToolProgress);
      } else if (status === "consult_progress") {
        // A consulted agent's machine commands, decks and workspaces are this turn's too.
        const activity = readRunnerActivityFrame(frame);
        if (activity) setRunnerActivity((prev) => upsertRunnerActivity(prev, activity));
        // What the agent doing the work emitted: the consulted agent, or a peer it consulted in turn.
        const inner = unwrapConsultFrame(frame);
        if (inner?.status === TOOL_PROGRESS_STATUS) handleToolProgress(qc, inner, onToolProgress);
        if (inner?.status === "tool_done") refreshAfterPlatformTool(qc, inner.tool, cid);
      } else if (status === "investigation_planned") {
        if (typeof frame.investigation_id === "string") setActiveInvestigationId(frame.investigation_id);
      } else if (status === "investigation_complete") {
        if (typeof frame.investigation_id === "string") {
          void qc.invalidateQueries({ queryKey: ["investigations", frame.investigation_id] });
        }
      } else if (status === "transfer_start") {
        // The conversation now answers as the agent it was handed to: its tools follow.
        const targetId = frame.agent_id;
        if (cid && typeof targetId === "string") {
          qc.setQueryData<ConversationDetail>(["conversation", cid], (old) => (old ? { ...old, agent_id: targetId } : old));
        }
      }
    },
    [qc, onToolProgress],
  );

  const onTurnRunningChange = useCallback(
    (running: boolean) => {
      runningRef.current = running;
      setWorking(running);
      if (running) return;
      waitingRef.current = false;
      setWaitItems([]);
      // The `turn_resuming` note is kept with its turn (see `resuming`): the
      // panel's flag blinks while a new conversation takes its id.
      // The wake note is not (see `waking`): a stored end missed would
      // otherwise announce the task again on every later turn.
      setWaking(null);
      setTurnCharges(NO_TURN_CHARGES);
      setRunnerActivity(closeLiveRunnerActivity);
      setRunnerApprovals([]);
      // The persisted `[[INVESTIGATION:…]]` marker takes the run over.
      setActiveInvestigationId(null);
      // After the panel's own end-of-turn render.
      const cid = conversationIdRef.current;
      window.setTimeout(() => {
        void qc.invalidateQueries({ queryKey: ["my-quotas"] });
        void qc.invalidateQueries({ queryKey: ["conversations"] });
        void qc.invalidateQueries({ queryKey: ["presentations"] });
        if (cid) {
          void qc.invalidateQueries({ queryKey: ["conversation", cid] });
          void qc.invalidateQueries({ queryKey: ["awaited-work", cid] });
        }
      }, TURN_END_REFETCH_DELAY_MS);
    },
    [qc],
  );

  const dismissRunnerApproval = useCallback((commandId: string) => {
    setRunnerApprovals((prev) => prev.filter((p) => p.command_id !== commandId));
  }, []);

  return {
    onStreamEvent,
    /** Hand to the panel: it says when a turn starts and stops running on screen. */
    onTurnRunningChange,
    working,
    turnCharges,
    runnerActivity,
    runnerApprovals,
    dismissRunnerApproval,
    activeInvestigationId,
    /** What the pause on screen waits on; empty when the turn is not waiting. */
    waitItems,
    /** The turn in flight is one the platform resumed by itself: its `turn_resuming` frame. */
    resumingTurn:
      working && resuming?.read && (resuming.turn === null || resuming.turn === liveTurn) ? resuming.note : null,
    /** The turn in flight was woken by finished background work: its `thinking` ack's `wake`. */
    wakingTurn: working && waking && (waking.turn === null || waking.turn === liveTurn) ? waking.note : null,
  };
}
