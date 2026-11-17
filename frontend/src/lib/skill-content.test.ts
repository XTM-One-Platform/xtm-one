import { describe, expect, it } from "vitest";
import { splitSkillFrontmatter } from "./skill-content";

describe("splitSkillFrontmatter", () => {
  it("separates the frontmatter from the body", () => {
    expect(splitSkillFrontmatter("---\nname: code-review\ndescription: Reviews\n---\n\n# Steps\n")).toEqual({
      frontmatter: "name: code-review\ndescription: Reviews",
      body: "# Steps\n",
    });
  });

  it("reads a CRLF document", () => {
    expect(splitSkillFrontmatter("---\r\nname: a\r\n---\r\n\r\nBody")).toEqual({
      frontmatter: "name: a",
      body: "Body",
    });
  });

  it("returns the whole text as the body without frontmatter", () => {
    expect(splitSkillFrontmatter("# Title\n\n---\n\ntext")).toEqual({
      frontmatter: null,
      body: "# Title\n\n---\n\ntext",
    });
  });

  it("does not treat an unclosed block as frontmatter", () => {
    expect(splitSkillFrontmatter("---\nname: a\nBody").frontmatter).toBeNull();
  });
});
