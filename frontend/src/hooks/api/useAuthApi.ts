// SPDX-License-Identifier: Apache-2.0

/** Auth, user, and profile API hooks. */

import { keepPreviousData, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, setToken } from "@/lib/api";
import type {
  User,
  TokenResponse,
  AuthConfig,
  UserDirectoryItem,
  UserListItem,
  UserDetail,
  UserStats,
  UserIntegrationItem,
  UserMCPServerItem,
  UserCustomToolItem,
  CreateUserRequest,
  CreateUserResponse,
  UserAgentItem,
  UserKnowledgeBaseItem,
  ImpersonationResponse,
  QuotaConfig,
  UserQuotaInfo,
  PasswordResetResponse,
  ValidatePasswordResetResponse,
  TermsOfUseState,
  LoginResponse,
  MfaState,
  MfaSetup,
  MfaRecoveryCodes,
  SessionStatus,
  CopilotDashboardLayout,
  CtemDashboardLayout,
  CtemVertical,
} from "@/types";

// --- Auth ---
/** The signed-in person; `enabled: false` where a page holds no session (an export's render, a published link). */
export function useMe(enabled = true) {
  return useQuery<User>({ queryKey: ["me"], queryFn: () => api.get("/auth/me"), enabled });
}

/** The same `["me"]` query for a host mounted above the routes and their
 *  error boundaries (`AppIntlProvider`, `ArcadeProvider`): idle without a
 *  session - the sign-in pages render under it - and never thrown, since no
 *  boundary sits above it to catch the error. Pages keep {@link useMe}. */
export function useMeAboveRoutes(signedIn: boolean) {
  return useQuery<User>({
    queryKey: ["me"],
    queryFn: () => api.get("/auth/me"),
    enabled: signedIn,
    throwOnError: false,
  });
}

/** Whether the stored session still holds, for a surface that cannot read its
 *  own refusal - the activity stream's `EventSource`. A request
 *  through the API client, whose 401 handling ends a dead session (sign-in
 *  page, or the admin session after an impersonation); outside the `["me"]`
 *  query, so an outage it meets never reaches that query's observers. */
export function checkSession(): Promise<User> {
  return api.get<User>("/auth/me");
}

// --- Session lifetime and idle timeout ---

const SESSION_STATUS_QUERY_KEY = ["auth-session"] as const;

/** When the session ends. Read again by the timers of `useSessionTimeout`, never on a
 *  schedule of its own: reading it moves no deadline, and a 401 signs out (`api.ts`).
 *  A failed read is retried like any query; until one succeeds, the next activity
 *  signal's answer fills it in. */
export function useSessionStatus() {
  return useQuery<SessionStatus>({
    queryKey: SESSION_STATUS_QUERY_KEY,
    queryFn: () => api.get("/auth/session"),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    throwOnError: false,
  });
}

/** The person did something: their session's idle deadline moves on. Only on real
 *  input (`useSessionTimeout`), never from a refresh or a poll. */
export function useRecordSessionActivity() {
  const qc = useQueryClient();
  return useMutation<SessionStatus, Error, void>({
    mutationFn: () => api.post("/auth/session/activity"),
    onSuccess: (status) => qc.setQueryData(SESSION_STATUS_QUERY_KEY, status),
    // A missed signal costs nothing the next one does not repair, and it is
    // sent in the background: no toast, and nothing else to refresh.
    meta: { readOnly: true, suppressGlobalErrorToast: true },
  });
}

export function useUpdateProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      full_name?: string;
      title?: string;
      timezone?: string | null;
      theme?: string;
      language?: string;
      ctem_vertical?: User["ctem_vertical"];
      ctem_splash_seen?: boolean;
      /** The fleets invitation closed: what the person saw then; null shows it again. */
      ctem_fleet_invite_dismissed?: { fleets: string[]; modules: string[] } | null;
      /** The home dashboard's context notices closed (#11586), written whole; null shows them again. */
      organization_context_notice_dismissed?: string[] | null;
      /** The fleets the command center shows, in order; null restores the built-in ones (#11464). */
      ctem_dashboard_fleet_ids?: string[] | null;
      /** The fleets the Copilot home shows, in order; null restores every fleet (#12063). */
      copilot_dashboard_fleet_ids?: string[] | null;
      ctem_dashboard_layout?: { vertical: CtemVertical; layout: CtemDashboardLayout | null };
      // The whole Copilot dashboard layout (one, no verticals): the user
      // field of the same name, patched as sent.
      copilot_dashboard_layout?: CopilotDashboardLayout | null;
      default_agent_id?: string;
      /** One of the caller's custom dashboards for /dashboard; null = the platform's. */
      home_dashboard_id?: string | null;
      ga_builtin_tool_ids?: string[] | null;
      ga_custom_tool_ids?: string[] | null;
      ga_integration_bindings?: Record<string, string[]> | null;
      ga_mcp_server_ids?: string[] | null;
      ga_knowledge_base_ids?: string[] | null;
      ga_browser_profile_ids?: string[] | null;
      channel_agent_config?: Record<string, string> | null;
      mcp_exposed_agent_ids?: string[] | null;
      favorite_agent_ids?: string[] | null;
      oauth_share_dismissed_integration_ids?: string[] | null;
      /** The chat sidebar's workspace groups shown expanded, written whole. */
      chat_sidebar_expanded_workspaces?: string[] | null;
      /** The bell shows the work items waiting for this person (#9437). */
      notify_waiting_work_items?: boolean;
      /** The composer's reply suggestion after a chat turn. */
      reply_suggestions_enabled?: boolean;
    }) =>
      api.put("/settings/preferences", data),
    // Serialized: every preference write PUTs a whole field value, so two
    // in-flight updates race and whichever lands last wins on the server —
    // star two agents quickly and the second could be lost on reload even
    // though the optimistic UI showed it. A shared scope makes TanStack queue
    // them instead, so each write is sent from the state the previous one
    // produced. The cost is that a queued mutation's `onMutate` (and so its
    // optimistic patch) waits for the one ahead of it to settle: under rapid
    // clicking the flip can lag by one round-trip. Correctness wins — a lost
    // write is invisible and permanent, a brief lag is neither.
    scope: { id: "user-preferences" },
    // Optimistically merge the patch into the cached `["me"]` user so any UI
    // that derives from it (a favourite star, a switch) flips instantly
    // and stays correct under rapid clicks, instead of waiting for the
    // round-trip + the `["me"]` refetch. Without this, a preference only
    // updated after the post-success refetch landed, so a quick re-click read a
    // stale value and sent a redundant (dropped) update. `cancelQueries` stops
    // an in-flight refetch from clobbering the optimistic value; `onError`
    // rolls back; `onSettled` reconciles with the server (last write wins).
    onMutate: async (data) => {
      await qc.cancelQueries({ queryKey: ["me"] });
      const previous = qc.getQueryData<User>(["me"]);
      if (previous) {
        // One vertical's dashboard layout is sent, the user carries them all:
        // merge it into `ctem_dashboard_layouts` as the server does.
        const { ctem_dashboard_layout: layoutUpdate, ...fields } = data;
        const next = { ...previous, ...fields } as User;
        if (layoutUpdate) {
          const layouts = { ...(previous.ctem_dashboard_layouts ?? {}) };
          if (layoutUpdate.layout) layouts[layoutUpdate.vertical] = layoutUpdate.layout;
          else delete layouts[layoutUpdate.vertical];
          next.ctem_dashboard_layouts = Object.keys(layouts).length > 0 ? layouts : null;
        }
        qc.setQueryData<User>(["me"], next);
      }
      return { previous };
    },
    onError: (_err, _data, context) => {
      if (context?.previous) qc.setQueryData(["me"], context.previous);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}

export function useAuthConfig() {
  return useQuery<AuthConfig>({
    queryKey: ["auth-config"],
    queryFn: () => api.get("/auth/config"),
    staleTime: 60_000,
    /* Opted out of the global `throwOnError: true` (main.tsx), same as
       `usePlatformConfig`. This query is no longer page-scoped: `Header` and
       `CommandsProvider` read it through `useFeatureFlag`, so it now runs
       inside `AppLayout` — under the *fullPage* ErrorBoundary. Throwing there
       means one transient 5xx on `/auth/config` replaces the entire app with
       the error screen instead of degrading one control. Every consumer here
       reads optional config (feature flags, `runners_enabled`, SSO providers)
       and handles `undefined`, so degrading beats an unmount.

       The trade-off is that this query no longer announces its own failure, so
       consumers that cannot work without it must say so themselves — `Login`
       has an `isError` branch and `useFeatureFlagResolved` reports `isError`
       for route guards. Silencing the query without those is worse than
       throwing: a login form that can never succeed, or a redirect that looks
       like the feature does not exist.

       Its ~15 other consumers degrade silently, and that is knowingly
       accepted rather than unnoticed. On a transient 5xx:

       - `Settings` reads `VIDEO_STUDIO` as off and drops the video models
         and video-only providers;
       - `ForgotPassword` loses its "SSO accounts cannot reset a password" note;
       - `NewUser` / `Profile` / `ResetPassword` fall back to the client
         `MIN_PASSWORD_LENGTH`, so a stricter server policy shows inline
         validation that passes and then a server rejection;
       - `ToolingSection` assumes `runners_enabled` and offers a Computer Use
         card that will fail.

       Each is a wrong-but-recoverable default on a page the user is already
       looking at, which is why none of them justifies taking the app down
       from the header. If that calculus changes, the fix is to parameterize
       this hook — `throwOnError` is an observer-level option, so page-scoped
       callers can opt back into throwing while the layout ones stay quiet —
       rather than to flip the default back.

       Retries stay at the app default (main.tsx: two attempts for network
       errors, one retry otherwise) rather than `usePlatformConfig`'s three.
       Nothing blocks on the platform config, whereas `/flows` and
       `/flows/library/:id` now hold a skeleton until this settles — three
       retries would put a full 1s + 2s + 4s backoff in front of the page on a
       single transient 500. */
    throwOnError: false,
  });
}

export function useLogin() {
  return useMutation<LoginResponse, Error, { email: string; password: string }>({
    mutationFn: (data) => api.post("/auth/login", data),
    // Only store a token when the server actually issued one. With 2FA enabled
    // the response carries `mfa_required` and NO `access_token`, and
    // the caller advances to the code step instead — storing an undefined token
    // here would leave the app looking signed-in with a broken session.
    onSuccess: (data) => { if (data.access_token) setToken(data.access_token); },
  });
}

// --- TOTP two-factor authentication ---

/** Second half of the login: exchange the challenge token + a code (TOTP or
 *  recovery) for a real session token. Public — the caller holds no session
 *  yet, which is the whole point. */
export function useVerifyMfa() {
  return useMutation<TokenResponse, Error, { mfa_token: string; code: string }>({
    mutationFn: (body) => api.post("/auth/mfa/verify", body),
    onSuccess: (data) => setToken(data.access_token),
  });
}

/** Begin enrollment: mint a secret and return the QR + manual entry key.
 *  Not a query — it has a side effect (it replaces any pending secret), so it
 *  must never be re-run by a cache refetch or a window refocus. */
export function useSetupMfa() {
  return useMutation<MfaSetup, Error, void>({
    mutationFn: () => api.post("/auth/mfa/setup"),
  });
}

/** Confirm enrollment with a live code. Returns the recovery codes — the only
 *  time they are ever readable, so the caller must render them before the user
 *  can navigate away. */
export function useEnableMfa() {
  const qc = useQueryClient();
  return useMutation<MfaRecoveryCodes, Error, { code: string }>({
    mutationFn: (body) => api.post("/auth/mfa/enable", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}

export function useDisableMfa() {
  const qc = useQueryClient();
  return useMutation<MfaState, Error, { password?: string; code: string }>({
    mutationFn: (body) => api.post("/auth/mfa/disable", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}

/** Issue a fresh recovery set, invalidating the previous one. Requires a live
 *  TOTP code specifically — a recovery code cannot mint ten more. */
export function useRegenerateRecoveryCodes() {
  const qc = useQueryClient();
  return useMutation<MfaRecoveryCodes, Error, { code: string }>({
    mutationFn: (body) => api.post("/auth/mfa/recovery-codes", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}

/** Admin lockout escape hatch: clear a user's enrollment so they can re-enroll
 *  on a device they actually hold. */
export function useResetUserMfa() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (userId) => api.delete(`/users/${userId}/mfa`),
    onSuccess: (_d, userId) => {
      qc.invalidateQueries({ queryKey: ["user-detail", userId] });
      qc.invalidateQueries({ queryKey: ["users"] });
    },
  });
}

export function useRegister() {
  return useMutation<TokenResponse, Error, { email: string; password: string; full_name: string }>({
    mutationFn: (data) => api.post("/auth/register", data),
    onSuccess: (data) => setToken(data.access_token),
  });
}

// --- Terms of Use acceptance gate ---
//
// All three endpoints are exempt from the server-side gate (they depend on
// `get_current_user_ungated`), so they stay callable while the user is still
// blocked — otherwise accepting would 403 on the very call that clears it.

export function useAcceptTerms() {
  const qc = useQueryClient();
  return useMutation<TermsOfUseState, Error, { version: string }>({
    mutationFn: (body) => api.post("/auth/terms/accept", body),
    // Refetch `["me"]` so `TermsGate` re-evaluates and drops out of the tree.
    // Not optimistic on purpose: this is a legal consent record, so the UI
    // should only unblock once the server confirms it stored one.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["terms-of-use"] });
    },
  });
}

/** Records the refusal in the audit trail. The caller signs the user out
 *  immediately after — declining changes no state, it just makes a refusal
 *  distinguishable from a closed tab. */
export function useDeclineTerms() {
  return useMutation<TermsOfUseState, Error, void>({
    mutationFn: () => api.post("/auth/terms/decline"),
  });
}

// --- Password Change (self-service) ---
export function useChangePassword() {
  return useMutation<{ status: string; access_token?: string }, Error, { current_password: string; new_password: string }>({
    mutationFn: (body) => api.put("/auth/password", body),
    // Changing the password invalidates older tokens server-side; store the
    // fresh one so the current session stays signed in.
    onSuccess: (data) => { if (data.access_token) setToken(data.access_token); },
  });
}

// --- Admin password management ---
export function useSetUserPassword() {
  return useMutation<void, Error, { userId: string; new_password: string }>({
    mutationFn: ({ userId, new_password }) =>
      api.put(`/users/${userId}/password`, { new_password }),
  });
}

export function useSendPasswordReset() {
  return useMutation<PasswordResetResponse, Error, string>({
    mutationFn: (userId) => api.post(`/users/${userId}/password-reset`),
  });
}

// --- Self-service password reset request (public) ---
export function useForgotPassword() {
  return useMutation<{ status: string; message: string }, Error, { email: string }>({
    mutationFn: (body) => api.post("/auth/forgot-password", body),
  });
}

// --- Password reset via link (public) ---
export function useValidatePasswordReset(token: string | null) {
  return useQuery<ValidatePasswordResetResponse>({
    queryKey: ["password-reset-validate", token],
    queryFn: () => api.get(`/auth/reset-password/${token}`),
    enabled: !!token,
    retry: false,
  });
}

export function useResetPassword() {
  return useMutation<LoginResponse, Error, { token: string; new_password: string }>({
    mutationFn: (body) => api.post("/auth/reset-password", body),
    // Same contract as `useLogin`: an account with two-factor enabled has its
    // password reset but gets NO session until a code is verified.
    // Otherwise the reset link would be a complete bypass of the factor.
    onSuccess: (data) => { if (data.access_token) setToken(data.access_token); },
  });
}

// --- Users (admin) ---

/** The Users table's query (`GET /users`, #11940): every filter is one value or several comma-separated. */
export interface UserTableQuery {
  search?: string;
  role?: string;
  status?: string;
  group?: string;
  created?: string;
  sort?: string;
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

function userCountQuery(query: UserTableQuery): string {
  const params = new URLSearchParams();
  for (const key of ["search", "role", "status", "group", "created"] as const) {
    if (query[key]) params.set(key, query[key]);
  }
  return params.toString();
}

/** One page of the Users table, searched, filtered and sorted on the server. */
export function useUsersPage(query: UserTableQuery) {
  const params = new URLSearchParams(userCountQuery(query));
  if (query.sort) params.set("sort", query.sort);
  if (query.direction) params.set("direction", query.direction);
  if (query.limit) params.set("limit", String(query.limit));
  if (query.offset) params.set("offset", String(query.offset));
  const qs = params.toString();
  return useQuery<UserListItem[]>({
    queryKey: ["users", "page", qs],
    queryFn: () => api.get(`/users?${qs}`),
    placeholderData: keepPreviousData,
  });
}

/** How many users the Users table's filters match: its total. */
export function useUsersCount(query: UserTableQuery) {
  const qs = userCountQuery(query);
  return useQuery<{ count: number }>({
    queryKey: ["users", "count", qs],
    queryFn: () => api.get(`/users/count${qs ? `?${qs}` : ""}`),
    placeholderData: keepPreviousData,
  });
}

export function useUsers(enabled = true) {
  return useQuery<UserListItem[]>({
    queryKey: ["users"],
    queryFn: () => api.get("/users"),
    enabled,
  });
}

/**
 * Search active users for a per-user share picker.
 *
 * Distinct from {@link useUsers}, which hits the admin-only `/users` endpoint:
 * this one is readable by any authenticated user and returns names and emails
 * only. The backend requires a search term, so the query stays disabled until
 * the user has typed two characters — there is no "list everyone" mode to fall
 * back to, by design.
 */
/**
 * Resolve already-granted user ids back into names.
 *
 * The search mode above only ever returns matches for the *current* term, so
 * an entity loaded with existing grants has no way to label them — they would
 * render as bare UUIDs. This mode takes the ids the entity already stores and
 * deliberately includes deactivated accounts, so a grant that outlived its
 * grantee stays legible and removable rather than becoming an unexplained id.
 */
export function useUserDirectoryByIds(ids: string[] | null | undefined) {
  const key = [...(ids ?? [])].sort().join(",");
  return useQuery<UserDirectoryItem[]>({
    queryKey: ["user-directory-ids", key],
    queryFn: () => api.get(`/users/directory?ids=${encodeURIComponent(key)}`),
    enabled: key.length > 0,
    staleTime: 60_000,
  });
}

export function useUserDirectory(query: string) {
  const term = query.trim();
  return useQuery<UserDirectoryItem[]>({
    queryKey: ["user-directory", term],
    queryFn: () => api.get(`/users/directory?q=${encodeURIComponent(term)}`),
    enabled: term.length >= 2,
    // Names and emails barely change; keep results while the user edits the
    // term so the dropdown does not flicker between keystrokes.
    staleTime: 60_000,
    placeholderData: (previous) => previous,
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation<CreateUserResponse, Error, CreateUserRequest>({
    mutationFn: (data) => api.post("/users", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => api.delete(`/users/${userId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useUpdateUserRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      api.put(`/users/${userId}/role`, { role }),
    onSuccess: (_d, { userId }) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["user-detail", userId] });
    },
  });
}

/** An administrator changes a user's email: the one way it changes (a person never sets their own). */
export function useUpdateUserEmail() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, email }: { userId: string; email: string }) =>
      api.put<{ status: string; user_id: string; email: string }>(`/users/${userId}/email`, { email }),
    onSuccess: (_d, { userId }) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["user-detail", userId] });
    },
  });
}

export function useToggleUserActive() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => api.put(`/users/${userId}/active`, {}),
    onSuccess: (_d, userId) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["user-detail", userId] });
    },
  });
}

export function useUser(userId: string | undefined) {
  return useQuery<UserDetail>({
    queryKey: ["user-detail", userId],
    queryFn: () => api.get(`/users/${userId}`),
    enabled: !!userId,
  });
}

export function useUserStats(userId: string | undefined) {
  return useQuery<UserStats>({
    queryKey: ["user-stats", userId],
    queryFn: () => api.get(`/users/${userId}/stats`),
    enabled: !!userId,
  });
}

export function useUserIntegrations(userId: string | undefined) {
  return useQuery<UserIntegrationItem[]>({
    queryKey: ["user-integrations", userId],
    queryFn: () => api.get(`/users/${userId}/integrations`),
    enabled: !!userId,
  });
}

export function useUserMCPServers(userId: string | undefined) {
  return useQuery<UserMCPServerItem[]>({
    queryKey: ["user-mcp-servers", userId],
    queryFn: () => api.get(`/users/${userId}/mcp-servers`),
    enabled: !!userId,
  });
}

export function useUserAgents(userId: string | undefined) {
  return useQuery<UserAgentItem[]>({
    queryKey: ["user-agents", userId],
    queryFn: () => api.get(`/users/${userId}/agents`),
    enabled: !!userId,
  });
}

export function useUserKnowledgeBases(userId: string | undefined) {
  return useQuery<UserKnowledgeBaseItem[]>({
    queryKey: ["user-knowledge-bases", userId],
    queryFn: () => api.get(`/users/${userId}/knowledge-bases`),
    enabled: !!userId,
  });
}

export function useUserCustomTools(userId: string | undefined) {
  return useQuery<UserCustomToolItem[]>({
    queryKey: ["user-custom-tools", userId],
    queryFn: () => api.get(`/users/${userId}/custom-tools`),
    enabled: !!userId,
  });
}

export function useImpersonateUser() {
  return useMutation<ImpersonationResponse, Error, string>({
    mutationFn: (userId: string) => api.post(`/users/${userId}/impersonate`),
  });
}

// --- User Quotas ---

export function useUserQuotas(userId: string | undefined) {
  return useQuery<UserQuotaInfo>({
    queryKey: ["user-quotas", userId],
    queryFn: () => api.get(`/users/${userId}/quotas`),
    enabled: !!userId,
  });
}

export function useUpdateUserQuotas() {
  const qc = useQueryClient();
  return useMutation<{ quota_config: QuotaConfig | null }, Error, { userId: string; quota_config: QuotaConfig | null }>({
    mutationFn: ({ userId, quota_config }) => api.put(`/users/${userId}/quotas`, { quota_config }),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["user-quotas", vars.userId] });
    },
  });
}

export function useResetUserQuotas() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (userId: string) => api.post(`/users/${userId}/quotas/reset`),
    onSuccess: (_, userId) => {
      qc.invalidateQueries({ queryKey: ["user-quotas", userId] });
    },
  });
}
