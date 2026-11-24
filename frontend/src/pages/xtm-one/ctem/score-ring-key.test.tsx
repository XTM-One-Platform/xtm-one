// The hero score key and top risk tile of the CTEM command center (S2b of
// #11547, design note sections 2 SP1 and 4 row `hero`): the key reads the
// gauge's two rings without uppercase label words, never makes up a
// qualitative score or a trend, and the top risk tile opens Prioritization or
// asks to connect OpenCTI.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { ScoreRingKey, signedDelta } from "./ctem-viz";
import { TopRiskTile } from "./TopRiskTile";

const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: auth.isAdmin }) }));

function key(props: Partial<Parameters<typeof ScoreRingKey>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <ScoreRingKey
        quantitative={{ score: 62, band: "good", delta: 4 }}
        qualitative={{ score: 48, band: "fair", delta: null }}
        {...props}
      />
    </MemoryRouter>,
  );
}

describe("signedDelta", () => {
  it("signs a rise, keeps the minus of a fall and writes 0 bare", () => {
    expect(signedDelta(4)).toBe("+4");
    expect(signedDelta(-2)).toBe("-2");
    expect(signedDelta(0)).toBe("0");
  });
});

describe("ScoreRingKey", () => {
  it("shows each score with its band and the period delta, no label word printed", () => {
    key();
    expect(screen.getByTestId("ctem-score-quantitative-score").textContent).toBe("62");
    expect(screen.getByTestId("ctem-score-quantitative-delta").textContent).toBe("+4");
    expect(screen.getByTestId("ctem-score-qualitative-score").textContent).toBe("48");
    expect(screen.queryByTestId("ctem-score-qualitative-delta")).toBeNull();
    expect(screen.getByText("Good")).toBeTruthy();
    expect(screen.getByText("Fair")).toBeTruthy();
    // The words are the definition triggers' names, never text beside the number.
    expect(screen.queryByText("Quantitative")).toBeNull();
    expect(screen.queryByText("Qualitative")).toBeNull();
    expect(screen.getByRole("button", { name: "Quantitative: definition" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Qualitative: definition" })).toBeTruthy();
  });

  it("asks an administrator for the organization context instead of a qualitative number", () => {
    auth.isAdmin = true;
    key({ qualitative: { score: null, band: "unknown" }, contextMissing: true });
    expect(screen.queryByTestId("ctem-score-qualitative-score")).toBeNull();
    expect(screen.getByRole("link", { name: "Set up organization context" }).getAttribute("href")).toBe("/settings/organization?setup=1");
  });

  it("tells a reader who cannot set it up that there is no organization context, with no link", () => {
    auth.isAdmin = false;
    key({ qualitative: { score: null, band: "unknown" }, contextMissing: true });
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("No organization context")).toBeTruthy();
    auth.isAdmin = true;
  });
});

describe("TopRiskTile", () => {
  it("shows the count with its change over the recorded days and opens Prioritization", () => {
    const onOpen = vi.fn();
    renderWithProviders(<TopRiskTile count={7} history={[10, 9, 7]} tailored={false} onOpen={onOpen} onConnect={() => undefined} />);
    expect(screen.getByTestId("ctem-top-risk-count").textContent).toBe("7");
    expect(screen.getByTestId("ctem-top-risk-delta").textContent).toBe("-3");
    expect(screen.getByTestId("ctem-top-risk-trend")).toBeTruthy();
    expect(screen.getByText("Known-exploited exposures")).toBeTruthy();
    fireEvent.click(screen.getByTestId("ctem-top-risk"));
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("draws no trend and no delta from a single recorded day", () => {
    renderWithProviders(<TopRiskTile count={3} history={[null, 3]} tailored onOpen={() => undefined} onConnect={() => undefined} />);
    expect(screen.queryByTestId("ctem-top-risk-trend")).toBeNull();
    expect(screen.queryByTestId("ctem-top-risk-delta")).toBeNull();
    expect(screen.getByText("Trend after 2 days of history")).toBeTruthy();
    expect(screen.getByText("Known-exploited in your software")).toBeTruthy();
  });

  it("asks to connect OpenCTI and shows no number when it is not connected", () => {
    const onConnect = vi.fn();
    renderWithProviders(<TopRiskTile count={null} history={[]} tailored={false} onOpen={() => undefined} onConnect={onConnect} />);
    expect(screen.getByTestId("ctem-top-risk").getAttribute("data-state")).toBe("not-configured");
    expect(screen.queryByTestId("ctem-top-risk-count")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledOnce();
  });
});
