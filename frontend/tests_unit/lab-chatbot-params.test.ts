/**
 * The URL parsing of the lab-only `/lab-chatbot.html` (#11360). The module lives
 * under `.xtm-one/lab/chatbot-host/` and is copied into the lab's frontend copy
 * only; this test reads it in place, so the product build never ships it.
 */
import { describe, expect, it } from "vitest";
import {
  LAB_QUOTA_STATES,
  LAB_REVIEW_PARAMS,
  isHistoryRead,
  isQuotaRead,
  quotaPayload,
  reviewStateFromSearch,
  searchWith,
} from "../../.xtm-one/lab/chatbot-host/lab-chatbot-params";

describe("reviewStateFromSearch", () => {
  it("is all off with no parameter", () => {
    expect(reviewStateFromSearch("?mode=inline")).toEqual({
      historyPending: false,
      welcome: null,
      conversation: null,
      holdHistory: false,
      waiting: null,
      quota: false,
      quotaState: null,
    });
  });

  it("reads every review parameter", () => {
    expect(
      reviewStateFromSearch("?mode=inline&historyPending=1&welcome=placeholder&conversation=c-1&holdHistory=true&waiting=package"),
    ).toEqual({
      historyPending: true,
      welcome: "placeholder",
      conversation: "c-1",
      holdHistory: true,
      waiting: "package",
      quota: false,
      quotaState: null,
    });
    expect(reviewStateFromSearch("?waiting=package-nourl").waiting).toBe("package-nourl");
  });

  it("reads the quota parameters, a state implying the route", () => {
    expect(reviewStateFromSearch("?quota=1")).toMatchObject({ quota: true, quotaState: null });
    expect(reviewStateFromSearch("?quotaState=danger")).toMatchObject({ quota: true, quotaState: "danger" });
    expect(reviewStateFromSearch("?quota=0&quotaState=red")).toMatchObject({ quota: false, quotaState: null });
  });

  it("ignores unknown values and a blank conversation", () => {
    expect(reviewStateFromSearch("?historyPending=yes&welcome=other&conversation=%20&holdHistory=0&waiting=arcade")).toEqual({
      historyPending: false,
      welcome: null,
      conversation: null,
      holdHistory: false,
      waiting: null,
      quota: false,
      quotaState: null,
    });
  });

  it("lists the parameters the lab logs", () => {
    expect(LAB_REVIEW_PARAMS).toEqual([
      "historyPending=1",
      "welcome=placeholder",
      "conversation=ID",
      "holdHistory=1",
      "waiting=package",
      "waiting=package-nourl",
      "quota=1",
      `quotaState=${LAB_QUOTA_STATES.join("|")}`,
    ]);
  });
});

describe("searchWith", () => {
  it("changes one parameter and keeps the others", () => {
    expect(searchWith("?mode=inline&historyPending=1", "mode", "floating")).toBe("?mode=floating&historyPending=1");
  });

  it("removes a parameter set to null", () => {
    expect(searchWith("?mode=inline&conversation=c-1", "conversation", null)).toBe("?mode=inline");
    expect(searchWith("?conversation=c-1", "conversation", null)).toBe("");
  });
});

describe("quotaPayload", () => {
  const now = new Date("2026-11-12T07:40:00Z");

  it("answers in the shape of the route, today first, with the reset instants", () => {
    const payload = quotaPayload("ok", now);
    expect(payload).toMatchObject({ used: 12, limit: 100, period: "daily", scope: "user" });
    expect(payload.periods?.map((p) => [p.period, p.resets_at])).toEqual([
      ["daily", "2026-11-13T00:00:00Z"],
      ["monthly", "2026-12-01T00:00:00Z"],
      ["yearly", "2027-01-01T00:00:00Z"],
    ]);
  });

  it("spends the share of today's limit each state names", () => {
    expect(quotaPayload("warning", now).periods?.[0]).toMatchObject({ used: 85, limit: 100 });
    expect(quotaPayload("danger", now).periods?.[0]).toMatchObject({ used: 100, limit: 100 });
    expect(quotaPayload("unlimited", now).periods?.every((p) => p.limit === null)).toBe(true);
    expect(quotaPayload("global", now).periods?.every((p) => p.scope === "global")).toBe(true);
  });

  it("has no periods for an older backend", () => {
    const legacy = quotaPayload("legacy", now);
    expect(legacy).not.toHaveProperty("periods");
    expect(legacy).toMatchObject({ used: 12, limit: 100, period: "daily" });
  });
});

describe("isQuotaRead", () => {
  it("is a GET of the quota route, query ignored", () => {
    expect(isQuotaRead("http://lab/api/v1/web/chat/quota?x=1", undefined, "/web/chat/quota")).toBe(true);
    expect(isQuotaRead("/api/v1/web/chat/quota", { method: "POST" }, "/web/chat/quota")).toBe(false);
    expect(isQuotaRead("/api/v1/web/chat/sessions", undefined, "/web/chat/quota")).toBe(false);
  });
});

describe("isHistoryRead", () => {
  const sessions = "/web/chat/sessions";
  const post = (body: unknown) => ({ method: "POST", body: JSON.stringify(body) });

  it("is the read-back of a named conversation", () => {
    expect(isHistoryRead("http://lab/api/v1/web/chat/sessions", post({ conversation_id: "c-1" }), sessions)).toBe(true);
  });

  it("ignores the query of the route and of the URL", () => {
    expect(isHistoryRead("/api/v1/web/chat/sessions?x=1", post({ conversation_id: "c-1" }), `${sessions}?studio=1`)).toBe(true);
  });

  it("never holds a new conversation, another route or another method", () => {
    expect(isHistoryRead("/api/v1/web/chat/sessions", post({ agent_slug: "a" }), sessions)).toBe(false);
    expect(isHistoryRead("/api/v1/web/chat/messages", post({ conversation_id: "c-1" }), sessions)).toBe(false);
    expect(isHistoryRead("/api/v1/web/chat/sessions", { method: "GET" }, sessions)).toBe(false);
    expect(isHistoryRead("/api/v1/web/chat/sessions", undefined, sessions)).toBe(false);
    expect(isHistoryRead("/api/v1/web/chat/sessions", { method: "POST", body: "not json" }, sessions)).toBe(false);
  });
});
