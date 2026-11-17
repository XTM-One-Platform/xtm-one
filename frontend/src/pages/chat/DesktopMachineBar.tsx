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
 * The machine a chat acts on inside XTM One Desktop.
 *
 * A compact strip above the composer: the runner the app embeds (its name, its
 * platform mark, its live connection state as the PLATFORM sees it - the same
 * heartbeat-derived signal the Desktop page shows) and the per-conversation
 * switch that decides whether this conversation's turns carry
 * `desktop_runner_id`. On, the agent's `computer_use` tools default to this
 * machine; off, the turn is an ordinary web chat.
 *
 * A Desktop-only concern: the bar renders nothing on the web.
 */
import { Link } from "react-router-dom";
import { Laptop, Settings2 } from "lucide-react";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useDesktopRunner } from "@/hooks/api/useRunnerApi";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { DESKTOP_PRODUCT_NAME, DESKTOP_ROUTE } from "@/lib/desktop";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export function DesktopMachineBar({
  runnerId,
  fallbackName,
  enabled,
  onToggle,
}: {
  runnerId: string;
  /** Name the shell injected, shown until the platform's record loads. */
  fallbackName: string | null;
  enabled: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  // A 404 / 403 for the shell's machine is a state to show (the record is
  // gone, or this account may not see it), never a fault.
  const { data: runner, isError: unknownMachine } = useDesktopRunner(runnerId);
  const name = runner?.name ?? fallbackName ?? t_i18n("This machine");
  const online = runner?.status === "online";
  const revoked = runner?.status === "revoked" || unknownMachine;
  const platformCfg = runner ? RUNNER_PLATFORM_CONFIG[runner.platform] : undefined;

  const stateLabel = unknownMachine
    ? t_i18n("Unknown machine")
    : revoked
      ? t_i18n("Revoked")
      : runner
        ? online
          ? t_i18n("Connected")
          : t_i18n("Reconnecting")
        : t_i18n("Checking");

  return (
    <div
      data-testid="desktop-machine-bar"
      className={cn(
        "flex items-center gap-2.5 border-t border-elevation-subtle-soft px-4 py-1.5 content-compact",
        enabled ? "bg-accent/[0.04]" : "bg-elevation-hover",
      )}
    >
      {platformCfg ? (
        <ProviderLogo provider={runner!.platform} logo={platformCfg.logo} className="h-3.5 w-3.5 shrink-0" />
      ) : (
        <Laptop className="h-3.5 w-3.5 shrink-0 text-accent" />
      )}
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="shrink-0 text-default-secondary">{DESKTOP_PRODUCT_NAME}</span>
        <span className="text-default-secondary">&middot;</span>
        <span className="truncate font-medium text-default-primary">{name}</span>
        <span
          className={cn(
            "ml-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full",
            revoked ? "bg-feedback-error-primary" : online ? "bg-feedback-success-primary" : "bg-feedback-alert-primary animate-pulse",
          )}
          aria-hidden
        />
        <span className="shrink-0 text-default-secondary">{stateLabel}</span>
      </span>

      <span className="ml-auto flex items-center gap-2 text-default-secondary">
        <span className="hidden sm:inline">
          {enabled
            ? t_i18n("Agents can act on this machine")
            : t_i18n("Local execution off for this conversation")}
        </span>
        <Tip content={t_i18n("Machine settings, permissions and pending approvals")}>
          <Link
            to={DESKTOP_ROUTE}
            aria-label={t_i18n("Machine settings, permissions and pending approvals")}
            className="flex h-6 w-6 items-center justify-center rounded-sm text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary"
          >
            <Settings2 className="h-3.5 w-3.5" />
          </Link>
        </Tip>
        <Tip content={enabled ? t_i18n("Stop acting on this machine in this conversation") : t_i18n("Let agents act on this machine in this conversation")}>
          <Switch
            checked={enabled}
            onCheckedChange={onToggle}
            aria-label={t_i18n("Agents can act on this machine")}
            className="h-4 w-7 [&>span]:h-3 [&>span]:w-3 [&>span]:data-[state=checked]:translate-x-3"
          />
        </Tip>
      </span>
    </div>
  );
}
