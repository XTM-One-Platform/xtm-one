import { describe, expect, it } from "vitest";

import {
  CLICKER_ACTIVE_POLL_MS,
  CLICKER_IDLE_POLL_MS,
  clickerPollMs,
  handClickerRefusal,
  parseClickerCommand,
  type PresentationClicker,
} from "./usePresentationClicker";

const DECK = "4f6c1d2e-0000-4000-8000-000000000001";

const state = (over: Partial<PresentationClicker> = {}): PresentationClicker => ({
  presenter_id: null,
  holder: null,
  handed_at: null,
  is_presenter: false,
  is_holder: false,
  slide_index: null,
  ttl_seconds: 45,
  ...over,
});

describe("parseClickerCommand", () => {
  const frame = (over: Record<string, unknown> = {}) =>
    JSON.stringify({ type: "presentation_clicker_command", presentation_id: DECK, command: "next", index: null, ...over });

  it("reads a command for the deck", () => {
    expect(parseClickerCommand(frame(), DECK)?.command).toBe("next");
    expect(parseClickerCommand(frame({ command: "goto", index: 3 }), DECK)?.index).toBe(3);
  });

  it("ignores another deck, another frame type, a malformed frame and goto without an index", () => {
    expect(parseClickerCommand(frame({ presentation_id: "other" }), DECK)).toBeNull();
    expect(parseClickerCommand(frame({ type: "flow_run_update" }), DECK)).toBeNull();
    expect(parseClickerCommand(frame({ command: "rm -rf" }), DECK)).toBeNull();
    expect(parseClickerCommand(frame({ command: "goto", index: null }), DECK)).toBeNull();
    expect(parseClickerCommand("not json", DECK)).toBeNull();
  });
});

describe("handClickerRefusal", () => {
  const failure = (status?: number) => Object.assign(new Error("refused"), status ? { status } : {});

  it("words a 404 as a person who cannot open the deck and a 503 as unavailable", () => {
    expect(handClickerRefusal(failure(404))).toBe("cannot_open");
    expect(handClickerRefusal(failure(503))).toBe("unavailable");
  });

  it("falls back to the generic wording for anything else", () => {
    expect(handClickerRefusal(failure(400))).toBe("other");
    expect(handClickerRefusal(failure())).toBe("other");
    expect(handClickerRefusal(null)).toBe("other");
  });
});

describe("clickerPollMs", () => {
  const holder = { user_id: "u2", name: "Co Presenter" };

  it("polls fast for the holder, so the clicker stays inside the server TTL", () => {
    expect(clickerPollMs(state({ holder, is_holder: true }))).toBe(CLICKER_ACTIVE_POLL_MS);
    expect(CLICKER_ACTIVE_POLL_MS * 3).toBeLessThan(45_000);
  });

  it("polls fast for the presenter while handed, slow otherwise", () => {
    expect(clickerPollMs(state({ holder, is_presenter: true }))).toBe(CLICKER_ACTIVE_POLL_MS);
    expect(clickerPollMs(state())).toBe(CLICKER_IDLE_POLL_MS);
    expect(clickerPollMs(undefined)).toBe(CLICKER_IDLE_POLL_MS);
    expect(clickerPollMs(state({ holder }))).toBe(CLICKER_IDLE_POLL_MS);
  });
});
