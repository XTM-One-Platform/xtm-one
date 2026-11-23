// SPDX-License-Identifier: Apache-2.0

/**
 * The Signals page's routes (#1263): `/ctem/signals` lists the signals of the
 * loop, `/ctem/signals/sources` the signal sources. The filters live in the
 * URL (`state`, `path`, the table's `s_` keys) and so does the open loop trace (`signal`),
 * so every view is a link.
 */
import { CTEM_SIGNAL_STATES, type CtemSignalState } from "@/hooks/api/useCtemSignalsApi";

export const SIGNALS_PATH = "/ctem/signals";
export const SIGNAL_SOURCES_PATH = "/ctem/signals/sources";

/** The query parameters of the Signals tab. */
/** The URL prefix of the signals table's own state (search, sort, page, its filters `s_f_path` and `s_f_source`). */
export const SIGNALS_TABLE_URL_KEY = "s_";

/** The query parameters of the Signals tab: the trigger path is the table's own filter, read by the server too. */
export const SIGNALS_PARAMS = { state: "state", path: `${SIGNALS_TABLE_URL_KEY}f_path`, signal: "signal" } as const;

/**
 * What "select all" may send beside the state (#1505, review on #1521): the server filters by
 * state and one trigger path only, so a search, a source filter or several paths (the table's
 * client-side filters) answer null and no "select all" is offered, else the bulk action would
 * reach signals the table hides.
 */
export function selectAllScope(params: URLSearchParams): { trigger_path?: string } | null {
  const key = (name: string) => `${SIGNALS_TABLE_URL_KEY}${name}`;
  if ((params.get(key("q")) ?? "").trim()) return null;
  if (params.getAll(key("f_source")).some(Boolean)) return null;
  const paths = params.getAll(key("f_path")).filter(Boolean);
  if (paths.length > 1) return null;
  return paths.length === 1 ? { trigger_path: paths[0] } : {};
}

export interface SignalsView {
  state?: CtemSignalState | null;
  path?: string | null;
  /** The signal whose loop trace is open. */
  signal?: string | null;
}

/** The link to the Signals tab on these filters. */
export function signalsHref(view: SignalsView = {}): string {
  const params = new URLSearchParams();
  if (view.state) params.set(SIGNALS_PARAMS.state, view.state);
  if (view.path) params.set(SIGNALS_PARAMS.path, view.path);
  if (view.signal) params.set(SIGNALS_PARAMS.signal, view.signal);
  const query = params.toString();
  return query ? `${SIGNALS_PATH}?${query}` : SIGNALS_PATH;
}

/** The view a Signals tab URL names; an unknown state reads as every state. */
export function readSignalsView(params: URLSearchParams): Required<SignalsView> {
  const rawState = params.get(SIGNALS_PARAMS.state);
  const state = CTEM_SIGNAL_STATES.find((s) => s === rawState) ?? null;
  return {
    state,
    path: params.get(SIGNALS_PARAMS.path) || null,
    signal: params.get(SIGNALS_PARAMS.signal) || null,
  };
}

