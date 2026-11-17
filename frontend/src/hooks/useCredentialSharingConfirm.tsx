// SPDX-License-Identifier: Apache-2.0

import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CredentialSharingDialog, orphanedAgentsOf, type OrphanedAgent } from "@/components/ui/credential-sharing-notice";
import { api } from "@/lib/api";
import { useResourceExposurePreview } from "@/hooks/api/useExposureApi";
import {
  toGenericPreviewRequest,
  type FlatPreviewRequest,
  type GenericPreviewRequest,
} from "@/lib/credential-exposure";
import { DELETE_PREVIEW_WAIT_MS, settleWithin } from "@/lib/preview-wait";
import type { Agent, ResourcePreviewItem, ResourcePreviewRequest } from "@/types";

/** The agent's bindings without one integration (an unbind write). */
export function bindingsWithout(agent: Pick<Agent, "integration_bindings">, integrationId: string): Record<string, string[]> {
  const next: Record<string, string[]> = {};
  for (const [provider, ids] of Object.entries(agent.integration_bindings ?? {})) {
    const kept = (ids ?? []).filter((id) => id !== integrationId);
    if (kept.length) next[provider] = kept;
  }
  return next;
}

/** Unbinds each orphaned agent the viewer may open from the integration the
 *  change takes from its owner; one that fails keeps its binding (its runs
 *  refuse the tools, and the integration page flags it). */
async function unbindOrphans(orphans: OrphanedAgent[]): Promise<void> {
  const byAgent = new Map<string, Set<string>>();
  for (const o of orphans) {
    if (!o.agentId) continue;
    byAgent.set(o.agentId, (byAgent.get(o.agentId) ?? new Set()).add(o.integrationId));
  }
  await Promise.allSettled(
    [...byAgent].map(async ([id, integrationIds]) => {
      const agent = await api.get<Agent>(`/agents/${id}`);
      let bindings: Pick<Agent, "integration_bindings"> = agent;
      for (const integrationId of integrationIds) {
        bindings = { integration_bindings: bindingsWithout(bindings, integrationId) };
      }
      await api.put(`/agents/${id}`, bindings);
    }),
  );
}

interface CredentialSharingConfirmOptions {
  /** Confirm button label; "Share" by default ("Delete" / "Unshare" by `mode`). */
  confirmLabel?: string;
  /** Confirm button label when the change only takes access away (the dialog
   *  then reads "Stop sharing", its cancel "Keep sharing"); `confirmLabel` otherwise. */
  unshareConfirmLabel?: string;
  /** Cancel button label; "Keep things as they are" by default. */
  cancelLabel?: string;
  /** Something the change does besides lending resources, said in the dialog
   *  even when the preview finds nothing (a connected account removed). */
  note?: string;
  /** What the confirmation is for: its wording and default labels. */
  mode?: "share" | "unshare" | "delete";
  /** A confirmation that always asks (a deletion's own): its title and text,
   *  on screen from the click while the preview adds who loses access. */
  fallback?: { title: string; description?: string };
}

type PreviewRequest = FlatPreviewRequest | GenericPreviewRequest | ResourcePreviewRequest;

/**
 * The one confirmation every bind, share, create, unshare and delete surface
 * uses, for every resource a person lends: integrations, MCP servers,
 * custom tools, channel bots and variables. It asks the read-only preview
 * (`POST /exposure/preview`) and, only when a resource reaches new people or is
 * taken from people who use it, opens ONE dialog whose confirm proceeds. A
 * change made of several saves (sharing several integrations) passes one
 * request per save, and the dialog lists what all of them lend. A preview
 * whose lookups were cut (`truncated`) opens the dialog even when it lists
 * nothing, saying the list may be incomplete. With a
 * `fallback` the dialog opens at once with that text, Confirm waits for the
 * preview's details, and a failed or slow preview (`DELETE_PREVIEW_WAIT_MS`)
 * leaves that text with Confirm enabled. It never blocks: without a fallback, a
 * failed preview proceeds as the change always did. Same shape as
 * `useConfirm()`: render `element`, await `confirm()`; the third entry is true
 * while the preview is being asked.
 */
export function useCredentialSharingConfirm(): [
  React.ReactNode,
  (request: PreviewRequest | PreviewRequest[], opts?: CredentialSharingConfirmOptions) => Promise<boolean>,
  boolean,
] {
  const { mutateAsync, isPending } = useResourceExposurePreview();
  const qc = useQueryClient();
  const [state, setState] = useState<{
    open: boolean;
    checking: boolean;
    items: ResourcePreviewItem[];
    truncated: boolean;
    opts: CredentialSharingConfirmOptions;
  }>({
    open: false,
    checking: false,
    items: [],
    truncated: false,
    opts: {},
  });
  const resolveRef = useRef<((value: boolean) => void) | null>(null);
  // The confirmation the dialog belongs to: a preview answering for an older
  // one (closed, or replaced by a new ask) is dropped.
  const askedRef = useRef(0);

  const confirm = useCallback(
    async (request: PreviewRequest | PreviewRequest[], opts: CredentialSharingConfirmOptions = {}) => {
      const asked = ++askedRef.current;
      const requests = Array.isArray(request) ? request : [request];
      const preview = Promise.allSettled(
        requests.map((r) => mutateAsync(toGenericPreviewRequest(r) as ResourcePreviewRequest)),
      ).then((answers) => {
        const fulfilled = answers.flatMap((a) => (a.status === "fulfilled" ? [a.value] : []));
        if (!fulfilled.length) throw new Error("preview unavailable");
        return {
          items: fulfilled.flatMap((a) => a.items),
          truncated: fulfilled.some((a) => a.truncated),
        };
      });
      const unanswered = { items: [] as ResourcePreviewItem[], truncated: false };
      if (opts.fallback) {
        const answer = new Promise<boolean>((resolve) => {
          resolveRef.current = resolve;
          setState({ open: true, checking: true, items: [], truncated: false, opts });
        });
        void settleWithin(preview, DELETE_PREVIEW_WAIT_MS, unanswered).then(({ items, truncated }) => {
          if (askedRef.current !== asked) return;
          setState((s) => (s.open ? { ...s, checking: false, items, truncated } : s));
        });
        return answer;
      }
      const { items, truncated } = await preview.catch(() => unanswered);
      if (!items.length && !truncated && !opts.note) return true;
      return new Promise<boolean>((resolve) => {
        resolveRef.current = resolve;
        setState({ open: true, checking: false, items, truncated, opts });
      });
    },
    [mutateAsync],
  );

  const resolve = useCallback(
    (value: boolean, unbind?: boolean) => {
      const orphans = value && unbind ? orphanedAgentsOf(state.items) : [];
      const done = resolveRef.current;
      resolveRef.current = null;
      setState((s) => ({ ...s, open: false }));
      if (!orphans.length) {
        done?.(value);
        return;
      }
      // Unbind first, then let the change go ahead: the agents never run
      // with a binding their owner can no longer use.
      void unbindOrphans(orphans).finally(() => {
        void qc.invalidateQueries({ queryKey: ["agents"] });
        void qc.invalidateQueries({ queryKey: ["exposure"] });
        for (const o of orphans) if (o.agentId) void qc.invalidateQueries({ queryKey: ["agent", o.agentId] });
        done?.(value);
      });
    },
    [qc, state.items],
  );

  const element = (
    <CredentialSharingDialog
      open={state.open}
      checking={state.checking}
      items={state.items}
      truncated={state.truncated}
      note={state.opts.note}
      mode={state.opts.mode}
      fallback={state.opts.fallback}
      confirmLabel={state.opts.confirmLabel}
      unshareConfirmLabel={state.opts.unshareConfirmLabel}
      cancelLabel={state.opts.cancelLabel}
      onResolve={resolve}
    />
  );
  return [element, confirm, isPending];
}
