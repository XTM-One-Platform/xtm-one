import { describe, expect, it } from "vitest";

import { BackendUnavailableError, notFoundIsOrdinary, unavailableIsOrdinary } from "@/lib/api";
import { OUTAGE_QUERY_RETRIES, retryQuery, throwQueryError } from "@/lib/query-outage-policy";

const down = () => new BackendUnavailableError("Failed to fetch");
const onScreen = { state: { data: { id: "c-1" } } };
const nothingYet = { state: { data: undefined } };

describe("query outage policy (#10910)", () => {
  it("reads again for about two minutes while the backend is unreachable, any other error once", () => {
    expect(retryQuery(OUTAGE_QUERY_RETRIES - 1, down())).toBe(true);
    expect(retryQuery(OUTAGE_QUERY_RETRIES, down())).toBe(false);
    expect(retryQuery(0, new Error("boom"))).toBe(true);
    expect(retryQuery(1, new Error("boom"))).toBe(false);
  });

  it("keeps a page whose data is on screen during an outage instead of throwing to the boundary", () => {
    expect(throwQueryError(down(), onScreen)).toBe(false);
    expect(throwQueryError(new TypeError("Failed to fetch"), onScreen)).toBe(false);
  });

  it("still throws an outage met before any data, and every other error", () => {
    expect(throwQueryError(down(), nothingYet)).toBe(true);
    expect(throwQueryError(Object.assign(new Error("Server error"), { status: 500 }), onScreen)).toBe(true);
  });

  it("applies the same rule to the ordinary-404 and ordinary-403 options", () => {
    expect(notFoundIsOrdinary.throwOnError(down(), onScreen)).toBe(false);
    expect(notFoundIsOrdinary.throwOnError(down(), nothingYet)).toBe(true);
    expect(notFoundIsOrdinary.throwOnError(down())).toBe(true);
    expect(unavailableIsOrdinary.throwOnError(down(), onScreen)).toBe(false);
    expect(unavailableIsOrdinary.throwOnError(Object.assign(new Error("nope"), { status: 403 }))).toBe(false);
  });
});
