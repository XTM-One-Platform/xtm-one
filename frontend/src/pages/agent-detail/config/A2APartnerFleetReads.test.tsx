// The #2202 opt-in of a live A2A exposure: the switch reflects the exposure,
// a flip is one PUT on the exposure's opt-in route, and the answer lands in
// the shared exposure cache entry. The route is AI-gated, so the switch is
// disabled while AI is off.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import { A2APartnerFleetReads } from "./A2APartnerFleetReads";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", () => ({
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? "AI is disabled on this platform." : null),
}));

const put = vi.mocked(api.put);
const AGENT = "agent-1";

describe("A2APartnerFleetReads", () => {
  beforeEach(() => {
    put.mockReset();
    ai.off = false;
  });

  it("is disabled while AI is off and sends nothing", () => {
    ai.off = true;
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />);
    const toggle = screen.getByRole("switch", { name: "Partners read this agent's fleet" });
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(put).not.toHaveBeenCalled();
  });

  it("shows the opt-in off by default", () => {
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />);
    const toggle = screen.getByRole("switch", { name: "Partners read this agent's fleet" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("shows the opt-in on when the exposure carries it", () => {
    renderWithProviders(
      <A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true, partner_fleet_reads: true }} />,
    );
    const toggle = screen.getByRole("switch", { name: "Partners read this agent's fleet" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("switches it on with one PUT and caches the answer", async () => {
    const answer = { exposed: true, partner_fleet_reads: true };
    put.mockResolvedValue(answer);
    const client = testQueryClient();
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />, client);

    fireEvent.click(screen.getByRole("switch", { name: "Partners read this agent's fleet" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(`/agents/${AGENT}/a2a/partner-fleet-reads`, { enabled: true }),
    );
    await waitFor(() => expect(client.getQueryData(["a2a-exposure", AGENT])).toEqual(answer));
  });

  it("says a failed switch once, in its own toast", async () => {
    const reason = "500 Internal Server Error: database unavailable";
    put.mockRejectedValue(new Error(reason));
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />, {
      client: testQueryClient({ appMutationCache: true }),
    });

    fireEvent.click(screen.getByRole("switch", { name: "Partners read this agent's fleet" }));

    expect(await screen.findAllByText("Could not change the partner fleet reads")).toHaveLength(1);
    expect(screen.getAllByText(reason)).toHaveLength(1);
    expect(screen.queryByText("Error")).toBeNull();
  });

  it("shows the owner reads opt-in off by default, on when the exposure carries it", () => {
    const { unmount } = renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />);
    const name = "Partners read fleets and runs as the owner";
    expect(screen.getByRole("switch", { name }).getAttribute("aria-checked")).toBe("false");
    unmount();
    renderWithProviders(
      <A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true, partner_owner_reads: true }} />,
    );
    expect(screen.getByRole("switch", { name }).getAttribute("aria-checked")).toBe("true");
  });

  it("switches the owner reads opt-in on its own route", async () => {
    put.mockResolvedValue({ exposed: true, partner_owner_reads: true });
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />);

    fireEvent.click(screen.getByRole("switch", { name: "Partners read fleets and runs as the owner" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(`/agents/${AGENT}/a2a/partner-owner-reads`, { enabled: true }),
    );
  });

  it("names the owner reads switch when it fails, never the fleet switch", async () => {
    put.mockRejectedValue(new Error("404 Not Found: Agent not exposed"));
    renderWithProviders(<A2APartnerFleetReads agentId={AGENT} exposure={{ exposed: true }} />, {
      client: testQueryClient({ appMutationCache: true }),
    });

    fireEvent.click(screen.getByRole("switch", { name: "Partners read fleets and runs as the owner" }));

    expect(await screen.findAllByText("Could not change the partner owner reads")).toHaveLength(1);
    expect(screen.queryByText("Could not change the partner fleet reads")).toBeNull();
  });
});
