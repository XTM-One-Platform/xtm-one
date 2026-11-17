// A drag hands the builder a new `nodes` on each frame: the typed
// paths are neither asked again nor recomputed for it, only when a node's id
// or kind changes.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

import type { CanvasEdge, CanvasNode } from "./graph-adapter";
import { useOutputShapes } from "./useOutputShapes";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const SEVERITY = { type: "object", properties: { severity: { type: "string" } } };
const EDGES: CanvasEdge[] = [];

function node(id: string, kind: string, x = 0): CanvasNode {
  return { id, position: { x, y: 0 }, data: { kind, config: {} } };
}

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({ nodes: { a1: { fields: SEVERITY } } });
});

describe("useOutputShapes", () => {
  it("keeps the shapes it computed while nodes only move", async () => {
    const { result, rerender } = renderHook(
      ({ nodes }: { nodes: CanvasNode[] }) => useOutputShapes(undefined, nodes, EDGES),
      {
        wrapper: withProviders(testQueryClient()),
        initialProps: { nodes: [node("a1", "agent"), node("t1", "trigger.manual")] },
      },
    );
    await waitFor(() => expect(Object.keys(result.current.get("a1") ?? {})).toEqual(["fields"]));
    const computed = result.current;

    rerender({ nodes: [node("a1", "agent", 40), node("t1", "trigger.manual", 80)] });
    rerender({ nodes: [node("a1", "agent", 60), node("t1", "trigger.manual", 120)] });
    expect(result.current).toBe(computed);
    expect(post).toHaveBeenCalledTimes(1);

    rerender({ nodes: [node("a1", "agent", 60), node("t2", "trigger.manual")] });
    expect(result.current).not.toBe(computed);
    expect([...result.current.keys()]).toEqual(["a1", "t2"]);

    const withT2 = result.current;
    rerender({ nodes: [node("a1", "util.cast", 60), node("t2", "trigger.manual")] });
    expect(result.current).not.toBe(withT2);
  });
});
