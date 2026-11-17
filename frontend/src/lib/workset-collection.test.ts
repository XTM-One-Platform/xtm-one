import { describe, expect, it } from "vitest";
import { WORKSET_COLLECTION_LABELS, worksetCollection } from "./workset-collection";

describe("worksetCollection", () => {
  it("reads an implicit agent or assignment corpus as rolling, never still collecting", () => {
    expect(worksetCollection({ sealed_at: null, implicit: true })).toBe("rolling");
    expect(WORKSET_COLLECTION_LABELS.rolling).toBe("Rolling");
  });

  it("keeps a named unsealed corpus still collecting", () => {
    expect(worksetCollection({ sealed_at: null, implicit: false })).toBe("collecting");
    expect(WORKSET_COLLECTION_LABELS.collecting).toBe("Still collecting");
  });

  it("reads a sealed corpus as sealed, implicit or not", () => {
    expect(worksetCollection({ sealed_at: "2026-11-16T00:00:00Z", implicit: false })).toBe("sealed");
    expect(worksetCollection({ sealed_at: "2026-11-16T00:00:00Z", implicit: true })).toBe("sealed");
  });
});
