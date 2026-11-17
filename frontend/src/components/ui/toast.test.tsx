/**
 * A toast raised over an open dialog - `yarn test:dom`.
 *
 * The toast viewport is a Radix dismissable-layer branch, so pressing a toast
 * is not a press outside the dialog. That holds only while the toast and the
 * dialog share ONE copy of `@radix-ui/react-dismissable-layer`: the library's
 * `^1.2.23` range on `@radix-ui/react-toast` resolves to a release carrying
 * newer Radix internals than every other primitive here, which is why
 * `package.json` pins it through `resolutions`. With two copies, the Reload
 * button of the stale-build notice closes whatever dialog is open.
 */
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";

function RaiseToast({ onAction }: { onAction: () => void }) {
  const { toast } = useToast();
  useEffect(() => {
    toast({ title: "A new version is available", variant: "info", action: { label: "Reload", onClick: onAction } });
  }, [toast, onAction]);
  return null;
}

function RaiseNotice() {
  const { toast } = useToast();
  useEffect(() => {
    toast({ title: "Agent updated", variant: "success" });
  }, [toast]);
  return null;
}

describe("a toast's auto-remove timer", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  // A timer left behind fires after the test file's DOM is gone, and vitest
  // fails the whole run on its "window is not defined".
  it("is cleared when the provider unmounts", () => {
    vi.useFakeTimers();
    const { unmount } = renderWithProviders(<RaiseNotice />);
    expect(screen.getByText("Agent updated")).toBeTruthy();
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("a toast over an open dialog", () => {
  it("takes the click without closing the dialog", async () => {
    const onAction = vi.fn();
    const onOpenChange = vi.fn();
    renderWithProviders(
      <Dialog open onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogTitle>Edit agent</DialogTitle>
          <DialogDescription>The dialog the toast is raised from.</DialogDescription>
          <RaiseToast onAction={onAction} />
        </DialogContent>
      </Dialog>,
    );

    // A modal dialog hides everything outside it from the accessibility tree,
    // the toast region included.
    const reload = await screen.findByRole("button", { name: "Reload", hidden: true });
    fireEvent.pointerDown(reload);
    fireEvent.click(reload);

    expect(onAction).toHaveBeenCalledOnce();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Edit agent" })).toBeTruthy();
  });
});
