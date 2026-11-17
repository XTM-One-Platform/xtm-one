// The design-system menu opened at a point, as the flow builder's
// canvas, node and edge menu uses it: focus inside on open, arrow
// keys over the enabled rows, Escape closes, an option of a set reads checked.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { PointMenu } from "./point-menu";
import type { OverflowMenuItem } from "./overflow-menu";

function items(onSelect = vi.fn()): OverflowMenuItem[] {
  return [
    { id: "delegate", label: "Delegate", checked: true, onSelect },
    { id: "consult", label: "Consult", checked: false, onSelect },
    { id: "delete", label: "Delete edge", destructive: true, separatorBefore: true, onSelect },
  ];
}

describe("PointMenu", () => {
  it("opens at the point with focus inside and the rows in order", async () => {
    renderWithProviders(<PointMenu at={{ x: 40, y: 60 }} items={items()} label="Edge actions" onClose={() => {}} />);
    const menu = await screen.findByRole("menu", { name: "Edge actions" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    const rows = Array.from(menu.querySelectorAll('[role^="menuitem"]'));
    expect(rows.map((r) => r.textContent)).toEqual(["Delegate", "Consult", "Delete edge"]);
    expect(screen.getByTestId("point-menu-anchor").style.left).toBe("40px");
  });

  it("reads the current option of a set as checked", async () => {
    renderWithProviders(<PointMenu at={{ x: 0, y: 0 }} items={items()} label="Edge actions" onClose={() => {}} />);
    const radios = await screen.findAllByRole("menuitemradio");
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false"]);
    expect(screen.getByTestId("overflow-menu-item-delete").getAttribute("role")).toBe("menuitem");
  });

  it("runs the row on Enter and closes on Escape", async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    renderWithProviders(<PointMenu at={{ x: 0, y: 0 }} items={items(onSelect)} label="Edge actions" onClose={onClose} />);
    const menu = await screen.findByRole("menu");
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId("overflow-menu-item-consult"));
    expect(onSelect).toHaveBeenCalled();
  });

  it("draws its header itself, one header element above the rows", async () => {
    renderWithProviders(
      <PointMenu at={{ x: 0, y: 0 }} items={items()} label="Edge actions" header="Handover type" onClose={() => {}} />,
    );
    const menu = await screen.findByRole("menu", { name: "Edge actions" });
    const headers = screen.getAllByTestId("menu-header");
    expect(headers).toHaveLength(1);
    expect(headers[0].textContent).toBe("Handover type");
    expect(menu.contains(headers[0])).toBe(true);
    // The header comes before the first row, outside the rows' roles.
    const firstRow = menu.querySelector('[role^="menuitem"]');
    expect(firstRow).not.toBeNull();
    expect(headers[0].compareDocumentPosition(firstRow!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(headers[0].getAttribute("role")).toBeNull();
  });

  it("groups the options under the header, named by it", async () => {
    renderWithProviders(
      <PointMenu at={{ x: 0, y: 0 }} items={items()} label="Edge actions" header="Handover type" onClose={() => {}} />,
    );
    const group = await screen.findByRole("group", { name: "Handover type" });
    const options = Array.from(group.querySelectorAll('[role^="menuitem"]'));
    expect(options.map((r) => r.getAttribute("role"))).toEqual(["menuitemradio", "menuitemradio"]);
    expect(options.map((r) => r.textContent)).toEqual(["Delegate", "Consult"]);
    // The action after the set stays outside the group, still a row of the menu.
    const remove = screen.getByTestId("overflow-menu-item-delete");
    expect(group.contains(remove)).toBe(false);
    expect(screen.getByRole("menu", { name: "Edge actions" }).contains(remove)).toBe(true);
  });

  it("adds no group without a header", async () => {
    renderWithProviders(<PointMenu at={{ x: 0, y: 0 }} items={items()} label="Edge actions" onClose={() => {}} />);
    await screen.findByRole("menu");
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("draws no header when none is given", async () => {
    renderWithProviders(<PointMenu at={{ x: 0, y: 0 }} items={items()} label="Edge actions" onClose={() => {}} />);
    await screen.findByRole("menu");
    expect(screen.queryByTestId("menu-header")).toBeNull();
  });

  it("renders nothing without a point", () => {
    renderWithProviders(<PointMenu at={null} items={items()} label="Edge actions" onClose={() => {}} />);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
