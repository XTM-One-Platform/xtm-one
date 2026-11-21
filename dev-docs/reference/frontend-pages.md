# Frontend pages and navigation

How to add a page (component, route, `nav-model` entry), the rules the navigation model enforces, and the page → API hooks table.

## How to Add a New Page

### Step 1: Create the page component

```tsx
// frontend/src/pages/MyPage.tsx
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";

export default function MyPage() {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold">{t_i18n("My Page")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Description here.")}</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t_i18n("Section")}</CardTitle>
        </CardHeader>
        <CardContent>
          {/* Content */}
        </CardContent>
      </Card>
    </div>
  );
}
```

Every `t_i18n` key goes into the four catalogs (`frontend-common-pitfalls.md`).

### Step 2: Add route in App.tsx

```tsx
const MyPage = lazyWithRetry(() => import("@/pages/MyPage"));
// Inside the protected Route element (the one rendering AppLayout):
<Route path="/my-page" element={<Page><MyPage /></Page>} />
// Admin-only page: <AdminRoute><Page><MyPage /></Page></AdminRoute>
```

**Path, not query (Sam, 8 Nov 2026, #10203).** The path says where you are:
the page, its tab, a sub-view, a list (`/agents/:id/:tab?/:section?`,
`/flows/fleets/:fleetId/:tab?/:list?`, `/flows/map/schedule`). The query
keeps only state that is legitimately a query: filters, search, sort,
pagination, the time window (`?window=`), a focus or selection (`?focus=`,
`?agent=`), visibility (`?vis=`). A tab change is a `navigate` (a history
entry, so back and forward move between tabs; reload and share keep it).
When a place moves from the query to the path, the page reads the old link
once and redirects with `replace` (`legacyFleetLocation`,
`legacyMapLocation`, the `?tab=` shims of Tools and Video Studio). A tab
whose name could collide with an id is a static route declared before the
id route (`/tools/custom` before `/tools/:id/:tab?`, `/videos/media` before
`/videos/:id`). `tests_unit/path-routes-gate.test.ts` fails on a new fleet
tab, map view or Agentic Flow tab written as a query, and on any code that
reads or writes `?tab=` / `?view=` outside its `QUERY_ALLOWED` files (a
redirect shim, or a list filter such as the work items' Waiting segment),
each listed with its reason.

### Step 3: Add to the navigation

The nav tree is data, in `src/components/layout/nav-model.ts`: one edit there and the
page appears in the sidebar and in the ⌘K palette. A child of a group is also a card
of the group's hub page (`pages/AgentsHub.tsx`, `Work.tsx`, `AgentStudio.tsx`,
`ToolsAndIntegrations.tsx`): the hubs read their cards from the resolved nav group
(`useNavGroup` + `hubCards` in `components/layout/nav-hub.ts`, #9301 N2c), so the hub
page only adds the card's data (description, items, create route) keyed by the route;
a visible child with no data entry makes the hub test fail:

```ts
import { MyIcon } from "lucide-react";
// A top-level destination in a section:
leaf("/my-page", MyIcon, "My Page")
// …or a child of a group (`children:` of "work" / "agent-studio" / "tools-and-integrations")
// Gate it on a platform feature with the optional predicate:
leaf("/my-page", MyIcon, "My Page", (flags) => flags.chat)
// An AI area: `aiLeaf` keeps it listed and reachable with AI off, marked unavailable
aiLeaf("/my-page", MyIcon, "My Page")
```

Rules the model enforces (don't work around them):

- **Two levels, maximum.** A section holds leaves or groups; a group holds leaves.
  Anything deeper belongs in page tabs (`detail-tabs.tsx`).
- **Place a page by one question, never by traffic.** The top block reads as a
  story: Home (lit on the command center pages `/dashboard`, `/briefing`, `/ctem/*`
  too, through `alsoActive`), Chat, the **Orchestration** group (its row opens the
  `/agents-hub` hub; children Agents, Fleets, Agentic Flow, and Intents for administrators
  through a leaf `adminOnly`; "Create an agent" is an action, a `paletteOnly` destination
  of the group and a button on the list and the hubs, never a row), then the **Work** group, what agents
  *produce* (Tasks, then Operations, Presentations, Videos (the Video Studio, behind `VIDEO_STUDIO`), Dashboards, Files).
  A group's row and each of its children carry their own lock (`requiresAi`,
  `requiresLicense`), shown on the row inline and in the rail flyout. The left-menu
  consolidation (#9301) moves rows in slices; `nav-reachability.test.ts` pins who
  reaches which page, in which lock state, and every slice keeps it true. The next block (the `resources` section) holds two
  groups: **Agent Studio** is what they are *made of* (knowledge,
  prompts, templates, skills, investigation packs), **Tools & Integrations** is what
  they *act through* (MCP servers first, the richest capability and the page that
  also carries the MCP endpoints this platform exposes, then tools, integrations,
  browser profiles, variables, relays). A frequently visited page is not promoted to the top level for
  that reason: every extra top-level entry turns the block into a flat most-used list.
  The admin-only Administration section is collapsible.
- **A page that holds the settings of another page links to it, both ways** (#9301 N4):
  the Platform Health page carries a "Platform Health settings" button and the Platform Health settings
  section an "Open" button back; the Presentations and Videos headers link to Templates,
  where the designs they are dressed with live.
- **A list page draws its header through `ListPageHeader`** (title, its help in the "i",
  actions on one row; `src/list-header-help.test.ts` names every page it guards).
- **A group label with a `to` is a real destination** — a hub page listing its
  resources with counts, last-modified and quick-create (see `ResourceHub`), not a
  second copy of the menu. The chevron is the only expand/collapse control.
- Groups remember their open/closed state (`useNavPrefs`), and the group holding the
  page you navigate to opens itself.
- The sidebar can be collapsed to an **icon rail** (per-user, persisted); groups and
  collapsible sections become hover/click flyouts there, so nothing gets stranded.
  A flyout's open state is the rail's own and transient, never the groups' persisted
  expansion, and `NavbarSubmenu`'s `onOpenChange(next)` is always applied as `next`,
  never as a toggle: the library repeats "open" when the pointer enters the panel.
- A collapsible *section* needs an `icon` — the rail has no room for its label.
- **The shell is OpenCTI's and OpenAEV's.** Blocks are separated by a rule and carry
  no heading (only a collapsible section has a `label`); the header is the product
  switcher, listing the suite connected or not (`product-switcher.ts`); the foot is
  "Made by Filigran" (`MadeByFiligran`) and the collapse toggle. Anything else (About,
  XTM One Desktop) lives in the profile menu, not in the nav.
- **The nav is fixed structure, never user data.** No entry is derived from an entity
  the user created: an integration's own pages (a tab registered in
  `pages/integration-detail/provider-tabs.tsx`, `PROVIDER_EXTRA_TABS`: an imported
  OpenAPI spec, Nova's rules, OpenRouter's console) are reached from the Integrations
  page and live as tabs of its detail page, never as a leaf per connected integration
  (pinned by `components/layout/nav-model.test.ts`). Gate such a tab with its
  `visible` predicate: a named rule of `provider-tab-visibility.ts` (`adminsOnly`,
  `integrationManagersOnly`) matching the guard of the routes the tab calls, never an
  inline one. Register the tab's component in `provider-tab-pages.tsx` (lazy).


E2E note: a link inside a collapsed group is not in the DOM. Use
`expandNavGroup(page, 'Tools & Integrations')` from `tests_e2e/helpers.ts` before
clicking it, or navigate by URL.

## Current Pages and Their API Hooks

| Page | Route | Key Hooks |
|------|-------|-----------|
| Home (`HomeRoute`: AiDisabledLanding with AI off, else XtmOneDashboard or Dashboard by platform mode) | `/` | Dashboard: `useMe`, `useConversations`, `useIntegrations`, `useDashboardStats`, `useAllAssignmentRuns`, `useDashboardLayoutState` + `useUpdateProfile` (the widget grid each person arranges); XtmOneDashboard: `usePlatformRegistrations` |
| Command center (the mode dashboard) | `/dashboard` | as `/` |
| CTEM stage (XTM One mode only) | `/ctem/:stage` | `useCtemPosture`, `useCtemPostureHistory`, `useCtemStageAssessment`, `useRefreshStageAssessment` |
| Exposure briefing (XTM One mode only) | `/briefing` | `useCtemBriefing`, `useCtemPosture`, `useRefreshMyBriefing` |
| Chat | `/chat` | `useConversation`, `useAgents`, `useSessionTools`, `useTurnFrames`, `useDismissInterruptedTurn` |
| Work (hub) | `/work` | `useAllAssignmentRuns`, `useAssignmentRunCount`, `usePresentations`, `useFiles` |
| Agent Studio (hub) | `/agent-studio` | `useKnowledgeBases`, `usePrompts`, `useTemplateContainers`, `useSkills`, `useInvestigationPacks` (behind `DEEP_INVESTIGATION`) |
| Investigation packs | `/investigation-packs`, `/investigation-packs/:id/:tab?` | `useInvestigationPacks`, `useInvestigationPack`, `usePackBuilderCatalog`, `useBuiltinPacks`, `useDuplicateBuiltinPack`, `useCreateInvestigationPack`, `useUpdateInvestigationPack`, `usePreviewInvestigationPack`, `useInvestigationPackVersions` |
| Tools & Integrations (hub) | `/tools-and-integrations` | `useMCPServers`, `useCustomTools`, `useIntegrations`, `useBrowserProfiles`, `useVariables`, `useRelayGroups` |
| Operations (what is running or past; Executions tab = `TaskHistory`, Background Tasks, Shared State, Worksets) | `/operations/<tab>` | `useAllAssignmentRuns`, `useAllFlowRuns`, `useAssignmentRunCount`, `useDeleteAssignmentRun`, `useBulkDeleteRuns`, `useCleanupStaleRuns`, `useRespondToHumanInput`, `useDismissRun`, `useAgents`, `useBackgroundTasks`, `useSharedStates` |
| Tasks (what to do and follow, #775; Work Items the default tab, Value, Workspaces; every link through `lib/task-routes.ts`, no alias of a former path) | `/tasks/work-items[/<id>]`, `/tasks/value[/<table>]`, `/tasks/value/methodology`, `/tasks/workspaces[/<id>]` | `useWorkItems`, `useValueReport`, `useWorkspaces` |
| Presentations | `/presentations` | `usePresentations`, `usePresentationActions`, `useLibraryParams` |
| Video Studio (behind `VIDEO_STUDIO`; tabs Videos and Media, browser profiles live at `/connections`) | `/videos` | `useVideoProjects`, `useVideoTakes`, `useVideoClips`, `useVideoTemplates`, `useUpdateVideoAsset`, `useDuplicateVideoProject`, `useDeleteVideoProject` |
| Video Editor (behind `VIDEO_STUDIO`) | `/videos/:id` (outside `AppLayout`, full screen) | `useVideoProject`, `useVideoEditor`, `useVideoQuality`, `useVideoTemplates`, `useDuplicateVideoProject`, `useDeleteVideoProject` |
| Public video (public link, no session) | `/public/videos/:token` | `usePublicVideo`, `downloadPublicVideo` |
| Files | `/files` | `useFiles`, `useDeleteFile` |
| Agents | `/agents` | `useAgents`, `useAgentTemplates`, `useCreateAgent` |
| New Agent (mode choice) | `/agents/new` | none (links to the three ways below) |
| New Agent (advanced form) | `/agents/new/advanced` | `useAgentTemplates`, `useCreateAgent` |
| New Agent (wizard) | `/agents/new/wizard` | `useAgentTemplates`, `useAssistAgentWizard`, `useCreateAgent`, `useProviderCatalog`, `useFlowNodeCatalog` |
| New Agent (import) | `/agents/new/import` | `useCheckAgentImportFile`, `useConfirmAgentImport`, `useSuggestImportFixes` |
| Agent Detail | `/agents/:id/:tab?/:section?` | `useAgent`, `useUpdateAgent`, `useDeleteAgent`, `useAssignments`, `useSubAgentTasks`, `useAgentMemories`, `useSearchAgentMemory`, `useAgentChats`, `useClearAgentChats`, `useBrowserProfiles` (Configuration > Tooling, while the `browser` tool is on) |
| Agent Chat Conversation | `/agents/:id/chats/:conversationId` | `useAgent`, `useAgentChatDetailInfinite` |
| Agent Flow | `/agents/flow` | `useAgents` |
| Flows (map views: `/flows/map`, `/flows/map/schedule`) | `/flows/:tab?`, `/flows/map/:mapView` | `useFlows`, `useCreateFlow`, `useDeleteFlow`, `useDuplicateFlow`, `useExportFlow`, `useImportFlow`, `useAgents`, `useUpdateAgent`, `useAssignments`, `useGeneralAssistantConfig`, `useActiveAgentIds`, `useAllAssignmentRuns`, `useAgentDelegations`, `useWorkItems`, `useAssignmentSchedule` |
| Flow Builder (behind `FLOW_BUILDER`) | `/flows/library/:id` | `useFlow`, `useUpdateFlow`, `useFlowNodeCatalog`, `useFlowVersions`, `useRestoreFlowVersion`, `useImportFlow` |
| Integrations | `/integrations` | `useIntegrations`, `useProviderCatalog`, `useCreateIntegration`, `useDeleteIntegration` |
| Integration Detail | `/integrations/:id/:tab?` | `useIntegrations`, `useUpdateIntegration`, `useDeleteIntegration`, `useTestIntegration`, `useAuthorizeIntegration` |
| OAuth implicit sign-in callback | `/oauth/implicit` | none |
| Knowledge | `/knowledge` | `useKnowledgeBases`, `useCreateKnowledgeBase` |
| Knowledge documents (across bases) | `/knowledge/documents` | `useGlobalKnowledgeDocuments`, `useReprocessDocument` |
| Knowledge Base Detail | `/knowledge/:id/:tab?` | `useKnowledgeBase`, `useUpdateKnowledgeBase`, `useKnowledgeDocuments`, `useAddTextDocument` |
| MCP Servers | `/mcp-servers` | `useMCPServers`, `useCreateMCPServer` |
| MCP Server Detail | `/mcp-servers/:id/:tab?` | `useMCPServers`, `useUpdateMCPServer`, `useDeleteMCPServer`, `useTestMCPServer`, `useRefreshMCPTools` |
| Custom Tools | `/tools` | `useCustomTools`, `useCreateCustomTool` |
| Custom Tool Detail | `/tools/:id/:tab?` | `useCustomTools`, `useUpdateCustomTool`, `useDeleteCustomTool`, `useTestCustomTool` |
| Browser profiles (saved sign-ins the agents' browser and the Video Studio open; the default tab of Tools & Integrations > Connections, #797 S2: no alias of `/browser-profiles`, `/variables`, `/relays`) | `/connections` | `useBrowserProfiles`, `useDeleteBrowserProfile`, `useEntityFilter` (cards: `useSaveBrowserProfile`, `useSignInBrowserProfile`, `useSubmitMfaCode`) |
| Variables | `/connections/variables`, `/connections/variables/:id` | `useVariables`, `useVariable`, `useCreateVariable`, `useUpdateVariable`, `useDeleteVariable` |
| Skills | `/skills` | `useSkills`, `useCreateSkill`, `useDeleteSkill`, `useRefreshSkill` |
| Skill Detail | `/skills/:id/:tab?` | `useSkill`, `useUpdateSkill`, `useDeleteSkill`, `useRefreshSkill` |
| Prompts | `/prompts` | `usePrompts`, `useCreatePrompt` |
| Prompt Detail | `/prompts/:id/:tab?` | `usePrompt`, `useUpdatePrompt`, `useDeletePrompt` |
| Templates | `/templates`, `/templates/new`, `/templates/:id/:tab?` | `useTemplateContainers`, `useImportTemplateContainer`, `useCreateTemplateContainer`, `useTemplateContainer`, `useDeleteTemplateContainer`, `useDuplicateTemplateContainer`, `useExportTemplateContainer` |
| Assignment Detail | `/agents/assignments/:id/:tab?` | `useAssignment`, `useUpdateAssignment`, `useAssignmentRuns`, `useTriggerAssignment`, `useIntegrations`, `useChannelBots` |
| Run Detail | `/operations/executions/:id` | `useAssignmentRunDetail`, `useDescendantRuns`, `useRespondToHumanInput`, `useDismissRun` |
| Evaluations (admin) | `/evaluations`, `/evaluations/new`, `/evaluations/:id/:tab?`, `/evaluations/runs/:id`, `/evaluations/:evaluationId/compare` | `useEvaluations`, `useCreateEvaluation`, `useEvaluation`, `useEvaluationRuns`, `useTriggerEvaluation`, `useEvaluationRun`, `useMultipleEvaluationRuns` |
| Datasets (admin) | `/evaluations/datasets`, `/evaluations/datasets/:id/:tab?` | `useDatasets`, `useCreateDataset`, `useImportDatasetZip`, `useDataset`, `useUpdateDataset`, `useDeleteDataset` |
| Audit Logs (admin) | `/logs` | `useAuditLogs`, `useAuditLogActions`, `useAuditLogEntityTypes`, `useUsers` |
| Objects (admin) | `/objects` | `useAdminObjects`, `useUsers` |
| Intents (controls admin-only) | `/intents` | `useIntents`, `useVerticals`, `useCreateIntent`, `useUpdateIntent`, `useBindIntent` |
| Observability: Model usage and Outcomes (admin; a group manager reads Model usage; #797 S2 moved the family under its menu entry, no alias of `/analytics`, `/traces`, `/trace-sessions`) | `/observability`, `/observability/outcomes` | `useTraceAnalyticsSummary`, `useTraceAnalyticsCostOverTime`, `useTraceAnalyticsCostByAgent`, `useTraceAnalyticsLatency`, `useTraceAnalyticsErrors` |
| Traces (admin) | `/observability/traces` | `useTraces`, `useAgents` |
| Trace Sessions (admin) | `/observability/sessions` | `useTraceSessions`, `useAgents` |
| Trace Detail | `/observability/traces/:id/:tab?` | `useTrace`, `useTraceSpans`, `useTraceSessionExplorer` |
| AI Adoption (admin, when the feature is on) | `/ai-adoption/:tab?` | `useGroups` (tabs use `useAiAdoptionApi.ts`) |
| Settings (admin) | `/settings/:tab?/:section?` | `useLLMProviders`, `useCreateLLMProvider`, `useUpdateLLMProvider`, `useDeleteLLMProvider`, `useLLMProviderDeletionImpact`, `useGeneralAssistantConfig`, `useUpdateGeneralAssistantConfig`, `useAvailableModels`, `useAvailableMusicModels`, `useMCPServers`, `useUsers`, `useSSOConfig`, `useChannelBots`, `useChannelBotCatalog`, `useCreateChannelBot`, `useUpdateChannelBot`, `useDeleteChannelBot`, `usePurgeAllMemories`, `useGenerateMCPKey`, `useRevokeMCPKey` |
| Profile | `/profile/:tab?` | `useMe`, `useUpdateProfile`, `useChangePassword`, `useProviderModels`, `useApiKeys` |
| Agent account (`/profile/runners` redirects to `/desktop`) | `/profile/agent-accounts/:accountId/:tab?` | `useAgentAccount`, `useUpdateAgentAccount`, `useDeleteAgentAccount`, `useSyncAgentAccount` |
| XTM One Desktop | `/desktop` | `useDesktopRunner` |
| Runner Detail | `/runners/:id` | `useRunner` |
| Relays | `/connections/relays`, `/connections/relays/:id/:tab?` | `useRelayGroups`, `useCreateRelayGroup`, `useRelayMeta`, `useRelaysEnabled`, `useRelayGroup` |
| Users (admin; the default tab of Users and groups, #797 S2: no alias of `/users`, `/groups`) | `/users-and-groups` | `useUsers` |
| User Detail (admin) | `/users-and-groups/users/:id/:tab?` | `useUser`, `useUpdateUserRole`, `useToggleUserActive`, `useDeleteUser`, `useUserStats` |
| New User (admin) | `/users-and-groups/users/new` | `useCreateUser`, `useAuthConfig` |
| Groups | `/users-and-groups/groups` | `useGroups`, `useCreateGroup`, `useDeleteGroup` |
| Group Detail (admin) | `/users-and-groups/groups/:id/:tab?` | `useGroup`, `useUpdateGroup`, `useDeleteGroup`, `useAddGroupMembers`, `useRemoveGroupMember` |
| New Group (admin) | `/users-and-groups/groups/new` | `useCreateGroup` |
| Presentation Viewer (outside AppLayout) | `/presentations/:id` | `usePresentation`, `usePresentationPresence` |
| Speaker view / notes handout | `/presentations/:id/speaker`, `/presentations/:id/notes` | `usePresentation` |
| Public deck (public link, no session) | `/public/presentations/:token` | `usePublicPresentation`, `useDownloadPublicPresentationPdf` |
| Static renders behind PDF / PPTX exports (render token, no session) | `/presentations/:id/static`, `/public/presentations/:token/static`, `/reports/:id/static`, `/videos/figures/static` (a video's figure scene) | none |
| Report Viewer (outside AppLayout) | `/reports/:id` | `useReport` |
| Login | `/login` | `useLogin`, `useRegister`, `useAuthConfig` (SSO: OIDC + SAML) |
| Register (invitation) | `/register` | `useValidateInvitation`, `useRegisterWithInvitation`, `useAuthConfig` |
| Forgot / reset password | `/forgot-password`, `/reset-password` | `useForgotPassword`, `useValidatePasswordReset`, `useResetPassword`, `useAuthConfig` |
| Catalog (when the AI catalog is enabled; `/public/*` redirects here) | `/catalog` | `usePublicCatalogSummary` |
| Catalog Agents | `/catalog/agents`, `/catalog/agents/:id` | `usePublicCatalogAgents`, `usePublicCatalogAgent`, `useAddCatalogItemToAccount` |
| Catalog Skills | `/catalog/skills`, `/catalog/skills/:id` | `usePublicCatalogSkills`, `usePublicCatalogSkill`, `useAddCatalogItemToAccount` |
| Catalog Prompts | `/catalog/prompts`, `/catalog/prompts/:id` | `usePublicCatalogPrompts`, `usePublicCatalogPrompt`, `useAddCatalogItemToAccount` |
| Catalog MCP Servers | `/catalog/mcp-servers`, `/catalog/mcp-servers/:id` | `usePublicCatalogMCPServers`, `usePublicCatalogMCPServer`, `useAddCatalogItemToAccount` |
| Catalog Custom Tools | `/catalog/tools`, `/catalog/tools/:id` | `usePublicCatalogCustomTools`, `usePublicCatalogCustomTool`, `useAddCatalogItemToAccount` |
