import { describe, expect, it } from "vitest";
import { worksetLink, worksetStorageOf } from "./workset-storage";

const failure = (status: number, detail: unknown) => Object.assign(new Error("x"), { status, detail });

describe("worksetStorageOf", () => {
  it("names the workset a workset's internal knowledge base belongs to", () => {
    expect(worksetStorageOf(failure(404, { code: "workset_storage", workset_id: "w", workset_name: "Lab sample closed week" }))).toEqual({
      id: "w",
      name: "Lab sample closed week",
    });
  });

  it("falls back to the workset id when the 404 carries no name", () => {
    expect(worksetStorageOf(failure(404, { code: "workset_storage", workset_id: "w" }))).toEqual({ id: "w", name: "w" });
  });

  it("leaves any other failure to the plain not-found state or the error boundary", () => {
    expect(worksetStorageOf(failure(404, "Knowledge base not found"))).toBeNull();
    expect(worksetStorageOf(failure(500, { code: "workset_storage" }))).toBeNull();
    expect(worksetStorageOf(null)).toBeNull();
    expect(worksetStorageOf(new Error("network"))).toBeNull();
  });
});

describe("worksetLink", () => {
  it("opens Operations > Worksets on that workset alone", () => {
    expect(worksetLink({ id: "w", name: "Lab sample closed week" })).toBe("/operations/worksets?vis=all&wks_q=Lab+sample+closed+week");
  });
});
