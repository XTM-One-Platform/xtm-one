/**
 * Every mutation of an entity the flow node catalog lists also syncs the
 * catalog - `yarn test:unit`.
 *
 * The catalog (the agent wizard's and the flow builder's pickers) is cached
 * for five minutes, so an entity it lists that was created, changed or deleted
 * elsewhere stayed missing, listed or misnamed there until it expired. The
 * hooks cannot run under `node --test`, so this reads them: every hook that
 * invalidates the list of an entity the catalog lists must also call
 * `syncFlowNodeCatalog` or `dropFromFlowNodeCatalog` (whose behaviour
 * `flow-node-catalog.test.ts` checks against a real query cache), and every
 * catalog mutation is named with the call it must make.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { parseSync } from "oxc-parser";

import type { CatalogEntityKind } from "./flow-node-catalog.ts";

const HOOKS_DIR = new URL("../hooks/api/", import.meta.url);

interface HookFunction {
  name: string;
  exported: boolean;
  text: string;
}

function topLevelFunctions(file: string): HookFunction[] {
  const text = readFileSync(new URL(file, HOOKS_DIR), "utf8");
  return parseSync(file, text).program.body.flatMap((statement) => {
    const exported = statement.type === "ExportNamedDeclaration" || statement.type === "ExportDefaultDeclaration";
    const declaration = exported ? statement.declaration : statement;
    if (declaration?.type !== "FunctionDeclaration" || !declaration.id) return [];
    return [{ name: declaration.id.name, exported, text: text.slice(statement.start, statement.end) }];
  });
}

// Knowledge bases are left out on purpose: document hooks invalidate that list
// for its counts without changing what the catalog lists, so its mutations are
// only named below.
const INVALIDATES_LISTED_ENTITY =
  /invalidateQueries\(\{\s*queryKey:\s*\["(agents|skills|custom-tools|flows|flow-event-types|integrations|assignments|variables|runners|llm-providers)"\]/;
const SYNCS_CATALOG = /(syncFlowNodeCatalog|dropFromFlowNodeCatalog)\(qc\b/;

/** Hooks that refresh one of those lists without changing what the catalog lists. */
const CANNOT_CHANGE_THE_CATALOG = new Set([
  "useFlowApi.ts: useActivateFlow",
  "useFlowApi.ts: useDeactivateFlow",
  "useFlowApi.ts: useRestoreFlowVersion",
  "useA2AApi.ts: useRegenerateA2ACredential",
  "useA2AClientApi.ts: useVerifyA2APartner",
  "useA2AClientApi.ts: useAcceptA2ACardChange",
  "useRunnerApi.ts: useApproveRunnerCommand",
  "useRunnerApi.ts: useRejectRunnerCommand",
  // A person's own account on an integration changes nothing the catalog lists.
  "useIntegrationApi.ts: useDeleteMyCredential",
  "useIntegrationApi.ts: useDeleteUserCredential",
]);

test("every hook that invalidates the list of an entity the catalog lists also syncs the catalog", () => {
  const missing: string[] = [];
  const exempted: string[] = [];
  let checked = 0;
  for (const file of readdirSync(HOOKS_DIR).filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts"))) {
    for (const fn of topLevelFunctions(file)) {
      if (!fn.exported || !INVALIDATES_LISTED_ENTITY.test(fn.text)) continue;
      checked += 1;
      const label = `${file}: ${fn.name}`;
      if (CANNOT_CHANGE_THE_CATALOG.has(label)) exempted.push(label);
      else if (!SYNCS_CATALOG.test(fn.text)) missing.push(label);
    }
  }
  assert.ok(checked >= 40, `expected the entity mutation hooks to be found, found ${checked}`);
  assert.deepEqual(missing, []);
  // An exemption that no longer matches anything is stale and must go.
  assert.deepEqual(exempted.sort(), [...CANNOT_CHANGE_THE_CATALOG].sort());
});

/** "sync": refreshes the catalog; "created": also writes the created
 *  knowledge base in; a kind: a delete that takes that entity out first. */
type Expected = "sync" | "created" | CatalogEntityKind;

const CATALOG_MUTATIONS: [file: string, hook: string, expected: Expected][] = [
  ["useAgentApi.ts", "useCreateAgent", "sync"],
  ["useAgentApi.ts", "useUpdateAgent", "sync"],
  ["useAgentApi.ts", "useRollbackAgentPersonaVersion", "sync"],
  ["useAgentApi.ts", "useRollbackAgentConfigVersion", "sync"],
  ["useAgentApi.ts", "useDuplicateAgent", "sync"],
  ["useAgentApi.ts", "useConfirmAgentImport", "sync"],
  ["useAgentApi.ts", "useUnrouteAgent", "sync"],
  ["useAgentApi.ts", "useDeleteAgent", "agent"],
  ["useCustomToolApi.ts", "useCreateCustomTool", "sync"],
  ["useCustomToolApi.ts", "useUpdateCustomTool", "sync"],
  ["useCustomToolApi.ts", "useDeleteCustomTool", "custom_tool"],
  ["useSkillApi.ts", "useCreateSkill", "sync"],
  ["useSkillApi.ts", "useUploadSkill", "sync"],
  ["useSkillApi.ts", "useUpdateSkill", "sync"],
  ["useSkillApi.ts", "useRollbackSkillVersion", "sync"],
  ["useSkillApi.ts", "useRefreshSkill", "sync"],
  ["useSkillApi.ts", "useForkSkill", "sync"],
  ["useSkillApi.ts", "invalidateAfterPackageImport", "sync"],
  ["useSkillApi.ts", "useDeleteSkill", "skill"],
  ["useKnowledgeBaseApi.ts", "useCreateKnowledgeBase", "created"],
  ["useKnowledgeBaseApi.ts", "useImportKnowledgeBase", "created"],
  ["useKnowledgeBaseApi.ts", "useUpdateKnowledgeBase", "sync"],
  ["useKnowledgeBaseApi.ts", "useDeleteKnowledgeBase", "knowledge_base"],
  ["useFlowApi.ts", "useCreateFlow", "sync"],
  ["useFlowApi.ts", "useUpdateFlow", "sync"],
  ["useFlowApi.ts", "useDuplicateFlow", "sync"],
  ["useFlowApi.ts", "useImportFlow", "sync"],
  ["useFlowApi.ts", "useDetachFlow", "sync"],
  ["useFlowApi.ts", "useDeleteFlow", "flow"],
  ["useFlowApi.ts", "useCreateFlowEventType", "sync"],
  ["useFlowApi.ts", "useDeleteFlowEventType", "event_type"],
  ["useIntegrationApi.ts", "useCreateIntegration", "sync"],
  ["useIntegrationApi.ts", "useUpdateIntegration", "sync"],
  ["useIntegrationApi.ts", "useImportIntegrationBundle", "sync"],
  ["useIntegrationApi.ts", "useUninstallCustomIntegrationProvider", "sync"],
  ["useIntegrationApi.ts", "useSetCustomIntegrationProviderEnabled", "sync"],
  ["useIntegrationApi.ts", "useDeleteIntegration", "integration"],
  ["useSettingsApi.ts", "useSetProviderDisabled", "sync"],
  ["useSettingsApi.ts", "useUpdateDisabledProviders", "sync"],
  ["useSettingsApi.ts", "useCreateLLMProvider", "sync"],
  ["useSettingsApi.ts", "useUpdateLLMProvider", "sync"],
  ["useSettingsApi.ts", "useDeleteLLMProvider", "llm_provider"],
  ["useAssignmentApi.ts", "useCreateAssignment", "sync"],
  ["useAssignmentApi.ts", "useUpdateAssignment", "sync"],
  ["useAssignmentApi.ts", "useDeleteAssignment", "assignment"],
  ["useVariableApi.ts", "useCreateVariable", "sync"],
  ["useVariableApi.ts", "useUpdateVariable", "sync"],
  ["useVariableApi.ts", "useDeleteVariable", "variable"],
  ["useRunnerApi.ts", "useUpdateRunner", "sync"],
  ["useRunnerApi.ts", "useRevokeRunner", "sync"],
  ["useRunnerApi.ts", "useDeleteRunner", "runner"],
  ["usePublicCatalogApi.ts", "useAddCatalogItemToAccount", "sync"],
  ["usePlatformApi.ts", "usePurgeRegistration", "sync"],
  ["useFleetApi.ts", "useCreateFleet", "sync"],
  ["useFleetApi.ts", "useApplyFleetPracticeSuggestion", "sync"],
];

function expectedCall(expected: Expected): RegExp {
  if (expected === "sync") return /syncFlowNodeCatalog\(qc\b/;
  if (expected === "created") return /syncFlowNodeCatalog\(qc,\s*\w+\)/;
  return new RegExp(`dropFromFlowNodeCatalog\\(qc,\\s*"${expected}",\\s*\\w+\\)`);
}

test("every catalog mutation makes its call: a sync, a created base written in, or a deleted entity taken out", () => {
  const functions = new Map<string, string>();
  for (const file of new Set(CATALOG_MUTATIONS.map(([file]) => file))) {
    for (const fn of topLevelFunctions(file)) functions.set(`${file}: ${fn.name}`, fn.text);
  }
  const missing = CATALOG_MUTATIONS.filter(
    ([file, hook, expected]) => !expectedCall(expected).test(functions.get(`${file}: ${hook}`) ?? ""),
  ).map(([file, hook, expected]) => `${file}: ${hook} (${expected})`);
  assert.deepEqual(missing, []);
});
