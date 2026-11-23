// The Inventory edit view opened by a card link outlives the card parameter (UX round 2 on #1356, B1), and a late
// landing that writes back a URL snapshot taken before `edit=1` (cold load, UX round 3 on #1356, B1).

import { useEffect, useRef } from "react";
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

/** The cold load: the profile data resolves after mount and the landing writes the URL it saw first, `edit=1` lost. */
function LateLanding({ dataReady }: { dataReady: Promise<void> }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const first = useRef(new URLSearchParams(searchParams));
  const landed = useRef(false);
  useEffect(() => {
    let live = true;
    void dataReady.then(() => {
      // Once, as OrganizationSection lands once: its effect runs only while `?card=` is in the URL.
      if (!live || landed.current) return;
      landed.current = true;
      const stale = new URLSearchParams(first.current);
      stale.delete(PROFILE_CARD_PARAM);
      setSearchParams(stale, { replace: true });
    });
    return () => {
      live = false;
    };
  }, [dataReady, setSearchParams]);
  return <div data-testid="card">card</div>;
}

function Page({ isAdmin = true, dataReady }: { isAdmin?: boolean; dataReady?: Promise<void> }) {
  const [editing, setEditing] = useInventoryEditView(CARDS, isAdmin);
  const location = useLocation();
  return (
    <>
      <span data-testid="search">{location.search}</span>
      {editing ? (
        dataReady ? <LateLanding dataReady={dataReady} /> : <Landing />
      ) : (
        <div data-testid="list">list</div>
      )}
      <button onClick={() => setEditing(false)}>Done</button>
    </>
  );
}

function renderAt(url: string, isAdmin = true, dataReady?: Promise<void>) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Page isAdmin={isAdmin} dataReady={dataReady} />
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

  it("stays open when the profile data resolves late and the landing writes back a stale URL", async () => {
    let resolve: () => void = () => {};
    const dataReady = new Promise<void>((r) => {
      resolve = r;
    });
    renderAt("/ctem/inventory?card=evidence_health", true, dataReady);
    await act(async () => {});
    expect(new URLSearchParams(screen.getByTestId("search").textContent ?? "").get("edit")).toBe("1");
    await act(async () => {
      resolve();
      await dataReady;
    });
    await act(async () => {});
    expect(screen.getByTestId("card")).toBeTruthy();
    expect(screen.queryByTestId("list")).toBeNull();
    const search = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
    expect(search.get(PROFILE_CARD_PARAM)).toBeNull();
    expect(search.get("edit")).toBe("1");
    fireEvent.click(screen.getByText("Done"));
    expect(screen.getByTestId("list")).toBeTruthy();
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
