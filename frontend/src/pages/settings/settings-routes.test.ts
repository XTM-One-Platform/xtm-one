/**
 * The Settings sections (#9173, S1) - `yarn test`.
 *
 * #797: a former tab route is not a section (not found, no redirect), and
 * no route literal of a former tab is left in the frontend (src, tests_e2e) or
 * the backend. Only route literals are matched: the path then a quote, `?`,
 * `#` or `/`; API calls (`api.get("/settings/quotas")`) and the API hooks are
 * left out, they name backend endpoints, not pages.
 * UX condition C3: bare `/settings/automations` opens Fleets in XTM One mode
 * and Agent runs in Copilot mode; Platform Health with its flag off falls back to it.
 * Fleets (and its member presets) is reachable in both modes (#9162 scope).
 */
import { describe, expect, it } from "vitest";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { automationsSection, isSettingsTab, pickSection, RETIRED_SETTINGS_SEGMENTS } from "./settings-routes";

describe("isSettingsTab (#797)", () => {
  it("accepts bare /settings and the 7 sections", () => {
    for (const tab of [undefined, "organization", "automations", "integrations", "ai", "security", "usage", "platform"]) {
      expect(isSettingsTab(tab)).toBe(true);
    }
  });

  it("refuses every former tab and anything unknown: not found, never redirected", () => {
    for (const tab of [...RETIRED_SETTINGS_SEGMENTS, "toString", "nope"]) {
      expect(isSettingsTab(tab)).toBe(false);
    }
  });
});

describe("automationsSection (C3)", () => {
  const xtm = { isXtmOne: true };
  const copilot = { isXtmOne: false };

  it("opens Fleets in XTM One mode and Agent runs in Copilot mode", () => {
    expect(automationsSection(undefined, xtm)).toBe("fleets");
    expect(automationsSection(undefined, copilot)).toBe("agent-runs");
  });

  it("opens Platform Health in every mode, built in with no flag; the former segment is no alias (#797)", () => {
    expect(automationsSection("health", xtm)).toBe("health");
    expect(automationsSection("health", copilot)).toBe("health");
    expect(automationsSection("concierge", xtm)).toBe("fleets");
    expect(automationsSection("concierge", copilot)).toBe("agent-runs");
  });

  it("never shows an XTM One section in Copilot mode", () => {
    expect(automationsSection("ctem-agents", copilot)).toBe("agent-runs");
    expect(automationsSection("quality", copilot)).toBe("quality");
  });

  it("opens Fleets and its member presets in every mode (#9162)", () => {
    expect(automationsSection("fleets", copilot)).toBe("fleets");
    expect(automationsSection("fleets", xtm)).toBe("fleets");
  });
});

it("pickSection keeps an allowed sub-section, else the first", () => {
  expect(pickSection("oauth", ["catalog", "oauth"])).toBe("oauth");
  expect(pickSection(undefined, ["catalog", "oauth"])).toBe("catalog");
  expect(pickSection("nope", ["catalog", "oauth"])).toBe("catalog");
});

// vitest runs from frontend/ (CI and `yarn test`).
const FRONTEND = process.cwd();
const BACKEND_APP = join(FRONTEND, "..", "backend", "app");
const SELF = join(FRONTEND, "src", "pages", "settings", "settings-routes.test.ts");
const ROUTES = join(FRONTEND, "src", "pages", "settings", "settings-routes.ts");
// hooks/api holds the API calls: the backend's own `/settings/*` endpoints.
const API_HOOKS = join(FRONTEND, "src", "hooks", "api");
const SKIP_DIRS = new Set(["node_modules", "__pycache__"]);

function* files(dir: string, ext: RegExp): Generator<string> {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && full !== API_HOOKS) yield* files(full, ext);
    } else if (ext.test(entry.name) && full !== SELF && full !== ROUTES) {
      yield full;
    }
  }
}

const OLD = RETIRED_SETTINGS_SEGMENTS.join("|");
// A page route literal: the path, then a quote, `?`, `#` or `/`, not an API call.
const LITERAL = new RegExp(`(?<!api\\.(?:get|put|post|patch|delete)\\()["'\`]/settings/(?:${OLD})(?=["'\`?#/])`);
// A frontend URL built in Python: after `{settings.frontend_url}` or as a
// path constant; the router paths of the backend's own API are left out.
const PY_LITERAL = new RegExp(`\\}/settings/(?:${OLD})(?=["'?#/])|_PATH = ["']/settings/(?:${OLD})(?=["'?#/])`);

it("no old Settings route literal is left in the frontend or the backend (C1)", () => {
  const offenders: string[] = [];
  for (const root of [join(FRONTEND, "src"), join(FRONTEND, "tests_e2e")]) {
    for (const file of files(root, /\.(ts|tsx)$/)) {
      readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        if (LITERAL.test(line)) offenders.push(`${relative(FRONTEND, file).split(sep).join("/")}:${i + 1}`);
      });
    }
  }
  for (const file of files(BACKEND_APP, /\.py$/)) {
    readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      if (PY_LITERAL.test(line)) offenders.push(`backend/app/${relative(BACKEND_APP, file).split(sep).join("/")}:${i + 1}`);
    });
  }
  expect(offenders).toEqual([]);
});
