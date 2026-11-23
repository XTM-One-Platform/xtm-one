// The shared typed-name destructive confirmation (#1508 UX round 1 B2, B3):
// one component for every "type the name to confirm" dialog, its body slot,
// pending state and inline error included.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { renderWithProviders } from "@/testing/providers";

function sourcesUnder(dir: string): string[] {
  const root = resolve(process.cwd(), dir);
  const out: string[] = [];
  const walk = (at: string) => {
    for (const name of readdirSync(at)) {
      const full = join(at, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (name.endsWith(".tsx") && !name.endsWith(".test.tsx")) out.push(full);
    }
  };
  walk(root);
  return out;
}

describe("ConfirmDeleteDialog", () => {
  it("renders its body, keeps an async action open, and shows the error inline", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    const { rerender } = renderWithProviders(
      <ConfirmDeleteDialog open onOpenChange={onOpenChange} name="CTEM loop" onConfirm={onConfirm} closeOnConfirm={false} confirmLabel="Reset fleet">
        <p>What is reset</p>
      </ConfirmDeleteDialog>,
    );
    expect(screen.getByText("What is reset")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Reset fleet" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText("Type CTEM loop to confirm deletion"), "CTEM loop");
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onOpenChange).not.toHaveBeenCalled();
    rerender(
      <ConfirmDeleteDialog open onOpenChange={onOpenChange} name="CTEM loop" onConfirm={onConfirm} closeOnConfirm={false} confirmLabel="Reset fleet" error="Refused: an emergency stop holds 2 automations">
        <p>What is reset</p>
      </ConfirmDeleteDialog>,
    );
    expect(screen.getByRole("alert").textContent).toContain("emergency stop");
  });

  // Review P3 on #1523: a caller that closes the dialog itself after a successful async action
  // must not leave the typed name in the field for the next open.
  it("clears the typed name when the caller closes the dialog", async () => {
    const user = userEvent.setup();
    const dialog = (open: boolean) => (
      <ConfirmDeleteDialog open={open} onOpenChange={() => undefined} name="CTEM loop" onConfirm={() => undefined} closeOnConfirm={false} confirmLabel="Reset fleet" />
    );
    const { rerender } = renderWithProviders(dialog(true));
    await user.type(screen.getByLabelText("Type CTEM loop to confirm deletion"), "CTEM loop");
    rerender(dialog(false));
    rerender(dialog(true));
    expect((screen.getByLabelText("Type CTEM loop to confirm deletion") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", { name: "Reset fleet" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("disables both buttons while pending and the confirm while confirmDisabled", () => {
    renderWithProviders(
      <ConfirmDeleteDialog open onOpenChange={() => undefined} name="x" onConfirm={() => undefined} pending />,
    );
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(true);
  });

  // UX round 3 B1: error text reads 4.5:1 in both themes with error-tertiary;
  // error-primary reads only 4.26:1 on the dark dialog surface (as agent-flow.test.tsx pins).
  it("draws every error text line in the error tone that reads 4.5:1, never error-primary", () => {
    const reset = readFileSync(resolve(process.cwd(), "src/components/fleets/FleetResetDialog.tsx"), "utf8");
    expect(reset).toContain("text-feedback-error-tertiary");
    expect(reset).not.toMatch(/(^|[\s:])text-feedback-error-primary/);
    const dialog = readFileSync(resolve(process.cwd(), "src/components/ui/confirm-delete-dialog.tsx"), "utf8");
    const errorLine = dialog.split("\n").find((line) => line.includes('data-testid="confirm-delete-error"')) ?? "";
    expect(errorLine).toContain("text-feedback-error-tertiary");
    expect(errorLine).not.toContain("text-feedback-error-primary");
  });

  // One component per pattern: the fleet reset renders through it, no second
  // typed-name destructive dialog is drawn under components/fleets.
  it("is the only typed-name confirmation of the fleet components", () => {
    const reset = readFileSync(resolve(process.cwd(), "src/components/fleets/FleetResetDialog.tsx"), "utf8");
    expect(reset).toContain("<ConfirmDeleteDialog");
    for (const file of sourcesUnder("src/components/fleets")) {
      const text = readFileSync(file, "utf8");
      if (/to confirm/i.test(text) && /<Input\b/.test(text)) {
        expect(text, file).toContain("ConfirmDeleteDialog");
      }
    }
  });
});
