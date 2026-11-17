// The agent's Skills card, expanded: the URL a skill was fetched from is a
// link only when it is a web address (`lib/safe-url.ts`), else plain text.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { Agent, AgentSkill } from "@/types";
import { SkillsCard } from "./SkillsCard";

let skills: AgentSkill[] = [];
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useSkillApi", () => ({
  useSkills: () => ({ data: skills }),
  useSkillPackageBindings: () => ({ data: [] }),
  useCreateSkill: () => idle,
  useRefreshSkill: () => idle,
}));
vi.mock("@/hooks/useAiDisabled", () => ({ useAiActionDisabled: () => false }));

function renderExpanded(url: string) {
  skills = [{ id: "skill-1", name: "Triage", url, enabled: true } as unknown as AgentSkill];
  const agent = { id: "agent-1", skill_ids: ["skill-1"] } as unknown as Agent;
  const noop = () => {};
  const result = renderWithProviders(
    <SkillsCard
      agent={agent}
      skillIds={["skill-1"]}
      setSkillIds={noop}
      skillLoadModes={{}}
      setSkillLoadModes={noop}
      editing={false}
      canManage={false}
      onStartEdit={noop}
      onSave={noop}
      onCancel={noop}
      saving={false}
      companyManaged={false}
    />,
  );
  fireEvent.click(screen.getByText("Triage"));
  return result;
}

const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("SkillsCard skill source", () => {
  it("links a web address", () => {
    const { container } = renderExpanded("https://github.com/acme/skills/triage.md");
    expect(hrefs(container)).toEqual(["https://github.com/acme/skills/triage.md"]);
  });

  it.each(HOSTILE_URLS)("shows %s as text, never as a link", (_label, url) => {
    const { container } = renderExpanded(url);
    expect(hrefs(container)).toEqual([]);
    expect(container.textContent).toContain(url);
  });
});
