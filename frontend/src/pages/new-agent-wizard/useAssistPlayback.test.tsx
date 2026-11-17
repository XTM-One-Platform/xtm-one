// How the wizard assistant's answer is played onto the draft (AGENT_WIZARD_ASSIST):
// step by step, onto the draft as it is when each operation plays, stopped at once.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { freshDraft, planAssist } from "./assist-plan";
import type { WizardDraft } from "./draft-storage";
import { useAssistPlayback } from "./useAssistPlayback";

let reducedMotion = false;
vi.mock("@/hooks/usePrefersReducedMotion", () => ({ usePrefersReducedMotion: () => reducedMotion }));

/** The wizard's state as the hook sees it, with what it opened and set. */
function wizard(initial: WizardDraft) {
  const state = { draft: initial, steps: [] as string[], writes: 0 };
  const hook = renderHook(() =>
    useAssistPlayback({
      getDraft: () => state.draft,
      setDraft: (d) => {
        state.draft = d;
        state.writes += 1;
      },
      openStep: (step) => state.steps.push(step),
    }),
  );
  return { state, hook };
}

const PERSONA = "You triage the CVEs that hit our estate and brief the SOC every morning.";

beforeEach(() => {
  reducedMotion = false;
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("useAssistPlayback", () => {
  it("opens each step before changing what it holds, and ends on the patch applied", async () => {
    const start = freshDraft(null, "Bot");
    const { state, hook } = wizard(start);
    const ops = planAssist(start, { name: "CVE brief", persona: PERSONA, tool_ids: ["builtin:web_fetch"], focus_step: "review" });

    let done!: Promise<void>;
    act(() => {
      done = hook.result.current.play(ops);
    });
    expect(hook.result.current.playing).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(hook.result.current.focus).toEqual({ step: "identity", target: "name" });
    await act(async () => {
      await vi.runAllTimersAsync();
      await done;
    });

    expect(state.steps).toEqual(["identity", "persona", "tools", "review"]);
    expect(state.draft).toEqual({ ...start, name: "CVE brief", persona: PERSONA, toolIds: ["builtin:web_fetch"] });
    expect(hook.result.current.playing).toBe(false);
    expect(hook.result.current.focus).toBeNull();
  });

  it("stops in the middle of typing: no further frame is written", async () => {
    const start = freshDraft(null, "Bot");
    const { state, hook } = wizard(start);
    act(() => {
      void hook.result.current.play(planAssist(start, { persona: PERSONA, tool_ids: ["builtin:web_fetch"] }));
    });
    // The persona step opens (600 ms), then a few typing frames (28 ms each).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600 + 28 * 5);
    });
    const typed = state.draft.persona;
    expect(typed.length).toBeGreaterThan(0);
    expect(typed.length).toBeLessThan(PERSONA.length);

    act(() => hook.result.current.stop());
    const writes = state.writes;
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(state.writes).toBe(writes);
    expect(state.draft.persona).toBe(typed);
    expect(state.draft.toolIds).toEqual([]);
    expect(state.steps).toEqual(["persona"]);
    expect(hook.result.current.playing).toBe(false);
  });

  it("keeps what the person types while it plays", async () => {
    const start = freshDraft(null, "Bot");
    const { state, hook } = wizard(start);
    act(() => {
      void hook.result.current.play(
        planAssist(start, { tool_ids: ["builtin:web_fetch", "builtin:browser"], knowledge_base_ids: ["kb-1"] }),
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    // The person edits another field between two operations.
    state.draft = { ...state.draft, description: "Mine, typed meanwhile" };
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(state.draft.description).toBe("Mine, typed meanwhile");
    expect(state.draft.toolIds).toEqual(["builtin:web_fetch", "builtin:browser"]);
    expect(state.draft.kbIds).toEqual(["kb-1"]);
  });

  it("applies everything at once when reduced motion is asked for", async () => {
    reducedMotion = true;
    const start = freshDraft(null, "Bot");
    const { state, hook } = wizard(start);
    await act(async () => {
      await hook.result.current.play(planAssist(start, { persona: PERSONA, memory: { user: false } }));
    });

    expect(state.draft.persona).toBe(PERSONA);
    expect(state.draft.memory.user).toBe(false);
    // One write per operation: no typing frames.
    expect(state.writes).toBe(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a newer answer replaces the one still playing", async () => {
    const start = freshDraft(null, "Bot");
    const { state, hook } = wizard(start);
    act(() => {
      void hook.result.current.play(planAssist(start, { persona: PERSONA, guardrails: "Never run exploit code." }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    let second!: Promise<void>;
    act(() => {
      second = hook.result.current.play(planAssist(state.draft, { name: "Second" }));
    });
    await act(async () => {
      await vi.runAllTimersAsync();
      await second;
    });

    expect(state.draft.name).toBe("Second");
    expect(state.draft.guardrails).toBe("");
    expect(state.steps[state.steps.length - 1]).toBe("identity");
  });
});
