// The Disable confirmation previews the deactivation cascade (#122): the
// counts as ICU plurals, the successor named, the confirm button off while
// the preview loads or when the backend would refuse.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { DeactivateUserDialog } from "./DeactivateUserDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

function renderDialog(onConfirm = vi.fn()) {
  renderWithProviders(
    <DeactivateUserDialog userId="u1" userName="Dana" open onOpenChange={vi.fn()} onConfirm={onConfirm} />,
  );
  return onConfirm;
}

describe("DeactivateUserDialog", () => {
  beforeEach(() => get.mockReset());

  it("lists what the cascade does, with the successor, and confirms", async () => {
    get.mockResolvedValue({
      user_id: "u1",
      refusal: null,
      successor: { id: "a1", name: "Platform Admin", email: "admin@test.local" },
      counts: { api_keys_revoked: 1, agents_transferred: 2 },
      kept_without_successor: 0,
    });
    const onConfirm = renderDialog();
    expect(await screen.findByText("1 personal API key revoked")).toBeTruthy();
    expect(screen.getByText("2 shared agents moved to Platform Admin")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/users/u1/deactivation-preview");
    await userEvent.click(screen.getByRole("button", { name: "Disable account" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("keeps the button off and names the refusal", async () => {
    get.mockResolvedValue({
      user_id: "u1",
      refusal: "The platform admin account cannot be disabled",
      successor: null,
      counts: {},
      kept_without_successor: 0,
    });
    const onConfirm = renderDialog();
    expect(await screen.findByTestId("deactivation-refusal")).toBeTruthy();
    const button = screen.getByRole("button", { name: "Disable account" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await userEvent.click(button);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("says what stays with the account when no administrator can take it over", async () => {
    get.mockResolvedValue({ user_id: "u1", refusal: null, successor: null, counts: {}, kept_without_successor: 1 });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByText("1 shared object stays with the account: no other active administrator can take it over.")).toBeTruthy(),
    );
  });
});
