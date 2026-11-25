// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import {
  Webhook,
  Globe,
  Plus,
  Trash2,
  GitBranch,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  X,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { offersSlackEventsUrl } from "@/lib/slack-events";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import {
  useGitHubWebhooks,
  useCreateGitHubWebhook,
  useDeleteGitHubWebhook,
  useIntegrationElements,
  useNotionWebhookSetup,
} from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import type { Integration, ProviderCatalogItem, IntegrationElement } from "@/types";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { CopyableUrl } from "./CopyableUrl";
import { showsAnyWebhooksBlock, webhooksCardBlocks } from "./webhooks-card-blocks";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

export function WebhooksCard({
  integration,
  catalogEntry,
  baseUrl,
  canManage,
}: {
  integration: Integration;
  catalogEntry?: ProviderCatalogItem;
  baseUrl: string;
  canManage?: boolean;
}) {
  const blocks = webhooksCardBlocks(integration, catalogEntry, canManage);
  const { t_i18n } = useFormatter();

  if (!showsAnyWebhooksBlock(blocks)) return null;
  const {
    microsoft: isMicrosoft,
    slack: isSlack,
    adminEvents: showsAdminEventsBlock,
    webhookUrl: showsWebhookUrlBlock,
    notionSetup: showsNotionSetupBlock,
    callback: showsCallbackBlock,
    webhookManager: showsWebhookManager,
  } = blocks;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Webhook className="h-4 w-4 text-accent" />
          {t_i18n("Webhooks")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isMicrosoft && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <span className="content-base font-semibold">{t_i18n("Microsoft 365 events")}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("New emails and Teams messages are picked up by polling every 5 minutes. No webhook needs to be configured in Microsoft 365.")}
            </p>
          </div>
        )}

        {isSlack && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <span className="content-base font-semibold">{t_i18n("{provider} Events", { provider: catalogEntry?.name ?? "" })}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("New Slack messages are picked up by polling every 5 minutes.")}
            </p>
            {offersSlackEventsUrl(catalogEntry) && (
              <>
                <p className="content-compact text-default-secondary leading-relaxed">
                  {t_i18n("For real-time delivery, store the Slack app's Signing Secret in the Signing secret field of this integration, or of the Slack OAuth default when your administrator provides the app. Then set this URL as the Request URL in the app's Event Subscriptions: Slack verifies it with a signed request.")}
                </p>
                <CopyableUrl url={`${baseUrl}/api/v1/integrations/notifications/slack`} />
              </>
            )}
          </div>
        )}

        {showsAdminEventsBlock && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="flex items-center gap-2">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <span className="content-base font-semibold">{t_i18n("{provider} Events", { provider: catalogEntry?.name ?? "" })}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Webhooks for {provider} are managed by your administrator. Events will be delivered to your agents automatically.", { provider: catalogEntry?.name ?? "" })}
            </p>
          </div>
        )}

        {showsWebhookUrlBlock && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <span className="content-base font-semibold">{t_i18n("{provider} Webhook URL", { provider: catalogEntry?.name ?? "" })}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Configure this URL in your {provider} settings to receive events. All agents bound to this integration will automatically receive incoming events.", { provider: catalogEntry?.name ?? "" })}
            </p>
            <CopyableUrl url={`${baseUrl}/api/hooks/integrations/${integration.id}`} />
            <p className="content-caption text-default-secondary">
              {t_i18n("Set content type to")} <code className="text-accent">application/json</code>{t_i18n(". Events are distributed to all agents using this integration; no per-agent configuration is needed.")}
            </p>
          </div>
        )}

        {showsNotionSetupBlock && (
          <NotionWebhookSetupBlock integration={integration} baseUrl={baseUrl} />
        )}

        {showsCallbackBlock && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Globe className="h-3.5 w-3.5 text-accent" />
              <span className="content-base font-semibold">{t_i18n("{provider} Webhook URL", { provider: catalogEntry?.name ?? "" })}</span>
            </div>
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Set this URL as the callback URL in the {provider} webhook settings of your developer app.", { provider: catalogEntry?.name ?? "" })}
            </p>
            <CopyableUrl url={`${baseUrl}${catalogEntry?.webhook_path ?? ""}`} />
          </div>
        )}

        {/* GitHub webhook management */}
        {showsWebhookManager && (
          <GitHubWebhookManager integration={integration} catalogEntry={catalogEntry} />
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Notion webhook setup sub-component
// ---------------------------------------------------------------------------

/**
 * Notion's setup for the integration's managers: the URL to subscribe in the
 * connection's Webhooks tab, then the verification token Notion sends to it,
 * to paste in Notion's Verify form. Read from GET /integrations/{id}/notion-webhook.
 */
function NotionWebhookSetupBlock({ integration, baseUrl }: { integration: Integration; baseUrl: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useNotionWebhookSetup(integration.id);
  const url = data?.webhook_url ?? `${baseUrl}/api/hooks/integrations/${integration.id}`;
  const token = data?.verification_token ?? null;
  return (
    <div className="rounded-sm bg-elevation-hover p-3 space-y-2" data-testid="notion-webhook-setup">
      <div className="flex items-center gap-2">
        <Globe className="h-3.5 w-3.5 text-accent" />
        <span className="content-base font-semibold">{t_i18n("Notion webhook setup")}</span>
      </div>
      <p className="content-compact text-default-secondary leading-relaxed">
        {t_i18n("1. In Notion, open the connection's Webhooks tab and create a subscription with this URL.")}
      </p>
      <CopyableUrl url={url} />
      <p className="content-compact text-default-secondary leading-relaxed">
        {t_i18n("2. Notion then sends a verification token here. Paste it in the Verify form of the subscription in Notion.")}
      </p>
      {isLoading ? (
        <Spinner size="sm" tone="inherit" />
      ) : isError ? (
        <p className="content-caption text-default-secondary">{t_i18n("The verification token could not be read. Reload the page to try again.")}</p>
      ) : token ? (
        <div className="flex items-center gap-2">
          <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-compact text-accent font-mono-plex break-all select-all">
            {token}
          </code>
          <CopyIconButton value={token} label={t_i18n("Copy the verification token")} className="shrink-0" />
        </div>
      ) : (
        <p className="content-caption text-default-secondary">
          {t_i18n("No verification token received yet. Create the subscription in Notion first; the token appears here once Notion sends it.")}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// GitHub webhook manager sub-component
// ---------------------------------------------------------------------------

function GitHubWebhookManager({
  integration,
  catalogEntry,
}: {
  integration: Integration;
  catalogEntry?: ProviderCatalogItem;
}) {
  const { toast } = useToast();
  const platformName = usePlatformName();
  const { t_i18n, intl } = useFormatter();
  const { data: webhooksData, isLoading } = useGitHubWebhooks(integration.id);
  const createWebhook = useCreateGitHubWebhook();
  const deleteWebhook = useDeleteGitHubWebhook();

  const [showAddForm, setShowAddForm] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<string[]>([
    "push", "issues", "pull_request",
  ]);
  // Creation failures often carry a multi-step fix (GitHub App permissions,
  // SSO, IP allow lists, ...) that a timed toast is the wrong shape for —
  // shown as a persistent inline alert in the form instead.
  const [createError, setCreateError] = useState<string | null>(null);

  const webhooks = webhooksData?.webhooks ?? [];
  const events = catalogEntry?.available_events ?? [];

  const handleCreate = () => {
    if (!selectedRepo) {
      toast({ title: t_i18n("Select a repository first"), variant: "error" });
      return;
    }
    setCreateError(null);
    createWebhook.mutate(
      {
        integrationId: integration.id,
        repo: selectedRepo,
        events: selectedEvents,
        target: "integration",
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Webhook created on {repo}", { repo: selectedRepo }), variant: "success" });
          setShowAddForm(false);
          setSelectedRepo("");
        },
        onError: (err) => setCreateError(err.message),
      },
    );
  };

  const handleDelete = (hookId: number, repo: string) => {
    deleteWebhook.mutate(
      { integrationId: integration.id, hookId },
      {
        onSuccess: () => toast({ title: t_i18n("Webhook removed from {repo}", { repo }), variant: "success" }),
        onError: (err) => toast({ title: err.message || t_i18n("Failed to delete webhook"), variant: "error" }),
      },
    );
  };

  const toggleEvent = (event: string) => {
    setSelectedEvents((prev) =>
      prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event],
    );
  };

  return (
    <div className="rounded-sm border border-elevation-subtle-soft p-3 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch className="h-3.5 w-3.5 text-accent" />
          <span className="content-base font-semibold">{t_i18n("Managed Repository Webhooks")}</span>
        </div>
        {!showAddForm && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 content-compact gap-1.5"
            onClick={() => setShowAddForm(true)}
          >
            <Plus className="h-3 w-3" /> {t_i18n("Add Webhook")}
          </Button>
        )}
      </div>

      <p className="content-caption text-default-secondary leading-relaxed">
        {t_i18n("Automatically create webhooks on GitHub repositories. {platform} will register webhooks via the GitHub API so events flow to your agents without manual configuration.", { platform: platformName })}
      </p>

      {/* Existing webhooks */}
      {isLoading ? (
        <div className="flex items-center gap-2 py-2 content-compact text-default-secondary">
          <Spinner size="sm" tone="inherit" /> {t_i18n("Loading webhooks...")}
        </div>
      ) : webhooks.length > 0 ? (
        <div className="space-y-1.5">
          {webhooks.map((wh) => (
            <div
              key={wh.hook_id}
              className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2 group"
            >
              <GitBranch className="h-3.5 w-3.5 text-default-secondary shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="content-compact font-medium truncate">{wh.repo}</p>
                <p className="text-[9px] text-default-secondary">
                  {wh.events.join(", ")} · {wh.target === "assignment" ? t_i18n("per-assignment") : t_i18n("integration")} · {formatTimeAgo(intl, wh.created_at)}
                </p>
              </div>
              <Tip content={t_i18n("Remove webhook from repository")}>
                <button aria-label={t_i18n("Remove webhook from repository")}
                  className="shrink-0 rounded-sm p-1 text-default-secondary hover:text-feedback-error-primary transition-colors opacity-0 group-hover:opacity-100"
                  onClick={() => handleDelete(wh.hook_id, wh.repo)}
                  disabled={deleteWebhook.isPending}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </Tip>
            </div>
          ))}
        </div>
      ) : (
        <p className="content-caption text-default-secondary italic py-1">
          {t_i18n("No managed webhooks yet. Click \"Add Webhook\" to create one.")}
        </p>
      )}

      {/* Add webhook form */}
      {showAddForm && (
        <AddWebhookForm
          integrationId={integration.id}
          events={events}
          selectedRepo={selectedRepo}
          onSelectRepo={(repo) => { setSelectedRepo(repo); setCreateError(null); }}
          selectedEvents={selectedEvents}
          onToggleEvent={toggleEvent}
          onCreate={handleCreate}
          onCancel={() => { setShowAddForm(false); setSelectedRepo(""); setCreateError(null); }}
          isPending={createWebhook.isPending}
          error={createError}
          onDismissError={() => setCreateError(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add webhook form with repo picker
// ---------------------------------------------------------------------------

function AddWebhookForm({
  integrationId,
  events,
  selectedRepo,
  onSelectRepo,
  selectedEvents,
  onToggleEvent,
  onCreate,
  onCancel,
  isPending,
  error,
  onDismissError,
}: {
  integrationId: string;
  events: { event: string; label: string }[];
  selectedRepo: string;
  onSelectRepo: (repo: string) => void;
  selectedEvents: string[];
  onToggleEvent: (event: string) => void;
  onCreate: () => void;
  onCancel: () => void;
  isPending: boolean;
  error?: string | null;
  onDismissError?: () => void;
}) {
  const { t_i18n, t_text } = useFormatter();
  const { data: repoData, isLoading: reposLoading, error: reposError } = useIntegrationElements(
    integrationId,
    "repository",
  );
  const [manualRepo, setManualRepo] = useState("");
  const [showEvents, setShowEvents] = useState(false);

  // A failed refetch keeps the last list; the refusal wins over it.
  const repos = reposError ? [] : (repoData?.elements ?? []);

  const handleManualAdd = () => {
    if (manualRepo.trim() && manualRepo.includes("/")) {
      onSelectRepo(manualRepo.trim());
    }
  };

  return (
    <div className="rounded-sm border border-accent/20 bg-accent/[0.02] p-3 space-y-3">
      <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">
        {t_i18n("Select repository")}
      </p>

      {/* Manual input for public repos */}
      <div className="flex items-start gap-2">
        <Input
          aria-label={t_i18n("Repository")}
          className="flex-1"
          placeholder={t_i18n("owner/repo (type for public repos)")}
          value={manualRepo}
          onChange={(e) => setManualRepo(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleManualAdd()}
        />
        <Button
          variant="ghost"
          onClick={handleManualAdd}
          disabled={!manualRepo.includes("/")}
        >
          {t_i18n("Use")}
        </Button>
      </div>

      {/* Private repo picker */}
      {reposLoading ? (
        <div className="flex items-center gap-2 py-2 content-compact text-default-secondary">
          <Spinner size="sm" tone="inherit" /> {t_i18n("Loading repositories...")}
        </div>
      ) : repos.length > 0 ? (
        <RepoList
          repos={repos}
          selectedRepo={selectedRepo}
          onSelectRepo={onSelectRepo}
        />
      ) : reposError ? (
        <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">{reposError.message}</p>
      ) : null}

      {selectedRepo && (
        <div className="rounded-sm bg-accent/5 border border-accent/20 px-3 py-2">
          <p className="content-compact">
            <span className="text-default-secondary">{t_i18n("Selected:")} </span>
            <span className="font-medium text-accent">{selectedRepo}</span>
          </p>
        </div>
      )}

      {/* Event picker */}
      <div className="space-y-1.5">
        <button
          className="flex items-center gap-1.5 content-caption font-medium text-default-secondary uppercase tracking-wider hover:text-negative-primary transition-colors"
          onClick={() => setShowEvents(!showEvents)}
        >
          {t_i18n("Events ({count} selected)", { count: selectedEvents.length })}
          {showEvents ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </button>
        {showEvents && (
          <div className="grid grid-cols-2 gap-1">
            {events.map((ev) => (
              <button
                key={ev.event}
                className={cn(
                  "text-left rounded-sm border px-2 py-1.5 content-caption transition-colors",
                  selectedEvents.includes(ev.event)
                    ? "border-accent/40 bg-accent/10 text-accent"
                    : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle",
                )}
                onClick={() => onToggleEvent(ev.event)}
              >
                {t_text(ev.label)}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Creation error — persistent (not a timed toast): fix steps can be several lines. */}
      {error && (
        <div className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/20 bg-feedback-error-primary/[0.06] px-3 py-2">
          <AlertCircle className="h-3.5 w-3.5 text-feedback-error-primary shrink-0 mt-0.5" />
          <p className="flex-1 text-[11px] text-feedback-error-primary/90 leading-relaxed whitespace-pre-line">{error}</p>
          <button
            className="shrink-0 text-feedback-error-primary/60 hover:text-feedback-error-primary transition-colors"
            onClick={onDismissError}
            aria-label={t_i18n("Dismiss")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Actions */}
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={onCancel}>
          {t_i18n("Cancel")}
        </Button>
        <Button
          size="sm"
          className="h-7 content-compact gap-1.5"
          onClick={onCreate}
          disabled={!selectedRepo || selectedEvents.length === 0 || isPending} aria-busy={isPending || undefined}>
          {isPending && <Spinner size="sm" tone="inherit" />}{!isPending && <Webhook className="h-3 w-3" />}
          {t_i18n("Create Webhook")}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Repo list (scrollable)
// ---------------------------------------------------------------------------

function RepoList({
  repos,
  selectedRepo,
  onSelectRepo,
}: {
  repos: IntegrationElement[];
  selectedRepo: string;
  onSelectRepo: (repo: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const filtered = repos.filter(
    (r) =>
      r.name.toLowerCase().includes(search.toLowerCase()) ||
      r.id.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="space-y-1.5">
      <SearchInput
        className="max-w-none"
        placeholder={t_i18n("Filter private repositories...")}
        value={search}
        onChange={setSearch}
      />
      <div className="max-h-40 overflow-y-auto space-y-0.5 rounded-sm">
        {filtered.slice(0, 50).map((r) => (
          <button
            key={r.id}
            className={cn(
              "w-full flex items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[11px] transition-colors",
              selectedRepo === r.id
                ? "bg-accent/10 text-accent border border-accent/30"
                : "hover:bg-elevation-hover text-default-secondary border border-transparent",
            )}
            onClick={() => onSelectRepo(r.id)}
          >
            <GitBranch className="h-3 w-3 shrink-0" />
            <span className="truncate">{r.id}</span>
            {!!r.metadata?.language && (
              <span className="ml-auto text-[9px] text-default-secondary shrink-0">
                {String(r.metadata.language)}
              </span>
            )}
          </button>
        ))}
        {filtered.length === 0 && (
          <p className="content-caption text-default-secondary py-2 text-center">{t_i18n("No matching repositories")}</p>
        )}
      </div>
    </div>
  );
}
