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

import {
  Globe,
  Bot,
  MessageSquare,
  AlertTriangle,
  Code2,
  ExternalLink,
  ArrowLeftRight,
  Filter,
  Lock,
  Save,
  Pencil,
  UserX,
  MessageCircle,
  AtSign,
  X,
  Plus,
  Check,
  Copy,
  ShieldAlert,
  Sparkles,
  Paperclip,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SettingsSwitchField } from "@/components/ui/settings-form";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { ChannelPicker } from "@/components/ui/channel-picker";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  CHANNEL_ICONS, CHANNEL_TYPE_LABELS, CHANNEL_ROUTING_ICON_KEY,
  EXTERNAL_CHANNEL_TYPES, providerLogoSrc } from "@/lib/constants";
import type { Agent, ChannelBot, ChannelConfig, ChannelEntry, AllowedChannel } from "@/types";
import type { ToolScopeDialogConfig } from "./ToolingSection";
import { Spinner } from "@filigran/design-system";

type ChannelsSectionProps = {
  agent: Agent;
  canManage: boolean;
  isPending: boolean;
  isAdmin: boolean;
  // Web Exposure
  onWebToggle: (enabled: boolean) => void;
  onWebUploadsToggle: (enabled: boolean) => void;
  embedDialogOpen: boolean;
  setEmbedDialogOpen: (v: boolean) => void;
  snippetCopied: boolean;
  embedSnippet: string;
  onCopySnippet: () => void;
  baseUrl: string;
  // Suggested Prompts
  suggestedPrompts: string[];
  newPromptText: string;
  setNewPromptText: (v: string) => void;
  editingSuggested: boolean;
  onAddSuggestedPrompt: () => void;
  onRemoveSuggestedPrompt: (index: number) => void;
  onSaveSuggestedPrompts: () => void;
  onCancelSuggestedPrompts: () => void;
  onGenerateSuggested: () => void;
  isGeneratingSuggested: boolean;
  // Channels & Bots
  editingChannels: boolean;
  channelConfigDraft: ChannelConfig;
  onToggleChannelBot: (chType: string, botInteg: ChannelBot) => void;
  onUpdateChannelEntry: (chType: string, updates: Partial<ChannelEntry>) => void;
  availableBots: ChannelBot[];
  restrictedBots: ChannelBot[];
  channelBots: ChannelBot[] | undefined;
  platformChannelsMap: Record<string, AllowedChannel[]>;
  channelsLoadingMap: Record<string, boolean>;
  onStartEditChannels: () => void;
  onSaveChannels: () => void;
  onCancelChannels: () => void;
  // Tool scope
  onOpenToolScope: (config: ToolScopeDialogConfig) => void;
};

export function ChannelsSection({
  agent,
  canManage,
  isPending,
  isAdmin,
  onWebToggle,
  onWebUploadsToggle,
  embedDialogOpen,
  setEmbedDialogOpen,
  snippetCopied,
  embedSnippet,
  onCopySnippet,
  baseUrl,
  suggestedPrompts,
  newPromptText,
  setNewPromptText,
  editingSuggested,
  onAddSuggestedPrompt,
  onRemoveSuggestedPrompt,
  onSaveSuggestedPrompts,
  onCancelSuggestedPrompts,
  onGenerateSuggested,
  isGeneratingSuggested,
  editingChannels,
  channelConfigDraft,
  onToggleChannelBot,
  onUpdateChannelEntry,
  availableBots,
  restrictedBots,
  channelBots,
  platformChannelsMap,
  channelsLoadingMap,
  onStartEditChannels,
  onSaveChannels,
  onCancelChannels,
  onOpenToolScope,
}: ChannelsSectionProps) {
  const { t_i18n, intl } = useFormatter();
  return (
    <>
      {/* Web Exposure */}
      <Card className="border-feedback-error-primary/20">
        <CardHeader>
          <CardTitle className="content-base flex items-center gap-2">
            <Globe className="h-4 w-4 text-accent" /> {t_i18n("Web Exposure")}
            <Badge className="text-[9px] bg-feedback-error-primary/10 text-feedback-error-primary border border-feedback-error-primary/20 gap-1 ml-1"><AlertTriangle className="h-3 w-3" /> {t_i18n("Danger Zone")}</Badge>
          </CardTitle>
          <p className="content-caption text-default-secondary mt-1">{t_i18n("Expose this agent as an embeddable AI assistant chat widget on any external website. This makes the agent publicly accessible without authentication.")}</p>
        </CardHeader>
        <CardContent className="space-y-4">
          <SettingsSwitchField
            id={`agent-${agent.id}-web-enabled`}
            label={t_i18n("Expose on Web")}
            helper={t_i18n("Anyone with the embed code can chat with this agent: no login required.")}
            info={t_i18n("Turns the public chat widget of this agent on or off. On: anyone who has the embed code can chat with the agent from any website, without an account. Off: the widget does not answer.")}
            checked={agent.web_enabled}
            onCheckedChange={onWebToggle}
            disabled={!canManage || isPending}
          >
            <p className="content-compact text-feedback-warning-primary flex items-center gap-1">
              <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
              {t_i18n("The public web widget can't generate downloadable files; file generation stays available in authenticated chat.")}
            </p>
          </SettingsSwitchField>

          {agent.web_enabled && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Badge variant="cyan" className="text-[9px] gap-1"><Globe className="h-3 w-3" /> {t_i18n("Live")}</Badge>
                <span className="content-caption text-default-secondary">{t_i18n("This agent is publicly accessible via the embed widget.")}</span>
              </div>
              {/* The token comes back only to people who may manage the agent. */}
              {agent.web_token && (
                <Button size="sm" className="gap-1.5" onClick={() => setEmbedDialogOpen(true)}>
                  <Code2 className="h-3.5 w-3.5" /> {t_i18n("Get Embed Code")}
                </Button>
              )}

              {/* Visitor uploads */}
              <SettingsSwitchField
                id={`agent-${agent.id}-web-uploads`}
                className="border-t border-elevation-subtle-soft pt-4"
                label={
                  <span className="flex items-center gap-1.5">
                    <Paperclip className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" /> {t_i18n("Allow visitor uploads")}
                  </span>
                }
                helper={t_i18n("Visitors can attach documents and images to their messages; the agent reads them.")}
                info={t_i18n("Visitors can attach documents and images to their messages, and the agent reads them. Each file is limited in type and size, counted against budgets per visitor and per widget, checked by a connected prompt guard, and deleted once the conversation is over.")}
                checked={agent.web_uploads_enabled}
                onCheckedChange={onWebUploadsToggle}
                disabled={!canManage || isPending}
              >
                <p className="content-compact text-feedback-warning-primary flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
                  {t_i18n("Anyone who can open a page with the widget can send files, without an account. Leave it off unless visitors need it.")}
                </p>
              </SettingsSwitchField>

              {/* Suggested Prompts */}
              <div className="border-t border-elevation-subtle-soft pt-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="content-base font-medium flex items-center gap-1.5">
                      <MessageSquare className="h-3.5 w-3.5 text-accent" /> {t_i18n("Suggested Prompts")}
                    </p>
                    <p className="content-caption text-default-secondary mt-0.5">
                      {t_i18n("Clickable conversation starters shown in the widget greeting.")}
                    </p>
                  </div>
                  {canManage && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="gap-1.5 text-[11px] h-7"
                      onClick={onGenerateSuggested}
                      disabled={isGeneratingSuggested}
                    >
                      {isGeneratingSuggested ? (
                        <><Spinner size="sm" tone="inherit" /> {t_i18n("Generating…")}</>
                      ) : (
                        <><Sparkles className="h-3 w-3" /> {t_i18n("Generate with AI")}</>
                      )}
                    </Button>
                  )}
                </div>

                {/* Prompt list */}
                {suggestedPrompts.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {suggestedPrompts.map((prompt, i) => (
                      <div
                        key={i}
                        className="flex items-center gap-1.5 rounded-full bg-elevation-hover border border-elevation-subtle-soft px-3 py-1.5 text-[11px] text-default-secondary"
                      >
                        <span>{prompt}</span>
                        {canManage && (
                          <button aria-label={t_i18n("Remove suggested prompt")}
                            onClick={() => onRemoveSuggestedPrompt(i)}
                            className="text-default-secondary hover:text-feedback-error-primary transition-colors ml-0.5"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Add new prompt */}
                {canManage && (
                  <div className="flex gap-2">
                    <Input aria-label={t_i18n("Add a suggested prompt…")}
                      value={newPromptText}
                      onChange={(e) => setNewPromptText(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onAddSuggestedPrompt(); } }}
                      placeholder={t_i18n("Add a suggested prompt…")}
                      className="text-[12px] h-8"
                    />
                    <Button aria-label={t_i18n("Add suggested prompt")}
                      size="sm"
                      variant="outline"
                      className="h-8 px-3"
                      onClick={onAddSuggestedPrompt}
                      disabled={!newPromptText.trim()}
                    >
                      <Plus className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                )}

                {/* Save button */}
                {editingSuggested && canManage && (
                  <div className="flex gap-2">
                    <Button size="sm" className="gap-1.5" onClick={onSaveSuggestedPrompts} disabled={isPending}>
                      <Save className="h-3.5 w-3.5" /> {t_i18n("Save Prompts")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={onCancelSuggestedPrompts}>
                      {t_i18n("Cancel")}
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Embed Code Dialog */}
      <Dialog open={embedDialogOpen} onOpenChange={setEmbedDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Code2 className="h-5 w-5 text-accent" /> {t_i18n("Embed Code")}
            </DialogTitle>
            <DialogDescription>
              {intl.formatMessage(
                { id: "Add this snippet to any webpage to embed an AI assistant powered by {name}. The widget appears as a floating chat button in the bottom-right corner." },
                { name: <strong className="text-default-primary">{agent.name}</strong> },
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 mt-2">
            {/* Snippet */}
            <div className="relative group">
              <pre className="rounded-sm bg-elevation-hover p-4 text-[12px] font-mono-plex leading-relaxed text-accent overflow-x-auto select-all whitespace-pre-wrap break-all">
                {embedSnippet}
              </pre>
              <button
                className="absolute top-3 right-3 rounded-sm layer-1 bg-elevation-default px-2.5 py-1.5 content-caption font-medium text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-all flex items-center gap-1.5"
                onClick={onCopySnippet}
              >
                {snippetCopied ? <><Check className="h-3 w-3 text-feedback-success-primary" /> {t_i18n("Copied")}</> : <><Copy className="h-3 w-3" /> {t_i18n("Copy")}</>}
              </button>
            </div>

            {/* Instructions */}
            <div className="rounded-sm bg-elevation-hover p-4 space-y-3">
              <p className="content-compact font-medium text-default-primary">{t_i18n("How to use")}</p>
              <ol className="text-[11px] text-default-secondary space-y-1.5 list-decimal pl-4">
                <li>{intl.formatMessage(
                  { id: "Copy the snippet above and paste it into your website's HTML, just before the closing {tag} tag." },
                  { tag: <code className="content-caption bg-elevation-hover px-1 py-0.5 rounded-sm">&lt;/body&gt;</code> },
                )}</li>
                <li>{t_i18n("The chat widget will appear as a floating button in the bottom-right corner.")}</li>
                <li>{t_i18n("Visitors can chat with your agent: no login required.")}</li>
                <li>{t_i18n("Conversations use the agent's persona, knowledge bases, and connected tools.")}</li>
              </ol>
              <div className="border-t border-elevation-subtle-soft pt-3 space-y-1.5">
                <p className="content-compact font-medium text-default-primary">{t_i18n("Attributes")}</p>
                <ul className="text-[11px] text-default-secondary space-y-1.5">
                  <li><code className="content-caption bg-elevation-hover px-1 py-0.5 rounded-sm text-accent">data-token</code> — <span className="text-default-secondary">{t_i18n("Required. The agent's web token for authentication.")}</span></li>
                  <li><code className="content-caption bg-elevation-hover px-1 py-0.5 rounded-sm text-accent">data-context</code> — <span className="text-default-secondary">{t_i18n("Optional. A JSON object with contextual information about the embedding page (e.g. current page, product, section, user role). This context is sent with every message so the agent can tailor its responses.")}</span></li>
                </ul>
              </div>
            </div>

            {/* Preview link */}
            <div className="flex items-center gap-2 text-[11px] text-default-secondary">
              <ExternalLink className="h-3.5 w-3.5" />
              <span>{intl.formatMessage(
                { id: "Widget URL: {url}" },
                { url: <code className="text-accent content-caption">{baseUrl}/api/v1/public/widget.js</code> },
              )}</span>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Channels & Bots Configuration */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <div>
            <CardTitle className="content-base flex items-center gap-2">
              <Bot className="h-4 w-4 text-accent" /> {t_i18n("Channels & Bots")}
              {(() => { const count = Object.keys(agent.channel_config || {}).length; return count > 0 && !editingChannels ? <CountBadge variant="secondary">{count}</CountBadge> : null; })()}
              <Badge className="text-[9px] bg-feedback-error-primary/10 text-feedback-error-primary border border-feedback-error-primary/20 gap-1 ml-1"><AlertTriangle className="h-3 w-3" /> {t_i18n("Danger Zone")}</Badge>
            </CardTitle>
            <p className="content-caption text-default-secondary mt-0.5">
              {t_i18n("Configure which platform bots this agent uses and whether it can receive or send messages on each channel. Enabling channels exposes this agent to external messaging platforms.")}
            </p>
          </div>
          {!editingChannels ? (
            canManage ? <Button variant="secondary" className="gap-1.5 shrink-0" onClick={onStartEditChannels}><Pencil className="h-3 w-3" /> {t_i18n("Edit")}</Button> : null
          ) : (
            <div className="flex gap-2 shrink-0">
              <Button variant="secondary" onClick={onCancelChannels}>{t_i18n("Cancel")}</Button>
              <Button className="gap-1.5" onClick={onSaveChannels} disabled={isPending}><Save className="h-3 w-3" /> {t_i18n("Save")}</Button>
            </div>
          )}
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingChannels ? (
            <div className="space-y-3">
              <p className="content-caption text-default-secondary">{t_i18n("Enable channels below and configure receive (incoming messages) and publish (outgoing messages) capabilities independently.")}</p>
              {EXTERNAL_CHANNEL_TYPES.map((chType) => {
                const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
                const botInteg = availableBots.find((b) => b.provider === botProvider);
                const restrictedBot = restrictedBots.find((b) => b.provider === botProvider);
                const chCfg = channelConfigDraft[chType];
                const isActive = !!chCfg;
                const iconConfig = CHANNEL_ICONS[botProvider] ?? { icon: MessageSquare, bg: "bg-elevation-hover", fg: "text-default-secondary" };
                const IconComp = iconConfig.icon;
                const logoSrc = providerLogoSrc(botProvider);
                const receiveWhitelist = botInteg?.allowed_receive_channels ?? [];
                const publishWhitelist = botInteg?.allowed_publish_channels ?? [];
                const platformCh = botInteg ? (platformChannelsMap[botInteg.id] ?? []) : [];
                const availableReceiveChannels = isAdmin
                  ? (platformCh.length > 0 ? platformCh : receiveWhitelist)
                  : (receiveWhitelist.length > 0 ? receiveWhitelist : platformCh);
                const availablePublishChannels = isAdmin
                  ? (platformCh.length > 0 ? platformCh : publishWhitelist)
                  : (publishWhitelist.length > 0 ? publishWhitelist : platformCh);

                if (!botInteg && !restrictedBot) {
                  return (
                    <div key={chType} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover opacity-40 p-3">
                      <div className="flex items-center gap-3">
                        <div className={`flex h-8 w-8 items-center justify-center rounded-sm ${iconConfig.bg}`}>
                          <IconComp className={`h-3.5 w-3.5 ${iconConfig.fg}`} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium text-default-secondary">{CHANNEL_TYPE_LABELS[chType]}</p>
                          <p className="content-caption text-default-secondary">{t_i18n("No bot configured: set up in")} <span className="text-accent">{t_i18n("Settings → Integrations → Channels & Bots")}</span></p>
                        </div>
                      </div>
                    </div>
                  );
                }

                if (!botInteg && restrictedBot) {
                  return (
                    <Tip key={chType} content={t_i18n("This bot has no channel whitelist configured. Add a whitelist in Settings → Integrations → Channels & Bots to make it available for personal agents.")}>
                    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover opacity-50 cursor-not-allowed p-3">
                      <div className="flex items-center gap-3">
                        <img src={logoSrc} alt={chType} className="h-6 w-6 shrink-0 grayscale" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium text-default-secondary">{CHANNEL_TYPE_LABELS[chType]}</p>
                          <p className="content-compact text-feedback-warning-primary flex items-center gap-1 mt-0.5">
                            <ShieldAlert className="h-3 w-3 shrink-0" aria-hidden="true" />
                            {t_i18n("No whitelist: add one in Settings → Integrations → Channels & Bots")}
                          </p>
                        </div>
                        <Lock className="h-3.5 w-3.5 text-default-disabled shrink-0" />
                      </div>
                    </div>
                    </Tip>
                  );
                }

                if (!botInteg) return null;

                const hasNoWhitelist = isAdmin && receiveWhitelist.length === 0 && publishWhitelist.length === 0;

                return (
                  <div key={chType} className={cn(
                    "rounded-sm border transition-all",
                    isActive ? "border-accent/30 bg-accent/[0.03]" : "border-elevation-subtle-soft bg-elevation-hover",
                  )}>
                    {/* Channel header with enable toggle */}
                    <div className="flex items-start gap-3 p-3">
                      <img src={logoSrc} alt="" aria-hidden="true" className="h-6 w-6 shrink-0" />
                      <SettingsSwitchField
                        id={`agent-channel-${chType}-enabled`}
                        className="flex-1"
                        label={<span className={cn(isActive && "text-accent")}>{CHANNEL_TYPE_LABELS[chType]}</span>}
                        helper={t_i18n("Through {bot}", { bot: botInteg.name })}
                        info={t_i18n("On: this agent receives and publishes messages on this platform through the bot, as set below. Off: the agent is not reachable on it.")}
                        checked={isActive}
                        onCheckedChange={() => onToggleChannelBot(chType, botInteg)}
                      >
                        {hasNoWhitelist && (
                          <p className="content-compact text-feedback-warning-primary flex items-center gap-1">
                            <ShieldAlert className="h-3 w-3 shrink-0" aria-hidden="true" />
                            {t_i18n("No whitelist: all channels (admin override)")}
                          </p>
                        )}
                      </SettingsSwitchField>
                    </div>

                    {/* Per-channel config when enabled */}
                    {isActive && chCfg && (
                      <div className="px-3 pb-3 space-y-3 border-t border-elevation-subtle-soft pt-3 mx-3">
                        {/* Bypass whitelist (admin-only) */}
                        {isAdmin && (receiveWhitelist.length > 0 || publishWhitelist.length > 0) && (
                          <SettingsSwitchField
                            id={`agent-channel-${chType}-bypass-whitelist`}
                            className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/15 px-3 py-2.5"
                            label={
                              <span className="flex items-center gap-2">
                                <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" aria-hidden="true" />
                                {t_i18n("Bypass whitelist")}
                              </span>
                            }
                            helper={t_i18n("Ignore bot-level channel restrictions: receive and publish on all channels")}
                            info={t_i18n("Administrators only. On: this agent ignores the channel whitelist set on the bot and can receive and publish on every channel the bot sees. Off: the bot whitelist applies.")}
                            checked={chCfg.bypass_whitelist ?? false}
                            onCheckedChange={(v) => onUpdateChannelEntry(chType, { bypass_whitelist: v })}
                          />
                        )}
                        {/* Receive */}
                        <div className="space-y-3">
                          <SettingsSwitchField
                            id={`agent-channel-${chType}-receive`}
                            label={
                              <span className="flex items-center gap-2">
                                <MessageSquare className="h-3.5 w-3.5 shrink-0 text-feedback-success-primary" aria-hidden="true" />
                                {t_i18n("Receive")}
                              </span>
                            }
                            helper={t_i18n("The agent answers incoming messages on this platform.")}
                            info={t_i18n("On: messages sent to the bot on the channels below start a conversation with this agent. Off: the agent ignores incoming messages here and the options under it do not apply.")}
                            checked={chCfg.receive_enabled}
                            onCheckedChange={(v) => onUpdateChannelEntry(chType, { receive_enabled: v })}
                          />
                          {chCfg.receive_enabled && (availableReceiveChannels.length > 0 || (channelsLoadingMap[botInteg.id] ?? false)) && (
                            <div className="ml-5 space-y-1.5">
                              <p id={`agent-channel-${chType}-receive-channels-label`} className="content-compact text-default-secondary flex items-center gap-1">
                                <Filter className="h-3 w-3" aria-hidden="true" /> {t_i18n("Restrict to specific channels")}
                              </p>
                              <ChannelPicker
                                ariaLabelledBy={`agent-channel-${chType}-receive-channels-label`}
                                selected={chCfg.receive_channels}
                                available={availableReceiveChannels}
                                onChange={(next) => onUpdateChannelEntry(chType, { receive_channels: next })}
                                variant="emerald"
                                placeholder={t_i18n("Search receive channels...")}
                                loading={channelsLoadingMap[botInteg.id] ?? false}
                                emptyHint={
                                  !isAdmin && receiveWhitelist.length > 0
                                    ? t_i18n("Authorized: {names}", { names: receiveWhitelist.map((ch) => ch.name).join(", ") })
                                    : undefined
                                }
                              />
                            </div>
                          )}

                          {/* Receive sub-options */}
                          <div className="ml-5 space-y-3">
                            {/* Allow anonymous */}
                            <SettingsSwitchField
                              id={`agent-channel-${chType}-allow-anonymous`}
                              label={
                                <span className="flex items-center gap-2">
                                  <UserX className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" aria-hidden="true" />
                                  {t_i18n("Allow anonymous users")}
                                </span>
                              }
                              helper={
                                !botInteg.allow_anonymous
                                  ? t_i18n("Turned off on the bot: change it in Settings > Integrations > Channels.")
                                  : t_i18n("People without an account can talk to this agent.")
                              }
                              info={t_i18n("On: messages from people with no platform account are answered; their channel identity is passed as context. Off: only people with an account are answered. Available when Receive is on and the bot allows it.")}
                              checked={!chCfg.receive_enabled ? false : !botInteg.allow_anonymous ? false : (chCfg.allow_anonymous ?? true)}
                              onCheckedChange={(v) => onUpdateChannelEntry(chType, { allow_anonymous: v })}
                              disabled={!chCfg.receive_enabled || !botInteg.allow_anonymous}
                            />

                            {/* Allow private conversations */}
                            <SettingsSwitchField
                              id={`agent-channel-${chType}-allow-private`}
                              label={
                                <span className="flex items-center gap-2">
                                  <MessageCircle className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
                                  {t_i18n("Allow private conversations")}
                                </span>
                              }
                              helper={
                                !isAdmin
                                  ? t_i18n("Only an administrator can change this.")
                                  : !botInteg.allow_private
                                    ? t_i18n("Turned off on the bot: change it in Settings > Integrations > Channels.")
                                    : t_i18n("People can message this agent directly, even when its channels are restricted.")
                              }
                              info={t_i18n("On: direct messages to the bot reach this agent, whatever the channel restriction above. Off: only the listed channels reach it. Available when Receive is on, the bot allows it, and you are an administrator.")}
                              checked={!chCfg.receive_enabled ? false : !botInteg.allow_private ? false : (chCfg.allow_private ?? true)}
                              onCheckedChange={(v) => onUpdateChannelEntry(chType, { allow_private: v })}
                              disabled={!chCfg.receive_enabled || !botInteg.allow_private || !isAdmin}
                            />

                            {/* Only when mentioned */}
                            <SettingsSwitchField
                              id={`agent-channel-${chType}-only-mention`}
                              label={
                                <span className="flex items-center gap-2">
                                  <AtSign className="h-3.5 w-3.5 shrink-0 text-filigran-ia-primary" aria-hidden="true" />
                                  {t_i18n("Only when mentioned")}
                                </span>
                              }
                              helper={
                                botInteg.only_mention
                                  ? t_i18n("Turned on for every agent on the bot: change it in Settings > Integrations > Channels.")
                                  : t_i18n("In group channels, this agent answers only messages that @mention it.")
                              }
                              info={t_i18n("On: in group channels the agent answers only messages that @mention the bot; direct messages are unaffected. Off: it answers every message it receives. Available when Receive is on.")}
                              checked={!chCfg.receive_enabled ? false : botInteg.only_mention ? true : (chCfg.only_mention ?? false)}
                              onCheckedChange={(v) => onUpdateChannelEntry(chType, { only_mention: v })}
                              disabled={!chCfg.receive_enabled || botInteg.only_mention}
                            />
                          </div>
                        </div>

                        {/* Publish */}
                        <div className="space-y-3">
                          <SettingsSwitchField
                            id={`agent-channel-${chType}-publish`}
                            label={
                              <span className="flex items-center gap-2">
                                <ArrowLeftRight className="h-3.5 w-3.5 shrink-0 text-feedback-info-primary" aria-hidden="true" />
                                {t_i18n("Publish")}
                              </span>
                            }
                            helper={t_i18n("The agent can send messages on this platform through the bot.")}
                            info={t_i18n("On: the agent and its assignments can post messages through the bot on the channels below. Off: nothing is sent on this platform.")}
                            checked={chCfg.publish_enabled}
                            onCheckedChange={(v) => onUpdateChannelEntry(chType, { publish_enabled: v })}
                          />
                          {chCfg.publish_enabled && (availablePublishChannels.length > 0 || (channelsLoadingMap[botInteg.id] ?? false)) && (
                            <div className="ml-5 space-y-1.5">
                              <p id={`agent-channel-${chType}-publish-channels-label`} className="content-compact text-default-secondary flex items-center gap-1">
                                <Filter className="h-3 w-3" aria-hidden="true" /> {t_i18n("Restrict to specific channels")}
                              </p>
                              <ChannelPicker
                                ariaLabelledBy={`agent-channel-${chType}-publish-channels-label`}
                                selected={chCfg.publish_channels}
                                available={availablePublishChannels}
                                onChange={(next) => onUpdateChannelEntry(chType, { publish_channels: next })}
                                variant="blue"
                                placeholder={t_i18n("Search publish channels...")}
                                loading={channelsLoadingMap[botInteg.id] ?? false}
                                emptyHint={
                                  !isAdmin && publishWhitelist.length > 0
                                    ? t_i18n("Authorized: {names}", { names: publishWhitelist.map((ch) => ch.name).join(", ") })
                                    : undefined
                                }
                              />
                            </div>
                          )}
                        </div>

                        {/* Tool scoping */}
                        {chCfg.publish_enabled && (
                          <button
                            className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                            onClick={() => onOpenToolScope({ type: "channel-bot", id: botInteg.id, label: botInteg.name })}
                          >
                            <Filter className="h-3 w-3" />
                            {chCfg.allowed_tools
                              ? t_i18n("{count, plural, one {# bot tool} other {# bot tools}} selected", { count: chCfg.allowed_tools.length })
                              : t_i18n("All bot tools")}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
              <p className="content-caption text-default-secondary mt-1">
                {t_i18n("Platform bots are configured in")} <span className="text-accent">{t_i18n("Settings → Integrations → Channels & Bots")}</span>. {t_i18n("Separate receive and publish whitelists are defined there.")}
              </p>
            </div>
          ) : (
            /* Read-only view */
            <div>
              {Object.keys(agent.channel_config || {}).length > 0 ? (
                <div className="space-y-2">
                  {Object.entries(agent.channel_config || {}).map(([chType, chCfg]) => {
                    const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
                    const botInteg = channelBots?.find((b) => b.id === chCfg.channel_bot_id);
                    const logoSrc = providerLogoSrc(botProvider);
                    return (
                      <div key={chType} className="rounded-sm bg-elevation-hover p-3">
                        <div className="flex items-center gap-3">
                          <img src={logoSrc} alt={chType} className="h-5 w-5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="content-base font-medium">{CHANNEL_TYPE_LABELS[chType]}</p>
                            <p className="content-caption text-default-secondary">{botInteg?.name ?? t_i18n("Unknown bot")}</p>
                          </div>
                          <div className="flex items-center gap-1.5">
                            {chCfg.receive_enabled && (
                              <Badge variant="outline" className="text-[9px] text-feedback-success-primary border-feedback-success-primary/30 bg-feedback-success-primary/5 gap-1">
                                <MessageSquare className="h-2.5 w-2.5" /> {t_i18n("Receive")}{chCfg.receive_channels.length > 0 ? ` ${t_i18n("({count} ch)", { count: chCfg.receive_channels.length })}` : ""}
                              </Badge>
                            )}
                            {chCfg.publish_enabled && (
                              <Badge variant="outline" className="text-[9px] text-feedback-info-primary border-feedback-info-primary/30 bg-feedback-info-primary/5 gap-1">
                                <ArrowLeftRight className="h-2.5 w-2.5" /> {t_i18n("Publish")}{chCfg.publish_channels.length > 0 ? ` ${t_i18n("({count} ch)", { count: chCfg.publish_channels.length })}` : ""}
                              </Badge>
                            )}
                            {chCfg.allowed_tools && (
                              <Badge variant="outline" className="text-[9px] text-feedback-warning-tertiary border-feedback-alert-primary/30 bg-feedback-alert-primary/5">
                                {t_i18n("{count, plural, one {# tool} other {# tools}}", { count: chCfg.allowed_tools.length })}
                              </Badge>
                            )}
                            {/* Effective values mirror the backend resolution rules:
                                bot allow_anonymous=false is a hard off; bot only_mention=true
                                is a hard on — the agent override only applies otherwise. */}
                            {chCfg.receive_enabled && (botInteg?.allow_anonymous ? (chCfg.allow_anonymous ?? true) : false) && (
                              <Badge variant="outline" className="text-[9px] text-feedback-warning-tertiary border-feedback-alert-primary/30 bg-feedback-alert-primary/5 gap-1">
                                <UserX className="h-2.5 w-2.5" /> {t_i18n("Anonymous")}
                              </Badge>
                            )}
                            {chCfg.receive_enabled && (botInteg?.only_mention ? true : (chCfg.only_mention ?? false)) && (
                              <Badge variant="outline" className="text-[9px] text-filigran-ia-primary border-filigran-ia-primary/30 bg-filigran-ia-primary/5 gap-1">
                                <AtSign className="h-2.5 w-2.5" /> {t_i18n("Mention only")}
                              </Badge>
                            )}
                            {chCfg.receive_enabled && !(botInteg?.allow_private === false ? false : (chCfg.allow_private ?? botInteg?.allow_private ?? true)) && (
                              <Badge variant="outline" className="text-[9px] text-feedback-error-primary border-feedback-error-primary/30 bg-feedback-error-primary/5 gap-1">
                                <MessageCircle className="h-2.5 w-2.5" /> {t_i18n("No DMs")}
                              </Badge>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No channels configured")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
