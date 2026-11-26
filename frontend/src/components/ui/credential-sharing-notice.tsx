// SPDX-License-Identifier: Apache-2.0

/**
 * CredentialSharingNotice - says, in plain words, whose lent resource a change
 * lets other people use (or takes back from them), who they are, through what,
 * what they can do with it and how to avoid it: an integration's
 * credential, an MCP server and its secrets, a custom tool's code, a channel
 * bot's token, a variable's value, and the knowledge bases, skills and prompts
 * an agent reads as its owner's for everyone it serves. `CredentialSharingDialog` is the one
 * confirmation around it, opened by `useCredentialSharingConfirm()`.
 */

import { useState } from "react";
import { Checkbox, Spinner } from "@filigran/design-system";
import { Database, FileText, KeyRound, MessageSquare, Plug, Server, Sparkles, UserMinus, Users, Variable, Wrench } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { CHANNEL_TYPE_LABELS, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import {
  audienceIsEmpty,
  confirmsAnUnshare,
  describeAudience,
  lentResourceLabel,
  type LentKindName,
  type Translate,
} from "@/lib/credential-exposure";
import { useFormatter } from "@/i18n";
import type { ResourcePreviewItem } from "@/types";

const channelLabel = (channel: string) => CHANNEL_TYPE_LABELS[channel] ?? channel;
const providerLabel = (provider: string) => INTEGRATION_PROVIDER_NAMES[provider] ?? provider;

const KIND_ICONS: Record<LentKindName, typeof Plug> = {
  integration: Plug,
  mcp_server: Server,
  custom_tool: Wrench,
  channel_bot: MessageSquare,
  variable: Variable,
  knowledge_base: Database,
  skill: Sparkles,
  prompt: FileText,
};

function viaLabel(t: Translate, item: ResourcePreviewItem): string {
  const name = item.via?.name ?? "";
  switch (item.via?.type) {
    case "agent":
      return t("Through the agent {name}", { name });
    case "assignment":
      return name ? t("Through the assignment {name}", { name }) : t("Through this new assignment");
    case "flow":
      return t("Through the flow {name}", { name });
    case "knowledge_base":
      return t("Through the knowledge base {name}: its readers see what is synced with it", { name });
    case "skill":
      return t("Through the skill {name}: it is refreshed with it", { name });
    case "prompt":
      return t("Through the prompt {name}", { name });
    case "custom_tool":
      return t("Through the custom tool {name}", { name });
    case "general_assistant":
      return t("Through the General Assistant, for every user");
    case "channel_bot":
      return t("Through the channel bot {name}: the agents it serves answer people without an account", { name });
    case "group":
      return t("Through the group {name}: its members use what is shared with it", { name });
    case "platform_mcp":
      return t("Through the platform MCP endpoint: its key reaches the agents it exposes");
    default:
      return t("By sharing it directly");
  }
}

function whatTheyDo(t: Translate, item: ResourcePreviewItem): string {
  const provider = providerLabel(item.resource.provider ?? "");
  switch (item.resource.kind) {
    case "integration":
      return item.secret.scopes.length
        ? t("They act on {provider} with this credential (scopes: {scopes}).", { provider, scopes: item.secret.scopes.join(", ") })
        : t("They act on {provider} with this credential, with everything it allows.", { provider });
    case "mcp_server":
      return t("They call this server's tools with what it stores.");
    case "custom_tool":
      return t("Its code runs when they use it.");
    case "channel_bot":
      return t("They read and post through this bot.");
    case "knowledge_base":
      return t("The agent searches and reads its documents for them, and adds to it where it may write.");
    case "skill":
      return t("Its instructions and files guide the agent for them.");
    case "prompt":
      return t("Its text is added to what the agent is told for them.");
    default:
      return item.secret.kind === "secret_value"
        ? t("Its secret value is used in what they run; it is never shown to them.")
        : t("Its value is used in what they run.");
  }
}

const NOTE_TEXT: Record<string, string> = {
  chat_uses_own_integration:
    "People who cannot see this integration use their own, in a chat and in the agent's runs; its public link and channels use this one.",
  variable_resolves_for_viewers_and_runs:
    "It resolves for people who can see it, and in runs acting as someone who can.",
  account_not_shared: "Shared without the connected account: they connect their own.",
  share_consent_refused: "Only the account holder or an admin can share this connected account.",
};

function CredentialItem({ item }: { item: ResourcePreviewItem }) {
  const { t_i18n } = useFormatter();
  const kind = item.resource.kind as LentKindName;
  const Icon = KIND_ICONS[kind] ?? KeyRound;
  const provider = providerLabel(item.resource.provider ?? "");
  const whose = lentResourceLabel(t_i18n, {
    kind,
    secretKind: item.secret.kind,
    name: item.resource.name ?? "",
    provider,
    isCompany: item.secret.is_company,
    isYours: !!item.secret.is_yours,
    holderName: item.secret.holder_name,
  });
  const gained = !audienceIsEmpty(item.gained);
  const lost = !audienceIsEmpty(item.lost);
  return (
    <li className="rounded-sm border border-feedback-alert-primary/25 bg-feedback-alert-primary/[0.05] p-3 space-y-1.5" data-testid="credential-sharing-item">
      <div className="flex items-center gap-2">
        {kind === "integration" && item.resource.provider ? (
          <ProviderLogo provider={item.resource.provider} className="h-4 w-4" />
        ) : (
          <Icon className="h-4 w-4 text-accent shrink-0" />
        )}
        <p className="content-base font-medium">
          {whose}
          {kind === "integration" && item.resource.name && (
            <span className="text-default-secondary font-normal"> - {item.resource.name}</span>
          )}
        </p>
      </div>
      {gained && (
        <p className="content-compact flex items-start gap-1.5" data-testid="credential-sharing-gained">
          <Users className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
          <span>{t_i18n("Newly usable by {who}.", { who: describeAudience(t_i18n, item.gained, channelLabel) })}</span>
        </p>
      )}
      {lost && (
        <p className="content-compact flex items-start gap-1.5" data-testid="credential-sharing-lost">
          <UserMinus className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
          <span>{t_i18n("No longer usable by {who}.", { who: describeAudience(t_i18n, item.lost, channelLabel) })}</span>
        </p>
      )}
      <p className="content-compact text-default-secondary">{viaLabel(t_i18n, item)}</p>
      {gained && (
        <p className="content-compact text-default-secondary flex items-start gap-1.5">
          <KeyRound className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>{whatTheyDo(t_i18n, item)}</span>
        </p>
      )}
      {item.notes.filter((n) => NOTE_TEXT[n]).map((note) => (
        <p key={note} className="text-[11px] text-default-secondary/90">{t_i18n(NOTE_TEXT[note])}</p>
      ))}
      {gained && item.opt_outs.length > 0 && (
        <ul className="text-[11px] text-default-secondary/90 list-disc pl-5 space-y-0.5">
          {item.opt_outs.includes("withhold_connected_account") && (
            <li>{t_i18n("To share the integration without the account, turn on \"Don't share my connected account\" in its configuration.")}</li>
          )}
          {item.opt_outs.includes("per_user_binding") && (
            <li>{t_i18n("To have each person use their own account instead, switch the agent to per-user integrations: its assignments then use the account of the person they run as.")}</li>
          )}
          {item.opt_outs.includes("company_resource") && (
            <li>{t_i18n("Or use a company-managed one instead of a personal one.")}</li>
          )}
        </ul>
      )}
    </li>
  );
}

/** The resources a change lends to new people or takes back, one card each. */
export function CredentialSharingNotice({ items }: { items: ResourcePreviewItem[] }) {
  if (!items.length) return null;
  return (
    <ul className="space-y-2" data-testid="credential-sharing-notice">
      {items.map((item) => (
        <CredentialItem key={`${item.resource.kind}:${item.resource.id ?? "new"}:${item.via?.id ?? ""}`} item={item} />
      ))}
    </ul>
  );
}

/** The bound agents a change orphans (their owner loses the integration),
 *  one entry per agent and integration; `id` is withheld when the viewer
 *  cannot open the agent, so it cannot be unbound from here. */
export interface OrphanedAgent {
  agentId: string | null;
  name: string | null;
  ownerName: string | null;
  integrationId: string;
}

export function orphanedAgentsOf(items: ResourcePreviewItem[]): OrphanedAgent[] {
  return items.flatMap((item) =>
    item.resource.kind === "integration" && item.resource.id
      ? (item.orphaned_agents ?? []).map((a) => ({
          agentId: a.id ?? null,
          name: a.name ?? null,
          ownerName: a.owner_name ?? null,
          integrationId: item.resource.id as string,
        }))
      : [],
  );
}

function OrphanedAgents({
  orphans,
  unbind,
  onUnbindChange,
}: {
  orphans: OrphanedAgent[];
  unbind: boolean;
  onUnbindChange: (next: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const unbindable = orphans.some((o) => o.agentId);
  return (
    <section className="space-y-2 rounded-sm border border-feedback-alert-primary/25 p-3" data-testid="credential-sharing-orphans">
      <p className="content-base font-medium">{t_i18n("These agents would lose this integration")}</p>
      <p className="content-compact text-default-secondary">
        {t_i18n("Their owner would no longer have access: their runs would refuse this integration's tools.")}
      </p>
      <ul className="content-compact list-disc pl-5 space-y-0.5">
        {orphans.map((o) => (
          <li key={`${o.integrationId}:${o.agentId ?? o.ownerName ?? ""}`}>
            {o.name
              ? t_i18n("{name}, owned by {owner}", { name: o.name, owner: o.ownerName ?? "" })
              : t_i18n("An agent you cannot open, owned by {owner}", { owner: o.ownerName ?? "" })}
          </li>
        ))}
      </ul>
      {unbindable && (
        <Checkbox
          id="credential-sharing-unbind"
          label={t_i18n("Unbind them too")}
          checked={unbind}
          onCheckedChange={(checked) => onUnbindChange(checked === true)}
        />
      )}
    </section>
  );
}

/** The one confirmation: confirm proceeds, cancel keeps things as they are.
 *  When the change orphans bound agents, they are listed and `onResolve`'s
 *  second argument says whether to unbind them in the same step. */
export function CredentialSharingDialog({
  open,
  checking = false,
  items,
  truncated = false,
  note,
  mode = "share",
  fallback,
  confirmLabel,
  unshareConfirmLabel,
  cancelLabel,
  onResolve,
}: {
  open: boolean;
  /** The preview is still being asked: Confirm waits for who loses access. */
  checking?: boolean;
  items: ResourcePreviewItem[];
  /** A lookup of the preview was cut: `items` may not be all the change lends. */
  truncated?: boolean;
  note?: string;
  mode?: "share" | "unshare" | "delete";
  fallback?: { title: string; description?: string };
  confirmLabel?: string;
  /** The confirm label when the change only takes access away. */
  unshareConfirmLabel?: string;
  cancelLabel?: string;
  onResolve: (confirmed: boolean, unbindOrphans?: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const [unbind, setUnbind] = useState(true);
  const orphans = orphanedAgentsOf(items);
  const anyLost = items.some((i) => !audienceIsEmpty(i.lost));
  const anyGained = items.some((i) => !audienceIsEmpty(i.gained));
  const title = !items.length
    ? fallback ? t_i18n(fallback.title) : t_i18n("Before you save")
    : mode === "delete"
      ? t_i18n("{count, plural, one {Delete a resource other people use?} other {Delete what # resources lend to other people?}}", { count: items.length })
      : anyGained
        ? t_i18n("{count, plural, one {Share a resource with other people?} other {Share # resources with other people?}}", { count: items.length })
        : t_i18n("{count, plural, one {Stop sharing a resource?} other {Stop sharing # resources?}}", { count: items.length });
  const description = !items.length
    ? fallback?.description ? t_i18n(fallback.description) : undefined
    : anyGained && anyLost
      ? t_i18n("After this change, some people can use what is listed below and others no longer can. Nothing is refused: confirm to go ahead, or keep things as they are.")
      : anyGained
        ? t_i18n("After this change, the people below can use what is listed. Nothing is refused: confirm to share, or keep things as they are.")
        : t_i18n("After this change, the people below can no longer use what is listed. Confirm to go ahead, or keep sharing.");
  const unsharing = confirmsAnUnshare(items, mode);
  const defaultConfirm = mode === "delete" ? t_i18n("Delete") : unsharing ? t_i18n("Unshare") : t_i18n("Share");
  const defaultCancel = unsharing ? t_i18n("Keep sharing") : t_i18n("Keep things as they are");
  const confirmText = (unsharing ? unshareConfirmLabel : undefined) ?? confirmLabel ?? defaultConfirm;
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onResolve(false); }}>
      <DialogContent className="max-w-lg" data-testid="credential-sharing-dialog">
        <DialogHeader>
          <DialogTitle className="text-base">{title}</DialogTitle>
          {description && (
            <DialogDescription className="content-base leading-relaxed">{description}</DialogDescription>
          )}
        </DialogHeader>
        {note && (
          <p className="content-compact rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 p-3 text-feedback-warning-tertiary dark:text-feedback-warning-tertiary">{note}</p>
        )}
        {truncated && !checking && (
          <p
            className="content-compact rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 p-3 text-feedback-warning-tertiary dark:text-feedback-warning-tertiary"
            data-testid="credential-sharing-truncated"
          >
            {t_i18n("The preview could not check everything this change reaches: it may affect more than what is listed here.")}
          </p>
        )}
        {checking && (
          <p role="status" className="flex items-center gap-2 content-compact text-default-secondary" data-testid="credential-sharing-checking">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Checking who would lose access...")}
          </p>
        )}
        <div className="max-h-[50vh] overflow-y-auto pr-1">
          <CredentialSharingNotice items={items} />
          {orphans.length > 0 && !checking && (
            <div className="pt-2">
              <OrphanedAgents orphans={orphans} unbind={unbind} onUnbindChange={setUnbind} />
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={() => onResolve(false)} data-testid="credential-sharing-cancel">
            {cancelLabel ?? defaultCancel}
          </Button>
          <Button size="sm" onClick={() => onResolve(true, orphans.length > 0 && unbind)} disabled={checking} data-testid="credential-sharing-confirm">
            {confirmText}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
