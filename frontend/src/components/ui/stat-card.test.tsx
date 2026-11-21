// A clickable StatCard is a button for the keyboard and assistive technology:
// Tab reaches it, Enter and Space
// activate it. Without `onClick` it stays inert.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Inbox } from "lucide-react";

import { renderWithProviders } from "@/testing/providers";
import { StatCard } from "./stat-card";

describe("StatCard", () => {
  it("is a button reached by Tab and activated by Enter and Space when clickable", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    renderWithProviders(<StatCard icon={Inbox} label="Heard" value={12} subtitle="for the agent" onClick={onClick} />);

    const card = screen.getByRole("button", { name: /Heard/ });
    expect(card.textContent).toContain("12");

    await user.tab();
    expect(card).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalledTimes(1);
    await user.keyboard(" ");
    expect(onClick).toHaveBeenCalledTimes(2);

    await user.click(card);
    expect(onClick).toHaveBeenCalledTimes(3);
  });

  it("has no button role and is not focusable without onClick", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatCard icon={Inbox} label="Heard" value={12} subtitle="for the agent" />);

    expect(screen.queryByRole("button")).toBeNull();
    await user.tab();
    expect(document.body).toHaveFocus();
  });

  it("draws its footer only when one is passed, so no card carries an empty divider (UX B1 on #965)", () => {
    renderWithProviders(
      <>
        <StatCard icon={Inbox} label="Bare" value={1} subtitle="7 days" />
        <StatCard icon={Inbox} label="With" value={2} subtitle="7 days" footer={<p>Outcome</p>} />
      </>,
    );
    const footers = screen.getAllByTestId("stat-card-footer");
    expect(footers).toHaveLength(1);
    expect(footers[0].textContent).toBe("Outcome");
  });

  it("keeps the footer in flow under the subtitle, so the dividers of one grid row share one y (UX B3 on #965)", () => {
    renderWithProviders(
      <>
        <StatCard icon={Inbox} label="Long" value={1} subtitle="7 days" footer={<p>A two line outcome</p>} />
        <StatCard icon={Inbox} label="Short" value={2} subtitle="7 days" footer={<p>Refused</p>} />
      </>,
    );
    // happy-dom computes no layout: pin the structure that gives the shared divider line
    // (the footer follows the value block, in flow).
    const wrappers = screen.getAllByTestId("stat-card-footer-wrapper");
    expect(wrappers).toHaveLength(2);
    for (const wrapper of wrappers) {
      expect(wrapper.previousElementSibling?.querySelector('[data-testid="stat-card-value"]')).not.toBeNull();
    }
  });

  it("shows the exact figure on hover, never the compact text again (UX B4 on #10529)", () => {
    renderWithProviders(
      <>
        <StatCard icon={Inbox} label="Runs" value={25_912} subtitle="7 days" />
        <StatCard icon={Inbox} label="Cost" value="$5.5K" exact="$5,512.30" subtitle="7 days" />
        <StatCard icon={Inbox} label="Small" value={12} subtitle="7 days" />
      </>,
    );
    const [runs, cost, small] = screen.getAllByTestId("stat-card-value");
    expect(runs.textContent).toBe("25.9K");
    expect(runs.getAttribute("title")).toBe("25,912");
    expect(cost.getAttribute("title")).toBe("$5,512.30");
    expect(small.getAttribute("title")).toBeNull();
  });
});
