import { describe, expect, it } from "vitest";
import { cn } from "@/lib/utils";

// The design system's composite type utilities set the whole type style, so the
// class passed last must win over a component's base one.
describe("cn: design-system type utilities", () => {
  it("lets a content-* token passed by the caller replace the base one", () => {
    const merged = cn("px-2.5 content-compact font-semibold", "content-caption leading-none").split(" ");
    expect(merged).toContain("content-caption");
    expect(merged).not.toContain("content-compact");
    expect(merged).toContain("font-semibold");
    expect(merged).toContain("leading-none");
  });

  it("lets a title-* token passed by the caller replace the base one", () => {
    const merged = cn("title-md", "title-xs").split(" ");
    expect(merged).toEqual(["title-xs"]);
  });

  it("keeps align-content utilities apart from the type tokens", () => {
    const merged = cn("content-center", "content-caption").split(" ");
    expect(merged).toEqual(["content-center", "content-caption"]);
  });
});
