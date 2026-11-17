// Each person's own account on a shared integration: the card a person
// who acts with their own account sees, and what it sends.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Integration } from "@/types";
import { YourAccountCard } from "./YourAccountCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const integration = (overrides: Partial<Integration> = {}) =>
  ({
    id: "i-1",
    provider: "openapi",
    auth_type: "oauth",
    name: "SaaS Manager",
    has_client_id: true,
    credential_mode: "per_user",
    uses_own_credential: true,
    my_credential: null,
    ...overrides,
  }) as Integration;

describe("YourAccountCard", () => {
  it("offers to connect an OAuth account through the integration's sign-in", () => {
    const onAuthorize = vi.fn();
    renderWithProviders(<YourAccountCard integration={integration()} onAuthorize={onAuthorize} />);

    expect(screen.getByText("Not connected")).toBeTruthy();
    expect(screen.queryByText("Disconnect my account")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Connect your account with/ }));
    expect(onAuthorize).toHaveBeenCalledOnce();
  });

  it("shows a connected account and lets its owner reconnect it", () => {
    renderWithProviders(
      <YourAccountCard
        integration={integration({
          my_credential: {
            status: "connected",
            has_access_token: true,
            token_expires_at: null,
            connected_at: "2026-10-09T10:00:00Z",
            updated_at: null,
          },
        })}
        onAuthorize={vi.fn()}
      />,
    );

    expect(screen.getByText("Connected")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Reconnect your account with/ })).toBeTruthy();
    expect(screen.getByText("Disconnect my account")).toBeTruthy();
  });

  it("signs the person in for their own account through the page's Authorize", () => {
    const onAuthorize = vi.fn();
    renderWithProviders(<YourAccountCard integration={integration()} onAuthorize={onAuthorize} />);

    fireEvent.click(screen.getByRole("button", { name: /Connect your account with/ }));

    expect(onAuthorize).toHaveBeenCalledOnce();
  });
});
