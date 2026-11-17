import { describe, expect, it } from "vitest";

import fr from "@/i18n/lang/fr.json";
import { runErrorIsNeutral, runErrorText } from "@/lib/run-error-text";

// A minimal ICU-free stand-in for t_i18n over the fr catalog: enough for one {actor}.
const t_fr = (id: string, values?: Record<string, string | number>) =>
  ((fr as Record<string, string>)[id] ?? id).replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? ""));

describe("runErrorText", () => {
  it("localises the bulk cancel reason of a cancelled run", () => {
    expect(runErrorText({ status: "cancelled", error: "Cancelled by Admin" }, t_fr)).toBe("Annulée par Admin");
  });

  it("shows any other error as written", () => {
    expect(runErrorText({ status: "failed", error: "Cancelled by Admin" }, t_fr)).toBe("Cancelled by Admin");
    expect(runErrorText({ status: "failed", error: "Timeout after 300s" }, t_fr)).toBe("Timeout after 300s");
  });
});

describe("runErrorIsNeutral", () => {
  it("reads a cancelled run and an interrupted one as neutral, a failure as an error", () => {
    expect(runErrorIsNeutral({ status: "cancelled", error: "Cancelled by Admin" })).toBe(true);
    expect(runErrorIsNeutral({ status: "failed", outcome: "interrupted", error: "Worker restarted" })).toBe(true);
    expect(runErrorIsNeutral({ status: "failed", error: "Timeout after 300s" })).toBe(false);
  });
});
