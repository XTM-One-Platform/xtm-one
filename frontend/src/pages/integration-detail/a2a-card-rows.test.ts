import { describe, expect, it } from "vitest";

import { boundTokenUrl, cardRowState, missingCardCount } from "./a2a-card-rows";

const live = { enabled: true, user_id: "u-1" };
const settled = { isLoading: false, isError: false };

describe("cardRowState (#11786)", () => {
  it("shows the card when the exposure has one", () => {
    expect(cardRowState(live, { exposed: true, card_url: "https://one.example/c" }, settled)).toEqual({
      kind: "card",
      cardUrl: "https://one.example/c",
    });
  });

  it("names why a bound agent has no card", () => {
    const none = { exposed: false };
    expect(cardRowState({ enabled: false, user_id: "u-1" }, none, settled)).toEqual({ kind: "missing", reason: "disabled" });
    expect(cardRowState({ enabled: true, user_id: null }, none, settled)).toEqual({ kind: "missing", reason: "no_owner" });
    expect(cardRowState(live, none, settled)).toEqual({ kind: "missing", reason: "not_exposed" });
  });

  it("never calls a card missing while it loads or when the viewer may not read it", () => {
    expect(cardRowState(live, undefined, { isLoading: true, isError: false })).toEqual({ kind: "loading" });
    expect(cardRowState(live, undefined, { isLoading: false, isError: true })).toEqual({ kind: "hidden" });
  });
});

describe("boundTokenUrl (#12333)", () => {
  it("takes the token URL the backend served with the card, never a host of its own", () => {
    expect(
      boundTokenUrl([
        undefined,
        { exposed: false },
        { exposed: true, card_url: "https://lab.example/c", token_url: "https://lab.example/mcp/oauth/token" },
      ]),
    ).toBe("https://lab.example/mcp/oauth/token");
  });

  it("answers null while no bound agent has a live card", () => {
    expect(boundTokenUrl([])).toBeNull();
    expect(boundTokenUrl([undefined, { exposed: false, token_url: null }])).toBeNull();
  });
});

describe("missingCardCount", () => {
  it("counts only the rows known to have no card", () => {
    expect(
      missingCardCount([
        { kind: "card", cardUrl: "x" },
        { kind: "loading" },
        { kind: "hidden" },
        { kind: "missing", reason: "not_exposed" },
        { kind: "missing", reason: "disabled" },
      ]),
    ).toBe(2);
    expect(missingCardCount([{ kind: "card", cardUrl: "x" }])).toBe(0);
  });
});
