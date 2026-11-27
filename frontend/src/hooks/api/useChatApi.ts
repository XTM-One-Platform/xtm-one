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

/** Chat and conversation API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { invalidateAgentAccounts } from "@/hooks/api/useAgentAccountApi";
import { invalidateDashboards } from "@/hooks/api/useCustomDashboardApi";
import { invalidateVideoStudio } from "@/hooks/api/useVideoStudioApi";
import { api, BackendUnavailableError, notFoundIsOrdinary } from "@/lib/api";
import { refreshChannelContinuation } from "@/lib/channel-bindings";
import { REFETCH_INTERVAL_FAST, REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { useAutoResumeRecheck } from "@/hooks/useAutoResumeRecheck";
import type {
  AwaitedWorkItem,
  ContinuationChannel,
  ConversationChannelBinding,
  ConversationChannelBindings,
  ConversationListItem,
  ConversationDetail,
  ConversationFileOrigin,
  ConversationFilesPage,
  ConversationLinkAccess,
  ConversationSharesState,
  ConversationShareTargets,
} from "@/types";

/** Delay of the refetches a turn's end schedules: the transition render
 *  (streaming bubble to final message) completes first, so a refetch-driven
 *  re-render cannot shift the layout mid-transition. */
export const TURN_END_REFETCH_DELAY_MS = 400;

/**
 * `event` of a `tool_progress` frame: a presentation revision is committed
 * and readable (`PRESENTATION_SAVED_EVENT` on the backend). Keyed on WHAT
 * happened rather than on `data.tool`, so a model-supplied alias of the deck
 * tool cannot hide a save from the stage. The frame carries `presentation_id`,
 * `revision` and — when the edit wrote a slide — `focus_slide_id`, the
 * stable id of the slide the studio stage follows.
 */
export const PRESENTATION_SAVED_EVENT = "presentation_saved";

/**
 * `event` of a `tool_progress` frame: a video revision is committed and
 * readable (`VIDEO_SAVED_EVENT` on the backend). Carries `video_id` and
 * `revision`; the editor's preview follows the agent's edit.
 */
export const VIDEO_SAVED_EVENT = "video_saved";

/**
 * `event` of a `tool_progress` frame: a custom dashboard revision is committed
 * and readable (`DASHBOARD_SAVED_EVENT` on the backend, Dashboard Studio).
 * Carries `dashboard_id`, `revision` and `focus_widget_id` (the widget the save
 * added or changed, or null); the studio page follows the agent's edit.
 */
export const DASHBOARD_SAVED_EVENT = "dashboard_saved";

/**
 * `event` of a `tool_progress` frame: a runner command the agent asked for is
 * held by the machine's policy and the tool is WAITING for the user's decision
 * (`RUNNER_APPROVAL_REQUIRED_EVENT` on the backend). Carries
 * `command_id`, `runner_id`, `runner_name`, `kind`, `summary` and
 * `expires_in_seconds`; shown above the composer (RunnerApprovalCard in ComposerHeader).
 * `RUNNER_APPROVAL_RESOLVED_EVENT` follows once the wait ends (approved and
 * run, rejected, or timed out) and dismisses the card.
 */
export const RUNNER_APPROVAL_REQUIRED_EVENT = "runner_approval_required";
export const RUNNER_APPROVAL_RESOLVED_EVENT = "runner_approval_resolved";

/**
 * The agent-builder tools that create, change or delete a workspace, or file a
 * conversation into one. The workspace pickers and the sidebar follow
 * the change the moment the tool returns, not when the list goes stale: the
 * person asked their assistant to move this very conversation. `list_workspaces`
 * is here because it creates the caller's default on first use.
 */
const WORKSPACE_TOOLS = new Set([
  "list_workspaces",
  "create_workspace",
  "update_workspace",
  "delete_workspace",
  "set_default_workspace",
  "update_workspace_knowledge_bases",
  "move_conversations_to_workspace",
]);

/**
 * The tools that create, change or delete an agent account: the Agent
 * Accounts page shows the account as the agent left it.
 */
const AGENT_ACCOUNT_TOOLS = new Set([
  "create_agent_account",
  "update_agent_account",
  "edit_agent_account_rule",
  "delete_agent_account",
]);

/**
 * The fleet tools that change a fleet: the fleet page (and its Modify with AI
 * rail, #10162) shows the fleet, its members, health and About as the agent
 * left them, without a reload.
 */
const FLEET_WRITE_TOOLS = new Set([
  "create_fleet",
  "update_fleet",
  "set_fleet_automations",
  "delete_fleet",
  "set_fleet_members",
  "set_fleet_exposure",
  "set_fleet_member_parent",
  "move_fleet_member",
  "regenerate_fleet_documentation",
  "apply_fleet_spec",
]);

/**
 * Refresh what a workspace, agent-account or fleet tool changed, whether the
 * turn's agent called it or an agent it consulted (whose frames arrive under
 * `consult_progress.inner`).
 */
export function refreshAfterPlatformTool(
  qc: ReturnType<typeof useQueryClient>,
  tool: unknown,
  conversationId: string | null | undefined,
) {
  if (typeof tool !== "string") return;
  if (AGENT_ACCOUNT_TOOLS.has(tool)) invalidateAgentAccounts(qc);
  if (FLEET_WRITE_TOOLS.has(tool)) {
    // Every fleet read (detail, members, graph, health, documentation) sits under
    // ["fleets"]; the agents carry their home fleet.
    qc.invalidateQueries({ queryKey: ["fleets"] });
    qc.invalidateQueries({ queryKey: ["agents"] });
    // A fleet built from a spec can create its member agents: the flow builder lists them.
    syncFlowNodeCatalog(qc);
  }
  if (!WORKSPACE_TOOLS.has(tool)) return;
  qc.invalidateQueries({ queryKey: ["workspaces"] });
  qc.invalidateQueries({ queryKey: ["conversations"] });
  if (conversationId) qc.invalidateQueries({ queryKey: ["conversation", conversationId] });
}

/**
 * Refresh what a tool the turn's agent just finished changed (`status:
 * tool_done`): the background tasks it spawned, the workspaces, agent accounts,
 * decks and videos it wrote.
 */
export function refreshAfterToolDone(
  qc: ReturnType<typeof useQueryClient>,
  tool: unknown,
  conversationId: string | null | undefined,
): void {
  // A new SubAgentTask row: refresh now, not at the 30 s idle poll.
  if (tool === "spawn_background_task") {
    qc.invalidateQueries({ queryKey: ["sub-agent-tasks"] });
    qc.invalidateQueries({ queryKey: ["background-tasks"] });
    qc.invalidateQueries({ queryKey: ["background-task-count"] });
  }
  refreshAfterPlatformTool(qc, tool, conversationId);
  // The studio shows a deck's revision, comments and sharing the moment a tool
  // writes them; a deck the agent deleted leaves the library and its
  // open views at once.
  if (
    tool === "generate_presentation" ||
    tool === "edit_presentation" ||
    tool === "duplicate_presentation" ||
    tool === "delete_presentation" ||
    tool === "add_presentation_comment" ||
    tool === "resolve_presentation_comment" ||
    tool === "share_presentation" ||
    tool === "set_presentation_public_link"
  ) {
    qc.invalidateQueries({ queryKey: ["presentations"] });
  }
  // The video's Share dialog and the library card show the audience and the
  // public link the agent just set, the renders panel the renders it
  // deleted, the library the video it deleted.
  if (
    tool === "share_video" ||
    tool === "set_video_public_link" ||
    tool === "delete_video_renders" ||
    tool === "delete_video"
  ) {
    void invalidateVideoStudio(qc);
  }
}

/** A `tool_progress` frame as the wire delivers it (`status` already checked). */
export type ToolProgressFrame = Record<string, unknown>;

/**
 * React to a tool's mid-execution report.
 *
 * The deck tools commit the new revision and THEN render the deck in a
 * headless browser for the post-save layout check, before returning — so the
 * `tool_done` invalidation below would leave a saved revision invisible for
 * the whole render (up to the check's timeout, longer behind an export). The
 * `presentation_saved` frame is emitted right after the commit: refresh the
 * mounted deck (studio stage, viewer, chat card — one query client) now.
 */
export function handleToolProgress(
  qc: ReturnType<typeof useQueryClient>,
  frame: ToolProgressFrame,
  onToolProgress?: (frame: ToolProgressFrame) => void,
): void {
  if (frame.event === PRESENTATION_SAVED_EVENT) {
    qc.invalidateQueries({ queryKey: ["presentations"] });
  }
  if (frame.event === VIDEO_SAVED_EVENT) {
    void invalidateVideoStudio(qc);
  }
  if (frame.event === DASHBOARD_SAVED_EVENT) {
    void invalidateDashboards(qc);
  }
  // The host's own reaction (the studio stage following the slide an edit
  // wrote) — after the refresh, so a listener that reads the deck reads
  // the invalidated query.
  onToolProgress?.(frame);
}

// --- Chat / Conversations ---
/**
 * The caller's own conversations, most recent first.
 *
 * `throwOnError: false` for a page that keeps working without the list: the
 * Chat page passes it so a failed read leaves the sidebar with an inline error
 * line and the welcome screen and composer usable, instead of the route
 * error boundary showing the raw backend detail. Without it the read
 * inherits the app's throwing policy (main.tsx).
 */
export function useConversations(limit = 50, options: { throwOnError?: boolean } = {}) {
  return useQuery<ConversationListItem[]>({
    queryKey: ["conversations", limit],
    queryFn: () => api.get(`/chat/conversations?limit=${limit}`),
    ...(options.throwOnError === false ? { throwOnError: false, retry: 1 } : {}),
  });
}

/**
 * Bind an EXISTING conversation to a deck — or unbind it with ``null``
 * (Presentation Studio). This is how the chat card's "Open in studio"
 * carries the conversation that produced the deck into the studio, so the
 * agent keeps what it already knows about the work.
 */
export function useBindConversationPresentation() {
  const qc = useQueryClient();
  return useMutation<
    ConversationListItem,
    Error,
    { conversationId: string; presentationId: string | null }
  >({
    mutationFn: ({ conversationId, presentationId }) =>
      api.put(`/chat/conversations/${conversationId}/presentation`, {
        presentation_id: presentationId,
      }),
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.setQueryData<ConversationDetail>(["conversation", conv.id], (old) =>
        old ? { ...old, presentation_id: conv.presentation_id ?? null } : old,
      );
    },
  });
}

/**
 * Bind an EXISTING conversation to a video — or unbind it with ``null``
 * (Video Studio): the presentation binding's twin, for "Open in
 * editor" from the chat that made the video.
 */
export function useBindConversationVideo() {
  const qc = useQueryClient();
  return useMutation<ConversationListItem, Error, { conversationId: string; videoId: string | null }>({
    mutationFn: ({ conversationId, videoId }) =>
      api.put(`/chat/conversations/${conversationId}/video`, { video_project_id: videoId }),
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.setQueryData<ConversationDetail>(["conversation", conv.id], (old) =>
        old ? { ...old, presentation_id: null, video_project_id: conv.video_project_id ?? null } : old,
      );
    },
  });
}

/**
 * Bind an EXISTING conversation to a custom dashboard - or unbind it with
 * `null` (Dashboard Studio): the video binding's twin, so the chat that built
 * a dashboard carries into its studio.
 */
export function useBindConversationDashboard() {
  const qc = useQueryClient();
  return useMutation<ConversationListItem, Error, { conversationId: string; dashboardId: string | null }>({
    mutationFn: ({ conversationId, dashboardId }) =>
      api.put(`/chat/conversations/${conversationId}/dashboard`, { custom_dashboard_id: dashboardId }),
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.setQueryData<ConversationDetail>(["conversation", conv.id], (old) =>
        old
          ? { ...old, presentation_id: null, video_project_id: null, custom_dashboard_id: conv.custom_dashboard_id ?? null }
          : old,
      );
    },
  });
}

/**
 * Continue an EXISTING conversation with another agent, in place:
 * it keeps its whole history, the agent answers from the next message and
 * reads the earlier turns labelled with who wrote them. `agentId: null`
 * continues it with the General Assistant. The server writes the note
 * "Continued by <agent>", so the transcript is refetched.
 */
export function useContinueConversationWithAgent() {
  const qc = useQueryClient();
  return useMutation<ConversationListItem, Error, { conversationId: string; agentId: string | null }>({
    mutationFn: ({ conversationId, agentId }) =>
      api.put(`/chat/conversations/${conversationId}/agent`, { agent_id: agentId }),
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["conversation", conv.id] });
    },
  });
}

/** What a studio's conversations are bound to: a deck, a video, a dashboard or a fleet (#10162). */
export type StudioBindingField = "presentation_id" | "video_project_id" | "custom_dashboard_id" | "fleet_id";

/**
 * The conversations bound to one deck — the Presentation Studio rail's
 * history — or to one video (`field`). Keyed under
 * ``["conversations", ...]`` so every existing invalidation (create, rename,
 * delete, a turn's end) refreshes it too.
 */
export function useStudioConversations(
  presentationId: string | undefined,
  limit = 50,
  field: StudioBindingField = "presentation_id",
) {
  return useQuery<ConversationListItem[]>({
    queryKey: ["conversations", "studio", field, presentationId, limit],
    queryFn: () =>
      api.get(`/chat/conversations?limit=${limit}&${field}=${encodeURIComponent(presentationId ?? "")}`),
    enabled: !!presentationId,
  });
}

export function useConversation(
  conversationId: string,
  options: {
    /**
     * Treat a 404 as an ordinary answer (`isError`, no retry) instead of
     * throwing it to the page boundary. For a caller that got the id from a
     * URL it did not mint - the Presentation Studio's `?c=` - "not yours or
     * gone" is a state to recover from, not a fault; 5xx still throws.
     */
    notFoundIsOrdinary?: boolean;
    /**
     * Read the conversation again every few seconds while it says a turn is
     * in flight (`is_processing`): for a host that follows no stream of that
     * turn (a page come back to, or reloaded, during it), so the turn's end and
     * its answer reach the screen without a reload (#10910).
     */
    followTurn?: boolean;
  } = {},
) {
  const followTurn = options.followTurn ?? false;
  const ordinary = options.notFoundIsOrdinary ? notFoundIsOrdinary : null;
  const conversationQuery = useQuery<ConversationDetail>({
    queryKey: ["conversation", conversationId],
    queryFn: () => api.get<ConversationDetail>(`/chat/conversations/${conversationId}`),
    enabled: !!conversationId,
    retry: (failureCount, error) => retryConversationRead(failureCount, error, ordinary === null),
    // Read again at a turn's end while the backend restarts, the conversation
    // on screen stays: only a first read that fails reaches the boundary.
    throwOnError: (error, query) => query.state.data === undefined && (ordinary ? ordinary.throwOnError(error) : true),
    refetchOnWindowFocus: false,
    // A copy cached mid-turn is stale at once: a page that left the
    // conversation during the turn and comes back reads it again, since the
    // turn may have ended meanwhile and the cut card would stay on resuming
    // with no answer until a reload (#10910).
    staleTime: (query) => (query.state.data?.is_processing ? 0 : CONVERSATION_STALE_MS),
    refetchInterval: (query) => (followTurn && query.state.data?.is_processing ? CONVERSATION_TURN_POLL_MS : false),
  });
  // A turn queued for its automatic resume: read the conversation once more
  // after the resume window, so the notice gives way to the working turn, or
  // offers Resume again when the turn left the queue (cap, expiry, AI off).
  // Here rather than in each host, so every surface that draws the notice
  // (the chat page, the studio rails) rechecks.
  useAutoResumeRecheck(
    conversationId,
    !!conversationQuery.data?.interrupted_turn?.auto_resume,
    conversationQuery.refetch,
  );
  return conversationQuery;
}

/** How long a conversation read with no turn in flight stays fresh (the app's default). */
const CONVERSATION_STALE_MS = REFETCH_INTERVAL_SLOW;

/** How often a conversation is read while a turn runs that its host follows no stream of. */
export const CONVERSATION_TURN_POLL_MS = REFETCH_INTERVAL_FAST;

/** How many times a read of the conversation is tried again while the backend is unreachable (about two minutes). */
export const CONVERSATION_BACKEND_RETRIES = 8;

/**
 * Whether the conversation is read again after *failureCount* failures: a
 * backend that is coming back (a restart, a deploy) for about two minutes, and
 * any other failure once when *retriesOthers* (a 404 the caller treats as
 * ordinary is never read again).
 */
export function retryConversationRead(failureCount: number, error: unknown, retriesOthers: boolean): boolean {
  if (error instanceof BackendUnavailableError) return failureCount < CONVERSATION_BACKEND_RETRIES;
  return retriesOthers && failureCount < 1;
}

/** How often the work a conversation waits on is read again while there is some (ms). */
const AWAITED_WORK_POLL_MS = 10_000;

/**
 * The work the conversation waits on, which resumes it once it ends:
 * the Video Studio jobs a turn handed to the background. Read again every
 * {@link AWAITED_WORK_POLL_MS} while there is some, and when a turn ends.
 */
export function useAwaitedWork(conversationId: string | null, enabled = true) {
  return useQuery<AwaitedWorkItem[]>({
    queryKey: ["awaited-work", conversationId],
    queryFn: async () => {
      const result = await api.get<{ items: AwaitedWorkItem[] }>(
        `/chat/conversations/${conversationId}/awaited-work`,
      );
      return result.items ?? [];
    },
    enabled: !!conversationId && enabled,
    ...notFoundIsOrdinary,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => ((query.state.data?.length ?? 0) > 0 ? AWAITED_WORK_POLL_MS : false),
  });
}

/** Stop showing the conversation's interrupted turn; its kept tool work stays for the next turn. */
export function useDismissInterruptedTurn(conversationId: string | null) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => api.delete(`/chat/conversations/${conversationId}/interrupted-turn`),
    onSuccess: () => {
      if (!conversationId) return;
      qc.setQueryData<ConversationDetail>(["conversation", conversationId], (old) =>
        old ? { ...old, interrupted_turn: null } : old,
      );
    },
  });
}

export function useCreateConversation() {
  const qc = useQueryClient();
  return useMutation<
    ConversationListItem,
    Error,
    // `workspace_id` files the new conversation there (a workspace's project
    // page); /chat opens it with `?c=`.
    {
      agent_id?: string;
      title?: string;
      presentation_id?: string;
      video_project_id?: string;
      custom_dashboard_id?: string;
      fleet_id?: string;
      workspace_id?: string;
    }
  >({
    mutationFn: (data) => api.post("/chat/conversations", data),
    // A 403 when chat is gated (CE-only deployment tier): the caller says
    // why in its own toast, not under a generic global one.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.setQueryData<ConversationDetail>(["conversation", conv.id], {
        id: conv.id,
        channel: conv.channel || "web",
        agent_id: conv.agent_id || null,
        transfer_source_agent_id: conv.transfer_source_agent_id ?? null,
        transfer_source_agent_name: conv.transfer_source_agent_name ?? null,
        title: conv.title || "New conversation",
        is_archived: false,
        created_at: conv.created_at,
        updated_at: conv.updated_at,
        messages: [],
        tool_overrides: null,
        presentation_id: conv.presentation_id ?? null,
        video_project_id: conv.video_project_id ?? null,
        custom_dashboard_id: conv.custom_dashboard_id ?? null,
        fleet_id: conv.fleet_id ?? null,
        // Created in a workspace: the page reads it at once, before
        // any refetch - the knowledge drawer and the filing dialog.
        workspace_id: conv.workspace_id ?? null,
      });
    },
  });
}

// --- Conversation files panel --------------------------------------

/**
 * Exact ``detail`` the backend returns when a non-owner admin lists files
 * without a live break-glass acknowledgement.
 *
 * Part of the API contract with ``list_conversation_files`` — the string is
 * matched, not merely displayed, so the panel can raise the confidentiality
 * gate instead of showing a generic "Forbidden". Keep in sync with
 * ``CONFIDENTIALITY_ACK_REQUIRED_DETAIL`` in ``backend/app/api/v1/chat.py``.
 */
const CONFIDENTIALITY_ACK_REQUIRED = "confidentiality_acknowledgement_required";

/**
 * True when a failed files request is the backend asking for a confidentiality
 * acknowledgement rather than a real error.
 *
 * ``lib/api`` turns a 403 into ``new Error(body.detail)``, so the sentinel
 * arrives as the message. Matched with ``includes`` rather than equality
 * because a proxy or middleware that prefixes the detail must not silently
 * turn the gate back into an unexplained error toast.
 */
export function isConfidentialityAckRequired(err: unknown): boolean {
  return (
    err instanceof Error && err.message.includes(CONFIDENTIALITY_ACK_REQUIRED)
  );
}

/**
 * List the files attached to (or generated inside) a conversation.
 *
 * ``enabled`` is the panel's open state, so a mounted-but-closed panel costs no
 * request. For a non-owner admin the request does fire on open and comes back
 * 403 with the acknowledgement sentinel — the *server* withholds the filenames
 * until the break-glass grant exists, which is what makes the gate meaningful:
 * skipping the dialog client-side gains nothing.
 */
export function useConversationFiles(
  conversationId: string | null,
  options: { enabled?: boolean; origin?: ConversationFileOrigin } = {},
) {
  const { enabled = true, origin } = options;
  return useQuery<ConversationFilesPage, Error>({
    queryKey: ["conversation-files", conversationId, origin ?? "all"],
    queryFn: () => {
      const params = new URLSearchParams();
      if (origin) params.set("origin", origin);
      const qs = params.toString();
      return api.get(
        `/chat/conversations/${conversationId}/files${qs ? `?${qs}` : ""}`,
      );
    },
    enabled: !!conversationId && enabled,
    // The gate is a 403 by design — a global error toast on every unacknowledged
    // open would fire alongside the dialog that explains it.
    meta: { suppressGlobalErrorToast: true },
    // Only retry transport/backend-availability failures; app-level 403/404 won't improve by retrying.
    retry: (count, err) =>
      err instanceof Error && err.name === "BackendUnavailableError" && count < 1,
  });
}

/**
 * Record an admin's confidentiality acknowledgement ("break the glass") before
 * viewing another user's conversation files.
 *
 * The server writes the audit entry and mints a short-lived grant; on success
 * the files query is invalidated so the panel loads immediately behind the gate.
 */
export function useAcknowledgeConversationFileAccess(conversationId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ status: string; expires_in_seconds: number }, Error, void>({
    mutationFn: () => {
      if (!conversationId) return Promise.reject(new Error("No conversation"));
      return api.post(
        `/chat/conversations/${conversationId}/files/access-ack`,
        {},
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["conversation-files", conversationId] });
    },
  });
}

// --- Conversation sharing -------------------------------------------

export function useSharedConversations(limit = 50) {
  return useQuery<ConversationListItem[]>({
    queryKey: ["conversations", "shared", limit],
    queryFn: () => api.get(`/chat/conversations?scope=shared&limit=${limit}`),
  });
}

/** A conversation as a Markdown or PDF document: its charts written as tables. */
export function useExportConversation() {
  return useMutation({
    mutationFn: ({ conversationId, format }: { conversationId: string; format: "markdown" | "pdf" }) =>
      api.download(`/chat/conversations/${encodeURIComponent(conversationId)}/export?format=${format}`),
  });
}

/**
 * The sharing state of a conversation: who has access, and the link tier.
 *
 * Owner-only server-side — a non-owner gets a 404, indistinguishable from a
 * conversation that does not exist. ``enabled`` is the dialog's open state so
 * a closed dialog costs no request.
 */
export function useConversationShares(
  conversationId: string | null,
  options: { enabled?: boolean } = {},
) {
  const { enabled = true } = options;
  return useQuery<ConversationSharesState, Error>({
    queryKey: ["conversation-shares", conversationId],
    queryFn: () => api.get(`/chat/conversations/${conversationId}/shares`),
    enabled: !!conversationId && enabled,
  });
}

/**
 * Search users and groups to share with.
 *
 * Gated on {@link SHARE_TARGET_MIN_QUERY_LENGTH} client-side to match the
 * server's validator: firing a request the backend will 422 would just show
 * the user an error for typing the first letter of a name.
 */
export function useShareTargets(query: string) {
  const trimmed = query.trim();
  return useQuery<ConversationShareTargets, Error>({
    queryKey: ["share-targets", trimmed],
    queryFn: () =>
      api.get(`/chat/share-targets?q=${encodeURIComponent(trimmed)}`),
    enabled: trimmed.length >= SHARE_TARGET_MIN_QUERY_LENGTH,
    // The directory changes far more slowly than the user types.
    staleTime: 30_000,
  });
}

/**
 * Minimum query length the share-target search accepts.
 *
 * Mirrors ``SHARE_TARGET_MIN_QUERY_LENGTH`` in ``backend/app/api/v1/chat.py``:
 * the picker is the platform's only non-admin-readable people directory, so it
 * answers "find the colleague I mean", not "list the org".
 */
export const SHARE_TARGET_MIN_QUERY_LENGTH = 2;

/**
 * Grant, revoke, or change the link tier.
 *
 * All three mutations return the server's full sharing state and write it
 * straight into the cache rather than patching a local list: a client that
 * guesses wrong about the outcome of a grant is a client that shows someone
 * as having access when they do not.
 */
export function useConversationShareMutations(conversationId: string | null) {
  const qc = useQueryClient();

  const writeState = (state: ConversationSharesState) => {
    qc.setQueryData(["conversation-shares", conversationId], state);
    // The recipient's "shared with me" list is now stale for *them*, not for
    // us — but the owner's own detail view carries ``link_access``, so refresh
    // it to keep the menu's share indicator honest.
    qc.invalidateQueries({ queryKey: ["conversation", conversationId] });
  };

  const addShare = useMutation<
    ConversationSharesState,
    Error,
    { userId?: string; groupId?: string }
  >({
    mutationFn: ({ userId, groupId }) =>
      api.post(`/chat/conversations/${conversationId}/shares`, {
        user_id: userId ?? null,
        group_id: groupId ?? null,
      }),
    onSuccess: writeState,
  });

  const removeShare = useMutation<ConversationSharesState, Error, string>({
    mutationFn: (shareId) =>
      api.delete(`/chat/conversations/${conversationId}/shares/${shareId}`),
    onSuccess: writeState,
  });

  const setLinkAccess = useMutation<
    ConversationSharesState,
    Error,
    ConversationLinkAccess
  >({
    mutationFn: (linkAccess) =>
      api.put(`/chat/conversations/${conversationId}/link-access`, {
        link_access: linkAccess,
      }),
    onSuccess: writeState,
  });

  return { addShare, removeShare, setLinkAccess };
}

// --- Continuing a conversation in Slack / Teams ---------------------

/**
 * Where a conversation is continued, and where it can be. Owner-only
 * server-side (a 404 for anyone else); ``enabled`` is the dialog's open state.
 */
export function useConversationChannelBindings(
  conversationId: string | null,
  options: { enabled?: boolean } = {},
) {
  const { enabled = true } = options;
  return useQuery<ConversationChannelBindings, Error>({
    queryKey: ["conversation-channel-bindings", conversationId],
    queryFn: () => api.get(`/chat/conversations/${conversationId}/channel-bindings`),
    enabled: !!conversationId && enabled,
  });
}

/**
 * Continue a conversation in the user's direct messages with the Slack or
 * Teams bot, or stop continuing it there. The bot posts the opening message;
 * what it changes is refreshed (``refreshChannelContinuation``), and the
 * mutation stays pending until it is: a control re-enabled over the old state
 * would send a second request (a second Slack thread).
 */
export function useConversationChannelBindingMutations(conversationId: string | null) {
  const qc = useQueryClient();
  const refresh = () => refreshChannelContinuation(qc);

  const continueIn = useMutation<ConversationChannelBinding, Error, ContinuationChannel>({
    mutationFn: (channel) =>
      api.post(`/chat/conversations/${conversationId}/channel-bindings`, { channel }),
    // The dialog shows the refusal in place.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: refresh,
  });

  const stopIn = useMutation<void, Error, ContinuationChannel>({
    mutationFn: (channel) =>
      api.delete(`/chat/conversations/${conversationId}/channel-bindings/${channel}`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: refresh,
  });

  return { continueIn, stopIn };
}

export function useReturnToSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (conversationId: string) =>
      api.post<ConversationDetail>(`/chat/conversations/${conversationId}/return-to-source`),
    onSuccess: (_data, conversationId) => {
      qc.invalidateQueries({ queryKey: ["conversation", conversationId] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/**
 * File a conversation into a workspace, or unfile it with `null`.
 *
 * Separate from `useRenameConversation` even though both PATCH the same
 * endpoint: they are different acts with different failure modes — filing can
 * be refused because the workspace is archived or not the caller's to use,
 * and a rename cannot. Sharing one mutation would mean one error message
 * covering both.
 *
 * The workspaces list is invalidated too: its rows carry the counts, and one
 * of them just changed.
 */
export function useFileConversation() {
  const qc = useQueryClient();
  return useMutation<
    ConversationListItem,
    Error,
    { id: string; workspaceId: string | null }
  >({
    mutationFn: ({ id, workspaceId }) =>
      api.patch(`/chat/conversations/${id}`, { workspace_id: workspaceId }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["conversation", vars.id] });
      qc.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}
