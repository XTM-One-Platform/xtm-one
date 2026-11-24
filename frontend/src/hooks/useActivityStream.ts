// SPDX-License-Identifier: Apache-2.0

/** SSE hook for real-time activity updates. */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { checkSession } from "@/hooks/api/useAuthApi";
import { clickerKey } from "@/hooks/api/usePresentationClicker";
import { API_BASE, endSession, getToken } from "@/lib/api";
import { streamRetry } from "@/lib/activity-stream-retry";
import { globalToast } from "@/lib/global-toast";
import { useFormatter } from "@/i18n";
import { SESSION_EXPIRED_CODE, streamSessionEndReason } from "@/lib/session";

export function useActivityStream() {
  const qc = useQueryClient();
  const { t_i18n } = useFormatter();
  const tRef = useRef(t_i18n);
  tRef.current = t_i18n;
  const eventSourceRef = useRef<EventSource | null>(null);
  // Connections in a row that failed before they opened.
  const failuresRef = useRef(0);
  // Bumped after a connection error to re-run the effect and reconnect.
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    const token = getToken();
    if (!token) return;

    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let opened = false;
    let retrying = false;

    const es = new EventSource(`${API_BASE}/activity/stream?token=${token}`);
    eventSourceRef.current = es;

    es.onopen = () => {
      opened = true;
      failuresRef.current = 0;
    };

    // The session ended while the stream was open: sign out rather
    // than reconnect a stream the server will refuse.
    es.addEventListener(SESSION_EXPIRED_CODE, (event) => {
      disposed = true;
      es.close();
      endSession(streamSessionEndReason((event as MessageEvent<string>).data));
    });

    es.onmessage = (event) => {
      let parsed: {
        type?: string;
        flow_id?: string;
        run_id?: string;
        state?: string;
        title?: string;
        presenter_name?: string;
        presentation_id?: string;
        signal_id?: string;
        case_id?: string;
      } | null = null;
      try {
        parsed = JSON.parse(event.data as string);
      } catch {
        // Non-JSON frames carry no routing info — fall through to the
        // blanket invalidations below.
      }

      // "Hand the clicker" (xtm-one #10552 B2): the co-presenter's one in-app
      // notice when handed a deck's clicker. Commands and releases are read
      // by the deck windows themselves; nothing else to refresh here.
      if (parsed?.type === "presentation_clicker_state") {
        if (parsed.state === "handed") {
          globalToast({
            title: tRef.current("You hold the clicker"),
            description: tRef.current("{name} handed you the clicker of {title}. Open the presentation to move the slides.", {
              name: parsed.presenter_name || tRef.current("Someone"),
              title: parsed.title || tRef.current("a presentation"),
            }),
            variant: "info",
          });
        }
        if (parsed.presentation_id) {
          qc.invalidateQueries({ queryKey: clickerKey(parsed.presentation_id) });
        }
        return;
      }
      if (parsed?.type === "presentation_clicker_command") return;

      // Flow executions stream one frame per node transition —
      // route them narrowly to the two flow-run keys instead of hammering
      // every polled query on each executed node.
      if (parsed?.type === "flow_run_update") {
        qc.invalidateQueries({
          queryKey: parsed.flow_id
            ? ["flow-runs", parsed.flow_id]
            : ["flow-runs"],
        });
        if (parsed.run_id) {
          qc.invalidateQueries({ queryKey: ["flow-run", parsed.run_id] });
        }
        // The Operations → Executions cross-flow feed.
        qc.invalidateQueries({ queryKey: ["flow-runs-global"] });
        return;
      }

      // CTEM loop frames (#838, dev-docs/ctem-loop-visibility.md section 5):
      // ids and states only, routed to the signal queries alone so a busy
      // pipeline does not refresh every query of the page; the re-read applies
      // the viewer's visibility.
      if (parsed?.type === "ctem_signal_update") {
        qc.invalidateQueries({ queryKey: ["ctem-signals", "recent"] });
        qc.invalidateQueries({ queryKey: ["ctem-signals", "list"] });
        if (parsed.signal_id) qc.invalidateQueries({ queryKey: ["ctem-signals", "signal", parsed.signal_id] });
        return;
      }
      // A case opened, joined, validated or past its SLA (CTEM-03-UI, #892):
      // the board, Act now and the case page re-read through the API.
      if (parsed?.type === "ctem_case_update") {
        qc.invalidateQueries({ queryKey: ["ctem-cases", "list"] });
        if (parsed.case_id) {
          qc.invalidateQueries({ queryKey: ["ctem-cases", "case", parsed.case_id] });
          qc.invalidateQueries({ queryKey: ["ctem-signals", "case", parsed.case_id] });
        }
        return;
      }

      // Invalidate activity, tasks, and assignment runs queries on new events.
      // "all-assignment-runs" powers the notification bell in the header —
      // without this, the bell only updates on its 15 s polling interval
      // while the dashboard digest refreshes instantly, making it look
      // like notifications are broken when both are enabled.
      qc.invalidateQueries({ queryKey: ["activity"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["all-assignment-runs"] });
      // The Agentic Flow "Live" feed merges inter-agent delegations, background
      // tasks, and work items alongside runs — refresh those on the same event
      // so the live cockpit updates instantly instead of waiting on polling.
      qc.invalidateQueries({ queryKey: ["agent-delegations"] });
      qc.invalidateQueries({ queryKey: ["background-tasks"] });
      // The background-task COUNT is cached under a separate key; invalidate it
      // too so count / pagination surfaces update instantly on the same event
      // instead of lagging until their own polling interval.
      qc.invalidateQueries({ queryKey: ["background-task-count"] });
      // "sub-agent-tasks" powers the chat BackgroundTasksBanner. Its query
      // only polls while cached data shows an in-flight task, so a freshly
      // spawned delegation must be pushed into the cache from here —
      // otherwise the banner never appears until refocus/remount.
      qc.invalidateQueries({ queryKey: ["sub-agent-tasks"] });
      qc.invalidateQueries({ queryKey: ["work-items"] });
      qc.invalidateQueries({ queryKey: ["active-agent-ids"] });
      // Flow executions — polling stays the floor, this is
      // the accelerator, same as assignment runs above.
      qc.invalidateQueries({ queryKey: ["flow-runs"] });
      // Background-task lifecycle events also refresh the open
      // conversation: a completed task proactively delivers its notice as a
      // new assistant message, which otherwise only surfaces on the next
      // user action or page reload.
      // A scheduled work item that ran posts its outcome the same way.
      if (parsed?.type === "background_task_update" || parsed?.type === "work_item_update") {
        qc.invalidateQueries({ queryKey: ["conversation"] });
      }
      // A run just paused for per-tool approval. The blanket
      // invalidations above already refresh the run lists, but the detail
      // query is keyed by run id and would otherwise keep serving a cached
      // copy with no pending_approvals — leaving the reviewer looking at a
      // run that appears to be running while it waits on them.
      //
      // Uses the hoisted `parsed` rather than parsing again: main moved the
      // JSON.parse to the top of the handler, and its type already carries
      // `run_id`.
      if (parsed?.type === "action_proposal" && parsed.run_id) {
        qc.invalidateQueries({ queryKey: ["assignment-run", parsed.run_id] });
      }
    };

    es.onerror = () => {
      es.close();
      if (eventSourceRef.current === es) {
        eventSourceRef.current = null;
      }
      if (retrying) return;
      retrying = true;
      if (!opened) failuresRef.current += 1;
      const retry = streamRetry(opened ? 0 : failuresRef.current);
      // Reconnect by re-running the effect, which reads the token again.
      const reconnect = () => {
        if (disposed) return;
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          setRetryCount((c) => c + 1);
        }, retry.delayMs);
      };
      if (!retry.checkSession) {
        reconnect();
        return;
      }
      // The stream cannot read why it was refused; the session check can, and
      // ends a dead session the way every other request does. Whatever else
      // it answers, retry after the backoff.
      checkSession().then(reconnect, reconnect);
    };

    return () => {
      disposed = true;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      es.close();
      eventSourceRef.current = null;
    };
  }, [qc, retryCount]);
}
