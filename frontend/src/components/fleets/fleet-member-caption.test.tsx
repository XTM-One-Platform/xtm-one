// The fleet member caption: the visible line and its title were built twice from
// the same parts and could drift; both now come from one list.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FleetMemberCaption } from "./fleet-member-caption";

describe("FleetMemberCaption", () => {
  it("renders the visible line and its title from the same parts", () => {
    render(<FleetMemberCaption roleLabel="Analyst" parts={["Last run 2 min ago", "1 in flight", "3 open items"]} />);
    const caption = screen.getByTitle("Analyst · Last run 2 min ago · 1 in flight · 3 open items");
    expect(caption.textContent).toBe(caption.getAttribute("title"));
  });

  it("leads with the role label", () => {
    render(<FleetMemberCaption roleLabel="Analyst" parts={["No run yet"]} />);
    expect(screen.getByText("Analyst")).toHaveAttribute("data-part", "role");
    expect(screen.getByTitle("Analyst · No run yet").textContent).toBe("Analyst · No run yet");
  });

  it("starts with the health parts when there is no role label", () => {
    render(<FleetMemberCaption roleLabel={null} parts={["No run yet", "2 open items"]} />);
    const caption = screen.getByTitle("No run yet · 2 open items");
    expect(caption.textContent).toBe("No run yet · 2 open items");
    expect(screen.getByText("No run yet")).toHaveAttribute("data-part", "health");
  });

  it("renders nothing without a role label or a health part", () => {
    const { container } = render(<FleetMemberCaption roleLabel="" parts={[]} />);
    expect(container.textContent).toBe("");
  });
});
