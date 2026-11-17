import { describe, expect, it } from "vitest";

import { isKnownMapView, legacyMapLocation, mapViewPath, readMapView } from "./map-view";

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

  it("knows the bare map and the schedule, so an unknown segment is replaced by /flows/map", () => {
    expect(isKnownMapView(undefined)).toBe(true);
    expect(isKnownMapView("schedule")).toBe(true);
    expect(isKnownMapView("nope")).toBe(false);
    expect(isKnownMapView("graph")).toBe(false);
  });

  it("redirects an old ?view= link to its path, the map's state kept", () => {
    expect(legacyMapLocation(new URLSearchParams("vis=all"))).toBeNull();
    expect(legacyMapLocation(new URLSearchParams("view=schedule&vis=all&agent=a1"))).toBe("/flows/map/schedule?vis=all&agent=a1");
    expect(legacyMapLocation(new URLSearchParams("view=fleets"))).toBe("/flows/map");
    expect(legacyMapLocation(new URLSearchParams("view=graph&focus=a1"))).toBe("/flows/map?focus=a1");
  });
});
