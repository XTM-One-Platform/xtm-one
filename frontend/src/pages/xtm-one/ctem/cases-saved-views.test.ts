// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  CASE_SAVED_VIEWS_MAX,
  activeCaseView,
  addCaseSavedView,
  applyCaseView,
  caseViewQuery,
  readCaseSavedViews,
  removeCaseSavedView,
} from "./cases-saved-views";

describe("cases saved views", () => {
  it("keeps the state tab and the table parameters only, in a stable order", () => {
    const params = new URLSearchParams("tab=x&c_group=owner&state=all&c_page=3&c_f_kev=yes");
    expect(caseViewQuery(params)).toBe("c_f_kev=yes&c_group=owner&state=all");
  });

  it("opens a view over the current parameters, other keys kept", () => {
    const next = applyCaseView(new URLSearchParams("tab=x&c_group=owner&c_page=4&state=all"), "c_f_kev=yes&state=remediating");
    expect(next.get("tab")).toBe("x");
    expect(next.get("c_page")).toBeNull();
    expect(next.get("c_group")).toBeNull();
    expect(next.get("c_f_kev")).toBe("yes");
    expect(next.get("state")).toBe("remediating");
  });

  it("reads malformed storage as no view", () => {
    expect(readCaseSavedViews(null)).toEqual([]);
    expect(readCaseSavedViews("{not json")).toEqual([]);
    expect(readCaseSavedViews('{"a":1}')).toEqual([]);
    expect(readCaseSavedViews('[{"id":"1","name":" ","query":""},{"id":"2","name":"Mine","query":"state=all"}]')).toEqual([
      { id: "2", name: "Mine", query: "state=all" },
    ]);
  });

  it("replaces a view saved under the same name, any case", () => {
    const views = addCaseSavedView([], "KEV first", "c_f_kev=yes", "a");
    const again = addCaseSavedView(views, "kev FIRST ", "c_f_kev=no", "b");
    expect(again).toEqual([{ id: "a", name: "kev FIRST", query: "c_f_kev=no" }]);
  });

  it("keeps one view per query: saving the same list again renames its view", () => {
    const views = addCaseSavedView([], "Critical fixes for the infrastructure team", "state=all", "a");
    const again = addCaseSavedView(views, "Open", "state=all", "b");
    expect(again).toEqual([{ id: "a", name: "Open", query: "state=all" }]);
    expect(activeCaseView(again, "state=all")?.name).toBe("Open");
  });

  it("drops another view holding the name when the query already has a view", () => {
    const views = addCaseSavedView(addCaseSavedView([], "A", "state=all", "a"), "B", "state=resolved", "b");
    const again = addCaseSavedView(views, "b", "state=all", "c");
    expect(again).toEqual([{ id: "a", name: "b", query: "state=all" }]);
  });

  it("drops the oldest past the cap and ignores a blank name", () => {
    let views = addCaseSavedView([], "  ", "state=all", "x");
    expect(views).toEqual([]);
    for (let i = 0; i <= CASE_SAVED_VIEWS_MAX; i += 1) views = addCaseSavedView(views, `v${i}`, `state=${i}`, `id${i}`);
    expect(views).toHaveLength(CASE_SAVED_VIEWS_MAX);
    expect(views[0].name).toBe("v1");
  });

  it("removes a view and finds the active one", () => {
    const views = addCaseSavedView(addCaseSavedView([], "A", "state=all", "a"), "B", "c_group=owner&state=all", "b");
    expect(activeCaseView(views, "c_group=owner&state=all")?.id).toBe("b");
    expect(activeCaseView(views, "state=resolved")).toBeUndefined();
    expect(removeCaseSavedView(views, "a").map((v) => v.id)).toEqual(["b"]);
  });
});
