// `<EditionBadge />` in both modes (#9448, design dev-docs/roi-outcomes.md
// 6.1 C1): the edition in XTM One mode, the licence state in Copilot mode.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { EditionBadge } from "./enterprise-upsell";

const platform = { config: null as null | object, edition: "" as "" | "enterprise" | "community", licensed: false };

vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  usePlatformConfig: () => platform.config,
  useEdition: () => platform.edition,
  useAgenticAIEnabled: () => platform.licensed,
  useIsXtmOne: () => platform.edition !== "",
  useAiEnabled: () => true,
}));

describe("EditionBadge", () => {
  beforeEach(() => {
    platform.config = { platform_mode: "copilot" };
    platform.edition = "";
    platform.licensed = false;
  });

  it("says Licensed in Copilot mode with a valid licence", () => {
    platform.licensed = true;
    renderWithProviders(<EditionBadge />);
    expect(screen.getByText("Licensed")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says Community in Copilot mode without a licence", () => {
    renderWithProviders(<EditionBadge />);
    expect(screen.getByText("Community")).toBeTruthy();
  });

  it("draws nothing before the platform config is read", () => {
    platform.config = null;
    const { container } = renderWithProviders(<EditionBadge />);
    expect(container.textContent).toBe("");
  });

  it("keeps the edition in XTM One mode", () => {
    platform.config = { platform_mode: "xtm_one" };
    platform.edition = "enterprise";
    platform.licensed = true;
    renderWithProviders(<EditionBadge />);
    expect(screen.getByText("Enterprise Edition")).toBeTruthy();
    expect(screen.queryByText("Licensed")).toBeNull();
  });

  it("opens the upsell from Community Edition in XTM One mode", () => {
    platform.config = { platform_mode: "xtm_one" };
    platform.edition = "community";
    renderWithProviders(<EditionBadge />);
    expect(screen.getByRole("button")).toBeTruthy();
  });
});
