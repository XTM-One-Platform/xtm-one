import { describe, expect, it } from "vitest";
import type { AgentConnectedFleet, AgentHomeFleet } from "@/hooks/api/useFleetApi";
import { BackendUnavailableError } from "@/lib/api";
import { agentAutomations, contextLabel, fleetAboutPath, fleetPath, hidesFleetCard, homeChips, isHomeHidden, isInPlayground, offersAddAutomation, reportsTo } from "./agent-fleet-card";
import type { StatusAssignment } from "../assignment-detail/assignment-status";

const automation = (over: Partial<StatusAssignment> = {}): StatusAssignment => ({
  enabled: true,
  paused_by_owner_deactivation: false,
  paused_reason: null,
  trigger_type: "schedule",
  trigger_config: {},
  ...over,
} as StatusAssignment);

const home = (over: Partial<AgentHomeFleet> = {}): AgentHomeFleet => ({
  fleet_id: "f1",
  fleet_name: "CEO Office",
  member_id: "m1",
  parent_member_id: null,
  parent_name: "",
  role_label: "",
  is_orchestrator: false,
  has_orchestrator: true,
  is_exposed: false,
  is_archived: false,
  can_manage: true,
  ...over,
});

const link = (over: Partial<AgentConnectedFleet> = {}): AgentConnectedFleet => ({
  fleet_id: "f2",
  fleet_name: "Design",
  member_id: "m2",
  collaboration_instruction: "",
  context_inclusion: "none",
  is_archived: false,
  can_manage: false,
  ...over,
});

describe("agent fleet card", () => {
  it("links to the fleet page", () => {
    expect(fleetPath("abc")).toBe("/fleets/abc");
  });

  it("links to the fleet's About page", () => {
    expect(fleetAboutPath("abc")).toBe("/fleets/abc");
  });

  it("counts the agent's automations that still start runs", () => {
    expect(agentAutomations(undefined)).toBeNull();
    expect(agentAutomations([])).toEqual({ on: 0, total: 0 });
    expect(agentAutomations([automation(), automation({ enabled: false }), automation()])).toEqual({ on: 2, total: 3 });
  });

  it("offers Add Assignment only when the agent has no automation and the reader can add one", () => {
    // The empty branch: "No automation yet" plus the action that sets one up on this page (UX round 1 B1).
    expect(offersAddAutomation(agentAutomations([]), true)).toBe(true);
    expect(offersAddAutomation(agentAutomations([]), false)).toBe(false);
    expect(offersAddAutomation(agentAutomations(undefined), true)).toBe(false);
    expect(offersAddAutomation(agentAutomations([automation({ enabled: false })]), true)).toBe(false);
  });

  it("says who the agent reports to", () => {
    expect(reportsTo(home({ is_orchestrator: true }))).toEqual({ key: "Leads the fleet" });
    expect(reportsTo(home())).toEqual({ key: "Reports to the orchestrator" });
    expect(reportsTo(home({ parent_member_id: "p", parent_name: "EA" }))).toEqual({ key: "Reports to {name}", name: "EA" });
    expect(reportsTo(home({ parent_member_id: "p" }))).toEqual({ key: "Reports to a member you cannot see" });
  });

  it("never says a top-level member of a headless fleet reports to an orchestrator", () => {
    expect(reportsTo(home({ has_orchestrator: false }))).toEqual({
      key: "At the top level: the fleet has no orchestrator",
    });
  });

  it("hides the card on the licence refusal only", () => {
    expect(hidesFleetCard({ status: 403 })).toBe(true);
    expect(hidesFleetCard({ status: 500 })).toBe(false);
    expect(hidesFleetCard({ status: 502 })).toBe(false);
    expect(hidesFleetCard(new TypeError("Failed to fetch"))).toBe(false);
  });

  it("hides the card on the Copilot licence wall, which the API client raises without a status", () => {
    expect(
      hidesFleetCard(
        new BackendUnavailableError(
          "Enterprise license required. Configure ENTERPRISE_LICENSE or paste the license PEM in Settings.",
        ),
      ),
    ).toBe(true);
    expect(hidesFleetCard(new BackendUnavailableError("Server unavailable (HTTP 502)"))).toBe(false);
    expect(hidesFleetCard(new BackendUnavailableError("Server unavailable (HTTP 503)"))).toBe(false);
  });

  it("lists the chips in order", () => {
    expect(homeChips(home())).toEqual([]);
    expect(homeChips(home({ is_orchestrator: true, is_exposed: true, is_archived: true }))).toEqual([
      "Orchestrator",
      "Reachable from other fleets",
      "Archived",
    ]);
  });

  it("names the context with the members panel's words", () => {
    expect(contextLabel(link())).toBe("Nothing");
    expect(contextLabel(link({ context_inclusion: "summary" }))).toBe("A summary");
    expect(contextLabel(link({ context_inclusion: "full" }))).toBe("The full conversation");
  });

  it("is in the playground with no home, hidden or not, and no connecting fleet", () => {
    expect(isInPlayground(undefined)).toBe(true);
    expect(isInPlayground({ agent_id: "a", home: null, home_hidden: false, connected_to: [] })).toBe(true);
    expect(isInPlayground({ agent_id: "a", home: home(), home_hidden: false, connected_to: [] })).toBe(false);
    expect(isInPlayground({ agent_id: "a", home: null, home_hidden: false, connected_to: [link()] })).toBe(false);
    expect(isInPlayground({ agent_id: "a", home: null, home_hidden: true, connected_to: [] })).toBe(false);
  });

  it("says a home fleet the caller cannot see, without naming it", () => {
    expect(isHomeHidden(undefined)).toBe(false);
    expect(isHomeHidden({ agent_id: "a", home: null, home_hidden: true, connected_to: [] })).toBe(true);
    expect(isHomeHidden({ agent_id: "a", home: null, home_hidden: false, connected_to: [] })).toBe(false);
  });
});
