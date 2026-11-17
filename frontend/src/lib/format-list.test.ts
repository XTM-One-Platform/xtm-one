import { describe, expect, it } from "vitest";

import { formatList } from "./format-list";

describe("formatList", () => {
  it("joins in the viewer's language", () => {
    expect(formatList(["Alpha", "Beta", "Gamma"], "en")).toBe("Alpha, Beta, and Gamma");
    expect(formatList(["Alpha", "Beta"], "fr")).toBe("Alpha et Beta");
    expect(formatList(["Alpha", "Beta"], "es")).toBe("Alpha y Beta");
  });

  it("returns a single name alone and nothing for none", () => {
    expect(formatList(["Alpha"], "en")).toBe("Alpha");
    expect(formatList([], "en")).toBe("");
  });

  it("falls back to a comma join on a locale the runtime rejects", () => {
    expect(formatList(["Alpha", "Beta"], "not a locale!")).toBe("Alpha, Beta");
  });
});
