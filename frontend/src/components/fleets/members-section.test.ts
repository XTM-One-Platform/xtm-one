// The Members section of a fleet's Members view (#1387): one fold for the fleet
// defaults, the toolbar, the selection bar and the table, folded by default; a
// link carrying a members search, filter or sort opens it, so deep links keep working.
import { describe, expect, it } from "vitest";

import type { FleetMember } from "@/hooks/api/useFleetApi";

import { FLEET_DEFAULTS_OPEN_BY_DEFAULT } from "./capability-slots-table";
import { membersCounts, readMembersSectionOpen, writeMembersSectionOpen } from "./members-view";

const url = (query: string) => new URLSearchParams(query);

function member(id: string, role: FleetMember["role"], is_orchestrator = false): FleetMember {
  return { id, name: id, role, is_orchestrator } as unknown as FleetMember;
}

describe("Members section fold (#1387)", () => {
  it("opens folded by default", () => {
    expect(FLEET_DEFAULTS_OPEN_BY_DEFAULT).toBe(false);
    expect(readMembersSectionOpen(url(""), false)).toBe(false);
    expect(readMembersSectionOpen(url("tab=members"), false)).toBe(false);
  });

  it("opens on a deep link carrying a members search, filter or sort", () => {
    for (const query of ["mq=soc", "mrole=connected", "mstate=overridden", "msort=role", "msort=member&mdir=desc"])
      expect(readMembersSectionOpen(url(query), false), query).toBe(true);
  });

  it("reads mopen=1 open and mopen=0 folded, over a table state too", () => {
    expect(readMembersSectionOpen(url("mopen=1"), false)).toBe(true);
    expect(readMembersSectionOpen(url("mq=soc&mopen=0"), false)).toBe(false);
  });

  it("writes Show as mopen=1 and Hide back to a clean URL, other parameters kept", () => {
    const shown = writeMembersSectionOpen(url("tab=members"), true, false);
    expect(shown.get("mopen")).toBe("1");
    expect(shown.get("tab")).toBe("members");
    const hidden = writeMembersSectionOpen(shown, false, false);
    expect(hidden.has("mopen")).toBe(false);
    expect(readMembersSectionOpen(hidden, false)).toBe(false);
  });

  it("writes Hide as mopen=0 when a table state in the link would reopen it", () => {
    const hidden = writeMembersSectionOpen(url("mq=soc&mopen=1"), false, false);
    expect(hidden.get("mopen")).toBe("0");
    expect(hidden.get("mq")).toBe("soc");
    expect(readMembersSectionOpen(hidden, false)).toBe(false);
  });

  it("counts home members (orchestrator included) and connected ones for the folded bar", () => {
    const members = [member("o", "member", true), member("a", "member"), member("c1", "connected"), member("c2", "connected"), member("c3", "connected")];
    expect(membersCounts(members)).toEqual({ home: 2, connected: 3 });
    expect(membersCounts([])).toEqual({ home: 0, connected: 0 });
  });
});
