import { describe, expect, it } from "vitest";

import { isKnownMapView, mapViewPath, readMapView } from "./map-view";

describe("map views as path routes (#10203)", () => {
  it("writes the graph as the page with none and the schedule as a segment", () => {
    expect(mapViewPath("graph")).toBe("/flows/map");
    expect(mapViewPath("schedule")).toBe("/flows/map/schedule");
  });

  it("reads a segment, an unknown one being the graph", () => {
    expect(readMapView(undefined)).toBe("graph");
    expect(readMapView("schedule")).toBe("schedule");
    expect(readMapView("nope")).toBe("graph");
  });

  it("knows the bare map and the schedule, so an unknown segment is not found (#797)", () => {
    expect(isKnownMapView(undefined)).toBe(true);
    expect(isKnownMapView("schedule")).toBe(true);
    expect(isKnownMapView("nope")).toBe(false);
    expect(isKnownMapView("graph")).toBe(false);
  });
});
