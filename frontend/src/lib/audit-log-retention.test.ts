import { describe, expect, it } from "vitest";

import {
  AUDIT_LOG_RETENTION_DEFAULT_DAYS,
  MAX_AUDIT_LOG_RETENTION_DAYS,
  MIN_AUDIT_LOG_RETENTION_DAYS,
  parseAuditLogRetentionDays,
} from "./audit-log-retention";

describe("parseAuditLogRetentionDays", () => {
  it("goes back to 90 days on an empty field", () => {
    expect(AUDIT_LOG_RETENTION_DEFAULT_DAYS).toBe(90);
    expect(parseAuditLogRetentionDays("")).toBe(90);
    expect(parseAuditLogRetentionDays("  ")).toBe(90);
  });

  it("keeps a whole number within the bounds", () => {
    expect(parseAuditLogRetentionDays("365")).toBe(365);
    expect(parseAuditLogRetentionDays(String(MIN_AUDIT_LOG_RETENTION_DAYS))).toBe(30);
    expect(parseAuditLogRetentionDays(String(MAX_AUDIT_LOG_RETENTION_DAYS))).toBe(3650);
  });

  it("refuses a value out of the bounds instead of clamping it", () => {
    expect(parseAuditLogRetentionDays("29")).toBeNull();
    expect(parseAuditLogRetentionDays("3651")).toBeNull();
    expect(parseAuditLogRetentionDays("-5")).toBeNull();
  });

  it("refuses a value that is not a whole number", () => {
    expect(parseAuditLogRetentionDays("abc")).toBeNull();
    expect(parseAuditLogRetentionDays("90.5")).toBeNull();
  });
});
