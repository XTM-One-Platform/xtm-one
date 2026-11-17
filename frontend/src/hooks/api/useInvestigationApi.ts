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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { awaitingReport } from "@/components/investigation/goal-state";
import { api, notFoundIsOrdinary } from "@/lib/api";
import type {
  ContinuationOffer,
  ContinuationStarted,
  Investigation,
  SubjectPlan,
} from "@/types";

const INVESTIGATIONS_KEY = ["investigations"] as const;

const POLL_MS = 3000;

/** How long a run may report no progress before the poll gives up.
 *
 *  `revision` is bumped on every state write, so a run that is working moves it
 *  at least once per source. Long enough that a slow source (a 30s HTTP
 *  timeout, a retried fetch) never trips it; short enough that a stranded run
 *  stops costing requests within a couple of minutes. */
const STALL_TIMEOUT_MS = 2 * 60 * 1000;

// Where a finished run's report stands is pure state, next to the block's
// other derivations (unit-tested there).
export { awaitingReport, REPORT_WAIT_MS } from "@/components/investigation/goal-state";

/** Fetch one Deep Investigation run by id.
 *
 *  Read-only: runs are written exclusively by the orchestrator, whose state
 *  machine is what guarantees a failed source can never be stored as a
 *  success. `useStartContinuation` below is not an exception to that — it
 *  writes no step, it asks the server to start a NEW run from sources the
 *  caller is re-authorised for, and the orchestrator remains the only writer.
 *
 *  `refetchInterval` polls only while the run is unfinished. The block's live
 *  updates arrive as stream frames; this poll is the fallback that makes the
 *  block correct when they do not (an entry point without frames, a dropped
 *  stream, another tab). It stops as soon as the run reaches a terminal state,
 *  so a finished conversation costs nothing.
 *
 *  It also stops when the run stops MOVING, which is not the same thing. The
 *  server drives every run to a terminal state — but "the status field will eventually be terminal" is
 *  the kind of assumption that costs a request every three seconds, per mounted
 *  block, for as long as the conversation is open, on the day it turns out to
 *  be false. So the client does not rely on it: no `revision` movement for
 *  `STALL_TIMEOUT_MS` and the poll gives up, leaving `stalled` for the caller
 *  to render as a static "no longer updating" block. Progress resumes the timer
 *  rather than resetting a count, so a slow run is never mistaken for a dead
 *  one however long it takes. */
export function useInvestigation(id: string | undefined) {
  // The last movement seen, keyed by run: read by `refetchInterval`, which
  // must stay a pure read (it runs inside the query observer, where setting
  // state warns). Not state, so writing it never causes a render.
  const lastMove = useRef<{ runId: string; revision: number; at: number } | null>(null);
  const [stalled, setStalled] = useState(false);

  const query = useQuery<Investigation>({
    queryKey: [...INVESTIGATIONS_KEY, id],
    queryFn: () => api.get(`/investigations/${id}`),
    enabled: !!id,
    // A deleted or unreadable run (404) is the block's own "no longer
    // available" state. The app's retry stays: this query polls, and a
    // one-off 5xx between two polls must not reach the boundary unretried.
    throwOnError: notFoundIsOrdinary.throwOnError,
    refetchInterval: (query) => {
      const run = query.state.data;
      if (run && awaitingReport(run)) return POLL_MS;
      if (!run || !isLive(run)) return false;
      const moved = lastMove.current;
      const stale =
        moved?.runId === run.id &&
        moved.revision === run.revision &&
        Date.now() - moved.at >= STALL_TIMEOUT_MS;
      return stale ? false : POLL_MS;
    },
  });

  const run = query.data;
  const runId = run?.id;
  const revision = run?.revision;
  const live = !!run && isLive(run);
  // Every movement (a new run, a new revision, a terminal status) restarts
  // the stall timer, so a slow run is never mistaken for a dead one, and a
  // block switching to a fresh run never says it is dead before it moved.
  useEffect(() => {
    setStalled(false);
    if (!live || runId === undefined || revision === undefined) return;
    lastMove.current = { runId, revision, at: Date.now() };
    const timer = window.setTimeout(() => setStalled(true), STALL_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [id, runId, revision, live]);

  return { ...query, stalled };
}

function isLive(run: Investigation): boolean {
  return run.status === "planning" || run.status === "running";
}

/** Classify a subject and list the packs that could investigate it.
 *
 *  Server-side because the classifier is server-side — a deterministic regex.
 *  Duplicating it in TypeScript would create two answers to "what kind of
 *  subject is this?", and the one that picks the sources must be the one shown
 *  to the user.
 *
 *  `enabled` on a null subject so the picker fetches only once a subject
 *  exists; `staleTime` because the answer is a pure function of the subject
 *  and the code-declared packs, so re-opening the picker for the same subject
 *  must not re-request. */
export function useSubjectPlan(subject: string | null) {
  return useQuery<SubjectPlan>({
    queryKey: [...INVESTIGATIONS_KEY, "subject-plan", subject],
    queryFn: () =>
      api.get(`/investigations/source-packs?subject=${encodeURIComponent(subject!)}`),
    enabled: !!subject,
    staleTime: 5 * 60 * 1000,
  });
}

/** What is left of a run's plan, and what the chain has already cost.
 *
 *  Fetched only for a finished run: a run still in flight may yet query the
 *  very steps this would invite a second spend on, and the server refuses to
 *  offer them anyway — asking while it runs would just be a poll for `null`.
 *
 *  No `refetchInterval`. The offer changes when the analyst acts on it, and
 *  those paths invalidate it explicitly; polling would re-run the chain walk
 *  on every open block for an answer that had not moved. */
export function useContinuationOffer(id: string | undefined, enabled: boolean) {
  return useQuery<ContinuationOffer>({
    queryKey: [...INVESTIGATIONS_KEY, id, "continuation"],
    queryFn: () => api.get(`/investigations/${id}/continuation`),
    enabled: !!id && enabled,
  });
}

/** Query the selected remainder as a new run linked to this one.
 *
 *  Invalidates three things, and each for a different reason: the parent's
 *  offer (its remainder is now smaller), the investigation list (a run
 *  appeared), and the conversation the run belongs to (the block is rendered
 *  from its messages). Missing the last one leaves the analyst looking at a
 *  block that will not show the child until they navigate away and back. */
export function useStartContinuation(conversationId?: string | null) {
  const queryClient = useQueryClient();
  return useMutation<
    ContinuationStarted,
    Error,
    { id: string; stepIds: string[] }
  >({
    mutationFn: ({ id, stepIds }) =>
      api.post(`/investigations/${id}/continuation`, { step_ids: stepIds }),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({
        queryKey: [...INVESTIGATIONS_KEY, id, "continuation"],
      });
      queryClient.invalidateQueries({ queryKey: INVESTIGATIONS_KEY });
      if (conversationId) {
        queryClient.invalidateQueries({
          queryKey: ["conversation", conversationId],
        });
      }
    },
    // A refusal means the offer moved (spent, in flight, withdrawn).
    onError: (_error, { id }) => {
      queryClient.invalidateQueries({
        queryKey: [...INVESTIGATIONS_KEY, id, "continuation"],
      });
    },
  });
}
