/**
 * The one info popover (#9836 R4b, UX round 1 B2-B6) - `yarn test:unit`.
 * Escape closes it with the focus on its trigger, a keyboard open moves the
 * focus into it, a page scroll closes it, it flips above a trigger near the
 * bottom of the viewport, and no other component portals its own popover.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { placeInfoPopover } from "@/lib/info-popover-placement";

import { InfoPopover } from "./info-popover";

function renderPopover(onCard?: () => void) {
  return render(
    <div onClick={onCard} onKeyDown={onCard}>
      <InfoPopover label="How this is computed" icon={<span>i</span>} heading="How this is computed" width={320} popoverTestId="pop" triggerTestId="trigger">
        <a href="#source">A source</a>
      </InfoPopover>
    </div>,
  );
}

describe("InfoPopover", () => {
  it("closes on Escape while the focus is on its trigger (B2)", () => {
    renderPopover();
    const trigger = screen.getByTestId("trigger");
    trigger.focus();
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByTestId("pop")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("moves the focus into the dialog when opened by keyboard (B3)", () => {
    renderPopover();
    const trigger = screen.getByTestId("trigger");
    trigger.focus();
    // A keyboard activation is a click with detail 0.
    fireEvent.click(trigger, { detail: 0 });
    const pop = screen.getByRole("dialog", { name: "How this is computed" });
    expect(pop.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(pop, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("closes when the page scrolls, never drifting from its trigger (B4)", () => {
    renderPopover();
    fireEvent.click(screen.getByTestId("trigger"));
    expect(screen.getByTestId("pop")).toBeTruthy();
    act(() => {
      window.dispatchEvent(new Event("scroll"));
    });
    expect(screen.queryByTestId("pop")).toBeNull();
  });

  it("never opens the card it sits in", () => {
    let opened = 0;
    renderPopover(() => (opened += 1));
    const trigger = screen.getByTestId("trigger");
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(opened).toBe(0);
  });
});

describe("placeInfoPopover (B5)", () => {
  const viewport = { width: 1024, height: 800 };
  it("goes under its trigger when it fits", () => {
    const p = placeInfoPopover({ top: 100, bottom: 128, left: 600, right: 628 }, 400, 520, viewport, "end");
    expect(p.top).toBe(134);
    expect(p.left).toBe(108);
    expect(p.top + Math.min(400, p.maxHeight)).toBeLessThanOrEqual(viewport.height);
  });

  it("flips above a trigger near the bottom, inside the viewport", () => {
    const p = placeInfoPopover({ top: 505, bottom: 533, left: 600, right: 628 }, 407, 520, viewport, "end");
    expect(p.top).toBeGreaterThanOrEqual(8);
    expect(p.top + 407).toBeLessThanOrEqual(505);
  });

  it("caps its height to the room it has and keeps inside the width", () => {
    const p = placeInfoPopover({ top: 300, bottom: 328, left: 4, right: 32 }, 2000, 520, { width: 400, height: 600 }, "start");
    expect(p.width).toBe(384);
    expect(p.left).toBe(8);
    expect(p.maxHeight).toBeLessThanOrEqual(600);
  });
});

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src") + sep;
function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = join(dir, e.name);
    if (e.isDirectory()) return e.name === "node_modules" ? [] : sources(full);
    return /\.tsx$/.test(e.name) && !/\.test\.tsx$/.test(e.name) ? [full] : [];
  });
}

describe("one info popover (B6)", () => {
  it("no other component portals a role=dialog info popover of its own", () => {
    const offenders = sources(SRC)
      .filter((f) => !f.endsWith(`components${sep}ui${sep}info-popover.tsx`))
      .filter((f) => {
        const src = readFileSync(f, "utf8");
        return /createPortal\(/.test(src) && /aria-haspopup="dialog"/.test(src) && /\bInfo\b/.test(src);
      })
      .map((f) => f.slice(SRC.length).split(sep).join("/"));
    expect(offenders, "build the info bubble on components/ui/info-popover.tsx").toEqual([]);
  });
});
