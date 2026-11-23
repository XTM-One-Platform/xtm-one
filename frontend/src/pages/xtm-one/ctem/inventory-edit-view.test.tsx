// The Inventory edit view opened by a card link outlives the card parameter (UX round 2 on #1356, B1).

import { useEffect } from "react";
import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useSearchParams } from "react-router-dom";

import { PROFILE_CARD_PARAM } from "@/lib/organization-profile-tabs";
import { useInventoryEditView } from "./inventory-edit-view";

const CARDS = ["assets_exposure", "evidence_health"] as const;

/** OrganizationSection's landing: once the card is open, `?card=` leaves the URL. */
function Landing() {
  const [searchParams, setSearchParams] = useSearchParams();
  const card = searchParams.get(PROFILE_CARD_PARAM);
  useEffect(() => {
    if (!card) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(PROFILE_CARD_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [card, setSearchParams]);
  return <div data-testid="card">card</div>;
}

function Page({ isAdmin = true }: { isAdmin?: boolean }) {
  const [editing, setEditing] = useInventoryEditView(CARDS, isAdmin);
  const location = useLocation();
  return (
    <>
      <span data-testid="search">{location.search}</span>
      {editing ? <Landing /> : <div data-testid="list">list</div>}
      <button onClick={() => setEditing(false)}>Done</button>
    </>
  );
}

function renderAt(url: string, isAdmin = true) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Page isAdmin={isAdmin} />
    </MemoryRouter>,
  );
}

describe("useInventoryEditView", () => {
  it("keeps the edit view open on the linked card after the landing drops ?card=", async () => {
    renderAt("/ctem/inventory?card=evidence_health");
    await act(async () => {});
    expect(screen.getByTestId("card")).toBeTruthy();
    expect(screen.queryByTestId("list")).toBeNull();
    const search = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
    expect(search.get(PROFILE_CARD_PARAM)).toBeNull();
    expect(search.get("edit")).toBe("1");
  });

  it("Done closes the view a card link opened", async () => {
    renderAt("/ctem/inventory?card=evidence_health");
    await act(async () => {});
    fireEvent.click(screen.getByText("Done"));
    expect(screen.getByTestId("list")).toBeTruthy();
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("ignores a card of another tab and a reader", async () => {
    renderAt("/ctem/inventory?card=tech_stack");
    await act(async () => {});
    expect(screen.getByTestId("list")).toBeTruthy();
    renderAt("/ctem/inventory?card=evidence_health", false);
    await act(async () => {});
    expect(screen.getAllByTestId("list")).toHaveLength(2);
  });
});
