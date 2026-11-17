/**
 * The review states `/lab-chatbot.html` reads from its URL (#11360), beside
 * `?mode=`: a chatbot change that adds a `ChatPanel` prop or a host slot state
 * is rendered from a URL, with no page-side script. Lab only: `lab.py` copies
 * this file next to `lab-chatbot-host.tsx` in the lab's copy of `frontend/`.
 *
 * - `historyPending=1`: the panel's `historyPending` prop (the host is still
 *   resolving which conversation to show).
 * - `welcome=placeholder`: a visible host placeholder in the `welcome` slot.
 * - `conversation=ID`: the conversation the panel holds (`conversationId`).
 * - `holdHistory=1`: the conversation read-back is held until the page's
 *   "release the history read" control, so the restoring state stays on screen.
 * - `waiting=package`: the package's own waiting panel (`ChatWaitingPanel`:
 *   rotating messages and the game link), as OpenCTI, OpenAEV and OpenCRQ
 *   render it: no host `renderWaitingPanel` or `renderReasoningAction`, and
 *   `agentDashboardUrl` set to the lab's origin. `waiting=package-nourl` leaves
 *   `agentDashboardUrl` unset, so the messages show with no link.
 * - `quota=1`: the package's own quota indicator (`QuotaIndicator`), as
 *   every product renders it, XTM One included since it serves
 *   `CHAT_PANEL_ENDPOINTS.quota`: the panel reads the lab's real
 *   `GET /web/chat/quota`.
 * - `quotaState=ok|warning|danger|unlimited|global|legacy` (implies
 *   `quota=1`): the page answers that read with a fixed payload in the shape
 *   of the real route (`quotaPayload`), so each state of the indicator is
 *   reviewed with no page-side script; `legacy` is an older backend's answer
 *   with no `periods`.
 */

/** The waiting panel the page asks for: the package's own, with or without the game link. */
export type LabWaitingPanel = "package" | "package-nourl";

/** The fixed answers of the quota read the page can give (`quotaState=`). */
export const LAB_QUOTA_STATES = ["ok", "warning", "danger", "unlimited", "global", "legacy"] as const;
export type LabQuotaState = (typeof LAB_QUOTA_STATES)[number];

export interface LabReviewState {
  historyPending: boolean;
  welcome: "placeholder" | null;
  conversation: string | null;
  holdHistory: boolean;
  waiting: LabWaitingPanel | null;
  /** The panel gets the quota route, so the package's quota indicator renders. */
  quota: boolean;
  /** The fixed answer of the quota read; null = the lab's real route answers. */
  quotaState: LabQuotaState | null;
}

/** Every query parameter the page reads, in the order the lab logs them. */
export const LAB_REVIEW_PARAMS = ["historyPending=1", "welcome=placeholder", "conversation=ID", "holdHistory=1", "waiting=package", "waiting=package-nourl", "quota=1", "quotaState=ok|warning|danger|unlimited|global|legacy"] as const;

const isOn = (value: string | null) => value === "1" || value === "true";

const isQuotaState = (value: string | null): value is LabQuotaState =>
  (LAB_QUOTA_STATES as readonly string[]).includes(value ?? "");

/** The review state the URL names; every parameter off when absent or unknown. */
export function reviewStateFromSearch(search: string): LabReviewState {
  const params = new URLSearchParams(search);
  const conversation = params.get("conversation")?.trim() || null;
  const waiting = params.get("waiting");
  const quotaState = params.get("quotaState");
  return {
    historyPending: isOn(params.get("historyPending")),
    welcome: params.get("welcome") === "placeholder" ? "placeholder" : null,
    conversation,
    holdHistory: isOn(params.get("holdHistory")),
    waiting: waiting === "package" || waiting === "package-nourl" ? waiting : null,
    quota: isOn(params.get("quota")) || isQuotaState(quotaState),
    quotaState: isQuotaState(quotaState) ? quotaState : null,
  };
}

/** One row of the quota read (`QuotaPeriodUsage` of the backend). */
export interface LabQuotaPeriod {
  period: "daily" | "monthly" | "yearly";
  used: number;
  limit: number | null;
  scope: "user" | "global";
  resets_at: string;
}

/** The answer of `GET /web/chat/quota` (`PlatformChatQuotaOut`); `periods` absent on an older backend. */
export interface LabQuotaPayload {
  used: number;
  limit: number | null;
  period: string;
  scope: "user" | "global";
  periods?: LabQuotaPeriod[];
}

const iso = (date: Date) => date.toISOString().replace(/\.\d{3}Z$/, "Z");

/** The UTC instants today's, this month's and this year's counters start again, from *now*. */
function resets(now: Date): Record<LabQuotaPeriod["period"], string> {
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth();
  return {
    daily: iso(new Date(Date.UTC(y, m, now.getUTCDate() + 1))),
    monthly: iso(new Date(Date.UTC(y, m + 1, 1))),
    yearly: iso(new Date(Date.UTC(y + 1, 0, 1))),
  };
}

/**
 * The fixed quota read of *state*, in the shape of the real route: today's
 * row first (the button reads "N agentic actions today"), then this month and
 * this year. `warning` spends 85% of today's limit, `danger` all of it,
 * `unlimited` has no limit, `global` counts the shared platform pool, and
 * `legacy` is an older backend's answer (no `periods`), which the package
 * shows as an inert readout.
 */
export function quotaPayload(state: LabQuotaState, now: Date = new Date()): LabQuotaPayload {
  const at = resets(now);
  const scope = state === "global" ? "global" : "user";
  const [dailyUsed, dailyLimit]: [number, number | null] =
    state === "warning" ? [85, 100] : state === "danger" ? [100, 100] : state === "unlimited" ? [12, null] : state === "global" ? [640, 1000] : [12, 100];
  const scale = (n: number | null, by: number) => (n === null ? null : n * by);
  const periods: LabQuotaPeriod[] = [
    { period: "daily", used: dailyUsed, limit: dailyLimit, scope, resets_at: at.daily },
    { period: "monthly", used: dailyUsed * 10, limit: scale(dailyLimit, 30), scope, resets_at: at.monthly },
    { period: "yearly", used: dailyUsed * 40, limit: scale(dailyLimit, 365), scope, resets_at: at.yearly },
  ];
  const head = { used: dailyUsed, limit: dailyLimit, period: "daily", scope } as const;
  return state === "legacy" ? { ...head } : { ...head, periods };
}

/** Whether a request is the panel's quota read: a `GET` to *quotaPath* (query ignored). */
export function isQuotaRead(url: string, init: { method?: string } | undefined, quotaPath: string): boolean {
  if ((init?.method ?? "GET").toUpperCase() !== "GET") return false;
  const path = url.split("?")[0].split("#")[0];
  return path.endsWith(quotaPath.split("?")[0]);
}

/** *search* with `key` set to *value* (removed when null), every other parameter kept. */
export function searchWith(search: string, key: string, value: string | null): string {
  const params = new URLSearchParams(search);
  if (value === null) params.delete(key);
  else params.set(key, value);
  const text = params.toString();
  return text ? `?${text}` : "";
}

/**
 * Whether a request is the panel's read-back of a conversation: a `POST` to the
 * sessions route (*sessionsPath*, query ignored) whose JSON body names a
 * `conversation_id`. A new conversation's `POST` (no id) is never held.
 */
export function isHistoryRead(url: string, init: { method?: string; body?: unknown } | undefined, sessionsPath: string): boolean {
  if ((init?.method ?? "GET").toUpperCase() !== "POST" || typeof init?.body !== "string") return false;
  const path = url.split("?")[0].split("#")[0];
  if (!path.endsWith(sessionsPath.split("?")[0])) return false;
  try {
    const body = JSON.parse(init.body) as { conversation_id?: unknown };
    return typeof body.conversation_id === "string" && body.conversation_id !== "";
  } catch {
    return false;
  }
}
