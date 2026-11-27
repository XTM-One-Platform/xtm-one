// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  Shield,
  User as UserIcon,
  Link2,
  Server,
  Bot,
  Database,
  ScrollText,
  FileText,
  Zap,
  Key,
  Eye,
  Power,
  PowerOff,
  ChevronUp,
  ChevronDown,
  Wrench,
  UsersRound,
  Search,
  X,
  UserPlus,
  Trash2,
  MessageSquare,
  BarChart3,
  LogIn,
  Gauge,
  Save,
  RotateCcw,
  KeyRound,
  Mail,
  MailCheck,
  AlertCircle,
  Pause,
} from "lucide-react";
import type { UserDetail as UserDetailType, QuotaTypeConfig } from "@/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { roleLabelKey } from "@/lib/role-labels";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import {
  ssoProviderLabel,
  INTEGRATION_PROVIDER_NAMES,
  INTEGRATION_STATUS,
  TRANSPORT_DISPLAY_NAMES,
  displayedIntegrationStatus,
  QUOTA_PERIOD_LABELS,
} from "@/lib/constants";
import { useAgentTemplates } from "@/hooks/api/useAgentApi";
import {
  useUser, useUserStats, useUserIntegrations, useUserMCPServers, useUserAgents, useUserKnowledgeBases,
  useUserCustomTools,
  useUpdateUserRole, useToggleUserActive, useImpersonateUser, useDeleteUser,
  useUserQuotas, useUpdateUserQuotas, useResetUserQuotas,
  useSetUserPassword, useSendPasswordReset, useResetUserMfa, useUpdateUserEmail,
  useAuthConfig,
} from "@/hooks/api/useAuthApi";
import { useAvailableModels, usePlatformQuotas } from "@/hooks/api/useSettingsApi";
import { QuotaEditor } from "@/components/ui/quota-editor";
import { normalizeQuotaOverride } from "@/components/ui/quota-override";
import { useAdminApiKeys, useApiKeyPolicy } from "@/hooks/api/useMiscApi";
import { AuditLogList } from "@/components/audit-logs/AuditLogList";
import { AdminApiKeyRow } from "@/components/api-keys/AdminApiKeyRow";
import { useApiKeyRevocation } from "@/components/api-keys/useApiKeyRevocation";
import { useUserGroups, useGroups, useAddGroupMembers, useRemoveGroupMember } from "@/hooks/api/useGroupApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne, useAgenticQuotas, useIsCommunityEdition } from "@/hooks/usePlatformConfig";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DeactivateUserDialog } from "@/components/users/DeactivateUserDialog";
import { startImpersonation } from "@/lib/api";
import { cn, formatCompactNumber, formatKBStats, formatQuotaLimit, mergeSandboxIntoAgenticCount, pluralize, QUOTA_NOT_APPLICABLE_LABEL, validatePassword, MIN_PASSWORD_LENGTH } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import { useToast } from "@/components/ui/toast";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Tab definition
// ---------------------------------------------------------------------------

type Tab = "profile" | "groups" | "objects" | "api-keys" | "quotas" | "activity";

const TABS: { key: Tab; label: string; icon: React.ElementType }[] = [
  { key: "profile", label: "Profile", icon: UserIcon },
  { key: "groups", label: "Groups", icon: UsersRound },
  { key: "objects", label: "Objects", icon: Database },
  { key: "api-keys", label: "API Keys", icon: Key },
  { key: "quotas", label: "Quotas", icon: Gauge },
  { key: "activity", label: "Activity", icon: ScrollText },
];


// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function UserDetail() {
  const { t_i18n } = useFormatter();
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [tab] = useTabParam<Tab>("profile");

  const { data: user, isLoading } = useUser(id);
  const updateRole = useUpdateUserRole();
  const toggleActive = useToggleUserActive();
  const impersonate = useImpersonateUser();
  const deleteUser = useDeleteUser();
  const { user: currentUser, isImpersonating } = useAuth();
  const [ConfirmDialog, confirm] = useConfirm();
  const [deactivateOpen, setDeactivateOpen] = useState(false);

  if (isLoading) return <DetailPageSkeleton backTo="/users-and-groups" backLabel="Back to Users" tabs={TABS.length} actions={3} />;

  if (!user) {
    return (
      <NotFoundState
        icon={UserIcon}
        title="User not found"
        description="This user may have been deleted or doesn't exist."
        actionLabel="View All Users"
        onAction={() => navigate("/users-and-groups")}
        backTo="/users-and-groups"
        backLabel="Back to Users"
      />
    );
  }

  const handleToggleRole = () => {
    if (!id) return;
    const newRole = user.role === "admin" ? "user" : "admin";
    updateRole.mutate({ userId: id, role: newRole });
  };

  const handleToggleActive = () => {
    if (!id) return;
    // Disabling runs the whole cascade: the dialog previews it first (#122).
    if (user.is_active) setDeactivateOpen(true);
    else toggleActive.mutate(id);
  };

  const handleImpersonate = async () => {
    if (!id) return;
    const ok = await confirm({
      title: "Impersonate User",
      description: t_i18n('You will be logged in as "{name}" ({email}). All actions will appear as this user. You can stop impersonating at any time via the banner at the top of the page.', { name: user.full_name, email: user.email }),
      confirmLabel: "Impersonate",
    });
    if (!ok) return;
    impersonate.mutate(id, {
      onSuccess: (data) => {
        startImpersonation(data.access_token);
        window.location.href = withBasePath("/");
      },
    });
  };

  const handleDelete = async () => {
    if (!id) return;
    const ok = await confirm({
      title: "Delete User",
      description: t_i18n('This will permanently delete "{name}" ({email}) and ALL associated data: agents, assignments, runs, conversations, integrations, knowledge bases, MCP servers, custom tools, API keys, and audit logs. This action cannot be undone.', { name: user.full_name, email: user.email }),
      confirmLabel: "Delete permanently",
      variant: "danger",
    });
    if (!ok) return;
    deleteUser.mutate(id, { onSuccess: () => navigate("/users-and-groups") });
  };

  return (
    <div className="space-y-6">
      {ConfirmDialog}
      {id && (
        <DeactivateUserDialog
          userId={id}
          userName={user.full_name || user.email}
          open={deactivateOpen}
          onOpenChange={setDeactivateOpen}
          onConfirm={() => toggleActive.mutate(id)}
        />
      )}

      <EntityPageHeader
        backTo="/users-and-groups"
        backLabel="Back to Users"
        icon={user.role === "admin" ? <Shield className="h-7 w-7 text-accent" /> : <UserIcon className="h-7 w-7 text-default-secondary" />}
        tileClassName={user.role === "admin" ? "bg-accent/10" : "bg-elevation-hover"}
        title={user.full_name}
        badges={
          <>
            <p className="content-base text-default-secondary">{user.email}</p>
            <Badge variant={user.role === "admin" ? "cyan" : "outline"} className="content-caption">{t_i18n(roleLabelKey(user.role))}</Badge>
            {user.is_platform_admin && <Badge variant="cyan" className="content-caption">{t_i18n("Platform Admin")}</Badge>}
            {!user.is_active && <Badge variant="destructive" className="content-caption">{t_i18n("Disabled")}</Badge>}
            {user.sso_provider && <Badge variant="outline" className="content-caption">{ssoProviderLabel(user.sso_provider)}</Badge>}
          </>
        }
        actions={
          <>
            {!user.is_platform_admin && (
              <>
                <Tip content={user.role === "admin" ? t_i18n("Demote to User") : t_i18n("Promote to Admin")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-default-secondary hover:text-default-primary"
                    onClick={handleToggleRole}
                    disabled={updateRole.isPending}
                  >
                    {user.role === "admin" ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
                    {user.role === "admin" ? t_i18n("Demote") : t_i18n("Promote")}
                  </Button>
                </Tip>
                <Tip content={user.is_active ? t_i18n("Disable account") : t_i18n("Enable account")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className={cn("gap-1.5", user.is_active ? "text-default-secondary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" : "text-default-secondary hover:text-feedback-success-primary hover:bg-feedback-success-primary/10")}
                    onClick={handleToggleActive}
                    disabled={toggleActive.isPending}
                  >
                    {user.is_active ? <PowerOff className="h-3.5 w-3.5" /> : <Power className="h-3.5 w-3.5" />}
                    {user.is_active ? t_i18n("Disable") : t_i18n("Enable")}
                  </Button>
                </Tip>
              </>
            )}
            <EntityHeaderWideOnly>
            {user.is_active && currentUser?.id !== user.id && !isImpersonating && (
              <Tip content={t_i18n("Impersonate this user")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5 text-feedback-warning-tertiary hover:text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
                  onClick={handleImpersonate}
                  disabled={impersonate.isPending}
                >
                  <Eye className="h-3.5 w-3.5" /> {t_i18n("Impersonate")}
                </Button>
              </Tip>
            )}
            {!user.is_platform_admin && currentUser?.id !== user.id && (
              <Tip content={t_i18n("Permanently delete this user and all data")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                  onClick={handleDelete}
                  disabled={deleteUser.isPending}
                >
                  <Trash2 className="h-3.5 w-3.5" /> {t_i18n("Delete")}
                </Button>
              </Tip>
            )}
            </EntityHeaderWideOnly>
            {/* Below 1280 px: Impersonate and Delete fold into the
                "More actions" menu; Promote and Disable stay in the row. */}
            <EntityHeaderMoreActions
              items={[
                ...(user.is_active && currentUser?.id !== user.id && !isImpersonating
                  ? [{ id: "impersonate", label: t_i18n("Impersonate"), icon: Eye, onSelect: () => void handleImpersonate(), disabled: impersonate.isPending }]
                  : []),
                ...(!user.is_platform_admin && currentUser?.id !== user.id
                  ? [{ id: "delete", label: t_i18n("Delete user"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void handleDelete(), disabled: deleteUser.isPending }]
                  : []),
              ]}
            />
          </>
        }
      />

      {/* What disabling the account holds back: nothing is rewritten, so
          enabling it again resumes each assignment as its owner left it. */}
      {!user.is_active && (
        <div className="flex items-start gap-2.5 rounded-sm bg-feedback-alert-primary/5 p-3">
          <Pause className="h-4 w-4 shrink-0 mt-0.5 text-feedback-warning-primary" />
          <p className="content-compact leading-relaxed text-feedback-warning-primary">
            {t_i18n("While this account is disabled, the assignments and flows it owns are paused, shared ones included. Company-managed ones it created keep running.")}
          </p>
        </div>
      )}

      {/* Tabs */}
      <DetailTabs tabs={TABS} active={tab} />

      {/* Tab content */}
      {tab === "profile" && <ProfileTab user={user} userId={user.id} />}
      {tab === "groups" && <GroupsTab userId={user.id} />}
      {tab === "objects" && id && <ObjectsTab userId={id} />}
      {tab === "api-keys" && id && <ApiKeysTab userId={id} />}
      {tab === "quotas" && id && <UserQuotasTab userId={id} />}
      {tab === "activity" && id && <ActivityTab userId={id} />}
    </div>
  );
}

// ===========================================================================
// Profile Tab
// ===========================================================================

function ProfileTab({ user, userId }: { user: UserDetailType; userId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data: stats } = useUserStats(userId);

  return (
    <div className="space-y-6">
      {/* ── User KPIs ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
        <StatCard icon={Bot} label="Agents" value={stats?.agent_count ?? 0} subtitle={t_i18n("{count} active assignments", { count: formatCompactNumber(stats?.active_assignment_count ?? 0) })} />
        <StatCard icon={MessageSquare} label="Conversations" value={stats?.conversation_count ?? 0} subtitle={t_i18n("{count, plural, one {{shown} message} other {{shown} messages}}", { count: stats?.message_count ?? 0, shown: formatCompactNumber(stats?.message_count ?? 0) })} />
        <StatCard icon={BarChart3} label="Agentic Executions" value={stats?.assignment_run_count ?? 0} subtitle={t_i18n("{count} notifications", { count: formatCompactNumber(stats?.notification_count ?? 0) })} />
        <StatCard icon={Link2} label="Integrations" value={stats?.integration_count ?? 0} subtitle={t_i18n("{count} connected", { count: formatCompactNumber(stats?.connected_integration_count ?? 0) })} />
        <StatCard icon={Key} label="API Keys" value={stats?.api_key_count ?? 0} subtitle={t_i18n("{count} total requests", { count: formatCompactNumber(stats?.api_key_total_requests ?? 0) })} />
        <StatCard icon={LogIn} label="Logins" value={stats?.login_count ?? 0} subtitle={stats?.last_login_at ? t_i18n("Last {time}", { time: formatTimeAgo(intl, stats.last_login_at) }) : "No login recorded"} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t_i18n("Profile Information")}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 content-base">
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Full Name")}</p>
              <p>{user.full_name || "—"}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Email")}</p>
              <p>{user.email}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Title")}</p>
              <p>{user.title || "—"}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Role")}</p>
              <p>{t_i18n(roleLabelKey(user.role))}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Status")}</p>
              <p>{user.is_active ? t_i18n("Active") : t_i18n("Disabled")}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("SSO Provider")}</p>
              <p>{user.sso_provider ? ssoProviderLabel(user.sso_provider) : t_i18n("Local account")}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Joined")}</p>
              <p>{user.created_at ? formatTimeAgo(intl, user.created_at) : "—"}</p>
            </div>
          </div>
          {user.is_platform_admin && (
            <div className="mt-4 pt-4 border-t border-elevation-subtle-soft">
              <p className="content-compact text-default-secondary">{t_i18n("This is the platform admin account (configured via environment). It cannot be demoted or disabled.")}</p>
            </div>
          )}
        </CardContent>
      </Card>

      <EmailCard user={user} userId={userId} />
      <PasswordCard user={user} userId={userId} />
      <TwoFactorCard user={user} userId={userId} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Two-factor Card — status + the admin lockout escape hatch
// ---------------------------------------------------------------------------

function TwoFactorCard({ user, userId }: { user: UserDetailType; userId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const resetMfa = useResetUserMfa();
  const [ConfirmDialog, confirm] = useConfirm();

  const enabled = !!user.mfa_enabled;

  const handleReset = async () => {
    const ok = await confirm({
      title: t_i18n("Reset two-factor authentication?"),
      // Spell out the consequence rather than asking "are you sure": the risky
      // half is that the account drops to password-only until the user
      // re-enrolls, and an admin clicking through deserves to see that.
      description: t_i18n("{name} will sign in with their password alone until they enroll a new device. Their current authenticator and recovery codes stop working immediately.", { name: user.full_name }),
      confirmLabel: t_i18n("Reset"),
      variant: "danger",
    });
    if (!ok) return;
    resetMfa.mutate(userId, {
      onSuccess: () => toast({ title: t_i18n("Two-factor authentication reset"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("Failed to reset two-factor authentication"), description: err.message, variant: "error" }),
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t_i18n("Two-factor authentication")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={enabled ? "success" : "outline"} className="content-caption">
            {enabled ? t_i18n("Enabled") : t_i18n("Not enabled")}
          </Badge>
          {enabled && user.mfa_confirmed_at && (
            <span className="content-compact text-default-secondary">
              {t_i18n("Enrolled {when}", { when: formatTimeAgo(intl, user.mfa_confirmed_at) })}
            </span>
          )}
        </div>
        {enabled ? (
          <>
            <p className="content-compact text-default-secondary">
              {t_i18n("The secret and recovery codes are never readable: not even by administrators. If this user lost their device, reset their enrollment so they can set it up again.")}
            </p>
            <Button variant="secondary" className="self-start" data-testid="user-detail.reset-mfa" onClick={handleReset} disabled={resetMfa.isPending} aria-busy={resetMfa.isPending || undefined}>
              {resetMfa.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Reset two-factor")}
            </Button>
          </>
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n("This user has not enrolled an authenticator app. Only they can enable it, from their own profile.")}
          </p>
        )}
        {ConfirmDialog}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Email Card — the one place a user's email changes
// ---------------------------------------------------------------------------

function EmailCard({ user, userId }: { user: UserDetailType; userId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateEmail = useUpdateUserEmail();
  const [email, setEmail] = useState("");
  const next = email.trim().toLowerCase();
  const unchanged = !next || next === user.email.toLowerCase();

  const handleSave = () => {
    if (unchanged) return;
    updateEmail.mutate(
      { userId, email: next },
      {
        onSuccess: (res) => {
          setEmail("");
          toast({ title: t_i18n("Email changed"), description: t_i18n("The account now uses {email}.", { email: res.email }), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("Failed to change the email"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Mail className="h-4 w-4 text-accent" /> {t_i18n("Email")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {user.is_platform_admin ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("The platform admin account's email is set by ADMIN_EMAIL and cannot be changed.")}
          </p>
        ) : (
          <>
            <p className="content-compact text-default-secondary">
              {t_i18n("The other platforms find this person's account by this address. Here it changes without a code; people change their own with a code sent to the new address. The old and the new address are notified, and reset links already sent stop working.")}
            </p>
            <div className="flex gap-2">
              <Input
                aria-label={t_i18n("New email")}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={user.email}
              />
              <Button className="shrink-0" disabled={unchanged || updateEmail.isPending} onClick={handleSave}>
                {updateEmail.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} /> : t_i18n("Change")}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Password Card — admin reset link + direct set-password
// ---------------------------------------------------------------------------

function PasswordCard({ user, userId }: { user: UserDetailType; userId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const sendReset = useSendPasswordReset();
  const setPassword = useSetUserPassword();
  // Live ``min_password_length`` from the backend keeps the placeholder /
  // inline error / disabled-Set-button copy in sync with the server-side
  // ``check_password_policy`` floor.  Falls back to ``MIN_PASSWORD_LENGTH``
  // (8) while the auth-config query is in flight on first paint.
  const { data: authConfig } = useAuthConfig();
  const minPasswordLength = authConfig?.min_password_length ?? MIN_PASSWORD_LENGTH;

  const [resetResult, setResetResult] = useState<{ emailSent: boolean } | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  // Only hide the password-management UI for SSO-ONLY accounts (provider
  // linked AND no local bcrypt hash). Hybrid accounts (SSO link + local
  // password) keep the card available — the backend already accepts admin
  // set/reset on those, so the UI must too. ``has_local_password`` falls
  // back to ``false`` when an older backend build hasn't surfaced the
  // field yet, which preserves the legacy "hide for any SSO-linked user"
  // behaviour for that single rolling-deploy interval (safe direction —
  // admins can still manage passwords via the API directly).
  const isSsoOnly = !!user.sso_provider && !user.has_local_password;
  const isHybrid = !!user.sso_provider && !!user.has_local_password;

  const pwError = newPassword ? validatePassword(newPassword, minPasswordLength) : null;

  const handleSendReset = () => {
    setResetResult(null);
    sendReset.mutate(userId, {
      onSuccess: (res) => {
        setResetResult({ emailSent: res.email_sent });
        if (res.email_sent) toast({ title: t_i18n("Reset email sent to {email}", { email: user.email }), variant: "success" });
      },
      onError: (err) => toast({ title: t_i18n("Failed to send reset email"), description: err.message, variant: "error" }),
    });
  };

  const handleSetPassword = () => {
    // Reuse the render-time ``pwError`` instead of re-validating: avoids
    // duplicate work and keeps the click-time guard in lockstep with the
    // inline error message + the disabled-Set-button gate. ``pwError`` is
    // ``null`` for empty input by design (so the inline error doesn't
    // render before the user types), hence the explicit ``!newPassword``
    // short-circuit on top.
    if (!newPassword || pwError) return;
    setPassword.mutate(
      { userId, new_password: newPassword },
      {
        onSuccess: () => { setNewPassword(""); toast({ title: t_i18n("Password set"), description: t_i18n("Updated password for {email}", { email: user.email }), variant: "success" }); },
        onError: (err) => toast({ title: t_i18n("Failed to set password"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent" /> {t_i18n("Password")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {isSsoOnly && (
          <p className="rounded-sm bg-feedback-alert-primary/10 px-3 py-2 content-compact text-feedback-warning-tertiary">
            {t_i18n("This account uses SSO ({provider}) for authentication and has no local password. Password management is handled by the identity provider.", { provider: ssoProviderLabel(user.sso_provider) ?? "" })}
          </p>
        )}
        {isHybrid && (
          <p className="rounded-sm bg-accent/5 px-3 py-2 content-compact text-accent">
            {t_i18n("This account is linked to SSO ({provider}) and also has a local password. Setting or resetting it here only affects the local-password fallback: SSO sign-in is unaffected.", { provider: ssoProviderLabel(user.sso_provider) ?? "" })}
          </p>
        )}

        {!isSsoOnly && (
        <>
        {/* Email a reset link */}
        <div className="space-y-2">
          <p className="content-base font-medium">{t_i18n("Email a reset link")}</p>
          <p className="content-compact text-default-secondary">
            {t_i18n("Sends a single-use link directly to {email}. They set their own password: you never see it.", { email: user.email })}
          </p>
          {resetResult ? (
            resetResult.emailSent ? (
              <div className="flex items-start gap-2.5 rounded-sm bg-accent/5 px-3 py-2">
                <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                <p className="content-compact text-accent">{t_i18n("Reset email sent to {email}.", { email: user.email })}</p>
              </div>
            ) : (
              <div className="flex items-start gap-2.5 rounded-sm bg-feedback-alert-primary/10 px-3 py-2">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
                <p className="content-compact text-feedback-warning-primary">
                  {t_i18n("Reset link was created in Redis, but the SMTP server refused the message. Check the server logs for the SMTP error and retry: the pending reset will expire on its own if you can't deliver it.")}
                </p>
              </div>
            )
          ) : (
            <Button variant="outline" className="gap-1.5" onClick={handleSendReset} disabled={sendReset.isPending} aria-busy={sendReset.isPending || undefined}>
              {sendReset.isPending && <Spinner size="sm" tone="inherit" />}{!sendReset.isPending && <Mail className="h-4 w-4" />}
              {t_i18n("Send reset email")}
            </Button>
          )}
        </div>

        {/* Direct set */}
        <div className="space-y-2 border-t border-elevation-subtle-soft pt-4">
          <div className="flex items-center justify-between">
            <p className="content-base font-medium">{t_i18n("Set a password directly")}</p>
            <Button variant="ghost" size="sm" className="h-6 px-2 content-compact" onClick={() => setShowPassword((s) => !s)}>
              {showPassword ? t_i18n("Hide") : t_i18n("Show")}
            </Button>
          </div>
          <p className="content-compact text-default-secondary">{t_i18n("You choose the password and share it with the user.")}</p>
          <div className="flex gap-2">
            <Input aria-label={t_i18n("New Password")}
              type={showPassword ? "text" : "password"}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t_i18n("At least {min} characters, with a letter and a number", { min: minPasswordLength })}
            />
            <Button className="shrink-0" disabled={!newPassword || !!pwError || setPassword.isPending} onClick={handleSetPassword}>
              {setPassword.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} /> : t_i18n("Set")}
            </Button>
          </div>
          {pwError && <p className="text-[11px] text-feedback-error-primary">{pwError}</p>}
        </div>
        </>
        )}
      </CardContent>
    </Card>
  );
}

// ===========================================================================
// Groups Tab
// ===========================================================================

function GroupsTab({ userId }: { userId: string }) {
  return (
    <div className="space-y-6">
      <UserGroupsCard userId={userId} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// User Groups Card — shows which groups a user belongs to, with add/remove
// ---------------------------------------------------------------------------

function UserGroupsCard({ userId }: { userId: string }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: userGroups } = useUserGroups(userId);
  const { data: allGroups } = useGroups();
  const addMembers = useAddGroupMembers();
  const removeMember = useRemoveGroupMember();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const [showAdd, setShowAdd] = useState(false);
  const [groupSearch, setGroupSearch] = useState("");
  const addPickerRef = useRef<HTMLDivElement>(null);
  useClickOutside(addPickerRef, () => { setShowAdd(false); setGroupSearch(""); });

  const memberGroupIds = useMemo(() => new Set((userGroups ?? []).map((g) => g.id)), [userGroups]);

  const availableGroups = useMemo(() => {
    if (!allGroups) return [];
    return allGroups
      .filter((g) => !memberGroupIds.has(g.id))
      .filter((g) => {
        if (!groupSearch) return true;
        const q = groupSearch.toLowerCase();
        return g.name.toLowerCase().includes(q) || (g.description || "").toLowerCase().includes(q);
      });
  }, [allGroups, memberGroupIds, groupSearch]);

  // A member uses what is shared with the group, and whatever carries it.
  const handleAdd = async (groupId: string) => {
    setShowAdd(false);
    if (!(await confirmSharing({ entity_type: "group", entity_id: groupId, change: { add_user_ids: [userId] } }, { confirmLabel: t_i18n("Add") }))) return;
    await addMembers.mutateAsync({ groupId, userIds: [userId] });
    setGroupSearch("");
  };

  const handleRemove = async (groupId: string) => {
    if (!(await confirmSharing({ entity_type: "group", entity_id: groupId, change: { remove_user_ids: [userId] } }, { mode: "unshare", confirmLabel: t_i18n("Remove") }))) return;
    await removeMember.mutateAsync({ groupId, userId });
  };

  return (
    <Card>
      {consentElement}
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base flex items-center gap-2">
          <UsersRound className="h-4 w-4 text-accent" /> {t_i18n("Groups")}
          {userGroups && userGroups.length > 0 && (
            <CountBadge variant="outline" className="ml-1">{userGroups.length}</CountBadge>
          )}
        </CardTitle>
        {!showAdd && (
          <Button variant="ghost" size="sm" className="h-7 gap-1.5 content-compact" onClick={() => setShowAdd(true)}>
            <UserPlus className="h-3 w-3" /> {t_i18n("Add to Group")}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Add to group picker */}
        {showAdd && (
          <div className="relative" ref={addPickerRef}>
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-default-secondary" />
            <Input aria-label={t_i18n("Search groups to add...")}
              placeholder={t_i18n("Search groups to add...")}
              value={groupSearch}
              onChange={(e) => setGroupSearch(e.target.value)}
              className="pl-9 pr-8 h-8 content-base"
              autoFocus
            />
            <button aria-label={t_i18n("Close")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-default-secondary hover:text-default-primary"
              onClick={() => { setShowAdd(false); setGroupSearch(""); }}
            >
              <X className="h-4 w-4" />
            </button>
            {groupSearch && availableGroups.length > 0 && (
              <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-48 overflow-y-auto">
                {availableGroups.slice(0, 10).map((g) => (
                  <button
                    key={g.id}
                    className="flex items-center gap-2 w-full px-3 py-2 text-left content-base hover:bg-elevation-hover"
                    onClick={() => handleAdd(g.id)}
                  >
                    <UsersRound className="h-3 w-3 shrink-0 text-accent" />
                    <span className="truncate flex-1">{g.name}</span>
                    <span className="content-caption text-default-secondary">{g.member_count} {t_i18n(pluralize(g.member_count, "member"))}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* User's groups */}
        {userGroups && userGroups.length > 0 ? (
          <div className="space-y-1">
            {userGroups.map((g) => (
              <div
                key={g.id}
                className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-4 py-2.5 cursor-pointer transition-colors hover:bg-elevation-hover"
                onClick={() => navigate(`/users-and-groups/groups/${g.id}`)}
              >
                <UsersRound className="h-3.5 w-3.5 text-accent shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="content-base font-medium truncate">{g.name}</p>
                  {g.description && <p className="content-caption text-default-secondary truncate">{g.description}</p>}
                </div>
                <Badge variant="outline" className="content-caption shrink-0">{g.member_count} {t_i18n(pluralize(g.member_count, "member"))}</Badge>
                <Tip content={t_i18n("Remove from group")}>
                  <button aria-label={t_i18n("Remove from group")}
                    className="text-default-secondary hover:text-feedback-error-primary transition-colors"
                    onClick={(e) => { e.stopPropagation(); handleRemove(g.id); }}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </Tip>
              </div>
            ))}
          </div>
        ) : (
          <p className="content-base text-default-secondary italic text-center py-4">{t_i18n("Not a member of any group.")}</p>
        )}
      </CardContent>
    </Card>
  );
}

// ===========================================================================
// Objects Tab
// ===========================================================================

function ObjectsTab({ userId }: { userId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data: agents } = useUserAgents(userId);
  const { data: knowledgeBases } = useUserKnowledgeBases(userId);
  const { data: mcpServers } = useUserMCPServers(userId);
  const { data: customTools } = useUserCustomTools(userId);
  const { data: integrations } = useUserIntegrations(userId);
  const { data: templates } = useAgentTemplates();

  return (
    <div className="space-y-8">
      {/* ── Agents ──────────────────────────────────────────────── */}
      <ObjectSection icon={Bot} label="Agents" count={agents?.length ?? 0}>
        {agents && agents.length > 0 ? (
          <div className="space-y-2">
            {agents.map((a) => (
              <div
                key={a.id}
                className={cn(
                  "flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors hover:bg-elevation-hover",
                  a.enabled ? "border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs" : "border-elevation-subtle-soft layer-1 bg-elevation-default opacity-60",
                )}
              >
                <div className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm",
                  a.enabled ? "bg-accent/10" : "bg-elevation-hover",
                )}>
                  <Bot className={cn("h-4 w-4", a.enabled ? "text-accent" : "text-default-secondary")} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="content-base font-medium truncate">{a.name}</p>
                    {a.template_slug && (
                      <Badge variant="outline" className="text-[9px]">
                        {templates?.find((t) => t.slug === a.template_slug)?.name ?? a.template_slug}
                      </Badge>
                    )}
                    <Badge variant={a.enabled ? "success" : "outline"} className="text-[9px]">
                      {a.enabled ? t_i18n("Active") : t_i18n("Disabled")}
                    </Badge>
                    {a.is_company_managed && <CompanyManagedBadge />}
                  </div>
                  <p className="content-caption text-default-secondary mt-0.5">
                    {t_i18n("Created {time}", { time: formatTimeAgo(intl, a.created_at) })}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptySection label="agents" />
        )}
      </ObjectSection>

      {/* ── Knowledge Bases ─────────────────────────────────────── */}
      <ObjectSection icon={Database} label="Knowledge Bases" count={knowledgeBases?.length ?? 0}>
        {knowledgeBases && knowledgeBases.length > 0 ? (
          <div className="space-y-2">
            {knowledgeBases.map((kb) => (
              <div
                key={kb.id}
                className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs px-4 py-3 transition-colors hover:bg-elevation-hover"
              >
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                  <Database className="h-4 w-4 text-accent" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="content-base font-medium truncate">{kb.name}</p>
                    {kb.is_system_managed && <Badge variant="cyan" className="text-[9px]">{t_i18n("System")}</Badge>}
                    {kb.is_company_managed && <CompanyManagedBadge />}
                  </div>
                  <div className="flex items-center gap-3 mt-0.5">
                    <span className="inline-flex items-center gap-1 content-caption text-default-secondary">
                      <FileText className="h-3 w-3" /> {formatKBStats(kb.document_count, kb.chunk_count)}
                    </span>
                    <span className="content-caption text-default-secondary">
                      {t_i18n("Created {time}", { time: formatTimeAgo(intl, kb.created_at) })}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptySection label="knowledge bases" />
        )}
      </ObjectSection>

      {/* ── MCP Servers ─────────────────────────────────────────── */}
      <ObjectSection icon={Server} label="MCP Servers" count={mcpServers?.length ?? 0}>
        {mcpServers && mcpServers.length > 0 ? (
          <div className="space-y-2">
            {mcpServers.map((s) => (
              <div
                key={s.id}
                className={cn(
                  "flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors hover:bg-elevation-hover",
                  s.enabled ? "border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs" : "border-elevation-subtle-soft layer-1 bg-elevation-default opacity-60",
                )}
              >
                <div className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm",
                  s.enabled ? "bg-accent/10" : "bg-elevation-hover",
                )}>
                  <Server className={cn("h-4 w-4", s.enabled ? "text-accent" : "text-default-secondary")} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="content-base font-medium truncate">{s.name}</p>
                    <Badge variant="outline" className="text-[9px]">
                      {TRANSPORT_DISPLAY_NAMES[s.transport] ?? s.transport}
                    </Badge>
                    <Badge variant={s.enabled ? "success" : "outline"} className="text-[9px]">
                      {s.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
                    </Badge>
                    {s.is_company_managed && <CompanyManagedBadge />}
                  </div>
                  <p className="content-caption text-default-secondary mt-0.5">
                    {t_i18n("Created {time}", { time: formatTimeAgo(intl, s.created_at) })}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptySection label="MCP servers" />
        )}
      </ObjectSection>

      {/* ── Custom Tools ────────────────────────────────────────── */}
      <ObjectSection icon={Wrench} label="Custom Tools" count={customTools?.length ?? 0}>
        {customTools && customTools.length > 0 ? (
          <div className="space-y-2">
            {customTools.map((t) => (
              <div
                key={t.id}
                className={cn(
                  "flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors hover:bg-elevation-hover",
                  t.enabled ? "border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs" : "border-elevation-subtle-soft layer-1 bg-elevation-default opacity-60",
                )}
              >
                <div className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm",
                  t.enabled ? "bg-accent/10" : "bg-elevation-hover",
                )}>
                  <Wrench className={cn("h-4 w-4", t.enabled ? "text-accent" : "text-default-secondary")} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="content-base font-medium truncate">{t.name}</p>
                    <Badge variant={t.enabled ? "success" : "outline"} className="text-[9px]">
                      {t.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
                    </Badge>
                    {t.is_company_managed && <CompanyManagedBadge />}
                  </div>
                  <p className="content-caption text-default-secondary mt-0.5">
                    {t_i18n("Created {time}", { time: formatTimeAgo(intl, t.created_at) })}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptySection label="custom tools" />
        )}
      </ObjectSection>

      {/* ── Integrations ────────────────────────────────────────── */}
      <ObjectSection icon={Link2} label="Integrations" count={integrations?.length ?? 0}>
        {integrations && integrations.length > 0 ? (
          <div className="space-y-2">
            {integrations.map((i) => {
              const shownStatus = displayedIntegrationStatus(i);
              const statusCfg = INTEGRATION_STATUS[shownStatus] ?? INTEGRATION_STATUS.disconnected;
              const StatusIcon = statusCfg.icon;
              return (
                <div
                  key={i.id}
                  className={cn(
                    "flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors hover:bg-elevation-hover",
                    shownStatus === "connected"
                      ? "border-feedback-success-primary/20 layer-1 bg-elevation-default backdrop-blur-xs"
                      : "border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs",
                  )}
                >
                  <div className={cn(
                    "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm",
                    shownStatus === "connected" ? "bg-feedback-success-primary/10" : "bg-elevation-hover",
                  )}>
                    <Link2 className={cn("h-4 w-4", shownStatus === "connected" ? "text-feedback-success-primary" : "text-default-secondary")} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="content-base font-medium truncate">{i.name}</p>
                      <Badge variant="outline" className="text-[9px]">
                        {INTEGRATION_PROVIDER_NAMES[i.provider] ?? i.provider}
                      </Badge>
                      <Badge variant={statusCfg.badge} className="text-[9px] gap-1">
                        <StatusIcon className="h-3 w-3" /> {t_i18n(statusCfg.label)}
                      </Badge>
                      {i.is_company_managed && <CompanyManagedBadge />}
                    </div>
                    <p className="content-caption text-default-secondary mt-0.5">
                      {i.auth_type} · {t_i18n("Created {time}", { time: formatTimeAgo(intl, i.created_at) })}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptySection label="integrations" />
        )}
      </ObjectSection>
    </div>
  );
}

/** Section header for object groups – icon + label + count badge. */
function ObjectSection({ icon: Icon, label, count, children }: {
  icon: React.ElementType;
  label: string;
  count: number;
  children: React.ReactNode;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-accent" />
        <h3 className="content-base font-semibold">{t_i18n(label)}</h3>
        <Badge variant="outline" className="text-[9px] px-1.5">{formatCompactNumber(count)}</Badge>
      </div>
      {children}
    </div>
  );
}

/** Empty state for object sections. */
function EmptySection({ label }: { label: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="rounded-sm border border-dashed border-elevation-subtle-soft py-8 text-center">
      <p className="content-base text-default-secondary">{t_i18n("No {label}.", { label: t_i18n(label) })}</p>
    </div>
  );
}

// ===========================================================================
// API Keys Tab
// ===========================================================================

const USER_API_KEYS_LIMIT = 200;

function ApiKeysTab({ userId }: { userId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading } = useAdminApiKeys({ ownerIds: [userId], withSummary: true, limit: USER_API_KEYS_LIMIT });
  const { data: policy } = useApiKeyPolicy();
  const { confirmElement, revoke, pendingId } = useApiKeyRevocation();
  const now = useNow();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Spinner size="lg" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
      </div>
    );
  }

  const apiKeys = data?.items ?? [];
  const total = data?.total ?? 0;

  if (apiKeys.length === 0) {
    return (
      <Card className="border-dashed">
        <CardContent className="flex flex-col items-center justify-center py-16 text-center">
          <Key className="h-12 w-12 text-default-disabled mb-4" />
          <p className="text-default-secondary">{t_i18n("No API keys.")}</p>
          <p className="content-compact text-default-secondary mt-1">{t_i18n("This user has not created any API key.")}</p>
        </CardContent>
      </Card>
    );
  }

  // Every key of the user, not only the rows served: past the row limit the
  // page would understate them.
  const summary = data?.summary;

  return (
    <div className="space-y-6">
      {confirmElement}
      {/* ── Stats ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={Key}
          label="Total Keys"
          value={total}
          subtitle={t_i18n("{count} active", { count: summary?.usable ?? 0 })}
        />
        <StatCard icon={BarChart3} label="Total Requests" value={summary?.request_count ?? 0} subtitle="Across all keys" />
        <StatCard
          icon={Zap}
          label="Last Activity"
          value={summary?.last_used_at ? formatTimeAgo(intl, summary.last_used_at) : t_i18n("Never")}
          subtitle={
            summary?.last_used_key_name
              ? t_i18n("Key: {name}", { name: summary.last_used_key_name })
              : "No usage recorded"
          }
        />
        <StatCard
          icon={Key}
          label="Most Active Key"
          value={summary?.most_active_key_name ?? "-"}
          subtitle={t_i18n("{count} requests", { count: formatCompactNumber(summary?.most_active_request_count ?? 0) })}
        />
      </div>

      {/* ── Keys List ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Key className="h-4 w-4 text-accent" /> {t_i18n("API Keys")}
            <CountBadge variant="outline" className="ml-1">{total}</CountBadge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {apiKeys.map((k) => (
            <AdminApiKeyRow
              key={k.id}
              apiKey={k}
              maxLifetimeDays={policy?.max_lifetime_days ?? 0}
              now={now}
              showOwner={false}
              revoking={pendingId === k.id}
              onRevoke={() => void revoke(k)}
            />
          ))}
          {total > apiKeys.length && (
            <p className="pt-1 content-compact text-default-secondary">
              {t_i18n("Showing the {shown} most recent of {total} keys: Settings > Access and security > API keys lists them all.", {
                shown: apiKeys.length,
                total,
              })}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}


// ===========================================================================
// Activity Tab
// ===========================================================================

/** The user's audit trail: the shared audit log table with the user fixed (#9378 slice 6y). */
function ActivityTab({ userId }: { userId: string }) {
  const { t_i18n } = useFormatter();
  return (
    <AuditLogList
      prefix="audit_"
      storageKey="user-audit-logs"
      userId={userId}
      emptyLabel={t_i18n("No audit logs for this user.")}
    />
  );
}

// ---------------------------------------------------------------------------
// UserQuotasTab
// ---------------------------------------------------------------------------

function UserQuotasTab({ userId }: { userId: string }) {
  const { t_i18n } = useFormatter();
  const { data: quotaInfo } = useUserQuotas(userId);
  const updateQuotas = useUpdateUserQuotas();
  const resetQuotas = useResetUserQuotas();
  const { data: models } = useAvailableModels({ scope: "all" });
  const { data: platformQuotas } = usePlatformQuotas();
  const isXtmOne = useIsXtmOne();
  const platformAgenticQuotas = useAgenticQuotas();
  // CE-only (no Filigran license, no EE platform) — per-user quota overrides
  // are meaningless because the agentic engine itself is disabled
  // platform-wide. Lock the form and surface "Not Applicable" instead of
  // the user-misleading "Unlimited" / "No quota configured" copy.
  const isCeOnly = useIsCommunityEdition();
  const ceOnlyTooltip = t_i18n("Activate a license or register an EE platform to configure user quotas.");

  const agenticMaxLimits = isXtmOne
    ? { daily: platformAgenticQuotas.daily, monthly: platformAgenticQuotas.monthly, yearly: platformAgenticQuotas.yearly }
    : undefined;

  const platformAgenticCap = platformQuotas?.quota_config?.agentic?.platform_limit ?? null;
  const platformAgenticCapPeriod = platformQuotas?.quota_config?.agentic?.period ?? "monthly";
  const platformLLMProxyCap = platformQuotas?.quota_config?.llm_proxy?.platform_limit ?? null;
  const platformLLMProxyCapPeriod = platformQuotas?.quota_config?.llm_proxy?.period ?? "monthly";

  const [llmProxy, setLLMProxy] = useState<QuotaTypeConfig | null>(null);
  const [agentic, setAgentic] = useState<QuotaTypeConfig | null>(null);
  const [dirty, setDirty] = useState(false);
  const [initialized, setInitialized] = useState(false);

  // Initialise from ``user_config`` (the user's raw override) — NOT
  // ``resolved_config`` (which walks user → group → platform → license).
  // Otherwise an empty override looks pre-filled with the platform
  // default, the "Clear (inherit)" button appears when there is
  // nothing to clear, and clearing + saving silently round-trips back
  // to the same resolved number on reload.
  if (quotaInfo && !initialized) {
    setLLMProxy((quotaInfo.user_config?.llm_proxy as QuotaTypeConfig) ?? null);
    setAgentic((quotaInfo.user_config?.agentic as QuotaTypeConfig) ?? null);
    setInitialized(true);
  }

  const handleSave = () => {
    // An emptied type inherits and is sent as absent.
    const llm = isXtmOne ? null : normalizeQuotaOverride(llmProxy);
    const agenticOverride = normalizeQuotaOverride(agentic);
    const quota_config = (llm || agenticOverride)
      ? { llm_proxy: llm ?? undefined, agentic: agenticOverride ?? undefined }
      : null;
    updateQuotas.mutate(
      { userId, quota_config },
      { onSuccess: () => setDirty(false) },
    );
  };

  const handleReset = () => {
    resetQuotas.mutate(userId);
  };

  const sources = quotaInfo?.sources ?? {};
  const usage = quotaInfo?.usage ?? [];

  const SOURCE_LABELS: Record<string, string> = { user: "User override", group: "Group quota", global: "Platform default", none: "No quota" };
  const QUOTA_TYPES: { key: string; label: string; icon: React.ElementType; color: string; barColor: string }[] = [
    { key: "agentic", label: "Agentic Executions", icon: Zap, color: "text-accent", barColor: "bg-accent" },
    ...(!isXtmOne ? [{ key: "llm_proxy", label: "LLM Proxy Requests", icon: Key, color: "text-feedback-success-primary", barColor: "bg-feedback-success-primary" }] : []),
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="content-base text-default-secondary">
            {t_i18n("Configure quota overrides for this specific user. These take priority over group and platform defaults. Platform-wide quotas still apply as a hard cap.")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
            <Button variant="outline" onClick={handleReset} disabled={isCeOnly || resetQuotas.isPending} className="gap-2">
              <RotateCcw className="h-4 w-4" />
              {t_i18n("Reset usage")}
            </Button>
          </Tip>
          <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
            <Button onClick={handleSave} disabled={isCeOnly || !dirty || updateQuotas.isPending} className="gap-2">
              <Save className="h-4 w-4" />
              {t_i18n("Save")}
            </Button>
          </Tip>
        </div>
      </div>

      {/* Consumption cards */}
      <div className="space-y-4">
        {QUOTA_TYPES.map(({ key, label, icon: QIcon, color, barColor }) => {
          const cfg = key === "agentic" ? quotaInfo?.resolved_config?.agentic : quotaInfo?.resolved_config?.llm_proxy;
          const source = sources[key] ?? "none";
          const limit = (cfg as QuotaTypeConfig | undefined)?.default ?? null;
          const period = (cfg as QuotaTypeConfig | undefined)?.period ?? "monthly";
          const totalEntry = usage.find((u) => u.usage_type === key && u.model === null);
          // Sandbox has no row of its own — its count is folded into the
          // displayed agentic total instead (spec 002-sandbox-quota-usage-tracking
          // FR-013/FR-014, revised).
          const current = key === "agentic"
            ? mergeSandboxIntoAgenticCount(usage, totalEntry)
            : totalEntry?.count ?? 0;
          // A ``global`` agentic entry is the shared platform-wide pool, not
          // this user's own consumption — label it so an admin doesn't read a
          // platform total as one user's usage.
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
                    <QIcon className={cn("h-4 w-4", color)} />
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

                    {key === "llm_proxy" && (cfg as QuotaTypeConfig | undefined)?.per_model && Object.keys((cfg as QuotaTypeConfig).per_model!).length > 0 && (
                      <div className="space-y-2 pt-1">
                        <p className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Per-model limits")}</p>
                        {Object.entries((cfg as QuotaTypeConfig).per_model!).map(([model, modelLimit]) => {
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

      <QuotaEditor
        llmProxy={llmProxy}
        agentic={agentic}
        onChangeLLMProxy={(v) => { setLLMProxy(v); setDirty(true); }}
        onChangeAgentic={(v) => { setAgentic(v); setDirty(true); }}
        availableModels={models}
        hideLLMProxy={isXtmOne}
        agenticMaxLimits={agenticMaxLimits}
        platformAgenticCap={platformAgenticCap}
        platformAgenticCapPeriod={platformAgenticCapPeriod}
        platformLLMProxyCap={platformLLMProxyCap}
        platformLLMProxyCapPeriod={platformLLMProxyCapPeriod}
        llmProxyTitle={t_i18n("LLM Proxy Quota Override")}
        llmProxyDescription={t_i18n("Set the maximum number of LLM proxy requests this user can make. Leave empty to inherit from their group or the platform default; turn on Unlimited to lift the limit.")}
        agenticTitle={t_i18n("Agentic Execution Quota Override")}
        agenticDescription={t_i18n("Set the maximum number of agentic executions (chat messages + assignment runs) this user can make. Leave empty to inherit from their group or the platform default; turn on Unlimited to lift the limit.")}
        limitLabel={t_i18n("Limit for this user")}
        limitHint={t_i18n("Maximum requests this user can make per period")}
        clearLabel={t_i18n("Clear (inherit from group or platform)")}
        disabled={isCeOnly}
        ceOnly={isCeOnly}
        unlimitedLabel={t_i18n("Unlimited")}
        unlimitedHint={t_i18n("This user has no limit, whatever their group or the platform default.")}
        inheritPlaceholder={t_i18n("Inherit")}
        notices={quotaInfo?.user_config_notices}
      />
    </div>
  );
}
