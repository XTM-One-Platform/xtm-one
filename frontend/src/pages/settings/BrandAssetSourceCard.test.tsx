// Brand asset source: an administrator sets a GitHub folder and the integration
// reading it; a URL that is not a GitHub folder, or no integration, is flagged on
// the field and cannot be saved; only integrations that share their account are
// offered; a saved source can be synced, or cleared once confirmed.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { BrandAssetSource } from "@/types";

const save = vi.fn();
const sync = vi.fn();
let source: BrandAssetSource;

vi.mock("@/hooks/api/useTemplateContainerApi", () => ({
  useBrandAssetSource: () => ({ data: source }),
  useSaveBrandAssetSource: () => ({ mutate: save, isPending: false }),
  useSyncBrandAssetSource: () => ({ mutate: sync, isPending: false }),
}));

vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useIntegrations: () => ({
    data: [
      { id: "gh-1", name: "Brand GitHub", provider: "github", status: "connected" },
      { id: "slack-1", name: "Slack", provider: "slack", status: "connected" },
      { id: "gh-mine", name: "My own GitHub", provider: "github", status: "connected", credential_mode: "per_user" },
    ],
  }),
}));

import { BrandAssetSourceCard } from "./BrandAssetSourceCard";

beforeEach(() => {
  save.mockReset();
  sync.mockReset();
  source = { url: null, integration_id: null, integration_name: null, sync: null };
});

describe("BrandAssetSourceCard", () => {
  it("refuses a URL that is not a GitHub folder, on the field", () => {
    renderWithProviders(<BrandAssetSourceCard />);
    const field = screen.getByLabelText("GitHub folder");
    fireEvent.change(field, { target: { value: "https://gitlab.com/acme/brand" } });
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter a GitHub folder URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
  });

  it("takes the host in any case, as the server does", () => {
    renderWithProviders(<BrandAssetSourceCard />);
    const field = screen.getByLabelText("GitHub folder");
    fireEvent.change(field, { target: { value: "https://GitHub.com/acme/brand" } });
    expect(field).not.toHaveAttribute("aria-invalid", "true");
  });

  it("offers only the GitHub integrations that share their account", async () => {
    renderWithProviders(<BrandAssetSourceCard />);
    await userEvent.setup().click(screen.getByLabelText("GitHub integration"));
    expect(await screen.findByText("Brand GitHub")).toBeInTheDocument();
    expect(screen.queryByText("My own GitHub")).not.toBeInTheDocument();
    expect(screen.queryByText("Slack")).not.toBeInTheDocument();
  });

  it("asks for the integration before a folder can be saved", () => {
    renderWithProviders(<BrandAssetSourceCard />);
    fireEvent.change(screen.getByLabelText("GitHub folder"), {
      target: { value: "https://github.com/acme/brand/tree/main/packs" },
    });
    expect(screen.getByText("Select the GitHub integration that reads the folder")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
  });

  it("saves the folder with its integration", async () => {
    source = { ...source, integration_id: "gh-1", integration_name: "Brand GitHub" };
    renderWithProviders(<BrandAssetSourceCard />);
    fireEvent.change(screen.getByLabelText("GitHub folder"), {
      target: { value: " https://github.com/acme/brand/tree/main/packs " },
    });
    await userEvent.setup().click(screen.getByRole("button", { name: /save/i }));
    expect(save).toHaveBeenCalledWith(
      { url: "https://github.com/acme/brand/tree/main/packs", integration_id: "gh-1" },
      expect.anything(),
    );
  });

  it("syncs a saved source, and clears it only once confirmed", async () => {
    source = {
      url: "https://github.com/acme/brand",
      integration_id: "gh-1",
      integration_name: "Brand GitHub",
      sync: null,
    };
    renderWithProviders(<BrandAssetSourceCard />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(sync).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(save).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: "Clear the brand asset source?" });
    await user.click(within(dialog).getByRole("button", { name: "Clear" }));
    expect(save).toHaveBeenCalledWith({ url: null, integration_id: null }, expect.anything());
  });
});
