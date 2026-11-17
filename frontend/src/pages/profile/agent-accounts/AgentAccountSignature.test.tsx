// Where an account's reply signature comes from: its own, a signature integration's, or none.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { AgentAccountSignatureSource } from "@/types";
import { AgentAccountSignature } from "./AgentAccountSignature";

let integrations = [
  { id: "ms-1", name: "Sam's Microsoft", provider: "microsoft" },
  { id: "lsk-1", name: "Company Letsignit", provider: "letsignit" },
];

vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useIntegrations: () => ({ data: integrations }),
}));

function renderCard(source: AgentAccountSignatureSource, integrationId = "") {
  const onChange = vi.fn();
  renderWithProviders(
    <AgentAccountSignature source={source} integrationId={integrationId} providers={["letsignit"]} onChange={onChange} />,
  );
  return onChange;
}

describe("AgentAccountSignature", () => {
  it("offers the three sources and says what the chosen one does", () => {
    renderCard("own");
    expect(screen.getByRole("button", { name: "The account's own" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "From a signature integration" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "None" })).toBeTruthy();
    expect(screen.getByText("Copied from the account's sent mail: the signature its latest emails carried.")).toBeTruthy();
    expect(screen.queryByLabelText("Signature integration")).toBeNull();
  });

  it("picks the first signature integration when switched to one, never another provider's", () => {
    const onChange = renderCard("own");
    fireEvent.click(screen.getByRole("button", { name: "From a signature integration" }));
    expect(onChange).toHaveBeenCalledWith("integration", "lsk-1");
    fireEvent.click(screen.getByRole("button", { name: "None" }));
    expect(onChange).toHaveBeenLastCalledWith("none", "");
  });

  it("asks for a signature integration when there is none to pick", () => {
    integrations = [{ id: "ms-1", name: "Sam's Microsoft", provider: "microsoft" }];
    try {
      renderCard("integration");
      expect(screen.getByLabelText("Signature integration")).toBeTruthy();
      expect(screen.getByText("Connect a signature integration (Letsignit) first.")).toBeTruthy();
    } finally {
      integrations = [
        { id: "ms-1", name: "Sam's Microsoft", provider: "microsoft" },
        { id: "lsk-1", name: "Company Letsignit", provider: "letsignit" },
      ];
    }
  });

  it("says the mail server signs when none is chosen", () => {
    renderCard("none");
    expect(screen.getByText(/The mail server signs every outgoing email in transit/)).toBeTruthy();
  });
});
