import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { OrphanedIntegrationsNotice } from "./OrphanedIntegrationsNotice";

const render = (ui: ReactElement, options?: Parameters<typeof renderWithProviders>[1]) =>
  renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>, options);

const hibob = { id: "i-1", name: "HiBob", provider: "hibob" };

describe("OrphanedIntegrationsNotice", () => {
  it("renders nothing without an orphaned binding", () => {
    render(<OrphanedIntegrationsNotice agent={{ id: "a-1", orphaned_integrations: [], integration_bindings: {} }} />);
    expect(screen.queryByTestId("orphaned-integrations-notice")).toBeNull();
  });

  it("names the integration its owner lost, links it and says why its runs refuse it", () => {
    render(
      <OrphanedIntegrationsNotice agent={{ id: "a-1", orphaned_integrations: [hibob], integration_bindings: { hibob: ["i-1"] } }} />,
    );
    expect(screen.getByText("Owner no longer has access to this integration")).toBeTruthy();
    expect(screen.getByRole("link", { name: "HiBob" }).getAttribute("href")).toBe("/integrations/i-1");
    expect(
      screen.getByText("Its runs refuse this integration's tools until its owner gets access back or the binding is removed."),
    ).toBeTruthy();
  });

  it("offers Remove binding to a viewer who manages the agent only", () => {
    const agent = { id: "a-1", orphaned_integrations: [hibob], integration_bindings: { hibob: ["i-1"] } };
    const { unmount } = render(<OrphanedIntegrationsNotice agent={agent} />);
    expect(screen.queryByRole("button", { name: "Remove binding" })).toBeNull();
    unmount();
    render(<OrphanedIntegrationsNotice agent={agent} canManage />);
    expect(screen.getByRole("button", { name: "Remove binding" })).toBeTruthy();
  });

  it("reads two integrations in French", () => {
    render(
      <OrphanedIntegrationsNotice
        agent={{ id: "a-1", orphaned_integrations: [hibob, { id: "i-2", name: "Jira", provider: "jira" }], integration_bindings: {} }}
      />,
      { locale: "fr" },
    );
    expect(screen.getByTestId("orphaned-integrations-notice").textContent).toContain("ces 2 intégrations");
  });
});
