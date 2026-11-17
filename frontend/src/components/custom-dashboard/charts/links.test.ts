import { describe, expect, it } from "vitest";

import { dataHref } from "./links";

describe("a data link", () => {
  it("opens a web address or a page of the platform, nothing else", () => {
    expect(dataHref("https://cti.example/dashboard/id/1")).toBe("https://cti.example/dashboard/id/1");
    expect(dataHref("/agents")).toBe("/agents");
    for (const refused of ["javascript:alert(1)", "//evil.example/x", "/\\evil.example", "/api/v1/me", "https://user:pw@cti.example/", "", null, 3]) {
      expect(dataHref(refused)).toBeUndefined();
    }
  });
});
