// A page holding the navigation's rail (a dashboard whose definition says
// `collapseNavigation`): the rail shows while it is open, the person may still
// toggle it meanwhile, and leaving the page gives their own preference back,
// never written over.

import { afterEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { NavPrefsProvider, useNavPrefs, useRailHold } from "./useNavPrefs";

function Probe({ hold }: { hold: boolean }) {
  useRailHold(hold);
  const { rail, setRail } = useNavPrefs();
  return (
    <button type="button" onClick={() => setRail(!rail)}>
      {rail ? "rail" : "full"}
    </button>
  );
}

function draw(hold: boolean) {
  return render(
    <NavPrefsProvider>
      <Probe hold={hold} />
    </NavPrefsProvider>,
  );
}

afterEach(() => localStorage.clear());

describe("useRailHold", () => {
  it("shows the rail while held and gives the person's preference back after, unwritten", () => {
    const view = draw(true);
    expect(screen.getByRole("button").textContent).toBe("rail");
    // Toggled meanwhile: the person's, for this visit only.
    act(() => screen.getByRole("button").click());
    expect(screen.getByRole("button").textContent).toBe("full");
    expect(localStorage.getItem("copilot-nav:rail")).toBeNull();
    view.rerender(
      <NavPrefsProvider>
        <Probe hold={false} />
      </NavPrefsProvider>,
    );
    expect(screen.getByRole("button").textContent).toBe("full");
    expect(localStorage.getItem("copilot-nav:rail")).toBeNull();
  });

  it("leaves a rail the person chose as it is", () => {
    localStorage.setItem("copilot-nav:rail", "1");
    const view = draw(true);
    expect(screen.getByRole("button").textContent).toBe("rail");
    view.rerender(
      <NavPrefsProvider>
        <Probe hold={false} />
      </NavPrefsProvider>,
    );
    expect(screen.getByRole("button").textContent).toBe("rail");
    expect(localStorage.getItem("copilot-nav:rail")).toBe("1");
  });

  it("does nothing outside the app's navigation (a test, an embed)", () => {
    function Bare() {
      useRailHold(true);
      return <span>ok</span>;
    }
    render(<Bare />);
    expect(screen.getByText("ok")).toBeTruthy();
  });

  it("toggles and stores the preference when nothing holds the rail", () => {
    draw(false);
    act(() => screen.getByRole("button").click());
    expect(screen.getByRole("button").textContent).toBe("rail");
    expect(localStorage.getItem("copilot-nav:rail")).toBe("1");
  });
});
