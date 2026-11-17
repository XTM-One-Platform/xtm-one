// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * A picker fed by runtime data is searchable.
 *
 * The design system's `Select` cannot filter, so a list that grows with the
 * platform - agents, users, models, integrations, templates - is a
 * `SearchableSelect` (`components/ui/searchable-select.tsx`), which also sorts
 * it. A plain `Select` maps a module constant (`DECK_TRANSITIONS`,
 * `Object.keys(SORT_LABELS)`): a list fixed at build time, in an order chosen
 * on purpose.
 *
 * This reads every `<SelectContent>` and flags the ones mapping anything else.
 * A short list that only looks dynamic (the auth types of one provider, the
 * tiers of a share) is named in FIXED_LISTS with why it stays short; anything
 * else becomes a `SearchableSelect` (`searchable="auto"` for a list of unknown
 * length that is usually short).
 *
 * Static, like the dialog-title and accessible-name guards: a picker behind a
 * permission or an empty state never renders in a test.
 */

const SRC = path.resolve(__dirname, "../src");

/** The component itself renders a plain `Select` for its short lists. */
const COMPONENT = "components/ui/searchable-select.tsx";

/** `file | mapped expression` -> why the list stays short and keeps its order. */
const FIXED_LISTS: Record<string, string> = {
  "pages/xtm-one/ctem/stage-heroes.tsx | matrix.chains": "the CTEM frameworks the posture declares, a handful (#11549)",
  "pages/xtm-one/ctem/DashboardContextBar.tsx | frameworks": "the kill chains the CTEM data carries, the same handful as the stage matrix (#11547)",
  "pages/xtm-one/ctem/CasesBulkActions.tsx | cases": "the cases the user selected for a merge, picked one by one in the list (#11545)",
  "components/evaluation/evaluation-extractor-card.tsx | offeredKinds": "the output kinds the evaluation's family scores, of the three an extractor has",
  "components/evaluation/evaluation-scoring-card.tsx | (familyCatalog ?? [])": "the evaluation families the platform implements, a handful",
  "components/integrations/openapi/openapi-auth-fields.tsx | supported": "the authentication schemes one OpenAPI spec declares",
  "components/custom-dashboard/DashboardInspector.tsx | views": "the views that draw one live widget's data, at most six",
  "components/custom-dashboard/charts/OutcomeMatrix.tsx | named": "the kill chains of a validation matrix, a handful, sorted by name as the module lists them",
  "components/investigation/pack-builder/PackBuilder.tsx | catalog.icons": "the action glyphs of the pack catalog, picked by their look",
  "components/investigation/pack-builder/PackBuilder.tsx | catalog.kinds": "the source kinds of the investigation catalog, in its order",
  "components/investigation/pack-builder/PackBuilder.tsx | catalog.fingerprint_sets": "the fingerprint sets of the investigation catalog",
  "components/presentation/PresentationEditorPanel.tsx | rowLayouts": "the layouts one slide row can take, in design order",
  "components/presentation/TemplateDesignEditor.tsx | emblemSizeOptions(draft.emblem_size)": "the eight emblem size steps of the 24 to 96 px range, plus the stored size when it is off them",
  "components/presentation/TemplateDesignEditor.tsx | coverEmblemScaleOptions(emblem.scale)": "the six cover emblem scale steps, plus the stored scale when it is off them",
  "components/presentation/TemplateDesignEditor.tsx | coverEmblemOpacityOptions(emblem.opacity)": "the four cover emblem opacity steps, plus the stored opacity when it is off them",
  "components/presentation/TemplateDesignEditor.tsx | supported": "the colour schemes a template supports",
  "components/ui/agent-graph-toolbar.tsx | options": "the five graph layouts, in design order",
  "components/ui/data-table.tsx | columns": "the sortable columns of one card list (GridSort), a handful its page declares, in column order",
  "components/ui/principal-sharing-card.tsx | visiblePermissions": "the sharing tiers, in the order of the rights they grant",
  "pages/assignment-detail/TriggerCard.tsx | (param.options ?? [])": "the options the editor typed just above for one select parameter, its own short closed set (#9375 slice 45)",
  "pages/DatasetDetail.tsx | (familyCatalog ?? [])": "the evaluation families the platform implements, a handful",
  'pages/desktop/RunnersSection.tsx | (caps?.permission_modes ?? ["restricted", "full_control"])': "a runner's permission modes, from the least power to the most",
  "pages/integration-detail/ConfigurationCard.tsx | authModeChoices": "the authentication types a provider supports, plus the one a row is stored with (#8386)",
  "pages/integration-detail/OpenAPISpecTab.tsx | compatible": "the authentication options compatible with the spec",
  "pages/NewIntegration.tsx | selected.supported_auth_types": "the authentication types a provider supports",
  "pages/profile/agent-accounts/AgentAccountEditor.tsx | types": "the conversation types of a messaging platform",
  "pages/profile/ApiKeyDialog.tsx | expiryOptions": "the expiry presets the policy allows, shortest first",
  "pages/profile/ApiKeyDialog.tsx | policy.quota_periods": "the quota periods, shortest first",
  "pages/settings/OrganizationExposureCards.tsx | Object.entries(labels)": "the asset type, exposure, environment, exclusion kind and knowledge kind of an organization row, three to nine fixed constants in rank order",
  "pages/settings/OrganizationListEditor.tsx | fields": "the fields the bulk toolbar of an organization list sets, one to three per list, in column order",
  "pages/settings/OrganizationListEditor.tsx | Object.entries(current.options)": "the criticality, AI system kind or exposure levels the bulk toolbar sets, three to six fixed constants in rank order",
  "pages/settings/OrganizationSection.tsx | Object.entries(labels)": "the criticality, AI system kind and exposure levels of an organization row, three to six fixed constants in rank order",
  "pages/settings/SecurityAuditSection.tsx | categoryOptions": "the audit categories, sorted by label and mirrored by the category tiles",
  "pages/settings/SecurityOptionsSection.tsx | option.choices": "the states of a security option, in enforcement order",
};

const CONSTANT = /^[A-Z][A-Z0-9_]*$/;

async function* walk(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx") && !full.includes(".test.")) yield full;
  }
}

/** What each `{... .map(` inside a `<SelectContent>` iterates over, keyed `file | expression`. */
async function mappedLists(): Promise<{ blocks: number; runtime: { key: string; at: string }[] }> {
  let blocks = 0;
  const runtime: { key: string; at: string }[] = [];
  for await (const file of walk(SRC)) {
    const relative = path.relative(SRC, file).split(path.sep).join("/");
    if (relative === COMPONENT) continue;
    const source = await readFile(file, "utf8");
    for (const match of source.matchAll(/<SelectContent[\s>]/g)) {
      blocks += 1;
      const close = source.indexOf("</SelectContent>", match.index);
      const body = source.slice(match.index, close === -1 ? undefined : close);
      for (const map of body.matchAll(/\.map\(/g)) {
        const expression = body.slice(body.lastIndexOf("{", map.index) + 1, map.index).replace(/\s+/g, " ").trim();
        const root = expression
          .replace(/^\(+/, "")
          .replace(/^Object\.(keys|entries|values)\(/, "")
          .match(/^[A-Za-z_$][\w$]*/)?.[0] ?? "";
        if (CONSTANT.test(root)) continue;
        const line = source.slice(0, match.index + map.index).split("\n").length;
        runtime.push({ key: `${relative} | ${expression}`, at: `${relative}:${line}` });
      }
    }
  }
  return { blocks, runtime };
}

describe("Searchable lists", () => {
  it("never feeds a plain Select from runtime data", async () => {
    const { runtime } = await mappedLists();
    const unexplained = runtime.filter(({ key }) => !(key in FIXED_LISTS)).map(({ key, at }) => `${at}  (${key})`);
    expect(
      unexplained,
      `plain Selects mapping runtime data - make them a SearchableSelect, or name the list in FIXED_LISTS with why it stays short:\n  ${unexplained.join("\n  ")}`,
    ).toEqual([]);
  });

  it("names no list that is gone", async () => {
    const seen = new Set((await mappedLists()).runtime.map(({ key }) => key));
    expect(Object.keys(FIXED_LISTS).filter((key) => !seen.has(key))).toEqual([]);
  });

  it("checks a meaningful number of selects, so a broken matcher cannot pass", async () => {
    expect((await mappedLists()).blocks).toBeGreaterThan(60);
  });
});
