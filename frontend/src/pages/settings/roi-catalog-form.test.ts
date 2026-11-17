// The units of work and rates forms check what the server checks (#9448).
import { describe, expect, it } from "vitest";

import { amountError, currencyError, defaultRateRole, minutesErrors, rateRoleChoices, rateRoleError, unitKeyError, unitKeyFromName } from "./roi-catalog-form";

describe("defaultRateRole", () => {
  it("starts a new unit on the mode's fallback role when the list offers it (#9550)", () => {
    expect(defaultRateRole(["analyst", "ciso", "legal"])).toBe("analyst");
    expect(defaultRateRole(["executive_assistant", "knowledge_worker", "manager"])).toBe("knowledge_worker");
    expect(defaultRateRole(["legal"])).toBe("legal");
    expect(defaultRateRole([])).toBe("analyst");
  });
});

describe("minutesErrors", () => {
  it("accepts ordered minutes with review under the likely ones", () => {
    expect(minutesErrors({ low: "10", likely: "20", high: "30", review: "5" })).toEqual({});
  });

  it("names each empty or negative field", () => {
    expect(minutesErrors({ low: "", likely: "-1", high: "x", review: "0" })).toEqual({
      low: "Enter a number of minutes.",
      likely: "Minutes cannot be negative.",
      high: "Enter a number of minutes.",
    });
  });

  it("refuses low over likely, likely over high, review not under likely", () => {
    expect(minutesErrors({ low: "30", likely: "20", high: "40", review: "5" })).toEqual({ likely: "At least the low estimate." });
    expect(minutesErrors({ low: "10", likely: "50", high: "40", review: "5" })).toEqual({ high: "At least the likely estimate." });
    expect(minutesErrors({ low: "10", likely: "20", high: "30", review: "20" })).toEqual({ review: "Less than the likely manual minutes." });
  });

  it("reads a decimal comma", () => {
    expect(minutesErrors({ low: "1,5", likely: "2,5", high: "3", review: "0,5" })).toEqual({});
  });
});

describe("keys, roles, currencies and amounts", () => {
  it("derives a unit key from its name", () => {
    expect(unitKeyFromName("Contrat révisé (legal)")).toBe("contrat_revise_legal");
    expect(unitKeyError(unitKeyFromName("Ticket resolved"))).toBeUndefined();
    expect(unitKeyError("x")).toBeDefined();
    expect(unitKeyError("Bad-Key")).toBeDefined();
  });

  it("checks a rate role, a currency and an amount", () => {
    expect(rateRoleError("legal")).toBeUndefined();
    expect(rateRoleError("Legal")).toBeDefined();
    expect(currencyError("EUR")).toBeUndefined();
    expect(currencyError("euro")).toBeDefined();
    expect(amountError("85")).toBeUndefined();
    expect(amountError("0")).toBe("An hourly rate is above zero.");
    expect(amountError("")).toBe("Enter an hourly rate.");
    expect(amountError("100001")).toBe("At most 100,000 per hour.");
  });

  it("offers each rate role once, sorted", () => {
    expect(rateRoleChoices([{ role: "analyst" }, { role: "ciso" }, { role: "analyst" }], [{ role: "legal" }, { role: "analyst" }])).toEqual([
      "analyst",
      "ciso",
      "legal",
    ]);
  });
});
