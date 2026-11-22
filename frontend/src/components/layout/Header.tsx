// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useCallback } from "react";
import { Header as HeaderBar, HeaderGroup } from "@filigran/design-system";
import { useNavigate } from "react-router-dom";
import { Bell, Check, CheckCircle2, ChevronDown, Gamepad2, Globe, Hand, Info, Key, LayoutDashboard, Link2Off, LogOut, MessageCircleQuestion, MonitorDown, OctagonX, RotateCcw, Settings, Shield, ShieldAlert, Sparkles, Trophy, User as UserIcon, UserCog, Workflow, AlarmClock, Bot } from "lucide-react";
import { useMe } from "@/hooks/api/useAuthApi";
import { useDismissNotification, useImportLegacyReadState, useMarkNotificationsRead, useNotifications } from "@/hooks/api/useNotificationApi";
import { clearAllTokens } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { AiDisabledBadge } from "@/components/ui/ai-disabled";
import { useEmergencyStopStatus, type PanicScope } from "@/hooks/api/usePanicApi";
import { EmergencyStopDialog } from "./EmergencyStopDialog";
import { EmergencyResumeDialog } from "./EmergencyResumeDialog";
import { AboutDialog } from "./AboutDialog";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/relative-time";
import { canMarkAllRead as anyReadable, decisionSource, hasUnreadApproval, isWaitingDecision, notificationSubtitle, readsOnOpen } from "@/lib/notification-kinds";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useDesktopBridge, useRunnersEnabled } from "@/hooks/useDesktop";
import { useTick } from "@/hooks/useTick";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { withBasePath } from "@/lib/base-path";
import { browserDraftStorage, clearAllWizardDrafts } from "@/pages/new-agent-wizard/draft-storage";
import { ManualTriggerButton } from "./ManualTriggerButton";
import { GlobalSearch } from "./GlobalSearch";
import { useArcade } from "@/components/arcade/arcade-context";
import { useAICatalogEnabled, usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";


export function Header() {
  useTick(REFETCH_INTERVAL_SLOW); // keep notification timestamps fresh
  // Notification times in the app language: "à l'instant", not "just now".
  const { t_i18n, intl } = useFormatter();
  const { data: user } = useMe();
  /* The bell reads the server's feed (`GET /notifications`): the same items,
     routes and read state the companion's Inbox and a phone push use, so a
     notification read on one device is read on all of them. */
  const { data: feed } = useNotifications();
  const markRead = useMarkNotificationsRead();
  const dismissNotification = useDismissNotification();
  useImportLegacyReadState();
  const navigate = useNavigate();
  const catalogEnabled = useAICatalogEnabled();
  /* XTM One Desktop: the user menu is its one entry point. Inside the
     app itself the item reads as the machines page. */
  const runnersEnabled = useRunnersEnabled();
  const desktopBridge = useDesktopBridge();
  const platformName = usePlatformName();
  const arcade = useArcade();
  const [showNotifs, setShowNotifs] = useState(false);
  const [showUser, setShowUser] = useState(false);
  // Emergency stop ("panic button"). The status is polled while signed in and
  // shared with the page-wide banner; here it decides which "resume" entries
  // are worth showing.
  const [emergencyStopOpen, setEmergencyStopOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [resumeScope, setResumeScope] = useState<PanicScope | null>(null);
  const isAdmin = user?.role === "admin";
  const { data: panicStatus } = useEmergencyStopStatus(!!user);
  // A self resume is refused while a platform stop is open: do not offer it.
  const canResumeSelf =
    ((panicStatus?.self_paused_assignments ?? 0) > 0 || panicStatus?.self_stop_active === true) &&
    panicStatus?.platform_stop_active !== true;
  const canResumePlatform =
    isAdmin && (panicStatus?.platform_stop_active === true || (panicStatus?.platform_paused_assignments ?? 0) > 0);
  const notifsRef = useRef<HTMLDivElement>(null);
  const userRef = useRef<HTMLDivElement>(null);

  useClickOutside(notifsRef, () => setShowNotifs(false));
  useClickOutside(userRef, () => setShowUser(false));

  const handleLogout = () => {
    clearAllTokens();
    clearAllWizardDrafts(browserDraftStorage());
    window.location.href = withBasePath("/login");
  };

  const allNotifs = feed?.items ?? [];
  // A decision still waiting stays unread until answered, whatever is opened
  // or marked (#991); opening or dismissing any other entry reads it (#901).
  const unreadNotifs = allNotifs.filter((item) => item.unread);
  const canMarkAllRead = anyReadable(allNotifs);

  const markAllRead = useCallback(() => markRead.mutate(), [markRead]);

  const markOneRead = useCallback((id: string) => dismissNotification.mutate(id), [dismissNotification]);

  const dismissOne = useCallback((id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    markOneRead(id);
  }, [markOneRead]);

  return (
    /* The library owns the bar itself: its 68px height, the gradient surface at
       Figma's 94% opacity over a backdrop blur, the bottom hairline, the
       asymmetric padding and the start/end distribution. Everything inside is
       product content — the component is a container with slots, never content.

       It deliberately carries no positioning, so `sticky` is this app's choice
       and a documented departure: the library's doctrine is that the bar is
       always fixed. Sticky keeps the page as the scroll container, where fixed
       would move scrolling into the content zone and change scroll restoration,
       in-page sticky headers and the assistant panel's geometry all at once.
       Recorded in fds-migration/LIBRARY-FEEDBACK.md rather than done quietly. */
    <HeaderBar className="sticky top-0 z-30">
      <HeaderGroup>
        {/* The inline search of the platform (#11201): entities, pages and, by
            prefix, commands. Padded left below `lg`: the sidebar's hamburger is
            fixed at 16px and 36px wide, so without clearance it sits on the field. */}
        <div className="pl-8 lg:pl-0">
          <GlobalSearch />
        </div>

      </HeaderGroup>

      <HeaderGroup className="gap-2">
        {/* The persistent AI-disabled marker (FR-020, SC-007). In the header
            rather than the sidebar so it survives rail mode,
            and it renders for non-administrators too — it explains why AI
            actions are unavailable to the people who cannot change it. */}
        <AiDisabledBadge />
        {/* Manual triggers quick-access */}
        <ManualTriggerButton />

        {/* Public catalog quick-access */}
        {catalogEnabled && (
        <Tip content={t_i18n("AI Catalog")}>
          {/* Not an IconButton: this is a link, and IconButton takes no child to
              render as one. The name goes on the anchor, which is the control. */}
          <Button variant="ghost" size="icon" className="h-8 w-8" asChild>
            <a href={withBasePath("/catalog")} target="_blank" rel="noopener noreferrer" aria-label={t_i18n("AI Catalog")}>
              <Globe className="h-4 w-4" />
            </a>
          </Button>
        </Tip>
        )}

        {/* Notifications bell */}
        <div className="relative" ref={notifsRef}>
          {/* Not an IconButton: the bell carries a count badge as a second child,
              and IconButton renders exactly one icon. It keeps an explicit name. */}
          <Button variant="ghost" size="icon" aria-label={t_i18n("Notifications")} className="relative h-8 w-8" onClick={() => { setShowNotifs(!showNotifs); setShowUser(false); }}>
            <Bell className="h-4 w-4" />
            {unreadNotifs.length > 0 && (
              <span className={cn(
                "absolute -top-0.5 -right-0.5 flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-bold",
                hasUnreadApproval(allNotifs) ? "bg-feedback-alert-primary text-on-feedback-fill animate-pulse" : "bg-accent text-negative-primary",
              )}>
                {unreadNotifs.length}
              </span>
            )}
          </Button>

          {showNotifs && (
            <div className="absolute right-0 top-full mt-2 w-96 rounded-sm layer-1 bg-elevation-default shadow-overlay animate-fade-in">
              <div className="flex items-center justify-between px-4 py-3 border-b border-elevation-subtle-soft">
                <p className="content-compact font-semibold">{t_i18n("Notifications")}</p>
                <div className="flex items-center gap-2">
                  {canMarkAllRead && (
                    <button className="content-caption text-accent hover:underline" onClick={markAllRead}>{t_i18n("Mark all read")}</button>
                  )}
                  <button className="content-caption text-default-secondary hover:text-default-primary" onClick={() => { navigate("/operations/executions?filter=notifications"); setShowNotifs(false); }}>{t_i18n("View all")}</button>
                </div>
              </div>
              <div className="max-h-96 overflow-y-auto">
                {allNotifs.length > 0 ? allNotifs.map((item) => {
                  // One decision kind, three sources (#842, S4): the run's hand,
                  // the flow's question, the work item's hand.
                  const source = decisionSource(item);
                  // Pinned until answered: the warning chrome, no read dot, no
                  // "Mark as read"; opening it does not read it (#991).
                  const isPinned = isWaitingDecision(item) && item.unread;
                  const isAnalyticsRefresh = item.kind === "analytics_refresh";
                  const unread = isPinned || item.unread;
                  return (
                    <div
                      key={item.id}
                      className={cn(
                        "px-4 py-3 border-b border-elevation-subtle-soft last:border-0 hover:bg-elevation-hover transition-colors cursor-pointer relative group",
                        isPinned ? "bg-feedback-warning-primary/[0.05] border-l-2 border-l-feedback-warning-primary/50" : unread && "bg-accent/[0.03]",
                      )}
                      /* Opening a notification reads it (#901), except a decision
                         still waiting, read only by its answer (#991). */
                      onClick={() => { if (readsOnOpen(item)) markOneRead(item.id); navigate(item.route); setShowNotifs(false); }}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5 min-w-0">
                          {unread && !isPinned && <span className="flex h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />}
                          {source ? (
                            /* One decision kind, one treatment (UX round 1, B1): the same
                               12 px glyph for every source, no opacity animation; only the
                               glyph changes (question for a flow), and an answered decision
                               is dimmed by its colour token (#901). */
                            source === "flow_run" ? (
                              <MessageCircleQuestion className={cn("h-3 w-3 shrink-0", item.unread ? "text-feedback-warning-primary" : "text-default-secondary")} />
                            ) : (
                              <Hand className={cn("h-3 w-3 shrink-0", item.unread ? "text-feedback-warning-primary" : "text-default-secondary")} />
                            )
                          ) : isAnalyticsRefresh ? (
                            <LayoutDashboard className="h-3 w-3 text-accent shrink-0" />
                          ) : item.kind === "flow" ? (
                            <Workflow className="h-3 w-3 text-filigran-ia-primary shrink-0" />
                          ) : item.kind === "auth_failure" ? (
                            <ShieldAlert className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          ) : item.kind === "stale_reference" || item.kind === "continuation_refused" ? (
                            <Link2Off className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          ) : item.kind === "security_audit" ? (
                            <ShieldAlert className={cn("h-3 w-3 shrink-0", item.critical ? "text-feedback-error-primary" : "text-feedback-warning-primary")} />
                          ) : item.kind === "api_key" ? (
                            <Key className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          ) : item.kind === "agent_account" ? (
                            <UserCog className="h-3 w-3 text-accent shrink-0" />
                          ) : item.kind === "run_action_request" ? (
                            <Hand className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          ) : item.kind === "reminder" ? (
                            <AlarmClock className="h-3 w-3 text-accent shrink-0" />
                          ) : item.kind === "agent_message" ? (
                            <Bot className="h-3 w-3 text-filigran-ia-primary shrink-0" />
                          ) : item.kind === "builtin_tools_outside_allowlist" ? (
                            <Sparkles className="h-3 w-3 text-accent shrink-0" />
                          ) : (
                            <CheckCircle2 className="h-3 w-3 text-feedback-success-primary shrink-0" />
                          )}
                          <p className={cn("content-compact truncate", isPinned ? "font-semibold text-feedback-warning-primary" : unread ? "font-semibold" : "font-medium text-default-secondary")}>{item.title}</p>
                        </div>
                        <div className="flex items-center gap-1 shrink-0">
                          {isPinned && <span className="content-caption font-medium text-feedback-warning-primary mr-1">{t_i18n("Action needed")}</span>}
                          <span className="content-caption text-default-secondary">{formatTimeAgo(intl, item.created_at)}</span>
                          {unread && !isPinned && (
                            <Tip content={t_i18n("Mark as read")}>
                              <button aria-label={t_i18n("Mark as read")} className="hidden group-hover:flex h-4 w-4 items-center justify-center rounded-sm hover:bg-elevation-hover" onClick={(e) => dismissOne(item.id, e)}>
                                <Check className="h-2.5 w-2.5 text-default-secondary" />
                              </button>
                            </Tip>
                          )}
                        </div>
                      </div>
                      <p className="content-caption text-default-secondary mt-0.5">{notificationSubtitle(item, t_i18n)}</p>
                      <p className={cn("content-caption mt-0.5 line-clamp-1", isPinned ? "text-feedback-warning-primary" : "text-default-secondary")}>
                        {item.summary}
                      </p>
                    </div>
                  );
                }) : (
                  <div className="py-8 text-center">
                    <p className="content-compact text-default-secondary">{t_i18n("No notifications yet")}</p>
                    <p className="content-caption text-default-secondary mt-1">{t_i18n("Completed assignment results and analytics refresh updates will appear here")}</p>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* User dropdown */}
        <div className="relative" ref={userRef}>
          <button className="flex items-center gap-2 rounded-sm px-2 py-1.5 hover:bg-elevation-hover transition-colors" onClick={() => { setShowUser(!showUser); setShowNotifs(false); }} data-testid="user-menu-trigger">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-accent/10"><UserIcon className="h-3.5 w-3.5 text-accent" /></div>
            <div className="hidden sm:block text-left">
              <span className="content-compact font-medium text-default-primary block leading-tight">{user?.full_name}</span>
              {user?.title && <span className="text-[9px] text-default-secondary block leading-tight">{user.title}</span>}
            </div>
            <ChevronDown className={cn("h-3 w-3 text-default-secondary transition-transform", showUser && "rotate-180")} />
          </button>

          {showUser && (
            <div className="absolute right-0 top-full mt-2 w-56 rounded-sm layer-1 bg-elevation-default shadow-overlay animate-fade-in">
              {user && (
                <div className="p-3 border-b border-elevation-subtle-soft">
                  <p className="content-base font-medium">{user.full_name}</p>
                  {user.title && <p className="content-caption text-accent">{user.title}</p>}
                  <p className="text-[11px] text-default-secondary">{user.email}</p>
                  {user.role === "admin" && <Badge variant="cyan" className="mt-1 gap-1 text-[9px]"><Shield className="h-2.5 w-2.5" /> Admin</Badge>}
                </div>
              )}
              <div className="p-1">
                <button className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onClick={() => { navigate("/profile"); setShowUser(false); }}><UserIcon className="h-3.5 w-3.5 text-default-secondary" /> {t_i18n("My Profile")}</button>
                {runnersEnabled && (
                  <button data-testid="user-menu-desktop" className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onClick={() => { navigate("/desktop"); setShowUser(false); }}>
                    <MonitorDown className="h-3.5 w-3.5 text-default-secondary" />
                    {desktopBridge ? t_i18n("My machines") : t_i18n("Get XTM One Desktop")}
                  </button>
                )}
                {/* The arcade outside a wait: the waiting panel only
                    offers it while an agent works. */}
                {arcade.enabled && (
                  <>
                    <button data-testid="user-menu-arcade-play" className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onPointerEnter={arcade.prefetch} onFocus={arcade.prefetch} onClick={() => { setShowUser(false); arcade.launch({ source: "menu", origin: null }); }}>
                      <Gamepad2 className="h-3.5 w-3.5 text-default-secondary" />
                      {t_i18n("Play Space Invaders")}
                    </button>
                    <button data-testid="user-menu-arcade-leaderboard" className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onClick={() => { setShowUser(false); arcade.openLeaderboard(); }}>
                      <Trophy className="h-3.5 w-3.5 text-default-secondary" />
                      {t_i18n("Arcade leaderboard")}
                    </button>
                  </>
                )}
                {user?.role === "admin" && (
                  <button className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onClick={() => { navigate("/settings"); setShowUser(false); }}><Settings className="h-3.5 w-3.5 text-default-secondary" /> {t_i18n("Settings")}</button>
                )}
                <button data-testid="user-menu-about" className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-default-primary hover:bg-elevation-hover transition-colors" onClick={() => { setAboutOpen(true); setShowUser(false); }}><Info className="h-3.5 w-3.5 text-default-secondary" /> {t_i18n("About {name}", { name: platformName })}</button>
                <div className="my-1 border-t border-elevation-subtle-soft" />
                {/* The two destructive rows take the tertiary error token and the
                    menu's own hover step, as the shared destructive row of
                    OverflowMenuItems does: the primary read 4.26:1 on the dark
                    panel (held by tests_unit/destructive-menu-row-contrast.test.ts). */}
                <button className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-feedback-error-tertiary hover:bg-elevation-hover transition-colors" onClick={() => { setEmergencyStopOpen(true); setShowUser(false); }} data-testid="emergency-stop-menu"><OctagonX className="h-3.5 w-3.5" /> {t_i18n("Emergency stop")}</button>
                {canResumeSelf && (
                  <button className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-feedback-warning-tertiary hover:bg-feedback-alert-primary/5 transition-colors" onClick={() => { setResumeScope("self"); setShowUser(false); }} data-testid="emergency-resume-self"><RotateCcw className="h-3.5 w-3.5" /> {t_i18n(isAdmin ? "Resume my activity" : "Resume after emergency stop")}</button>
                )}
                {canResumePlatform && (
                  <button className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-feedback-warning-tertiary hover:bg-feedback-alert-primary/5 transition-colors" onClick={() => { setResumeScope("platform"); setShowUser(false); }} data-testid="emergency-resume-platform"><RotateCcw className="h-3.5 w-3.5" /> {t_i18n("Resume the whole platform")}</button>
                )}
                <div className="my-1 border-t border-elevation-subtle-soft" />
                <button data-testid="user-menu-sign-out" className="flex w-full items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-feedback-error-tertiary hover:bg-elevation-hover transition-colors" onClick={handleLogout}><LogOut className="h-3.5 w-3.5" /> {t_i18n("Sign out")}</button>
              </div>
            </div>
          )}
        </div>
      </HeaderGroup>

      {/* Rendered outside the dropdown so closing the menu does not unmount them. */}
      <EmergencyStopDialog open={emergencyStopOpen} onOpenChange={setEmergencyStopOpen} isAdmin={isAdmin} />
      <EmergencyResumeDialog scope={resumeScope} onClose={() => setResumeScope(null)} />
      <AboutDialog open={aboutOpen} onOpenChange={setAboutOpen} />
    </HeaderBar>
  );
}
