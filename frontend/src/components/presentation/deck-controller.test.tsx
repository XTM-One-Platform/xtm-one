// The deck controller re-reads reveal's position on its events; an event that
// leaves the position as it was must not re-render the host, or a deck that
// re-syncs on every host render feeds itself.

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { RevealApi } from "reveal.js";

import { useDeckController } from "./deck-controller";

function fakeDeck(total: number) {
  const handlers = new Map<string, Set<() => void>>();
  const state = { index: 0, total };
  const api = {
    isReady: () => true,
    availableRoutes: () => ({
      left: state.index > 0,
      right: state.index < state.total - 1,
      up: false,
      down: false,
    }),
    getSlidePastCount: () => state.index,
    getTotalSlides: () => state.total,
    isOverview: () => false,
    isPaused: () => false,
    on: (type: string, handler: () => void) => {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type)!.add(handler);
    },
    off: (type: string, handler: () => void) => handlers.get(type)?.delete(handler),
  } as unknown as RevealApi;
  const emit = (type: string) => handlers.get(type)?.forEach((handler) => handler());
  return { api, state, emit };
}

function mountController() {
  let renders = 0;
  const hook = renderHook(() => {
    renders += 1;
    return useDeckController();
  });
  return { hook, renders: () => renders };
}

describe("useDeckController", () => {
  it("does not re-render its host when a sync leaves the position unchanged", () => {
    const deck = fakeDeck(3);
    const { hook, renders } = mountController();
    act(() => hook.result.current.attach(deck.api));
    expect(hook.result.current.total).toBe(3);
    const before = renders();
    const position = hook.result.current;

    act(() => {
      deck.emit("sync");
      deck.emit("sync");
      deck.emit("slidechanged");
    });

    expect(renders()).toBe(before);
    expect(hook.result.current).toBe(position);
  });

  it("re-renders with the new position when an event moves the deck", () => {
    const deck = fakeDeck(3);
    const { hook } = mountController();
    act(() => hook.result.current.attach(deck.api));

    deck.state.index = 1;
    act(() => deck.emit("slidechanged"));
    expect(hook.result.current.index).toBe(1);
    expect(hook.result.current.hasPrev).toBe(true);

    deck.state.total = 4;
    act(() => deck.emit("sync"));
    expect(hook.result.current.total).toBe(4);
  });

  it("returns to the idle position when the deck is detached", () => {
    const deck = fakeDeck(3);
    const { hook } = mountController();
    act(() => hook.result.current.attach(deck.api));
    act(() => hook.result.current.attach(null));
    expect(hook.result.current.total).toBe(0);
    expect(hook.result.current.ready).toBe(false);
  });
});
