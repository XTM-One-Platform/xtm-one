import { describe, it, expect, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { COMMIT_SHORT_SHA } from "@/lib/constants";

/**
 * The About dialog is opened from the profile menu, which owns its state: the
 * left navigation keeps to the navigation, as in OpenCTI and OpenAEV.
 */

vi.mock("@/hooks/usePlatformConfig", () => ({
  usePlatformConfig: () => ({ platform_version: "1.2.3" }),
  useIsXtmOne: () => true,
  usePlatformName: () => "XTM One",
  usePlatformLogoUrl: () => "/logo.png",
}));

const { AboutDialog } = await import("./AboutDialog");

describe("AboutDialog", () => {
  it("shows nothing until it is opened", () => {
    renderWithProviders(<AboutDialog open={false} onOpenChange={() => {}} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("names the platform and its running version", () => {
    renderWithProviders(<AboutDialog open onOpenChange={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "XTM One" });
    expect(dialog).toHaveTextContent(`Version 1.2.3 (${COMMIT_SHORT_SHA})`);
  });

  it("hands its closing back to the menu that opened it", () => {
    const onOpenChange = vi.fn();
    renderWithProviders(<AboutDialog open onOpenChange={onOpenChange} />);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
