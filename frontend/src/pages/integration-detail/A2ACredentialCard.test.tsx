// The Partner credential card of an inbound oauth_a2a integration (#105).
// The page hands the card the integration it found in the LIST, whose rows
// carry no a2a_credential: the last refused token request must come from the
// single-integration read, or the panel never renders (UX round 1, B2).

import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { api } from "@/lib/api";
import type { Integration } from "@/types";
import { A2ACredentialCard } from "./A2AProtocolCards";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/api/useA2AClientApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useA2AClientApi")>()),
  useA2ADirection: () => "inbound",
}));
vi.mock("./a2a-card-rows", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./a2a-card-rows")>()),
  useBoundAgentCards: () => ({ tokenUrl: null }),
}));

// As the list endpoint serves it: no a2a_credential.
const listed = {
  id: "i-1",
  provider: "oauth_a2a",
  name: "Partner intake",
  a2a_direction: "inbound",
  extra_config: { client_id: "a2a-current" },
  a2a_credential: null,
} as unknown as Integration;

const single = (reason: string | null) => ({
  ...listed,
  a2a_credential: {
    last_refused_token_request: {
      at: "2026-11-18T09:39:00.678436+00:00",
      client_id: "a2a-current",
      reason,
      what_to_do: "Backend English fallback.",
    },
  },
});

describe("A2ACredentialCard, last refused token request", () => {
  it("renders the refusal read from the single integration, not from the list row", async () => {
    vi.mocked(api.get).mockResolvedValue(single("secret_mismatch"));
    renderWithProviders(<A2ACredentialCard integration={listed} canManage />);

    const panel = await screen.findByTestId("a2a-credential-last-refusal");
    expect(api.get).toHaveBeenCalledWith("/integrations/i-1");
    expect(panel.textContent).toContain("Wrong secret for the current client ID");
    expect(panel.textContent).toContain("The partner must paste the latest secret.");
    // A formatted date, never the raw ISO string.
    expect(screen.getByTestId("a2a-credential-last-refusal-at").textContent).not.toContain("2026-11-18T09:39");
    expect(panel.textContent).not.toContain("Backend English fallback.");
  });

  it("falls back to the backend sentence for a reason code it does not know", async () => {
    vi.mocked(api.get).mockResolvedValue(single("something_new"));
    renderWithProviders(<A2ACredentialCard integration={listed} canManage />);

    const panel = await screen.findByTestId("a2a-credential-last-refusal");
    expect(panel.textContent).toContain("Backend English fallback.");
  });

  it("does not read the refusal for a person who cannot manage the integration", async () => {
    vi.mocked(api.get).mockClear();
    renderWithProviders(<A2ACredentialCard integration={listed} canManage={false} />);

    await waitFor(() => expect(screen.getByText("Partner credential")).toBeTruthy());
    expect(api.get).not.toHaveBeenCalledWith("/integrations/i-1");
    expect(screen.queryByTestId("a2a-credential-last-refusal")).toBeNull();
  });
});
