/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { IconButton, Spinner } from "@filigran/design-system";
import { useEffect, useId, useState, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Bot,
  Building2,
  Check,
  CheckCircle2,
  Plus,
  Trash2,
  MessageSquare,
  Eye,
  EyeOff,
  Sparkles,
  ShieldAlert,
  Wrench,
  BookOpen,
  Pencil,
  ArrowDownToLine,
  ArrowUpFromLine,
  Info,
  Save,
  Copy,
  UserX,
  AtSign,
  X,
} from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SettingsField, SettingsFieldGrid, SettingsGroupTitle, SettingsSwitchField, focusFirstInvalidField, settingsFieldHintId } from "@/components/ui/settings-form";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { hitlState, toolApprovalsHref } from "@/lib/hitl";
import { useToolGateCatalog, type ToolGateEntry } from "@/hooks/api/useHitlWhitelist";
import { useApprovalsInUse } from "@/hooks/api/useToolApprovals";
import { useExemptReason, useGateReason } from "@/hooks/useHitlWords";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { useChannelBotCatalog, useChannelBots, useChannelBotChannelsMap, useCreateChannelBot, useDeleteChannelBot, useUpdateChannelBot, useChannelBotTools, useTestChannelBotTool } from "@/hooks/api/useChannelBotApi";
import { useToast } from "@/components/ui/toast";
import { useChannelRoutingSummary, useUnrouteAgent } from "@/hooks/api/useAgentApi";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { ToolTestDialog } from "@/components/ui/tool-test-dialog";
import type { ChannelBotCatalogItem, ChannelBot, JsonSchema, ProviderTool, AllowedChannel } from "@/types";
import { CHANNEL_ICONS, ICON_MAP, COPY_FEEDBACK_DELAY, providerLogoSrc } from "@/lib/constants";
import { SetupGuideDialog } from "@/components/ui/setup-guide-dialog";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { ResourceSharingWaysBadge } from "@/components/ui/sharing-ways-badge";
import { ResourceSharingCard } from "@/components/ui/resource-sharing-card";
import { SecretField } from "@/components/ui/secret-field";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { ChannelPicker } from "@/components/ui/channel-picker";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { platformOrigin } from "@/lib/base-path";

// ---------------------------------------------------------------------------
// Bot form fields: what each credential is, an example, where to find it, and
// what happens when it is empty (Design standards s.2). `required` fields block
// the creation of a bot; `secret: false` values are typed in clear (an ID).
// ---------------------------------------------------------------------------
const GUID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";

interface ChannelFieldCopy {
  label: string;
  helper?: string;
  info?: string;
  placeholder?: string;
  required?: boolean;
  secret?: boolean;
}

const channelFieldId = (provider: string, key: string) => `channel-${provider}-${key}`;

function channelFieldCopy(t_i18n: (text: string) => string, provider: string, key: string, fallbackLabel?: string): ChannelFieldCopy {
  switch (key) {
    case "api_key": {
      const label = t_i18n("Bot Token / Access Token");
      switch (provider) {
        case "slack_bot":
          return { label, required: true, secret: true, helper: t_i18n("The Bot User OAuth Token of the Slack app, starting with xoxb-."), info: t_i18n("In api.slack.com/apps, open the app, then OAuth & Permissions, Bot User OAuth Token. The agents read and post in Slack with it.") };
        case "whatsapp_bot":
          return { label, required: true, secret: true, helper: t_i18n("The permanent access token of a system user of the Meta app."), info: t_i18n("In Meta Business Settings, System users, Generate new token, with the whatsapp_business_messaging permission. It starts with EAA. Messages are sent with it.") };
        case "telegram_bot":
          return { label, required: true, secret: true, helper: t_i18n("The token BotFather gave when the bot was created."), info: t_i18n("In Telegram, send /newbot or /token to @BotFather. The token is the bot number followed by a secret part. Messages are read and sent with it.") };
        case "googlechat_bot":
          return { label, required: true, secret: true, helper: t_i18n("The JSON key of a service account of the Google Cloud project."), info: t_i18n("In Google Cloud console, IAM and admin, Service accounts, Keys, Add key, JSON. Paste the whole file. The bot replies in Google Chat with it.") };
        case "discord_bot":
          return { label, required: true, secret: true, helper: t_i18n("The bot token of the Discord application."), info: t_i18n("In discord.com/developers/applications, open the application, then Bot, Reset Token. It is shown once. The bot reads and posts with it.") };
        default:
          return { label, required: true, secret: true };
      }
    }
    case "client_id":
      return { label: t_i18n("App ID / Client ID"), required: true, secret: false, placeholder: GUID_PLACEHOLDER, helper: t_i18n("The Application (client) ID of the bot's app registration in Microsoft Entra."), info: t_i18n("A GUID, in Azure portal, App registrations, the bot's app, Overview. Teams sends messages to the bot under this ID.") };
    case "client_secret":
      return { label: t_i18n("App Password / Client Secret"), required: true, secret: true, helper: t_i18n("A client secret of the same app registration."), info: t_i18n("In Azure portal, App registrations, the bot's app, Certificates & secrets, New client secret. Copy its Value, shown once. The bot signs in to Teams with it.") };
    case "tenant_id":
      return { label: t_i18n("Tenant ID"), secret: false, placeholder: GUID_PLACEHOLDER, helper: t_i18n("The Directory (tenant) ID of the app registration in Microsoft Entra."), info: t_i18n("A GUID, on the Overview of the app registration in Microsoft Entra. Empty, the bot signs in through the common endpoint and files shared in Teams are skipped.") };
    case "signing_secret":
      return { label: t_i18n("Signing secret"), required: true, secret: true, helper: t_i18n("The secret Slack signs its events with, so forged events are refused."), info: t_i18n("In api.slack.com/apps, open the app, then Basic Information, App Credentials, Signing Secret. Without it, every Slack event is rejected.") };
    case "phone_number_id":
      return { label: t_i18n("Phone number ID"), required: true, secret: false, placeholder: "106540352242922", helper: t_i18n("The ID of the WhatsApp Business phone number the bot sends from."), info: t_i18n("In Meta for Developers, open the app, then WhatsApp, API Setup, Phone number ID. It is a number ID, not the phone number itself.") };
    case "verify_token":
      return { label: t_i18n("Verify token"), required: true, secret: true, helper: t_i18n("A value you choose, typed again in the webhook settings of the Meta app."), info: t_i18n("Any random string, for example 32 characters from a password manager. Meta sends it once to confirm the webhook URL below. Without it, the webhook cannot be confirmed.") };
    case "app_secret":
      return { label: t_i18n("App secret"), secret: true, helper: t_i18n("The secret of the Meta app, so forged webhook calls are refused."), info: t_i18n("In Meta for Developers, open the app, then App settings, Basic, App secret. Empty, webhook calls are accepted unverified.") };
    case "webhook_secret_token":
      return { label: t_i18n("Webhook secret token"), secret: true, helper: t_i18n("A value you choose and pass as secret_token to setWebhook."), info: t_i18n("1 to 256 letters, digits, underscores or hyphens. Telegram sends it with every update and any other update is refused. Empty, updates are accepted unverified.") };
    case "project_number":
      return { label: t_i18n("Project number"), secret: false, placeholder: "123456789012", helper: t_i18n("The number of the Google Cloud project that hosts the Chat app."), info: t_i18n("Digits only, on the dashboard of the project in Google Cloud console. Google Chat calls are checked against it. Empty, calls are accepted unverified.") };
    case "public_key":
      return { label: t_i18n("Public key"), secret: false, helper: t_i18n("The public key of the Discord application, so forged interactions are refused."), info: t_i18n("64 hexadecimal characters, in discord.com/developers/applications, the application, General Information, Public Key. Empty, interactions are accepted unverified.") };
    default:
      return { label: fallbackLabel ?? key };
  }
}


// ---------------------------------------------------------------------------
// Channel Management (Settings section)
// ---------------------------------------------------------------------------
export function ChannelManagement({ isAdmin }: { isAdmin: boolean }) {
  const { t_i18n } = useFormatter();
  const { data: channelCatalog } = useChannelBotCatalog();
  // `?bot=<id>` (a row of Administration > Objects) brings that bot's card into
  // view and the card shows that bot. It may be outside the library set (a bot
  // restricted to other people), so it is read from the admin scope.
  const [searchParams] = useSearchParams();
  const focusedBotId = searchParams.get("bot");
  const { data: visibleChannelBots } = useChannelBots();
  const { data: scopedChannelBots } = useChannelBots({ scope: focusedBotId ? "all" : "visible" });
  const channelBots = useMemo(
    () => withBoundEntities(visibleChannelBots, scopedChannelBots, focusedBotId ? [focusedBotId] : []),
    [visibleChannelBots, scopedChannelBots, focusedBotId],
  );
  const focusedBot = focusedBotId ? channelBots?.find((b) => b.id === focusedBotId) : undefined;
  const { data: authConfig } = useAuthConfig();
  const { data: routingSummary } = useChannelRoutingSummary(isAdmin);
  const createBot = useCreateChannelBot();
  const aiOff = useAiActionDisabled();
  const deleteBot = useDeleteChannelBot();
  // Deleting a bot takes it from every agent that answers or posts through it.
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const updateBot = useUpdateChannelBot();
  const unrouteAgent = useUnrouteAgent();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const platformName = usePlatformName();
  const baseUrl = authConfig?.base_url ?? platformOrigin();

  const [configuringProvider, setConfiguringProvider] = useState<string | null>(null);
  const [editingBotId, setEditingBotId] = useState<string | null>(null);
  const editingBot = editingBotId ? channelBots?.find((bot) => bot.id === editingBotId) : undefined;
  const editingBotLocations = editingBot?.vault_secrets?.locations;
  const [showSecrets, setShowSecrets] = useState(false);
  const [formFields, setFormFields] = useState<Record<string, string>>({});
  const [formExtra, setFormExtra] = useState<Record<string, string>>({});
  // Set by a refused Save of a new bot: the empty required fields then show
  // their inline error, cleared as soon as the field is filled.
  const [saveAttempted, setSaveAttempted] = useState(false);
  const [toolsDialog, setToolsDialog] = useState<{ bot: ChannelBot; catalog: ChannelBotCatalogItem } | null>(null);

  // Inline whitelist editing state: { botId: { direction: channels[] } }
  const [editingWhitelist, setEditingWhitelist] = useState<{ id: string; direction: "receive" | "publish" } | null>(null);
  const [editChannels, setEditChannels] = useState<AllowedChannel[]>([]);
  const [newChId, setNewChId] = useState("");
  const [newChName, setNewChName] = useState("");
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);

  const focusedProvider = focusedBot?.provider ?? null;
  useEffect(() => {
    if (!focusedProvider) return;
    document.getElementById(`channel-card-${focusedProvider}`)?.scrollIntoView({ block: "center" });
  }, [focusedProvider]);

  // Only fetch channels for the bot currently being edited (lazy loading).
  const activeBotIds = useMemo(() => {
    if (!editingWhitelist) return [];
    return [editingWhitelist.id];
  }, [editingWhitelist]);
  const { map: platformChannelsMap, loadingMap: channelsLoadingMap } = useChannelBotChannelsMap(activeBotIds);

  const copyUrl = (url: string) => {
    navigator.clipboard.writeText(url);
    setCopiedUrl(url);
    setTimeout(() => setCopiedUrl(null), COPY_FEEDBACK_DELAY);
  };

  const handleConfigure = (catalog: ChannelBotCatalogItem) => {
    setConfiguringProvider(catalog.provider);
    setEditingBotId(null);
    setFormFields({});
    setFormExtra({});
    setShowSecrets(false);
    setSaveAttempted(false);
  };

  const handleEdit = (_catalog: ChannelBotCatalogItem, bot: ChannelBot) => {
    setConfiguringProvider(bot.provider);
    setEditingBotId(bot.id);
    setFormFields({});
    setFormExtra({});
    setShowSecrets(false);
    setSaveAttempted(false);
  };

  // The required fields of a provider left empty, in the form's order. An edit
  // keeps the stored values of blank fields, so only a new bot is checked.
  const missingRequired = (catalog: ChannelBotCatalogItem): string[] => {
    if (editingBotId) return [];
    const keys = [...catalog.fields, ...Object.keys(catalog.extra_fields ?? {})];
    return keys.filter((key) => {
      if (!channelFieldCopy(t_i18n, catalog.provider, key).required) return false;
      const value = catalog.fields.includes(key) ? formFields[key] : formExtra[key];
      return !value?.trim();
    });
  };

  const fieldError = (catalog: ChannelBotCatalogItem, key: string): string | undefined =>
    saveAttempted && missingRequired(catalog).includes(key) ? t_i18n("This field is required.") : undefined;

  const closeConfigure = () => {
    setConfiguringProvider(null);
    setEditingBotId(null);
    setSaveAttempted(false);
  };

  // Who stops reaching the agent through this bot, and what it carried.
  const unroute = async (agentId: string, channelType: string) => {
    if (!(await confirmSharing(
      { entity_type: "agent", entity_id: agentId, change: { remove_channel: channelType } },
      { mode: "unshare", confirmLabel: t_i18n("Unroute") },
    ))) return;
    unrouteAgent.mutate({ agentId, channelType }, {
      onSuccess: () => toast({ title: t_i18n("Agent unrouted from channel"), variant: "success" }),
    });
  };

  const handleSave = async () => {
    if (!configuringProvider) return;
    const catalog = channelCatalog?.find((c) => c.provider === configuringProvider);
    if (!catalog) return;

    const missing = missingRequired(catalog);
    if (missing.length > 0) {
      setSaveAttempted(true);
      focusFirstInvalidField(missing.map((key) => channelFieldId(catalog.provider, key)));
      return;
    }

    const closeForm = () => {
      closeConfigure();
      setFormFields({});
      setFormExtra({});
    };

    if (editingBotId) {
      // Update existing channel bot credentials
      updateBot.mutate(
        {
          id: editingBotId,
          ...(formFields.client_id ? { client_id: formFields.client_id } : {}),
          ...(formFields.client_secret ? { client_secret: formFields.client_secret } : {}),
          ...(formFields.api_key ? { api_key: formFields.api_key } : {}),
          ...(Object.keys(formExtra).length > 0 ? { extra_config: formExtra } : {}),
        },
        { onSuccess: closeForm },
      );
    } else {
      // A company bot, usable by every agent's channels once created.
      const holdsSecret = !!(formFields.api_key || formFields.client_secret || Object.keys(formExtra).length);
      if (!(await confirmSharing(
        { entity_type: "channel_bot", change: { name: catalog.name, provider: configuringProvider, is_company_managed: true, holds_secret: holdsSecret } },
        { confirmLabel: t_i18n("Create") },
      ))) return;
      createBot.mutate(
        {
          provider: configuringProvider,
          name: catalog.name,
          client_id: formFields.client_id || undefined,
          client_secret: formFields.client_secret || undefined,
          api_key: formFields.api_key || undefined,
          extra_config: Object.keys(formExtra).length > 0 ? formExtra : undefined,
          is_company_managed: true,
        },
        { onSuccess: closeForm },
      );
    }
  };

  const getChannelBot = (provider: string): ChannelBot | undefined =>
    focusedBot?.provider === provider ? focusedBot : channelBots?.find((b) => b.provider === provider);

  const startEditWhitelist = (botId: string, direction: "receive" | "publish", current: AllowedChannel[] | null) => {
    setEditingWhitelist({ id: botId, direction });
    setEditChannels(current ?? []);
    setNewChId("");
    setNewChName("");
  };

  const addEditChannel = () => {
    const id = newChId.trim();
    const name = newChName.trim() || id;
    if (!id) return;
    if (editChannels.some((c) => c.id === id)) {
      toast({ title: t_i18n("Duplicate"), description: t_i18n("This channel ID is already in the list."), variant: "error" });
      return;
    }
    setEditChannels((prev) => [...prev, { id, name, group: "" }]);
    setNewChId("");
    setNewChName("");
  };

  const saveWhitelist = () => {
    if (!editingWhitelist) return;
    const field = editingWhitelist.direction === "receive" ? "allowed_receive_channels" : "allowed_publish_channels";
    updateBot.mutate(
      { id: editingWhitelist.id, [field]: editChannels.length > 0 ? editChannels : [] },
      {
        onSuccess: () => {
          toast({ title: editingWhitelist.direction === "receive" ? t_i18n("Receive whitelist updated") : t_i18n("Publish whitelist updated"), variant: "success" });
          setEditingWhitelist(null);
        },
      },
    );
  };

  return (
    <>
      {ConfirmEl}
      {consentElement}
      <Card>
        <CardContent className="pt-6">
          <div className="space-y-4">
            {/* ── Web Chat (always active) ── */}
            <div className="rounded-sm border border-accent/20 bg-accent/[0.02] p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10">
                    <MessageSquare className="h-5 w-5 text-accent" />
                  </div>
                  <div>
                    <p className="content-base font-medium">{t_i18n("Web Chat")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("Built-in chat interface, always available for all users and agents.")}</p>
                  </div>
                </div>
                <Badge variant="success" className="content-caption">
                  <Check className="h-3 w-3 mr-1" />
                  {t_i18n("Always Active")}
                </Badge>
              </div>
            </div>

            {/* ── Dynamic channels from catalog ── */}
            {channelCatalog?.map((ch) => {
              const existing = getChannelBot(ch.provider);
              const iconConfig = CHANNEL_ICONS[ch.provider] ?? { icon: MessageSquare, bg: "bg-elevation-hover", fg: "text-default-secondary" };
              const IconComp = iconConfig.icon;
              const logoSrc = providerLogoSrc(ch.provider);
              const channelType = channelTypeFromProvider(ch.provider);
              const routing = routingSummary?.[channelType];
              const toolCount = ch.available_tools?.length ?? 0;

              return (
                <div
                  key={ch.provider}
                  id={`channel-card-${ch.provider}`}
                  className={cn(
                    "rounded-sm border transition-all",
                    existing ? "border-elevation-subtle-soft bg-elevation-hover" : "border-elevation-subtle-soft bg-elevation-hover",
                    focusedProvider === ch.provider && "ring-1 ring-accent/40",
                  )}
                >
                  {/* ── Header row ── */}
                  <div className="flex items-center justify-between p-4">
                    <div className="flex items-center gap-3">
                      <div className={cn("flex h-10 w-10 items-center justify-center rounded-sm", iconConfig.bg)}>
                        {iconConfig.logo
                          ? <img src={iconConfig.logo} alt={ch.name} className="h-5 w-5 object-contain" />
                          : existing
                            ? <img src={logoSrc} alt={ch.name} className="h-5 w-5 object-contain" />
                            : <IconComp className={cn("h-5 w-5", iconConfig.fg)} />}
                      </div>
                      <div>
                        <div className="flex items-center gap-2">
                          <p className="content-base font-medium">{ch.name}</p>
                          {existing && (
                            <Badge variant="success" className="content-caption h-4">
                              <CheckCircle2 className="h-2.5 w-2.5 mr-0.5" />
                              {t_i18n("Connected")}
                            </Badge>
                          )}
                        </div>
                        <p className="content-compact text-default-secondary mt-0.5">{ch.description}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {/* Where this bot is used, and by whom; admins manage bots. */}
                      {existing && <ResourceSharingWaysBadge kind="channel_bot" id={existing.id} />}
                      {/* Tools button (connected bots only) */}
                      {existing && toolCount > 0 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="gap-1.5 px-2 text-default-secondary hover:text-accent"
                          onClick={() => setToolsDialog({ bot: existing, catalog: ch })}
                        >
                          <Wrench className="h-3 w-3" />
                          {toolCount} {t_i18n(pluralize(toolCount, "tool"))}
                        </Button>
                      )}
                      {ch.setup_guide && (
                        <SetupGuideDialog guide={ch.setup_guide} providerName={ch.name} />
                      )}
                      {existing ? (
                        isAdmin && (
                          <div className="flex items-center gap-1">
                            <Tip content={t_i18n("Edit credentials")}>
                              <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Edit credentials")} icon={<Pencil className="h-3.5 w-3.5" />} className="h-8 w-8 text-default-secondary hover:text-accent" onClick={() => handleEdit(ch, existing)} />
                            </Tip>
                            <Button aria-label={t_i18n("Delete bot")}
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8 text-default-secondary hover:text-feedback-error-primary"
                              onClick={async () => { if (await confirmSharing({ entity_type: "channel_bot", entity_id: existing.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete this channel bot?", description: "Agents stop receiving and sending messages through it. This cannot be undone." } })) deleteBot.mutate(existing.id); }} disabled={aiOff}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        )
                      ) : (
                        isAdmin ? (
                            <Button
                              variant="secondary"
                              size="sm"
                              className="gap-1.5 content-compact"
                              onClick={() => handleConfigure(ch)}
                            >
                              <Plus className="h-3 w-3" />
                              {t_i18n("Configure")}
                            </Button>
                        ) : (
                          <Badge variant="outline" className="content-caption">{t_i18n("Not configured")}</Badge>
                        )
                      )}
                    </div>
                  </div>

                  {/* ── Connected bot details — 2-column: Receive | Publish ── */}
                  {existing && (
                    <div className="border-t border-elevation-subtle-soft px-4 pb-4 pt-3">
                      <VaultSecretsNotice
                        status={existing.vault_secrets}
                        fieldLabels={{ api_key: t_i18n("Bot Token / Access Token"), client_secret: t_i18n("App Password / Client Secret") }}
                        className="mb-3"
                      />
                      {/* Allow Anonymous toggle */}
                      {isAdmin && (
                        <SettingsSwitchField
                          id={`channel-bot-${existing.id}-allow-anonymous`}
                          className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5 mb-3"
                          label={
                            <span className="flex items-center gap-2">
                              <UserX className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" aria-hidden="true" />
                              {t_i18n("Allow anonymous users")}
                            </span>
                          }
                          helper={t_i18n("People without an account can talk to the agents this bot serves.")}
                          info={t_i18n("Process messages from users without a {platform} account. Channel user info is passed as context. Can be overridden per-agent.", { platform: platformName })}
                          checked={existing.allow_anonymous}
                          onCheckedChange={async (v) => {
                            // Every agent it serves answers people without an account, or stops.
                            if (!(await confirmSharing(
                              { entity_type: "channel_bot", entity_id: existing.id, change: { allow_anonymous: v } },
                              v ? { confirmLabel: t_i18n("Enable") } : { mode: "unshare" },
                            ))) return;
                            updateBot.mutate(
                              { id: existing.id, allow_anonymous: v },
                              {
                                onSuccess: () =>
                                  toast({
                                    title: v ? t_i18n("Anonymous access enabled") : t_i18n("Anonymous access disabled"),
                                    variant: "success",
                                  }),
                              },
                            );
                          }}
                          disabled={updateBot.isPending || aiOff}
                        />
                      )}

                      {/* Allow Private Conversations toggle */}
                      {isAdmin && (
                        <SettingsSwitchField
                          id={`channel-bot-${existing.id}-allow-private`}
                          className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5 mb-3"
                          label={
                            <span className="flex items-center gap-2">
                              <MessageSquare className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
                              {t_i18n("Allow private conversations")}
                            </span>
                          }
                          helper={t_i18n("People can message the bot directly, even when its receive channels are restricted.")}
                          info={t_i18n("Private/DM conversations bypass channel restrictions. When enabled, users can always message the bot directly even if receive channels are restricted. Can be overridden per-agent.")}
                          checked={existing.allow_private}
                          onCheckedChange={(v) =>
                            updateBot.mutate(
                              { id: existing.id, allow_private: v },
                              {
                                onSuccess: () =>
                                  toast({
                                    title: v ? t_i18n("Private conversations enabled") : t_i18n("Private conversations disabled"),
                                    variant: "success",
                                  }),
                              },
                            )
                          }
                          disabled={updateBot.isPending || aiOff}
                        />
                      )}

                      {/* Approval gate. A bot contributes tools that
                          send, edit and delete messages in somebody's Slack,
                          Teams or Discord, and until now the platform-wide
                          whitelist was the only lever — one decision for every
                          bot at once. This one is per configured bot.

                          Deliberately not offered as an "off by default"
                          convenience: a bot's read tools are the one family the
                          classifier never exempts automatically, because
                          reading a channel is reading people's conversations. */}
                      {isAdmin && (
                        <SettingsSwitchField
                          id={`channel-bot-${existing.id}-requires-approval`}
                          className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5 mb-3"
                          label={
                            <span className="flex items-center gap-2">
                              <HitlIcon state={hitlState(existing.requires_approval)} decorative />
                              {t_i18n("Ask before running tools")}
                            </span>
                          }
                          helper={t_i18n("Conversations through this bot, and its own message tools, pause for approval.")}
                          info={t_i18n("Conversations through this bot pause for approval, and so do its own send/edit/delete tools wherever they are called. Turning this off runs them uninterrupted: this surface cannot collect an approval, so a gated tool asked for here otherwise just stops.")}
                          checked={existing.requires_approval}
                          onCheckedChange={async (v) => {
                            if (!v) {
                              const ok = await confirm({
                                title: t_i18n("Let {bot} act without asking?", { bot: existing.name }),
                                description: t_i18n(
                                  "Nothing an agent does in a conversation through this bot will pause for approval, including tools that change or delete things outside chat, and its own message tools stop pausing everywhere. The change is audited and can be undone here.",
                                ),
                                confirmLabel: t_i18n("Let it act"),
                                variant: "warning",
                              });
                              if (!ok) return;
                            }
                            updateBot.mutate(
                              { id: existing.id, requires_approval: v },
                              {
                                onSuccess: () =>
                                  toast({
                                    title: v
                                      ? t_i18n("Its tools ask for approval again")
                                      : t_i18n("Its tools now run without asking"),
                                    variant: "success",
                                  }),
                              },
                            );
                          }}
                          disabled={updateBot.isPending || aiOff}
                        />
                      )}

                      {/* Only Mention toggle */}
                      {isAdmin && (
                        <SettingsSwitchField
                          id={`channel-bot-${existing.id}-only-mention`}
                          className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5 mb-3"
                          label={
                            <span className="flex items-center gap-2">
                              <AtSign className="h-3.5 w-3.5 shrink-0 text-filigran-ia-primary" aria-hidden="true" />
                              {t_i18n("Only when mentioned")}
                            </span>
                          }
                          helper={t_i18n("In group channels, the bot answers only messages that @mention it.")}
                          info={t_i18n("Only respond to messages that explicitly @mention the bot in group channels. Private/DM conversations are unaffected. Can be overridden per-agent.")}
                          checked={existing.only_mention}
                          onCheckedChange={(v) =>
                            updateBot.mutate(
                              { id: existing.id, only_mention: v },
                              {
                                onSuccess: () =>
                                  toast({
                                    title: v ? t_i18n("Only-mention mode enabled") : t_i18n("Only-mention mode disabled"),
                                    variant: "success",
                                  }),
                              },
                            )
                          }
                          disabled={updateBot.isPending || aiOff}
                        />
                      )}

                      <div className="grid gap-3 sm:grid-cols-2">

                        {/* ── Receive column ── */}
                        <div className="rounded-sm border border-feedback-success-primary/10 bg-feedback-success-primary/[0.02] p-3 space-y-3">
                          <div className="flex items-center gap-2">
                            <ArrowDownToLine className="h-3.5 w-3.5 text-feedback-success-primary" />
                            <span className="content-compact font-medium text-feedback-success-primary">{t_i18n("Receive")}</span>
                          </div>

                          {/* Webhook URL */}
                          {ch.webhook_path && (() => {
                            const fullUrl = `${baseUrl}${ch.webhook_path}`;
                            return (
                              <div className="space-y-1">
                                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Webhook URL")}</p>
                                <div className="flex items-center gap-1.5 rounded-sm bg-elevation-hover px-2 py-1.5 border border-elevation-subtle-soft">
                                  <p className="content-caption font-mono-plex text-accent/80 break-all flex-1 select-all">
                                    {fullUrl}
                                  </p>
                                  <button
                                    className="shrink-0 rounded-sm p-1 hover:bg-elevation-hover transition-colors"
                                    onClick={() => copyUrl(fullUrl)}
                                  >
                                    {copiedUrl === fullUrl
                                      ? <Check className="h-3 w-3 text-feedback-success-primary" />
                                      : <Copy className="h-3 w-3 text-default-secondary" />}
                                  </button>
                                </div>
                              </div>
                            );
                          })()}

                          {/* Whitelist */}
                          {isAdmin && (
                            <InlineWhitelistCard
                              direction="receive"
                              channels={existing.allowed_receive_channels}
                              platformChannels={platformChannelsMap[existing.id] ?? []}
                              isEditing={editingWhitelist?.id === existing.id && editingWhitelist.direction === "receive"}
                              editChannels={editChannels}
                              newChId={newChId}
                              newChName={newChName}
                              isSaving={updateBot.isPending}
                              loading={channelsLoadingMap[existing.id] ?? false}
                              onStartEdit={() => startEditWhitelist(existing.id, "receive", existing.allowed_receive_channels)}
                              onCancel={() => setEditingWhitelist(null)}
                              onSave={saveWhitelist}
                              onAddChannel={addEditChannel}
                              onSetChannels={setEditChannels}
                              onNewIdChange={setNewChId}
                              onNewNameChange={setNewChName}
                            />
                          )}

                          {/* Routing */}
                          {isAdmin && (
                            <RoutedAgentsSection
                              direction="receive"
                              agents={routing?.agents ?? []}
                              gaEnabled={routing?.general_assistant_enabled ?? false}
                              onUnroute={(agentId) => void unroute(agentId, channelType)}
                          />
                        )}
                      </div>

                      {/* ── Publish column ── */}
                      <div className="rounded-sm border border-feedback-info-primary/10 bg-feedback-info-primary/[0.02] p-3 space-y-3">
                          <div className="flex items-center gap-2">
                            <ArrowUpFromLine className="h-3.5 w-3.5 text-feedback-info-primary" />
                            <span className="content-compact font-medium text-feedback-info-primary">{t_i18n("Publish")}</span>
                          </div>

                          {/* Whitelist */}
                          {isAdmin && (
                            <InlineWhitelistCard
                              direction="publish"
                              channels={existing.allowed_publish_channels}
                              platformChannels={platformChannelsMap[existing.id] ?? []}
                              isEditing={editingWhitelist?.id === existing.id && editingWhitelist.direction === "publish"}
                              editChannels={editChannels}
                              newChId={newChId}
                              newChName={newChName}
                              isSaving={updateBot.isPending}
                              loading={channelsLoadingMap[existing.id] ?? false}
                              onStartEdit={() => startEditWhitelist(existing.id, "publish", existing.allowed_publish_channels)}
                              onCancel={() => setEditingWhitelist(null)}
                              onSave={saveWhitelist}
                              onAddChannel={addEditChannel}
                              onSetChannels={setEditChannels}
                              onNewIdChange={setNewChId}
                              onNewNameChange={setNewChName}
                            />
                          )}

                          {/* Binding */}
                          {isAdmin && (
                            <RoutedAgentsSection
                              direction="publish"
                              agents={routing?.publish_agents ?? []}
                              gaEnabled={false}
                              onUnroute={(agentId) => void unroute(agentId, channelType)}
                            />
                          )}
                        </div>

                      </div>
                    </div>
                  )}

                  {/* ── Configuration form ── */}
                  {configuringProvider === ch.provider && isAdmin && (
                    <div className="border-t border-elevation-subtle-soft p-4">
                      <div className="rounded-sm bg-accent/5 p-4 space-y-4">
                        <div className="flex items-center justify-between">
                          <h4 className="content-base font-medium">{editingBotId ? t_i18n("Edit") : t_i18n("Configure")} {ch.name}</h4>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 px-2 content-compact"
                            onClick={() => setShowSecrets(!showSecrets)}
                          >
                            {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
                            {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
                          </Button>
                        </div>

                        {ch.fields.includes("api_key") && (() => {
                          const copy = channelFieldCopy(t_i18n, ch.provider, "api_key");
                          const id = channelFieldId(ch.provider, "api_key");
                          return (
                            <SettingsField id={id} label={copy.label} helper={copy.helper} info={copy.info} error={fieldError(ch, "api_key")} required={copy.required && !editingBotId}>
                              <SecretField
                                id={id}
                                aria-label={copy.label}
                                aria-describedby={settingsFieldHintId(id)}
                                aria-required={copy.required && !editingBotId ? true : undefined}
                                placeholder={editingBotId ? t_i18n("Enter new token to update...") : t_i18n("Paste bot token...")}
                                value={formFields.api_key || ""}
                                onChange={(api_key) => setFormFields({ ...formFields, api_key })}
                                stored={editingBotLocations?.api_key?.[0] ?? null}
                                hasStoredValue={Boolean(editingBot?.has_api_key)}
                              />
                            </SettingsField>
                          );
                        })()}
                        {ch.fields.includes("client_id") && (() => {
                          const idCopy = channelFieldCopy(t_i18n, ch.provider, "client_id");
                          const secretCopy = channelFieldCopy(t_i18n, ch.provider, "client_secret");
                          const idField = channelFieldId(ch.provider, "client_id");
                          const secretField = channelFieldId(ch.provider, "client_secret");
                          return (
                            <SettingsFieldGrid>
                              <SettingsField id={idField} label={idCopy.label} helper={idCopy.helper} info={idCopy.info} error={fieldError(ch, "client_id")} required={idCopy.required && !editingBotId}>
                                <Input
                                  id={idField}
                                  aria-label={idCopy.label}
                                  aria-describedby={settingsFieldHintId(idField)}
                                  aria-required={idCopy.required && !editingBotId ? "true" : undefined}
                                  type="text"
                                  placeholder={editingBotId ? t_i18n("Enter new ID to update...") : idCopy.placeholder}
                                  value={formFields.client_id || ""}
                                  onChange={(e) => setFormFields({ ...formFields, client_id: e.target.value })}
                                />
                              </SettingsField>
                              <SettingsField id={secretField} label={secretCopy.label} helper={secretCopy.helper} info={secretCopy.info} error={fieldError(ch, "client_secret")} required={secretCopy.required && !editingBotId}>
                                <SecretField
                                  id={secretField}
                                  aria-label={secretCopy.label}
                                  aria-describedby={settingsFieldHintId(secretField)}
                                  aria-required={secretCopy.required && !editingBotId ? true : undefined}
                                  placeholder={editingBotId ? t_i18n("Enter new secret to update...") : t_i18n("Paste app password...")}
                                  value={formFields.client_secret || ""}
                                  onChange={(client_secret) => setFormFields({ ...formFields, client_secret })}
                                  stored={editingBotLocations?.client_secret?.[0] ?? null}
                                  hasStoredValue={Boolean(editingBot?.has_client_secret)}
                                />
                              </SettingsField>
                            </SettingsFieldGrid>
                          );
                        })()}
                        {editingBotId && ch.fields.includes("client_id") && (
                          <p className="content-compact text-default-secondary">{t_i18n("Leave fields blank to keep current values.")}</p>
                        )}

                        {ch.extra_fields && Object.entries(ch.extra_fields).map(([key, desc]) => {
                          const copy = channelFieldCopy(t_i18n, ch.provider, key, desc);
                          const id = channelFieldId(ch.provider, key);
                          // A key this form does not know may hold a secret: masked behind Show.
                          const masked = copy.secret !== false && !showSecrets;
                          return (
                            <SettingsField key={key} id={id} label={copy.label} helper={copy.helper} info={copy.info} error={fieldError(ch, key)} required={copy.required && !editingBotId}>
                              <Input
                                id={id}
                                aria-label={copy.label}
                                aria-describedby={copy.helper ? settingsFieldHintId(id) : undefined}
                                aria-required={copy.required && !editingBotId ? "true" : undefined}
                                type={masked ? "password" : "text"}
                                placeholder={editingBotId ? undefined : copy.placeholder}
                                value={formExtra[key] || ""}
                                onChange={(e) => setFormExtra({ ...formExtra, [key]: e.target.value })}
                              />
                            </SettingsField>
                          );
                        })}
                        {editingBotId && ch.extra_fields && Object.keys(ch.extra_fields).length > 0 && (
                          <p className="content-compact text-default-secondary">
                            {t_i18n("Only the fields you fill in are updated: other stored values are kept.")}
                          </p>
                        )}

                        {ch.webhook_path && (
                          <div className="rounded-sm bg-elevation-default p-2">
                            <p className="content-compact text-default-secondary">
                              {t_i18n("Set this as your webhook URL in the platform settings:")}
                            </p>
                            <p className="content-compact font-mono-plex text-accent mt-1">
                              {baseUrl}{ch.webhook_path}
                            </p>
                          </div>
                        )}

                        <div className="flex flex-wrap justify-end gap-2">
                          <Button variant="secondary" onClick={closeConfigure}>
                            {t_i18n("Cancel")}
                          </Button>
                          <Button
                            onClick={handleSave}
                            disabled={createBot.isPending || updateBot.isPending || aiOff}
                            aria-busy={createBot.isPending || updateBot.isPending || undefined}
                          >
                            {(createBot.isPending || updateBot.isPending) ? (
                              <><Spinner size="sm" tone="inherit" />{t_i18n("Saving...")}</>
                            ) : (
                              editingBotId ? t_i18n("Update Channel") : t_i18n("Save Channel")
                            )}
                          </Button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

            {/* Security guidance */}
            {(channelBots?.length ?? 0) > 0 && (
              <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-4 space-y-2">
                <SettingsGroupTitle icon={ShieldAlert}>{t_i18n("Bot Security & Scoping")}</SettingsGroupTitle>
                <p className="content-compact text-default-secondary leading-relaxed">
                  {t_i18n("Bot integrations have ambient authority: they can access all channels they have been invited to, including private channels. Separate whitelists control which channels agents can receive messages from and publish messages to.")}
                </p>
                <ul className="content-compact text-default-secondary leading-relaxed space-y-1 ml-4 list-disc">
                  <li><strong>{t_i18n("No whitelists:")}</strong> {t_i18n("The bot can only be used by company-managed agents (admin-controlled).")}</li>
                  <li><strong>{t_i18n("Receive whitelist:")}</strong> {t_i18n("Restricts which channels agents can listen to for incoming messages.")}</li>
                  <li><strong>{t_i18n("Publish whitelist:")}</strong> {t_i18n("Restricts which channels agents can send messages to via bot API tools.")}</li>
                </ul>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Tools dialog ── */}
      {toolsDialog && (
        <ChannelToolsDialog
          bot={toolsDialog.bot}
          catalog={toolsDialog.catalog}
          onClose={() => setToolsDialog(null)}
        />
      )}
    </>
  );
}


// ---------------------------------------------------------------------------
// Channel Tools Dialog — browse + test tools for a channel bot
// ---------------------------------------------------------------------------

function ChannelToolsDialog({
  bot, catalog, onClose,
}: {
  bot: ChannelBot;
  catalog: ChannelBotCatalogItem;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: liveToolsData, isLoading } = useChannelBotTools(bot.id);
  const testTool = useTestChannelBotTool();
  const [testingToolName, setTestingToolName] = useState<string | null>(null);

  const tools = catalog.available_tools ?? [];
  const readTools = tools.filter((t) => t.tool_type === "read");
  const writeTools = tools.filter((t) => t.tool_type === "write");
  const isConnected = bot.status === "connected";

  // Build live schema map for test dialog
  const schemaMap = new Map<string, JsonSchema | null>();
  for (const lt of liveToolsData?.tools ?? []) {
    schemaMap.set(lt.name, lt.input_schema ?? null);
  }

  const testingTool = testingToolName ? tools.find((t) => t.name === testingToolName) : null;
  const logoSrc = providerLogoSrc(catalog.provider);

  /* The Read/Write split below is the one place it must NOT be read as the
     gate: a bot's reads are deliberately excluded from the derived whitelist,
     because `read_slack_bot_messages` reads people's conversations rather than
     platform data. So every row here asks unless somebody said otherwise, and
     a reader arriving from the integration table — where Read does mean
     unattended — would carry the opposite rule across. */
  const { data: gateCatalog } = useToolGateCatalog();
  const reason = useExemptReason();
  const gateReason = useGateReason();
  const gateByName = useMemo(() => {
    const map = new Map<string, ToolGateEntry>();
    for (const entry of gateCatalog?.tools ?? []) map.set(entry.tool_key, entry);
    return map;
  }, [gateCatalog?.tools]);
  /* The Approval column only while approvals are in use (an agent asks before
     running tools): otherwise every tool here runs without asking. */
  const inUse = useApprovalsInUse();
  const columns = inUse ? "grid-cols-[160px_1fr_70px_112px_50px]" : "grid-cols-[160px_1fr_70px_50px]";

  const ToolRow = ({ tool, color, isLast }: { tool: ProviderTool; color: "emerald" | "amber"; isLast: boolean }) => {
    const gate = gateByName.get(tool.name);
    /* Per bot, not deployment-wide — the one place this dialog must differ from
       the integration table. The catalogue reports a name as exempt only when
       EVERY bot providing it is switched off, because that is the honest
       summary for a list of names. Here we are looking at one bot, the gate
       keys on its id, and its own switch is on the page behind this dialog. A
       reader who turns this bot off and still reads "Asks first" concludes the
       switch is broken. */
    const gatesHere = gate ? gate.gates && bot.requires_approval : undefined;
    const why = !gate
      ? undefined
      : gatesHere
      ? gateReason(gate)
      : gate.gates
      ? t_i18n("This bot is set to run its tools without asking.")
      : reason(gate);
    return (
    <div className={cn("grid gap-3 px-4 py-2.5 items-center group", columns, !isLast ? "border-b border-elevation-subtle-soft" : "")}>
      <code className={cn("rounded-sm px-1.5 py-0.5 content-caption font-mono-plex w-fit truncate", color === "emerald" ? "bg-feedback-success-primary/10 text-feedback-success-primary" : "bg-feedback-alert-primary/10 text-feedback-warning-tertiary")}>
        {tool.name}
      </code>
      <span className="content-compact text-default-secondary leading-relaxed">{tool.description}</span>
      <Badge variant="outline" className="content-caption w-fit">
        {color === "emerald" ? <BookOpen className="h-2.5 w-2.5 mr-0.5 text-feedback-success-primary" /> : <Pencil className="h-2.5 w-2.5 mr-0.5 text-feedback-warning-primary" />}
        {tool.tool_type === "read" ? t_i18n("Read") : t_i18n("Write")}
      </Badge>
      {!inUse ? null : gate ? (
        <Tip content={`${why} ${t_i18n("Change it in Settings > Access and security > Tool approvals.")}`}>
          <Link to={toolApprovalsHref(tool.name, true)}>
            <Badge variant="outline" className="content-caption w-fit gap-0.5 hover:bg-elevation-hover">
              <HitlIcon
                state={hitlState(gatesHere)}
                className={cn("size-2.5", !gatesHere && "text-default-secondary")}
                decorative
              />
              {gatesHere ? t_i18n("Asks first") : t_i18n("Runs without asking")}
            </Badge>
          </Link>
        </Tip>
      ) : (
        <Badge variant="outline" className="content-caption w-fit gap-0.5">
          <HitlIcon state="unknown" className="size-2.5" />—
        </Badge>
      )}
      {isConnected && (
        <Button
          variant="ghost"
          size="sm"
          className="gap-1 px-1.5 opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={() => setTestingToolName(tool.name)}
        >
          {t_i18n("Test")}
        </Button>
      )}
    </div>
    );
  };

  return (
    <>
      <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
        {/* Sized to the content, not to the window. The four fixed columns,
            the gaps and the dialog's own padding take 496px, so the width sets
            what the description gets: 176px at `2xl` (four wrapped lines once
            the approval column landed), 272px at `3xl`, 400px at `4xl` — which
            is where the longest of these one-line summaries stops wrapping.
            `w-full` still shrinks it on a small screen. */}
        <DialogContent className="max-w-4xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-3">
              <img src={logoSrc} alt={catalog.name} className="h-6 w-6" />
              {catalog.name} — {t_i18n("Available Tools")}
              <Badge variant="cyan" className="content-caption ml-auto">{t_i18n("{count} tools", { count: tools.length })}</Badge>
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4 mt-2">
            {/* Info banner */}
            <div className="rounded-sm bg-elevation-hover p-3 flex items-start gap-2">
              <Info className="h-3.5 w-3.5 shrink-0 text-accent mt-0.5" />
              <p className="content-compact text-default-secondary leading-relaxed">
                {t_i18n("These tools are available to agents with this channel's publish capability enabled. Read tools retrieve information from the platform; write tools send messages, reactions, and files.")}
                {isConnected && " " + t_i18n("Hover a tool and click Test to try it with real credentials.")}
              </p>
            </div>

            {isLoading ? (
              <div className="flex items-center justify-center py-8">
                <Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
              </div>
            ) : tools.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center">
                <Wrench className="h-8 w-8 text-default-disabled mb-2" />
                <p className="content-base text-default-secondary">{t_i18n("No tools registered for this provider")}</p>
              </div>
            ) : (
              <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
                {/* Header */}
                <div className={cn("grid gap-3 px-4 py-2 bg-elevation-hover border-b border-elevation-subtle-soft", columns)}>
                  <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Tool Name")}</p>
                  <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Description")}</p>
                  <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Type")}</p>
                  {inUse && (
                    <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Approval")}</p>
                  )}
                  <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary"></p>
                </div>

                {readTools.length > 0 && (
                  <>
                    <div className="px-4 py-1.5 bg-feedback-success-primary/[0.03] border-b border-elevation-subtle-soft">
                      <div className="flex items-center gap-1.5">
                        <BookOpen className="h-3 w-3 text-feedback-success-primary" />
                        <h4 className="content-caption font-semibold uppercase tracking-wider text-feedback-success-primary">{t_i18n("Read Tools")}</h4>
                        <CountBadge variant="outline" className="ml-1">{readTools.length}</CountBadge>
                      </div>
                    </div>
                    {readTools.map((tool, idx) => (
                      <ToolRow key={tool.name} tool={tool} color="emerald" isLast={idx === readTools.length - 1 && writeTools.length === 0} />
                    ))}
                  </>
                )}

                {writeTools.length > 0 && (
                  <>
                    <div className="px-4 py-1.5 bg-feedback-alert-primary/[0.03] border-b border-elevation-subtle-soft">
                      <div className="flex items-center gap-1.5">
                        <Pencil className="h-3 w-3 text-feedback-warning-primary" />
                        <h4 className="content-caption font-semibold uppercase tracking-wider text-feedback-warning-primary">{t_i18n("Write Tools")}</h4>
                        <CountBadge variant="outline" className="ml-1">{writeTools.length}</CountBadge>
                      </div>
                    </div>
                    {writeTools.map((tool, idx) => (
                      <ToolRow key={tool.name} tool={tool} color="amber" isLast={idx === writeTools.length - 1} />
                    ))}
                  </>
                )}
              </div>
            )}
          </div>
          {/* Which agents answer or post through this bot, for whom. */}
          <ResourceSharingCard kind="channel_bot" id={bot.id} />
        </DialogContent>
      </Dialog>

      {/* Tool test dialog (nested) */}
      <ToolTestDialog
        open={testingToolName !== null}
        onOpenChange={(open) => { if (!open) setTestingToolName(null); }}
        toolName={testingToolName ?? ""}
        toolDescription={testingTool?.description}
        inputSchema={testingToolName ? (schemaMap.get(testingToolName) ?? null) : null}
        onRun={async (args) => {
          return new Promise((resolve) => {
            testTool.mutate(
              { botId: bot.id, tool_name: testingToolName ?? "", arguments: args },
              {
                onSuccess: (data) => resolve(data),
                onError: (err) => resolve({ status: "error", message: err.message }),
              },
            );
          });
        }}
      />
    </>
  );
}


// ---------------------------------------------------------------------------
// Inline Whitelist Card — edit receive or publish whitelist inline
// ---------------------------------------------------------------------------

function InlineWhitelistCard({
  direction,
  channels,
  platformChannels,
  isEditing,
  editChannels,
  newChId,
  newChName,
  isSaving,
  loading,
  onStartEdit,
  onCancel,
  onSave,
  onAddChannel,
  onSetChannels,
  onNewIdChange,
  onNewNameChange,
}: {
  direction: "receive" | "publish";
  channels: AllowedChannel[] | null;
  platformChannels: AllowedChannel[];
  isEditing: boolean;
  editChannels: AllowedChannel[];
  newChId: string;
  newChName: string;
  isSaving: boolean;
  loading?: boolean;
  onStartEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
  onAddChannel: () => void;
  onSetChannels: (channels: AllowedChannel[]) => void;
  onNewIdChange: (v: string) => void;
  onNewNameChange: (v: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const isReceive = direction === "receive";
  const hasWhitelist = (channels ?? []).length > 0;
  const fieldScope = useId();
  const botFieldPrefix = `whitelist-${fieldScope}-${direction}`;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Allowed Channels")}</p>
          {hasWhitelist && !isEditing && (
            <Badge variant="secondary" className="content-caption h-3.5 px-1">{channels?.length ?? 0}</Badge>
          )}
        </div>
        {!isEditing ? (
          <Button variant="ghost" size="sm" className="px-1.5 gap-1 shrink-0" onClick={onStartEdit}>
            <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
          </Button>
        ) : (
          <div className="flex gap-2 shrink-0">
            <Button variant="secondary" onClick={onCancel}>{t_i18n("Cancel")}</Button>
            <Button className="gap-1" onClick={onSave} disabled={isSaving} aria-busy={isSaving || undefined}>
              <Save className="h-4 w-4" /> {t_i18n("Save")}
            </Button>
          </div>
        )}
      </div>

      {isEditing ? (
        <div className="space-y-1.5">
          {/* Autocomplete channel picker */}
          <ChannelPicker
            selected={editChannels}
            available={platformChannels}
            onChange={(next) => onSetChannels(next.map(ch => ({ ...ch, group: ch.group ?? "" })))}
            variant={isReceive ? "emerald" : "blue"}
            placeholder={isReceive ? t_i18n("Search receive channels...") : t_i18n("Search publish channels...")}
            loading={loading}
          />

          {/* Manual entry fallback */}
          <div className="flex items-end gap-2">
            <SettingsField id={`${botFieldPrefix}-channel-id`} label={t_i18n("Channel ID")} className="flex-1">
              <Input
                id={`${botFieldPrefix}-channel-id`}
                aria-label={t_i18n("Channel ID")}
                value={newChId}
                onChange={(e) => onNewIdChange(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onAddChannel()}
              />
            </SettingsField>
            <SettingsField id={`${botFieldPrefix}-channel-name`} label={t_i18n("Name (optional)")} className="flex-1">
              <Input
                id={`${botFieldPrefix}-channel-name`}
                aria-label={t_i18n("Name (optional)")}
                value={newChName}
                onChange={(e) => onNewNameChange(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onAddChannel()}
              />
            </SettingsField>
            <Button variant="secondary" className="gap-1 shrink-0" onClick={onAddChannel}>
              <Plus className="h-4 w-4" /> {t_i18n("Add")}
            </Button>
          </div>
        </div>
      ) : hasWhitelist ? (
        <div className="flex flex-wrap gap-1">
          {(channels ?? []).slice(0, 8).map((ch) => (
            <span key={ch.id} className={cn(
              "content-caption rounded-full px-1.5 py-0.5 border",
              isReceive
                ? "bg-feedback-success-primary/10 text-feedback-success-primary/80 border-feedback-success-primary/20"
                : "bg-feedback-info-primary/10 text-feedback-info-primary/80 border-feedback-info-primary/20",
            )}>
              {ch.name}
            </span>
          ))}
          {(channels?.length ?? 0) > 8 && (
            <span className="content-caption text-default-secondary">{t_i18n("+{count} more", { count: (channels?.length ?? 0) - 8 })}</span>
          )}
        </div>
      ) : (
        <p className="content-caption text-feedback-warning-primary italic flex items-center gap-1">
          <ShieldAlert className="h-2.5 w-2.5 shrink-0" />
          {t_i18n("No whitelist: ALL channels are allowed. Add channels to restrict access.")}
        </p>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Routed Agents Section — shows receive routing or publish binding
// ---------------------------------------------------------------------------

function RoutedAgentsSection({
  direction,
  agents,
  gaEnabled,
  onUnroute,
}: {
  direction: "receive" | "publish";
  agents: { id: string; name: string; icon: string; is_company_managed: boolean }[];
  gaEnabled: boolean;
  onUnroute?: (agentId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const isReceive = direction === "receive";
  const hasAgents = agents.length > 0;
  const label = isReceive ? t_i18n("Routing") : t_i18n("Binding");
  const emptyMsg = isReceive ? t_i18n("No agents receive here.") : t_i18n("No agents publish here.");

  return (
    <div className="space-y-1.5">
      <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">
        {label} {(hasAgents || gaEnabled) && <span className="normal-case">— {t_i18n("{count} active", { count: agents.length + (gaEnabled ? 1 : 0) })}</span>}
      </p>
      {!hasAgents && !gaEnabled ? (
        <p className="content-caption text-default-secondary italic">{emptyMsg}</p>
      ) : (
        <div className="flex flex-wrap gap-1">
          {gaEnabled && (
            <span className="inline-flex items-center gap-1 rounded-sm bg-accent/5 border border-accent/10 px-2 py-0.5 content-caption text-default-secondary">
              <Sparkles className="h-2.5 w-2.5 text-accent shrink-0" />
              {t_i18n("General Assistant")}
            </span>
          )}
          {agents.map((a) => {
            const AgentIcon = ICON_MAP[a.icon] ?? Bot;
            return (
              <span
                key={a.id}
                className="group inline-flex items-center gap-1 rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-2 py-0.5 content-caption text-default-secondary"
              >
                <AgentIcon className="h-2.5 w-2.5 text-accent shrink-0" />
                {a.name}
                {a.is_company_managed && <Building2 className="h-2 w-2 text-feedback-warning-primary shrink-0" />}
                {onUnroute && (
                  <Tip content={t_i18n("Remove {name} from this channel", { name: a.name })}>
                    <button aria-label={t_i18n("Remove {name} from this channel", { name: a.name })}
                      onClick={() => onUnroute(a.id)}
                      className="ml-0.5 rounded-sm p-0.5 opacity-0 group-hover:opacity-100 hover:bg-feedback-error-primary/20 hover:text-feedback-error-primary transition-all"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </Tip>
                )}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Map a channel provider key (e.g. "slack_bot") to a channel type (e.g. "slack"). */
function channelTypeFromProvider(provider: string): string {
  return provider.replace(/_bot$/, "");
}
