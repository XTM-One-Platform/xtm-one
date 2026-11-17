// The whole fleet card opens the fleet, its description included (#10547).

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Fleet } from "@/hooks/api/useFleetApi";
import { FleetListCard } from "./FleetListCard";

const fleet = {
  id: "f1",
  user_id: "me",
  owner_name: "Me",
  name: "Security fleet",
  slug: "security-fleet",
  description: "Triage of the security alerts of the company.",
  icon: "",
  color: "",
  workspace_id: "w1",
  workspace_name: "Security",
  orchestrator_agent_id: null,
  skills: null,
  accepted_handover_types: null,
  is_archived: false,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  shared_user_ids: null,
  access_restricted: false,
  can_manage: true,
  member_count: 2,
  connected_count: 0,
  exposed_agent_ids: [],
  gated_action_count: 0,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
} satisfies Fleet;

const modules = ["github", "jira", "notion", "slack", "teams", "google", "hubspot"];

function Path() {
  return <output aria-label="current path">{useLocation().pathname}</output>;
}

function renderCard(onToggle = vi.fn()) {
  renderWithProviders(
    <MemoryRouter initialEntries={["/fleets"]}>
      <FleetListCard
        fleet={fleet}
        orchestratorName={null}
        userId="me"
        selected={false}
        onToggle={onToggle}
        modules={modules}
        starts={{ events: true, schedules: 0 }}
        canControl={false}
      />
      <Path />
    </MemoryRouter>,
  );
  return onToggle;
}

const path = () => screen.getByRole("status", { name: "current path" }).textContent;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FleetListCard", () => {
  it("a click on the description opens the fleet", () => {
    renderCard();
    fireEvent.click(screen.getByTestId("fleet-card-description"));
    expect(path()).toBe("/fleets/f1");
  });

  it("a click on the empty space or a metadata line opens the fleet", () => {
    renderCard();
    fireEvent.click(screen.getByTestId("fleet-card-starts"));
    expect(path()).toBe("/fleets/f1");
  });

  it("a drag that selected text in the description does not navigate", () => {
    renderCard();
    const description = screen.getByTestId("fleet-card-description");
    const range = document.createRange();
    range.selectNodeContents(description);
    window.getSelection()?.addRange(range);
    fireEvent.click(description);
    expect(path()).toBe("/fleets");
    window.getSelection()?.removeAllRanges();
  });

  it("the selection box and the +N modules chip keep their own action", () => {
    const onToggle = renderCard();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Security fleet" }));
    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(path()).toBe("/fleets");
    fireEvent.click(screen.getByTestId("fleet-card-modules-more"));
    expect(path()).toBe("/fleets");
  });

  it("a Ctrl click or a middle click on the description opens the fleet in a new tab", () => {
    renderCard();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    fireEvent.click(screen.getByTestId("fleet-card-description"), { ctrlKey: true });
    fireEvent(screen.getByTestId("fleet-card-description"), new MouseEvent("auxclick", { bubbles: true, button: 1 }));
    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenCalledWith("/fleets/f1", "_blank", "noopener,noreferrer");
    expect(path()).toBe("/fleets");
  });

  it("the name stays the keyboard link to the fleet", () => {
    renderCard();
    expect(screen.getByRole("link", { name: "Security fleet" }).getAttribute("href")).toBe("/fleets/f1");
  });
});
