import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/**
 * A form taller than the viewport must keep its actions reachable. The
 * library panel scrolls only a `DialogBody` child, so the app wrapper exports
 * `DialogBody` and `DialogFooter` and a long form puts its fields in the
 * first and its actions in the second, outside the scrolling region.
 * Layout itself (heights, scrollbars) is measured in the UX lab: jsdom has none.
 */
describe("dialog body and footer", () => {
  it("renders the fields inside the body and the actions in a footer outside it", () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Connect a vault</DialogTitle>
          </DialogHeader>
          <DialogBody data-testid="body">
            <label>
              Display name
              <input />
            </label>
          </DialogBody>
          <DialogFooter data-testid="footer">
            <button type="button">Connect</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    );

    const dialog = screen.getByRole("dialog", { name: "Connect a vault" });
    const body = screen.getByTestId("body");
    const footer = screen.getByTestId("footer");
    const connect = screen.getByRole("button", { name: "Connect" });

    expect(dialog).toContainElement(body);
    expect(dialog).toContainElement(footer);
    expect(body).toContainElement(screen.getByRole("textbox", { name: "Display name" }));
    expect(footer).toContainElement(connect);
    expect(body).not.toContainElement(connect);
  });
});
