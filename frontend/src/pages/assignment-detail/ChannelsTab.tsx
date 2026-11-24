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

import { useState, useMemo } from "react";
import {
  Bot, MessageSquare, ArrowDownFromLine,
  ArrowLeftRight, Filter, CheckCircle2, AlertTriangle, UserX,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { SettingsSwitchField } from "@/components/ui/settings-form";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useChannelBots, useChannelBotChannelsMap } from "@/hooks/api/useChannelBotApi";
import { useToast } from "@/components/ui/toast";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn, pluralize } from "@/lib/utils";
import {
  CHANNEL_ROUTING_ICON_KEY, CHANNEL_TYPE_LABELS,
  EXTERNAL_CHANNEL_TYPES, providerLogoSrc } from "@/lib/constants";
import { ChannelPicker } from "@/components/ui/channel-picker";
import { useFormatter } from "@/i18n";
import type { Assignment, Agent, ChannelBot, ChannelConfig, ChannelEntry } from "@/types";
import { AssignmentToolScopeLoader } from "./AssignmentToolScopeLoader";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";

// ---------------------------------------------------------------------------
// ChannelsTab — Assignment channel/bot configuration (inherit or custom)
// ---------------------------------------------------------------------------

interface ChannelsTabProps {
  assignment: Assignment;
  agent: Agent | undefined;
  canManage: boolean;
  onUpdate: () => void;
}

export function ChannelsTab({ assignment, agent, canManage, onUpdate }: ChannelsTabProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  // The viewer's bots plus the ones the agent and this assignment are bound to
  // (an admin on a colleague's assignment sees and keeps them).
  const { data: visibleChannelBots } = useChannelBots();
  const { data: everyChannelBot } = useChannelBots({ scope: "all" });
  const allChannelBots = useMemo(
    () => withBoundEntities(visibleChannelBots, everyChannelBot, [
      ...Object.values(agent?.channel_config ?? {}).map((entry) => entry.channel_bot_id),
      ...Object.values(assignment.channel_config ?? {}).map((entry) => entry.channel_bot_id),
    ]),
    [visibleChannelBots, everyChannelBot, agent?.channel_config, assignment.channel_config],
  );

  const [editing, setEditing] = useState(false);
  const [customScope, setCustomScope] = useState(assignment.channel_config !== null);
  const [draft, setDraft] = useState<ChannelConfig>(
    assignment.channel_config ?? (agent?.channel_config ?? {}),
  );
  const [toolScopeDialog, setToolScopeDialog] = useState<{
    channelBotId: string; provider: string; label: string;
    currentAllowed: string[] | null; agentAllowed: string[] | null;
    chType: string;
  } | null>(null);

  // Resolve connected channel bots
  const connectedBots = useMemo(
    () => allChannelBots?.filter((b) => b.status === "connected") ?? [],
    [allChannelBots],
  );

  const activeBotIds = useMemo(() => {
    if (!editing || !customScope) return [];
    return connectedBots
      .filter((b) => {
        const chType = EXTERNAL_CHANNEL_TYPES.find(
          (ct) => CHANNEL_ROUTING_ICON_KEY[ct] === b.provider,
        );
        return chType && !!draft[chType];
      })
      .map((b) => b.id);
  }, [editing, customScope, connectedBots, draft]);
  const { map: platformChannelsMap, loadingMap: channelsLoadingMap } = useChannelBotChannelsMap(activeBotIds);

  // Effective agent channel config (what the assignment inherits by default)
  const agentChannelConfig = agent?.channel_config ?? {};

  const startEditing = () => {
    const agentHasChannels = Object.keys(agentChannelConfig).length > 0;
    setCustomScope(agentHasChannels && assignment.channel_config !== null);
    setDraft(assignment.channel_config ?? { ...agentChannelConfig });
    setEditing(true);
  };

  const handleSave = async () => {
    const configToSave = customScope ? draft : null;
    // A channel bot this assignment posts through reaches the agent's audience.
    if (!(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { channel_config: configToSave } }))) return;
    update.mutate(
      { id: assignment.id, channel_config: configToSave },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Channel configuration updated"), variant: "success" });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };

  const toggleChannel = (chType: string) => {
    setDraft((prev) => {
      const next = { ...prev };
      if (next[chType]) {
        delete next[chType];
      } else {
        const agentEntry = agentChannelConfig[chType];
        if (agentEntry) {
          next[chType] = { ...agentEntry };
        }
      }
      return next;
    });
  };

  const updateEntry = (chType: string, updates: Partial<ChannelEntry>) => {
    setDraft((prev) => ({
      ...prev,
      [chType]: { ...prev[chType], ...updates },
    }));
  };

  const findBot = (chType: string): ChannelBot | undefined => {
    const provider = CHANNEL_ROUTING_ICON_KEY[chType];
    return connectedBots.find((b) => b.provider === provider);
  };

  // Effective config for read-only display
  const effectiveConfig = assignment.channel_config !== null
    ? assignment.channel_config
    : agentChannelConfig;
  const isInheriting = assignment.channel_config === null;

  return (
    <>
      {consentElement}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
          <div>
            <CardTitle className="content-base flex items-center gap-2">
              <Bot className="h-4 w-4 text-accent" /> {t_i18n("Channels & Bots")}
              {(() => {
                const count = Object.keys(effectiveConfig).length;
                return count > 0 && !editing
                  ? <CountBadge variant="secondary">{count}</CountBadge>
                  : null;
              })()}
              <Badge className="text-[9px] bg-feedback-error-primary/10 text-feedback-error-primary border border-feedback-error-primary/20 gap-1 ml-1"><AlertTriangle className="h-3 w-3" /> {t_i18n("Danger Zone")}</Badge>
            </CardTitle>
            <p className="content-caption text-default-secondary mt-0.5">
              {t_i18n("Configure which channels this assignment can receive from and publish to. Enabling channels exposes this assignment to external messaging platforms.")}
              {isInheriting && !editing && ` ${t_i18n("Currently inheriting from the parent agent.")}`}
            </p>
          </div>
          <CardEditActions
            editing={editing}
            canEdit={canManage}
            aiGated
            onEdit={startEditing}
            onCancel={() => setEditing(false)}
            onSave={handleSave}
            saveDisabled={update.isPending || aiOff}
          />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editing ? (
            <div className="space-y-4">
              {/* Inherit / Custom toggle */}
              <SettingsSwitchField
                id={`assignment-${assignment.id}-channels-custom-scope`}
                className="rounded-sm bg-elevation-hover px-3 py-2.5"
                label={
                  <span className="flex items-center gap-2">
                    <ArrowDownFromLine className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
                    {t_i18n("Custom scope")}
                  </span>
                }
                helper={
                  Object.keys(agentChannelConfig).length === 0
                    ? t_i18n("Configure channels on the parent agent first.")
                    : customScope
                      ? t_i18n("Assignment has its own channel configuration")
                      : t_i18n("Inheriting channel configuration from the parent agent")
                }
                info={t_i18n("On: this assignment uses its own channels, picked below within what the parent agent allows. Off: it uses the channels of the parent agent. Unavailable when the agent has no channel.")}
                checked={customScope}
                disabled={Object.keys(agentChannelConfig).length === 0}
                onCheckedChange={(v) => {
                  setCustomScope(v);
                  if (v) {
                    setDraft({ ...agentChannelConfig });
                  } else {
                    setDraft({});
                  }
                }}
              />

              {!customScope ? (
                <div className="space-y-2">
                  <p className="content-caption text-default-secondary">
                    {Object.keys(agentChannelConfig).length === 0
                      ? t_i18n("No channels are configured on the parent agent. Configure channels on the agent first.")
                      : t_i18n('This assignment uses the channel configuration from the parent agent. Toggle "Custom scope" to restrict or modify access.')}
                  </p>
                  {Object.keys(agentChannelConfig).length === 0 ? (
                    <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 p-3">
                      <p className="content-caption text-feedback-warning-primary">{t_i18n("No channels configured on the parent agent. Configure channels on the agent first.")}</p>
                    </div>
                  ) : (
                    Object.entries(agentChannelConfig).map(([chType, chCfg]) => {
                      const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
                      const logoSrc = providerLogoSrc(botProvider);
                      return (
                        <div key={chType} className="flex items-center gap-3 rounded-sm border border-accent/20 bg-accent/5 px-3 py-2.5">
                          <img src={logoSrc} alt={chType} className="h-5 w-5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="content-compact font-medium text-accent">{t_i18n(CHANNEL_TYPE_LABELS[chType])}</p>
                            <div className="flex items-center gap-2 mt-0.5">
                              {chCfg.receive_enabled && <Badge variant="secondary" className="text-[8px] h-3.5 px-1"><MessageSquare className="h-2 w-2 mr-0.5" /> {t_i18n("Receive")}</Badge>}
                              {chCfg.publish_enabled && <Badge variant="secondary" className="text-[8px] h-3.5 px-1"><ArrowLeftRight className="h-2 w-2 mr-0.5" /> {t_i18n("Publish")}</Badge>}
                            </div>
                          </div>
                          <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" />
                        </div>
                      );
                    })
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Only channels enabled on the parent agent are available. You can further restrict which capabilities this assignment uses.")}
                  </p>
                  {Object.keys(agentChannelConfig).length === 0 ? (
                    <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                      <p className="content-caption text-feedback-warning-primary">{t_i18n("No channels configured on the parent agent. Configure channels on the agent first.")}</p>
                    </div>
                  ) : (
                    EXTERNAL_CHANNEL_TYPES.map((chType) => {
                      const agentEntry = agentChannelConfig[chType];
                      if (!agentEntry) return null;

                      const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
                      const bot = findBot(chType);
                      const logoSrc = providerLogoSrc(botProvider);
                      const chCfg = draft[chType];
                      const isActive = !!chCfg;
                      const receiveWhitelist = bot?.allowed_receive_channels ?? [];
                      const publishWhitelist = bot?.allowed_publish_channels ?? [];
                      const platformCh = bot ? (platformChannelsMap[bot.id] ?? []) : [];
                      const effectiveReceiveWhitelist = receiveWhitelist.length > 0 ? receiveWhitelist : platformCh;
                      const effectivePublishWhitelist = publishWhitelist.length > 0 ? publishWhitelist : platformCh;

                      const agentReceiveChannels = agentEntry.receive_channels ?? [];
                      const agentPublishChannels = agentEntry.publish_channels ?? [];

                      return (
                        <div key={chType} className={cn(
                          "rounded-sm border transition-all",
                          isActive ? "border-accent/30 bg-accent/[0.03]" : "border-elevation-subtle-soft bg-elevation-hover",
                        )}>
                          <div className="flex items-start gap-3 p-3">
                            <img src={logoSrc} alt="" aria-hidden="true" className="h-6 w-6 shrink-0" />
                            <SettingsSwitchField
                              id={`assignment-channel-${chType}-enabled`}
                              className="flex-1"
                              label={<span className={cn(isActive && "text-accent")}>{t_i18n(CHANNEL_TYPE_LABELS[chType])}</span>}
                              helper={t_i18n("Through {bot}", { bot: bot?.name ?? botProvider })}
                              info={t_i18n("On: this assignment can be triggered from, and publish to, this platform as set below. Off: it is not reachable on it.")}
                              checked={isActive}
                              onCheckedChange={() => toggleChannel(chType)}
                            />
                          </div>

                          {isActive && chCfg && (
                            <div className="px-3 pb-3 space-y-3 border-t border-elevation-subtle-soft pt-3 mx-3">
                              {/* Receive */}
                              <div className="space-y-3">
                                <SettingsSwitchField
                                  id={`assignment-channel-${chType}-receive`}
                                  label={
                                    <span className="flex items-center gap-2">
                                      <MessageSquare className="h-3.5 w-3.5 shrink-0 text-feedback-success-primary" aria-hidden="true" />
                                      {t_i18n("Receive")}
                                    </span>
                                  }
                                  helper={
                                    !agentEntry.receive_enabled
                                      ? t_i18n("Agent does not have receive enabled for this channel")
                                      : t_i18n("Incoming messages on this platform trigger this assignment.")
                                  }
                                  info={t_i18n("On: a message received by the bot on the channels below starts this assignment. Off: incoming messages do not trigger it. Available only when the parent agent receives on this platform.")}
                                  checked={chCfg.receive_enabled}
                                  onCheckedChange={(v) => updateEntry(chType, { receive_enabled: v })}
                                  disabled={!agentEntry.receive_enabled}
                                />
                                {chCfg.receive_enabled && agentEntry.receive_enabled && (agentReceiveChannels.length > 0 || effectiveReceiveWhitelist.length > 0 || (bot ? (channelsLoadingMap[bot.id] ?? false) : false)) && (
                                  <div className="ml-5 space-y-1.5">
                                    <p id={`assignment-channel-${chType}-receive-channels-label`} className="content-compact text-default-secondary">{t_i18n("Restrict to specific channels (within agent scope):")}</p>
                                    <ChannelPicker
                                      ariaLabelledBy={`assignment-channel-${chType}-receive-channels-label`}
                                      selected={chCfg.receive_channels ?? []}
                                      available={agentReceiveChannels.length > 0 ? agentReceiveChannels : effectiveReceiveWhitelist}
                                      onChange={(next) => updateEntry(chType, { receive_channels: next })}
                                      variant="emerald"
                                      placeholder={t_i18n("Search receive channels...")}
                                      loading={bot ? (channelsLoadingMap[bot.id] ?? false) : false}
                                    />
                                  </div>
                                )}

                                {/* Inbound gate flags (allow anonymous / allow private /
                                    only mention) are resolved at the agent + bot level —
                                    assignments never process inbound messages themselves,
                                    so per-assignment toggles for them were dead config. */}
                                {chCfg.receive_enabled && (
                                  <p className="content-compact text-default-secondary ml-5">
                                    {t_i18n("Anonymous / private / mention gates are configured on the parent agent and the channel bot.")}
                                  </p>
                                )}
                              </div>

                              {/* Publish */}
                              <div className="space-y-3">
                                <SettingsSwitchField
                                  id={`assignment-channel-${chType}-publish`}
                                  label={
                                    <span className="flex items-center gap-2">
                                      <ArrowLeftRight className="h-3.5 w-3.5 shrink-0 text-feedback-info-primary" aria-hidden="true" />
                                      {t_i18n("Publish")}
                                    </span>
                                  }
                                  helper={
                                    !agentEntry.publish_enabled
                                      ? t_i18n("Agent does not have publish enabled for this channel")
                                      : t_i18n("This assignment can send messages on this platform.")
                                  }
                                  info={t_i18n("On: this assignment can post messages through the bot on the channels below. Off: it sends nothing on this platform. Available only when the parent agent publishes on this platform.")}
                                  checked={chCfg.publish_enabled}
                                  onCheckedChange={(v) => updateEntry(chType, { publish_enabled: v })}
                                  disabled={!agentEntry.publish_enabled}
                                />
                                {chCfg.publish_enabled && agentEntry.publish_enabled && (agentPublishChannels.length > 0 || effectivePublishWhitelist.length > 0 || (bot ? (channelsLoadingMap[bot.id] ?? false) : false)) && (
                                  <div className="ml-5 space-y-1.5">
                                    <p id={`assignment-channel-${chType}-publish-channels-label`} className="content-compact text-default-secondary">{t_i18n("Restrict to specific channels (within agent scope):")}</p>
                                    <ChannelPicker
                                      ariaLabelledBy={`assignment-channel-${chType}-publish-channels-label`}
                                      selected={chCfg.publish_channels ?? []}
                                      available={agentPublishChannels.length > 0 ? agentPublishChannels : effectivePublishWhitelist}
                                      onChange={(next) => updateEntry(chType, { publish_channels: next })}
                                      variant="blue"
                                      placeholder={t_i18n("Search publish channels...")}
                                      loading={bot ? (channelsLoadingMap[bot.id] ?? false) : false}
                                    />
                                  </div>
                                )}
                              </div>

                              {/* Tool scoping */}
                              {chCfg.publish_enabled && bot && (
                                <button
                                  className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                                  onClick={() =>
                                    setToolScopeDialog({
                                      channelBotId: bot.id,
                                      provider: botProvider,
                                      label: bot.name,
                                      currentAllowed: chCfg.allowed_tools,
                                      agentAllowed: agentEntry.allowed_tools,
                                      chType,
                                    })
                                  }
                                >
                                  <Filter className="h-3 w-3" />
                                  {chCfg.allowed_tools
                                    ? t_i18n("{count} {noun} selected", { count: chCfg.allowed_tools.length, noun: t_i18n(pluralize(chCfg.allowed_tools.length, "tool")) })
                                    : t_i18n("All tools")}
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {isInheriting && (
                <div className="flex items-center gap-2 rounded-sm bg-accent/5 border border-accent/20 px-3 py-2 mb-1">
                  <ArrowDownFromLine className="h-3 w-3 text-accent shrink-0" />
                  <p className="content-caption text-accent">{t_i18n("Inheriting channel configuration from parent agent")}</p>
                </div>
              )}
              {!isInheriting && (
                <div className="flex items-center gap-2 rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 px-3 py-2 mb-1">
                  <Filter className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                  <p className="content-caption text-feedback-warning-primary">
                    {t_i18n("Custom scope: {count} {noun}", { count: Object.keys(effectiveConfig).length, noun: t_i18n(pluralize(Object.keys(effectiveConfig).length, "channel")) })}
                  </p>
                </div>
              )}

              {Object.keys(effectiveConfig).length === 0 ? (
                <div className="flex flex-col items-center justify-center py-8 text-center">
                  <Bot className="h-8 w-8 text-default-disabled mb-3" />
                  <p className="content-base text-default-secondary">{t_i18n("No channels configured")}</p>
                  <p className="content-compact text-default-disabled mt-1">
                    {isInheriting
                      ? t_i18n("No channels configured on the parent agent")
                      : t_i18n("This assignment has no channel access")}
                  </p>
                </div>
              ) : (
                Object.entries(effectiveConfig).map(([chType, chCfg]) => {
                  const botProvider = CHANNEL_ROUTING_ICON_KEY[chType];
                  const logoSrc = providerLogoSrc(botProvider);
                  return (
                    <div
                      key={chType}
                      className="flex items-center gap-3 rounded-sm bg-elevation-hover px-4 py-3"
                    >
                      <img src={logoSrc} alt={chType} className="h-5 w-5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="content-base font-medium">{t_i18n(CHANNEL_TYPE_LABELS[chType])}</p>
                        <div className="flex items-center gap-2 mt-0.5">
                          {chCfg.receive_enabled && (
                            <span className="text-[8px] text-feedback-success-primary bg-feedback-success-primary/10 rounded-sm px-1.5 py-0.5 flex items-center gap-0.5">
                              <MessageSquare className="h-2 w-2" /> {t_i18n("Receive")}{chCfg.receive_channels?.length ? ` ${t_i18n("({count} ch)", { count: chCfg.receive_channels.length })}` : ""}
                            </span>
                          )}
                          {chCfg.publish_enabled && (
                            <span className="text-[8px] text-feedback-info-primary bg-feedback-info-primary/10 rounded-sm px-1.5 py-0.5 flex items-center gap-0.5">
                              <ArrowLeftRight className="h-2 w-2" /> {t_i18n("Publish")}{chCfg.publish_channels?.length ? ` ${t_i18n("({count} ch)", { count: chCfg.publish_channels.length })}` : ""}
                            </span>
                          )}
                          {chCfg.allowed_tools && (
                            <span className="text-[8px] text-feedback-warning-tertiary bg-feedback-alert-primary/10 rounded-sm px-1.5 py-0.5">
                              {chCfg.allowed_tools.length} {t_i18n(pluralize(chCfg.allowed_tools.length, "tool"))}
                            </span>
                          )}
                          {chCfg.receive_enabled && chCfg.allow_anonymous && (
                            <span className="text-[8px] text-feedback-warning-tertiary bg-feedback-alert-primary/10 rounded-sm px-1.5 py-0.5 flex items-center gap-0.5">
                              <UserX className="h-2 w-2" /> {t_i18n("Anonymous")}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tool Scope Dialog */}
      {toolScopeDialog && (
        <AssignmentToolScopeLoader
          channelBotId={toolScopeDialog.channelBotId}
          sourceLabel={toolScopeDialog.label}
          currentAllowed={toolScopeDialog.currentAllowed}
          agentAllowed={toolScopeDialog.agentAllowed}
          onSave={(allowed) => {
            updateEntry(toolScopeDialog.chType, { allowed_tools: allowed });
            setToolScopeDialog(null);
          }}
          onClose={() => setToolScopeDialog(null)}
        />
      )}
    </>
  );
}
