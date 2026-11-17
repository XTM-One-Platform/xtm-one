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
 * XTM One Desktop hooks - the React face of `@/lib/desktop.ts`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import {
  detectVisitorArm64,
  getDesktopBridge,
  readLocalExecutionPreference,
  runnersEnabledFrom,
  writeLocalExecutionPreference,
} from "@/lib/desktop";
import type { DesktopBridge } from "@/types";

/** The Client Hints surface Chromium exposes for the CPU architecture. */
interface UserAgentDataLike {
  getHighEntropyValues?: (hints: string[]) => Promise<{ architecture?: string }>;
}

/**
 * Whether the visitor's CPU is arm64, for the download page's primary button.
 * The user-agent string says so on Linux (`aarch64`) but NOT on Windows on
 * ARM, where Chromium reports `Win64; x64`; Client Hints
 * (`getHighEntropyValues(["architecture"])`) do, asynchronously - so the
 * synchronous UA guess is the first render and the hint corrects it.
 */
export function useVisitorArm64(): boolean {
  const [arm64, setArm64] = useState(() => detectVisitorArm64());
  useEffect(() => {
    const uaData = (navigator as Navigator & { userAgentData?: UserAgentDataLike }).userAgentData;
    let cancelled = false;
    uaData?.getHighEntropyValues?.(["architecture"])
      .then((values) => {
        if (!cancelled && values.architecture === "arm") setArm64(true);
      })
      .catch(() => {
        /* the hint is optional; the UA guess stands */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return arm64;
}

/**
 * The bridge the native shell injected, or `null` on the web. Stable for the
 * page's lifetime (the shell never changes it after load), so consumers can
 * put it in dependency arrays.
 */
export function useDesktopBridge(): DesktopBridge | null {
  return useMemo(() => getDesktopBridge(), []);
}

/**
 * Whether the runner subsystem - and with it XTM One Desktop - is on for this
 * deployment (`RUNNERS_ENABLED`). Gates the Desktop page, its sidebar and
 * user-menu entries and its palette command together, so switching it off
 * never leaves a dead link behind. `undefined` until `/auth/config` answers
 * (`runnersEnabledFrom`): an entry point renders nothing until then, and a
 * page gated on it waits instead of redirecting.
 */
export function useRunnersEnabled(): boolean | undefined {
  const { data: authConfig, isPending } = useAuthConfig();
  return runnersEnabledFrom(authConfig, isPending);
}

/**
 * "May the agent act on this machine in this conversation?" - the bridge's
 * runner id when yes, `null` otherwise. Combines the Desktop presence with the
 * per-conversation switch; the Chat page sends the result as
 * `desktop_runner_id` on every turn. Default ON inside the app (that is what
 * the app is for), remembered per conversation for the session.
 */
export function useDesktopLocalExecution(conversationId: string | null): {
  bridge: DesktopBridge | null;
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
  /**
   * A new chat (no id yet) just became `newConversationId`: store the choice
   * made on the draft under that id BEFORE the page switches to it, so the
   * switch-over reads it back instead of the default (the switch would
   * otherwise flip back on after the first message of a chat the person
   * turned it off for). Explicit rather than inferred from the id changing
   * away from `null`: leaving a draft for an EXISTING conversation must read
   * that conversation's own choice, never overwrite it with the draft's.
   * A no-op when the page is already on a conversation.
   */
  carryOver: (newConversationId: string) => void;
  desktopRunnerId: string | null;
} {
  const bridge = useDesktopBridge();
  const [enabled, setEnabledState] = useState(() => readLocalExecutionPreference(conversationId));
  // The latest value, readable from `carryOver` without re-creating it on
  // every toggle.
  const enabledRef = useRef(enabled);
  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);
  // A different conversation has its own remembered choice.
  useEffect(() => {
    setEnabledState(readLocalExecutionPreference(conversationId));
  }, [conversationId]);
  const setEnabled = useCallback(
    (next: boolean) => {
      setEnabledState(next);
      writeLocalExecutionPreference(conversationId, next);
    },
    [conversationId],
  );
  const carryOver = useCallback(
    (newConversationId: string) => {
      if (conversationId !== null) return;
      writeLocalExecutionPreference(newConversationId, enabledRef.current);
    },
    [conversationId],
  );
  const desktopRunnerId = bridge?.runner_id && enabled ? bridge.runner_id : null;
  return { bridge, enabled, setEnabled, carryOver, desktopRunnerId };
}
