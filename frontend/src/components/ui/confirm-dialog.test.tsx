// ConfirmDialog translates each string once: a catalog key
// translates, a title the caller already translated (a count baked into an
// ICU plural) shows as written, never looked up or parsed as ICU again.

import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { ConfirmDialog, useConfirm } from "./confirm-dialog";

function BulkDelete({ onResult }: { onResult: (ok: boolean) => void }) {
  const [confirmElement, confirm] = useConfirm();
  return (
    <>
      <button
        type="button"
        onClick={async () => {
          onResult(await confirm({ title: "Delete 3 tasks?", confirmLabel: "Delete" }));
        }}
      >
        Delete selection
      </button>
      {confirmElement}
    </>
  );
}

describe("ConfirmDialog", () => {
  it("shows a title the caller already translated as written", () => {
    // An apostrophe and braces: parsed as an ICU message a second time, the
    // quote would be eaten and the braces read as an argument.
    const title = "Supprimer l'entrée {draft} ?";
    renderWithProviders(<ConfirmDialog open onOpenChange={vi.fn()} onConfirm={vi.fn()} title={title} />);
    expect(screen.getByRole("dialog", { name: title })).toBeTruthy();
  });

  it("translates a catalog key", () => {
    renderWithProviders(
      <ConfirmDialog open onOpenChange={vi.fn()} onConfirm={vi.fn()} title="Delete shared state?" confirmLabel="Delete" />,
    );
    expect(screen.getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });
});

// The bulk-delete confirm gives focus back to the button that opened it:
// after Escape, Cancel or the
// confirm, focus is on the trigger again, never on document.body.
describe("useConfirm focus return", () => {
  type User = ReturnType<typeof userEvent.setup>;
  it.each([
    { how: "Escape", close: (user: User) => user.keyboard("{Escape}"), expected: false },
    { how: "Cancel", close: (user: User) => user.click(screen.getByRole("button", { name: "Cancel" })), expected: false },
    { how: "Delete", close: (user: User) => user.click(screen.getByRole("button", { name: "Delete" })), expected: true },
  ])("gives focus back to the trigger after $how", async ({ close, expected }) => {
    const user = userEvent.setup();
    const results: boolean[] = [];
    renderWithProviders(<BulkDelete onResult={(ok) => results.push(ok)} />);
    const trigger = screen.getByRole("button", { name: "Delete selection" });
    trigger.focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog");
    await close(user);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(results).toEqual([expected]);
  });
});
