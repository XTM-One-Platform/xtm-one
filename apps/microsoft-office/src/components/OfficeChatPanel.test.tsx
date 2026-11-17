/**
 * The task pane in Word, end to end short of Office and the server: a quick
 * action on the selection, its answer streamed over SSE, and Replace writing
 * the agent's text over the selection. Office.js (`Office`, `Word.run`) and
 * `fetch` are faked; everything between them is the pane's own code and the
 * real `@filigran/chatbot` panel.
 */
import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";

const BASE = "https://xtm.test";
const API = `${BASE}/api/v1`;

const SELECTED = "The results was more better than expected.";
const IMPROVED = "The results exceeded expectations.";
const ANSWER = `Here is a clearer version.\n\n\`\`\`action\n${JSON.stringify({ action: "replace_selection", content: IMPROVED })}\n\`\`\``;

/* ---------- Word: a two-paragraph document, the second one selected ---------- */

interface FakeParagraph {
  text: string;
  style: string;
}

function fakeWord() {
  const paragraphs: FakeParagraph[] = [
    { text: "Quarterly report", style: "Heading 1" },
    { text: SELECTED, style: "Normal" },
  ];
  const selected = paragraphs[1];
  const writes: { text: string; location: string }[] = [];
  // Set: every read Office is asked for fails with it (protected view, a lock).
  const state: { failure: Error | null } = { failure: null };
  const loadable = <T extends object>(value: T) => Object.assign(value, { load: () => value });

  const run = async <T,>(batch: (ctx: unknown) => Promise<T>): Promise<T> => {
    if (state.failure) throw state.failure;
    const selection = loadable({
      get text() {
        return selected.text;
      },
      paragraphs: loadable({ items: [selected] }),
      insertText(text: string, location: string) {
        writes.push({ text, location });
        if (location === "Replace") selected.text = text;
      },
    });
    const body = loadable({
      get text() {
        return paragraphs.map((p) => p.text).join("\r");
      },
      paragraphs: loadable({ items: paragraphs }),
      tables: loadable({ items: [] }),
    });
    const ctx = { document: { getSelection: () => selection, body }, sync: async () => undefined };
    return batch(ctx);
  };
  return { run, writes, paragraphs, state };
}

/* ---------- The server: the routes the pane and the panel ask ---------- */

interface Recorded {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

function sseStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: () => new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    send: (frame: Record<string, unknown>) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)),
    close: () => {
      try {
        controller.close();
      } catch {
        // Already closed or cancelled.
      }
    },
  };
}

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

/** `/auth/me` as the backend answers it: the display name is `full_name`. */
const ME = { id: "u1", email: "ada@xtm.test", full_name: "Ada Lovelace", role: "user" };

function fakeServer(answer: ReturnType<typeof sseStream>, me: Record<string, unknown> = ME, agentIcon: string | null = null) {
  const requests: Recorded[] = [];
  // What the server stores of the conversation, for a read-back.
  const stored: { id: string; role: string; content: string }[] = [];
  const fetchFake = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init?.method ?? "GET";
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => {
      headers[key] = value;
    });
    let body: unknown = init?.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        // Kept as sent.
      }
    }
    requests.push({ method, url, headers, body });
    if (url === `${API}/auth/me`) return json(me);
    if (url === `${API}/platform/config`)
      return json({ platform_mode: "xtm_one", platform_name: "XTM One", platform_logo_url: null, platform_version: "test" });
    if (url === `${API}/web/chat/agents`) return json([{ id: "a1", name: "Writer", slug: "writer", icon: agentIcon, description: null }]);
    if (url === `${API}/web/chat/messages` && method === "POST") {
      stored.push({ id: "m1", role: "user", content: (body as { content: string }).content });
      return answer.response();
    }
    if (url === `${API}/web/chat/sessions` && method === "POST") return json({ conversation_id: "c1", messages: stored });
    return new Response("not found", { status: 404 });
  };
  return { requests, stored, fetchFake };
}

/* ---------- Rendering ---------- */

let root: Root;
let container: HTMLElement;
// The running test's signal: Vitest aborts it when the test times out. A wait
// that outlives its test would keep calling act() on the next test's page
// ("overlapping act() calls") and fail every test after it.
let testSignal: AbortSignal;

function stopWhenEnded(signal: AbortSignal) {
  if (signal.aborted) throw new Error("The test ended while the page was still waited for");
}

async function settle(rounds = 5, signal = testSignal) {
  for (let i = 0; i < rounds; i += 1) {
    stopWhenEnded(signal);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

/** Waits for *find* to return something, letting the page update between tries. */
async function until<T>(find: () => T | null | undefined, what: string): Promise<T> {
  const signal = testSignal;
  for (let i = 0; i < 100; i += 1) {
    stopWhenEnded(signal);
    const found = find();
    if (found) return found;
    await settle(1, signal);
  }
  throw new Error(`Never appeared: ${what}`);
}

const buttons = () => [...container.querySelectorAll("button")];
const buttonNamed = (name: string) =>
  buttons().find((b) => b.getAttribute("aria-label") === name || b.textContent?.trim() === name);
const buttonStartingWith = (text: string) => buttons().find((b) => b.textContent?.trim().startsWith(text));
/** A quick action of the composer's menu (the empty state's suggestions carry some of the same words). */
const quickAction = (text: string) =>
  [...container.querySelectorAll<HTMLButtonElement>(".action-dropdown-item")].find((b) => b.textContent?.trim().startsWith(text));
const byTestId = (id: string) => [...container.querySelectorAll<HTMLElement>(`[data-testid="${id}"]`)];

async function click(element: HTMLElement) {
  await act(async () => {
    element.click();
  });
  await settle();
}

// jsdom lays nothing out: the transcript's scrolling is a no-op.
Element.prototype.scrollTo ??= () => undefined;
Element.prototype.scrollIntoView ??= () => undefined;

let word: ReturnType<typeof fakeWord>;

beforeEach(({ signal }) => {
  testSignal = signal;
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  localStorage.setItem("filigran_copilot_url", BASE);
  localStorage.setItem("filigran_copilot_token", "test-token");
  word = fakeWord();
  const office = {
    onReady: (callback: () => void) => {
      callback();
      return Promise.resolve();
    },
    HostType: { Word: "Word", Excel: "Excel", PowerPoint: "PowerPoint", Outlook: "Outlook" },
    EventType: { DocumentSelectionChanged: "documentSelectionChanged" },
    context: { host: "Word", document: { addHandlerAsync: vi.fn() } },
  };
  vi.stubGlobal("Office", office);
  vi.stubGlobal("Word", { run: word.run });
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({ matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined }) as unknown as MediaQueryList,
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

test("Improve on a Word selection: the context goes, the request shows, Replace writes the agent's text over the selection", async () => {
  const answer = sseStream();
  const server = fakeServer(answer);
  vi.stubGlobal("fetch", server.fetchFake);

  await act(async () => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
  await until(() => buttonNamed("Quick actions"), "the quick actions toggle");
  // The selection is read through Word and shown above the composer.
  await until(() => container.querySelector(".selection-pill-text")?.textContent?.includes(SELECTED), "the selection pill");

  await click(buttonNamed("Quick actions")!);
  await click(await until(() => quickAction("Improve"), "the Improve action"));

  // The send carries the document's context and the quick action's prompt.
  const send = await until(
    () => server.requests.find((r) => r.method === "POST" && r.url === `${API}/web/chat/messages`),
    "the send",
  );
  expect(send.headers.authorization).toBe("Bearer test-token");
  const sent = (send.body as { content: string }).content;
  expect(sent.startsWith("[Office Add-in]")).toBe(true);
  expect(sent).toContain(`Selected content (Paragraph 1):\n\`\`\`\n${SELECTED}\n\`\`\``);
  expect(sent).toContain("Surrounding context:");
  expect(sent).toContain("Headings: Quarterly report");
  expect(sent).toContain("Requested action: Improve the writing quality of the selected content.");
  expect(sent.endsWith("\nUser request: Enhance the writing quality.")).toBe(true);

  // The user's bubble shows the action's label, not what was sent.
  const bubble = await until(() => byTestId("filigran-chat-user-message")[0], "the user's bubble");
  expect(bubble.textContent?.trim()).toBe("Improve the writing quality");
  const transcript = () => byTestId("filigran-chat-transcript")[0]?.textContent ?? "";
  expect(transcript()).not.toContain("[Office Add-in]");
  expect(transcript()).not.toContain(SELECTED);

  // The answer streams, then ends with the action block.
  await act(async () => {
    answer.send({ type: "stream", content: "Here is a clearer version." });
    answer.send({ type: "done", content: ANSWER, conversation_id: "c1", message_id: "m2" });
    answer.close();
  });
  server.stored.push({ id: "m2", role: "assistant", content: ANSWER });
  await until(() => buttonNamed("Replace"), "the Replace button under the answer");
  const [reply] = byTestId("filigran-chat-assistant-message");
  expect(reply.textContent).toContain("Here is a clearer version.");
  expect(reply.textContent).not.toContain("```action");
  expect(reply.textContent).not.toContain("replace_selection");
  expect(byTestId("filigran-chat-user-message")[0].textContent?.trim()).toBe("Improve the writing quality");

  // Replace previews the swap; nothing is written before Apply.
  await click(buttonNamed("Replace")!);
  const dialog = await until(() => container.querySelector<HTMLElement>('[role="dialog"]'), "the apply preview");
  expect(dialog.querySelector(".apply-preview-original")?.textContent).toContain(SELECTED);
  expect(dialog.querySelector(".apply-preview-proposed")?.textContent).toContain(IMPROVED);
  expect(word.writes).toEqual([]);

  await click(buttonStartingWith("Apply")!);
  expect(word.writes).toEqual([{ text: IMPROVED, location: "Replace" }]);
  expect(word.paragraphs.map((p) => p.text)).toEqual(["Quarterly report", IMPROVED]);
  await until(() => container.textContent?.includes("Applied to document"), "the applied notice");
  expect(container.querySelector('[role="dialog"]')).toBeNull();

  // The pane reopened reads the conversation back: the stored question is the
  // built prompt, and the bubble still shows the action's label.
  await act(async () => root.unmount());
  root = createRoot(container);
  await act(async () => {
    root.render(<App />);
  });
  await until(() => byTestId("filigran-chat-assistant-message").length, "the conversation read back");
  expect(server.stored[0].content).toBe(sent);
  expect(byTestId("filigran-chat-user-message")[0].textContent?.trim()).toBe("Improve the writing quality");
  expect(transcript()).not.toContain("[Office Add-in]");
  expect(byTestId("filigran-chat-assistant-message")[0].textContent).not.toContain("```action");
  // The whole flow under StrictMode, then a remount: about 0.5 s on a free
  // machine, 8 s seen on a busy CI runner (main 60cb92ee). Every wait is bounded
  // by the rounds of `until`, so the clock only measures the runner.
}, 30_000);

test("A new conversation shows the panel's empty state: the agent named, the pane's prompts as chips that fill the composer", async () => {
  const server = fakeServer(sseStream());
  vi.stubGlobal("fetch", server.fetchFake);

  await act(async () => {
    root.render(<App />);
  });
  const heading = await until(() => container.querySelector("h2"), "the greeting");
  await until(() => heading.textContent?.includes("Writer"), "the agent in the greeting");
  expect(heading.textContent).toBe("How can Writer help you, Ada?");
  // No welcome of the pane's own any more: no sparkles mark, no pills.
  expect(container.querySelector(".empty-state-icon")).toBeNull();
  expect(container.querySelector(".suggestion-chip")).toBeNull();

  const group = await until(() => container.querySelector<HTMLElement>('[role="group"][aria-label="Suggestions"]'), "the suggestions");
  const chips = [...group.querySelectorAll<HTMLElement>("button")];
  expect(chips.map((c) => c.textContent?.trim())).toEqual([
    "Interpret this document",
    "Summarize this document",
    "Translate the selected text",
    "Rewrite the selected content",
  ]);

  // A chip puts its words in the composer and sends nothing by itself.
  await click(chips[1]);
  expect(container.querySelector<HTMLTextAreaElement>("textarea")?.value).toBe("Summarize this document");
  expect(server.requests.some((r) => r.method === "POST" && r.url === `${API}/web/chat/messages`)).toBe(false);
});

test("The welcome mark is the agent's own icon, as on the web chat", async () => {
  const server = fakeServer(sseStream(), ME, "Bug");
  vi.stubGlobal("fetch", server.fetchFake);

  await act(async () => {
    root.render(<App />);
  });
  const heading = await until(() => container.querySelector("h2"), "the greeting");
  await until(() => heading.textContent?.includes("Writer"), "the agent in the greeting");
  const mark = await until(() => container.querySelector<HTMLElement>("[data-chat-welcome-mark]"), "the welcome mark");
  await until(() => mark.querySelector('[data-agent-icon="Bug"]'), "the agent's Bug icon in the welcome mark");
});

test("The switcher draws the agent's icon and opens the agent's page on the platform in a new window", async () => {
  const server = fakeServer(sseStream(), ME, "Bug");
  vi.stubGlobal("fetch", server.fetchFake);
  const open = vi.fn();
  vi.stubGlobal("open", open);

  await act(async () => {
    root.render(<App />);
  });
  const heading = await until(() => container.querySelector("h2"), "the greeting");
  await until(() => heading.textContent?.includes("Writer"), "the agent in the greeting");
  const trigger = await until(
    () => container.querySelector<HTMLElement>('[data-testid="filigran-chat-agent-switcher"]'),
    "the agent switcher",
  );
  // The header trigger draws the agent's own icon (renderAgentIcon), not the panel's sparkle.
  await until(() => trigger.querySelector('[data-agent-icon="Bug"]'), "the agent's Bug icon on the switcher");

  await click(trigger);
  const link = await until(
    () => document.querySelector<HTMLAnchorElement>('[data-testid="filigran-chat-agent-page-link"]'),
    "the Open agent page item",
  );
  expect(link.getAttribute("href")).toBe(`${BASE}/agents/a1`);
  await click(link);
  expect(open).toHaveBeenCalledWith(`${BASE}/agents/a1`, "_blank", "noopener,noreferrer");
});

test("The greeting never ends on an empty name: an account without a full name is addressed by its email's local part", async () => {
  const server = fakeServer(sseStream(), { id: "u2", email: "admin@xtm.test", full_name: null, role: "admin" });
  vi.stubGlobal("fetch", server.fetchFake);

  await act(async () => {
    root.render(<App />);
  });
  const heading = await until(() => container.querySelector("h2"), "the greeting");
  await until(() => heading.textContent?.includes("Writer"), "the agent in the greeting");
  expect(heading.textContent).toBe("How can Writer help you, admin?");
});

test("The panel takes the pane's body font, as the web chat does", async () => {
  const server = fakeServer(sseStream());
  vi.stubGlobal("fetch", server.fetchFake);
  document.body.style.fontFamily = '"IBM Plex Sans", sans-serif';

  try {
    await act(async () => {
      root.render(<App />);
    });
    await until(() => container.querySelector("h2"), "the greeting");
    const styled = [...container.querySelectorAll<HTMLElement>("[style]")].some((el) => el.style.fontFamily.includes("IBM Plex Sans"));
    expect(styled).toBe(true);
  } finally {
    document.body.style.fontFamily = "";
  }
});

// The whole document read for Summarize, its sections for Review Doc.
test.each(["Summarize", "Review Doc"])("%s, when Word cannot read the document, says so above the composer and sends nothing", async (action) => {
  const server = fakeServer(sseStream());
  vi.stubGlobal("fetch", server.fetchFake);
  const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);

  await act(async () => {
    root.render(<App />);
  });
  await until(() => container.querySelector(".selection-pill-text")?.textContent?.includes(SELECTED), "the selection pill");
  word.state.failure = new Error("The document is in protected view.");

  await click(buttonNamed("Quick actions")!);
  await click(await until(() => quickAction(action), `the ${action} action`));

  const notice = await until(() => container.querySelector<HTMLElement>('.pane-notice[role="alert"]'), "the notice");
  expect(notice.textContent).toContain("Could not read the document: The document is in protected view.");
  expect(server.requests.some((r) => r.method === "POST" && r.url === `${API}/web/chat/messages`)).toBe(false);
  expect(logged).toHaveBeenCalledWith("Quick action could not read the document:", word.state.failure);

  // Dismissed, it goes.
  await click(buttonNamed("Dismiss")!);
  expect(container.querySelector(".pane-notice")).toBeNull();
  logged.mockRestore();
});
