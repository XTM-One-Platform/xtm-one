// Per-user (template) integrations can be kept to their read tools (#10741):
// the Tooling tab carries a Read-only switch on each per-user card, saves it as
// integration_template_read_only (only providers still bound per user, as the
// server refuses any other) and shows a Read-only badge outside edit mode.
// jsdom cannot render the whole ConfigTab cheaply, so the guard reads the source.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (rel: string) => readFileSync(join(process.cwd(), rel), "utf8");
const CONFIG_TAB = read("src/pages/agent-detail/ConfigTab.tsx");
const TOOLING = read("src/pages/agent-detail/config/ToolingSection.tsx");

describe("per-user integration read-only switch", () => {
  it("saves integration_template_read_only filtered to the providers still bound per user", () => {
    expect(CONFIG_TAB).toContain(
      "integration_template_read_only: templateReadOnly.filter((p) => templateBindings.includes(p)).sort()",
    );
    expect(CONFIG_TAB).toContain('integration_template_read_only: "integration_template_read_only"');
  });

  it("clears the read-only mark when the per-user binding is turned off", () => {
    const toggle = CONFIG_TAB.slice(CONFIG_TAB.indexOf("const toggleTemplateBinding"), CONFIG_TAB.indexOf("const toggleTemplateBinding") + 600);
    expect(toggle).toContain("setTemplateReadOnly((prev) => prev.filter((p) => p !== provider))");
  });

  it("resets the draft from the agent on edit and on cancel", () => {
    const resets = CONFIG_TAB.match(/setTemplateReadOnly\(agent\.integration_template_read_only \|\| \[\]\)/g) ?? [];
    expect(resets.length).toBe(2);
  });

  it("renders a named Switch on each per-user card and a badge outside edit mode", () => {
    expect(TOOLING).toMatch(/<Switch\s+checked=\{templateReadOnly\.includes\(provider\)\}/);
    expect(TOOLING).toContain('aria-label={t_i18n("Read-only: {provider}"');
    expect(TOOLING).toContain("(agent.integration_template_read_only || []).includes(provider)");
  });
});
