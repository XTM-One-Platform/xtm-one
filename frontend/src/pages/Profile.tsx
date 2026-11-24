// SPDX-License-Identifier: Apache-2.0

import { IconButton, Spinner } from "@filigran/design-system";
import { platformOrigin } from "@/lib/base-path";
import { useState, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import { User as UserIcon, Shield, Key, Pencil, Save, Lock, Check, Copy, Trash2, Fingerprint, Bot, Wrench, RotateCcw, Code, Server, BookOpen, KeyRound, Terminal, Radio, MessageSquare, X, Globe, BarChart3, Info, ExternalLink, Brain, ChevronDown, ChevronRight, Zap, Palette, Languages, UserCog, Sparkles, Smartphone } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { Switch } from "@/components/ui/switch";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { SearchInput } from "@/components/ui/search-input";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useMe, useUpdateProfile, useChangePassword, useAuthConfig } from "@/hooks/api/useAuthApi";
import { useApiKeys } from "@/hooks/api/useMiscApi";
import { apiKeyUsableOn } from "@/lib/api-keys";
import { useAvailableModels, useGeneralAssistantConfig, useMyMemory, usePurgeMyMemory, usePurgeMyUserProfile, usePurgeMyAgentMemory, useMyQuotas } from "@/hooks/api/useSettingsApi";
import type { UserMemoryAgent } from "@/hooks/api/useSettingsApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { PlatformMCPServerList, usePlatformMCPServerList } from "@/components/ui/platform-mcp-server-list";
import { XtmMcpPackageDownload } from "@/components/ui/xtm-mcp-package-download";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useBrowserProfiles } from "@/hooks/api/useBrowserProfilesApi";
import { useChannelBots } from "@/hooks/api/useChannelBotApi";
import { useToast } from "@/components/ui/toast";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useTabParam } from "@/hooks/useTabParam";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useNow } from "@/hooks/useTick";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useIsXtmOne, useIsCommunityEdition, usePlatformName, useCustomToolsEnabled } from "@/hooks/usePlatformConfig";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import { CtemStageAgentsCard } from "@/pages/xtm-one/ctem/CtemStageAgentsCard";
import { TwoFactorRow } from "@/pages/profile/TwoFactorRow";
import { EmailRow } from "@/pages/profile/EmailRow";
import { ToolApprovalsRow } from "@/pages/profile/ToolApprovalsRow";
import { HealthCompanionCard } from "@/pages/profile/HealthCompanionCard";
import { ReplySuggestionsCard } from "@/pages/profile/ReplySuggestionsCard";
import { WaitingWorkItemsNotifyCard } from "@/pages/profile/WaitingWorkItemsNotifyCard";
import { HealthOptOutRow } from "@/pages/profile/HealthOptOutRow";
import { PersonalApiKeys } from "@/pages/profile/PersonalApiKeys";
import { MyMemoryConsolidations } from "@/pages/profile/MemoryConsolidations";
import { AgentAccountsSection } from "@/pages/profile/agent-accounts/AgentAccountsSection";
import { PhonesSection } from "@/pages/profile/PhonesSection";
import { ToolApprovalList } from "@/pages/tools/ToolApprovalList";
import { isProfileTab, type ProfileTab } from "@/pages/profile/profile-tabs";
import RouteNotFound from "@/pages/RouteNotFound";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { hitlGlyph } from "@/lib/hitl";
import { cn, toggleArrayItem, formatCompactNumber, formatQuotaLimit, mergeSandboxIntoAgenticCount, QUOTA_NOT_APPLICABLE_LABEL, validatePassword, MIN_PASSWORD_LENGTH, pluralize } from "@/lib/utils";
import { ICON_MAP, INTEGRATION_PROVIDER_NAMES, ssoProviderLabel, inferMCPLogo, COPY_FEEDBACK_DELAY, CHANNEL_ICONS, CHANNEL_TYPE_LABELS, COMMON_TIMEZONES, QUOTA_PERIOD_LABELS } from "@/lib/constants";
import { applyTheme, effectiveTheme } from "@/hooks/useThemeEffect";
import { useFormatter, AVAILABLE_LANGUAGES, LANGUAGE_AUTO, languageLabel } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";

type Tab = ProfileTab;

export default function Profile() {
  const { data: user, isLoading } = useMe();
  const updateProfile = useUpdateProfile();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const isXtmOne = useIsXtmOne();
  const [tab] = useTabParam<Tab>("overview");
  const { tab: tabSegment } = useParams<{ tab?: string }>();
  // Linked phones belong to the mobile companion: no tab while it is off.
  const companionEnabled = useFeatureFlag("MOBILE_COMPANION");
  /* Approvals are opt-in: the tab appears once an agent asks before running
     tools, or while this person still holds a choice of their own — the
     place to take it back must never disappear. A link to it always works. */
  const { data: approvals } = useToolApprovals();
  const showApprovalsTab =
    tab === "approvals" ||
    approvals?.approvals_in_use === true ||
    approvals?.approve_all_tools === true ||
    (approvals?.preferences.length ?? 0) > 0;
  // Live ``min_password_length`` from the backend so the inline error
  // and placeholder copy track the configured server-side floor.
  // Falls back to ``MIN_PASSWORD_LENGTH`` while the auth-config query
  // is in flight on first paint.
  const { data: authConfig } = useAuthConfig();
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  // The runners live on the Desktop page (`/desktop`), one click from the
  // sidebar instead of a tab under the profile.
  const TABS = useMemo<{ key: Tab; label: string; icon: React.ElementType }[]>(
    () => [
      { key: "overview", label: "Profile", icon: UserIcon },
      ...(showApprovalsTab
        ? [{ key: "approvals" as const, label: "Tool approvals", icon: hitlGlyph("topic") }]
        : []),
      { key: "channels", label: "Channels & Bots", icon: Radio },
      ...(companionEnabled ? [{ key: "phones" as const, label: "Phones", icon: Smartphone }] : []),
      { key: "agent-accounts", label: "Agent Accounts", icon: UserCog },
      { key: "memory", label: "Memory", icon: Brain },
      { key: "quotas", label: "Quotas", icon: BarChart3 },
      { key: "api-keys", label: "API Keys", icon: Key },
      { key: "mcp", label: "MCP Endpoint", icon: Server },
    ],
    [showApprovalsTab, companionEnabled],
  );

  const changePassword = useChangePassword();

  const [editing, setEditing] = useState(false);
  const [fullName, setFullName] = useState("");
  const [title, setTitle] = useState("");
  const [timezone, setTimezone] = useState("");
  const [theme, setTheme] = useState<string>("dark");
  const [language, setLanguage] = useState<string>(LANGUAGE_AUTO);

  // Detect browser timezone for default suggestion
  const browserTimezone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);

  // Password change
  const [showPasswordForm, setShowPasswordForm] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [passwordSuccess, setPasswordSuccess] = useState(false);

  // A segment that names no tab (the former `runners` included) is not found,
  // before the profile header renders: no redirect, no empty page (#822).
  if (!isProfileTab(tabSegment)) return <RouteNotFound />;
  if (isLoading || !user) return <PageSkeleton cards={2} />;

  const startEditing = () => {
    setFullName(user.full_name);
    setTitle(user.title ?? "");
    setTimezone(user.timezone ?? "");
    setTheme(effectiveTheme(user.theme));
    setLanguage(user.language ?? LANGUAGE_AUTO);
    setEditing(true);
  };

  const handleSave = () => {
    updateProfile.mutate(
      {
        full_name: fullName,
        title: title || undefined,
        timezone: timezone || null,
        theme,
        language,
      },
      {
        onSuccess: () => {
          applyTheme(theme);
          toast({ title: t_i18n("Profile updated"), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  const handlePasswordChange = () => {
    setPasswordError("");
    setPasswordSuccess(false);
    const pwErr = validatePassword(newPassword, minPasswordLength);
    if (pwErr) {
      setPasswordError(pwErr);
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError(t_i18n("Passwords do not match"));
      return;
    }
    changePassword.mutate(
      { current_password: currentPassword, new_password: newPassword },
      {
        onSuccess: () => {
          setPasswordSuccess(true);
          setCurrentPassword("");
          setNewPassword("");
          setConfirmPassword("");
          setTimeout(() => {
            setShowPasswordForm(false);
            setPasswordSuccess(false);
          }, COPY_FEEDBACK_DELAY);
        },
        onError: (err) => {
          setPasswordError(err.message || t_i18n("Failed to change password"));
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10 ring-2 ring-accent/20">
            <UserIcon className="h-6 w-6 text-accent" />
          </div>
          <div>
            <h1 className="text-3xl font-bold">{user.full_name}</h1>
            <div className="flex items-center gap-2 mt-1">
              {user.title && <span className="content-base text-accent">{user.title}</span>}
              {user.role === "admin" ? (
                <Badge variant="cyan" className="gap-1 content-caption"><Shield className="h-2.5 w-2.5" /> {t_i18n("Admin")}</Badge>
              ) : (
                <Badge variant="outline" className="gap-1 content-caption"><UserIcon className="h-2.5 w-2.5" /> {t_i18n("User")}</Badge>
              )}
              {user.sso_provider && (
                <Badge variant="outline" className="content-caption">{ssoProviderLabel(user.sso_provider)}</Badge>
              )}
            </div>
          </div>
        </div>
        {tab === "overview" && (
          !editing ? (
            <Button variant="secondary" className="gap-1.5" onClick={startEditing}>
              <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit Profile")}
            </Button>
          ) : (
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => setEditing(false)}>{t_i18n("Cancel")}</Button>
              <Button className="gap-1.5" onClick={handleSave} disabled={updateProfile.isPending || !fullName.trim()} aria-busy={updateProfile.isPending || undefined}>
                {updateProfile.isPending && <Spinner size="sm" tone="inherit" />}{!updateProfile.isPending && <Save className="h-3.5 w-3.5" />}
                {t_i18n("Save")}
              </Button>
            </div>
          )
        )}
      </div>

      {/* Tabs */}
      <DetailTabs tabs={TABS} active={tab} />

      {/* ── Overview Tab: Account + GA Tools ──────────────────────────── */}
      {tab === "overview" && (
        <div className="space-y-6">
          <Card>
            <CardContent className="p-0">
              {/* Info rows */}
              <div className="divide-y divide-[var(--border-elevation-subtle)]">
                {editing ? (
                  <div className="px-5 py-4 space-y-3">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="space-y-1">
                        <Label id="profile-full-name-label" htmlFor="profile-full-name">{t_i18n("Full Name")}</Label>
                        <Input id="profile-full-name" aria-labelledby="profile-full-name-label" value={fullName} onChange={(e) => setFullName(e.target.value)} autoFocus />
                      </div>
                      <div className="space-y-1">
                        <Label id="profile-title-label" htmlFor="profile-title">{t_i18n("Title")}</Label>
                        <Input id="profile-title" aria-labelledby="profile-title-label" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t_i18n("e.g. CEO, Engineering Manager")} />
                      </div>
                    </div>
                  </div>
                ) : null}

                <EmailRow email={user.email} ssoProvider={user.sso_provider} impersonated={!!user.impersonated_by_id} />

                <InfoRow icon={Globe} label={t_i18n("Timezone")} editing={editing} fieldId="profile-timezone">
                  {editing ? (
                    <div className="flex items-center gap-2 max-w-xs">
                      <SearchableSelect
                        id="profile-timezone"
                        aria-labelledby="profile-timezone-label"
                        options={COMMON_TIMEZONES.map((tz) => ({ value: tz, label: tz.replace(/_/g, " ") }))}
                        pinned={[{ value: "__none__", label: t_i18n("Not set") }]}
                        value={timezone || "__none__"}
                        onValueChange={(v) => setTimezone(v === "__none__" ? "" : v)}
                        placeholder={t_i18n("Select timezone")}
                      />
                      {!timezone && (
                        <Button variant="secondary" className="shrink-0" data-testid="profile.timezone-detect" onClick={() => setTimezone(browserTimezone)}>
                          {t_i18n("Detect")} ({browserTimezone.replace(/_/g, " ")})
                        </Button>
                      )}
                    </div>
                  ) : (
                    <span className="content-base">
                      {user.timezone ? user.timezone.replace(/_/g, " ") : <span className="text-default-secondary">{t_i18n("Not set (UTC used for AI context)")}</span>}
                    </span>
                  )}
                </InfoRow>

                <InfoRow icon={Palette} label={t_i18n("Theme")} editing={editing} fieldId="profile-theme">
                  {editing ? (
                    <Select value={theme} onValueChange={setTheme}>
                      <SelectTrigger id="profile-theme" aria-labelledby="profile-theme-label" className="max-w-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="dark">{t_i18n("Dark")}</SelectItem>
                        <SelectItem value="light">{t_i18n("Light")}</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : (
                    <span className="content-base">{effectiveTheme(user.theme) === "light" ? t_i18n("Light") : t_i18n("Dark")}</span>
                  )}
                </InfoRow>

                <InfoRow icon={Languages} label={t_i18n("Language")} editing={editing} fieldId="profile-language">
                  {editing ? (
                    <Select value={language} onValueChange={setLanguage}>
                      <SelectTrigger id="profile-language" aria-labelledby="profile-language-label" className="max-w-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={LANGUAGE_AUTO}>
                          <span className="italic">{t_i18n("Automatic")}</span>
                        </SelectItem>
                        {AVAILABLE_LANGUAGES.map(({ value, label }) => (
                          <SelectItem key={value} value={value}>{label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <span className="content-base">
                      {languageLabel(user.language) ?? <span className="text-default-secondary">{t_i18n("Automatic")}</span>}
                    </span>
                  )}
                </InfoRow>

                <InfoRow icon={Fingerprint} label={t_i18n("Authentication")}>
                  <span className="content-base">{user.sso_provider ? `SSO (${ssoProviderLabel(user.sso_provider)})` : t_i18n("Local account")}</span>
                </InfoRow>

                {/* Password change (local accounts only) — inline as last row */}
                {!user.sso_provider && (
                  <div className="px-5 py-3.5">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <Lock className="h-4 w-4 text-accent shrink-0" />
                        <span className="content-base font-medium w-36 shrink-0" data-testid="profile.password-label">{t_i18n("Password")}</span>
                      </div>
                      {!showPasswordForm && (
                        <Button variant="secondary" className="gap-1.5" data-testid="profile.password-change" onClick={() => { setShowPasswordForm(true); setPasswordError(""); setPasswordSuccess(false); }}>
                          <Pencil className="h-3.5 w-3.5" /> {t_i18n("Change")}
                        </Button>
                      )}
                    </div>
                    {showPasswordForm && (
                      <div className="mt-3 space-y-3 max-w-sm pl-7">
                        <div className="space-y-1">
                          <Label id="profile-current-password-label" htmlFor="profile-current-password">{t_i18n("Current Password")}</Label>
                          <Input id="profile-current-password" aria-labelledby="profile-current-password-label" type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} placeholder={t_i18n("Enter current password")} autoFocus />
                        </div>
                        <div className="space-y-1">
                          <Label id="profile-new-password-label" htmlFor="profile-new-password">{t_i18n("New Password")}</Label>
                          <Input id="profile-new-password" aria-labelledby="profile-new-password-label" type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder={t_i18n("At least {min} characters, with a letter and a number", { min: minPasswordLength })} />
                        </div>
                        <div className="space-y-1">
                          <Label id="profile-confirm-password-label" htmlFor="profile-confirm-password">{t_i18n("Confirm New Password")}</Label>
                          <Input id="profile-confirm-password" aria-labelledby="profile-confirm-password-label" type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder={t_i18n("Confirm new password")} />
                        </div>
                        {passwordError && <p className="content-compact text-feedback-error-primary">{passwordError}</p>}
                        {passwordSuccess && <p className="content-compact text-feedback-success-primary">{t_i18n("Password changed successfully!")}</p>}
                        <div className="flex justify-end gap-2" data-testid="profile.password-actions">
                          <Button variant="secondary" onClick={() => { setShowPasswordForm(false); setPasswordError(""); }}>
                            {t_i18n("Cancel")}
                          </Button>
                          <Button onClick={handlePasswordChange} disabled={!currentPassword || !newPassword || !confirmPassword || changePassword.isPending} aria-busy={changePassword.isPending || undefined}>
                            {changePassword.isPending && <Spinner size="sm" tone="inherit" />}
                            {t_i18n("Update Password")}
                          </Button>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {/* Two-factor authentication — sits next to the password
                    row it complements. Shown for SSO accounts too: the factor is
                    XTM One's own, layered on top of whichever way the first
                    factor was proven (password or IdP assertion). */}
                <TwoFactorRow mfa={user.mfa} email={user.email} />

              </div>
            </CardContent>
          </Card>

          {/* General Assistant Tools — only relevant when the General Assistant is
              the user's default chat agent. A custom default agent (default_agent_id
              set) replaces the General Assistant, so its tool overrides are moot. */}
          {!user.default_agent_id && <GAToolsSection />}

          {/* Per-user CTEM analyst overrides (XTM One). Mirrors the General
              Assistant model override: pick your own agent for the briefing /
              any stage, overriding the admin-set platform default. */}
          {isXtmOne && <CtemStageAgentsCard scope="user" />}

          {/* The bell's waiting work items (#9437): on by default. */}
          <WaitingWorkItemsNotifyCard />

          {/* The composer's reply suggestion after a chat turn: on by default. */}
          <ReplySuggestionsCard />

          {/* The Health companion (#8261): off by default. */}
          <HealthCompanionCard />
        </div>
      )}

      {/* ── Tool approvals Tab ─────────────────────────────────────── */}
      {/* Every personal control of tool approvals, and only those: the
          platform's rules are in Settings > Access and security > Tool approvals. The
          only place a user can take back a "Yes, always" granted inside a run. */}
      {tab === "approvals" && (
        <div className="space-y-6">
          <ToolApprovalsRow />
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="content-base">{t_i18n("Tools that ask first")}</CardTitle>
              <CardDescription className="content-compact">
                {t_i18n(
                  "Always allow a tool for yourself: it stops asking you on every agent, and still asks everyone else first.",
                )}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-0">
              <ToolApprovalList scope="personal" />
            </CardContent>
          </Card>
        </div>
      )}

      {/* ── Channels & Bots Tab ─────────────────────────────────────── */}
      {tab === "channels" && <ChannelBotsSection />}

      {/* ── Phones Tab: the linked phones (mobile companion) ──────────── */}
      {tab === "phones" && companionEnabled && <PhonesSection />}

      {/* ── Agent Accounts Tab ─────────────────────────────────── */}
      {tab === "agent-accounts" && <AgentAccountsSection />}

      {/* ── API Keys Tab ─────────────────────────────────────────────── */}
      {tab === "api-keys" && <ApiKeysSection />}

      {/* ── Memory Tab ────────────────────────────────────────────────── */}
      {tab === "memory" && <ProfileMemorySection />}

      {/* ── Quotas Tab ───────────────────────────────────────────────── */}
      {tab === "quotas" && <QuotaProfileSection />}

      {/* ── MCP Endpoint Tab ──────────────────────────────────────────── */}
      {tab === "mcp" && <MCPEndpointProfileSection />}
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════════════
   Profile Memory Section
   ═══════════════════════════════════════════════════════════════════════════ */

function ProfileMemorySection() {
  const { t_i18n, intl } = useFormatter();
  const { data: memory, isLoading } = useMyMemory();
  const purgeAll = usePurgeMyMemory();
  const purgeUserProfile = usePurgeMyUserProfile();
  const purgeAgentMemory = usePurgeMyAgentMemory();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const [expandedAgents, setExpandedAgents] = useState<Set<string>>(new Set());
  const [profileExpanded, setProfileExpanded] = useState(false);

  const agentNotes = (memory?.user_profile?.entries ?? []).filter(entry => entry.scope !== "global");
  const hasUserProfile = !!(memory?.user_profile?.medium_term || memory?.user_profile?.legacy_profile || memory?.user_profile?.short_term?.length || agentNotes.length);
  const userMemoryAgentIds = new Set([
    ...agentNotes.map(note => note.owner_agent_id),
    ...(memory?.user_profile?.short_term ?? []).map(entry => entry.agent_id),
  ].filter((id): id is string => !!id));
  const holdsMemory = (a: UserMemoryAgent) =>
    !!(a.redis_memory?.medium_term || a.redis_memory?.short_term?.length || a.persistent_count > 0 || userMemoryAgentIds.has(a.agent_id));
  const hasAgentMemories = !!memory?.agents?.some(holdsMemory);
  const hasAny = hasUserProfile || hasAgentMemories;

  const toggleAgent = (id: string) => {
    setExpandedAgents(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const handlePurgeAll = async () => {
    const ok = await confirm({
      title: "Purge all your memories",
      description: "This will permanently delete your cross-agent profile and all per-agent memories. Agents will no longer remember any context from your previous conversations.",
      confirmLabel: "Purge All",
      variant: "danger",
    });
    if (!ok) return;
    purgeAll.mutate(undefined, {
      onSuccess: () => toast({ title: t_i18n("All memories purged"), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to purge memories"), variant: "error" }),
    });
  };

  const handlePurgeUserProfile = async () => {
    const ok = await confirm({
      title: "Purge cross-agent profile",
      description: "This will delete your cross-agent user profile. All agents will lose the shared context about your preferences, role, and communication style.",
      confirmLabel: "Purge Profile",
      variant: "danger",
    });
    if (!ok) return;
    purgeUserProfile.mutate(undefined, {
      onSuccess: () => toast({ title: t_i18n("User profile memory purged"), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to purge profile"), variant: "error" }),
    });
  };

  const handlePurgeAgent = async (agentId: string, agentName: string) => {
    const ok = await confirm({
      title: t_i18n("Purge memory with {name}", { name: agentName }),
      description: t_i18n('This will delete all memory between you and "{name}". The agent will no longer remember your past interactions.', { name: agentName }),
      confirmLabel: "Purge",
      variant: "danger",
    });
    if (!ok) return;
    purgeAgentMemory.mutate(agentId, {
      onSuccess: () => toast({ title: t_i18n("Memory with {name} purged", { name: agentName }), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to purge agent memory"), variant: "error" }),
    });
  };

  if (isLoading) return <div className="flex items-center justify-center py-20 text-default-secondary"><Spinner size="md" tone="inherit" className="mr-2" /> {t_i18n("Loading memories...")}</div>;

  const IconMap = ICON_MAP;

  return (
    <div className="space-y-6">
      {ConfirmEl}

      {/* Header with purge all */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-default-secondary content-base">
            {t_i18n("This is what agents remember about you. Each agent maintains a separate memory of your interactions, and a cross-agent profile is shared with all agents.")}
          </p>
        </div>
        {hasAny && (
          <Button variant="destructive" className="gap-1.5 shrink-0" data-testid="profile.memory-purge-all" onClick={handlePurgeAll} disabled={purgeAll.isPending} aria-busy={purgeAll.isPending || undefined}>
            {purgeAll.isPending && <Spinner size="sm" tone="inherit" />}{!purgeAll.isPending && <Trash2 className="h-3.5 w-3.5" />}
            {t_i18n("Purge All")}
          </Button>
        )}
      </div>

      {/* Platform Health opt-out (#8254) */}
      <HealthOptOutRow />

      <MyMemoryConsolidations />

      {/* Cross-agent user profile */}
      <Card>
        <CardHeader className="cursor-pointer" onClick={() => setProfileExpanded(!profileExpanded)}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              {profileExpanded ? <ChevronDown className="h-4 w-4 text-default-secondary" /> : <ChevronRight className="h-4 w-4 text-default-secondary" />}
              <Brain className="h-5 w-5 text-accent" />
              <div>
                <CardTitle className="text-base">{t_i18n("Cross-Agent Profile")}</CardTitle>
                <CardDescription>{t_i18n("Shared with all agents: your role, preferences, and patterns")}</CardDescription>
              </div>
            </div>
            {hasUserProfile && (
              <Button variant="ghost" className="text-feedback-error-primary hover:text-feedback-error-primary gap-1.5" data-testid="profile.memory-clear-profile" onClick={(e) => { e.stopPropagation(); handlePurgeUserProfile(); }} disabled={purgeUserProfile.isPending} aria-busy={purgeUserProfile.isPending || undefined}>
                {purgeUserProfile.isPending && <Spinner size="sm" tone="inherit" />}{!purgeUserProfile.isPending && <Trash2 className="h-3 w-3" />}
                {t_i18n("Clear")}
              </Button>
            )}
          </div>
        </CardHeader>
        {profileExpanded && (
          <CardContent className="pt-0 space-y-4">
            {memory?.user_profile?.medium_term ? (
              <div className="rounded-sm bg-elevation-default p-4 content-base whitespace-pre-wrap leading-relaxed">
                {memory.user_profile.medium_term}
              </div>
            ) : memory?.user_profile?.legacy_profile ? (
              <div className="space-y-2">
                <p className="content-compact text-default-secondary">{t_i18n("Not reorganized yet: no agent reads this profile until it is split into your profile and each agent's notes.")}</p>
                <div className="rounded-sm bg-elevation-default p-4 content-base whitespace-pre-wrap leading-relaxed text-default-secondary">
                  {memory.user_profile.legacy_profile}
                </div>
              </div>
            ) : (
              <p className="content-base text-default-secondary italic">{t_i18n("No cross-agent profile yet. It will be built as you interact with agents.")}</p>
            )}
            {memory?.user_profile?.short_term && memory.user_profile.short_term.length > 0 && (
              <div>
                <p className="content-compact font-medium text-default-secondary mb-2">{t_i18n("Recent interactions")} ({memory.user_profile.short_term.length})</p>
                <div className="space-y-1.5">
                  {memory.user_profile.short_term.slice().reverse().map((entry, i) => (
                    <div key={i} className="content-compact text-default-secondary flex gap-2">
                      <span className="text-accent shrink-0">{t_i18n("via {agent}", { agent: entry.agent })}</span>
                      <span className="truncate">{entry.summary}</span>
                      <span className="shrink-0 tabular-nums">{formatTimeAgo(intl, entry.timestamp)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {agentNotes.length > 0 && (
              <div>
                <p className="content-compact font-medium text-default-secondary">{t_i18n("Notes about agents' work ({count})", { count: agentNotes.length })}</p>
                <p className="content-compact text-default-secondary mb-2">{t_i18n("Each note is read only by the agent whose work it is about. A note about something that can change is dropped after 72 hours.")}</p>
                <div className="space-y-1.5">
                  {agentNotes.map(note => (
                    <div key={note.id} className="content-compact text-default-secondary flex items-start gap-2">
                      <span className="text-accent shrink-0">{note.owner_agent_name}</span>
                      <span className="flex-1 min-w-0 break-words">{note.text}</span>
                      {note.perishable && (
                        <Badge variant="outline" className="shrink-0 content-caption">{t_i18n("Unverified")}</Badge>
                      )}
                      <span className="shrink-0 tabular-nums">{formatTimeAgo(intl, note.last_verified_at)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        )}
      </Card>

      {/* Per-agent memories */}
      <div>
        <h3 className="content-base font-medium text-default-secondary mb-3">{t_i18n("Per-Agent Memory ({count})", { count: memory?.agents?.length || 0 })}</h3>
        {!hasAgentMemories && (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center justify-center py-12 text-center">
              <Bot className="h-10 w-10 text-default-disabled mb-3" />
              <p className="font-medium">{t_i18n("No agent memories yet")}</p>
              <p className="content-base text-default-secondary mt-1">{t_i18n("As you chat with agents, they will build memories here.")}</p>
            </CardContent>
          </Card>
        )}
        <div className="space-y-2">
          {memory?.agents?.filter(holdsMemory).map(agent => {
            const isExpanded = expandedAgents.has(agent.agent_id);
            const AgentIcon = agent.agent_icon && IconMap[agent.agent_icon as keyof typeof IconMap] ? IconMap[agent.agent_icon as keyof typeof IconMap] : Bot;
            const shortCount = agent.redis_memory?.short_term?.length || 0;
            const hasMedium = !!agent.redis_memory?.medium_term;
            return (
              <Card key={agent.agent_id}>
                <CardHeader className="cursor-pointer py-3" onClick={() => toggleAgent(agent.agent_id)}>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      {isExpanded ? <ChevronDown className="h-4 w-4 text-default-secondary" /> : <ChevronRight className="h-4 w-4 text-default-secondary" />}
                      <AgentIcon className="h-5 w-5 text-accent" />
                      <div>
                        <CardTitle className="content-base">{agent.agent_name}</CardTitle>
                        <CardDescription className="content-compact">
                          {[
                            hasMedium && t_i18n("consolidated memory"),
                            shortCount > 0 && t_i18n("{count} recent", { count: shortCount }),
                            agent.persistent_count > 0 && t_i18n("{count} persistent", { count: agent.persistent_count }),
                          ].filter(Boolean).join(" · ")}
                        </CardDescription>
                      </div>
                    </div>
                    <Button variant="ghost" className="text-feedback-error-primary hover:text-feedback-error-primary gap-1.5" data-testid="profile.memory-clear-agent" onClick={(e) => { e.stopPropagation(); handlePurgeAgent(agent.agent_id, agent.agent_name); }} disabled={purgeAgentMemory.isPending} aria-busy={purgeAgentMemory.isPending || undefined}>
                      {purgeAgentMemory.isPending && <Spinner size="sm" tone="inherit" />}{!purgeAgentMemory.isPending && <Trash2 className="h-3 w-3" />}
                      {t_i18n("Clear")}
                    </Button>
                  </div>
                </CardHeader>
                {isExpanded && (
                  <CardContent className="pt-0 space-y-4">
                    {agent.redis_memory?.medium_term && (
                      <div>
                        <p className="content-compact font-medium text-default-secondary mb-2">{t_i18n("Consolidated Memory")}</p>
                        <div className="rounded-sm bg-elevation-default p-3 content-base whitespace-pre-wrap leading-relaxed">
                          {agent.redis_memory.medium_term}
                        </div>
                      </div>
                    )}
                    {agent.redis_memory?.short_term && agent.redis_memory.short_term.length > 0 && (
                      <div>
                        <p className="content-compact font-medium text-default-secondary mb-2">{t_i18n("Recent Interactions ({count})", { count: agent.redis_memory.short_term.length })}</p>
                        <div className="space-y-1.5">
                          {agent.redis_memory.short_term.slice().reverse().map((entry, i) => (
                            <div key={i} className="content-compact text-default-secondary flex gap-2">
                              <span className="truncate">{entry.summary}</span>
                              <span className="shrink-0 tabular-nums">{formatTimeAgo(intl, entry.timestamp)}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {agent.persistent_count > 0 && (
                      <p className="content-compact text-default-secondary">{t_i18n("+ {count} persistent {word} in long-term storage", { count: agent.persistent_count, word: t_i18n(pluralize(agent.persistent_count, "memory", "memories")) })}</p>
                    )}
                  </CardContent>
                )}
              </Card>
            );
          })}
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Channels & Bots Section – per-user channel agent overrides for DMs
// ---------------------------------------------------------------------------

/** Maps channel bot provider to the channel routing type. */
const PROVIDER_TO_CHANNEL: Record<string, string> = {
  slack_bot: "slack",
  teams_bot: "teams",
  whatsapp_bot: "whatsapp",
  telegram_bot: "telegram",
};

function ChannelBotsSection() {
  const { t_i18n } = useFormatter();
  const { data: user } = useMe();
  const { data: channelBots } = useChannelBots();
  const { data: agents } = useAgents();
  const updateProfile = useUpdateProfile();
  const { toast } = useToast();

  if (!user) return null;

  // Only show connected bots that have a known channel mapping
  const connectedBots = (channelBots ?? []).filter(
    (b) => b.status === "connected" && PROVIDER_TO_CHANNEL[b.provider],
  );

  // Chat-enabled agents (same filter as default agent picker)
  const chatAgents = (agents ?? [])
    .filter((a) => a.enabled && !a.disable_chat)
    .sort((a, b) => a.name.localeCompare(b.name));

  const channelConfig = user.channel_agent_config ?? {};

  const setChannelAgent = (channelType: string, agentId: string | null) => {
    const updated = { ...channelConfig };
    if (agentId) {
      updated[channelType] = agentId;
    } else {
      delete updated[channelType];
    }
    // Send null if empty to clear the config, otherwise the updated map
    const payload = Object.keys(updated).length > 0 ? updated : null;
    updateProfile.mutate(
      { channel_agent_config: payload },
      { onSuccess: () => toast({ title: t_i18n("Channel agent updated"), variant: "success" }) },
    );
  };

  const setDefaultAgent = (agentId: string) => {
    updateProfile.mutate(
      { default_agent_id: agentId || "" },
      { onSuccess: () => toast({ title: t_i18n("Default chat agent updated"), variant: "success" }) },
    );
  };

  const defaultAgentCard = (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Bot className="h-4 w-4 text-accent" />
          {t_i18n("Default Chat Agent")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("Choose which agent handles your web chat conversations by default.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <SearchableSelect
          aria-label={t_i18n("Default Chat Agent")}
          className="max-w-xs"
          options={chatAgents.map((a) => agentSelectOption(a))}
          pinned={[{ value: "__general__", label: t_i18n("General Assistant"), icon: <Sparkles className="h-3.5 w-3.5 text-accent" /> }]}
          value={user.default_agent_id || "__general__"}
          onValueChange={(v) => setDefaultAgent(v === "__general__" ? "" : v)}
          placeholder={t_i18n("Search agents...")}
        />
      </CardContent>
    </Card>
  );

  if (connectedBots.length === 0) {
    return (
      <div className="space-y-6">
        {defaultAgentCard}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="content-base flex items-center gap-2">
              <Radio className="h-4 w-4 text-accent" />
              {t_i18n("External Channels")}
            </CardTitle>
            <CardDescription className="content-compact">
              {t_i18n("No channel bots are configured yet. An administrator can set up Slack, Teams, WhatsApp, or Telegram bots in Settings.")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex items-center justify-center rounded-sm border border-dashed border-elevation-subtle-soft py-8">
              <p className="content-compact text-default-secondary">{t_i18n("No channel bots available")}</p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {defaultAgentCard}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Radio className="h-4 w-4 text-accent" />
            {t_i18n("External Channels")}
          </CardTitle>
          <CardDescription className="content-compact">
            {t_i18n("Assign your own agent for private conversations (DMs) on each messaging platform. When set, your agent will handle direct messages instead of the platform-routed agent.")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 pt-0">
        {connectedBots.map((bot) => {
          const channelType = PROVIDER_TO_CHANNEL[bot.provider];
          const iconCfg = CHANNEL_ICONS[bot.provider];
          const Icon = iconCfg?.icon ?? MessageSquare;
          const selectedAgentId = channelConfig[channelType] ?? "";
          return (
            <div
              key={bot.id}
              className={cn(
                "flex items-center gap-4 rounded-sm px-4 py-3.5 transition-colors",
                selectedAgentId
                  ? "bg-accent/5 ring-1 ring-accent/20"
                  : "bg-elevation-hover",
              )}
            >
              {/* Bot icon & info */}
              <div className={cn("flex h-9 w-9 items-center justify-center rounded-sm shrink-0", iconCfg?.bg ?? "bg-elevation-hover")}>
                {iconCfg?.logo
                  ? <img src={iconCfg.logo} alt={bot.name} className="h-4.5 w-4.5 object-contain" />
                  : <Icon className={cn("h-4.5 w-4.5", iconCfg?.fg ?? "text-default-secondary")} />}
              </div>
              <div className="flex-1 min-w-0">
                <p className="content-base font-medium">{bot.name}</p>
                <p className="content-caption text-default-secondary">
                  {CHANNEL_TYPE_LABELS[channelType] ?? channelType} &middot; {t_i18n("Private conversations only")}
                </p>
              </div>

              {/* Agent picker */}
              <div className="flex items-center gap-2 shrink-0">
                <SearchableSelect
                  aria-label={t_i18n("Agent for {channel}", { channel: bot.name })}
                  className="w-60"
                  options={chatAgents.map((a) => agentSelectOption(a))}
                  pinned={[{ value: "__none__", label: t_i18n("Default routing") }]}
                  value={selectedAgentId || "__none__"}
                  onValueChange={(v) => setChannelAgent(channelType, v === "__none__" ? null : v)}
                  placeholder={t_i18n("Search agents...")}
                />
                {selectedAgentId && (
                  <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Clear selected agent")} icon={<X className="h-3 w-3" />} className="h-7 w-7 shrink-0 text-default-secondary hover:text-feedback-error-primary" onClick={() => setChannelAgent(channelType, null)} disabled={updateProfile.isPending} />
                )}
              </div>
            </div>
          );
        })}

        {/* Info note */}
        <div className="rounded-sm bg-elevation-hover p-3 mt-3">
          <p className="content-caption text-default-secondary leading-relaxed">
            {t_i18n("When you assign an agent to a channel, it will handle your direct messages (DMs) on that platform. Group channels and @mentions will continue to use the admin-configured routing.")}
          </p>
        </div>
      </CardContent>
      </Card>
    </div>
  );
}


// ---------------------------------------------------------------------------
// GA Tools Section – per-user General Assistant tool overrides
// ---------------------------------------------------------------------------
/** Exported for its unit test (`Profile.test.tsx`). */
export function GAToolsSection() {
  const { t_i18n } = useFormatter();
  const { data: user } = useMe();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const { data: integrations } = useIntegrations();
  const { data: mcpServers } = useMCPServers();
  const { data: customTools } = useCustomTools();
  const { data: knowledgeBases } = useBindableKnowledgeBases();
  const { data: browserProfiles } = useBrowserProfiles();
  const updateProfile = useUpdateProfile();
  const { toast } = useToast();
  const {
    offered: offeredBuiltinTools,
    carries: carriesBuiltin,
    canonicalIds: canonicalBuiltinIds,
    isReady: builtinToolsReady,
    isError: builtinToolsFailed,
  } = useBuiltinTools();
  const customToolsEnabled = useCustomToolsEnabled();
  const [search, setSearch] = useState("");

  if (!user || !gaConfig) return null;

  // Platform defaults
  const platformBuiltins = gaConfig.builtin_tool_ids ?? [];
  const platformIntegrations = gaConfig.integration_bindings ?? {};
  const platformMCPs = gaConfig.mcp_server_ids ?? [];
  const platformCustomTools = gaConfig.custom_tool_ids ?? [];
  const platformKBs = gaConfig.knowledge_base_ids ?? [];

  // Effective values (user override or platform defaults)
  const effectiveBuiltins = user.ga_builtin_tool_ids ?? platformBuiltins;
  const effectiveIntegrations = user.ga_integration_bindings ?? platformIntegrations;
  const effectiveMCPs = user.ga_mcp_server_ids ?? platformMCPs;
  const effectiveCustomTools = user.ga_custom_tool_ids ?? platformCustomTools;
  const effectiveKBs = user.ga_knowledge_base_ids ?? platformKBs;
  // No platform default: a browser profile is a login, given by its user only.
  const effectiveProfiles = user.ga_browser_profile_ids ?? [];

  const hasOverride = user.ga_builtin_tool_ids !== null
    || user.ga_integration_bindings !== null
    || user.ga_mcp_server_ids !== null
    || user.ga_custom_tool_ids !== null
    || user.ga_knowledge_base_ids !== null
    || effectiveProfiles.length > 0;

  // Available resources (user's own + company-managed, filtered to connected/enabled, excluding channel bots)
  const connectedIntegrations = (integrations ?? []).filter(
    (i) => i.status === "connected",
  );
  const enabledMCPs = (mcpServers ?? []).filter((s) => s.enabled && !s.hide_from_chat);
  const enabledCustomTools = (customTools ?? []).filter((t) => t.enabled && !t.hide_from_chat);
  const allKBs = knowledgeBases ?? [];

  // Helper to save a specific override
  const saveOverride = (field: string, value: unknown) => {
    updateProfile.mutate(
      { [field]: value } as Record<string, unknown>,
      { onSuccess: () => toast({ title: t_i18n("Assistant tools updated"), variant: "success" }) },
    );
  };

  // Toggle built-in tool
  const toggleBuiltin = (toolId: string) => {
    // A former id toggles as its group: the saved list names the group.
    saveOverride("ga_builtin_tool_ids", toggleArrayItem(canonicalBuiltinIds(effectiveBuiltins), toolId));
  };

  // Toggle integration binding
  const toggleIntegration = (integration: { id: string; provider: string }) => {
    const current = { ...effectiveIntegrations };
    if ((current[integration.provider] || []).includes(integration.id)) {
      current[integration.provider] = (current[integration.provider] || []).filter((id) => id !== integration.id);
      if (current[integration.provider].length === 0) delete current[integration.provider];
    } else {
      current[integration.provider] = [...(current[integration.provider] || []), integration.id];
    }
    saveOverride("ga_integration_bindings", current);
  };

  // Toggle list-based resources (MCP, custom tools, KBs)
  const toggleListItem = (field: string, effectiveList: string[], itemId: string) => {
    saveOverride(field, toggleArrayItem(effectiveList, itemId));
  };

  // Reset all overrides to platform defaults
  const resetAll = () => {
    updateProfile.mutate(
      {
        ga_builtin_tool_ids: null,
        ga_custom_tool_ids: null,
        ga_integration_bindings: null,
        ga_mcp_server_ids: null,
        ga_knowledge_base_ids: null,
        ga_browser_profile_ids: null,
      },
      { onSuccess: () => toast({ title: t_i18n("Reset to platform defaults"), variant: "success" }) },
    );
  };

  // Build flat tool items for compact searchable display
  type ToolItem = {
    id: string;
    label: string;
    description?: string;
    category: string;
    categoryColor: string;
    icon: React.ReactNode;
    checked: boolean;
    onToggle: () => void;
  };

  const CATEGORY_BADGES: Record<string, string> = {
    "Built-in Tools": "Built-in",
    "Integrations": "Integration",
    "MCP Servers": "MCP",
    "Custom Tools": "Custom",
    "Knowledge Bases": "KB",
    "Browser profiles": "Sign-in",
  };

  const CATEGORY_COLORS: Record<string, string> = {
    "Built-in Tools": "bg-accent/10 text-accent",
    "Integrations": "bg-feedback-success-primary/10 text-feedback-success-primary",
    "MCP Servers": "bg-filigran-ia-primary/10 text-filigran-ia-primary",
    "Custom Tools": "bg-feedback-alert-primary/10 text-feedback-warning-tertiary",
    "Knowledge Bases": "bg-feedback-info-primary/10 text-feedback-info-primary",
    "Browser profiles": "bg-accent/10 text-accent",
  };

  const items: ToolItem[] = [];

  // 1. Built-in Tools. Before the registry answers the list is
  // unknown: the group shows a skeleton, or an inline error line, instead of
  // vanishing and pushing every other group up when it lands.
  const showsBuiltins = platformBuiltins.length > 0 || effectiveBuiltins.length > 0;
  const builtinsPending = showsBuiltins && !builtinToolsReady;
  if (showsBuiltins && builtinToolsReady) {
    for (const t of offeredBuiltinTools("general_assistant")) {
      const Icon = t.icon;
      items.push({
        id: `builtin-${t.id}`, label: t.label, description: t.description,
        category: "Built-in Tools", categoryColor: CATEGORY_COLORS["Built-in Tools"],
        icon: <Icon className="h-3.5 w-3.5 text-accent" />, checked: carriesBuiltin(effectiveBuiltins, t.id),
        onToggle: () => toggleBuiltin(t.id),
      });
    }
  }

  // 2. Integrations
  for (const integ of connectedIntegrations) {
    items.push({
      id: `integ-${integ.id}`, label: integ.name,
      description: INTEGRATION_PROVIDER_NAMES[integ.provider] || integ.provider,
      category: "Integrations", categoryColor: CATEGORY_COLORS["Integrations"],
      icon: <ProviderLogo provider={integ.provider} className="h-3.5 w-3.5" />,
      checked: (effectiveIntegrations[integ.provider] || []).includes(integ.id),
      onToggle: () => toggleIntegration(integ),
    });
  }

  // 3. MCP Servers
  for (const srv of enabledMCPs) {
    const logo = inferMCPLogo(srv.name);
    items.push({
      id: `mcp-${srv.id}`, label: srv.name, description: srv.transport,
      category: "MCP Servers", categoryColor: CATEGORY_COLORS["MCP Servers"],
      icon: logo ? <img src={logo} alt="" className="h-3.5 w-3.5 rounded-sm" /> : <Server className="h-3.5 w-3.5 text-accent" />,
      checked: effectiveMCPs.includes(srv.id),
      onToggle: () => toggleListItem("ga_mcp_server_ids", effectiveMCPs, srv.id),
    });
  }

  // 4. Custom Tools
  if (customToolsEnabled) {
    for (const tool of enabledCustomTools) {
      items.push({
        id: `custom-${tool.id}`, label: tool.name, description: tool.description ?? undefined,
        category: "Custom Tools", categoryColor: CATEGORY_COLORS["Custom Tools"],
        icon: <Wrench className="h-3.5 w-3.5 text-accent" />,
        checked: effectiveCustomTools.includes(tool.id),
        onToggle: () => toggleListItem("ga_custom_tool_ids", effectiveCustomTools, tool.id),
      });
    }
  }

  // 5. Knowledge Bases
  for (const kb of allKBs) {
    items.push({
      id: `kb-${kb.id}`, label: kb.name,
      description: `${formatCompactNumber(kb.document_count)} docs`,
      category: "Knowledge Bases", categoryColor: CATEGORY_COLORS["Knowledge Bases"],
      icon: <BookOpen className="h-3.5 w-3.5 text-accent" />,
      checked: effectiveKBs.includes(kb.id),
      onToggle: () => toggleListItem("ga_knowledge_base_ids", effectiveKBs, kb.id),
    });
  }

  // 6. Browser profiles the assistant's browser tool may sign in with
  if (effectiveBuiltins.includes("browser") || carriesBuiltin(effectiveBuiltins, "web")) {
    for (const profile of browserProfiles ?? []) {
      items.push({
        id: `profile-${profile.id}`, label: profile.name, description: profile.start_url,
        category: "Browser profiles", categoryColor: CATEGORY_COLORS["Browser profiles"],
        icon: <KeyRound className="h-3.5 w-3.5 text-accent" />,
        checked: effectiveProfiles.includes(profile.id),
        onToggle: () => toggleListItem("ga_browser_profile_ids", effectiveProfiles, profile.id),
      });
    }
  }

  // Filter by search query
  const q = search.toLowerCase();
  const filtered = q ? items.filter((item) => item.label.toLowerCase().includes(q)) : items;

  // Group by category (preserving order)
  const CATEGORY_ORDER = ["Built-in Tools", "Integrations", "MCP Servers", "Custom Tools", "Knowledge Bases", "Browser profiles"];
  const grouped = CATEGORY_ORDER
    .map((cat) => ({ category: cat, items: filtered.filter((i) => i.category === cat) }))
    .filter((g) => g.items.length > 0);

  const overrideMap: Record<string, boolean> = {
    "Built-in Tools": user.ga_builtin_tool_ids !== null,
    "Integrations": user.ga_integration_bindings !== null,
    "MCP Servers": user.ga_mcp_server_ids !== null,
    "Custom Tools": user.ga_custom_tool_ids !== null,
    "Knowledge Bases": user.ga_knowledge_base_ids !== null,
    "Browser profiles": effectiveProfiles.length > 0,
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="content-base flex items-center gap-2">
            <Wrench className="h-4 w-4 text-accent" />
            {t_i18n("General Assistant Tools")}
            {hasOverride && (
              <Badge variant="outline" className="text-[9px] h-4 px-1.5 text-accent border-accent/30">{t_i18n("customised")}</Badge>
            )}
          </CardTitle>
          <Button variant="secondary" className="gap-1.5" data-testid="profile.reset-to-defaults" onClick={resetAll} disabled={!hasOverride || updateProfile.isPending}>
            <RotateCcw className="h-3 w-3" /> {t_i18n("Reset to defaults")}
          </Button>
        </div>
        <CardDescription className="content-compact">
          {t_i18n("Customise which tools and integrations are available in your General Assistant.")}
          {hasOverride ? " " + t_i18n("You have custom overrides active.") : " " + t_i18n("Currently using platform defaults.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <SearchInput placeholder={t_i18n("Filter tools...")} value={search} onChange={setSearch} className="max-w-full" />

        {builtinsPending && (
          <div className="space-y-0.5">
            <div className="flex items-center gap-2 px-1 pb-1">
              <span className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Built-in Tools")}</span>
            </div>
            <BuiltinToolsPending isError={builtinToolsFailed} variant="rows" count={3} testId="profile-builtin-tools" />
          </div>
        )}
        {grouped.length === 0 ? (
          builtinsPending ? null : (
            <p className="content-compact text-default-secondary text-center py-4">{t_i18n("No matching tools.")}</p>
          )
        ) : (
          <div className="space-y-3">
            {grouped.map((group) => (
              <div key={group.category} className="space-y-0.5">
                <div className="flex items-center gap-2 px-1 pb-1">
                  <span className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n(group.category)}</span>
                  {overrideMap[group.category] && (
                    <Badge variant="outline" className="text-[8px] h-3.5 px-1 text-accent border-accent/30">{t_i18n("customised")}</Badge>
                  )}
                </div>
                {group.items.map((item) => (
                  <div
                    key={item.id}
                    className={cn(
                      "flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 transition-colors cursor-pointer",
                      item.checked ? "bg-accent/5 ring-1 ring-accent/20" : "hover:bg-elevation-hover",
                    )}
                    onClick={() => !updateProfile.isPending && item.onToggle()}
                  >
                    <div className="shrink-0">{item.icon}</div>
                    <span className="content-compact font-medium truncate">{item.label}</span>
                    <span className={cn("text-[9px] px-1.5 py-0.5 rounded-full font-medium shrink-0", item.categoryColor)}>
                      {t_i18n(CATEGORY_BADGES[item.category])}
                    </span>
                    {item.description && (
                      <span className="content-caption text-default-secondary truncate hidden sm:inline">{item.description}</span>
                    )}
                    <div className="ml-auto shrink-0">
                      <Switch aria-label={t_i18n(CATEGORY_BADGES[item.category])} checked={item.checked} onCheckedChange={item.onToggle} disabled={updateProfile.isPending} />
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// InfoRow — single row inside the account card
// ---------------------------------------------------------------------------
function InfoRow({ icon: Icon, label, editing, fieldId, tied, children }: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  editing?: boolean;
  /** Id of the field the row edits: in edit mode the label becomes a tied 14 px Label (`${fieldId}-label`). */
  fieldId?: string;
  /** The row's control is live in read mode too (a switch): its label stays tied outside edit mode. */
  tied?: boolean;
  children: React.ReactNode;
}) {
  // Label is pre-translated by callers.
  return (
    <div className={cn("flex items-center gap-3 px-5", editing ? "py-3" : "py-3.5")}>
      <Icon className="h-4 w-4 text-accent shrink-0" />
      {fieldId && (editing || tied) ? (
        <Label id={`${fieldId}-label`} htmlFor={fieldId} className="shrink-0 w-36">{label}</Label>
      ) : (
        <span className="content-base font-medium shrink-0 w-36" data-testid="profile.info-row-label">{label}</span>
      )}
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// API Keys Section – personal API keys for LLM Proxy & Platform API
// ---------------------------------------------------------------------------
function ApiKeysSection() {
  const { t_i18n } = useFormatter();
  const { data: authConfig } = useAuthConfig();
  const { data: availableModels } = useAvailableModels();
  const { data: agents } = useAgents();
  const isXtmOneMode = useIsXtmOne();
  const platformName = usePlatformName();

  const baseUrl = authConfig?.base_url || platformOrigin();
  const enabledAgentsWithSlug = agents?.filter((a) => a.enabled && a.slug && !a.disable_chat) || [];

  return (
    <div className="space-y-6">
      {/* ── Section 1: API Keys ─────────────────────────────────────── */}
      <PersonalApiKeys platformName={platformName} />

      {/* ── Section 2: Platform API ─────────────────────────────────── */}
      <Card className="bg-elevation-hover border-elevation-subtle-soft backdrop-blur-sm">
        <CardContent className="p-5 space-y-3">
          <div className="flex items-center gap-2">
            <Shield className="h-4 w-4 text-accent" />
            <span className="content-base font-semibold">{t_i18n("Platform API")}</span>
          </div>
          <p className="content-compact text-default-secondary leading-relaxed">
            {t_i18n("Access the full {platform} REST API programmatically. Manage agents, trigger assignments, search knowledge bases, manage integrations, and more.", { platform: platformName || t_i18n("platform") })}
          </p>
          <div className="flex items-center gap-2">
            <span className="content-compact text-default-secondary">{t_i18n("Endpoint:")}</span>
            <code className="content-compact text-accent font-mono-plex bg-elevation-hover rounded-sm px-2 py-0.5 select-all">{baseUrl}/api/v1</code>
          </div>
          <pre className="bg-elevation-hover rounded-sm px-3 py-2 font-mono-plex text-[11px] text-accent overflow-x-auto whitespace-pre-wrap break-all leading-relaxed">{`curl ${baseUrl}/api/v1/agents \\
  -H "Authorization: Bearer YOUR_KEY"`}</pre>
          <a
            href={`${baseUrl}/api/reference`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 content-compact text-accent hover:text-accent/80 transition-colors"
          >
            <BookOpen className="h-3.5 w-3.5" />
            {t_i18n("View full API Reference")}
            <ExternalLink className="h-3 w-3" />
          </a>
        </CardContent>
      </Card>

      {/* ── Section 3: LLM Proxy (hidden in XTM One mode) ──────────── */}
      {!isXtmOneMode && (
        <Card className="bg-elevation-hover border-elevation-subtle-soft backdrop-blur-sm">
          <CardContent className="p-5 space-y-3">
            <div className="flex items-center gap-2">
              <Globe className="h-4 w-4 text-accent" />
              <span className="content-base font-semibold">{t_i18n("LLM Proxy")}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Use your API key for the LLM Proxy when you need generative AI capabilities within third-party tools.")}
            </p>
            <div className="flex items-center gap-2">
              <span className="content-compact text-default-secondary">{t_i18n("Endpoint:")}</span>
              <code className="content-compact text-accent font-mono-plex bg-elevation-hover rounded-sm px-2 py-0.5 select-all">{baseUrl}/v1</code>
            </div>

            <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 px-3 py-2">
              <p className="content-caption text-feedback-warning-primary leading-relaxed">

                <strong>{t_i18n("Vibe coding is not permitted through the LLM Proxy.")}</strong> {t_i18n("Do not use tools like Cursor, Aider, Continue, or Claude Code with this endpoint. Reach out to your manager for access to such capabilities.")}
              </p>
            </div>

            <pre className="bg-elevation-hover rounded-sm px-3 py-2 font-mono-plex text-[11px] text-accent overflow-x-auto whitespace-pre-wrap break-all leading-relaxed">{`curl ${baseUrl}/v1/chat/completions \\
  -H "Authorization: Bearer YOUR_KEY" \\
  -d '{"model":"auto","messages":[...]}'`}</pre>

            {availableModels && availableModels.length > 0 && (
              <div className="space-y-1.5 pt-1">
                <p className="content-compact font-semibold flex items-center gap-1.5">
                  <Code className="h-3.5 w-3.5 text-accent" />
                  {t_i18n("Available models")}
                </p>
                <div className="flex flex-wrap gap-1">
                  {availableModels.map((m) => (
                    <code key={m.model} className="rounded-sm bg-elevation-hover px-2 py-0.5 content-caption text-accent font-mono-plex">
                      {m.model}
                    </code>
                  ))}
                </div>
              </div>
            )}

            {enabledAgentsWithSlug.length > 0 && (
              <div className="space-y-1.5 pt-1">
                <p className="content-compact font-semibold flex items-center gap-1.5">
                  <Bot className="h-3.5 w-3.5 text-accent" />
                  {t_i18n("Available agents")}
                </p>
                <div className="flex flex-wrap gap-1">
                  {enabledAgentsWithSlug.map((a) => {
                    const AgentIcon = (a.icon && ICON_MAP[a.icon]) || Bot;
                    return (
                      <div key={a.id} className="inline-flex items-center gap-1.5 rounded-sm bg-elevation-hover px-2 py-0.5">
                        <AgentIcon className="h-3 w-3 text-default-secondary shrink-0" />
                        <span className="content-caption text-default-secondary">{a.name}</span>
                        <code className="content-caption text-accent font-mono-plex">agent:{a.slug}</code>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// MCP Endpoint Profile Section
// ---------------------------------------------------------------------------

const SOURCE_LABELS: Record<string, string> = { user: "Your override", group: "Group quota", global: "Platform default", none: "No quota" };

function QuotaProfileSection() {
  const { t_i18n } = useFormatter();
  const { data: quotaInfo, isLoading } = useMyQuotas();
  const isXtmOne = useIsXtmOne();
  // CE-only — agentic execution is disabled platform-wide, so any
  // quota number (current usage, limit, "Unlimited" fallback) is
  // irrelevant. Surface "Not Applicable" instead so the user isn't
  // led to believe they have unmetered access.
  const isCeOnly = useIsCommunityEdition();

  if (isLoading) return <div className="space-y-4"><Card><CardContent className="py-8 text-center content-compact text-default-secondary">{t_i18n("Loading quota info...")}</CardContent></Card></div>;

  const resolved = quotaInfo?.resolved_config;
  const usage = quotaInfo?.usage ?? [];
  const sources = quotaInfo?.sources ?? {};

  const types: { key: string; label: string; icon: React.ElementType; color: string; barColor: string }[] = [];
  types.push({ key: "agentic", label: "Agentic Executions", icon: Zap, color: "text-accent", barColor: "bg-accent" });
  if (!isXtmOne) types.push({ key: "llm_proxy", label: "LLM Proxy Requests", icon: Key, color: "text-feedback-success-primary", barColor: "bg-feedback-success-primary" });

  return (
    <div className="space-y-4">
      {types.map(({ key, label, icon: Icon, color, barColor }) => {
        const cfg = key === "agentic" ? resolved?.agentic : resolved?.llm_proxy;
        const source = sources[key] ?? "none";
        const limit = cfg?.default ?? null;
        const period = cfg?.period ?? "monthly";
        const totalEntry = usage.find((u) => u.usage_type === key && u.model === null);
        // Sandbox has no row of its own — its count is folded into the
        // displayed agentic total instead (spec 002-sandbox-quota-usage-tracking
        // FR-013/FR-014, revised — sandbox has no enforceable limit of its
        // own, so it never gets its own card, only adds to agentic's number).
        const current = key === "agentic"
          ? mergeSandboxIntoAgenticCount(usage, totalEntry)
          : totalEntry?.count ?? 0;
        // When the agentic quota is a platform-wide pool (the included allowance /
        // any Filigran license shipping global quotas) the backend reports the
        // shared total, not this user's slice — say so, otherwise the number
        // reads as "your usage" and looks wrong when it moves on its own.
        const isSharedPool = totalEntry?.scope === "global";
        // An explicit 0 limit is a hard stop (see formatQuotaLimit), so the
        // gauge pins at 100% instead of dividing by zero.
        const hasLimit = limit != null;
        const pct = !hasLimit ? 0 : limit > 0 ? Math.min((current / limit) * 100, 100) : 100;
        const isDanger = pct >= 100;
        const isWarning = pct >= 80 && pct < 100;

        return (
          <Card key={key} className="transition-all duration-300 hover:bg-elevation-hover">
            <CardContent className="pt-5 pb-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Icon className={cn("h-4 w-4", color)} />
                  <span className="content-base font-semibold">{t_i18n(label)}</span>
                </div>
                <Badge variant="outline" className="content-caption font-normal">
                  {t_i18n(SOURCE_LABELS[source])}
                </Badge>
              </div>

              {isCeOnly ? (
                <p className="content-compact text-default-secondary">
                  {QUOTA_NOT_APPLICABLE_LABEL} &mdash; {t_i18n("agentic execution is disabled until a license is activated or an EE platform is registered.")}
                </p>
              ) : source === "none" ? (
                <p className="content-compact text-default-secondary">{t_i18n("No quota configured: unlimited usage.")}</p>
              ) : (
                <>
                  <div className="flex items-baseline justify-between">
                    <span className={cn("text-2xl font-bold tabular-nums", isDanger ? "text-feedback-error-primary" : isWarning ? "text-feedback-warning-primary" : color)}>
                      {formatCompactNumber(current)}
                    </span>
                    <span className="content-compact text-default-secondary">
                      {formatQuotaLimit(limit, t_i18n(QUOTA_PERIOD_LABELS[period] ?? period))}
                    </span>
                  </div>
                  {isSharedPool && (
                    <p className="text-[11px] text-default-secondary">
                      {t_i18n("Shared across all users: this pool counts every user and entry point (chat, assignments, connectors).")}
                    </p>
                  )}

                  {hasLimit && (
                    <div className="h-2 w-full rounded-full bg-elevation-hover">
                      <div
                        className={cn("h-2 rounded-full transition-all duration-700", isDanger ? "bg-feedback-error-primary" : isWarning ? "bg-feedback-alert-primary" : barColor)}
                        style={{ width: `${Math.min(pct, 100)}%` }}
                      />
                    </div>
                  )}

                  {key === "llm_proxy" && cfg?.per_model && Object.keys(cfg.per_model).length > 0 && (
                    <div className="space-y-2 pt-1">
                      <p className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Per-model limits")}</p>
                      {Object.entries(cfg.per_model).map(([model, modelLimit]) => {
                        const modelEntry = usage.find((u) => u.usage_type === "llm_proxy" && u.model === model);
                        const modelCurrent = modelEntry?.count ?? 0;
                        const modelPct = modelLimit > 0 ? Math.min((modelCurrent / modelLimit) * 100, 100) : 0;
                        return (
                          <div key={model} className="space-y-1">
                            <div className="flex items-center justify-between">
                              <span className="text-[11px] font-medium truncate max-w-[60%]">{model}</span>
                              <span className="text-[11px] text-default-secondary tabular-nums">{formatCompactNumber(modelCurrent)} / {formatCompactNumber(modelLimit)}</span>
                            </div>
                            <div className="h-1 w-full rounded-full bg-elevation-hover">
                              <div className={cn("h-1 rounded-full transition-all duration-500", barColor)} style={{ width: `${modelPct}%` }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------

function MCPEndpointProfileSection() {
  const { t_i18n } = useFormatter();
  const { data: user } = useMe();
  const { data: authConfig } = useAuthConfig();
  const { data: agents } = useAgents();
  const { data: apiKeys } = useApiKeys();
  const updateProfile = useUpdateProfile();
  const { toast } = useToast();
  const platformName = usePlatformName();

  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedXtmUrl, setCopiedXtmUrl] = useState(false);
  const [copiedXtmSnippet, setCopiedXtmSnippet] = useState(false);
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null);
  const [agentSearch, setAgentSearch] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  useClickOutside(pickerRef, () => { setPickerOpen(false); setAgentSearch(""); }, pickerOpen);

  const baseUrl = authConfig?.base_url ?? "";
  const userUrl = `${baseUrl}/mcp/user`;

  // Agents visible to user that are eligible as MCP tools
  const eligibleAgents = useMemo(
    () => (agents ?? []).filter((a) => a.enabled && !a.disable_chat && a.slug),
    [agents],
  );

  // User's current selection (null = all agents exposed)
  const selectedIds = useMemo(() => user?.mcp_exposed_agent_ids ?? [], [user?.mcp_exposed_agent_ids]);
  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const selectedAgents = useMemo(
    () => eligibleAgents.filter((a) => selectedIdSet.has(a.id)),
    [eligibleAgents, selectedIdSet],
  );

  const filteredAvailable = useMemo(() => {
    const available = eligibleAgents.filter((a) => !selectedIdSet.has(a.id));
    if (!agentSearch) return available;
    const q = agentSearch.toLowerCase();
    return available.filter(
      (a) => a.name.toLowerCase().includes(q) || (a.slug ?? "").toLowerCase().includes(q),
    );
  }, [eligibleAgents, selectedIdSet, agentSearch]);

  const now = useNow();
  const mcpKeys = useMemo(
    () => (apiKeys ?? []).filter((key) => apiKeyUsableOn(key, "mcp", now)),
    [apiKeys, now],
  );
  const hasApiKeys = mcpKeys.length > 0;

  // Platform MCP servers (OpenCTI / OpenAEV / OpenCRQ) — shared descriptor
  // with mode-aware visibility (all servers in XTM One mode, connected-only
  // in Copilot mode). The card hides when the admin toggle is off or when
  // nothing is worth showing; in XTM One mode it stays visible while the
  // descriptor query loads (skeleton rows) instead of flashing in and out.
  const {
    enabled: platformMcpEnabled,
    servers: platformServers,
    connectedCount: connectedServerCount,
    isLoading: platformServersLoading,
    isXtmOne: isXtmOnePlatform,
    xtmServer,
  } = usePlatformMCPServerList();
  const showPlatformMcpCard =
    platformMcpEnabled && (isXtmOnePlatform || platformServers.length > 0);

  // XTM One's own MCP server. Off by default and admin-gated, so the card only
  // appears once an administrator has actually turned it on — advertising an
  // endpoint that answers 403 would send users to a dead URL.
  const showXtmMcpCard = Boolean(xtmServer?.enabled);

  const handleCopyXtmUrl = () => {
    if (!xtmServer) return;
    navigator.clipboard.writeText(xtmServer.url);
    setCopiedXtmUrl(true);
    setTimeout(() => setCopiedXtmUrl(false), COPY_FEEDBACK_DELAY);
  };

  const handleCopyXtmSnippet = () => {
    if (!xtmServer) return;
    navigator.clipboard.writeText(
      JSON.stringify(
        {
          mcpServers: {
            "xtm-one-platform": {
              url: xtmServer.url,
              headers: { Authorization: "Bearer <your-api-key>" },
            },
          },
        },
        null,
        2,
      ),
    );
    setCopiedXtmSnippet(true);
    setTimeout(() => setCopiedXtmSnippet(false), COPY_FEEDBACK_DELAY);
  };

  const handleCopyUrl = () => {
    navigator.clipboard.writeText(userUrl);
    setCopiedUrl(true);
    setTimeout(() => setCopiedUrl(false), COPY_FEEDBACK_DELAY);
  };

  const handleAddAgent = (agentId: string) => {
    const newIds = [...selectedIds, agentId];
    updateProfile.mutate(
      { mcp_exposed_agent_ids: newIds },
      { onSuccess: () => toast({ title: t_i18n("MCP agents updated"), variant: "success" }) },
    );
    setAgentSearch("");
    searchInputRef.current?.focus();
  };

  const handleRemoveAgent = (agentId: string) => {
    const newIds = selectedIds.filter((id) => id !== agentId);
    updateProfile.mutate(
      { mcp_exposed_agent_ids: newIds.length > 0 ? newIds : null },
      { onSuccess: () => toast({ title: t_i18n("MCP agents updated"), variant: "success" }) },
    );
  };

  // Connection snippet (same JSON for both Cursor and Claude Desktop)
  const connectionSnippet = JSON.stringify(
    {
      mcpServers: {
        "xtm-one-agents": {
          url: userUrl,
          headers: {
            Authorization: "Bearer <your-api-key>",
          },
        },
      },
    },
    null,
    2,
  );

  return (
    <div className="space-y-6">
      {/* Your MCP Endpoint */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Server className="h-4 w-4 text-accent" />
            {t_i18n("Your MCP Endpoint")}
          </CardTitle>
          <CardDescription>
            {t_i18n("Connect external AI tools (Cursor, Claude Desktop, etc.) to your {platform} agents via MCP.", { platform: platformName || t_i18n("platform") })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <label className="content-compact font-medium text-default-secondary">{t_i18n("Endpoint URL")}</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-sm bg-elevation-default px-3 py-2 content-compact font-mono-plex select-all">
                {userUrl}
              </code>
              <Button variant="ghost" className="shrink-0 gap-1.5" data-testid="profile.mcp-copy-url" aria-label={t_i18n("Copy the endpoint URL")} onClick={handleCopyUrl}>
                {copiedUrl ? (
                  <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            <label className="content-compact font-medium text-default-secondary">{t_i18n("Authentication")}</label>
            <p className="content-compact text-default-secondary">
              {t_i18n("Use any of your API keys as a Bearer token in the")} <code>Authorization</code> {t_i18n("header.")}
            </p>
            {hasApiKeys ? (
              <div className="flex items-center gap-2">
                <Badge variant="success" className="content-caption">
                  <Check className="h-3 w-3 mr-1" />
                  {t_i18n("{count} API {word} available", { count: mcpKeys.length, word: t_i18n(pluralize(mcpKeys.length, "key")) })}
                </Badge>
              </div>
            ) : (
              <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                <p className="content-compact text-feedback-warning-primary">
                  {t_i18n("No API keys yet. Go to the")} <strong>{t_i18n("API Keys")}</strong> {t_i18n("tab to create one.")}
                </p>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Platform Tools MCP Server (this instance's own surface) */}
      {showXtmMcpCard && xtmServer && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Server className="h-4 w-4 text-accent" />
              {t_i18n("Platform Tools MCP Server")}
            </CardTitle>
            <CardDescription>
              {t_i18n("Read agent traces and manage your {platform} objects (agents, assignments, runs, knowledge bases, prompts, skills) from an external MCP client. Scoped to what you can already see and manage here.", { platform: platformName || "XTM One" })}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <label className="content-compact font-medium text-default-secondary">{t_i18n("Endpoint URL")}</label>
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded-sm bg-elevation-default px-3 py-2 content-compact font-mono-plex select-all truncate">
                  {xtmServer.url}
                </code>
                <Tip content={t_i18n("Copy the endpoint URL")}>
                  <Button
                    variant="ghost"
                    className="shrink-0 gap-1.5"
                    data-testid="profile.xtm-mcp-copy-url"
                    aria-label={t_i18n("Copy the platform tools MCP endpoint URL")}
                    onClick={handleCopyXtmUrl}
                  >
                    {copiedXtmUrl ? (
                      <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </Button>
                </Tip>
                <Tip content={t_i18n("Copy a ready-to-paste MCP client configuration (Cursor, Claude Desktop, ...)")}>
                  <Button
                    variant="outline"
                    className="shrink-0 gap-1.5"
                    data-testid="profile.xtm-mcp-copy-config"
                    onClick={handleCopyXtmSnippet}
                  >
                    {copiedXtmSnippet ? (
                      <Check className="h-3 w-3 text-feedback-success-primary" />
                    ) : (
                      <Terminal className="h-3 w-3" />
                    )}
                    {t_i18n("Config")}
                  </Button>
                </Tip>
              </div>
            </div>
            <div className="space-y-2 border-t border-elevation-subtle-soft pt-4">
              <label className="content-compact font-medium text-default-secondary">
                {t_i18n("Install in your client")}
              </label>
              <XtmMcpPackageDownload apiKeyCount={mcpKeys.length} />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Platform MCP Servers (OpenCTI / OpenAEV / OpenCRQ) */}
      {showPlatformMcpCard && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Globe className="h-4 w-4 text-accent" />
              {t_i18n("Platform MCP Servers")}
              {connectedServerCount > 0 ? (
                <Badge variant="secondary" className="content-caption ml-auto">
                  {t_i18n("{count} connected", { count: connectedServerCount })}
                </Badge>
              ) : (
                <Badge variant="outline" className="content-caption ml-auto">
                  {t_i18n("No platform connected")}
                </Badge>
              )}
            </CardTitle>
            <CardDescription>
              {t_i18n("Native MCP servers for the platforms connected to {platform}. Business and knowledge tools only: authenticated with your API key, every call runs with your own identity on the platform.", { platform: platformName || "XTM One" })}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <PlatformMCPServerList
              servers={platformServers}
              showConfig
              isLoading={platformServersLoading}
            />
            <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2">
              <Info className="h-3.5 w-3.5 text-accent mt-0.5 shrink-0" />
              <p className="content-compact text-default-secondary">
                {t_i18n("Use the")} <strong>{t_i18n("Config")}</strong> {t_i18n("button to copy a ready-to-paste MCP client configuration (Cursor, Claude Desktop, ...). Replace")}{" "}
                <code className="content-caption">&lt;your-api-key&gt;</code> {t_i18n("with one of your API keys.")}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Exposed Agents */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Bot className="h-4 w-4 text-accent" />
            {t_i18n("Exposed Agents")}
            {selectedAgents.length > 0 ? (
              <Badge variant="secondary" className="content-caption ml-auto">
                {t_i18n("{count} selected", { count: selectedAgents.length })}
              </Badge>
            ) : (
              <Badge variant="outline" className="content-caption ml-auto">
                {t_i18n("All agents ({count})", { count: eligibleAgents.length })}
              </Badge>
            )}
          </CardTitle>
          <CardDescription>
            {t_i18n("Choose which agents to expose as MCP tools. Leave empty to expose all your visible agents.")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* Autocomplete agent picker */}
          <div className="relative" ref={pickerRef}>
            <SearchInput
              inputRef={searchInputRef}
              placeholder={t_i18n("Search agents to add...")}
              value={agentSearch}
              onChange={(value) => { setAgentSearch(value); setPickerOpen(true); }}
              onFocus={() => setPickerOpen(true)}
            />

            {pickerOpen && (
              <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-52 overflow-y-auto">
                {filteredAvailable.length > 0 ? (
                  filteredAvailable.map((agent) => {
                    const IconComp = ICON_MAP[agent.icon] ?? Bot;
                    return (
                      <button
                        key={agent.id}
                        className="flex items-center gap-2.5 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                        onClick={() => handleAddAgent(agent.id)}
                      >
                        <IconComp className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                        <span className="truncate flex-1">{agent.name}</span>
                        {agent.slug && (
                          <code className="content-caption text-default-secondary font-mono-plex shrink-0">
                            {agent.slug}
                          </code>
                        )}
                      </button>
                    );
                  })
                ) : (
                  <p className="px-3 py-3 text-[11px] text-default-secondary text-center">
                    {agentSearch ? t_i18n("No matching agents.") : t_i18n("All eligible agents are already selected.")}
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Current selection */}
          {selectedAgents.length === 0 ? (
            <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2">
              <Info className="h-3.5 w-3.5 text-accent mt-0.5 shrink-0" />
              <p className="content-compact text-default-secondary">
                {t_i18n("No selection: {count, plural, =0 {no eligible agent is} one {the one eligible agent is} other {all # eligible agents are}} exposed on your MCP endpoint. Add agents above to expose only specific ones.", { count: eligibleAgents.length })}
              </p>
            </div>
          ) : (
            <div className="space-y-1">
              {selectedAgents.map((agent) => {
                const IconComp = ICON_MAP[agent.icon] ?? Bot;
                return (
                  <div
                    key={agent.id}
                    className={cn(
                      "flex items-center gap-3 rounded-sm bg-elevation-default px-4 py-2.5",
                      "group hover:bg-elevation-hover transition-colors",
                    )}
                  >
                    <div className="flex h-7 w-7 items-center justify-center rounded-sm bg-accent/10 shrink-0">
                      <IconComp className="h-3.5 w-3.5 text-accent" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="content-base font-medium truncate">{agent.name}</p>
                      <p className="text-[11px] text-default-secondary truncate">
                        {t_i18n("Tool name:")} <code className="content-caption">{agent.slug}</code>
                      </p>
                    </div>
                    {agent.is_company_managed && (
                      <Badge variant="secondary" className="text-[9px] shrink-0">{t_i18n("Company")}</Badge>
                    )}
                    <Button aria-label={t_i18n("Remove agent")}
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 opacity-0 group-hover:opacity-100 text-default-secondary hover:text-feedback-error-primary transition-all"
                      onClick={() => handleRemoveAgent(agent.id)}
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Connection Snippets */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Terminal className="h-4 w-4 text-accent" />
            {t_i18n("Connection Snippets")}
          </CardTitle>
          <CardDescription>
            {t_i18n("Copy and paste these into your MCP client configuration. Replace")} <code>&lt;your-api-key&gt;</code> {t_i18n("with one of your API keys.")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Cursor */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="content-compact font-medium text-default-secondary">
                Cursor (<code className="content-caption">.cursor/mcp.json</code>)
              </h4>
              <Button
                variant="ghost"
                className="gap-1.5"
                data-testid="profile.snippet-copy-cursor"
                onClick={() => {
                  navigator.clipboard.writeText(connectionSnippet);
                  setCopiedSnippet("cursor");
                  setTimeout(() => setCopiedSnippet(null), COPY_FEEDBACK_DELAY);
                }}
              >
                {copiedSnippet === "cursor" ? (
                  <Check className="h-3 w-3 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3 w-3" />
                )}
                {t_i18n("Copy")}
              </Button>
            </div>
            <pre className="rounded-sm bg-elevation-default p-3 text-[11px] font-mono-plex overflow-x-auto whitespace-pre-wrap break-all">
              {connectionSnippet}
            </pre>
          </div>

          {/* Claude Desktop */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="content-compact font-medium text-default-secondary">
                Claude Desktop (<code className="content-caption">claude_desktop_config.json</code>)
              </h4>
              <Button
                variant="ghost"
                className="gap-1.5"
                data-testid="profile.snippet-copy-claude"
                onClick={() => {
                  navigator.clipboard.writeText(connectionSnippet);
                  setCopiedSnippet("claude");
                  setTimeout(() => setCopiedSnippet(null), COPY_FEEDBACK_DELAY);
                }}
              >
                {copiedSnippet === "claude" ? (
                  <Check className="h-3 w-3 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3 w-3" />
                )}
                {t_i18n("Copy")}
              </Button>
            </div>
            <pre className="rounded-sm bg-elevation-default p-3 text-[11px] font-mono-plex overflow-x-auto whitespace-pre-wrap break-all">
              {connectionSnippet}
            </pre>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
