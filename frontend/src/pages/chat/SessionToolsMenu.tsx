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

/**
 * The composer's Session tools menu: the wrench button opens a menu with one
 * sub-menu per kind of tool (built-in, custom tools, integrations, MCP
 * servers, knowledge bases), each listing its tools with a switch, then the
 * actions that reset, enable or disable the whole selection.
 *
 * A row toggles its tool and leaves the menu open, so several tools can be
 * switched in one go. The built-in list comes from the served registry
 * (`GET /settings/builtin-tools/groups`, `offeredInChat`): before it answers,
 * or after its read failed, the sub-menu shows `BuiltinToolsPending`, and
 * "Enable all" stays disabled, since it would enable no built-in tool and say
 * nothing (round 2, A2).
 *
 * An integration whose provider is scoped to several registered platforms
 * opens its own sub-menu, with its switch and the platform to use. Hiding an
 * integration from the menu, and showing it again, happens in the dialog the
 * Integrations sub-menu opens.
 */
import { useState, type ReactNode } from "react";
import {
  BookOpen,
  Eye,
  EyeOff,
  Filter,
  Plug,
  RotateCcw,
  Server,
  Sparkles,
  ToggleLeft,
  ToggleRight,
  Wrench,
} from "lucide-react";
import {
  Badge,
  IconButton,
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuSub,
  MenuSubContent,
  MenuSubTrigger,
  MenuTrigger,
} from "@filigran/design-system";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import type { BuiltinToolOption } from "@/hooks/useBuiltinTools";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES, inferMCPLogo, isCoordinationBuiltinTool } from "@/lib/constants";
import { sortByLabel } from "@/lib/select-options";
import { cn } from "@/lib/utils";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import type { CustomTool, Integration, KnowledgeBase, MCPServer } from "@/types";

/** The cap of the count drawn on the Session tools button: above it, "99+". */
const TOOL_COUNT_CAP = 99;

/**
 * The Session tools glyph with its count, drawn by the design system's
 * `Badge` (1.5.1+): one or two digits paint a true 20x20 circle, the capped
 * "99+" a pill, and a count of 0 draws nothing. The badge is anchored on the
 * 16 px glyph, absolute, so the 32 px button keeps its size and nothing in the
 * toolbar moves when the count changes or disappears. Its 2 px ring, in the
 * composer surface colour, separates it from the glyph; nothing on the way
 * clips its overflow. The button's own `aria-label` names the control, so the
 * badge stays out of the accessibility tree (`accessibleText={false}`).
 * `rounded-full` keeps the circle round inside `.filigran-chatbot`, whose
 * stylesheet sets `--radius-lg` (the Badge's own radius) to 8px
 * (fds-migration/LIBRARY-FEEDBACK.md F47).
 */
function ToolsGlyph({ count }: { count: number }) {
  return (
    <Badge
      content={count}
      max={TOOL_COUNT_CAP}
      tone="brand"
      accessibleText={false}
      data-testid="session-tools-count"
      data-shape={count > TOOL_COUNT_CAP ? "pill" : "circle"}
      className="rounded-full ring-2 ring-[var(--filigran-chat-input-surface,var(--bg-elevation-default))]"
    >
      <Wrench className="h-4 w-4" />
    </Badge>
  );
}

export interface SessionToolsMenuProps {
  /** Tooltip and accessible name of the wrench button. */
  label: string;
  disabled: boolean;
  /** The number of tools the session carries, drawn on the button. */
  count: number;
  /** The built-in suites offered in this chat (`offeredInChat`), concierge-only ones excluded. */
  builtinTools: BuiltinToolOption[];
  /** From `useBuiltinTools()`: the registry has answered. */
  builtinToolsReady: boolean;
  /** From `useBuiltinTools()`: the read failed and nothing is cached. */
  builtinToolsFailed: boolean;
  /** From `useBuiltinTools()`: a stored former id lights its group's row. */
  carriesBuiltin: (ids: readonly string[], suiteId: string) => boolean;
  sessionBuiltinIds: string[];
  onToggleBuiltin: (id: string) => void;
  /** Empty when the custom tools feature is off. */
  customTools: CustomTool[];
  sessionCustomToolIds: string[];
  onToggleCustomTool: (id: string) => void;
  /** Every connected integration of the user. */
  connectedIntegrations: Integration[];
  /** The integrations this chat offers: the agent's own in an agent chat, all of them otherwise. */
  chatIntegrations: Integration[];
  hiddenIntegrationIds: string[];
  sessionIntegrationIds: string[];
  onToggleIntegration: (id: string) => void;
  onHideIntegration: (id: string) => void;
  onUnhideIntegration: (id: string) => void;
  /** Selected integration id per scoped provider. */
  scopeValues: Record<string, string>;
  onScopeChange: (provider: string, integrationId: string) => void;
  mcpServers: MCPServer[];
  sessionMCPServerIds: string[];
  onToggleMCPServer: (id: string) => void;
  knowledgeBases: KnowledgeBase[];
  sessionKBIds: string[];
  onToggleKB: (id: string) => void;
  onResetDefault: () => void;
  onEnableAll: () => void;
  onDisableAll: () => void;
}

const integrationName = (integ: Integration) =>
  integ.name || INTEGRATION_PROVIDER_NAMES[integ.provider] || integ.provider;

/**
 * The switch drawn at the end of a row. Display only: the row is the control
 * (`menuitemcheckbox`), so the switch is hidden from assistive technologies
 * and never takes focus or a click. It still carries the row's name, the one
 * every switch of the app has (`tests_unit/switch-accessible-name.test.ts`).
 */
function RowSwitch({ on, label }: { on: boolean; label: string }) {
  return <Switch checked={on} aria-label={label} aria-hidden tabIndex={-1} className="pointer-events-none" />;
}

interface ToggleRowProps {
  label: string;
  icon: ReactNode;
  on: boolean;
  onToggle: () => void;
  testId: string;
  /** Shown before the switch. */
  trailing?: ReactNode;
}

/** One tool of a sub-menu: selecting it toggles the tool and keeps the menu open. */
function ToggleRow({ label, icon, on, onToggle, testId, trailing }: ToggleRowProps) {
  return (
    <MenuItem
      role="menuitemcheckbox"
      aria-checked={on}
      data-testid={testId}
      startIcon={icon}
      endIcon={
        <span className="ml-3 flex items-center gap-2">
          {trailing}
          <RowSwitch on={on} label={label} />
        </span>
      }
      onSelect={(event) => {
        event.preventDefault();
        onToggle();
      }}
    >
      <span className="block truncate">{label}</span>
    </MenuItem>
  );
}

interface SectionProps {
  label: string;
  icon: ReactNode;
  /** Tools on / tools listed, drawn after the label. */
  on: number;
  total: number;
  testId: string;
  children: ReactNode;
}

/** A kind of tool: a row of the menu that opens its list of tools. */
function Section({ label, icon, on, total, testId, children }: SectionProps) {
  return (
    <MenuSub>
      <MenuSubTrigger startIcon={icon} data-testid={`${testId}-trigger`}>
        <span className="flex items-center gap-3">
          <span className="min-w-0 flex-1 truncate">{label}</span>
          <span className="shrink-0 content-caption text-default-secondary tabular-nums">
            {on}/{total}
          </span>
        </span>
      </MenuSubTrigger>
      <MenuSubContent
        data-testid={`${testId}-menu`}
        className="max-h-[min(70vh,32rem)] min-w-[18rem] max-w-[24rem] overflow-y-auto"
      >
        {children}
      </MenuSubContent>
    </MenuSub>
  );
}

export function SessionToolsMenu({
  label,
  disabled,
  count,
  builtinTools,
  builtinToolsReady,
  builtinToolsFailed,
  carriesBuiltin,
  sessionBuiltinIds,
  onToggleBuiltin,
  customTools,
  sessionCustomToolIds,
  onToggleCustomTool,
  connectedIntegrations,
  chatIntegrations,
  hiddenIntegrationIds,
  sessionIntegrationIds,
  onToggleIntegration,
  onHideIntegration,
  onUnhideIntegration,
  scopeValues,
  onScopeChange,
  mcpServers,
  sessionMCPServerIds,
  onToggleMCPServer,
  knowledgeBases,
  sessionKBIds,
  onToggleKB,
  onResetDefault,
  onEnableAll,
  onDisableAll,
}: SessionToolsMenuProps) {
  const { t_i18n, locale } = useFormatter();
  const [visibilityDialogOpen, setVisibilityDialogOpen] = useState(false);
  const { data: platformRegistrations } = usePlatformRegistrations();

  const visibleIntegrations = chatIntegrations.filter((i) => !hiddenIntegrationIds.includes(i.id));
  const hiddenCount = chatIntegrations.length - visibleIntegrations.length;
  // The providers that take a platform: the first scope configuration seen per provider.
  const scopedConfig = (provider: string) =>
    connectedIntegrations.find((i) => i.provider === provider && i.scope_config?.resolve_from_header)?.scope_config;
  const platformsFor = (integ: Integration) => {
    const cfg = scopedConfig(integ.provider);
    if (!cfg?.resolve_from_platform || !platformRegistrations) return [];
    return platformRegistrations.filter(
      (r) =>
        r.integration_id &&
        (r.platform_identifier === integ.provider || (r.platform_identifier === "openbas" && integ.provider === "openaev")),
    );
  };

  const builtinOn = builtinTools.filter((tool) => carriesBuiltin(sessionBuiltinIds, tool.id)).length;
  const keepOpen = (action: () => void) => (event: Event) => {
    event.preventDefault();
    action();
  };

  return (
    <>
      <Menu>
        <Tip content={label} wrap="auto">
          <MenuTrigger asChild disabled={disabled}>
            <IconButton
              variant="default"
              priority="tertiary"
              aria-label={label}
              data-testid="session-tools-trigger"
              disabled={disabled}
              icon={<ToolsGlyph count={count} />}
              className={cn(
                "h-8 w-8 shrink-0 transition-all data-[state=open]:text-accent",
                count > 0 ? "text-accent hover:text-accent/80" : "text-default-secondary hover:text-default-primary",
              )}
            />
          </MenuTrigger>
        </Tip>
        <MenuContent side="top" align="start" className="min-w-[16rem]" data-testid="session-tools-menu">
          <Section
            label={t_i18n("Built-in")}
            icon={<Sparkles className="size-4" />}
            on={builtinOn}
            total={builtinTools.length}
            testId="session-tools-builtin"
          >
            {builtinToolsReady ? (
              builtinTools.map((tool) => {
                const Icon = tool.icon;
                const isCoord = isCoordinationBuiltinTool(tool.id);
                return (
                  <ToggleRow
                    key={tool.id}
                    label={tool.label}
                    icon={<Icon className={cn("size-4", isCoord && "text-filigran-ia-primary")} />}
                    on={carriesBuiltin(sessionBuiltinIds, tool.id)}
                    onToggle={() => onToggleBuiltin(tool.id)}
                    testId={`session-tool-builtin-${tool.id}`}
                  />
                );
              })
            ) : (
              <div className="px-4 py-2">
                <BuiltinToolsPending isError={builtinToolsFailed} variant="rows" count={4} testId="session-builtin-tools" />
              </div>
            )}
          </Section>

          {customTools.length > 0 && (
            <Section
              label={t_i18n("Custom Tools")}
              icon={<Wrench className="size-4" />}
              on={customTools.filter((tool) => sessionCustomToolIds.includes(tool.id)).length}
              total={customTools.length}
              testId="session-tools-custom"
            >
              {customTools.map((tool) => (
                <ToggleRow
                  key={tool.id}
                  label={tool.name}
                  icon={<Wrench className="size-4" />}
                  on={sessionCustomToolIds.includes(tool.id)}
                  onToggle={() => onToggleCustomTool(tool.id)}
                  testId={`session-tool-custom-${tool.id}`}
                />
              ))}
            </Section>
          )}

          {connectedIntegrations.length > 0 && (
            <Section
              label={t_i18n("Integrations")}
              icon={<Plug className="size-4" />}
              on={visibleIntegrations.filter((integ) => sessionIntegrationIds.includes(integ.id)).length}
              total={visibleIntegrations.length}
              testId="session-tools-integrations"
            >
              {visibleIntegrations.map((integ) => {
                const on = sessionIntegrationIds.includes(integ.id);
                const name = integrationName(integ);
                const logo = <ProviderLogo provider={integ.provider} className="size-4" />;
                const platforms = platformsFor(integ);
                if (platforms.length > 1) {
                  const scope = scopeValues[integ.provider] || "";
                  return (
                    <MenuSub key={integ.id}>
                      <MenuSubTrigger startIcon={logo} data-testid={`session-tool-integration-${integ.id}-trigger`}>
                        <span className="flex items-center gap-3">
                          <span className="min-w-0 flex-1 truncate">{name}</span>
                          <RowSwitch on={on} label={name} />
                        </span>
                      </MenuSubTrigger>
                      <MenuSubContent className="max-h-[min(70vh,32rem)] min-w-[16rem] overflow-y-auto">
                        <ToggleRow
                          label={name}
                          icon={logo}
                          on={on}
                          onToggle={() => onToggleIntegration(integ.id)}
                          testId={`session-tool-integration-${integ.id}`}
                        />
                        <MenuSeparator />
                        <MenuLabel>{t_i18n("Platform")}</MenuLabel>
                        {[
                          { value: "", title: t_i18n("All platforms") },
                          ...sortByLabel(platforms, (reg) => reg.platform_title, locale).map((reg) => ({
                            value: reg.integration_id!,
                            title: reg.platform_title,
                          })),
                        ].map((option) => (
                          <MenuItem
                            key={option.value || "all"}
                            role="menuitemradio"
                            aria-checked={scope === option.value}
                            startIcon={
                              <span aria-hidden className="flex size-4 items-center justify-center">
                                {scope === option.value && <span className="size-1.5 rounded-full bg-accent" />}
                              </span>
                            }
                            onSelect={keepOpen(() => onScopeChange(integ.provider, option.value))}
                          >
                            <span className="block truncate">{option.title}</span>
                          </MenuItem>
                        ))}
                      </MenuSubContent>
                    </MenuSub>
                  );
                }
                return (
                  <ToggleRow
                    key={integ.id}
                    label={name}
                    icon={logo}
                    on={on}
                    onToggle={() => onToggleIntegration(integ.id)}
                    testId={`session-tool-integration-${integ.id}`}
                    trailing={
                      scopedConfig(integ.provider) ? (
                        <Tip content={t_i18n("Specifically scoped")}>
                          <Filter
                            role="img"
                            aria-label={t_i18n("Specifically scoped")}
                            className="size-3.5 text-feedback-warning-primary"
                          />
                        </Tip>
                      ) : undefined
                    }
                  />
                );
              })}
              {chatIntegrations.length > 0 && (
                <>
                  {visibleIntegrations.length > 0 && <MenuSeparator />}
                  <MenuItem
                    startIcon={<EyeOff className="size-4" />}
                    data-testid="session-tools-integrations-visibility"
                    onSelect={() => setVisibilityDialogOpen(true)}
                  >
                    <span className="flex items-center gap-3">
                      <span className="min-w-0 flex-1 truncate">{t_i18n("Show or hide integrations")}</span>
                      {hiddenCount > 0 && (
                        <span className="shrink-0 content-caption text-default-secondary">
                          {t_i18n("{count} hidden", { count: hiddenCount })}
                        </span>
                      )}
                    </span>
                  </MenuItem>
                </>
              )}
            </Section>
          )}

          {mcpServers.length > 0 && (
            <Section
              label={t_i18n("MCP Servers")}
              icon={<Server className="size-4" />}
              on={mcpServers.filter((server) => sessionMCPServerIds.includes(server.id)).length}
              total={mcpServers.length}
              testId="session-tools-mcp"
            >
              {mcpServers.map((server) => {
                const logoUrl = inferMCPLogo(server.name);
                return (
                  <ToggleRow
                    key={server.id}
                    label={server.name}
                    icon={
                      logoUrl ? (
                        <img src={logoUrl} alt="" className="size-4 rounded-sm object-contain" />
                      ) : (
                        <Server className="size-4" />
                      )
                    }
                    on={sessionMCPServerIds.includes(server.id)}
                    onToggle={() => onToggleMCPServer(server.id)}
                    testId={`session-tool-mcp-${server.id}`}
                  />
                );
              })}
            </Section>
          )}

          {knowledgeBases.length > 0 && (
            <Section
              label={t_i18n("Knowledge Bases")}
              icon={<BookOpen className="size-4" />}
              on={knowledgeBases.filter((kb) => sessionKBIds.includes(kb.id)).length}
              total={knowledgeBases.length}
              testId="session-tools-kb"
            >
              {knowledgeBases.map((kb) => (
                <ToggleRow
                  key={kb.id}
                  label={kb.name}
                  icon={<BookOpen className="size-4" />}
                  on={sessionKBIds.includes(kb.id)}
                  onToggle={() => onToggleKB(kb.id)}
                  testId={`session-tool-kb-${kb.id}`}
                />
              ))}
            </Section>
          )}

          <MenuSeparator />
          <MenuItem
            startIcon={<RotateCcw className="size-4" />}
            data-testid="session-tools-reset"
            onSelect={keepOpen(onResetDefault)}
          >
            {t_i18n("Reset default")}
          </MenuItem>
          <MenuItem
            startIcon={<ToggleRight className="size-4" />}
            data-testid="session-tools-enable-all"
            disabled={!builtinToolsReady}
            aria-busy={!builtinToolsReady || undefined}
            onSelect={keepOpen(onEnableAll)}
          >
            {t_i18n("Enable all")}
          </MenuItem>
          <MenuItem
            startIcon={<ToggleLeft className="size-4" />}
            data-testid="session-tools-disable-all"
            onSelect={keepOpen(onDisableAll)}
          >
            {t_i18n("Disable all")}
          </MenuItem>
        </MenuContent>
      </Menu>

      <Dialog open={visibilityDialogOpen} onOpenChange={setVisibilityDialogOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n("Show or hide integrations")}</DialogTitle>
            <DialogDescription>
              {t_i18n("A hidden integration stays out of the session tools menu until you show it again.")}
            </DialogDescription>
          </DialogHeader>
          <div className="mt-2 space-y-2">
            {chatIntegrations.map((integ) => {
              const hidden = hiddenIntegrationIds.includes(integ.id);
              return (
                <div
                  key={integ.id}
                  className="flex items-center justify-between rounded-sm border border-elevation-subtle-soft bg-elevation-default px-3 py-2"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <ProviderLogo provider={integ.provider} className="h-4 w-4" />
                    <span className={cn("truncate content-base", hidden && "text-default-secondary")}>
                      {integrationName(integ)}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => (hidden ? onUnhideIntegration(integ.id) : onHideIntegration(integ.id))}
                    data-testid={`session-tools-integration-visibility-${integ.id}`}
                    className="flex shrink-0 items-center gap-1.5 rounded-sm border border-elevation-subtle-soft px-2 py-1 content-compact text-default-secondary hover:text-default-primary hover:border-elevation-subtle transition-all"
                  >
                    {hidden ? <Eye className="h-3 w-3" /> : <EyeOff className="h-3 w-3" />}
                    {hidden ? t_i18n("Show") : t_i18n("Hide")}
                  </button>
                </div>
              );
            })}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
