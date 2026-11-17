import { describe, expect, it } from "vitest";

import { fleetChatCard } from "@/components/fleets/fleet-chat-cards";
import type { ChatMessage, ToolCallTraceEntry } from "@/types";

const PAUSE_SUMMARY =
  "Not applied. Pause all would switch off 3 automation(s) of fleet **CEO Office** (runs in flight continue; " +
  "Resume switches back on exactly these). Show this to the user and call again with confirm=true only once they agree.";
const RAISE_REFUSAL =
  "An agent can only lower a fleet's autonomy level. Raising it is an administrator's choice: " +
  "ask the user to change it on the fleet page. Nothing was saved.";

function msg(role: ChatMessage["role"], trace: ToolCallTraceEntry[] = [], id = "m1"): ChatMessage {
  return { id, role, content: "", tool_call_trace: trace } as unknown as ChatMessage;
}

const call = (name: string, output: string): ToolCallTraceEntry => ({ name, input: "{}", output, success: true });

describe("fleetChatCard", () => {
  it("shows the confirm card with the count of a pause summary", () => {
    expect(fleetChatCard([msg("assistant", [call("set_fleet_automations", PAUSE_SUMMARY)])])).toEqual({
      kind: "confirm",
      messageId: "m1",
      count: 3,
    });
  });

  it("shows the raise refusal card for update_fleet's refusal", () => {
    expect(fleetChatCard([msg("assistant", [call("update_fleet", RAISE_REFUSAL)])])).toEqual({
      kind: "raise_refused",
      messageId: "m1",
    });
  });

  it("shows nothing once the user answered", () => {
    const messages = [msg("assistant", [call("set_fleet_automations", PAUSE_SUMMARY)]), msg("user", [], "m2")];
    expect(fleetChatCard(messages)).toBeNull();
  });

  it("shows nothing when the pause was confirmed later in the same turn", () => {
    const trace = [call("set_fleet_automations", PAUSE_SUMMARY), call("set_fleet_automations", "Paused 3 automation(s).")];
    expect(fleetChatCard([msg("assistant", trace)])).toBeNull();
  });

  it("ignores other tools and an empty transcript", () => {
    expect(fleetChatCard([])).toBeNull();
    expect(fleetChatCard([msg("assistant", [call("get_fleet", PAUSE_SUMMARY)])])).toBeNull();
    expect(fleetChatCard([msg("assistant", [call("update_fleet", "Updated fleet.")])])).toBeNull();
  });
});
