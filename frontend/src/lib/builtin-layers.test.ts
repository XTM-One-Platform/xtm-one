import { describe, expect, it } from "vitest";

import type { AgentSkill } from "@/types";

import {
  LEARNED_FROZEN_TAG,
  learnedLessonTitles,
  learnedSkillFrozen,
  learnedSkillOf,
  versionRunId,
  yoursItemSet,
} from "./builtin-layers";

const skill = (id: string, tags: string[], content = ""): AgentSkill => ({ id, tags, content }) as unknown as AgentSkill;

describe("builtin layers", () => {
  it("finds the learned skill by its agent tag only", () => {
    const skills = [skill("a", ["learned-for-agent:other"]), skill("b", ["builtin-learned", "learned-for-agent:agent-1"])];
    expect(learnedSkillOf(skills, "agent-1")?.id).toBe("b");
    expect(learnedSkillOf(skills, "agent-2")).toBeUndefined();
    expect(learnedSkillOf(undefined, "agent-1")).toBeUndefined();
  });

  it("reads the frozen tag", () => {
    expect(learnedSkillFrozen(skill("a", [LEARNED_FROZEN_TAG]))).toBe(true);
    expect(learnedSkillFrozen(skill("a", []))).toBe(false);
    expect(learnedSkillFrozen(undefined)).toBe(false);
  });

  it("lists the lessons as the level-2 section titles", () => {
    const body = "# Learned\n\nIntro\n\n## Asset owners\n- db: platform team\n\n## Ticket routing\nITS for prod\n";
    expect(learnedLessonTitles(body)).toEqual(["Asset owners", "Ticket routing"]);
    expect(learnedLessonTitles(null)).toEqual([]);
  });

  it("reads the run a version names", () => {
    expect(versionRunId("append 'Asset owners' (run 7b610aa5-b1fc-4880-aafe-3f07b7d36ec7)")).toBe(
      "7b610aa5-b1fc-4880-aafe-3f07b7d36ec7",
    );
    expect(versionRunId("created empty")).toBeNull();
    expect(versionRunId(null)).toBeNull();
  });

  it("says which customer-layer item is set", () => {
    const agent = { customer_fields: ["mcp_server_ids"], customer_notes: "  ", integration_bindings: { jira: ["i1"] } };
    expect(yoursItemSet("MCP servers", agent)).toBe(true);
    expect(yoursItemSet("Integrations and their tools", agent)).toBe(true);
    expect(yoursItemSet("Organization notes", agent)).toBe(false);
    expect(yoursItemSet("Model", agent)).toBe(false);
    expect(yoursItemSet("Organization notes", { customer_notes: "Owner of db: platform" })).toBe(true);
  });
});
