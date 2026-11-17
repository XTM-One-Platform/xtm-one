/**
 * Lab-only page (#11319): the served `@filigran/chatbot` panel in the display
 * the URL names, through the host XTM One mounts it with (`XtmChatPanel`: its
 * theme, endpoints, translations and session). XTM One itself renders the
 * panel inline only, so a chatbot change to the floating window, the docked
 * sidebar or the full screen is reviewed here, with no page-side script.
 *
 * `lab.py` copies this file to `frontend/src/lab-chatbot-host.tsx` and its page
 * to `frontend/lab-chatbot.html` in the lab's copy of the checkout, when
 * `LAB_CHATBOT` is set; the repository's `frontend/` never holds them, so the
 * product build never ships them. dev-docs/reference/agentic-labs.md,
 * "A @filigran/chatbot branch in the lab".
 *
 * `?mode=floating|sidebar|fullscreen|inline` (floating by default): the mode
 * lives in the URL, so a reload keeps it, and the panel's view switcher writes
 * it back. The session is the app's (`copilot_token`): the lab logs this page's
 * URL with `#token=...`, which the page's first script stores then scrubs.
 *
 * `?agent=<slug or id>` picks the agent the panel opens on; without it, the
 * agent `seed_chat_suggestions` seeds with three suggested actions, so the
 * welcome screen shows its chips with no fetch override.
 *
 * Review states (#11360), beside the mode: `historyPending=1`,
 * `welcome=placeholder`, `conversation=ID` and `holdHistory=1`
 * (`lab-chatbot-params.ts`); the mode links keep them. `waiting=package` (or
 * `waiting=package-nourl`, no game link) shows the package's own waiting panel,
 * the one the other products render, instead of XTM One's arcade. `quota=1`
 * keeps the panel's quota route (XTM One's own, `CHAT_PANEL_ENDPOINTS.quota`)
 * explicit; `quotaState=ok|warning|danger|unlimited|global|legacy`
 * answers its read with a fixed payload (`quotaPayload`).
 */
import React, { useCallback, useState, useSyncExternalStore } from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import type { ChatPanelProps } from "@filigran/chatbot";
import { XtmChatPanel } from "@/components/chat-panel/XtmChatPanel";
import { ToastProvider } from "@/components/ui/toast";
import { PlatformProvider } from "@/hooks/usePlatformConfig";
import { useThemeEffect } from "@/hooks/useThemeEffect";
import { AppIntlProvider } from "@/i18n";
import { CHAT_PANEL_ENDPOINTS } from "@/components/chat-panel/endpoints";
import { isHistoryRead, isQuotaRead, quotaPayload, reviewStateFromSearch, searchWith } from "./lab-chatbot-params";
import "./fonts";
import "./index.css";

type Display = ChatPanelProps["mode"];

const DISPLAYS: readonly Display[] = ["floating", "sidebar", "fullscreen", "inline"];

/** The display the URL names; floating when it names none or another. */
export function displayFromSearch(search: string): Display {
  const asked = new URLSearchParams(search).get("mode");
  return DISPLAYS.find((d) => d === asked) ?? "floating";
}

/** The lab agent with suggested actions (`LAB_SUGGESTIONS_AGENT_SLUG` in lab.py). */
const SUGGESTIONS_AGENT = "lab-chat-suggestions";

/** The agent the URL names; the lab agent with suggested actions when it names none. */
export function agentFromSearch(search: string): string {
  return new URLSearchParams(search).get("agent") || SUGGESTIONS_AGENT;
}

const REVIEW = reviewStateFromSearch(window.location.search);

/*
 * `holdHistory=1`: the panel's read-back of a conversation waits until the
 * page's release control, so its restoring state stays on screen. Installed
 * before the first render, so the panel's first read is held too; the panel
 * looks `fetch` up per call.
 */
const heldReads: Array<() => void> = [];
const heldListeners = new Set<() => void>();
const notifyHeld = () => heldListeners.forEach((listener) => listener());
if (REVIEW.holdHistory) {
  const sessionsPath = CHAT_PANEL_ENDPOINTS.sessions ?? "/web/chat/sessions";
  const passThrough = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!isHistoryRead(url, init, sessionsPath)) return passThrough(input, init);
    return new Promise<void>((release) => {
      heldReads.push(release);
      notifyHeld();
    }).then(() => passThrough(input, init));
  };
}
/*
 * `quota=1`: the panel reads the web chat's quota route, the one
 * `CHAT_PANEL_ENDPOINTS.quota` names too. `quotaState=`: that read is answered with the fixed
 * payload of the state, installed before the first render like the hold.
 */
const QUOTA_PATH = "/web/chat/quota";
const LAB_ENDPOINTS = REVIEW.quota ? { ...CHAT_PANEL_ENDPOINTS, quota: QUOTA_PATH } : undefined;
if (REVIEW.quotaState) {
  const state = REVIEW.quotaState;
  const passThrough = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!isQuotaRead(url, init, QUOTA_PATH)) return passThrough(input, init);
    return Promise.resolve(
      new Response(JSON.stringify(quotaPayload(state)), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  };
}

const subscribeHeld = (listener: () => void) => {
  heldListeners.add(listener);
  return () => heldListeners.delete(listener);
};
const heldCount = () => heldReads.length;
const releaseHeld = () => {
  heldReads.splice(0).forEach((release) => release());
  notifyHeld();
};

function LabChatbotHost() {
  // The signed-in user's colour mode, as the app's layout applies it.
  useThemeEffect();
  const [display, setDisplay] = useState<Display>(() => displayFromSearch(window.location.search));
  const [open, setOpen] = useState(true);
  const [initialAgent] = useState(() => agentFromSearch(window.location.search));
  const [conversationId, setConversationId] = useState<string | null>(REVIEW.conversation);
  const held = useSyncExternalStore(subscribeHeld, heldCount);
  const onModeChange = useCallback((next: Display) => {
    window.history.replaceState(null, "", searchWith(window.location.search, "mode", next) || window.location.pathname);
    setDisplay(next);
  }, []);
  const onConversationChange = useCallback((next: string | null) => {
    window.history.replaceState(null, "", searchWith(window.location.search, "conversation", next) || window.location.pathname);
    setConversationId(next);
  }, []);
  const onClose = useCallback(() => setOpen(false), []);

  return (
    <div
      className="h-screen w-screen flex flex-col"
      data-testid="lab-chatbot-host"
      data-lab-display={display}
      data-lab-history-held={held}
      data-lab-quota={REVIEW.quotaState ?? (REVIEW.quota ? "live" : "off")}
    >
      <p className="p-4 text-sm">
        Lab host of @filigran/chatbot, display {display}.{" "}
        {DISPLAYS.filter((d) => d !== display).map((d) => (
          <a key={d} className="underline mr-3" href={searchWith(window.location.search, "mode", d) || "?"}>
            {d}
          </a>
        ))}
        {REVIEW.holdHistory && (
          <button type="button" className="underline mr-3" onClick={releaseHeld} disabled={held === 0}>
            release the history read ({held} held)
          </button>
        )}
        {!open && (
          <button type="button" className="underline" onClick={() => setOpen(true)}>
            open the panel
          </button>
        )}
      </p>
      {open && (
        <div className={display === "inline" ? "flex-1 min-h-0 flex" : undefined}>
          <XtmChatPanel
            mode={display}
            initialAgent={initialAgent}
            onModeChange={display === "inline" ? undefined : onModeChange}
            onClose={display === "inline" ? undefined : onClose}
            // The quota route the other products pass, so the package's quota indicator renders.
            {...(LAB_ENDPOINTS ? { apiEndpoints: LAB_ENDPOINTS } : {})}
            // Held by the page only when the URL names one: otherwise the panel keeps its own, as before.
            {...(REVIEW.conversation ? { conversationId, onConversationChange } : {})}
            // A prop of the chatbot under review (filigran-design-system #333), absent from the pinned version's types.
            {...(REVIEW.historyPending ? ({ historyPending: true } as unknown as Partial<ChatPanelProps>) : {})}
            // The package's own waiting panel, as OpenCTI, OpenAEV and OpenCRQ get it: XtmChatPanel's
            // arcade slots are overridden with undefined (its props spread after them).
            {...(REVIEW.waiting
              ? {
                  renderWaitingPanel: undefined,
                  renderReasoningAction: undefined,
                  agentDashboardUrl: REVIEW.waiting === "package" ? window.location.origin : undefined,
                }
              : {})}
            welcome={
              REVIEW.welcome === "placeholder" ? (
                <div data-testid="lab-welcome-placeholder" className="m-auto rounded border border-dashed p-6 text-sm">
                  Host welcome placeholder (lab)
                </div>
              ) : undefined
            }
          />
        </div>
      )}
    </div>
  );
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <PlatformProvider>
          <AppIntlProvider>
            <ToastProvider>
              <LabChatbotHost />
            </ToastProvider>
          </AppIntlProvider>
        </PlatformProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
