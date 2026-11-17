import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { Boxes } from "lucide-react";

import { EmptyState } from "@/components/ui/empty-state";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiDisabledReason: () => null,
}));

describe("EmptyState", () => {
  it("draws its own card by default, for an empty page or list", () => {
    const { container } = renderWithProviders(
      <EmptyState icon={Boxes} title="No member yet" description="A fleet is a team of agents." />,
    );
    expect(screen.getByText("No member yet")).toBeTruthy();
    expect(container.querySelector('[data-slot="card"]')).not.toBeNull();
  });

  it("draws no card of its own inset, so a card holding it is not a card in a card", () => {
    const onAction = vi.fn();
    const { container } = renderWithProviders(
      <EmptyState
        variant="inset"
        icon={Boxes}
        title="No member yet"
        description="A fleet is a team of agents."
        actionLabel="Add a member"
        onAction={onAction}
      />,
    );
    expect(container.querySelector('[data-slot="card"]')).toBeNull();
    expect(container.querySelector('[data-slot="empty-state"]')).not.toBeNull();
    expect(screen.getByText("No member yet")).toBeTruthy();
    expect(screen.getByText("A fleet is a team of agents.")).toBeTruthy();
    screen.getByRole("button", { name: /Add a member/ }).click();
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("keeps the default density unless a widget asks for the compact one", () => {
    const plain = renderWithProviders(<EmptyState variant="inset" icon={Boxes} title="No value recorded yet" />);
    expect(plain.container.querySelector('[data-slot="empty-state"]')?.getAttribute("data-density")).toBe("default");
    const compact = renderWithProviders(<EmptyState variant="inset" density="compact" icon={Boxes} title="Nothing yet" />);
    expect(compact.container.querySelector('[data-slot="empty-state"]')?.getAttribute("data-density")).toBe("compact");
    expect(screen.getByText("Nothing yet")).toBeTruthy();
  });

  it("takes only the free height under a widget header at compact density, never the parent's full height", () => {
    // UX round 2 of xtm-one #12013: a full-height block under the widget header
    // overflowed the h6 and h7 body by the header's height and scrolled.
    const plain = renderWithProviders(<EmptyState variant="inset" icon={Boxes} title="Full height" />);
    expect(plain.container.querySelector('[data-slot="empty-state"]')?.getAttribute("data-height")).toBe("full");
    const compact = renderWithProviders(<EmptyState variant="inset" density="compact" icon={Boxes} title="Free height" />);
    expect(compact.container.querySelector('[data-slot="empty-state"]')?.getAttribute("data-height")).toBe("free");
  });

  it("draws the title alone when no description is given, as on a filtered empty list", () => {
    const { container } = renderWithProviders(
      <EmptyState icon={Boxes} title="No files match your filters." />,
    );
    const slot = container.querySelector('[data-slot="empty-state"]');
    expect(slot).not.toBeNull();
    expect(screen.getByText("No files match your filters.")).toBeTruthy();
    expect(slot?.querySelectorAll("p").length).toBe(0);
  });
});
