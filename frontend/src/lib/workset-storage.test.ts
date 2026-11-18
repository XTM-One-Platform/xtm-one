import { describe, expect, it } from "vitest";
import { isWorksetStorage } from "./workset-storage";

const failure = (status: number, detail: unknown) => Object.assign(new Error("x"), { status, detail });

describe("isWorksetStorage", () => {
  it("reads the 404 a workset's internal knowledge base answers with", () => {
    expect(isWorksetStorage(failure(404, { code: "workset_storage", workset_id: "w", workset_name: "agent-x" }))).toBe(true);
  });

  it("leaves any other failure to the plain not-found state or the error boundary", () => {
    expect(isWorksetStorage(failure(404, "Knowledge base not found"))).toBe(false);
    expect(isWorksetStorage(failure(500, { code: "workset_storage" }))).toBe(false);
    expect(isWorksetStorage(null)).toBe(false);
    expect(isWorksetStorage(new Error("network"))).toBe(false);
  });
});
