// The public link of a deck or a video: on at once, off and replaced only once confirmed,
// its option switched on its own, and an administrator's switch said instead of a refused toggle.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Download } from "lucide-react";

import { renderWithProviders } from "@/testing/providers";
import { PublicLinkCard, type PublicLinkCardProps, type PublicLinkState } from "./public-link-card";

const enable = vi.fn();
const disable = vi.fn();
const regenerate = vi.fn();
const onOption = vi.fn();

const ON: PublicLinkState = {
  available: true,
  enabled: true,
  path: "/public/videos/tok",
  created_at: "2026-10-20T08:00:00Z",
  created_by_name: "Ada Lovelace",
};

function card(state: PublicLinkState | undefined, extra: Partial<PublicLinkCardProps> = {}) {
  renderWithProviders(
    <PublicLinkCard
      state={state}
      isLoading={false}
      isError={false}
      busy={false}
      replacing={false}
      enable={enable}
      disable={disable}
      regenerate={regenerate}
      description="Anyone with this link can watch."
      offConfirm="Whoever has the link is told it is gone."
      adminOffKept="Plays nothing until public links are back."
      option={{
        label: "Allow download",
        description: "A convenience, not a protection.",
        icon: Download,
        checked: false,
        onChange: onOption,
        onToast: "Download allowed",
        offToast: "Download turned off",
        testId: "t.download",
      }}
      testIdPrefix="t.link"
      {...extra}
    />,
  );
}

beforeEach(() => {
  for (const fn of [enable, disable, regenerate, onOption]) fn.mockReset().mockResolvedValue({});
});

describe("PublicLinkCard", () => {
  it("turns the link on at once, without asking", async () => {
    card({ ...ON, enabled: false, path: null });
    fireEvent.click(screen.getByTestId("t.link.switch"));
    await waitFor(() => expect(enable).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Public link turned on")).toBeInTheDocument();
    expect(screen.queryByTestId("t.download")).toBeNull();
  });

  it("shows the address, who turned it on, and asks before turning it off", async () => {
    card(ON);
    expect(screen.getByTestId("t.link.url").textContent).toMatch(/\/public\/videos\/tok$/);
    expect(screen.getByText(/Turned on by Ada Lovelace on/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("t.link.switch"));
    expect(await screen.findByText("Whoever has the link is told it is gone.")).toBeInTheDocument();
    expect(disable).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(disable).toHaveBeenCalledTimes(1));
  });

  it("asks before replacing the link, and a cancel replaces nothing", async () => {
    card(ON);
    fireEvent.click(screen.getByRole("button", { name: "Replace link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(regenerate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Replace link" }));
    // The confirmation hides the card behind it: its own button is the one left.
    expect(await screen.findByText("The current link stops working at once: whoever has it will need the new one.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Replace link" }));
    await waitFor(() => expect(regenerate).toHaveBeenCalledTimes(1));
  });

  it("switches its option on its own and says how the change went", async () => {
    card(ON);
    expect(screen.getByText("A convenience, not a protection.")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("t.download"));
    await waitFor(() => expect(onOption).toHaveBeenCalledWith(true));
    expect(await screen.findByText("Download allowed")).toBeInTheDocument();
    onOption.mockRejectedValueOnce(new Error("Public links are off"));
    fireEvent.click(screen.getByTestId("t.download"));
    expect(await screen.findByText("Could not update the public link")).toBeInTheDocument();
  });

  it("says an administrator turned public links off instead of offering a refused switch", () => {
    card({ ...ON, available: false, enabled: false, path: null });
    expect(screen.getByText("An administrator has turned public links off on this platform.")).toBeInTheDocument();
    expect(screen.getByTestId("t.link.switch")).toBeDisabled();
    expect(screen.queryByTestId("t.link.url")).toBeNull();
  });

  it("says a kept link plays nothing while public links are off", () => {
    card({ ...ON, available: false });
    expect(screen.getByText("Plays nothing until public links are back.")).toBeInTheDocument();
    expect(screen.queryByTestId("t.link.url")).toBeNull();
  });
});
