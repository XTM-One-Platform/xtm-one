/**
 * Generic AI in both modes - `yarn test` (slice S5 of #9533, `dev-docs/mode-split.md`).
 *
 * A generic AI capability (AI adoption, evaluations, ROI, fleets, Platform Health)
 * runs the same code path in XTM One mode and in Copilot mode; only
 * domain content, licence and edition semantics, the product-managed built-in
 * agents and the branding differ per mode. These source scans fail the build
 * when a change:
 *   1. hides a nav entry or a command on the mode (`xtmOne`);
 *   2. puts a generic AI route behind `XtmOneRoute`;
 *   3. reads the mode in a file that is not in MODE_READERS, where each entry
 *      names its class and reason from the inventory of `dev-docs/mode-split.md`.
 * MODE_READERS only shrinks: a file that no longer reads the mode leaves it in
 * the same change (the "stale" test).
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as data-table-gates.test.ts.
const SRC = join(process.cwd(), "src") + sep;

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sources(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

const FILES: { path: string; text: string }[] = [...sources(SRC)]
  .map((full) => ({ path: full.slice(SRC.length).split(sep).join("/"), text: readFileSync(full, "utf8") }))
  .sort((a, b) => a.path.localeCompare(b.path));

const text = (path: string): string => FILES.find((file) => file.path === path)?.text ?? "";

/** Destinations of generic AI capabilities: never gated on the mode. Intents are
 *  domain content of the XTM platforms, XTM One mode only (#445, dev-docs/mode-split.md). */
const GENERIC_AI = ["/ai-adoption", "/evaluations", "/roi", "/flows", "/health"];

type ModeClass = "domain content" | "licence and edition" | "built-in agents" | "branding" | "plumbing";

/** Files that may read the platform mode, each with its class and reason (dev-docs/mode-split.md). */
const MODE_READERS: Record<string, [ModeClass, string]> = {
  "App.tsx": ["domain content", "CTEM lifecycle, exposure briefing and Intents routes, CTEM home dashboard"],
  "components/companion/CompanionToday.tsx": ["domain content", "CTEM home dashboard vs general dashboard"],
  "components/fleets/CtemLoopSetupEntry.tsx": ["domain content", "'Set up the CTEM loop' entry of the command center and the CTEM loop fleet page, XTM One mode only (#1087)"],
  "components/fleets/FleetSetupDialog.tsx": ["domain content", "the CTEM loop fleet's Set up opens its versioned guided setup in XTM One mode only, the generic fleet wizard otherwise (#1151)"],
  "components/layout/AboutDialog.tsx": ["branding", "product name and edition in the About dialog"],
  "components/layout/OrganizationSetupPrompt.tsx": ["domain content", "first-login organization (CTEM) profile setup, XTM One mode only"],
  "components/layout/Sidebar.tsx": ["plumbing", "passes the mode to nav-model.ts, whose entries are gated here"],
  "components/layout/use-nav-group.ts": ["plumbing", "passes the mode to nav-model.ts for the hub pages' cards (#9301 N2c)"],
  "components/roi/OutcomesCard.tsx": ["domain content", "orders the mode's built-in ROI units first; unit help example per mode"],
  "components/ui/a2a-unavailable-notice.tsx": ["licence and edition", "licence vs Enterprise Edition wording"],
  "components/ui/enterprise-upsell.tsx": ["licence and edition", "licence vs Enterprise Edition upsell"],
  "components/ui/held-badge.tsx": ["licence and edition", "licence vs Enterprise Edition wording of a held run"],
  "components/ui/license-gate-dialog.tsx": ["licence and edition", "licence vs Enterprise Edition gate"],
  "components/ui/platform-mcp-server-list.tsx": ["domain content", "OpenCTI / OpenAEV bridges listed before they are connected"],
  "hooks/useEnterpriseUpsell.ts": ["licence and edition", "licence vs Enterprise Edition upsell"],
  "hooks/useHandoverEdgeEditing.tsx": ["built-in agents", "built-in agents are product-managed in XTM One mode"],
  "hooks/usePlatformConfig.tsx": ["plumbing", "defines useIsXtmOne and isXtmOneMode"],
  "lib/commands/CommandsProvider.tsx": ["plumbing", "exposes the xtmOne context key, which no command may read (gate 1)"],
  "pages/AgentDetail.tsx": ["built-in agents", "built-in agents are product-managed in XTM One mode"],
  "pages/AssignmentDetail.tsx": ["built-in agents", "built-in agents are product-managed in XTM One mode"],
  "pages/BackgroundTasks.tsx": ["licence and edition", "licence vs Enterprise Edition wording of a held task"],
  "pages/ChatPanelPage.tsx": ["domain content", "default chat agent is the CTEM Assistant in XTM One mode"],
  "pages/GroupDetail.tsx": ["licence and edition", "LLM proxy usage and quota rows follow the proxy (Copilot product)"],
  "pages/Profile.tsx": ["domain content", "CTEM stage agents card; LLM proxy rows follow the proxy (licence)"],
  "pages/Settings.tsx": ["domain content", "CTEM stage agents section and the Automations fallback section"],
  "pages/UserDetail.tsx": ["licence and edition", "LLM proxy usage and quota rows follow the proxy (Copilot product)"],
  "pages/Value.tsx": ["domain content", "page subtitle per mode (exposure management vs agents); figures and tables identical"],
  "pages/agent-detail/ConfigTab.tsx": ["built-in agents", "built-in agents are product-managed in XTM One mode"],
  "pages/xtm-one/ctem/CaseWorkCard.tsx": ["domain content", "the case work card offers the built-in CTEM loop setup in XTM One mode only (#1266)"],
  "pages/settings/HealthModuleAnalystBlock.tsx": ["domain content", "Module Analyst spend limits of Platform Health (OpenCTI, OpenAEV, OpenCRQ modules); Copilot mode has no module"],
  "pages/settings/HealthModuleThresholdsBlock.tsx": ["domain content", "OpenCTI / OpenAEV module thresholds of Platform Health; Copilot mode has no module"],
  "pages/settings/HealthRunAsBlock.tsx": ["domain content", "'Run checks as' user of the Platform Health module checks (OpenCTI, OpenAEV, OpenCRQ modules); Copilot mode has no module (#1351)"],
  "pages/settings/OrganizationSection.tsx": ["domain content", "Copilot mode keeps the Identity card alone: risk appetite, ticketing and CTEM case sync are CTEM inputs, and no fields-not-filled banner (#1690)"],
  "pages/settings/ParametersSection.tsx": ["domain content", "XTM Hub registration card; Reset onboarding card (guided setup, XTM One mode only); edition badge (licence)"],
  "pages/settings/QuotasSection.tsx": ["licence and edition", "agentic quota source and edition cards"],
  "pages/settings/RoiUnitsSection.tsx": ["domain content", "title and order of the mode's built-in ROI catalog"],
  "pages/agent-detail/AddAssignmentPanel.tsx": ["domain content", "the CTEM signal tile of the trigger picker, XTM One only (#1409)"],
  "pages/assignment-detail/TriggerCard.tsx": ["domain content", "the CTEM signal tile of the trigger picker, XTM One only (#1409)"],
};

const READS_MODE = /\buseIsXtmOne\(|\bisXtmOneMode\(|\bplatform_mode\s*[!=]==/;

describe("Mode split guard (dev-docs/mode-split.md, S5)", () => {
  it("1. gates no nav entry and no command on the mode", () => {
    const offenders = FILES.filter(({ path }) => path === "components/layout/nav-model.ts" || path.endsWith(".commands.ts"))
      .filter(({ text: source }) => /\.xtmOne\b|"xtmOne"/.test(source))
      .map(({ path }) => path);
    expect(offenders, "a generic AI entry is listed in both modes: gate it on its feature flag, not on xtmOne").toEqual([]);
  });

  it("2. puts no generic AI route behind XtmOneRoute", () => {
    const routes = text("App.tsx").split("\n").filter((line) => /<Route\b/.test(line) && /\bXtmOneRoute\b/.test(line));
    const offenders = routes.filter((line) => GENERIC_AI.some((to) => line.includes(`path="${to}`)));
    expect(offenders, "a generic AI page is served in both modes").toEqual([]);
  });

  it("3. reads the mode only in the files MODE_READERS names with a reason", () => {
    const offenders = FILES.filter(({ path, text: source }) => READS_MODE.test(source) && !(path in MODE_READERS)).map(({ path }) => path);
    expect(offenders, "generic AI runs the same code in both modes; domain content, licence, built-in agents or branding go in MODE_READERS with the reason").toEqual([]);
  });

  it("3. keeps MODE_READERS to the files that still read the mode", () => {
    const stale = Object.keys(MODE_READERS).filter((path) => !READS_MODE.test(text(path)));
    expect(stale, "drop a file that no longer reads the mode from MODE_READERS").toEqual([]);
  });
});
