// "How it works" on the fleet About page (#10327): each step shows its
// markdown body under the title, sanitized (no raw HTML runs, no heading); a
// legacy step without a body reads its title only, with no empty row; past
// five steps the list folds behind a keyboard-reachable "Show all N steps";
// each trigger chip explains, from the configuration, what starts it.
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { FleetDocStructured } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetAboutSteps } from "./FleetAboutSteps";
import { STEPS_FOLD, triggerKindSummaries, visibleSteps } from "./fleet-about-steps";

const steps = (n: number) => Array.from({ length: n }, (_, i) => ({ title: `Step ${i + 1}`, detail: `Body **${i + 1}**.` }));

describe("FleetAboutSteps", () => {
  it("renders the step body as markdown under its title", () => {
    renderWithProviders(
      <FleetAboutSteps structured={{ steps: [{ title: "Work enters", detail: "**Assistant** reads it.\n\n- files an item\n- posts to Teams" }] }} />,
    );
    const body = screen.getByTestId("about-step-body");
    expect(body.querySelector("strong")?.textContent).toBe("Assistant");
    expect(body.querySelectorAll("li")).toHaveLength(2);
    expect(screen.getByText("Work enters")).toBeInTheDocument();
  });

  it("never runs raw HTML nor draws a heading from a step body", () => {
    const structured: FleetDocStructured = { steps: [{ title: "Unsafe", detail: "# Big\n\n<script>window.__pwned = 1</script><img src=x onerror=alert(1)> plain [x](javascript:alert(1))" }] };
    renderWithProviders(<FleetAboutSteps structured={structured} />);
    const body = screen.getByTestId("about-step-body");
    expect(body.querySelector("script")).toBeNull();
    expect(body.querySelector("img")).toBeNull();
    expect(body.querySelector("h1")).toBeNull();
    expect(body.querySelector("a")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(body.textContent).toContain("Big");
  });

  it("reads a legacy step without a body as its title only, with no empty body row", () => {
    renderWithProviders(<FleetAboutSteps structured={{ steps: [{ title: "Assistant delegates" }, { title: "  " }, { title: "Desk acts", detail: "  " }] }} />);
    expect(screen.getAllByTestId("about-step")).toHaveLength(2);
    expect(screen.queryByTestId("about-step-body")).toBeNull();
  });

  it("folds after the fourth step with bodies clamped to two lines, and unfolds them whole on press", () => {
    renderWithProviders(<FleetAboutSteps structured={{ steps: steps(8) }} />);
    expect(STEPS_FOLD).toBe(4);
    expect(screen.getAllByTestId("about-step")).toHaveLength(STEPS_FOLD);
    for (const body of screen.getAllByTestId("about-step-body")) expect(body).toHaveAttribute("data-clamped", "true");
    const toggle = screen.getByRole("button", { name: "Show all 8 steps" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(screen.getAllByTestId("about-step")).toHaveLength(8);
    for (const body of screen.getAllByTestId("about-step-body")) expect(body).not.toHaveAttribute("data-clamped");
    expect(screen.getByTestId("about-steps-toggle")).toHaveAttribute("aria-expanded", "true");
  });

  it("tells the page when the steps unfold and fold back, so the diagram card stops following their height", () => {
    const seen: boolean[] = [];
    renderWithProviders(<FleetAboutSteps structured={{ steps: steps(6) }} onExpandedChange={(v) => seen.push(v)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 6 steps" }));
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(seen).toEqual([true, false]);
  });

  it("shows no toggle and clamps no body at four steps or fewer", () => {
    renderWithProviders(<FleetAboutSteps structured={{ steps: steps(4) }} />);
    expect(screen.getAllByTestId("about-step")).toHaveLength(4);
    expect(screen.queryByTestId("about-steps-toggle")).toBeNull();
    for (const body of screen.getAllByTestId("about-step-body")) expect(body).not.toHaveAttribute("data-clamped");
  });

  it("gives each trigger chip a focusable trigger naming what starts it", () => {
    renderWithProviders(
      <FleetAboutSteps structured={{ automations: [{ name: "Inbox pick-up", trigger: "Work item" }, { name: "Nightly sweep", trigger: "Schedule" }] }} />,
    );
    const chips = screen.getAllByTestId("about-trigger-kind");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveAttribute("tabindex", "0");
    expect(chips[0].getAttribute("aria-label")).toBe("Schedule. 1 automation starts this way: Nightly sweep");
  });
});

describe("fleet-about-steps helpers", () => {
  it("summarises each trigger kind with its automations, three named then the rest counted", () => {
    const names = ["A", "B", "C", "D", "E"];
    const [summary] = triggerKindSummaries({ automations: names.map((name) => ({ name, trigger: "Event" })) });
    expect(summary).toEqual({
      kind: "Event",
      count: 5,
      names: [
        { name: "A", count: 1 },
        { name: "B", count: 1 },
        { name: "C", count: 1 },
      ],
      more: 2,
    });
  });

  it("keeps the tooltip numbers true when automations share a name", () => {
    const names = ["Mail", "Mail", "Mail", "Alpha", "Beta", "Zed", "Zed"];
    const [summary] = triggerKindSummaries({ automations: names.map((name) => ({ name, trigger: "Event" })) });
    expect(summary.count).toBe(7);
    expect(summary.names).toEqual([
      { name: "Alpha", count: 1 },
      { name: "Beta", count: 1 },
      { name: "Mail", count: 3 },
    ]);
    expect(summary.more).toBe(2);
    expect(summary.names.reduce((sum, n) => sum + n.count, 0) + summary.more).toBe(summary.count);
  });

  it("names a shared automation name with its count in the chip tooltip", () => {
    renderWithProviders(
      <FleetAboutSteps structured={{ automations: ["Mail", "Mail", "Mail", "Alpha", "Beta", "Zed"].map((name) => ({ name, trigger: "Event" })) }} />,
    );
    expect(screen.getByTestId("about-trigger-kind").getAttribute("aria-label")).toBe("Event. 6 automations start this way: Alpha, Beta, Mail (3) and 1 more");
  });

  it("keeps every step when expanded", () => {
    expect(visibleSteps(steps(7), true)).toMatchObject({ hidden: 0 });
    expect(visibleSteps(steps(7), false).hidden).toBe(3);
    expect(visibleSteps(null, false)).toEqual({ shown: [], hidden: 0 });
  });
});
