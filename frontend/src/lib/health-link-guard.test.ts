/**
 * Link guard of the Platform Health UI (#101 S13) - `yarn test:unit` (node:test).
 *
 * Visible <=> openable: every in-app link a Platform Health surface draws opens a
 * page its reader may open. `/health` and `/settings` are `AdminRoute`s, so a
 * surface that links there is drawn for administrators only, either because it
 * gates itself on `isAdmin` or because only an administrator page renders it.
 * A surface open to everyone (the agent row, the flow panel, the profile opt-out)
 * links only to pages with no route guard (the target page checks access itself).
 *
 * `SURFACES` is written by hand: one line per Health file, with its audience and
 * why. A new Health file fails here until it is classified; a link added to an
 * administrator page from a surface open to everyone fails with the file and line.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import { healthTargetHref } from "./health.ts";

const SRC = new URL("../", import.meta.url);
const cache = new Map<string, string>();
const read = (rel: string): string => {
  let text = cache.get(rel);
  if (text === undefined) {
    text = readFileSync(new URL(rel, SRC), "utf8");
    cache.set(rel, text);
  }
  return text;
};

type Audience =
  /** Gates itself: its exported component returns null for a non-administrator. */
  | { kind: "admin-self" }
  /** Rendered by `via` (and by nothing open to everyone): every file that imports it is an administrator surface. */
  | { kind: "admin-via"; via: string }
  /** The page of an `AdminRoute` of App.tsx. */
  | { kind: "admin-route"; route: string }
  /** Drawn for whoever opens the page that renders it. */
  | { kind: "any" };

const SURFACES: Readonly<Record<string, Audience>> = {
  "components/health/CycleDigestView.tsx": { kind: "any" },
  "components/health/HealthCompanion.tsx": { kind: "admin-self" },
  "components/health/HealthConsentNotice.tsx": { kind: "any" },
  "components/health/HealthCyclesList.tsx": { kind: "any" },
  "components/health/HealthImprovementRow.tsx": { kind: "any" },
  "components/health/HealthOptInCard.tsx": { kind: "any" },
  "components/health/HealthOverviewTab.tsx": { kind: "admin-via", via: "pages/Health.tsx" },
  "components/health/HealthSignalsList.tsx": { kind: "admin-via", via: "pages/Health.tsx" },
  "components/health/ImproveWithHealthButton.tsx": { kind: "any" },
  "components/health/ImprovementEvidence.tsx": { kind: "any" },
  "components/health/ImprovementProposalView.tsx": { kind: "any" },
  "components/health/ImprovementsInbox.tsx": { kind: "any" },
  "components/health/ModuleAnalysisSummary.tsx": { kind: "admin-via", via: "components/health/HealthOverviewTab.tsx" },
  "components/health/ModuleAnalystParts.tsx": { kind: "admin-via", via: "components/health/HealthOverviewTab.tsx" },
  "components/health/ModuleCheckReport.tsx": { kind: "admin-via", via: "components/health/HealthOverviewTab.tsx" },
  "components/health/PetSprite.tsx": { kind: "any" },
  "pages/Health.tsx": { kind: "admin-route", route: "/health" },
  "pages/flow-builder/HealthPanel.tsx": { kind: "any" },
  "pages/profile/HealthCompanionCard.tsx": { kind: "admin-self" },
  "pages/profile/HealthOptOutRow.tsx": { kind: "any" },
  "pages/settings/HealthAlertEmailsBlock.tsx": { kind: "admin-via", via: "pages/settings/HealthSection.tsx" },
  "pages/settings/HealthModuleAnalystBlock.tsx": { kind: "admin-via", via: "pages/settings/HealthSection.tsx" },
  "pages/settings/HealthModuleThresholdsBlock.tsx": { kind: "admin-via", via: "pages/settings/HealthSection.tsx" },
  "pages/settings/HealthPetLibrary.tsx": { kind: "admin-via", via: "pages/settings/HealthSection.tsx" },
  /* The run-as picker: also rendered by the CTEM setup dialog; it draws no link, so it is held to the stricter rule. */
  "pages/settings/HealthRunAsBlock.tsx": { kind: "any" },
  "pages/settings/HealthSection.tsx": { kind: "admin-via", via: "pages/Settings.tsx" },
  "pages/settings/HealthTriggerBlocks.tsx": { kind: "admin-via", via: "pages/settings/HealthSection.tsx" },
  "pages/Settings.tsx": { kind: "admin-route", route: "/settings" },
  /* Not a Health file: its critical findings notice links to /health (#101 S8b). */
  "pages/xtm-one/DashboardNotices.tsx": { kind: "admin-self" },
};

/** Links whose target is computed, each with where it comes from. */
const DYNAMIC: Readonly<Record<string, string>> = {
  "components/health/HealthCyclesList.tsx": "healthTargetHref: the target's own page (tested below)",
  "components/health/ImprovementsInbox.tsx": "healthTargetHref: the target's own page (tested below)",
  "components/health/ImprovementEvidence.tsx": "the evidence links the server resolves for this reader (hidden ones carry no href)",
  "pages/xtm-one/DashboardNotices.tsx": "its sharing notice opens /integrations[/<id>], a page with no route guard; not a Health link",
};

/** Every non-test Health source: the components folder and the Health* pages. */
function healthFiles(): string[] {
  const out: string[] = [];
  const walk = (rel: string) => {
    for (const entry of readdirSync(new URL(rel, SRC), { withFileTypes: true })) {
      const child = `${rel}${entry.name}`;
      if (entry.isDirectory()) walk(`${child}/`);
      else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
        if (child.startsWith("components/health/") || /(^|\/)Health[A-Za-z]*\.tsx$/.test(child)) out.push(child);
      }
    }
  };
  walk("components/health/");
  walk("pages/");
  return out.sort();
}

/** Each `<Route ...>` of App.tsx with its path and guard (as nav-reachability.test.ts reads them). */
const ROUTES = read("App.tsx")
  .split("<Route ")
  .slice(1)
  .map((body) => ({ path: /^path="([^"]+)"/.exec(body)?.[1] ?? "", admin: /^[^\n]*<AdminRoute>/.test(body) }))
  .filter((route) => route.path !== "");

/** The route serving a path (query and hash left out); exact or trailing optional segments first. */
function routeOf(to: string) {
  const path = to.replace(/[?#].*$/, "");
  const pattern = (route: string) =>
    new RegExp(
      `^${route
        .split("/")
        .slice(1)
        .map((s) => (s === "*" ? "(?:/.*)?" : s.startsWith(":") ? (s.endsWith("?") ? "(?:/[^/]+)?" : "/[^/]+") : `/${s}`))
        .join("")}$`,
    );
  return ROUTES.find((route) => route.path === path) ?? ROUTES.find((route) => pattern(route.path).test(path));
}

const HEALTH_SETTINGS = /HEALTH_SETTINGS_PATH = "([^"]+)"/.exec(read("pages/settings/settings-routes.ts"))?.[1] ?? "";
const CONSTANTS: Readonly<Record<string, string>> = {
  HEALTH_SETTINGS_PATH: HEALTH_SETTINGS,
  /* ModuleCheckReport: `${HEALTH_SETTINGS_PATH}#<run-as anchor>`; routeOf drops the hash. */
  HEALTH_RUN_AS_SETTINGS_PATH: HEALTH_SETTINGS,
};

interface Found {
  line: number;
  to: string | null;
  raw: string;
}

/** The in-app links of a file: `to=` of a Link or a Navigate, and `navigate(...)`. */
function linksOf(source: string): Found[] {
  const out: Found[] = [];
  source.split("\n").forEach((text, index) => {
    const line = index + 1;
    for (const m of text.matchAll(/\bto=(?:"([^"]+)"|\{"([^"]+)"\}|\{([A-Z_]+)\}|\{([^}]+)\})/g)) {
      const literal = m[1] ?? m[2] ?? (m[3] ? CONSTANTS[m[3]] : undefined);
      out.push({ line, to: literal ?? null, raw: m[0] });
    }
    for (const m of text.matchAll(/\bnavigate\(\s*(?:"([^"]+)"|([^)]+))\)/g)) {
      out.push({ line, to: m[1] ?? null, raw: m[0] });
    }
  });
  return out;
}

const isAdminAudience = (rel: string): boolean => {
  const audience = SURFACES[rel];
  return audience !== undefined && audience.kind !== "any";
};

test("every Health source file is classified", () => {
  const missing = healthFiles().filter((rel) => !(rel in SURFACES));
  assert.deepEqual(missing, [], "add each new Health file to SURFACES with its audience");
  for (const rel of Object.keys(SURFACES)) assert.ok(read(rel).length > 0, `${rel} exists`);
});

test("an administrator surface is really drawn for administrators only", () => {
  const sources = (() => {
    const all: string[] = [];
    const walk = (rel: string) => {
      for (const entry of readdirSync(new URL(rel, SRC), { withFileTypes: true })) {
        const child = `${rel}${entry.name}`;
        if (entry.isDirectory()) {
          if (entry.name !== "node_modules") walk(`${child}/`);
        } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) all.push(child);
      }
    };
    walk("");
    return all;
  })();
  for (const [rel, audience] of Object.entries(SURFACES)) {
    if (audience.kind === "admin-self") {
      assert.match(read(rel), /if \(!isAdmin\) return null;/, `${rel} gates itself on isAdmin`);
    } else if (audience.kind === "admin-route") {
      const route = ROUTES.find((r) => r.path.replace(/(\/:[A-Za-z]+\?)+$/, "") === audience.route);
      assert.ok(route?.admin, `${audience.route} is an AdminRoute in App.tsx`);
    } else if (audience.kind === "admin-via") {
      assert.ok(isAdminAudience(audience.via), `${rel}: its parent ${audience.via} is an administrator surface`);
      const module = rel.replace(/^.*\//, "").replace(/\.tsx?$/, "");
      const importers = sources.filter((file) => file !== rel && new RegExp(`from "[^"]*/${module}"`).test(read(file)));
      assert.ok(importers.includes(audience.via), `${rel} is rendered by ${audience.via}`);
      const open = importers.filter((file) => !isAdminAudience(file));
      assert.deepEqual(open, [], `${rel} is imported by administrator surfaces only`);
    }
  }
});

test("a surface open to everyone never links to an administrator page; every literal link has a route", () => {
  const problems: string[] = [];
  for (const rel of Object.keys(SURFACES)) {
    if (rel === "pages/Settings.tsx") continue; // the Settings shell: its own links are not Health's
    for (const link of linksOf(read(rel))) {
      if (link.to === null) {
        if (!(rel in DYNAMIC)) problems.push(`${rel}:${link.line} computed link ${link.raw} with no reason in DYNAMIC`);
        continue;
      }
      if (link.to.startsWith("/health") && /[?&]tab=/.test(link.to)) {
        problems.push(`${rel}:${link.line} ${link.to}: the Health page reads its tab from the path, never ?tab=`);
      }
      const route = routeOf(link.to);
      if (!route) {
        problems.push(`${rel}:${link.line} ${link.to} has no route in App.tsx`);
      } else if (route.admin && !isAdminAudience(rel)) {
        problems.push(`${rel}:${link.line} ${link.to} is an AdminRoute, linked from a surface open to everyone`);
      }
    }
  }
  assert.deepEqual(problems, []);
});

test("the critical findings notice gates on isAdmin in the function that links to /health", () => {
  const source = read("pages/xtm-one/DashboardNotices.tsx");
  const at = source.indexOf('navigate("/health")');
  assert.ok(at > 0, "the notice opens /health");
  const fn = source.lastIndexOf("\nfunction ", at);
  assert.match(source.slice(fn, at), /if \(!isAdmin\) return null;/);
});

test("a cycle's, signal's or proposal's target opens a page with no route guard", () => {
  for (const type of ["agent", "flow", "assignment"] as const) {
    const to = healthTargetHref(type, "x1");
    assert.ok(to, type);
    const route = routeOf(to);
    assert.ok(route, `${to} has a route`);
    assert.equal(route.admin, false, `${to} is open to the target's managers`);
  }
});
