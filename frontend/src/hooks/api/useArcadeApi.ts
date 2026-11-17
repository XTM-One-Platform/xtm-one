// SPDX-License-Identifier: Apache-2.0

/**
 * The waiting arcade: leaderboards, the player profile and game sessions.
 *
 * Every hook here is a side-show to the chat, so none of them may take a page
 * down: queries opt out of the global `throwOnError` (a board that fails to
 * load is an empty board) and mutations out of the global error toast (the
 * arcade says what went wrong in its own screens, not over the chat).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  ArcadeFinishResult,
  ArcadeHeartbeat,
  ArcadeLeaderboard,
  ArcadePeriod,
  ArcadeProfile,
  ArcadeRunSubmission,
  ArcadeSessionStart,
} from "@/types";

const ARCADE_LEADERBOARD_KEY = ["arcade", "leaderboard"] as const;
const ARCADE_PROFILE_KEY = ["arcade", "profile"] as const;

export function useArcadeLeaderboard(
  period: ArcadePeriod,
  options: { enabled?: boolean; refetchInterval?: number | false; limit?: number } = {},
) {
  const limit = options.limit ?? 20;
  return useQuery<ArcadeLeaderboard>({
    queryKey: [...ARCADE_LEADERBOARD_KEY, period, limit],
    queryFn: () => api.get(`/arcade/leaderboard?period=${period}&limit=${limit}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval ?? false,
    staleTime: 15_000,
    retry: false,
    throwOnError: false,
  });
}

export function useArcadeProfile(options: { enabled?: boolean } = {}) {
  return useQuery<ArcadeProfile>({
    queryKey: ARCADE_PROFILE_KEY,
    queryFn: () => api.get("/arcade/profile"),
    enabled: options.enabled ?? true,
    staleTime: 30_000,
    retry: false,
    throwOnError: false,
  });
}

export function useUpdateArcadeProfile() {
  const qc = useQueryClient();
  return useMutation<ArcadeProfile, Error, { callsign?: string | null; hidden?: boolean }>({
    mutationFn: (body) => api.put("/arcade/profile", body),
    // The profile form shows the refusal (a taken or invalid callsign) inline.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (profile) => {
      qc.setQueryData(ARCADE_PROFILE_KEY, profile);
      void qc.invalidateQueries({ queryKey: ARCADE_LEADERBOARD_KEY });
    },
  });
}

export function useStartArcadeSession() {
  return useMutation<ArcadeSessionStart, Error, { mode: "wait" | "free" }>({
    mutationFn: (body) => api.post("/arcade/sessions", body),
    // No session = an offline game; the arcade says so itself.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useArcadeHeartbeat() {
  return useMutation<ArcadeHeartbeat, Error, { sessionId: string; score: number; wave: number }>({
    mutationFn: ({ sessionId, score, wave }) =>
      api.post(`/arcade/sessions/${sessionId}/heartbeat`, { score, wave }),
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useFinishArcadeSession() {
  const qc = useQueryClient();
  return useMutation<ArcadeFinishResult, Error, { sessionId: string; run: ArcadeRunSubmission }>({
    mutationFn: ({ sessionId, run }) => api.post(`/arcade/sessions/${sessionId}/finish`, run),
    meta: { suppressGlobalErrorToast: true },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ARCADE_LEADERBOARD_KEY });
      void qc.invalidateQueries({ queryKey: ARCADE_PROFILE_KEY });
    },
  });
}
