// @vitest-environment node
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Warning text reads at 4.5:1 on a card and on the faint amber boxes, in both themes.
 *
 * `text-feedback-warning-primary` is the only warning text colour. Faded with an opacity
 * modifier (`/90`, `/80`, `/70`...) over an amber `/5` box it fell to 3.98:1 in the light
 * theme at `/90`, under WCAG AA for the 11-12 px notes it dresses. At full strength it
 * clears 4.5:1 on every faint amber fill the frontend uses, over the card and dialog surface
 * (`--bg-elevation-default-layer-1`) those boxes sit on. This computes that contrast from the
 * design system's own tokens and keeps the faded forms out of the sources: a quieter line in
 * a warning box takes `text-default-secondary`, never a faded warning colour.
 *
 * A box drawn straight on the page (`--bg-elevation-default-layer-0`) is the exception: the
 * same faint tint over the bare page background stays under 4.5:1 in the light theme, so such
 * a box paints the opaque `--color-warning-box` (index.css: the tint mixed into the card
 * surface) and the check below holds that token at 4.5:1 too.
 */

const FRONTEND = path.resolve(__dirname, "..");
const APP_CSS = path.join(FRONTEND, "src/index.css");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");

type Rgb = [number, number, number];
type Scope = Record<string, string>;

function declarations(css: string, selector: string): Scope {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `the token sheet no longer declares ${selector}`).toBeGreaterThan(-1);
  const end = css.indexOf("\n}", start);
  return Object.fromEntries(
    [...css.slice(start, end).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
  );
}

function hex(value: string): Rgb {
  const h = value.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
}

function resolve(value: string, scope: Scope, primitives: Scope, over: Rgb): Rgb {
  const v = value.trim();
  const variable = v.match(/^var\(--([\w-]+)\)$/);
  if (variable) {
    const name = variable[1];
    if (primitives[name]) return hex(primitives[name]);
    expect(scope[name], `--${name} is not declared`).toBeDefined();
    return resolve(scope[name], scope, primitives, over);
  }
  const mix = v.match(/^color-mix\(in srgb,\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*transparent\)$/);
  if (mix) return blend(resolve(mix[1], scope, primitives, over), over, Number(mix[2]) / 100);
  const mixTwo = v.match(/^color-mix\(in srgb,\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*(.+)\)$/);
  if (mixTwo) {
    const base = resolve(mixTwo[3], scope, primitives, over);
    return blend(resolve(mixTwo[1], scope, primitives, base), base, Number(mixTwo[2]) / 100);
  }
  if (v.startsWith("#")) return hex(v);
  throw new Error(`cannot resolve ${v}`);
}

function blend(colour: Rgb, over: Rgb, share: number): Rgb {
  return colour.map((c, i) => c * share + over[i] * (1 - share)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

async function schemes() {
  const css = await readFile(THEME, "utf8");
  const primitives: Scope = Object.fromEntries(
    [...css.matchAll(/--([a-z]+-\d+):\s*(#[0-9a-fA-F]{6})/g)].map(([, name, value]) => [name, value]),
  );
  const root = declarations(css, ":root");
  return {
    primitives,
    themes: { dark: { ...root, ...declarations(css, ".dark") }, light: { ...root, ...declarations(css, ".light") } },
  };
}

async function* sources(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(full);
    else if (/\.(tsx?)$/.test(entry.name) && !entry.name.includes(".test.")) yield full;
  }
}

// The faint amber fills warning boxes are drawn with: none, /5, [0.07], [0.08] and /10.
const FAINT_AMBER_FILLS = [0, 0.05, 0.07, 0.08, 0.1];
// Warning boxes sit in cards and dialogs: the first raised surface.
const SURFACE = "var(--bg-elevation-default-layer-1)";
// The bare page background a box drawn straight on the page sits on.
const PAGE = "var(--bg-elevation-default-layer-0)";

// The shared warning class of rows that fill on hover, and the rows that use it.
const WARNING_ON_HOVER = path.join(FRONTEND, "src/lib/warning-text.ts");
// Each hover row, with the lines that may still name the warning primary: icons and
// controls (3:1 is their bar), never text.
const HOVER_ROWS: Record<string, string[]> = {
  "src/components/ui/schedule-row.tsx": ["<AgentIcon"],
  "src/components/ui/run-card.tsx": ["animate-pulse", "hover:text-feedback-warning-primary", "focus-visible:ring-feedback-warning-primary"],
  "src/pages/chat/RunnerActivity.tsx": ['warning: "text-feedback-warning-primary"', "<FolderTree"],
  // The approval prompt the run card and its lightweight row render.
  "src/components/ui/action-proposal.tsx": [],
};
// The rows that fill on hover carry the named group the shared class listens to; a bare
// `group-hover` would also follow an outer `group` such as the chat message bubble.
const HOVER_ROW_GROUP = "group/hover-row";
const HOVER_ROW_OWNERS = ["src/components/ui/schedule-row.tsx", "src/components/ui/run-card.tsx", "src/pages/chat/RunnerActivity.tsx"];
const LIGHT_HOVER = "light:group-hover/hover-row:text-feedback-warning-";

async function warningBoxToken(): Promise<string> {
  const css = await readFile(APP_CSS, "utf8");
  const value = css.match(/--color-warning-box:\s*([^;]+);/)?.[1];
  expect(value, "index.css no longer declares --color-warning-box").toBeDefined();
  return value as string;
}

describe("warning text on faint amber boxes", () => {
  it("reads at 4.5:1 at full strength on a card and on every faint amber fill, in both themes", async () => {
    const { primitives, themes } = await schemes();
    for (const [name, scope] of Object.entries(themes)) {
      const page = resolve(SURFACE, scope, primitives, [0, 0, 0]);
      const amber = resolve("var(--color-feedback-alert-primary)", scope, primitives, page);
      const warning = resolve("var(--color-feedback-warning-primary)", scope, primitives, page);
      for (const share of FAINT_AMBER_FILLS) {
        const box = blend(amber, page, share);
        expect(contrast(warning, box), `warning text on amber at ${share}, ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the faded /90 form would not: it is why the modifier is banned", async () => {
    const { primitives, themes } = await schemes();
    const scope = themes.light;
    const page = resolve(SURFACE, scope, primitives, [0, 0, 0]);
    const box = blend(resolve("var(--color-feedback-alert-primary)", scope, primitives, page), page, 0.05);
    const faded = blend(resolve("var(--color-feedback-warning-primary)", scope, primitives, page), box, 0.9);
    expect(contrast(faded, box)).toBeLessThan(4.5);
  });

  it("a faint tint straight on the page background would not, in the light theme", async () => {
    const { primitives, themes } = await schemes();
    const scope = themes.light;
    const page = resolve(PAGE, scope, primitives, [0, 0, 0]);
    const box = blend(resolve("var(--color-feedback-alert-primary)", scope, primitives, page), page, 0.06);
    const warning = resolve("var(--color-feedback-warning-primary)", scope, primitives, page);
    expect(contrast(warning, box)).toBeLessThan(4.5);
  });

  it("reads at 4.5:1 on the opaque warning box token drawn on the page, in both themes", async () => {
    const { primitives, themes } = await schemes();
    const token = await warningBoxToken();
    for (const [name, scope] of Object.entries(themes)) {
      const page = resolve(PAGE, scope, primitives, [0, 0, 0]);
      const box = resolve(token, scope, primitives, page);
      const warning = resolve("var(--color-feedback-warning-primary)", scope, primitives, box);
      expect(contrast(warning, box), `warning text on --color-warning-box over the page, ${name}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("warning text on a hovered row reads at 4.5:1: tertiary in the light theme, primary in the dark", async () => {
    const { primitives, themes } = await schemes();
    const on = (scope: Scope, text: string) => {
      const hover = resolve("var(--bg-elevation-hover-layer-1)", scope, primitives, [0, 0, 0]);
      return contrast(resolve(`var(--color-feedback-warning-${text})`, scope, primitives, hover), hover);
    };
    expect(on(themes.light, "primary"), "the primary misses on the light hover fill").toBeLessThan(4.5);
    expect(on(themes.light, "tertiary")).toBeGreaterThanOrEqual(4.5);
    expect(on(themes.dark, "primary")).toBeGreaterThanOrEqual(4.5);
  });

  it("the shared hover-row class reads at 4.5:1 at rest and on hover, in both themes, on the bare row and in its amber boxes", async () => {
    const { primitives, themes } = await schemes();
    const source = await readFile(WARNING_ON_HOVER, "utf8");
    const value = source.match(/WARNING_ON_HOVER_TEXT\s*=\s*"([^"]+)"/)?.[1];
    expect(value, "src/lib/warning-text.ts no longer declares WARNING_ON_HOVER_TEXT").toBeDefined();
    const classes = (value as string).split(/\s+/);
    const rest = classes.find((c) => /^text-feedback-warning-\w+$/.test(c))?.replace("text-feedback-warning-", "");
    const lightHover = classes.find((c) => c.startsWith(LIGHT_HOVER))?.replace(LIGHT_HOVER, "");
    expect(rest).toBe("primary");
    expect(lightHover).toBe("tertiary");
    const failures: string[] = [];
    for (const [name, scope] of Object.entries(themes)) {
      const states = {
        rest: { fill: SURFACE, text: rest as string },
        hover: { fill: "var(--bg-elevation-hover-layer-1)", text: name === "light" ? (lightHover as string) : (rest as string) },
      };
      for (const [state, { fill, text }] of Object.entries(states)) {
        const row = resolve(fill, scope, primitives, resolve(SURFACE, scope, primitives, [0, 0, 0]));
        const amber = resolve("var(--color-feedback-alert-primary)", scope, primitives, row);
        // The run card's approval and question boxes paint amber /5 inside the row.
        for (const share of [0, 0.03, 0.05]) {
          const box = blend(amber, row, share);
          const ratio = contrast(resolve(`var(--color-feedback-warning-${text})`, scope, primitives, box), box);
          if (ratio < 4.5) failures.push(`${name} ${state} amber ${share}: ${ratio.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("every hover row takes the shared class, never a copy of its string", async () => {
    const copies: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      if (file === WARNING_ON_HOVER) continue;
      const source = await readFile(file, "utf8");
      source.split("\n").forEach((line, index) => {
        if (/group-hover(\/[\w-]+)?:text-feedback-warning-tertiary/.test(line)) copies.push(`${path.relative(FRONTEND, file)}:${index + 1}`);
      });
    }
    expect(copies, "import WARNING_ON_HOVER_TEXT from @/lib/warning-text instead").toEqual([]);
    for (const [row, warningLines] of Object.entries(HOVER_ROWS)) {
      const source = await readFile(path.join(FRONTEND, row), "utf8");
      expect(source, `${row} no longer imports the shared class`).toMatch(/import \{[^}]*WARNING_ON_HOVER_TEXT[^}]*\} from "@\/lib\/warning-text"/);
      const bare = source
        .split("\n")
        .map((line, index) => [line, index + 1] as const)
        .filter(([line]) => /\btext-feedback-warning-primary\b/.test(line) && !warningLines.some((allowed) => line.includes(allowed)))
        .map(([, n]) => `${row}:${n}`);
      expect(bare, `warning text in a hover row takes WARNING_ON_HOVER_TEXT`).toEqual([]);
    }
  });

  it("the shared class follows the hovered row only: a named group on every hover row", async () => {
    const source = await readFile(WARNING_ON_HOVER, "utf8");
    const value = source.match(/WARNING_ON_HOVER_TEXT\s*=\s*"([^"]+)"/)?.[1] ?? "";
    expect(value.split(/\s+/).filter((c) => /(^|:)group-hover:/.test(c)), "a bare group-hover also matches an outer group").toEqual([]);
    for (const row of HOVER_ROW_OWNERS) {
      const rowSource = await readFile(path.join(FRONTEND, row), "utf8");
      expect(rowSource.includes(HOVER_ROW_GROUP), `${row} no longer carries ${HOVER_ROW_GROUP}`).toBe(true);
    }
  });

  it("the tool scope dialog's parent-agent note reads at 4.5:1 on the dialog surfaces, in both themes", async () => {
    const { primitives, themes } = await schemes();
    const source = await readFile(path.join(FRONTEND, "src/components/ui/tool-scope-dialog.tsx"), "utf8");
    const classes = source.match(/export const TOOL_SCOPE_NOTE_TEXT = "([^"]+)"/)?.[1];
    expect(classes, "tool-scope-dialog.tsx no longer exports TOOL_SCOPE_NOTE_TEXT").toBeDefined();
    const tokens = (classes as string).split(/\s+/);
    const pick = (prefix: string) => tokens.find((c) => c.startsWith(prefix))?.slice(prefix.length);
    const base = pick("text-");
    const failures: string[] = [];
    for (const [name, scope] of Object.entries(themes)) {
      const text = (name === "light" ? pick("light:text-") : undefined) ?? base;
      expect(text, `no text colour for the ${name} theme`).toBeDefined();
      // The dialog paints layer 1; #F4F4F6 (layer 2) is the light surface the review measured.
      for (const surface of [SURFACE, "var(--bg-elevation-default-layer-2)"]) {
        const bg = resolve(surface, scope, primitives, [0, 0, 0]);
        const ratio = contrast(resolve(`var(--color-${text})`, scope, primitives, bg), bg);
        if (ratio < 4.5) failures.push(`${name} ${surface}: ${ratio.toFixed(2)}:1`);
      }
    }
    expect(failures).toEqual([]);
    // The none-selected warning sits on the same surface and takes the same colour.
    expect(source, "the none-selected warning no longer uses TOOL_SCOPE_NOTE_TEXT").toMatch(
      /data-testid="tool-scope-none-warning" className=\{cn\("content-caption", TOOL_SCOPE_NOTE_TEXT\)\}/,
    );
  });

  it("the plain warning primary it replaces would not, on the light layer 2", async () => {
    const { primitives, themes } = await schemes();
    const bg = resolve("var(--bg-elevation-default-layer-2)", themes.light, primitives, [0, 0, 0]);
    expect(contrast(resolve("var(--color-feedback-warning-primary)", themes.light, primitives, bg), bg)).toBeLessThan(4.5);
  });

  it("the shared filled-bar class reads at 4.5:1 at rest, on hover and on focus, in both themes", async () => {
    const { primitives, themes } = await schemes();
    const source = await readFile(WARNING_ON_HOVER, "utf8");
    const value = source.match(/WARNING_ON_FILL_TEXT\s*=\s*"([^"]+)"/)?.[1];
    expect(value, "src/lib/warning-text.ts no longer declares WARNING_ON_FILL_TEXT").toBeDefined();
    const classes = (value as string).split(/\s+/);
    const tone = (prefix: string) => classes.find((c) => c.startsWith(`${prefix}text-feedback-warning-`))?.replace(`${prefix}text-feedback-warning-`, "");
    const picked = {
      dark: { rest: tone(""), hover: tone("hover:"), focus: tone("focus-visible:") },
      light: { rest: tone("light:"), hover: tone("light:hover:"), focus: tone("light:focus-visible:") },
    };
    expect(picked.light).toEqual({ rest: "tertiary", hover: "tertiary", focus: "tertiary" });
    const failures: string[] = [];
    for (const [name, scope] of Object.entries(themes)) {
      const texts = picked[name as keyof typeof picked];
      // The bar sits on the page or in a card and paints the hover fill at rest; the
      // ghost button lays a second hover fill over it on hover and focus.
      for (const under of [PAGE, SURFACE]) {
        const bar = resolve("var(--bg-elevation-hover-layer-1)", scope, primitives, resolve(under, scope, primitives, [0, 0, 0]));
        const pressed = resolve("var(--bg-elevation-hover-layer-1)", scope, primitives, bar);
        for (const [state, fill] of [["rest", bar], ["hover", pressed], ["focus", pressed]] as const) {
          const text = texts[state] as string;
          const ratio = contrast(resolve(`var(--color-feedback-warning-${text})`, scope, primitives, fill), fill);
          if (ratio < 4.5) failures.push(`${name} ${state} on ${under}: ${ratio.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
    const bar = await readFile(path.join(FRONTEND, "src/components/ui/list-control-bar.tsx"), "utf8");
    expect(bar, "the list control bar no longer imports the shared class").toMatch(/import \{[^}]*WARNING_ON_FILL_TEXT[^}]*\} from "@\/lib\/warning-text"/);
    expect(bar.split("\n").filter((line) => /\btext-feedback-warning-primary\b/.test(line)), "warning text on the bar takes WARNING_ON_FILL_TEXT").toEqual([]);
  });

  it("never fades text-feedback-warning-primary with an opacity modifier", async () => {
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const source = await readFile(file, "utf8");
      source.split("\n").forEach((line, index) => {
        if (/\btext-feedback-warning-primary\/(?:\d|\[)/.test(line)) {
          offenders.push(`${path.relative(FRONTEND, file)}:${index + 1}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });

  it("draws no new translucent amber box: on the page it takes bg-warning-box", async () => {
    const counts: Record<string, number> = {};
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const n = (await readFile(file, "utf8")).split("\n").filter(isTranslucentAmberBox).length;
      if (n) counts[path.relative(path.join(FRONTEND, "src"), file).split(path.sep).join("/")] = n;
    }
    const grown = Object.entries(counts)
      .filter(([file, n]) => n > (TRANSLUCENT_AMBER_BOXES[file] ?? 0))
      .map(([file, n]) => `${file}: ${n} translucent amber box(es), ${TRANSLUCENT_AMBER_BOXES[file] ?? 0} known`);
    expect(grown, "a box on the page paints bg-warning-box; one inside a card or dialog is added to TRANSLUCENT_AMBER_BOXES").toEqual([]);
  });

  it("the census catches a seeded page-level amber box and leaves bg-warning-box alone", () => {
    expect(isTranslucentAmberBox('<div className="rounded-sm bg-feedback-alert-primary/5 p-3 text-feedback-warning-primary">')).toBe(true);
    expect(isTranslucentAmberBox('<p className="rounded-sm bg-feedback-alert-primary/10 px-3 py-2">')).toBe(true);
    expect(isTranslucentAmberBox('<div className="rounded-sm bg-warning-box p-3 text-feedback-warning-tertiary">')).toBe(false);
    expect(isTranslucentAmberBox('<span className="rounded-full bg-feedback-alert-primary/10" />')).toBe(false);
  });
});

/**
 * Translucent amber boxes (`bg-feedback-alert-primary/5` or `/10` on a padded, rounded element),
 * counted per file under src/. Over a card or a dialog (layer-1) such a box clears 4.5:1
 * with full-strength warning text; straight on the page (layer-0) it reads 4.23 (/5) and 4.12
 * (/10) in the light theme, so a page-level box paints the opaque `bg-warning-box` instead
 * (Agents.tsx, Analytics.tsx and NewAgentWizard.tsx moved). This census is the ratchet:
 * a file whose count grows fails above. A new box inside a card or dialog raises its file's
 * count here; a new box on the page uses `bg-warning-box`.
 */
function isTranslucentAmberBox(line: string): boolean {
  return (
    /\bbg-feedback-alert-primary\/(?:5|10)(?![\d\]])/.test(line) && /\brounded/.test(line) && /\b(?:p|px|py)-\d/.test(line)
  );
}

const TRANSLUCENT_AMBER_BOXES: Record<string, number> = {
  "components/InviteUserDialog.tsx": 1,
  "components/auth/DesktopSignIn.tsx": 1,
  "components/custom-dashboard/NewDashboardDialog.tsx": 1,
  "components/integrations/openapi/openapi-integration-form.tsx": 1,
  "components/investigation/SourcePackPicker.tsx": 1,
  "components/layout/Header.tsx": 2,
  "components/presentation/NewPresentationDialog.tsx": 1,
  "components/presentation/TemplateDesignEditor.tsx": 4,
  "components/presentation/TemplateGeneratorDialog.tsx": 1,
  "components/presentation/TemplatePreviewDialog.tsx": 1,
  "components/traces/span-inspector.tsx": 2,
  "components/ui/agent-export-dialog.tsx": 1,
  "components/ui/company-managed-banner.tsx": 1,
  "components/ui/credential-sharing-notice.tsx": 2,
  "components/ui/license-gate-dialog.tsx": 1,
  "components/ui/missing-variables-banner.tsx": 1,
  "components/ui/owner-deactivated-notice.tsx": 1,
  "components/ui/principal-sharing-card.tsx": 1,
  "components/ui/setup-guide-dialog.tsx": 1,
  "components/ui/vault-secrets.tsx": 1,
  "components/ui/xtm-mcp-package-download.tsx": 2,
  "components/video-studio/NewVideoDialog.tsx": 1,
  "components/video-studio/editor/SceneInspector.tsx": 1,
  "pages/BackgroundTasks.tsx": 1,
  "pages/DatasetDetail.tsx": 1,
  "pages/EvaluationDetail.tsx": 1,
  "pages/EvaluationRunDetail.tsx": 2,
  "pages/KnowledgeBaseDetail.tsx": 1,
  "pages/NewAgentImport.tsx": 1,
  "pages/Presentations.tsx": 1,
  "pages/Profile.tsx": 2,
  "pages/ResetPassword.tsx": 1,
  "pages/UserDetail.tsx": 3,
  "pages/agent-detail/AddAssignmentPanel.tsx": 1,
  "pages/agent-detail/MemoryTab.tsx": 2,
  "pages/agent-detail/OverviewTab.tsx": 1,
  "pages/agent-detail/config/ChannelsSection.tsx": 2,
  "pages/agent-detail/config/GeneralSection.tsx": 1,
  "pages/agent-detail/config/ToolingSection.tsx": 8,
  "pages/ai-adoption/shared.tsx": 1,
  "pages/assignment-detail/BuiltinScopeCard.tsx": 1,
  "pages/assignment-detail/ChannelsTab.tsx": 6,
  "pages/assignment-detail/IntegrationBindingsCard.tsx": 7,
  "pages/assignment-detail/TriggerCard.tsx": 3,
  "pages/chat/ChatMessages.tsx": 2,
  "pages/chat/SessionToolsActions.tsx": 1,
  "pages/desktop/RunnersSection.tsx": 3,
  "pages/integration-detail/A2AProtocolCards.tsx": 1,
  "pages/integration-detail/ConfigurationCard.tsx": 5,
  "pages/integration-detail/OpenAPISpecTab.tsx": 1,
  "pages/integration-detail/StatusCard.tsx": 1,
  "pages/integration-detail/ToolsCard.tsx": 1,
  "pages/integration-detail/WhatsAppPairingCard.tsx": 1,
  "pages/integration-detail/opencrq/OpenCrqVersionCard.tsx": 2,
  "pages/knowledge-base-detail/ConfigTab.tsx": 1,
  "pages/knowledge-base-detail/DocumentsTab.tsx": 1,
  "pages/mcp-server-detail/NewServerForm.tsx": 1,
  "pages/operations/SharedStateTab.tsx": 1,
  "pages/operations/WorkItemsTab.tsx": 1,
  "pages/profile/ToolApprovalsRow.tsx": 2,
  "pages/profile/TwoFactorRow.tsx": 1,
  "pages/public/components.tsx": 5,
  "pages/settings/ChannelManagement.tsx": 1,
  "pages/settings/GeneralAssistantSection.tsx": 2,
  "pages/settings/IntegrationCatalogSection.tsx": 1,
  "pages/settings/MCPEndpointSection.tsx": 2,
  "pages/settings/ModelAnnouncements.tsx": 1,
  "pages/settings/ModelPricingSection.tsx": 1,
  "pages/settings/ObservabilitySection.tsx": 3,
  "pages/settings/SecurityAuditSection.tsx": 1,
  "pages/settings/ToolApprovalsSection.tsx": 1,
  "pages/tools/ToolApprovalList.tsx": 3,
};

/**
 * Every DashboardNotice variant reads at 4.5:1 on the page, in both themes.
 *
 * The notice is drawn straight on the page background (`--bg-elevation-default-layer-0`).
 * Its error, success and progress variants used to fade their own tone (`/90` title, `/70`
 * or `/80` body) over a translucent tint of it, which fell to 3.49:1 for the error body in
 * the light theme. This reads the variant table from the component itself and computes the
 * title and body contrast on the box each variant paints, so a new variant or a faded class
 * fails here rather than in a review.
 */
const NOTICE = path.join(FRONTEND, "src/components/ui/dashboard-notice.tsx");
// `--color-accent` is the product accent: the brand colour, or the IA colour in Copilot mode.
const ACCENTS = ["var(--color-filigran-brand-primary)", "var(--color-filigran-ia-primary)"];

function toneColours(name: string): string[] {
  if (name === "accent") return ACCENTS;
  if (name.startsWith("default-")) return [`var(--text-${name})`];
  return [`var(--color-${name})`];
}

async function noticeVariants(): Promise<Record<string, Record<string, string>>> {
  const source = await readFile(NOTICE, "utf8");
  const start = source.indexOf("const VARIANT_STYLES");
  expect(start, "dashboard-notice.tsx no longer declares VARIANT_STYLES").toBeGreaterThan(-1);
  const table = source.slice(start, source.indexOf("\n};", start));
  return Object.fromEntries(
    [...table.matchAll(/^\s{2}(\w+): \{([^}]*)\}/gm)].map(([, variant, body]) => [
      variant,
      Object.fromEntries([...body.matchAll(/(\w+): "([^"]*)"/g)].map(([, key, value]) => [key, value])),
    ]),
  );
}

describe("dashboard notice text on the page", () => {
  it("declares the five variants", async () => {
    expect(Object.keys(await noticeVariants()).sort()).toEqual(["error", "info", "progress", "success", "warning"]);
  });

  it("reads at 4.5:1 for the title and the body of every variant, in both themes", async () => {
    const { primitives, themes } = await schemes();
    const token = await warningBoxToken();
    const failures: string[] = [];
    for (const [variant, style] of Object.entries(await noticeVariants())) {
      const fill = style.container.match(/\bbg-([\w-]+)(?:\/(?:\[([\d.]+)\]|(\d+)))?/);
      expect(fill, `${variant}: no background on the container`).not.toBeNull();
      const [, fillName, bracket, percent] = fill as RegExpMatchArray;
      for (const part of ["title", "body"] as const) {
        const text = style[part].match(/^text-([\w-]+)$/);
        expect(text, `${variant} ${part} "${style[part]}" is not one plain text colour (no opacity modifier)`).not.toBeNull();
        for (const [theme, scope] of Object.entries(themes)) {
          const page = resolve(PAGE, scope, primitives, [0, 0, 0]);
          const tints = fillName === "warning-box" ? [token] : toneColours(fillName);
          tints.forEach((tint, i) => {
            const share = bracket ? Number(bracket) : percent ? Number(percent) / 100 : 1;
            const box = fillName === "warning-box"
              ? resolve(tint, scope, primitives, page)
              : blend(resolve(tint, scope, primitives, page), page, share);
            const colours = toneColours((text as RegExpMatchArray)[1]);
            const colour = resolve(colours[Math.min(i, colours.length - 1)], scope, primitives, box);
            const ratio = contrast(colour, box);
            if (ratio < 4.5) failures.push(`${variant} ${part} ${theme} ${tint}: ${ratio.toFixed(2)}:1`);
          });
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("the faded error body it replaces would not, in the light theme", async () => {
    const { primitives, themes } = await schemes();
    const scope = themes.light;
    const page = resolve(PAGE, scope, primitives, [0, 0, 0]);
    const red = resolve("var(--color-feedback-error-primary)", scope, primitives, page);
    const box = blend(red, page, 0.06);
    expect(contrast(blend(red, box, 0.7), box)).toBeLessThan(4.5);
  });
});

/**
 * Error text on the runner surfaces reads at 4.5:1 in both themes.
 *
 * The chat's runner approval card (an amber `/[0.06]` box on a card) and the Machines page
 * block-list (a hover box on a card, or on the larger view's raised surface) painted
 * `text-feedback-error-primary/80`: 3.40:1 on the dark card. Even at full strength the primary
 * stays under 4.5:1 there (4.26:1 on the dark raised surface, 4.31:1 on the amber fill over a
 * dark card, 4.08:1 on the light hover grey), so both take `text-feedback-error-tertiary`.
 */
const ERROR_SURFACES = [
  "--bg-elevation-default-layer-0",
  "--bg-elevation-default-layer-1",
  "--bg-elevation-default-layer-2",
  "--bg-elevation-hover-layer-0",
  "--bg-elevation-hover-layer-1",
  "--bg-elevation-hover-layer-2",
];
const RUNNER_ERROR_SOURCES = ["src/pages/chat/RunnerApprovalCard.tsx", "src/pages/desktop/RunnersSection.tsx"];

describe("error text on the runner surfaces", () => {
  it("error-tertiary reads at 4.5:1 on every surface and on the amber approval fill, in both themes", async () => {
    const { primitives, themes } = await schemes();
    const failures: string[] = [];
    for (const [theme, scope] of Object.entries(themes)) {
      const page = resolve(PAGE, scope, primitives, [0, 0, 0]);
      for (const surface of ERROR_SURFACES) {
        expect(scope[surface.slice(2)], `${surface} is not declared in ${theme}`).toBeDefined();
        const bg = resolve(`var(${surface})`, scope, primitives, page);
        const amber = blend(resolve("var(--color-feedback-alert-primary)", scope, primitives, bg), bg, 0.06);
        const red = resolve("var(--color-feedback-error-tertiary)", scope, primitives, bg);
        for (const [name, box] of [[surface, bg], [`${surface} + amber 6%`, amber]] as const) {
          const ratio = contrast(red, box);
          if (ratio < 4.5) failures.push(`${theme} ${name}: ${ratio.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("error-primary would not, on the dark raised surface: it is why these surfaces take tertiary", async () => {
    const { primitives, themes } = await schemes();
    const scope = themes.dark;
    const bg = resolve("var(--bg-elevation-default-layer-2)", scope, primitives, resolve(PAGE, scope, primitives, [0, 0, 0]));
    expect(contrast(resolve("var(--color-feedback-error-primary)", scope, primitives, bg), bg)).toBeLessThan(4.5);
  });

  it("the runner sources never fade an error colour with an opacity modifier", async () => {
    const offenders: string[] = [];
    for (const file of RUNNER_ERROR_SOURCES) {
      const source = await readFile(path.join(FRONTEND, file), "utf8");
      source.split("\n").forEach((line, index) => {
        if (/\btext-feedback-error-\w+\/(?:\d|\[)/.test(line)) offenders.push(`${file}:${index + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});
