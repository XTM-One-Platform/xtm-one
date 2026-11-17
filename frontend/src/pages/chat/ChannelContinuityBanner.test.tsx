// Where an open conversation also lives: the banner above the transcript.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { ChannelContinuityBanner } from "./ChannelContinuityBanner";

describe("ChannelContinuityBanner", () => {
  it("says nothing for a web conversation continued nowhere else", () => {
    const { container } = renderWithProviders(
      <ChannelContinuityBanner channel="web" webContinuable continuedIn={[]} />,
    );
    expect(container.textContent).toBe("");
  });

  it("names every channel a web conversation is continued in", () => {
    renderWithProviders(<ChannelContinuityBanner channel="web" webContinuable continuedIn={["slack", "teams"]} />);
    expect(screen.getByText("Also continued in Slack, Teams.")).toBeTruthy();
    expect(screen.getByAltText("slack")).toBeTruthy();
    expect(screen.getByAltText("teams")).toBeTruthy();
    expect(screen.queryByText(/Started in/)).toBeNull();
  });

  it("says where a direct message thread started, and not that it is continued in its own channel", () => {
    renderWithProviders(<ChannelContinuityBanner channel="slack" webContinuable continuedIn={["slack"]} />);
    expect(
      screen.getByText(
        "Started in Slack: continue it here or there, each reply is written where you send your message.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/Also continued in/)).toBeNull();
    expect(screen.getAllByAltText("slack")).toHaveLength(1);
  });

  it("says both when a Slack thread is also continued in Teams", () => {
    renderWithProviders(<ChannelContinuityBanner channel="slack" webContinuable continuedIn={["teams"]} />);
    expect(screen.getByText(/^Started in Slack: .* Also continued in Teams\.$/)).toBeTruthy();
    expect(screen.getByAltText("slack")).toBeTruthy();
    expect(screen.getByAltText("teams")).toBeTruthy();
  });

  it("locks a group thread to its channel", () => {
    renderWithProviders(<ChannelContinuityBanner channel="teams" webContinuable={false} continuedIn={["slack"]} />);
    expect(
      screen.getByText("This Teams thread is shared with other people, so it is continued in Teams only."),
    ).toBeTruthy();
    // Nothing else is offered: where a shared thread is continued is not the web's to say.
    expect(screen.queryByText(/Also continued in/)).toBeNull();
    expect(screen.queryByAltText("slack")).toBeNull();
  });
});
