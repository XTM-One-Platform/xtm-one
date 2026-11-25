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
 * /desktop — XTM One Desktop.
 *
 * The one place that answers "how do I get the native app, and what does it
 * let my agents do on my machine?" — reachable from the sidebar footer, the
 * user menu and the ⌘K palette, so it is never again a tab hidden under
 * Profile. Composed from the building blocks in `pages/desktop/`:
 *
 *   1. the pitch + the download (the visitor's OS first), or - when the page
 *      is opened INSIDE the Desktop app - the machine's own connection status;
 *   2. connecting a machine (enrollment key, Desktop preset by default);
 *   3. servers and headless machines (the runner daemon, command line);
 *   4. the enrolled machines (this one first, expanded).
 *
 * The Profile > Runners tab redirects here; `RUNNERS_ENABLED=false` hides the
 * whole page like it hid that tab.
 */
import { Navigate } from "react-router-dom";
import { Laptop, MessageSquare, ShieldCheck, TerminalSquare, MonitorSmartphone } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useDesktopRunner } from "@/hooks/api/useRunnerApi";
import { useDesktopBridge, useRunnersEnabled, useVisitorArm64 } from "@/hooks/useDesktop";
import { DESKTOP_PRODUCT_NAME, detectVisitorPlatform } from "@/lib/desktop";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { DownloadCard, EnrollmentKeysCard, RunnerFleet } from "@/pages/desktop/RunnersSection";
import type { DesktopBridge } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

export default function Desktop() {
  const { t_i18n } = useFormatter();
  const bridge = useDesktopBridge();
  const visitorArm64 = useVisitorArm64();
  // Runner subsystem can be disabled deployment-wide (RUNNERS_ENABLED=false):
  // the page then has nothing to offer, like the tab it replaces.
  const runnersEnabled = useRunnersEnabled();
  // Not known yet: wait, a redirect now would bounce every deep link.
  if (runnersEnabled === undefined) return <PageSkeleton cards={2} />;
  if (!runnersEnabled) return <Navigate to="/" replace />;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2.5">
            <MonitorSmartphone className="h-6 w-6 text-accent" />
            {DESKTOP_PRODUCT_NAME}
          </h1>
          <p className="content-base text-default-secondary mt-1 max-w-2xl">
            {t_i18n("Your agents in a native app - chat, history and agent selection in a real window, and an assistant that can act on the machine you are sitting at: run commands, read and edit files, take screenshots, drive your browser. Every action stays inside the machine's permission model and is audited.")}
          </p>
        </div>
        {bridge && (
          <Badge variant="cyan" className="shrink-0 gap-1.5 content-caption">
            <Laptop className="h-3 w-3" /> {t_i18n("You are in the Desktop app")} · v{bridge.version}
          </Badge>
        )}
      </div>

      {bridge ? <ThisMachineCard bridge={bridge} /> : (
        <>
          <ValueProps />
          <DownloadCard visitorPlatform={detectVisitorPlatform()} visitorArm64={visitorArm64} />
        </>
      )}

      {/* The shell's machine name pre-fills a key only while the app is NOT
          enrolled yet: once it is, a new key is for another machine. */}
      <EnrollmentKeysCard defaultName={bridge && !bridge.runner_id ? bridge.runner_name ?? undefined : undefined} />

      {/* Inside the app the download card is pointless for THIS machine, but
          the headless / other-platform installers still matter for the other
          machines you manage - keep them one card further down. */}
      {bridge && <DownloadCard />}

      <RunnerFleet highlightRunnerId={bridge?.runner_id ?? null} />
    </div>
  );
}

/** Three things the app adds over the browser tab, in the words of the person deciding to install it. */
function ValueProps() {
  const { t_i18n } = useFormatter();
  const items = [
    {
      icon: MessageSquare,
      title: t_i18n("A real window for your agents"),
      text: t_i18n("Chat, conversation history and agent selection in a native app that stays in your tray. One shortcut - Ctrl+Alt+Space - brings it back."),
    },
    {
      icon: TerminalSquare,
      title: t_i18n("An assistant that can act on your machine"),
      text: t_i18n("Run commands, read and edit files, take screenshots, drive your signed-in browser - on this machine, from the chat, with approvals inline."),
    },
    {
      icon: ShieldCheck,
      title: t_i18n("Same security posture as the runner"),
      text: t_i18n("Outbound-only connection, per-machine permission model (restricted by default), shell allow / block lists, and every command audited."),
    },
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {items.map(({ icon: Icon, title, text }) => (
        <Card key={title} className="layer-1 bg-elevation-default backdrop-blur-sm">
          <CardContent className="p-4 space-y-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
              <Icon className="h-4 w-4 text-accent" />
            </div>
            <p className="content-base font-semibold">{title}</p>
            <p className="content-compact text-default-secondary leading-relaxed">{text}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/** Inside the Desktop app: the machine the app embeds, as the platform sees it. */
function ThisMachineCard({ bridge }: { bridge: DesktopBridge }) {
  const { t_i18n, intl } = useFormatter();
  // A 404 / 403 for the shell's machine is a state to explain, never a fault.
  const { data: runner, isLoading, isError: unknownMachine } = useDesktopRunner(bridge.runner_id);
  const platformCfg = RUNNER_PLATFORM_CONFIG[bridge.platform];
  const online = runner?.status === "online";
  const revoked = runner?.status === "revoked" || unknownMachine;

  return (
    <Card className="border-accent/30 bg-accent/[0.04]">
      <CardContent className="flex flex-wrap items-center gap-4 p-4">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-sm bg-accent/10">
          <Laptop className="h-5 w-5 text-accent" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="content-base font-semibold truncate">
            {runner?.name ?? bridge.runner_name ?? t_i18n("This machine")}
            <span className="ml-2 content-compact font-normal text-default-secondary">
              {platformCfg?.label ?? bridge.platform} · {bridge.arch}
            </span>
          </p>
          <p className="content-compact text-default-secondary mt-0.5">
            {!bridge.runner_id
              ? t_i18n("This app is not enrolled yet - create an enrollment key below and paste it into its Connect screen.")
              : isLoading
                ? t_i18n("Checking the connection...")
                : unknownMachine
                  ? t_i18n("The platform does not know this machine under your account: it was removed, or it was enrolled by someone else. Re-enroll it from the app's connection panel.")
                  : revoked
                  ? t_i18n("This machine's credential was revoked. Re-enroll it from the app's connection panel.")
                  : online
                    ? t_i18n("Connected - agents in your chats can act on this machine within its permissions.")
                    : runner?.last_seen_at
                      ? t_i18n("Not connected right now (last seen {time}). The app reconnects on its own.", { time: formatTimeAgo(intl, runner.last_seen_at) })
                      : t_i18n("Not connected yet - the app connects a few seconds after it starts.")}
          </p>
        </div>
        {bridge.runner_id && (
          <span className={cn(
            "inline-flex items-center gap-1.5 rounded-sm border px-2 py-1 text-[11px] font-medium",
            revoked
              ? "border-feedback-error-primary/30 bg-feedback-error-primary/10 text-feedback-error-primary"
              : online
                ? "border-feedback-success-primary/30 bg-feedback-success-primary/10 text-feedback-success-primary"
                : "border-feedback-alert-primary/30 bg-feedback-alert-primary/10 text-feedback-warning-tertiary",
          )}>
            <span className={cn("h-1.5 w-1.5 rounded-full", revoked ? "bg-feedback-error-primary" : online ? "bg-feedback-success-primary" : "bg-feedback-alert-primary animate-pulse")} aria-hidden />
            {unknownMachine ? t_i18n("Unknown machine") : revoked ? t_i18n("Revoked") : online ? t_i18n("Connected") : t_i18n("Reconnecting")}
          </span>
        )}
      </CardContent>
    </Card>
  );
}
