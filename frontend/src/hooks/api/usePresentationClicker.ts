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
 * Hand the clicker (xtm-one #10552, slice B2): the client half of the
 * server relay merged in #10694 (`backend/app/services/presentation_clicker.py`).
 *
 * - `usePresentationClicker` reads who presents the deck and who holds its
 *   clicker. The holder's read keeps the clicker alive on the server, so the
 *   holder polls well inside the server's TTL; a holder whose tab died stops
 *   holding it and control falls back to the presenter on its own.
 * - `useHandClicker` / `useReleaseClicker` hand it to a person who can already
 *   read the deck, and take it back (presenter) or give it back (holder).
 * - `sendClickerCommand` is the holder's next / previous / go to slide.
 * - `useClickerRelay` is the presenter's side: the holder's commands arrive on
 *   the presenter's activity stream (`/activity/stream`, frames of type
 *   `presentation_clicker_command`). The deck's standalone windows live outside
 *   the app shell that opens that stream, so the window that obeys (the speaker
 *   view) opens its own while someone else holds the clicker, and only then.
 */
import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { API_BASE, api, getToken } from "@/lib/api";
import { streamRetry } from "@/lib/activity-stream-retry";
import { PRESENTATIONS_KEY } from "@/hooks/api/usePresentationApi";

export type ClickerCommand = "prev" | "next" | "goto";

export interface PresentationClicker {
  presenter_id: string | null;
  holder: { user_id: string; name: string | null } | null;
  handed_at: number | null;
  is_presenter: boolean;
  is_holder: boolean;
  /** The slide the presenter is on (0-based), from their presence beat. */
  slide_index: number | null;
  ttl_seconds: number;
}

/** The activity frame a holder's command travels in. */
export interface ClickerCommandFrame {
  type: "presentation_clicker_command";
  presentation_id: string;
  command: ClickerCommand;
  index: number | null;
  from_user_id: string;
}

/** Poll while the clicker matters to this window: well inside the 45 s TTL. */
export const CLICKER_ACTIVE_POLL_MS = 5_000;
/** Poll while nobody holds it, so a hand-over is seen without a reload. */
export const CLICKER_IDLE_POLL_MS = 20_000;

export const clickerKey = (id: string | undefined) => [...PRESENTATIONS_KEY, id, "clicker"] as const;

/** The polling cadence for a clicker state: fast while this window takes part in a hand-over. */
export function clickerPollMs(state: PresentationClicker | undefined): number {
  if (state?.holder && (state.is_holder || state.is_presenter)) return CLICKER_ACTIVE_POLL_MS;
  return CLICKER_IDLE_POLL_MS;
}

export function usePresentationClicker(id: string | undefined, enabled = true) {
  return useQuery<PresentationClicker>({
    queryKey: clickerKey(id),
    queryFn: () => api.get<PresentationClicker>(`/presentations/${id}/clicker`),
    enabled: !!id && enabled,
    throwOnError: false,
    retry: false,
    refetchInterval: (query) => clickerPollMs(query.state.data),
    refetchIntervalInBackground: true,
    staleTime: 0,
  });
}

export function useHandClicker(id: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => api.put<PresentationClicker>(`/presentations/${id}/clicker`, { user_id: userId }),
    onSuccess: (state) => queryClient.setQueryData(clickerKey(id), state),
    // The dialog words the refusal itself (`handClickerRefusal`): one toast, not two.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Why a hand-over was refused, as the dialog says it: keyed off the HTTP status. */
export function handClickerRefusal(error: unknown): "cannot_open" | "unavailable" | "other" {
  const status = (error as { status?: number } | null)?.status;
  if (status === 404) return "cannot_open";
  if (status === 503) return "unavailable";
  return "other";
}

export function useReleaseClicker(id: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete<void>(`/presentations/${id}/clicker`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: clickerKey(id) }),
  });
}

export function sendClickerCommand(id: string, command: ClickerCommand, index?: number): Promise<void> {
  return api.post<void>(`/presentations/${id}/clicker/command`, {
    command,
    index: command === "goto" ? (index ?? 0) : null,
  });
}

/** A frame of the activity stream that is a clicker command for `presentationId`, else null. */
export function parseClickerCommand(data: unknown, presentationId: string): ClickerCommandFrame | null {
  let frame: unknown = data;
  if (typeof data === "string") {
    try {
      frame = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (!frame || typeof frame !== "object") return null;
  const candidate = frame as Partial<ClickerCommandFrame>;
  if (candidate.type !== "presentation_clicker_command" || candidate.presentation_id !== presentationId) return null;
  if (candidate.command !== "prev" && candidate.command !== "next" && candidate.command !== "goto") return null;
  if (candidate.command === "goto" && typeof candidate.index !== "number") return null;
  return candidate as ClickerCommandFrame;
}

/**
 * Obey the holder's commands for `presentationId` while `enabled` (this window
 * is the presenter's and someone holds the clicker). Reconnects with the same
 * backoff as the app's activity stream.
 */
export function useClickerRelay(
  presentationId: string | undefined,
  enabled: boolean,
  onCommand: (frame: ClickerCommandFrame) => void,
): void {
  const handler = useRef(onCommand);
  handler.current = onCommand;

  useEffect(() => {
    if (!presentationId || !enabled) return;
    let disposed = false;
    let failures = 0;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const token = getToken();
      if (disposed || !token) return;
      let opened = false;
      const es = new EventSource(`${API_BASE}/activity/stream?token=${token}`);
      source = es;
      es.onopen = () => {
        opened = true;
        failures = 0;
      };
      es.onmessage = (event) => {
        const frame = parseClickerCommand(event.data, presentationId);
        if (frame) handler.current(frame);
      };
      es.onerror = () => {
        es.close();
        if (source === es) source = null;
        if (disposed) return;
        if (!opened) failures += 1;
        timer = setTimeout(connect, streamRetry(opened ? 0 : failures).delayMs);
      };
    };
    connect();

    return () => {
      disposed = true;
      if (timer !== null) clearTimeout(timer);
      source?.close();
    };
  }, [presentationId, enabled]);
}
