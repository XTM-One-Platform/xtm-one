// The agent-template gallery: "From Scratch" and every template card are buttons a
// keyboard reaches and picks with Enter or Space, and assistive technology names.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { AgentTemplate } from "@/types";
import { TemplateGallery } from "./template-gallery";

const TRIAGE: AgentTemplate = {
  id: "t-1",
  slug: "alert-triage",
  name: "Alert Triage",
  description: "Sorts incoming alerts.",
  icon: "bot",
  category: "soc",
  disable_chat: false,
  required_integrations: null,
  required_integration_types: null,
  recommended_mcp: null,
  must_be_defined: null,
  default_persona: null,
  default_skills: null,
  default_skill_ids: [],
  assignment_count: 0,
  is_builtin: true,
  enabled: true,
};

const onSelect = vi.fn();

function gallery() {
  renderWithProviders(<TemplateGallery templates={[TRIAGE]} onSelect={onSelect} />);
}

describe("TemplateGallery", () => {
  beforeEach(() => onSelect.mockReset());

  it("exposes each card as a focusable button named by its title and described by its description", () => {
    gallery();
    const scratch = screen.getByRole("button", { name: "From Scratch" });
    const template = screen.getByRole("button", { name: "Alert Triage" });
    expect(scratch.tabIndex).toBe(0);
    expect(template.tabIndex).toBe(0);
    expect(template).toHaveAccessibleDescription("Sorts incoming alerts.");
  });

  it("picks a template with Enter and from scratch with Space", () => {
    gallery();
    fireEvent.keyDown(screen.getByRole("button", { name: "Alert Triage" }), { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenLastCalledWith(TRIAGE);

    fireEvent.keyDown(screen.getByRole("button", { name: "From Scratch" }), { key: " " });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("still picks on click, and ignores other keys", () => {
    gallery();
    const template = screen.getByRole("button", { name: "Alert Triage" });
    fireEvent.keyDown(template, { key: "a" });
    fireEvent.keyDown(template, { key: "Tab" });
    expect(onSelect).toHaveBeenCalledTimes(0);

    fireEvent.click(template);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenLastCalledWith(TRIAGE);
  });
});
