/**
 * The audit log labels (UX r1 B2 on #11939): an action or entity type is shown by its
 * catalog label, and a key the table does not know yet is humanised, never raw.
 */
import { describe, expect, it } from "vitest";
import { auditActionLabel, auditEntityLabel } from "@/lib/constants";

const upper = (key: string) => `T:${key}`;

describe("audit log labels", () => {
  it("translates a known action through its catalog key", () => {
    expect(auditActionLabel("approval_gates_change", upper)).toBe("T:Approval gates change");
    expect(auditActionLabel("login", upper)).toBe("T:Sign-in");
  });

  it("translates a known entity type through its catalog key", () => {
    expect(auditEntityLabel("roi_declaration", upper)).toBe("T:ROI declaration");
    expect(auditEntityLabel("group", upper)).toBe("T:User group");
  });

  it("humanises an unknown key instead of showing it raw", () => {
    expect(auditActionLabel("brand_new_action", upper)).toBe("Brand new action");
    expect(auditEntityLabel("some_future_entity", upper)).toBe("Some future entity");
  });
});
