// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy, useEffect } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { getToken } from "@/lib/api";
import { rememberReturnPath } from "@/lib/session-return";
import { termsOfUsePending } from "@/lib/terms-of-use";
import { useAuth } from "@/hooks/useAuth";
import { useMe } from "@/hooks/api/useAuthApi";
import { AppLayout } from "@/components/layout/AppLayout";
import { ArcadeProvider } from "@/components/arcade/ArcadeProvider";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { PageSkeleton } from "@/components/ui/skeleton";
import { ChatFrame } from "@/components/chat-panel/ChatFrame";
import { SessionTimeoutWarning } from "@/components/auth/SessionTimeoutWarning";
import { AppLockGate } from "@/components/companion/AppLockGate";
import { EmptyState } from "@/components/ui/empty-state";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { lazyWithRetry, prefetchLazyChunks } from "@/lib/lazyWithRetry";
import { usePlatformConfig, useChatEnabled, useAICatalogEnabled, useAiEnabled, isXtmOneMode } from "@/hooks/usePlatformConfig";
import { useFeatureFlagResolved } from "@/hooks/useFeatureFlag";
import type { FeatureFlag } from "@/lib/utils";

/* Eager imports — auth pages render before the app shell exists, and the
 * license splash renders outside the Suspense boundary. */
import LicenseRequired from "@/pages/LicenseRequired";
import TermsOfUse from "@/pages/TermsOfUse";
import Login from "@/pages/Login";
import Register from "@/pages/Register";
import ResetPassword from "@/pages/ResetPassword";
import ForgotPassword from "@/pages/ForgotPassword";
import PublicLayout from "@/components/layout/PublicLayout";

/* Lazy routes — each page becomes its own chunk, loaded on first navigation.
 * All chunks are additionally warmed during idle time (see prefetchLazyChunks
 * in App), so the Suspense fallback rarely paints in practice. */
const Dashboard = lazyWithRetry(() => import("@/pages/Dashboard"));
/* The /chat page, on the shared @filigran/chatbot panel: its own chunk, never
 * prefetched, so the panel and its stylesheet load only where it is shown. */
const ChatPanelPage = lazyWithRetry(() => import("@/pages/ChatPanelPage"), { prefetch: false });
const AiDisabledLanding = lazyWithRetry(() => import("@/pages/AiDisabledLanding"));
/* The XTM One dashboard is a sibling top-level chunk, NOT nested inside the
 * Dashboard chunk — the mode is decided at the route level (platformConfig is
 * already resolved there), so the home page costs a single chunk download
 * instead of two sequential ones. */
const XtmOneDashboard = lazyWithRetry(() => import("@/pages/xtm-one/XtmOneDashboard"));
const CtemStageDetail = lazyWithRetry(() => import("@/pages/xtm-one/CtemStageDetail"));
const CtemGovernance = lazyWithRetry(() => import("@/pages/xtm-one/CtemGovernance"));
const ExposureBriefing = lazyWithRetry(() => import("@/pages/xtm-one/ExposureBriefing"));
const CtemCases = lazyWithRetry(() => import("@/pages/xtm-one/CtemCases"));
const CtemSignals = lazyWithRetry(() => import("@/pages/xtm-one/CtemSignals"));
const CtemCaseDetail = lazyWithRetry(() => import("@/pages/xtm-one/CtemCaseDetail"));
const CtemInventory = lazyWithRetry(() => import("@/pages/xtm-one/CtemInventory"));
const CtemAssetDetail = lazyWithRetry(() => import("@/pages/xtm-one/CtemAssetDetail"));
const CtemSoftwareDetail = lazyWithRetry(() => import("@/pages/xtm-one/CtemSoftwareDetail"));
/* Hub pages for the three nav groups that have one — the parent label in the
 * sidebar is a real destination, not just an expand/collapse row. */
const Work = lazyWithRetry(() => import("@/pages/Work"));
const AgentStudio = lazyWithRetry(() => import("@/pages/AgentStudio"));
const ToolsAndIntegrations = lazyWithRetry(() => import("@/pages/ToolsAndIntegrations"));
/* The cross-context file repository under Work. */
const Files = lazyWithRetry(() => import("@/pages/Files"));
/* The results page of the top bar search (#11201). */
const Search = lazyWithRetry(() => import("@/pages/Search"));
const Agents = lazyWithRetry(() => import("@/pages/Agents"));
const NewAgentMode = lazyWithRetry(() => import("@/pages/NewAgentMode"));
const NewAgentImport = lazyWithRetry(() => import("@/pages/NewAgentImport"));
// Lazy on purpose: the wizard's canvas pulls `@xyflow/react` in with it.
const NewAgentWizard = lazyWithRetry(() => import("@/pages/NewAgentWizard"));
const NewAgent = lazyWithRetry(() => import("@/pages/NewAgent"));
const AgentDetail = lazyWithRetry(() => import("@/pages/AgentDetail"));
/* An agent's conversation, read-only on the same panel: never prefetched either. */
const AgentChatConversation = lazyWithRetry(() => import("@/pages/AgentChatConversation"), { prefetch: false });
const AgentFlow = lazyWithRetry(() => import("@/pages/AgentFlow"));
const Evaluations = lazyWithRetry(() => import("@/pages/Evaluations"));
const Health = lazyWithRetry(() => import("@/pages/Health"));
const EvaluationDetail = lazyWithRetry(() => import("@/pages/EvaluationDetail"));
const EvaluationRunDetail = lazyWithRetry(() => import("@/pages/EvaluationRunDetail"));
const EvaluationRunCompare = lazyWithRetry(() => import("@/pages/EvaluationRunCompare"));
const EvaluationCompare = lazyWithRetry(() => import("@/pages/EvaluationCompare"));
const EvaluationBundleReview = lazyWithRetry(() => import("@/pages/EvaluationBundleReview"));
const NewEvaluation = lazyWithRetry(() => import("@/pages/NewEvaluation"));
const Datasets = lazyWithRetry(() => import("@/pages/Datasets"));
const DatasetDetail = lazyWithRetry(() => import("@/pages/DatasetDetail"));
const Flows = lazyWithRetry(() => import("@/pages/Flows"));
const FleetPage = lazyWithRetry(() => import("@/pages/flows/FleetPage"));
const Fleets = lazyWithRetry(() => import("@/pages/Fleets"));
const AgentsHub = lazyWithRetry(() => import("@/pages/AgentsHub"));
// Lazy on purpose: `@xyflow/react` (~150 kB) must stay out of the entry chunk.
const FlowBuilder = lazyWithRetry(() => import("@/pages/flow-builder"));
const Integrations = lazyWithRetry(() => import("@/pages/Integrations"));
const BrowserProfiles = lazyWithRetry(() => import("@/pages/BrowserProfiles"));
const IntegrationDetail = lazyWithRetry(() => import("@/pages/IntegrationDetail"));
const NewIntegration = lazyWithRetry(() => import("@/pages/NewIntegration"));
const OAuthImplicitCallback = lazyWithRetry(() => import("@/pages/OAuthImplicitCallback"));
const KnowledgeBases = lazyWithRetry(() => import("@/pages/KnowledgeBases"));
const NewKnowledgeBase = lazyWithRetry(() => import("@/pages/NewKnowledgeBase"));
const KnowledgeBaseDetail = lazyWithRetry(() => import("@/pages/KnowledgeBaseDetail"));
const WorkspaceProject = lazyWithRetry(() => import("@/pages/WorkspaceProject"));
const KnowledgeDocuments = lazyWithRetry(() => import("@/pages/KnowledgeDocuments"));
const Operations = lazyWithRetry(() => import("@/pages/Operations"));
const RunDetail = lazyWithRetry(() => import("@/pages/RunDetail"));
const AssignmentDetail = lazyWithRetry(() => import("@/pages/AssignmentDetail"));
const Intents = lazyWithRetry(() => import("@/pages/Intents"));
const Traces = lazyWithRetry(() => import("@/pages/Traces"));
const TraceSessions = lazyWithRetry(() => import("@/pages/TraceSessions"));
const TraceDetail = lazyWithRetry(() => import("@/pages/TraceDetail"));
const Analytics = lazyWithRetry(() => import("@/pages/Analytics"));
const Tasks = lazyWithRetry(() => import("@/pages/Tasks"));
const ValueMethodology = lazyWithRetry(() => import("@/pages/ValueMethodology"));
const Skills = lazyWithRetry(() => import("@/pages/Skills"));
const SkillDetail = lazyWithRetry(() => import("@/pages/SkillDetail"));
const MCPServers = lazyWithRetry(() => import("@/pages/MCPServers"));
const MCPServerDetail = lazyWithRetry(() => import("@/pages/MCPServerDetail"));
const CustomTools = lazyWithRetry(() => import("@/pages/CustomTools"));
const CustomToolDetail = lazyWithRetry(() => import("@/pages/CustomToolDetail"));
const Variables = lazyWithRetry(() => import("@/pages/Variables"));
const Prompts = lazyWithRetry(() => import("@/pages/Prompts"));
const InvestigationPacks = lazyWithRetry(() => import("@/pages/InvestigationPacks"));
const InvestigationPackDetail = lazyWithRetry(() => import("@/pages/InvestigationPackDetail"));
const PromptDetail = lazyWithRetry(() => import("@/pages/PromptDetail"));
/* The deck viewer pulls reveal.js in with it — a chunk of its own, loaded only
 * when a presentation is actually opened. */
const PresentationViewer = lazyWithRetry(() => import("@/pages/PresentationViewer"));
const PresentationSpeaker = lazyWithRetry(() => import("@/pages/PresentationSpeaker"));
const PresentationAudience = lazyWithRetry(() => import("@/pages/PresentationAudience"));
const PresentationNotes = lazyWithRetry(() => import("@/pages/PresentationNotes"));
const VideoFigureStaticRender = lazyWithRetry(() => import("@/pages/VideoFigureStaticRender"));
const PresentationStaticRender = lazyWithRetry(
  () => import("@/pages/PresentationStaticRender"),
);
/* A deck opened by its public link, by someone with no account. */
const PresentationPublic = lazyWithRetry(() => import("@/pages/PresentationPublic"));
/* The audience page of a live poll (/v/CODE): no session, one poll only (#10157). */
const PollVote = lazyWithRetry(() => import("@/pages/PollVote"));
const DashboardPublic = lazyWithRetry(() => import("@/pages/DashboardPublic"));
/* The answer link of a decision (/decide/TOKEN, dev-docs/human-gates.md I.4): signed in as its recipient. */
const DecisionLink = lazyWithRetry(() => import("@/pages/DecisionLink"));
const VideoPublic = lazyWithRetry(() => import("@/pages/VideoPublic"));
/* The library lists decks without rendering any, so it stays in the shell. */
const Presentations = lazyWithRetry(() => import("@/pages/Presentations"));
const CustomDashboards = lazyWithRetry(() => import("@/pages/CustomDashboards"));
/* A component with props (the fallback dashboard): React's `lazy`, which keeps them. */
const HomeDashboard = lazy(() => import("@/components/custom-dashboard/HomeDashboard"));
const CustomDashboardViewer = lazyWithRetry(() => import("@/pages/CustomDashboardViewer"));
const CompanionInbox = lazyWithRetry(() => import("@/components/companion/CompanionInbox").then((m) => ({ default: m.CompanionInbox })));
const SharePage = lazyWithRetry(() => import("@/components/companion/SharePage").then((m) => ({ default: m.SharePage })));
/* Video Studio: the library, the player and the browser profiles. */
const VideoStudio = lazyWithRetry(() => import("@/pages/VideoStudio"));
const VideoEditor = lazyWithRetry(() => import("@/pages/VideoEditor"));
const DashboardStudio = lazyWithRetry(() => import("@/pages/DashboardStudio"));
const WidgetPacks = lazyWithRetry(() => import("@/pages/WidgetPacks"));
/* The report viewer carries its own stylesheet, so it is split out too. */
const ReportViewer = lazyWithRetry(() => import("@/pages/ReportViewer"));
const ReportStaticRender = lazyWithRetry(() => import("@/pages/ReportStaticRender"));
const DashboardStaticRender = lazyWithRetry(() => import("@/pages/DashboardStaticRender"));
const Templates = lazyWithRetry(() => import("@/pages/Templates"));
const NewTemplate = lazyWithRetry(() => import("@/pages/NewTemplate"));
const TemplateDetail = lazyWithRetry(() => import("@/pages/TemplateDetail"));
const Users = lazyWithRetry(() => import("@/pages/Users"));
const NewUser = lazyWithRetry(() => import("@/pages/NewUser"));
const UserDetail = lazyWithRetry(() => import("@/pages/UserDetail"));
const Groups = lazyWithRetry(() => import("@/pages/Groups"));
const NewGroup = lazyWithRetry(() => import("@/pages/NewGroup"));
const GroupDetail = lazyWithRetry(() => import("@/pages/GroupDetail"));
const AdminObjects = lazyWithRetry(() => import("@/pages/AdminObjects"));
const AuditLogs = lazyWithRetry(() => import("@/pages/AuditLogs"));
const AiAdoption = lazyWithRetry(() => import("@/pages/AiAdoption"));
const Settings = lazyWithRetry(() => import("@/pages/Settings"));
const TrackedOrganizationsAiDraftReview = lazyWithRetry(() => import("@/pages/settings/TrackedOrganizationsAiDraftReview"));
const Profile = lazyWithRetry(() => import("@/pages/Profile"));
/* One agent account: its activity, decisions and replies. */
const AgentAccountPage = lazyWithRetry(() => import("@/pages/profile/agent-accounts/AgentAccountPage"));
/* XTM One Desktop: download, connect a machine, manage the enrolled ones. */
const Desktop = lazyWithRetry(() => import("@/pages/Desktop"));
const RouteNotFound = lazyWithRetry(() => import("@/pages/RouteNotFound"));
/* One enrolled machine by id - the page the admin Objects browser opens. */
const RunnerDetail = lazyWithRetry(() => import("@/pages/RunnerDetail"));
const Relays = lazyWithRetry(() => import("@/pages/Relays"));
const RelayGroupDetail = lazyWithRetry(() => import("@/pages/RelayGroupDetail"));

const PublicCatalog = lazyWithRetry(() => import("@/pages/public/PublicCatalog"));
const PublicAgents = lazyWithRetry(() => import("@/pages/public/PublicAgents"));
const PublicAgentDetail = lazyWithRetry(() => import("@/pages/public/PublicAgentDetail"));
const PublicSkills = lazyWithRetry(() => import("@/pages/public/PublicSkills"));
const PublicSkillDetail = lazyWithRetry(() => import("@/pages/public/PublicSkillDetail"));
const PublicPrompts = lazyWithRetry(() => import("@/pages/public/PublicPrompts"));
const PublicPromptDetail = lazyWithRetry(() => import("@/pages/public/PublicPromptDetail"));
const PublicMCPServers = lazyWithRetry(() => import("@/pages/public/PublicMCPServers"));
const PublicMCPServerDetail = lazyWithRetry(() => import("@/pages/public/PublicMCPServerDetail"));
const PublicCustomTools = lazyWithRetry(() => import("@/pages/public/PublicCustomTools"));
const PublicCustomToolDetail = lazyWithRetry(() => import("@/pages/public/PublicCustomToolDetail"));

function ProtectedRoute({ children, returnHere = false }: { children: React.ReactNode; returnHere?: boolean }) {
  const token = getToken();
  if (!token) {
    /* A page opened from outside the app (an answer link in Teams or an
     * email) is where the sign-in lands again, SSO included. */
    if (returnHere) rememberReturnPath();
    return <Navigate to="/login" replace />;
  }
  return (
    <>
      {/* Every signed-in page follows the session's lifetime and idle timeout. */}
      <SessionTimeoutWarning />
      {/* On a phone, nothing mounts behind the companion's PIN lock. */}
      <AppLockGate>{children}</AppLockGate>
    </>
  );
}

/**
 * Blocking Terms of Use consent gate.
 *
 * Wraps `AppLayout` — deliberately OUTSIDE the layout, so while consent is
 * pending nothing else mounts: no page component, no sidebar queries, no
 * activity SSE stream. That matters because the server 403s every one of those
 * calls until the user accepts, and mounting them would only produce a wall of
 * failed requests behind the gate.
 *
 * It sits INSIDE the full-page `ErrorBoundary` (not above it) because it calls
 * `useMe()` and the app runs with `throwOnError: true` — a 5xx on `/auth/me`
 * has to land on a boundary, not white-screen the app. 401s never get here
 * (`api.ts` redirects to `/login`).
 *
 * `/auth/me` is exempt from the server-side gate and carries `terms_of_use`
 * inline, so the gate resolves from the boot request the app already makes.
 * While it's in flight we render a skeleton rather than the children —
 * rendering first and gating after would flash the app to a user who must not
 * see it.
 */
function TermsGate({ children }: { children: React.ReactNode }) {
  const { data: user, isLoading } = useMe();
  if (isLoading || !user) return <PageSkeleton />;
  const terms = user.terms_of_use;
  /* One test for every deployment, shared with what must not act behind the
   * consent screen (the waiting arcade): see `termsOfUsePending`. */
  if (termsOfUsePending(terms)) return <TermsOfUse terms={terms} />;
  return <>{children}</>;
}

/**
 * Route-level admin guard. Redirects non-admin users away from admin-only
 * pages (Users, Settings, Evaluations, Objects, Logs, AI Adoption) before any
 * admin UI renders, instead of loading an empty shell with visible-but-non-
 * functional admin controls.
 *
 * Pages that have legitimate non-admin content are deliberately NOT guarded —
 * e.g. `/users-and-groups/groups` and `/intents`, whose list endpoints use `get_current_user`
 * (group names are visible for sharing; intents are listable by all). Those
 * pages hide their admin *controls* via `isAdmin` instead of redirecting.
 *
 * This is a UI hardening layer — `isAdmin` is derived from the authoritative
 * `/me` response, and every admin action is still enforced server-side. While
 * the initial `/me` fetch is in flight we render a `PageSkeleton` so an admin
 * hard-refreshing an admin page doesn't get a redirect flash; once `/me`
 * succeeds, a confirmed non-admin is redirected to `/`.
 *
 * A `/me` *failure* never reaches this guard: the app sets `throwOnError: true`
 * globally (see `main.tsx`), so a 5xx/network error throws to the full-page
 * `ErrorBoundary`, and a 401 redirects to `/login` via `api.ts`. So the only
 * states this component observes are "loading" (skeleton) and "success"
 * (render for admins, redirect for non-admins).
 */
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAdmin, isLoading } = useAuth();
  /* PageSkeleton (not a spinner) for visual continuity with the route
   * Suspense fallback and the page's own loading skeleton — a hard refresh
   * on an admin page paints one continuous skeleton instead of a
   * spinner → skeleton → skeleton sequence. */
  if (isLoading) return <PageSkeleton />;
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** `AdminRoute`, open to a group manager too: Analytics (Model usage) reads their team. Traces and Sessions stay admin-only. */
function AnalyticsRoute({ children }: { children: React.ReactNode }) {
  const { isAdmin, isGroupManager, isLoading } = useAuth();
  if (isLoading) return <PageSkeleton />;
  if (!isAdmin && !isGroupManager) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** Wrap each page in an error boundary so one page crash doesn't kill the whole app.
 *  `resetKey={pathname}` clears a sticky error state on navigation WITHOUT
 *  remounting the boundary (the previous `key={pathname}` approach). Keeping
 *  the Suspense boundary mounted across navigations lets React Router's
 *  startTransition show the previous page until the next route chunk is
 *  ready, instead of flashing the skeleton fallback on every navigation,
 *  and stops tab/section URL changes from remounting the whole page.
 *
 *  The Suspense boundary sits inside the layout (Page renders within the
 *  AppLayout/PublicLayout Outlet), so the app shell stays visible while a
 *  lazy route chunk loads and only the content area shows the skeleton. */
function Page({ children, fallback = <PageSkeleton /> }: { children: React.ReactNode; fallback?: React.ReactNode }) {
  const { pathname } = useLocation();
  return (
    <ErrorBoundary resetKey={pathname}>
      <Suspense fallback={fallback}>{children}</Suspense>
    </ErrorBoundary>
  );
}

function ChatGate({ children }: { children: React.ReactNode }) {
  const chatEnabled = useChatEnabled();
  if (!chatEnabled) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/* A page behind a feature flag: a direct URL redirects home while the flag is
 * off rather than rendering it. The visual builder canvas is behind
 * `FLOW_BUILDER` while it is not announced externally (its ⌘K commands are
 * gone for the same reason), the Video Studio behind `VIDEO_STUDIO`.
 *
 * `/flows` itself is deliberately NOT gated: that page's Agent-to-agent map
 * (with its Schedule view) and Fleets tabs do not touch a flow endpoint. Only
 * its `Flows` library tab is hidden, by `Flows.tsx`.
 *
 * Waits for `/auth/config` before deciding (`isResolved`): the flag reads as
 * off until the config lands, and redirecting on that would bounce every deep
 * link and hard refresh to the dashboard on an instance where it IS on. */
function FeatureFlagGate({ flag, children }: { flag: FeatureFlag; children: React.ReactNode }) {
  const { enabled, isResolved, isError } = useFeatureFlagResolved(flag);
  /* An unfetchable config is not an answer. Redirecting on it would render a
     real outage as a plausible bounce to the dashboard — indistinguishable, to
     the user, from the feature genuinely not existing. `useAuthConfig` no
     longer throws (it would take the whole app down from the header), so this
     is the surface that says so. */
  if (isError) {
    return (
      <div className="p-6">
        {/* Strings reused from the catalogue rather than written fresh — all
            three are already translated in every locale. `actionIcon` because
            the default is a plus, which promises a new thing rather than
            another attempt at the same one. */}
        <EmptyState
          icon={AlertTriangle}
          title="Something went wrong"
          description="Reload the page to see the current state."
          actionLabel="Try again"
          actionIcon={RefreshCw}
          onAction={() => window.location.reload()}
        />
      </div>
    );
  }
  if (!isResolved) return <PageSkeleton />;
  if (!enabled) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/* Home dashboard selector: the mode-based dashboard (XTM One vs Copilot), or
 * the custom dashboard the person chose. Reads ``useMe`` so that a change of
 * the chosen dashboard swaps the home page once ``["me"]`` is invalidated. */
function HomeRoute() {
  const { data: user } = useMe();
  const platformConfig = usePlatformConfig();
  const aiEnabled = useAiEnabled();
  if (!user) return <PageSkeleton />;
  /* Ahead of every other home state (FR-019). */
  if (!aiEnabled) return <AiDisabledLanding />;
  return <ChosenOrModeDashboard homeDashboardId={user.home_dashboard_id} platformMode={platformConfig?.platform_mode} />;
}

/* The mode dashboard, or the custom dashboard the person chose instead
 * (`User.home_dashboard_id`): the mode dashboard again when it cannot be read. */
function ChosenOrModeDashboard({ homeDashboardId, platformMode }: { homeDashboardId?: string | null; platformMode?: string }) {
  const modeDashboard = isXtmOneMode(platformMode) ? <XtmOneDashboard /> : <Dashboard />;
  if (!homeDashboardId) return modeDashboard;
  return <HomeDashboard dashboardId={homeDashboardId} fallback={modeDashboard} />;
}

/* Dedicated command-center route (the mode dashboard). */
function CommandCenterRoute() {
  const platformConfig = usePlatformConfig();
  const { data: user } = useMe();
  if (!platformConfig || !user) return <PageSkeleton />;
  return <ChosenOrModeDashboard homeDashboardId={user.home_dashboard_id} platformMode={platformConfig.platform_mode} />;
}

/* CTEM is an XTM One concept. In Copilot/standalone deployments the CTEM
 * lifecycle doesn't exist, so a direct `/ctem/:stage` URL redirects home
 * instead of rendering the CTEM stage UI (which would otherwise show sample
 * data and contradict "Copilot/standalone dashboards are untouched"). The
 * backend CTEM endpoints are already guarded by `_require_xtm_one()`. */
function XtmOneRoute({ children }: { children: React.ReactNode }) {
  const platformConfig = usePlatformConfig();
  if (!platformConfig) return <PageSkeleton />;
  if (!isXtmOneMode(platformConfig.platform_mode)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  const platformConfig = usePlatformConfig();
  const catalogEnabled = useAICatalogEnabled();

  /* Warm all lazy route chunks once the browser is idle so first navigation
   * to any page doesn't pay a chunk-download skeleton flash. */
  useEffect(() => {
    prefetchLazyChunks();
  }, []);

  /* Wait for platform config before rendering — prevents 403 race */
  if (!platformConfig) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-elevation-default">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent" />
      </div>
    );
  }

  /* In Copilot mode, show the license splash until a valid license is present */
  if (platformConfig.license_required && !platformConfig.license_valid) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route path="/reset-password" element={<ResetPassword />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
        <Route path="*" element={
          <ProtectedRoute><LicenseRequired /></ProtectedRoute>
        } />
      </Routes>
    );
  }

  return (
    /* The waiting arcade sits above the routes: a game started from the
       chat outlives the chat's waiting panel, and the studio rail (outside
       AppLayout) hosts a waiting panel too. Inert until someone plays. */
    <ArcadeProvider>
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />

      {/* Static render behind the PDF / PPTX export. Loaded by the
          platform's own headless browser with a render token in the query
          string and NO session, so it must sit outside ProtectedRoute; the page
          is an empty shell until the token-gated render-spec route answers. */}
      <Route
        path="/presentations/:id/static"
        element={
          <Suspense fallback={null}>
            <PresentationStaticRender />
          </Suspense>
        }
      />

      {/* A video's figure scene, drawn by the render's headless
          browser, which answers the page's spec and asset requests itself:
          no session, no API call. */}
      <Route
        path="/videos/figures/static"
        element={
          <Suspense fallback={null}>
            <VideoFigureStaticRender />
          </Suspense>
        }
      />

      {/* A deck opened by its public link: the token in the address is
          the only credential, so it sits outside ProtectedRoute - and outside
          AppLayout, like the viewer. */}
      {/* The PDF a public link offers is printed by the platform's
          headless browser from this page: the static render, reading the deck
          through the public routes with the link's own token. */}
      <Route
        path="/public/presentations/:token/static"
        element={
          <Suspense fallback={null}>
            <PresentationStaticRender />
          </Suspense>
        }
      />
      <Route
        path="/public/presentations/:token"
        element={
          <ErrorBoundary fullPage>
            <Suspense fallback={<PageSkeleton />}>
              <PresentationPublic />
            </Suspense>
          </ErrorBoundary>
        }
      />
      <Route
        path="/v/:code"
        element={
          <ErrorBoundary fullPage>
            <Suspense fallback={<PageSkeleton />}>
              <PollVote />
            </Suspense>
          </ErrorBoundary>
        }
      />
      {/* A dashboard's published link: no session, a snapshot of its data (dashboard_public_links.py). */}
      <Route
        path="/public/dashboards/:token"
        element={
          <ErrorBoundary fullPage>
            <Suspense fallback={<PageSkeleton />}>
              <DashboardPublic />
            </Suspense>
          </ErrorBoundary>
        }
      />
      {/* A video opened by its public link: the token is the only
          credential, so it sits outside ProtectedRoute and AppLayout, like
          the public deck. */}
      <Route
        path="/public/videos/:token"
        element={
          <ErrorBoundary fullPage>
            <Suspense fallback={<PageSkeleton />}>
              <VideoPublic />
            </Suspense>
          </ErrorBoundary>
        }
      />

      {/* Deck viewer — authenticated but deliberately OUTSIDE AppLayout: a deck
          is presented fullscreen (or printed to PDF), so the sidebar and header
          would be in the way. */}
      <Route
        path="/presentations/:id"
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <Suspense fallback={<PageSkeleton />}>
                <PresentationViewer />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      {/* Dashboard Studio — outside AppLayout like the other studios: the
          assistant, the dashboard and the inspector take the whole screen. */}
      <Route
        path="/dashboards/:id/studio"
        element={
          <ProtectedRoute>
            <FeatureFlagGate flag="DASHBOARD_STUDIO">
              <ErrorBoundary fullPage>
                <Suspense fallback={<PageSkeleton />}>
                  <DashboardStudio />
                </Suspense>
              </ErrorBoundary>
            </FeatureFlagGate>
          </ProtectedRoute>
        }
      />

      {/* Video Studio editor — outside AppLayout like the deck viewer:
          the assistant, the stage, the timeline and the inspector take the
          whole screen. */}
      <Route
        path="/videos/:id"
        element={
          <ProtectedRoute>
            <FeatureFlagGate flag="VIDEO_STUDIO">
              <ErrorBoundary fullPage>
                <Suspense fallback={<PageSkeleton />}>
                  <VideoEditor />
                </Suspense>
              </ErrorBoundary>
            </FeatureFlagGate>
          </ProtectedRoute>
        }
      />

      {/* Speaker view — its own route, NOT the viewer re-opened in a
          popup: the presenter's second screen must never carry the studio rail,
          the toolbar or the deck chrome, only current / next / notes / clock.
          Same auth and same outside-AppLayout reasoning as the viewer. */}
      <Route
        path="/presentations/:id/speaker"
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <Suspense fallback={<PageSkeleton />}>
                <PresentationSpeaker />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      {/* Audience window (#10552) — slides only, the window shared in a
          meeting; driven by the speaker view. Same auth as the speaker view. */}
      <Route
        path="/presentations/:id/audience"
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <Suspense fallback={null}>
                <PresentationAudience />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      {/* Notes handout — the printable slide + notes document. */}
      <Route
        path="/presentations/:id/notes"
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <Suspense fallback={<PageSkeleton />}>
                <PresentationNotes />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      {/* Static render behind the report PDF export: loaded by the
          platform's headless browser with a render token and NO session, like
          the deck's static render above. */}
      <Route
        path="/reports/:id/static"
        element={
          <Suspense fallback={null}>
            <ReportStaticRender />
          </Suspense>
        }
      />
      {/* The dashboard's PDF / PNG export: no session, a render token (dashboard_export.py). */}
      <Route
        path="/dashboards/:id/static"
        element={
          <Suspense fallback={null}>
            <DashboardStaticRender />
          </Suspense>
        }
      />

      {/* An answer link opened from Teams, Google Chat or an email: the page
          alone, no app shell; the sign-in comes back here. */}
      <Route
        path="/decide/:token"
        element={
          <ProtectedRoute returnHere>
            <ErrorBoundary fullPage>
              <Suspense fallback={<PageSkeleton />}>
                <DecisionLink />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      {/* Report viewer — same reasoning as the deck viewer above: a report is read
          full-width and printed to PDF, so the app chrome would end up in the
          exported document. */}
      <Route
        path="/reports/:id"
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <Suspense fallback={<PageSkeleton />}>
                <ReportViewer />
              </Suspense>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      />

      <Route
        element={
          <ProtectedRoute>
            <ErrorBoundary fullPage>
              <TermsGate>
                <AppLayout />
              </TermsGate>
            </ErrorBoundary>
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<Page><HomeRoute /></Page>} />
        <Route path="/dashboard" element={<Page><CommandCenterRoute /></Page>} />
        <Route path="/ctem/governance" element={<XtmOneRoute><Page><CtemGovernance /></Page></XtmOneRoute>} />
        <Route path="/ctem/cases" element={<XtmOneRoute><Page><CtemCases /></Page></XtmOneRoute>} />
        {/* The Cases Kanban view is a path, never ?view= (#10203, #1503); a static segment ranks above :id. */}
        <Route path="/ctem/cases/kanban" element={<XtmOneRoute><Page><CtemCases /></Page></XtmOneRoute>} />
        <Route path="/ctem/signals" element={<XtmOneRoute><Page><CtemSignals /></Page></XtmOneRoute>} />
        <Route path="/ctem/signals/:tab" element={<XtmOneRoute><Page><CtemSignals /></Page></XtmOneRoute>} />
        <Route path="/ctem/cases/:id" element={<XtmOneRoute><Page><CtemCaseDetail /></Page></XtmOneRoute>} />
        {/* CTEM > Inventory (#1178). */}
        <Route path="/ctem/inventory/assets/:id" element={<XtmOneRoute><Page><CtemAssetDetail /></Page></XtmOneRoute>} />
        <Route path="/ctem/inventory/software/:id" element={<XtmOneRoute><Page><CtemSoftwareDetail /></Page></XtmOneRoute>} />
        <Route path="/ctem/inventory/:tab?" element={<XtmOneRoute><Page><CtemInventory /></Page></XtmOneRoute>} />
        <Route path="/ctem/:stage" element={<XtmOneRoute><Page><CtemStageDetail /></Page></XtmOneRoute>} />
        <Route path="/briefing" element={<XtmOneRoute><Page><ExposureBriefing /></Page></XtmOneRoute>} />
        {/* The chat's chunk loads in its empty frame, never a page skeleton the panel's own would replace. */}
        <Route path="/chat" element={<ChatGate><Page fallback={<ChatFrame />}><ChatPanelPage /></Page></ChatGate>} />
        <Route path="/work" element={<Page><Work /></Page>} />
        <Route path="/agent-studio" element={<Page><AgentStudio /></Page>} />
        <Route path="/tools-and-integrations" element={<Page><ToolsAndIntegrations /></Page>} />
        {/* The Agents group's hub (#9301 N2c); `/agents` stays the list. */}
        <Route path="/agents-hub" element={<Page><AgentsHub /></Page>} />
        <Route path="/agents" element={<Page><Agents /></Page>} />
        <Route path="/agents/new" element={<Page><NewAgentMode /></Page>} />
        <Route path="/agents/new/advanced" element={<Page><NewAgent /></Page>} />
        <Route path="/agents/new/import" element={<Page><NewAgentImport /></Page>} />
        <Route path="/agents/new/wizard" element={<Page><NewAgentWizard /></Page>} />
        <Route path="/agents/flow" element={<Page><AgentFlow /></Page>} />
        <Route path="/agents/:id/chats/:conversationId" element={<Page><AgentChatConversation /></Page>} />
        <Route path="/agents/:id/:tab?/:section?" element={<Page><AgentDetail /></Page>} />
        <Route path="/health/:tab?" element={<AdminRoute><Page><Health /></Page></AdminRoute>} />
        <Route path="/evaluations" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><Evaluations /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/new" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><NewEvaluation /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/datasets" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><Datasets /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/datasets/:id/:tab?" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><DatasetDetail /></Page></AdminRoute></FeatureFlagGate>} />
        {/* Static one-segment paths, above `/evaluations/:id/:tab?` which would read them as an id. */}
        <Route path="/evaluations/compare/:tab?" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><EvaluationCompare /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/review/:view?" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><EvaluationBundleReview /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/runs/:id" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><EvaluationRunDetail /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/:evaluationId/compare" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><EvaluationRunCompare /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/evaluations/:id/:tab?" element={<FeatureFlagGate flag="EVALUATION"><AdminRoute><Page><EvaluationDetail /></Page></AdminRoute></FeatureFlagGate>} />
        <Route path="/flows/:tab?" element={<Page><Flows /></Page>} />
        {/* The map's sub-views are path segments (#10203 S2): `/flows/map/schedule`. */}
        <Route path="/flows/map/:mapView" element={<Page><Flows /></Page>} />
        {/* One fleet; the static `fleets` segment ranks above `/flows/library/:id`. */}
        {/* Its tabs are path segments (#10203 S2): `/graph`, `/data-flow`, `/members`,
            `/approval`, `/schedule`, `/operations/:list`, `/roi`; none is About. */}
        <Route path="/fleets" element={<Page><Fleets /></Page>} />
        <Route path="/fleets/:fleetId/:tab?/:list?" element={<Page><FleetPage /></Page>} />
        {/* Deeper than `/flows/:tab?`, so `/flows/map` and `/flows/library/<uuid>`
            never collide. Creation is a POST followed by a navigate — a
            `/flows/new` route WOULD collide with the tab segment. */}
        <Route path="/flows/library/:id" element={<FeatureFlagGate flag="FLOW_BUILDER"><Page><FlowBuilder /></Page></FeatureFlagGate>} />
        <Route path="/integrations" element={<Page><Integrations /></Page>} />
        <Route path="/integrations/:id/:tab?" element={<Page><IntegrationDetail /></Page>} />
        <Route path="/integrations/new" element={<Page><NewIntegration /></Page>} />
        {/* Saved sign-ins every browser of the platform opens: not behind the Video Studio flag. */}
        <Route path="/connections" element={<Page><BrowserProfiles /></Page>} />
        {/* An OAuth2 implicit sign-in lands here with the token in the fragment. */}
        <Route path="/oauth/implicit" element={<Page><OAuthImplicitCallback /></Page>} />
        <Route path="/knowledge" element={<Page><KnowledgeBases /></Page>} />
        <Route path="/knowledge/documents" element={<Page><KnowledgeDocuments /></Page>} />
        <Route path="/knowledge/new" element={<Page><NewKnowledgeBase /></Page>} />
        <Route path="/knowledge/:id/:tab?" element={<Page><KnowledgeBaseDetail /></Page>} />
        {/* Operations: what is running or past, one route per tab (#775). */}
        <Route path="/operations" element={<Page><Operations /></Page>} />
        <Route path="/operations/executions" element={<Page><Operations /></Page>} />
        <Route path="/operations/background-tasks" element={<Page><Operations /></Page>} />
        <Route path="/operations/shared-state" element={<Page><Operations /></Page>} />
        <Route path="/operations/worksets" element={<Page><Operations /></Page>} />
        {/* Tasks: what to do and follow, one route per tab, the paths of the menu (#775).
            No alias serves the former paths: every link is built by lib/task-routes.ts. */}
        <Route path="/tasks" element={<Page><Tasks /></Page>} />
        <Route path="/tasks/work-items" element={<Page><Tasks /></Page>} />
        <Route path="/tasks/work-items/:itemId" element={<Page><Tasks /></Page>} />
        {/* The methodology (#9836 R7b): every user, both modes, linked from every "How this is computed". */}
        <Route path="/tasks/value/methodology" element={<Page><ValueMethodology /></Page>} />
        {/* The Value report (#9836 R5b): every user, both modes; it shows the Community Edition upgrade path itself. */}
        <Route path="/tasks/value/:tab?" element={<Page><Tasks /></Page>} />
        <Route path="/tasks/workspaces" element={<Page><Tasks /></Page>} />
        <Route path="/tasks/workspaces/:workspaceId/:tab?" element={<Page><WorkspaceProject /></Page>} />
        <Route path="/operations/executions/:id" element={<Page><RunDetail /></Page>} />
        <Route path="/agents/assignments/:id/:tab?" element={<Page><AssignmentDetail /></Page>} />
        <Route path="/skills" element={<Page><Skills /></Page>} />
        <Route path="/skills/:id/:tab?" element={<Page><SkillDetail /></Page>} />
        <Route path="/mcp-servers" element={<Page><MCPServers /></Page>} />
        <Route path="/mcp-servers/:id/:tab?" element={<Page><MCPServerDetail /></Page>} />
        <Route path="/tools" element={<Page><CustomTools /></Page>} />
        {/* A static segment ranks above `/tools/:id/:tab?`: the Custom Tools tab, never a tool id (#10203). */}
        <Route path="/tools/custom" element={<Page><CustomTools /></Page>} />
        <Route path="/tools/sources" element={<Page><CustomTools /></Page>} />
        <Route path="/tools/:id/:tab?" element={<Page><CustomToolDetail /></Page>} />
        <Route path="/connections/variables" element={<Page><Variables /></Page>} />
        <Route path="/connections/variables/:id" element={<Page><Variables /></Page>} />
        <Route path="/prompts" element={<Page><Prompts /></Page>} />
        <Route path="/prompts/:id/:tab?" element={<Page><PromptDetail /></Page>} />
        {/* The packs API answers 403 while DEEP_INVESTIGATION is off. */}
        <Route path="/investigation-packs" element={<FeatureFlagGate flag="DEEP_INVESTIGATION"><Page><InvestigationPacks /></Page></FeatureFlagGate>} />
        <Route path="/investigation-packs/:id/:tab?" element={<FeatureFlagGate flag="DEEP_INVESTIGATION"><Page><InvestigationPackDetail /></Page></FeatureFlagGate>} />
        <Route path="/templates" element={<Page><Templates /></Page>} />
        <Route path="/templates/new" element={<Page><NewTemplate /></Page>} />
        <Route path="/templates/:id/:tab?" element={<Page><TemplateDetail /></Page>} />
        <Route path="/presentations" element={<Page><Presentations /></Page>} />
        <Route path="/dashboards" element={<FeatureFlagGate flag="DASHBOARD_STUDIO"><Page><CustomDashboards /></Page></FeatureFlagGate>} />
        <Route path="/dashboards/packs" element={<FeatureFlagGate flag="DASHBOARD_STUDIO"><Page><WidgetPacks /></Page></FeatureFlagGate>} />
        <Route path="/dashboards/:id" element={<FeatureFlagGate flag="DASHBOARD_STUDIO"><Page><CustomDashboardViewer /></Page></FeatureFlagGate>} />
        {/* The mobile companion's Inbox and Share pages: gone while MOBILE_COMPANION is off. */}
        <Route path="/inbox" element={<FeatureFlagGate flag="MOBILE_COMPANION"><Page><CompanionInbox /></Page></FeatureFlagGate>} />
        <Route path="/share" element={<FeatureFlagGate flag="MOBILE_COMPANION"><Page><SharePage /></Page></FeatureFlagGate>} />
        <Route path="/videos" element={<FeatureFlagGate flag="VIDEO_STUDIO"><Page><VideoStudio /></Page></FeatureFlagGate>} />
        {/* A static segment ranks above `/videos/:id`: the Media tab, never a video id (#10203). */}
        <Route path="/videos/media" element={<FeatureFlagGate flag="VIDEO_STUDIO"><Page><VideoStudio /></Page></FeatureFlagGate>} />
        <Route path="/files" element={<Page><Files /></Page>} />
        <Route path="/search" element={<Page><Search /></Page>} />
        <Route path="/users-and-groups" element={<AdminRoute><Page><Users /></Page></AdminRoute>} />
        <Route path="/users-and-groups/users/new" element={<AdminRoute><Page><NewUser /></Page></AdminRoute>} />
        <Route path="/users-and-groups/users/:id/:tab?" element={<AdminRoute><Page><UserDetail /></Page></AdminRoute>} />
        <Route path="/users-and-groups/groups" element={<Page><Groups /></Page>} />
        <Route path="/users-and-groups/groups/new" element={<AdminRoute><Page><NewGroup /></Page></AdminRoute>} />
        <Route path="/users-and-groups/groups/:id/:tab?" element={<AdminRoute><Page><GroupDetail /></Page></AdminRoute>} />
        <Route path="/objects" element={<AdminRoute><Page><AdminObjects /></Page></AdminRoute>} />
        <Route path="/intents" element={<XtmOneRoute><Page><Intents /></Page></XtmOneRoute>} />
        <Route path="/observability/:tab?" element={<AnalyticsRoute><Page><Analytics /></Page></AnalyticsRoute>} />
        <Route path="/observability/traces" element={<AdminRoute><Page><Traces /></Page></AdminRoute>} />
        <Route path="/observability/sessions" element={<AdminRoute><Page><TraceSessions /></Page></AdminRoute>} />
        <Route path="/observability/traces/:id/:tab?" element={<Page><TraceDetail /></Page>} />
        <Route path="/logs" element={<AdminRoute><Page><AuditLogs /></Page></AdminRoute>} />
        <Route path="/profile/agent-accounts/:accountId/:tab?" element={<Page><AgentAccountPage /></Page>} />
        <Route path="/profile/:tab?" element={<Page><Profile /></Page>} />
        <Route path="/desktop" element={<Page><Desktop /></Page>} />
        <Route path="/desktop/runners/:id" element={<Page><RunnerDetail /></Page>} />
        <Route path="/connections/relays" element={<Page><Relays /></Page>} />
        <Route path="/connections/relays/:id/:tab?" element={<Page><RelayGroupDetail /></Page>} />
        <Route path="/ai-adoption/:tab?" element={
          platformConfig?.features.ai_adoption === false
            ? <Navigate to="/" replace />
            : <AdminRoute><Page><AiAdoption /></Page></AdminRoute>
        } />
        <Route path="/settings/organization/tracked/ai-draft/:proposalId" element={<AdminRoute><Page><TrackedOrganizationsAiDraftReview /></Page></AdminRoute>} />
        <Route path="/settings/:tab?/:section?" element={<AdminRoute><Page><Settings /></Page></AdminRoute>} />
        {/* A sub-view of a section: the organization profile's tab (#12329, /settings/organization/profile/<tab>). */}
        <Route path="/settings/:tab/:section/:view" element={<AdminRoute><Page><Settings /></Page></AdminRoute>} />
        {/* No alias and no redirect (#797): an unknown path, a former one included, is not found, in the app chrome. */}
        <Route path="*" element={<Page><RouteNotFound /></Page>} />
      </Route>

      {/* Internal catalog (requires auth + feature flag) */}
      {catalogEnabled ? (
        <Route
          path="/catalog"
          element={
            <ProtectedRoute>
              <ErrorBoundary fullPage>
                <TermsGate><PublicLayout /></TermsGate>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        >
          <Route index element={<Page><PublicCatalog /></Page>} />
          <Route path="agents" element={<Page><PublicAgents /></Page>} />
          <Route path="agents/:id" element={<Page><PublicAgentDetail /></Page>} />

          <Route path="skills" element={<Page><PublicSkills /></Page>} />
          <Route path="skills/:id" element={<Page><PublicSkillDetail /></Page>} />
          <Route path="prompts" element={<Page><PublicPrompts /></Page>} />
          <Route path="prompts/:id" element={<Page><PublicPromptDetail /></Page>} />
          <Route path="mcp-servers" element={<Page><PublicMCPServers /></Page>} />
          <Route path="mcp-servers/:id" element={<Page><PublicMCPServerDetail /></Page>} />
          <Route path="tools" element={<Page><PublicCustomTools /></Page>} />
          <Route path="tools/:id" element={<Page><PublicCustomToolDetail /></Page>} />
        </Route>
      ) : (
        <Route path="/catalog/*" element={<Navigate to="/" replace />} />
      )}

    </Routes>
    </ArcadeProvider>
  );
}
