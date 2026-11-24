# Frontend directory map

Map of `frontend/src/`. Rules for working in the frontend live in `frontend/AGENTS.md`; every route and the page it renders is in `frontend-pages.md`. Directories list their main files, not every file.

## Directory Map

```
frontend/src/
├── main.tsx                 ← React root (QueryClient, BrowserRouter with the base path, PlatformProvider, AppIntlProvider, toasts)
├── App.tsx                  ← Route definitions and route gates (ProtectedRoute, TermsGate, AdminRoute, ChatGate, FlowBuilderGate, XtmOneRoute); protected routes wrap AppLayout
├── index.css                ← Tailwind entry: design-system tokens + the three product tokens + app utilities (`frontend-design-system.md`)
├── fonts.ts                 ← Self-hosted brand fonts (fontsource), no Google Fonts request
├── vite-env.d.ts            ← Vite client types
│
├── components/
│   ├── ui/                  ← Reusable primitives, among them: button, card, input, badge, switch, select, searchable-select (`SearchableSelect`: the picker of any list fed by runtime data, sorted and searchable), model-select (+ model-select-options), entity-select-options, dialog, confirm-dialog, confirm-delete-dialog, run-card, stat-card, provider-logo, prompt-generator, agent-flow (`orientation` horizontal | vertical), schedule-view (`ScheduleView`: the schedule of a set of agents, rendered by the Schedule tab, the playground and fleet views), beam-particle, skeleton, error-boundary, setup-guide-dialog, tag-filter, tag-input, tag-list, company-managed-badge, company-managed-banner, company-managed-switch, search-input, empty-state, toast, tool-scope-dialog, icon-picker, agents-using-card, detail-tabs, tool-test-dialog, read-only-badge, activity-empty-state, entity-activity-tab, entity-table, table-sort-header (the sortable `<th>` shared by `EntityTable` and the server-sorted observability tables), data-table (`DataTable`: a loaded list sorted, searched, paginated top and bottom and exported as CSV), charts/ (`ChartCard`, `RankedBars`), sparkline, pagination, key-value-editor, back-button, element-type-editor, assignment-run-list, timestamp-grid, not-found-state, principal-sharing-card (share an entity with people and groups), resource-sharing-card ("Sharing and access" of a lent resource), channel-picker, channel-icon, entity-config-card, shared-badge, shared-banner, animated-number, diff-badge, delegation-flow-dialog, annotation-buttons (`AnnotationButtons`: thumbs up / down + comment on rated agent work other than a chat message: a run, a background task, a delegation), annotation-dialog (`AnnotationDialog`: the one rating dialog, comment and per-criterion scores; input rules in `lib/feedback-criteria.ts`), tooltip, category-filter, segmented-filter, view-toggle, visibility-filter, favorite-star, list-control-bar (with `SelectionBox`, the select box of the rows it selects), date-range-filter (`DateRangeFilter`: a filter row's presets or From / To days, resolved by the pure `lib/date-range.ts`), property-filter, resource-hub (cards + "recently modified" body shared by the nav-group hub pages), enterprise-upsell (the XTM One `EditionBadge`, the Community Edition upsell dialog / notice / lock tooltip and the license activation form; see "Editions" in `frontend-design-system.md`), emergency-stop-badge (the red "Emergency stop" label replacing "Paused" on assignments whose `paused_reason` is `panic_stop`; predicate in `lib/emergency-stop.ts`), panel-resize-handle (the drag handle of a panel sized by `useResizableWidth`), public-link-card (`PublicLinkCard`: the public link of a shared deck or video in its Share dialog)
│   ├── layout/              ← AppLayout (sidebar + header + outlet), Sidebar, nav-model (the nav tree as data: single source for the sidebar and ⌘K), NavRowBody (the inside of a `NavbarItem asChild` nav row, so every slotted row sits on the rail's icon axis), product-switcher (the suite the header's switcher lists), MadeByFiligran (the nav's foot), CommandPalette (⌘K), app.commands.ts, AboutDialog (opened from the profile menu), Header, layout-metrics (fixed chrome heights), EmergencyStopDialog (the profile menu's "panic button": scope choice for admins + a random 6-char challenge code retyped before the red confirm enables, then a "why?" form once the stop is done; the stop never waits for a reason), EmergencyResumeDialog (lifting the stop; the reason is mandatory and asked before acting), EmergencyStopBanner (page-wide red notice while `GET /panic/status` says a stop is in effect: counts, who / when / why, and a "Resume" CTA opening `EmergencyResumeDialog`; the profile-menu entries are the second path), ManualTriggerButton, PublicLayout (catalog shell)
│   ├── chat-panel/          ← The shared panel as XTM One hosts it, for every surface that shows a conversation: XtmChatPanel (the package root and stylesheet, lazy pages only), LazyXtmChatPanel (XtmChatPanel as its own never-prefetched chunk, for a surface inside a prefetched page: the studio rails), endpoints (the `/api/v1/web` routes, a studio rail's, the agent detail's read-only view's), panel-host (token, language, accent and font), markers (the answer markers' cards), chart-code-block (an answer's `xtm-chart` fence as the panel's `codeBlockRenderers`), FileChips, InlineFileContent (a stored answer's marker cards outside the panel: the run detail), MarkerCardBoundary (a failing card stays in its place), MessageParts (what XTM One shows under a reply), message-text (how a message reads)
│   ├── chat/                ← ChatMarkdown, WaitItemList (what an announced wait watches), ChatWaitingGame, MessageAttributionLine, FileConversationDialog, chat-actions (`ChatActionsContext`: what a transcript block may ask of the chat page, e.g. an investigation's "Generate executive summary")
│   ├── companion/           ← The mobile companion (the app on a PHONE, told by the device - `isPhoneDevice`, Client Hints then user agent - never by the screen width): `CompanionShell` replaces `AppLayout`'s shell with three tabs (Today = the platform's home dashboard for its mode, Inbox = the server notification feed with waiting runs answered in place, Ask = the chat) and answers any other page "made for a computer"; `InstallAppBanner` offers the installable web app (`lib/pwa.ts` holds the browser's install prompt and registers `public/sw.js`, which caches nothing and shows pushes, sends a notification button's one-time token, and keeps a share until `/share` takes it); `EnableNotificationsCard` + `usePhoneLink` link this phone for Web Push (`lib/push.ts`). `AppLockGate` (in `ProtectedRoute`) mounts NOTHING on a phone until the PIN lock is unlocked (`lib/app-lock.ts`: a 423 fires `APP_LOCKED_EVENT`, and so does a chat stream's `app_locked` frame (`XtmChatPanel`), the PIN's 401 signs out with `session_ended=pin`). The bell (`Header.tsx`) and the Inbox read ONE feed, `GET /notifications` (`hooks/api/useNotificationApi.ts`): routes and read state are the server's
│   ├── custom-dashboard/    ← Agent-built dashboards: `normalizeDashboard` (`spec.ts`, the defensive reader of a stored spec), one component per widget kind (`widgets.tsx`), `CustomDashboardView` (container-query grid, an error boundary per widget), `CustomDashboardCard` (the `[[DASHBOARD:id]]` chat card), `ask.ts` (an ask control prefills a new chat's composer, never sends), `charts/` (the lazy renderers of a widget's data: `PlatformChart` dispatches to the XY, pie, scatter / bubble / punchcard, single value / gauge and table renderers; options read by `chart-options.ts`, Splunk's dynamic options subset parsed and evaluated by `dynamic-options.ts` - never `eval` -, colours from the design-system tokens of `colors.ts` only)
│   ├── investigation/       ← InvestigationBlock (chat surface for an [[INVESTIGATION:id]] marker), GoalPlanView (the default rendering of every run: objective, world state, planner, actions as a timeline with their steps, counters, the cited report and its full-report dialog; pack-agnostic, every label from the server's plan through `t_i18n`), goal-state (pure derivation of an action's status and a predicate's truth from the steps, `yarn test:unit`), InvestigationMessageText (a message whose text is the run's report shows the block alone), ContinuationPanel, SourcePackPicker, CitedAnswer, pack-builder/
│   ├── presentation/        ← PresentationCard (chat surface for a [[PRESENTATION:id]] marker), figure-build (a deck figure built in frame by frame for a video scene), PresentationDeck (@revealjs/react renderer), layouts/ (one component per slide layout of `layout-options.ts`, eight), sanitize (DOMPurify strict profile for report `rich_text` blocks; a deck has no HTML field), presentation.css (deck themes from platform tokens), speaker-sync (BroadcastChannel bridge stage <-> speaker window), speaking-time / notes-text (WPM estimate + markup stripping, mirrored by the backend), SlidePreview / SpeakerNotes / useSpeakerTimer + speaker.css (speaker window and notes handout), the editor (PresentationEditorPanel, PresentationStudioRail, PresentationSidePanel, SlideInspector, useDeckAutosave), PresentationShareDialog
│   ├── report/              ← ReportCard, ReportView, ReportExportMenu, blocks, report.css
│   ├── studio/              ← StudioRail: the chat bound to the work beside it (a deck in the Presentation Studio, a video in the Video Studio editor); StudioChrome (the bars both studios share: back to the library, title, studio toggle, share, loading and unavailable screens)
│   ├── video-studio/        ← Video Studio: VideoCard (+ video-card-context: a card for the video on the stage collapses to a receipt), VideoPlayer, NewVideoDialog + useNewVideoChoices, VideoShareDialog, VideoDownloadMenu + video-downloads (the download entries of every surface), video-links, JobStatusBadge, VideoTemplateRules, VideoStudioRail; editor/ (Timeline, PreviewStage, SceneInspector, MediaPanel, RendersPanel + RenderCleanupDialog, VideoSettings, MusicSettings, QualityStrip, useVideoEditor)
│   ├── browser-profiles/    ← BrowserProfileCard (a profile, its session, sign-in and pending MFA answered in place), BrowserProfileDialog (create / edit; write-only credentials, TOTP secret), browser-profile-format (labels and pure helpers)
│   ├── dashboard-grid/      ← The grid of widgets each person arranges, shared by the CTEM command center and the Copilot dashboard: DashboardGrid (+ CustomizeButton, edit mode, stacked below `GRID_MIN_WIDTH`), layout.ts (grid bounds, `WidgetRegistry`, saved layout → drawn layout), widget-density.ts (cell height, list capacity, KPI cards), widget-size(-context) (`useWidgetSize`, `WidgetSized`), grid-keyboard.ts (keyboard move / resize), dashboard-grid.css
│   ├── workspaces/          ← WorkspaceActionsMenu (the one actions menu of a workspace: make default, knowledge bases, share, archive / restore, delete; rendered by the Workspaces tab and the chat sidebar, each adding its own entries), WorkspaceKnowledgeBaseDialog, WorkspaceShareDialog
│   ├── arcade/              ← The waiting arcade (ArcadeProvider, ArcadeOverlay, ArcadeLeaderboard, engine/, arcade.commands.ts)
│   ├── auth/                ← DesktopSignIn, MfaCodeStep, SessionTimeoutWarning
│   ├── api-keys/            ← AdminApiKeyRow, ApiKeyBadges, useApiKeyRevocation
│   ├── traces/              ← Trace explorer pieces (trace-explorer, trace-tree, trace-timeline, trace-graph, span-inspector, trace-peek-drawer, trace-search)
│   ├── integrations/        ← Integration catalog browser, provider details dialog, openapi/
│   ├── work-items/          ← Scheduled work item card, details and actions
│   ├── desktop/             ← LocalPath
│   ├── feedback/            ← ReactionFeedbackSettings
│   ├── evaluation/          ← Among the evaluation pages' parts, the placebo control: PlaceboConfigCard + PlaceboConfigFields (the Configuration tab's card), PlaceboRunDialog (estimate, warnings, refusals, write-tools confirmation; sends the estimate's seed back), PlaceboComparisonsCard (the Overview's comparison block), PlaceboArmTag (a comparison run's arm), PlaceboSnapshotCard (a placebo run's placebo); pure rules in `lib/placebo.ts`
│   └── InviteUserDialog.tsx ← Invitation link dialog (admin)
│
├── pages/                   ← One file per routed page (`<Page>.tsx`), its tabs and parts in a kebab-case folder, its ⌘K commands in `<Page>.commands.ts`. Selection below; full route list in frontend-pages.md
│   ├── Dashboard.tsx        ← / (the dashboard outside XTM One mode; `HomeRoute` picks AiDisabledLanding, a custom dashboard, XtmOneDashboard or Dashboard)
│   ├── xtm-one/             ← XtmOneDashboard, CtemCommandCenter, CtemStageDetail (/ctem/:stage), ExposureBriefing (/briefing); ctem/ holds the command center's widgets (dashboard-layout.ts: `CTEM_WIDGETS` and their default places; widget-density.ts: its widgets' density rules) on the shared `components/dashboard-grid/`, WelcomeLanding, dashboard notices
│   ├── dashboard/           ← The Copilot dashboard's grid: copilot-dashboard-layout.ts (`COPILOT_WIDGETS` and their default places), copilot-density.ts (its status header's density)
│   ├── ChatPanelPage.tsx    ← /chat (the shared `@filigran/chatbot` panel over the web chat's HTTP + SSE routes, `/api/v1/web`, with this app's own content in its slots)
│   ├── chat-panel/          ← ChatPanelPage's own parts: ComposerParts (the composer toolbar), ConversationBanner (what holds for the whole conversation), useSessionTools, useTurnFrames (the frames the panel does not render)
│   ├── chat/                ← BackgroundTasksBanner, AwaitedWorkBanner (the Video Studio work a conversation waits on), InterruptedTurnNotice (a last turn that stopped before its answer, with Resume / Dismiss), SessionToolPicker, session-tools (an agent's / the General Assistant's default tools), ConversationSidebar, ConversationFilesPanel, ShareConversationDialog, MessageFeedbackButtons, attachments (a chat file's type label, the conversation's files by id), answerMarkers (an answer's markers outside the panel, read with the panel's grammar; `answer-markers.contract.json` holds the samples the backend is tested on too)
│   ├── Work.tsx             ← /work (hub: what agents PRODUCE: operations, presentations, files)
│   ├── AgentStudio.tsx      ← /agent-studio (hub: what agents are MADE OF: knowledge, prompts, templates, skills)
│   ├── ToolsAndIntegrations.tsx ← /tools-and-integrations (hub: what agents ACT THROUGH: MCP servers first, then tools, integrations, variables)
│   ├── Operations.tsx       ← /operations/<tab> (what is running or past: Executions = TaskHistory, BackgroundTasks, Shared State, Worksets)
│   ├── Tasks.tsx            ← /tasks/<tab> (what to do and follow: Work Items `/tasks/work-items[/<id>]`, Value `/tasks/value[/<table>]`, Workspaces `/tasks/workspaces[/<id>]`; links built by lib/task-routes.ts, no alias of a former path)
│   ├── operations/          ← tabs.ts (the tab list), WorkItemsTab, SharedStateTab, WorksetsTab, WorkspacesTab
│   ├── TaskHistory.tsx      ← The Executions tab of /operations
│   ├── Presentations.tsx    ← /presentations (deck library, paged by the server: Mine / Shared / Company, present / edit-with-assistant / share / duplicate / delete)
│   ├── VideoStudio.tsx      ← /videos, behind `VIDEO_STUDIO` (the Video Studio library: Videos and Media tabs; browser profiles have their own page)
│   ├── VideoEditor.tsx      ← /videos/:id, behind `VIDEO_STUDIO` (the full-screen video editor, outside AppLayout)
│   ├── VideoFigureStaticRender.tsx ← /videos/figures/static: the page a video's figure scene is drawn from by the render's headless browser (one deck block, built in frame by frame); not a user-facing page
│   ├── VideoPublic.tsx      ← /public/videos/:token (a video opened by its public link, no session)
│   ├── BrowserProfiles.tsx  ← /browser-profiles (saved sign-ins the agents' browser and the Video Studio open; Tools & Integrations)
│   ├── Files.tsx            ← /files (cross-context file repository: every file the user may download, from chats and assignment runs, filterable by context / origin / ownership, groupable by conversation or run, deep-linkable with `?file=<id>`)
│   ├── Agents.tsx           ← /agents (agent list)
│   ├── AgentDetail.tsx      ← /agents/:id/:tab?/:section? (shell → agent-detail/ tabs)
│   ├── AgentChatConversation.tsx ← /agents/:id/chats/:conversationId (conversation detail)
│   ├── agent-detail/        ← tabs.ts, OverviewTab, ConfigTab, MemoryTab, DelegationsTab, ChatsTab, ActivityTab, VersioningTab, FeedbackTab, EvaluationTab, BudgetTab, ObservabilityTab, PersonaImprovementsCard, AddAssignmentPanel; config/ (the Configuration tab's sections and cards, among them BrowserProfileSelectorCard: the browser profiles the agent's browser tool may open)
│   ├── AgentFlow.tsx        ← /agents/flow (handover topology graph)
│   ├── NewAgentMode.tsx     ← /agents/new (choose how to create: wizard, advanced form, import)
│   ├── NewAgent.tsx         ← /agents/new/advanced (create from template or scratch)
│   ├── NewAgentWizard.tsx + new-agent-wizard/ ← /agents/new/wizard
│   ├── NewAgentImport.tsx   ← /agents/new/import
│   ├── Flows.tsx            ← /flows/:tab? (shell → flows/ tabs: Flows library, Live Map, Schedule)
│   ├── flows/               ← LibraryTab (flow list, default tab), MapTab, ScheduleTab (thin wrapper of `ScheduleView`, reads `?agent=`)
│   ├── flow-builder/        ← /flows/library/:id, the visual flow builder canvas: `index.tsx` shell, `FlowCanvas` (@xyflow/react), `NodeLibraryPanel`, `NodeConfigPanel` + `VariableInput`, `FlowNode`, `TypedEdge`, `QuickAddMenu`, `BuilderToolbar`, `VersionHistoryPanel`, `RunsPanel`, `useFlowEditor` (reducer + 50-step undo/redo), `graph-adapter` (FlowGraph ⇄ React Flow), `connection-rules` (drag-time mirror of the server matrix), `auto-layout` (dagre), `flow-builder.css` (React Flow theming through platform tokens)
│   ├── Integrations.tsx     ← /integrations (integration list)
│   ├── IntegrationDetail.tsx ← /integrations/:id/:tab? (shell → integration-detail/ tabs)
│   ├── integration-detail/  ← OverviewTab, ConfigTab, ActivityTab, OpenAPISpecTab, NovaRulesTab, ConfigurationCard, StatusCard, EventTriggersCard, WebhooksCard, ToolsCard, KBsUsingCard, ConnectedAccountsCard, YourAccountCard, provider-tabs (provider-contributed tabs), openrouter/
│   ├── integrations/        ← PlatformGate, usePlatformFor
│   ├── NewIntegration.tsx   ← /integrations/new (create integration)
│   ├── KnowledgeBases.tsx   ← /knowledge (KB list)
│   ├── KnowledgeBaseDetail.tsx ← /knowledge/:id/:tab? (shell → knowledge-base-detail/ tabs)
│   ├── knowledge-base-detail/ ← OverviewTab, DocumentsTab, SourcesTab, ConfigTab, ActivityTab, TestSearchPanel
│   ├── NewKnowledgeBase.tsx ← /knowledge/new (create KB)
│   ├── MCPServers.tsx       ← /mcp-servers (MCP server list)
│   ├── MCPServerDetail.tsx  ← /mcp-servers/:id/:tab? (shell → mcp-server-detail/ tabs)
│   ├── mcp-server-detail/   ← OverviewTab, ConfigTab, ActivityTab, NewServerForm, PresetCard, presets
│   ├── CustomTools.tsx      ← /tools (custom tool list)
│   ├── CustomToolDetail.tsx ← /tools/:id/:tab? (shell → custom-tool-detail/ tabs)
│   ├── custom-tool-detail/  ← OverviewTab, ConfigTab, ActivityTab, NewToolForm, BuiltinToolDetail, BuiltinToolsCard, CustomToolCodeGenerator, schema-utils
│   ├── tools/               ← Tool approval gates (ToolApprovalList, ToolGateCard, ToolGateRow, ToolGateBadge, useToolGateSummary)
│   ├── Skills.tsx           ← /skills (agent skill list with search/tag filtering)
│   ├── SkillDetail.tsx      ← /skills/:id/:tab? (shell → skill-detail/ tabs)
│   ├── skill-detail/        ← InstructionsTab, ResourcesTab, CatalogTab, VersioningTab, ActivityTab, NewSkillForm, PrivateSourceFields
│   ├── Prompts.tsx          ← /prompts (prompt list)
│   ├── PromptDetail.tsx     ← /prompts/:id/:tab? (shell → prompt-detail/ tabs)
│   ├── prompt-detail/       ← OverviewTab, ConfigTab, VersioningTab, ActivityTab, NewPromptForm
│   ├── Templates.tsx / TemplateDetail.tsx + template-detail/ ← /templates, /templates/:id/:tab? (template containers: OverviewTab, ConfigTab, AssetsTab)
│   ├── AssignmentDetail.tsx ← /assignments/:id/:tab? (shell → assignment-detail/ tabs)
│   ├── assignment-detail/   ← OverviewTab, PromptTab, ChannelsTab, ActivityTab, ConfigCard, TriggerCard, PromptCard, ParametersCard, RunAsCard, PausedBanner, IntegrationBindingsCard, ElementBindingsCard, AssignmentToolScopeLoader
│   ├── RunDetail.tsx        ← /runs/:id (assignment run detail)
│   ├── Settings.tsx         ← /settings/:tab?/:section? (shell → settings/ sections)
│   ├── settings/            ← GeneralAssistantSection (+ GeneralAssistantSkills: the company-managed skills the General Assistant carries, a section of its Configure panel, filtered by name and description, with ga-skills.ts for the catalog and the choice; ResourceToggleCard: the compact attach/detach card and the 1/2/3-column grid every resource section of that panel uses, its tooltip leading with the full name), GAHandoverPicker, MusicGenerationSection (Settings > AI models > Music generation), ProviderCard, ModelDirectory, ModelPricingSection, DepartmentsSection (Settings > Observability > Departments: the department mapping, its preview, the directory import and "apply to history"), QuotasSection, SSOConfigSection, EmergencyStopTokenSection (Settings > Security > Emergency stop: generate / rotate / revoke the `xes-…` token, shown once, with the printable procedure from `lib/emergency-procedure.ts`), OAuthDefaultsSection, ObservabilitySection, ChannelManagement, IntegrationsSection, IntegrationCatalogSection, MemoryPurgeSection, MCPEndpointSection, FeedbackCriteriaSection, ToolApprovalsSection, XtmHubRegistrationCard, SecuritySection (the Security tab's URL-backed sections: audit / options / approvals / authentication / api-keys / emergency / memory), SecurityAuditSection + security-audit.ts (the `GET /security-audit` report: findings and security controls), SecurityOptionsSection, PlatformModelSetting (one platform-wide model choice in Settings → AI: routing, decision model)
│   ├── Profile.tsx + profile/ ← /profile/:tab? (profile, tool approvals, channels & bots, agent accounts, memory, quotas, API keys, MCP endpoint); /profile/agent-accounts/:accountId/:tab? opens one agent account
│   ├── Desktop.tsx + desktop/ ← /desktop (XTM One Desktop runners); RunnerDetail.tsx ← /desktop/runners/:id
│   ├── Relays.tsx / RelayGroupDetail.tsx + relay-group-detail/ ← /relays, /relays/:id/:tab?
│   ├── Users.tsx / UserDetail.tsx / NewUser.tsx ← /users, /users/:id/:tab?, /users/new (admin)
│   ├── Groups.tsx / GroupDetail.tsx / NewGroup.tsx ← /groups, /groups/:id/:tab?, /groups/new (detail and create admin-only)
│   ├── AuditLogs.tsx        ← /logs (audit logs, admin)
│   ├── Traces.tsx / TraceSessions.tsx / TraceDetail.tsx ← /traces, /trace-sessions (admin), /traces/:id/:tab?
│   ├── Evaluations.tsx, Datasets.tsx, … ← /evaluations/* (admin)
│   ├── AiAdoption.tsx + ai-adoption/ ← /ai-adoption/:tab? (admin)
│   ├── Analytics.tsx + analytics/ ← /analytics (admin)
│   ├── PresentationViewer.tsx ← /presentations/:id (standalone deck viewer, outside AppLayout; ?print-pdf = reveal.js PDF export); PresentationSpeaker, PresentationNotes, PresentationPublic, PresentationStaticRender alongside
│   ├── ReportViewer.tsx     ← /reports/:id (outside AppLayout); ReportStaticRender ← /reports/:id/static
│   ├── Login.tsx / Register.tsx / ForgotPassword.tsx / ResetPassword.tsx ← public auth pages
│   ├── TermsOfUse.tsx       ← Blocking Terms of Use consent screen. Not routed: `TermsGate` in App.tsx renders it in place of the whole authenticated tree while `termsOfUsePending()` (`lib/terms-of-use.ts`). Accepted once for the whole platform by an admin (`terms_of_use.can_accept`); non-admins see a "waiting for an administrator" state and can only sign out. Acceptance is recorded server-side on `PlatformSettings`, and the server gates every API call the same way.
│   └── public/              ← Catalog pages, routed under /catalog when the catalog is enabled (`/public/*` redirects to /catalog)
│       ├── components.tsx   ← Shared catalog UI (CatalogSearch, CatalogCard, DetailShell, CopyButton)
│       ├── PublicCatalog.tsx ← /catalog (catalog landing, summary counts)
│       ├── PublicAgents.tsx / PublicAgentDetail.tsx ← /catalog/agents, /catalog/agents/:id
│       ├── PublicSkills.tsx / PublicSkillDetail.tsx ← /catalog/skills, /catalog/skills/:id
│       ├── PublicPrompts.tsx / PublicPromptDetail.tsx ← /catalog/prompts, /catalog/prompts/:id
│       ├── PublicMCPServers.tsx / PublicMCPServerDetail.tsx ← /catalog/mcp-servers, /catalog/mcp-servers/:id
│       └── PublicCustomTools.tsx / PublicCustomToolDetail.tsx ← /catalog/tools, /catalog/tools/:id
│
├── hooks/
│   ├── api/                 ← TanStack Query hooks, one file per domain (selection below)
│   │   ├── useAgentApi.ts   ← Agents, templates, sub-agent tasks, memory, channel chats
│   │   ├── useAnnotationApi.ts ← Ratings of agent work: the caller's own rating of a target (`useMyAnnotation` / `useUpsertAnnotation` / `useDeleteAnnotation`), of a list page's rows in one request (`useMyAnnotations`, seeding each row's cache: rows render `AnnotationButtons batched`, never one fetch per row) and an agent's feedback (`useAgentAnnotations`, `useAgentAnnotationSummary`)
│   │   ├── usePersonaImprovementApi.ts ← The persona self-improvement loop: `usePersonaImprovements` (`/improvement-proposals?agent_id=`, polled while one is applied and watched), propose / approve / reject; rendered by `pages/agent-detail/PersonaImprovementsCard.tsx` on the Versioning tab
│   │   ├── useFeedbackCriteriaApi.ts ← Feedback criteria: `useFeedbackCriteria` (enabled ones for everyone, `includeDisabled` for admins) + admin create / update / delete
│   │   ├── useArcadeApi.ts  ← Waiting arcade: leaderboard, profile, game sessions (start / heartbeat / finish); never throws, never toasts
│   │   ├── useAuthApi.ts    ← Login, register, auth config
│   │   ├── useChannelBotApi.ts ← Channel bot CRUD, catalog, tools
│   │   ├── useChatApi.ts    ← Conversations, messages, studio bindings, `handleToolProgress` (a tool's progress frame: the caches it refreshes, then the host's `onToolProgress`)
│   │   ├── useCustomToolApi.ts ← Custom tool CRUD
│   │   ├── useFlowApi.ts    ← Flow CRUD, node catalog, graph validation, versions, import/export
│   │   ├── useGroupApi.ts   ← Group CRUD, membership
│   │   ├── useIntegrationApi.ts ← Integration CRUD, catalog, OAuth (redirect or device-code sign-in, implicit token intake); OpenAPI spec preview / detail / selection / refresh / replace, client certificate; `useIntegrationToolList()`: the tools an integration offers (catalog `available_tools`, or its own tools for a `spec_import` provider), for any tool list or count
│   │   ├── useKnowledgeBaseApi.ts ← KB CRUD, documents, sync
│   │   ├── useMCPServerApi.ts ← MCP server CRUD, test, tools
│   │   ├── useMiscApi.ts    ← Dashboard stats, activity, audit logs, misc
│   │   ├── usePanicApi.ts   ← Emergency stop: `useEmergencyStop` / `useEmergencyResume` (scope `self` | `platform`, invalidate the whole cache on success), `useEmergencyStopStatus` (polled while signed in; banner + profile menu)
│   │   ├── useSettingsApi.ts ← LLM providers, GA config, SSO, Langfuse, memory purge, MCP endpoint key management
│   │   ├── usePresentationApi.ts ← Deck reads + what the editor's autosave sends (`useUpdatePresentation`: the revision-checked `PATCH`, re-validated server-side like an agent's write) + collaboration hooks (versions, comments, `useDraftPresentationComment`: the composer's AI draft)
│   │   ├── useVideoStudioApi.ts ← Video Studio queries and mutations (projects, takes, clips, templates, renders, music, sharing, public links and the public video, media URLs)
│   │   ├── useBrowserProfilesApi.ts ← Browser profiles: list (polled while a sign-in runs or waits for an MFA code), save, delete, sign in / out, test sign-in, session check, MFA code and screenshot
│   │   ├── useFileApi.ts    ← Cross-context file repository (`useFiles` with context / origin / ownership / search / pagination, `useDeleteFile`) behind Work > Files
│   │   ├── usePromptApi.ts  ← Prompt CRUD
│   │   ├── useSkillApi.ts   ← Agent skill CRUD, refresh
│   │   ├── useAssignmentApi.ts ← Assignment CRUD, runs, triggers
│   │   ├── useMemoryApi.ts  ← Agent memory hooks, built by the `makeMemoryHooks` factory
│   │   └── usePublicCatalogApi.ts ← Catalog reads + add-to-account
│   ├── useAuth.ts           ← Auth state hook (user, isAdmin, logout)
│   ├── usePlatformConfig.tsx ← PlatformProvider + platform mode / AI / chat / catalog switches
│   ├── useFeatureFlag.ts    ← Feature flag reads (`useFeatureFlagResolved`)
│   ├── useActivityStream.ts ← SSE connection for real-time activity
│   ├── useClickOutside.ts   ← Click-outside hook for dropdowns/popovers
│   ├── useAuthenticatedSrc.ts ← Fetch a JWT-protected file/image URL and expose it as an object URL (chat previews + deck slide images)
│   ├── useDebounce.ts       ← Generic value debounce hook
│   ├── usePrefersReducedMotion.ts ← Live `prefers-reduced-motion` media query (the waiting arcade honours it)
│   ├── useBuiltinTools.ts   ← The built-in tool registry (`BUILTIN_TOOLS`) as a picker renders it: offered suites per surface and per chat, deployment gates applied, and what the agent configuration grid lists
│   ├── useDashboardLayoutState.ts ← The layout a widget-grid dashboard draws and how a change is saved to the profile (local override while preference writes are in flight)
│   ├── useResizableWidth.ts ← User-resizable panel width: bounds, pointer-capture drag, window-splitter keyboard, double-click reset, remembered preference (the studio rail and, with `edge: "start"`, the viewer's `PresentationSidePanel`)
│   ├── useEntityFilter.ts   ← Shared search + tag + visibility + `has:` property filtering for entity list pages
│   ├── useEntityListScope.ts ← The `scope` an entity list hook sends: `all` only for an admin who asked for it (`lib/entity-list-scope.ts`)
│   ├── useViewMode.ts       ← Persisted grid ⇄ list view preference for asset index pages
│   ├── useFavoriteAgents.ts ← Per-user starred agents (`User.favorite_agent_ids`): star state, toggle, and the pin-first partition the /agents grid + list share
│   ├── useNavPrefs.tsx      ← Left-nav preferences (expanded groups, icon-rail mode) shared by Sidebar + AppLayout, persisted in localStorage
│   ├── useLibraryParams.ts  ← URL state of a library paged by the server (search, one filter, page) + `useClampPage` (Presentations, Video Studio)
│   ├── useSpeechToText.ts   ← Web Speech API hook for voice input
│   ├── useTabParam.ts       ← URL-backed tab state for detail pages
│   ├── useSectionParam.ts   ← URL-backed section state for sub-tabs within a tab
│   ├── useTraceListPaging.ts ← How a Traces / Trace Sessions page is read within the API's offset bound: keyset cursor, offset from either end, or not at all
│   └── useTick.ts           ← Periodic re-render hook (keeps timeAgo fresh)
│
├── lib/                     ← Pure helpers, most with a `*.test.ts` next to them (selection below)
│   ├── commands/            ← ⌘K command system: types (the contribution contract), registry (import.meta.glob aggregation + `when` filtering), CommandsProvider (handlers, `@` sections, context keys)
│   ├── api.ts               ← Fetch client with JWT, error handling, token management (`API_BASE` carries the URL path prefix)
│   ├── builtin-tool-offers.ts ← Which agent-only built-in suites an agent chat's session picker offers (the ones the agent carries, all of them for an admin or the owner, mirroring the server's session bound), and which suites the agent configuration grid lists and how it toggles them (a switched-off one only while the agent carries it, removable, never added)
│   ├── base-path.ts         ← URL path prefix the platform is served under (BASE_PATH): `withBasePath()` for browser sinks, `absoluteUrl()` for shareable links, `platformOrigin()` for a copy-paste base URL. Read from the `<meta name="base-path">` the backend writes into index.html, so only the built bundle served by the backend runs under a prefix: `vite dev` / `vite preview` always run at `/`
│   ├── challenge-code.ts    ← generateChallengeCode() / challengeMatches() for type-to-confirm dialogs (unambiguous alphabet, case-insensitive match)
│   ├── constants.ts         ← Shared constants (icons, statuses, provider names, cron presets, timezones, speech API detection, BUILTIN_TOOLS)
│   ├── entity-list-scope.ts ← The library set vs. every row the viewer may open: `effectiveListScope()`, `entityListKey()` / `entityListPath()` for the list hooks, `withBoundEntities()` for a picker's options
│   ├── entity-permissions.ts ← canManageEntity(), canViewAgentChats(), configLockReason(), isEntityShared() (re-exported by utils)
│   ├── oauth-share.ts       ← canAuthorizeIntegration() and the OAuth credential-sharing predicates (re-exported by utils)
│   ├── graph-utils.ts       ← Shared graph utilities (GRAPH_COLORS, BEAM_PARTICLE_KEYFRAMES)
│   ├── tip-text.ts          ← `capTipText()`: the shared Tip cuts a long string at a word with an ellipsis (280 characters), so a tooltip stays inside the viewport
│   ├── public-video.ts      ← `PUBLIC_VIDEO_ROUTE`, `isPublicVideoPath()`: a public video page never sends a dead session to sign-in
│   ├── safe-url.ts          ← safeHttpUrl(): an `http(s)` URL safe for an `href`, else `null`
│   ├── select-options.ts    ← The one order and the one word-by-word match of every picker (`SearchableSelect`)
│   ├── terms-of-use.ts      ← termsOfUsePending(): the one Terms of Use test
│   └── utils.ts             ← cn(), timeAgo(), formatDate(), formatDateTime(), formatShortDate(), formatTime(), formatShortDateTime(), formatCompactNumber(), formatKBStats(), pluralize(), stripMarkdown(), toggleArrayItem(), isFeatureEnabled(), model choice helpers, plus the re-exports above
│
├── i18n/                    ← react-intl layer (`AppIntlProvider`, message ids are the English strings), lang/ (en, es, fr, ja)
├── testing/                 ← providers.tsx: the providers main.tsx mounts, for component tests; design-system-warnings.ts (a design-system `console.warn` fails the test); design-tokens.ts (the token sheet a component test loads)
├── dev/                     ← PrimitiveGallery + gallery.tsx: the dev-only visual reference at /gallery.html, absent from the production bundle
├── assets/                  ← filigran-logo.svg
└── types/
    ├── index.ts             ← All TypeScript interfaces (User, Agent, Assignment, etc.)
    ├── traces.ts            ← Observability trace contracts
    ├── lucide-icon-catalog.d.ts
    └── vitest.d.ts          ← jest-dom matcher types for vitest's `expect`
```
