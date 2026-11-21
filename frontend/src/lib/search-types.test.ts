// SPDX-License-Identifier: Apache-2.0
// Every search and Ctrl K hit opens a page of the router (#797): a moved route
// can never leave a hit on "Page not found". Each href is resolved against the
// `<Route path>` table of App.tsx, the catch-alls excluded.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { SearchHit, SearchType } from "@/hooks/api/useSearchApi";
import { SEARCH_TYPE_VIEW } from "./search-types";

const ROUTES = [...readFileSync(join(process.cwd(), "src/App.tsx"), "utf8").matchAll(/<Route\s+path="([^"]+)"/g)]
  .map((match) => match[1])
  .filter((path) => path !== "*" && !path.endsWith("/*"));

/** `/users-and-groups/users/:id/:tab?` serves `/users-and-groups/users/u-1`: a parameter takes one segment. */
const serves = (route: string, to: string) => {
  const pattern = route
    .split("/")
    .slice(1)
    .map((segment) => (segment.startsWith(":") ? (segment.endsWith("?") ? "(?:/[^/]+)?" : "/[^/]+") : `/${segment}`))
    .join("");
  return new RegExp(`^${pattern}$`).test(to);
};

const routeOf = (href: string) => ROUTES.find((route) => serves(route, href.split("?")[0]));

const HIT = { id: "0b1c2d3e", parent_id: "9f8e7d6c" } as unknown as SearchHit;

describe("search hits", () => {
  it("open a route of App.tsx for every entity type", () => {
    expect(ROUTES.length).toBeGreaterThan(50);
    for (const [type, view] of Object.entries(SEARCH_TYPE_VIEW) as [SearchType, (typeof SEARCH_TYPE_VIEW)[SearchType]][]) {
      const href = view.href(HIT);
      expect(routeOf(href), `${type} hit ${href} has a route`).toBeDefined();
    }
  });

  it("open users and groups under their menu entry", () => {
    expect(SEARCH_TYPE_VIEW.user.href(HIT)).toBe("/users-and-groups/users/0b1c2d3e");
    expect(routeOf(SEARCH_TYPE_VIEW.user.href(HIT))).toBe("/users-and-groups/users/:id/:tab?");
    expect(SEARCH_TYPE_VIEW.group.href(HIT)).toBe("/users-and-groups/groups/0b1c2d3e");
    expect(routeOf(SEARCH_TYPE_VIEW.group.href(HIT))).toBe("/users-and-groups/groups/:id/:tab?");
  });

  it("never resolve the former bare-entry user address", () => {
    expect(routeOf("/users-and-groups/0b1c2d3e")).toBeUndefined();
  });
});
