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

/** The Desktop page's building blocks — XTM One Desktop downloads and
 *  the machines it (or the headless runner) enrolls.
 *
 * Three concerns, composed by `pages/Desktop.tsx`:
 *   1. Download XTM One Desktop (version-matched native installers per OS) —
 *      `DownloadCard`, plus `HeadlessInstall` for servers / VMs / MDM fleets
 *      that run the runner daemon without a desktop session.
 *   2. Enroll a machine — `EnrollmentKeysCard`: create a `renr-` key (the
 *      Desktop preset makes the machine usable from the chat at once) and
 *      paste it, with this instance's URL, into the app's Connect screen.
 *   3. Manage the machines — `RunnerFleet`: status, host facts, permission
 *      model, pending approvals, revoke / delete.
 *
 * A runner is a first-class three-tier entity, so company-managed + group
 * sharing live on each runner card (owner/admin only). The permission model is
 * the security contract the user asked for: least-privilege "Restricted" (per
 * capability; shell commands get an allow-list = standing approvals, a
 * block-list = standing refusals, and an optional human-approval gate — every
 * other capability is enable-once, never prompted) vs a deliberate, audited
 * "Full control" opt-in. Approving a queued shell command offers "always
 * allow" / "always block", which persists the decision into those lists. From
 * a Desktop chat the same approval is decided inline (`RunnerApprovalCard`).
 */

import { platformOrigin } from "@/lib/base-path";
import { useState } from "react";
import {
  MonitorCog,
  Download,
  Plus,
  Trash2,
  Copy,
  Check,
  Camera,
  MousePointerClick,
  Terminal,
  FolderOpen,
  Globe,
  Monitor,
  ShieldCheck,
  ShieldAlert,
  ChevronDown,
  ChevronRight,
  Info,
  KeyRound,
  Ban,
  Cpu,
  Clock,
  Building2,
  Square,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { TagInput } from "@/components/ui/tag-input";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useMe, useAuthConfig } from "@/hooks/api/useAuthApi";
import {
  useRunners,
  useRunnerCapabilities,
  useRunnerDownloadManifest,
  useUpdateRunner,
  useRevokeRunner,
  useDeleteRunner,
  useRunnerCommands,
  useRunnerJobs,
  useStopRunnerCommand,
  useApproveRunnerCommand,
  useRejectRunnerCommand,
  useRunnerEnrollmentKeys,
  useCreateRunnerEnrollmentKey,
  useDeleteRunnerEnrollmentKey,
} from "@/hooks/api/useRunnerApi";
import {
  RUNNER_CAPABILITY_CONFIG,
  RUNNER_CAPABILITY_ORDER,
  RUNNER_PERMISSION_MODE_CONFIG,
  RUNNER_PLATFORM_CONFIG,
  RUNNER_COMMAND_KIND_LABELS,
  COPY_FEEDBACK_DELAY,
} from "@/lib/constants";
import { cn, toggleArrayItem, formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  LOCKED_DOWN_CAPS,
  offersApprovalSwitch,
  resolveEnrollmentPermissions,
  type EnrollmentPreset,
} from "./enrollment-presets";
import { jobsUnavailableMessage, orderJobs } from "./runner-jobs";
import { clipCommandForLabel, formatActivityBytes } from "@/lib/runner-activity";
import type {
  Runner,
  RunnerCapability,
  RunnerPermissionMode,
  RunnerPlatform,
  RunnerUpdate,
} from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useViewMode } from "@/hooks/useViewMode";

const CAP_ICON: Record<string, LucideIcon> = {
  screenshot: Camera,
  computer_action: MousePointerClick,
  run_shell: Terminal,
  file_ops: FolderOpen,
  browser_control: Globe,
};

const DOWNLOAD_PLATFORMS: RunnerPlatform[] = ["windows", "macos", "linux"];

/** Re-anchor a manifest URL on the page's own origin when it targets this
 *  host. The backend derives absolute URLs from the request it sees, and
 *  behind a TLS-terminating proxy that can come out as `http://` — clicking
 *  such a link from an HTTPS page is a mixed-content download that Chromium
 *  cancels silently: the tab flashes and nothing ever lands. A
 *  path-relative href always inherits the page scheme, whatever the backend
 *  emitted. Foreign hosts pass through untouched (split-horizon deployments).
 *  `absolute` returns a full URL on the page origin instead — for command
 *  strings that run on OTHER machines, where a relative path means nothing. */
function sameOriginUrl(url: string, absolute = false): string {
  try {
    const u = new URL(url, window.location.href);
    if (u.hostname !== window.location.hostname) return url;
    const path = `${u.pathname}${u.search}`;
    return absolute ? `${window.location.origin}${path}` : path;
  } catch {
    return url;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Fleet — the machines enrolled with this account (Desktop + headless)
   ═══════════════════════════════════════════════════════════════════════════ */

/** The machine's status as one plain word, the filter value of the table (data-tables.md). */
const RUNNER_STATUS_LABEL: Record<Runner["status"], string> = {
  online: "Online",
  offline: "Offline",
  revoked: "Revoked",
};

export function RunnerFleet({
  highlightRunnerId,
}: {
  /** The machine this page is being viewed from (inside XTM One Desktop). */
  highlightRunnerId?: string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { data: runners, isLoading, isError, refetch } = useRunners();
  // The expandable cards stay the default view (permissions, approvals, jobs in place);
  // the list view is the shared records table (data tables program, slice 7d, #9378).
  const [viewMode, setViewMode] = useViewMode("desktop-runners");

  const columns: DataTableColumn<Runner>[] = [
    {
      key: "name",
      header: "Name",
      // Bounded (UX r1 B3 on #11240): a long machine name is cut with the design-system
      // tooltip instead of widening the table past the screen at 1080.
      cell: (r) => (
        <span className="flex min-w-0 max-w-[15rem] items-center gap-2">
          <ProviderLogo provider={r.platform} logo={RUNNER_PLATFORM_CONFIG[r.platform]?.logo} className="h-5 w-5 shrink-0" />
          <TruncatedText text={r.name} className="font-medium" />
          {r.id === highlightRunnerId && (
            <Badge variant="cyan" className="shrink-0 gap-0.5 content-caption"><Monitor className="h-3 w-3" aria-hidden /> {t_i18n("This machine")}</Badge>
          )}
        </span>
      ),
      sortValue: (r) => r.name.toLowerCase(),
      csv: (r) => r.name,
    },
    {
      key: "status",
      header: "Status",
      cell: (r) => (
        <span className={cn("whitespace-nowrap", r.status === "online" ? "text-feedback-success-primary" : r.status === "revoked" ? "text-feedback-error-primary" : "text-default-secondary")}>
          {t_i18n(RUNNER_STATUS_LABEL[r.status])}
        </span>
      ),
      sortValue: (r) => r.status,
      csv: (r) => r.status,
    },
    {
      key: "platform",
      header: "Platform",
      cell: (r) => <span className="whitespace-nowrap text-default-secondary">{RUNNER_PLATFORM_CONFIG[r.platform]?.label ?? r.platform}</span>,
      sortValue: (r) => r.platform,
      csv: (r) => r.platform,
      hideable: true,
    },
    {
      key: "hostname",
      header: "Hostname",
      cell: (r) => <TruncatedText text={r.hostname ?? "-"} className="max-w-[12rem] text-default-secondary" />,
      sortValue: (r) => (r.hostname ?? "").toLowerCase(),
      csv: (r) => r.hostname ?? "",
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "permission_mode",
      header: "Permission mode",
      cell: (r) => (
        <span className={cn("whitespace-nowrap", RUNNER_PERMISSION_MODE_CONFIG[r.permission_mode]?.color)}>
          {t_i18n(RUNNER_PERMISSION_MODE_CONFIG[r.permission_mode]?.label ?? r.permission_mode)}
        </span>
      ),
      sortValue: (r) => r.permission_mode,
      csv: (r) => r.permission_mode,
      hideable: true,
    },
    {
      key: "commands",
      header: "Commands",
      align: "right",
      cell: (r) => formatCompactNumber(r.command_count),
      sortValue: (r) => r.command_count,
      csv: (r) => r.command_count,
      firstSort: "desc",
      hideable: true,
      hideBelow: "4xl",
    },
    {
      key: "last_seen",
      header: "Last seen",
      // Relative time for every row, the absolute date and time in the user's locale on
      // hover (UX r1 B4 on #11240); the Status column already says online.
      cell: (r) =>
        r.last_seen_at ? (
          <Tip content={intl.formatDate(r.last_seen_at, { dateStyle: "medium", timeStyle: "short" })}>
            <time dateTime={r.last_seen_at} className="whitespace-nowrap text-default-secondary">
              {formatTimeAgo(intl, r.last_seen_at)}
            </time>
          </Tip>
        ) : (
          <span className="whitespace-nowrap text-default-secondary">{t_i18n("never connected")}</span>
        ),
      // Online machines first when sorted newest first: they are seen now.
      sortValue: (r) => (r.status === "online" ? "9999" : r.last_seen_at ?? ""),
      csv: (r) => r.last_seen_at ?? "",
      firstSort: "desc",
      hideable: true,
    },
  ];

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <MonitorCog className="h-4 w-4 text-accent" />
        <h2 className="content-base font-semibold">{t_i18n("Your machines")}</h2>
      </div>

      {!isLoading && !isError && (!runners || runners.length === 0) ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <MonitorCog className="h-10 w-10 text-default-disabled mb-3" />
            <p className="font-medium">{t_i18n("No machines enrolled yet")}</p>
            <p className="content-base text-default-secondary mt-1 max-w-md">
              {t_i18n("Install XTM One Desktop on your computer (or the headless runner on a server), create an enrollment key above, and paste it with this instance's URL into the app's Connect screen.")}
            </p>
          </CardContent>
        </Card>
      ) : (
        // One records table (dev-docs/reference/data-tables.md): GET /runners returns every
        // machine the caller sees, a bounded set, so search, filters, sort and pages run on the
        // client, state in the URL (`dr_`). The machine you sit at is pinned first.
        <DataTable<Runner>
          rows={runners ?? []}
          columns={columns}
          rowKey={(r) => r.id}
          rowHref={(r) => `/desktop/runners/${r.id}`}
          rowTestId="runner-row"
          pinFirst={highlightRunnerId ? (r) => r.id === highlightRunnerId : undefined}
          defaultSort={{ key: "name", direction: "asc" }}
          defaultPageSize={25}
          searchFields={(r) => [r.name, r.hostname, ...r.tags, ...r.labels]}
          searchPlaceholder={t_i18n("Search machines by name, hostname or tag")}
          filters={[
            {
              key: "status",
              label: "Status",
              values: (r) => [r.status],
              options: (Object.keys(RUNNER_STATUS_LABEL) as Runner["status"][]).map((value) => ({ value, label: t_i18n(RUNNER_STATUS_LABEL[value]) })),
            },
            {
              key: "platform",
              label: "Platform",
              values: (r) => [r.platform],
              options: DOWNLOAD_PLATFORMS.map((value) => ({ value, label: RUNNER_PLATFORM_CONFIG[value]?.label ?? value })),
            },
            {
              key: "permission_mode",
              label: "Permission mode",
              values: (r) => [r.permission_mode],
              options: Object.entries(RUNNER_PERMISSION_MODE_CONFIG).map(([value, config]) => ({ value, label: t_i18n(config.label) })),
            },
          ]}
          exportName="machines"
          urlKey="dr_"
          storageKey="desktop-runners"
          view={{ value: viewMode, onChange: setViewMode }}
          renderGrid={
            viewMode === "grid"
              ? (rows) => (
                  <div className="space-y-2">
                    {rows.map((r) => (
                      <RunnerCard key={r.id} runner={r} isThisMachine={r.id === highlightRunnerId} />
                    ))}
                  </div>
                )
              : undefined
          }
          isLoading={isLoading}
          isError={isError}
          errorLabel={t_i18n("The machines could not be loaded.")}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No machines enrolled yet")}
          noResultsLabel={t_i18n("No machine matches the search and filters")}
        />
      )}
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════════════
   Download card — version-matched installers per OS
   ═══════════════════════════════════════════════════════════════════════════ */

export function DownloadCard({
  visitorPlatform,
  visitorArm64 = false,
}: {
  /** The visitor's detected OS — its installer is the primary button. */
  visitorPlatform?: RunnerPlatform | null;
  /** Best-effort arm64 detection (Windows on ARM, arm64 Linux boxes). */
  visitorArm64?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: manifest, isLoading } = useRunnerDownloadManifest();
  const configured = !!manifest?.download_configured;
  const scripts = manifest?.install_scripts;

  // The primary call to action: the visitor's own OS (and architecture when
  // the release ships a native build for it), when the deployment carries the
  // installers at all. Everything else stays one click away below.
  const primaryEntry = visitorPlatform ? manifest?.platforms?.[visitorPlatform] : undefined;
  const primaryArch =
    visitorArm64 && primaryEntry && !primaryEntry.universal
      ? primaryEntry.architectures?.arm64
      : undefined;
  const primaryUrlRaw = primaryArch?.installer_url ?? primaryEntry?.installer_url ?? null;
  const primaryUrl = configured && primaryUrlRaw ? sameOriginUrl(primaryUrlRaw) : null;
  const primaryCfg = visitorPlatform ? RUNNER_PLATFORM_CONFIG[visitorPlatform] : undefined;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Download className="h-4 w-4 text-accent" />
          {t_i18n("Download XTM One Desktop")}
          {/* The badge describes the DESKTOP build you download (MAJOR.YYMMDD.SEQ),
              not the platform's own version — the two use different schemes and
              showing the platform one here made "latest version" checks in the
              app look contradictory. */}
          {(manifest?.runner_version || manifest?.version) && (
            <Badge variant="outline" className="content-caption font-mono-plex">
              v{manifest?.runner_version ?? manifest?.version}
            </Badge>
          )}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("The native app: your chat, agents and history in a real window, plus an assistant that can run commands, read and write files and take screenshots on this machine - with your approval. It connects outbound and pulls its work; no inbound port is ever opened.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        {primaryUrl && primaryCfg && (
          <a
            href={primaryUrl}
            download
            data-testid="desktop-download-primary"
            className="flex items-center gap-3 rounded-sm border border-accent/40 bg-accent/10 px-4 py-3 transition-colors hover:bg-accent/15"
          >
            <ProviderLogo provider={visitorPlatform!} logo={primaryCfg.logo} className="h-6 w-6 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="content-base font-semibold">
                {t_i18n("Download for {platform}", { platform: primaryCfg.label })}
                {primaryArch ? " (arm64)" : ""}
              </p>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Detected from this browser - other platforms below.")}
              </p>
            </div>
            <Download className="h-4 w-4 shrink-0 text-accent" />
          </a>
        )}
        <div className="grid gap-2 sm:grid-cols-3 items-start">
          {DOWNLOAD_PLATFORMS.map((p) => {
            const entry = manifest?.platforms?.[p];
            const url = entry?.installer_url ? sameOriginUrl(entry.installer_url) : null;
            const enabled = configured && !!url;
            // The flat installer_url is the x64 build. macOS ships ONE
            // universal bundle (universal: true) that already runs on Apple
            // Silicon, so only Windows / Linux get a second, ARM-native line.
            const armEntry = entry?.universal ? undefined : entry?.architectures?.arm64;
            const cfg = RUNNER_PLATFORM_CONFIG[p];
            const installerLabel =
              p === "windows"
                ? t_i18n("Windows installer (.exe)")
                : p === "macos"
                  ? t_i18n("macOS disk image (.dmg)")
                  : t_i18n("Linux AppImage");
            return (
              <div key={p} className="space-y-1">
                {/* Same-tab + `download`: the browser saves the file in place
                    with zero navigation. A target="_blank" here opened a tab
                    that flashed and closed — and with a wrong-scheme URL the
                    download was silently cancelled on top. */}
                <a
                  href={enabled ? url! : undefined}
                  download
                  aria-disabled={!enabled}
                  onClick={(e) => {
                    if (!enabled) e.preventDefault();
                  }}
                  className={cn(
                    "flex items-center gap-2.5 rounded-sm border px-3 py-2.5 transition-colors",
                    enabled
                      ? "border-elevation-subtle hover:border-accent/40 hover:bg-accent/5 cursor-pointer"
                      : "border-dashed border-elevation-subtle-soft opacity-60 cursor-not-allowed",
                  )}
                >
                  <ProviderLogo provider={p} logo={cfg?.logo} className="h-5 w-5 shrink-0" />
                  <div className="min-w-0">
                    <p className="content-compact font-medium">{cfg?.label ?? p}</p>
                    <p className="content-caption text-default-secondary">
                      {enabled ? installerLabel : t_i18n("Coming soon")}
                    </p>
                  </div>
                  {enabled && <Download className="h-3.5 w-3.5 text-default-secondary ml-auto shrink-0" />}
                </a>
                {/* Linux alternate package formats (.deb / .rpm) */}
                {p === "linux" && enabled && entry?.formats && (
                  <div className="flex items-center gap-1.5 pl-1">
                    <span className="content-caption text-default-secondary">{t_i18n("or")}</span>
                    {(["deb", "rpm"] as const).map((fmt) => {
                      const fmtUrl = entry.formats?.[fmt] ?? null;
                      if (!fmtUrl) return null;
                      return (
                        <a
                          key={fmt}
                          href={sameOriginUrl(fmtUrl)}
                          download
                          className="rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption font-mono-plex text-default-secondary hover:border-accent/40 hover:text-accent transition-colors"
                        >
                          .{fmt}
                        </a>
                      );
                    })}
                  </div>
                )}
                {/* ARM-native build (Windows on ARM, arm64 Linux boxes). The
                    x64 installer above would not run on those machines. */}
                {enabled && armEntry?.installer_url && (
                  <div className="flex items-center gap-1.5 pl-1">
                    <span className="content-caption text-default-secondary">arm64</span>
                    <a
                      href={sameOriginUrl(armEntry.installer_url)}
                      download
                      className="rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption font-mono-plex text-default-secondary hover:border-accent/40 hover:text-accent transition-colors"
                    >
                      {p === "windows" ? ".exe" : ".AppImage"}
                    </a>
                    {p === "linux" &&
                      (["deb", "rpm"] as const).map((fmt) => {
                        const fmtUrl = armEntry.formats?.[fmt] ?? null;
                        if (!fmtUrl) return null;
                        return (
                          <a
                            key={fmt}
                            href={sameOriginUrl(fmtUrl)}
                            download
                            className="rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption font-mono-plex text-default-secondary hover:border-accent/40 hover:text-accent transition-colors"
                          >
                            .{fmt}
                          </a>
                        );
                      })}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <p className="text-[11px] text-default-secondary leading-relaxed">
          {t_i18n("Install it (next, next, finish - no command line), sign in, and paste an enrollment key with this instance's URL on the Connect screen. macOS asks for Screen Recording and Accessibility permission the first time an agent captures the screen or controls input.")}
        </p>

        {/* Headless / CLI install — servers, VMs, MDM (no GUI needed) */}
        {(scripts?.sh || scripts?.ps1) && <HeadlessInstall scripts={scripts} />}

        {!isLoading && !configured && (
          <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 p-3 flex items-start gap-2">
            <Info className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
            <p className="content-caption text-feedback-warning-primary leading-relaxed">
              {t_i18n("This deployment was built without embedded XTM One Desktop installers (source/dev build). Release images build and embed them automatically, so every instance serves its own version-matched downloads. You can still enroll a build you have built or obtained separately.")}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}


/* ── Headless / command-line install ────────────────────────────────────────
   For machines without a desktop session (servers, VMs, MDM fleets): one
   copy-paste command downloads the version-matched binary from THIS instance,
   verifies its SHA-256, installs it, enrolls, and sets up autostart
   (LaunchAgent / systemd --user / logon task). The headless runner is the same
   binary as XTM One Desktop without the window and the tray - shell, files
   and browser control on a server, no webview stack to install. Rendered only
   when this deployment actually serves the scripts, so the commands can never
   404. */

function HeadlessInstall({ scripts }: { scripts: { sh: string | null; ps1: string | null } }) {
  const { t_i18n } = useFormatter();
  const { data: authConfig } = useAuthConfig();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<"sh" | "ps1" | null>(null);

  const baseUrl = (authConfig?.base_url || platformOrigin()).replace(/\/+$/, "");
  const keyPlaceholder = "renr-XXXXXXXX";

  // Absolute on purpose (the command runs on ANOTHER machine), but re-anchored
  // on the page origin so a backend behind a TLS proxy can never hand out an
  // http:// command for an https:// instance.
  const shUrl = scripts.sh ? sameOriginUrl(scripts.sh, true) : null;
  const ps1Url = scripts.ps1 ? sameOriginUrl(scripts.ps1, true) : null;

  const shCmd = shUrl
    ? `curl -fsSL ${shUrl} | bash -s -- --server ${baseUrl} --key ${keyPlaceholder}`
    : null;
  const ps1Cmd = ps1Url
    ? `iwr ${ps1Url} -OutFile xtm-one-runner-installer.ps1; powershell -ExecutionPolicy Bypass -File .\\xtm-one-runner-installer.ps1 -Server ${baseUrl} -Key ${keyPlaceholder}`
    : null;

  const copy = (text: string, which: "sh" | "ps1") => {
    navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(null), COPY_FEEDBACK_DELAY);
  };

  const blocks: { id: "sh" | "ps1"; label: string; cmd: string }[] = [];
  if (shCmd) blocks.push({ id: "sh", label: t_i18n("macOS / Linux"), cmd: shCmd });
  if (ps1Cmd) blocks.push({ id: "ps1", label: t_i18n("Windows (PowerShell)"), cmd: ps1Cmd });

  return (
    <div className="rounded-sm border border-elevation-subtle-soft">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-default-secondary shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-default-secondary shrink-0" />
        )}
        <Terminal className="h-3.5 w-3.5 text-accent shrink-0" />
        <span className="content-compact font-medium">{t_i18n("Servers and headless machines (command line)")}</span>
        <span className="content-caption text-default-secondary ml-1 hidden sm:inline">
          {t_i18n("servers, VMs, MDM - the runner daemon without a window or a graphical session")}
        </span>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3">
          <p className="text-[11px] text-default-secondary leading-relaxed">
            {t_i18n("Create an enrollment key below, replace {placeholder} in the command, and run it on the target machine. It downloads this instance's version-matched binary, verifies its SHA-256, enrolls, and installs autostart (LaunchAgent on macOS, systemd --user on Linux, logon task on Windows). Use --no-autostart / -NoAutostart to skip autostart, or omit --server/--key to install only.", { placeholder: keyPlaceholder })}
          </p>
          {blocks.map((b) => (
            <div key={b.id} className="space-y-1">
              <label className="content-caption uppercase tracking-wider text-default-secondary font-medium">
                {b.label}
              </label>
              <div className="flex items-start gap-2">
                <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-caption text-accent font-mono-plex break-all select-all leading-relaxed">
                  {b.cmd}
                </code>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 shrink-0"
                  onClick={() => copy(b.cmd, b.id)}
                >
                  {copied === b.id ? (
                    <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </Button>
              </div>
            </div>
          ))}
          <p className="content-caption text-default-secondary leading-relaxed">
            {t_i18n("The same binary manages itself afterwards: xtm-one-runner status, run, reset. Screenshots and input control need a logged-in desktop session; shell, files, and browser control work on fully headless servers.")}
          </p>
        </div>
      )}
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════════════
   Enrollment keys card
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * What a new key stamps onto the machine it enrolls. The Desktop preset is the
 * A machine you install XTM One Desktop on is usable from
 * the chat at once - every capability - instead of "cannot do anything until
 * you enable some". Shell approval is a switch the person turns on, never
 * implied by a preset. Locked down keeps the historical default
 * (screenshots + browser, nothing else, no approval gate); Custom exposes
 * every control.
 */
export function EnrollmentKeysCard({
  defaultName,
}: {
  /** Pre-filled key name (the machine's name when creating from the Desktop app). */
  defaultName?: string;
} = {}) {
  const { t_i18n, intl } = useFormatter();
  const { data: user } = useMe();
  const { data: authConfig } = useAuthConfig();
  const { data: keys } = useRunnerEnrollmentKeys();
  const { data: caps } = useRunnerCapabilities();
  const createKey = useCreateRunnerEnrollmentKey();
  const deleteKey = useDeleteRunnerEnrollmentKey();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const isAdmin = user?.role === "admin";
  const baseUrl = authConfig?.base_url || platformOrigin();

  const availableCaps = (caps?.capabilities ?? RUNNER_CAPABILITY_ORDER) as RunnerCapability[];

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState(defaultName ?? "");
  const [preset, setPreset] = useState<EnrollmentPreset>("desktop");
  const [reusable, setReusable] = useState(false);
  const [mode, setMode] = useState<RunnerPermissionMode>("restricted");
  const [selectedCaps, setSelectedCaps] = useState<RunnerCapability[]>(LOCKED_DOWN_CAPS);
  const [requireApproval, setRequireApproval] = useState(false);
  const [companyManaged, setCompanyManaged] = useState(false);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState<"key" | "url" | null>(null);

  const resetForm = () => {
    setName(defaultName ?? "");
    setPreset("desktop");
    setReusable(false);
    setMode("restricted");
    setSelectedCaps(LOCKED_DOWN_CAPS);
    setRequireApproval(false);
    setCompanyManaged(false);
  };

  const resolved = resolveEnrollmentPermissions(preset, { availableCaps, mode, selectedCaps, requireApproval });

  const PRESETS: { id: EnrollmentPreset; label: string; hint: string; icon: LucideIcon }[] = [
    {
      id: "desktop",
      label: t_i18n("Desktop (recommended)"),
      hint: t_i18n("Every capability; turn on shell approval below to confirm each command."),
      icon: Monitor,
    },
    {
      id: "locked",
      label: t_i18n("Locked down"),
      hint: t_i18n("Screenshots and browser only; enable more later."),
      icon: ShieldCheck,
    },
    {
      id: "custom",
      label: t_i18n("Custom"),
      hint: t_i18n("Choose the mode, capabilities and approval gate."),
      icon: Cpu,
    },
  ];

  const handleCreate = () => {
    if (!name.trim()) return;
    createKey.mutate(
      {
        name: name.trim(),
        max_uses: reusable ? null : 1,
        default_permission_mode: resolved.mode,
        default_capabilities: resolved.caps,
        default_require_approval: resolved.approval,
        enroll_as_company_managed: isAdmin ? companyManaged : false,
      },
      {
        onSuccess: (data) => {
          setCreatedKey(data.key);
          setShowForm(false);
          resetForm();
          toast({ title: t_i18n("Enrollment key created"), variant: "success" });
        },
        onError: (e) => toast({ title: e.message || t_i18n("Failed to create key"), variant: "error" }),
      },
    );
  };

  const copy = (text: string, which: "key" | "url") => {
    navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(null), COPY_FEEDBACK_DELAY);
  };

  const handleDelete = async (id: string, keyName: string) => {
    const ok = await confirm({
      title: t_i18n("Revoke enrollment key"),
      description: t_i18n('This will permanently revoke "{name}". Machines already enrolled with it keep working; new enrollments with this key will be refused.', { name: keyName }),
      confirmLabel: t_i18n("Revoke"),
      variant: "danger",
    });
    if (!ok) return;
    deleteKey.mutate(id, {
      onSuccess: () => toast({ title: t_i18n("Enrollment key revoked"), variant: "success" }),
    });
  };

  return (
    <Card>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="content-base flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-accent" />
            {t_i18n("Connect a machine")}
          </CardTitle>
          {!showForm && (
            <Button size="sm" className="h-7 gap-1.5" onClick={() => { setShowForm(true); setCreatedKey(null); }}>
              <Plus className="h-3 w-3" /> {t_i18n("New enrollment key")}
            </Button>
          )}
        </div>
        <CardDescription className="content-compact">
          {t_i18n("Create a single-use key (or a reusable fleet key), then paste it - with this instance's URL - into the Connect screen of XTM One Desktop or of the headless runner. The app exchanges the key for a durable credential stored in the OS keychain.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        {/* Freshly created key (shown once) — paste into the installed app */}
        {createdKey && (
          <div className="rounded-sm bg-feedback-success-primary/5 border border-feedback-success-primary/20 p-3 space-y-3">
            <p className="content-compact font-medium text-feedback-success-primary">
              {t_i18n("Key created - it is shown only once. Open XTM One Desktop (or run the headless install command) and paste these into the Connect screen.")}
            </p>
            <div className="space-y-1.5">
              <label className="content-caption uppercase tracking-wider text-default-secondary font-medium">
                {t_i18n("XTM One instance URL")}
              </label>
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px] text-accent font-mono-plex break-all select-all">
                  {baseUrl}
                </code>
                <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={() => copy(baseUrl, "url")}>
                  {copied === "url" ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </div>
            </div>
            <div className="space-y-1.5">
              <label className="content-caption uppercase tracking-wider text-default-secondary font-medium">
                {t_i18n("Enrollment key")}
              </label>
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px] text-accent font-mono-plex break-all select-all">
                  {createdKey}
                </code>
                <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={() => copy(createdKey, "key")}>
                  {copied === "key" ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </div>
              <p className="content-caption text-default-secondary">
                {t_i18n("The app exchanges this for a durable credential stored in the OS keychain, then connects automatically.")}
              </p>
            </div>
            <Button variant="ghost" size="sm" className="content-caption h-6 px-2" onClick={() => setCreatedKey(null)}>
              {t_i18n("Dismiss")}
            </Button>
          </div>
        )}

        {/* Create form */}
        {showForm && (
          <div className="rounded-sm bg-elevation-hover p-4 space-y-4">
            <div className="space-y-1">
              <Label htmlFor="runner-key-name">{t_i18n("Key name")}</Label>
              <Input id="runner-key-name" aria-label={t_i18n("Key name")}
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleCreate()}
                placeholder={t_i18n("e.g. My laptop, Audit VM, Sales laptops")}
                autoFocus
                className="h-8 content-base"
              />
            </div>

            {/* Preset: what the enrolled machine may do, in one choice */}
            <div className="space-y-1.5">
              <Label id="runner-key-preset-label">{t_i18n("Permissions of the enrolled machine")}</Label>
              <div className="grid gap-2 sm:grid-cols-3" role="radiogroup" aria-labelledby="runner-key-preset-label">
                {PRESETS.map((p) => {
                  const Icon = p.icon;
                  const on = preset === p.id;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      data-testid={`enrollment-preset-${p.id}`}
                      onClick={() => setPreset(p.id)}
                      className={cn(
                        "flex items-start gap-2 rounded-sm border px-3 py-2 text-left transition-colors",
                        on ? "border-accent/40 bg-accent/10" : "border-elevation-subtle hover:border-elevation-subtle-soft",
                      )}
                    >
                      <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", on ? "text-accent" : "text-default-secondary")} />
                      <span className="min-w-0">
                        <span className={cn("block content-compact font-medium", on ? "text-accent" : "text-default-primary")}>{p.label}</span>
                        <span className="block content-caption leading-snug text-default-secondary">{p.hint}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            {preset === "custom" && (
              <>
                <div className="space-y-1 sm:max-w-xs">
                  <Label id="runner-key-default-mode-label">{t_i18n("Default permission mode")}</Label>
                  <Select value={mode} onValueChange={(v) => setMode(v as RunnerPermissionMode)}>
                    <SelectTrigger className="w-full" aria-labelledby="runner-key-default-mode-label">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(caps?.permission_modes ?? ["restricted", "full_control"]).map((m) => (
                        <SelectItem key={m} value={m}>
                          {t_i18n(RUNNER_PERMISSION_MODE_CONFIG[m]?.label ?? m)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                {/* Capabilities (restricted only) */}
                {mode === "restricted" ? (
                  <div className="space-y-1.5">
                    <Label id="runner-key-default-caps-label">{t_i18n("Default capabilities")}</Label>
                    <div className="flex flex-wrap gap-1.5" role="group" aria-labelledby="runner-key-default-caps-label">
                      {availableCaps.map((c) => {
                        const cfg = RUNNER_CAPABILITY_CONFIG[c];
                        const Icon = CAP_ICON[c] ?? ShieldCheck;
                        const on = selectedCaps.includes(c);
                        return (
                          <button
                            key={c}
                            type="button"
                            onClick={() => setSelectedCaps(toggleArrayItem(selectedCaps, c) as RunnerCapability[])}
                            className={cn(
                              "inline-flex items-center gap-1.5 rounded-sm border px-2 py-1 text-[11px] transition-colors",
                              on
                                ? "border-accent/40 bg-accent/10 text-accent"
                                : "border-elevation-subtle text-default-secondary hover:border-elevation-subtle-soft",
                            )}
                          >
                            <Icon className="h-3 w-3" />
                            {t_i18n(cfg?.label ?? c)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ) : (
                  <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 px-3 py-2 flex items-start gap-2">
                    <ShieldAlert className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
                    <p className="content-caption text-feedback-warning-primary leading-relaxed">
                      {t_i18n("Full control grants every capability and every command with no per-operation approval. Every command is still audited. Use only on machines you fully trust for automation.")}
                    </p>
                  </div>
                )}
              </>
            )}

            {offersApprovalSwitch(preset, mode) && (
              <div className="flex items-center gap-2">
                <Switch aria-label={t_i18n("Shell commands need approval")} checked={requireApproval} onCheckedChange={setRequireApproval} />
                <div>
                  <p className="content-compact font-medium">{t_i18n("Shell commands need approval")}</p>
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Held until you approve them - inline in a Desktop chat, or here on this page.")}
                  </p>
                </div>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              <div className="flex items-center gap-2">
                <Switch aria-label={t_i18n("Reusable fleet key")} checked={reusable} onCheckedChange={setReusable} />
                <div>
                  <p className="content-compact font-medium">{t_i18n("Reusable fleet key")}</p>
                  <p className="content-caption text-default-secondary">
                    {reusable ? t_i18n("Enroll many machines with one key.") : t_i18n("Single-use: expires after one machine enrolls.")}
                  </p>
                </div>
              </div>

              {isAdmin && (
                <div className="flex items-center gap-2">
                  <Switch aria-label={t_i18n("Company-managed fleet")} checked={companyManaged} onCheckedChange={setCompanyManaged} />
                  <div>
                    <p className="content-compact font-medium flex items-center gap-1">
                      <Building2 className="h-3 w-3 text-accent" /> {t_i18n("Company-managed fleet")}
                    </p>
                    <p className="content-caption text-default-secondary">
                      {t_i18n("Enrolled machines are shared with the whole company.")}
                    </p>
                  </div>
                </div>
              )}
            </div>

            <div className="flex gap-2 pt-1">
              <Button size="sm" className="h-8 gap-1.5" onClick={handleCreate} disabled={!name.trim() || createKey.isPending} aria-busy={createKey.isPending || undefined}>
                {createKey.isPending && <Spinner size="sm" tone="inherit" />}{!createKey.isPending && <Plus className="h-3.5 w-3.5" />}
                {t_i18n("Create key")}
              </Button>
              <Button variant="ghost" size="sm" className="h-8" onClick={() => { setShowForm(false); resetForm(); }}>
                {t_i18n("Cancel")}
              </Button>
            </div>
          </div>
        )}

        {/* Existing keys */}
        {keys && keys.length > 0 && (
          <div className="space-y-1.5">
            {keys.map((k) => (
              <div key={k.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                <KeyRound className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="content-compact font-medium truncate">{k.name}</p>
                    {k.enroll_as_company_managed && (
                      <Badge variant="secondary" className="text-[9px] gap-0.5"><Building2 className="h-2.5 w-2.5" /> {t_i18n("Fleet")}</Badge>
                    )}
                    {k.default_require_approval && (
                      <Tip content={t_i18n("Shell commands on machines enrolled with this key wait for your approval.")}>
                        <Badge variant="outline" className="text-[9px] gap-0.5"><ShieldCheck className="h-2.5 w-2.5" /> {t_i18n("Approval")}</Badge>
                      </Tip>
                    )}
                    {!k.is_active && <Badge variant="outline" className="text-[9px]">{t_i18n("Exhausted")}</Badge>}
                  </div>
                  <p className="content-caption text-default-secondary font-mono-plex">{k.key_prefix}</p>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <Tip content={t_i18n("Uses")}>
                    <span className="content-caption text-default-secondary">
                      {k.max_uses == null
                        ? t_i18n("{count} used - reusable", { count: k.use_count })
                        : t_i18n("{used}/{max} used", { used: k.use_count, max: k.max_uses })}
                    </span>
                  </Tip>
                  {k.expires_at && (
                    <span className="content-caption text-default-secondary">
                      {t_i18n("expires {time}", { time: formatTimeAgo(intl, k.expires_at) })}
                    </span>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t_i18n("Delete key")}
                  className="h-7 w-7 shrink-0 text-default-secondary hover:text-feedback-error-primary"
                  onClick={() => handleDelete(k.id, k.name)}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}


/* ═══════════════════════════════════════════════════════════════════════════
   Runner card — status, host facts, permission model editor
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * One machine: status, host facts, permission model, pending approvals and
 * the manage actions `can_manage` allows. Shared by the Desktop fleet list and
 * the `/desktop/runners/:id` detail page - the page the admin Objects browser
 * opens a colleague's machine on - so the two surfaces cannot drift.
 */
export function RunnerCard({
  runner,
  isThisMachine = false,
  defaultExpanded = isThisMachine,
  onDeleted,
}: {
  runner: Runner;
  /** True for the machine the page is being viewed from (inside XTM One Desktop). */
  isThisMachine?: boolean;
  /** Open with the details shown (the detail page; the machine you sit at). */
  defaultExpanded?: boolean;
  /** Called once the machine is deleted - the detail page leaves its dead route. */
  onDeleted?: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { data: user } = useMe();
  const { data: caps } = useRunnerCapabilities();
  const updateRunner = useUpdateRunner();
  const revokeRunner = useRevokeRunner();
  const deleteRunner = useDeleteRunner();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  // The machine you are sitting at opens expanded: its permissions and its
  // pending approvals are what you came for.
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [editing, setEditing] = useState(false);

  const isAdmin = user?.role === "admin";
  const canManage = runner.can_manage;

  // Editable draft (initialised from the runner when entering edit mode).
  const [draft, setDraft] = useState<RunnerUpdate>({});
  const startEditing = () => {
    setDraft({
      name: runner.name,
      permission_mode: runner.permission_mode,
      capabilities: [...runner.capabilities],
      require_approval: runner.require_approval,
      allowed_shell_commands: runner.allowed_shell_commands ?? [],
      blocked_shell_commands: runner.blocked_shell_commands ?? [],
      allowed_paths: runner.allowed_paths ?? [],
      labels: [...runner.labels],
      tags: [...runner.tags],
      is_company_managed: runner.is_company_managed,
      shared_group_ids: runner.shared_group_ids ?? undefined,
      shared_group_permissions: runner.shared_group_permissions ?? undefined,
    });
    setEditing(true);
    setExpanded(true);
  };

  const patch = (p: Partial<RunnerUpdate>) => setDraft((d) => ({ ...d, ...p }));

  const availableCaps = (caps?.capabilities ?? RUNNER_CAPABILITY_ORDER) as RunnerCapability[];
  const mode = (draft.permission_mode ?? runner.permission_mode) as RunnerPermissionMode;
  const draftCaps = draft.capabilities ?? runner.capabilities;

  const handleSave = () => {
    updateRunner.mutate(
      { id: runner.id, body: draft },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Runner updated"), variant: "success" });
          setEditing(false);
        },
        onError: (e) => toast({ title: e.message || t_i18n("Failed to update runner"), variant: "error" }),
      },
    );
  };

  const handleRevoke = async () => {
    const ok = await confirm({
      title: t_i18n("Revoke runner"),
      description: t_i18n('This immediately invalidates "{name}"\'s credential: it can no longer connect or run commands. This cannot be undone; the machine must be re-enrolled.', { name: runner.name }),
      confirmLabel: t_i18n("Revoke"),
      variant: "danger",
    });
    if (!ok) return;
    revokeRunner.mutate(runner.id, {
      onSuccess: () => toast({ title: t_i18n("Runner revoked"), variant: "success" }),
    });
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: t_i18n("Delete runner"),
      description: t_i18n('This permanently deletes "{name}" and its command history. This cannot be undone.', { name: runner.name }),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    deleteRunner.mutate(runner.id, {
      onSuccess: () => {
        toast({ title: t_i18n("Runner deleted"), variant: "success" });
        onDeleted?.();
      },
    });
  };

  const statusColor =
    runner.status === "online" ? "text-feedback-success-primary" : runner.status === "revoked" ? "text-feedback-error-primary" : "text-default-secondary";
  const facts = runner.host_facts ?? {};
  const platformLabel = RUNNER_PLATFORM_CONFIG[runner.platform]?.label ?? runner.platform;

  return (
    <Card>
      {ConfirmEl}
      {/* Header row */}
      <CardHeader className="py-3 cursor-pointer" onClick={() => setExpanded((v) => !v)}>
        <div className="flex items-center gap-3">
          {expanded ? <ChevronDown className="h-4 w-4 text-default-secondary shrink-0" /> : <ChevronRight className="h-4 w-4 text-default-secondary shrink-0" />}
          <div className="relative shrink-0">
            <ProviderLogo
              provider={runner.platform}
              logo={RUNNER_PLATFORM_CONFIG[runner.platform]?.logo}
              className="h-5 w-5"
            />
            <span
              className={cn(
                "absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full ring-2 ring-[var(--bg-elevation-default)]",
                runner.status === "online" ? "bg-feedback-success-primary" : runner.status === "revoked" ? "bg-feedback-error-primary" : "bg-[var(--text-default-secondary)]",
              )}
            />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <CardTitle className="content-base truncate">{runner.name}</CardTitle>
              {isThisMachine && (
                <Badge variant="cyan" className="text-[9px] gap-0.5"><Monitor className="h-2.5 w-2.5" /> {t_i18n("This machine")}</Badge>
              )}
              {runner.is_company_managed && (
                <Badge variant="secondary" className="text-[9px] gap-0.5"><Building2 className="h-2.5 w-2.5" /> {t_i18n("Company")}</Badge>
              )}
              {runner.permission_mode === "full_control" && (
                <Badge variant="outline" className="text-[9px] gap-0.5 text-feedback-warning-primary border-feedback-alert-primary/30"><ShieldAlert className="h-2.5 w-2.5" /> {t_i18n("Full control")}</Badge>
              )}
            </div>
            <p className="text-[11px] text-default-secondary truncate">
              {platformLabel}
              {runner.hostname ? ` · ${runner.hostname}` : ""}
              {" · "}
              <span className={statusColor}>
                {runner.status === "online"
                  ? t_i18n("online")
                  : runner.status === "revoked"
                    ? t_i18n("revoked")
                    : runner.last_seen_at
                      ? t_i18n("last seen {time}", { time: formatTimeAgo(intl, runner.last_seen_at) })
                      : t_i18n("never connected")}
              </span>
            </p>
          </div>
          {/* Labels preview */}
          <div className="hidden sm:flex items-center gap-1 shrink-0">
            {runner.labels.slice(0, 3).map((l) => (
              <Badge key={l} variant="outline" className="text-[9px]">{l}</Badge>
            ))}
          </div>
        </div>
      </CardHeader>

      {expanded && (
        <CardContent className="pt-0 space-y-4">
          {/* Host facts */}
          <HostFacts facts={facts} agentVersion={runner.agent_version} commandCount={runner.command_count} />

          {/* Permission model */}
          {!editing ? (
            <div className="space-y-3">
              <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
                <div className="flex items-center gap-2">
                  {runner.permission_mode === "full_control" ? (
                    <ShieldAlert className="h-4 w-4 text-feedback-warning-primary" />
                  ) : (
                    <ShieldCheck className="h-4 w-4 text-feedback-success-primary" />
                  )}
                  <span className="content-compact font-medium">
                    {t_i18n(RUNNER_PERMISSION_MODE_CONFIG[runner.permission_mode]?.label ?? runner.permission_mode)}
                  </span>
                  {runner.permission_mode === "restricted" && runner.require_approval && (
                    <Badge variant="outline" className="text-[9px] gap-0.5"><Clock className="h-2.5 w-2.5" /> {t_i18n("Shell approval")}</Badge>
                  )}
                </div>
                {runner.permission_mode === "restricted" && (
                  <div className="flex flex-wrap gap-1.5">
                    {runner.capabilities.length === 0 ? (
                      <span className="text-[11px] text-default-secondary italic">{t_i18n("No capabilities enabled: this runner cannot do anything until you enable some.")}</span>
                    ) : (
                      runner.capabilities.map((c) => {
                        const Icon = CAP_ICON[c] ?? ShieldCheck;
                        return (
                          <Badge key={c} variant="outline" className="content-caption gap-1">
                            <Icon className="h-2.5 w-2.5" /> {t_i18n(RUNNER_CAPABILITY_CONFIG[c]?.label ?? c)}
                          </Badge>
                        );
                      })
                    )}
                  </div>
                )}
                {runner.permission_mode === "restricted" && !!runner.allowed_shell_commands?.length && (
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Shell allow-list:")} <span className="font-mono-plex">{runner.allowed_shell_commands.join(", ")}</span>
                  </p>
                )}
                {/* The block-list reads in the error tone at 4.5:1 or better on this hover box in both
                    themes: `error-tertiary`; `error-primary` falls to 4.08:1 on the light hover grey and
                    4.26:1 on the dark larger-view surface. */}
                {runner.permission_mode === "restricted" && !!runner.blocked_shell_commands?.length && (
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Shell block-list:")} <span className="font-mono-plex text-feedback-error-tertiary" data-shell-block-list data-tone="error">{runner.blocked_shell_commands.join(", ")}</span>
                  </p>
                )}
                {runner.permission_mode === "restricted" && !!runner.allowed_paths?.length && (
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Path allow-list:")} <span className="font-mono-plex">{runner.allowed_paths.join(", ")}</span>
                  </p>
                )}
              </div>

              {canManage && (
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="secondary" className="h-7 gap-1.5" onClick={startEditing}>
                    <ShieldCheck className="h-3.5 w-3.5" /> {t_i18n("Edit permissions")}
                  </Button>
                  {runner.is_active && (
                    <Button size="sm" variant="ghost" className="h-7 gap-1.5 text-feedback-warning-primary hover:text-feedback-warning-primary" onClick={handleRevoke} disabled={revokeRunner.isPending}>
                      <Ban className="h-3.5 w-3.5" /> {t_i18n("Revoke")}
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" className="h-7 gap-1.5 text-default-secondary hover:text-feedback-error-primary" onClick={handleDelete} disabled={deleteRunner.isPending}>
                    <Trash2 className="h-3.5 w-3.5" /> {t_i18n("Delete")}
                  </Button>
                </div>
              )}
            </div>
          ) : (
            /* ── Editor ── */
            <div className="space-y-4">
              <div className="space-y-1">
                <Label htmlFor="runner-editor-name">{t_i18n("Name")}</Label>
                <Input id="runner-editor-name" aria-label={t_i18n("Name")} value={draft.name ?? ""} onChange={(e) => patch({ name: e.target.value })} className="h-8 content-base max-w-sm" />
              </div>

              {/* Permission mode */}
              <div className="space-y-1.5">
                <Label id="runner-editor-mode-label">{t_i18n("Permission mode")}</Label>
                <div className="grid gap-2 sm:grid-cols-2" role="group" aria-labelledby="runner-editor-mode-label">
                  {(caps?.permission_modes ?? ["restricted", "full_control"]).map((m) => {
                    const cfg = RUNNER_PERMISSION_MODE_CONFIG[m];
                    const on = mode === m;
                    return (
                      <button
                        key={m}
                        type="button"
                        onClick={() => patch({ permission_mode: m as RunnerPermissionMode })}
                        className={cn(
                          "text-left rounded-sm border p-2.5 transition-colors",
                          on ? "border-accent/40 bg-accent/5" : "border-elevation-subtle hover:border-elevation-subtle-soft",
                        )}
                      >
                        <div className="flex items-center gap-1.5">
                          {m === "full_control" ? <ShieldAlert className="h-3.5 w-3.5 text-feedback-warning-primary" /> : <ShieldCheck className="h-3.5 w-3.5 text-feedback-success-primary" />}
                          <span className="content-compact font-medium">{t_i18n(cfg?.label ?? m)}</span>
                        </div>
                        <p className="content-caption text-default-secondary mt-1 leading-relaxed">{t_i18n(cfg?.description ?? "")}</p>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Restricted-only scoping */}
              {mode === "restricted" && (
                <>
                  <div className="space-y-1.5">
                    <Label id="runner-editor-caps-label">{t_i18n("Capabilities")}</Label>
                    <div className="space-y-1.5" role="group" aria-labelledby="runner-editor-caps-label">
                      {availableCaps.map((c) => {
                        const cfg = RUNNER_CAPABILITY_CONFIG[c];
                        const Icon = CAP_ICON[c] ?? ShieldCheck;
                        const on = draftCaps.includes(c);
                        return (
                          <div
                            key={c}
                            className={cn(
                              "flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 transition-colors",
                              on ? "bg-accent/5 ring-1 ring-accent/20" : "hover:bg-elevation-hover",
                            )}
                          >
                            <Icon className="h-3.5 w-3.5 text-accent shrink-0" />
                            <div className="min-w-0 flex-1">
                              <p className="content-compact font-medium">{t_i18n(cfg?.label ?? c)}</p>
                              <p className="content-caption text-default-secondary truncate">{t_i18n(cfg?.description ?? "")}</p>
                            </div>
                            <Switch aria-label={t_i18n(cfg?.label ?? c)}
                              checked={on}
                              onCheckedChange={() => patch({ capabilities: toggleArrayItem(draftCaps, c) })}
                            />
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  {/* Human approval — SHELL COMMANDS ONLY. Every other
                      capability is an enable-once toggle: switching it on IS
                      the consent, nothing else ever queues for approval. */}
                  {draftCaps.includes("run_shell") && (
                    <div className="flex items-center gap-2.5 rounded-sm bg-elevation-hover px-2.5 py-2">
                      <Clock className="h-3.5 w-3.5 text-accent shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="content-compact font-medium">{t_i18n("Require approval for shell commands")}</p>
                        <p className="content-caption text-default-secondary">
                          {t_i18n("Shell commands wait for your approval before they run. Allow-listed commands run immediately, block-listed commands are always refused. Other enabled capabilities never require approval.")}
                        </p>
                      </div>
                      <Switch aria-label={t_i18n("Require approval for shell commands")}
                        checked={!!draft.require_approval}
                        onCheckedChange={(v) => patch({ require_approval: v })}
                      />
                    </div>
                  )}

                  {/* Shell allow/block lists (only meaningful if run_shell enabled) */}
                  {draftCaps.includes("run_shell") && (
                    <div className="space-y-1">
                      <Label id="runner-allowed-shell-label">{t_i18n("Allowed shell commands")}</Label>
                      <ListInput
                        aria-labelledby="runner-allowed-shell-label"
                        value={draft.allowed_shell_commands ?? []}
                        onChange={(v) => patch({ allowed_shell_commands: v })}
                        placeholder={t_i18n("e.g. git status, npm run")}
                      />
                      <p className="content-caption text-default-secondary">
                        {t_i18n("Standing approvals: a command matching one of these prefixes (or exactly one of these strings) runs immediately, without approval. With approval off, only these commands can run at all. Empty = no allow-list.")}
                      </p>
                    </div>
                  )}
                  {draftCaps.includes("run_shell") && (
                    <div className="space-y-1">
                      <Label id="runner-blocked-shell-label">{t_i18n("Blocked shell commands")}</Label>
                      <ListInput
                        aria-labelledby="runner-blocked-shell-label"
                        value={draft.blocked_shell_commands ?? []}
                        onChange={(v) => patch({ blocked_shell_commands: v })}
                        placeholder={t_i18n("e.g. shutdown, rm -rf")}
                      />
                      <p className="content-caption text-default-secondary">
                        {t_i18n("Standing refusals: a command containing one of these words or phrases is always refused - it never runs and never asks for approval. Wins over the allow-list.")}
                      </p>
                    </div>
                  )}

                  {/* Path allow-list (only meaningful if file_ops enabled) */}
                  {draftCaps.includes("file_ops") && (
                    <div className="space-y-1">
                      <Label id="runner-allowed-paths-label">{t_i18n("Allowed paths")}</Label>
                      <ListInput
                        aria-labelledby="runner-allowed-paths-label"
                        value={draft.allowed_paths ?? []}
                        onChange={(v) => patch({ allowed_paths: v })}
                        placeholder={t_i18n("e.g. C:/Evidence, /home/audit  (empty = allow all)")}
                      />
                      <p className="content-caption text-default-secondary">{t_i18n("File operations are restricted to paths under one of these prefixes. Empty = no path restriction.")}</p>
                    </div>
                  )}
                </>
              )}

              {mode === "full_control" && (
                <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 px-3 py-2 flex items-start gap-2">
                  <ShieldAlert className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
                  <p className="content-caption text-feedback-warning-primary leading-relaxed">
                    {t_i18n("Full control grants every capability and every command with no per-operation approval. Every command is still recorded in the audit trail.")}
                  </p>
                </div>
              )}

              {/* Labels & tags */}
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label id="runner-editor-labels-label">{t_i18n("Labels")}</Label>
                  <TagInput aria-labelledby="runner-editor-labels-label" value={draft.labels ?? []} onChange={(v) => patch({ labels: v })} placeholder={t_i18n("e.g. audit, sales")} />
                  <p className="content-caption text-default-secondary">{t_i18n("Agents can target machines by label (e.g. only 'audit' runners).")}</p>
                </div>
                <div className="space-y-1">
                  <Label id="runner-editor-tags-label">{t_i18n("Tags")}</Label>
                  <TagInput aria-labelledby="runner-editor-tags-label" value={draft.tags ?? []} onChange={(v) => patch({ tags: v })} placeholder={t_i18n("Organisational tags")} />
                </div>
              </div>

              {/* Sharing (owner/admin). Rendered for company-managed runners
                  too: there the picker names the audience an access
                  restriction narrows the runner down to. */}
              <PrincipalSharingCard
                sharedGroupIds={draft.shared_group_ids ?? null}
                onChange={(ids: string[] | null) => patch({ shared_group_ids: ids ?? undefined })}
                sharedGroupPermissions={draft.shared_group_permissions ?? null}
                onPermissionsChange={(perms: Record<string, string> | null) => patch({ shared_group_permissions: perms ?? undefined })}
                userSharing={{
                  userIds: draft.shared_user_ids ?? null,
                  onUserIdsChange: (ids) => patch({ shared_user_ids: ids ?? undefined }),
                  userPermissions: draft.shared_user_permissions ?? null,
                  onUserPermissionsChange: (perms) => patch({ shared_user_permissions: perms ?? undefined }),
                  accessRestricted: !!draft.access_restricted,
                  onAccessRestrictedChange: (v) => patch({ access_restricted: v }),
                }}
                entityType="runner"
                isCompanyManaged={!!draft.is_company_managed}
              />

              {isAdmin && (
                <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                  <div className="flex items-center gap-2">
                    <Building2 className="h-4 w-4 text-accent" />
                    <div>
                      <p className="content-base font-medium">{t_i18n("Company-managed")}</p>
                      <p className="text-[11px] text-default-secondary">{t_i18n("Shared with the whole company; only admins can edit.")}</p>
                    </div>
                  </div>
                  <Switch aria-label={t_i18n("Company-managed")}
                    checked={!!draft.is_company_managed}
                    onCheckedChange={(v) => patch({ is_company_managed: v })}
                  />
                </div>
              )}

              {/* Save / cancel */}
              <div className="flex gap-2 pt-1">
                <Button size="sm" className="h-8 gap-1.5" onClick={handleSave} disabled={updateRunner.isPending} aria-busy={updateRunner.isPending || undefined}>
                  {updateRunner.isPending && <Spinner size="sm" tone="inherit" />}{!updateRunner.isPending && <Check className="h-3.5 w-3.5" />}
                  {t_i18n("Save")}
                </Button>
                <Button variant="ghost" size="sm" className="h-8" onClick={() => setEditing(false)}>
                  {t_i18n("Cancel")}
                </Button>
              </div>
            </div>
          )}

          {/* Background shell jobs, asked to the machine itself */}
          {canManage && runner.status !== "revoked" && <RunnerJobs runnerId={runner.id} />}

          {/* Recent commands (audit) */}
          <RunnerCommands runnerId={runner.id} canManage={canManage} />
        </CardContent>
      )}
    </Card>
  );
}


/* ── Host facts summary ─────────────────────────────────────────────────── */

function HostFacts({
  facts,
  agentVersion,
  commandCount,
}: {
  facts: NonNullable<Runner["host_facts"]>;
  agentVersion: string | null;
  commandCount: number;
}) {
  const { t_i18n } = useFormatter();
  const shells = (facts.shells ?? {}) as Record<string, string>;
  const browsers = (facts.browsers ?? []) as { name: string; version?: string; authenticated?: boolean }[];
  const monitors = (facts.monitors ?? []) as { width?: number; height?: number; primary?: boolean }[];

  const rows: { label: string; value: string }[] = [];
  if (facts.os || facts.os_version) rows.push({ label: t_i18n("OS"), value: [facts.os, facts.os_version, facts.os_build && `(${facts.os_build})`].filter(Boolean).join(" ") });
  if (facts.arch) rows.push({ label: t_i18n("Architecture"), value: String(facts.arch) });
  if (facts.user) rows.push({ label: t_i18n("Session user"), value: String(facts.user) });
  if (facts.timezone) rows.push({ label: t_i18n("Timezone"), value: String(facts.timezone) });
  if (facts.locale) rows.push({ label: t_i18n("Locale"), value: String(facts.locale) });
  for (const [name, ver] of Object.entries(shells)) {
    if (ver) rows.push({ label: name, value: String(ver) });
  }
  if (agentVersion) rows.push({ label: t_i18n("Runner version"), value: agentVersion });

  const hasAny = rows.length > 0 || browsers.length > 0 || monitors.length > 0;
  if (!hasAny) {
    return (
      <p className="text-[11px] text-default-secondary italic">
        {t_i18n("No host details reported yet: they arrive when the runner next connects.")}
      </p>
    );
  }

  return (
    <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
      <div className="flex items-center gap-2">
        <Cpu className="h-3.5 w-3.5 text-accent" />
        <span className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Host context")}</span>
        <Badge variant="outline" className="text-[9px] ml-auto">{t_i18n("{count} commands", { count: formatCompactNumber(commandCount) })}</Badge>
      </div>
      {rows.length > 0 && (
        <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
          {rows.map((r, i) => (
            <div key={i} className="flex items-baseline gap-2 text-[11px]">
              <span className="text-default-secondary shrink-0 min-w-[90px]">{r.label}</span>
              <span className="font-mono-plex truncate">{r.value}</span>
            </div>
          ))}
        </div>
      )}
      {browsers.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <span className="content-caption text-default-secondary">{t_i18n("Browsers:")}</span>
          {browsers.map((b, i) => (
            <Badge key={i} variant="outline" className="text-[9px] gap-1">
              <Globe className="h-2.5 w-2.5" />
              {b.name}{b.version ? ` ${b.version}` : ""}
              {b.authenticated && <Check className="h-2.5 w-2.5 text-feedback-success-primary" />}
            </Badge>
          ))}
        </div>
      )}
      {monitors.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="content-caption text-default-secondary">{t_i18n("Displays:")}</span>
          {monitors.map((m, i) => (
            <Badge key={i} variant="outline" className="text-[9px] gap-1">
              <Monitor className="h-2.5 w-2.5" />
              {m.width && m.height ? `${m.width}x${m.height}` : t_i18n("display")}
              {m.primary && <span className="text-accent">*</span>}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}


/* ── Recent commands (audit trail + HITL approval) ──────────────────────── */

// A safely-formatted, one-line summary of what a command will actually do,
// shown before an operator approves it. require_approval is a security
// boundary, so the operator must see the real command / target / input - not
// just the command kind. Long values are clipped; write_file content is
// summarized by length rather than dumped.
function commandApprovalDetail(
  kind: string,
  payload: Record<string, unknown> | null,
): string | null {
  const p = payload ?? {};
  const str = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : undefined);
  const clip = (v: string, n = 300) => (v.length > n ? `${v.slice(0, n)}...` : v);
  switch (kind) {
    case "run_shell": {
      const command = str("command");
      if (!command) return null;
      const cwd = str("cwd");
      return cwd ? `${clip(command)}  (cwd: ${cwd})` : clip(command);
    }
    case "write_file": {
      const path = str("path");
      if (!path) return null;
      const content = str("content");
      return `${path}  (${content ? content.length : 0} chars)`;
    }
    case "read_file":
    case "list_dir":
      return str("path") ?? null;
    case "computer_action": {
      const action = str("action") ?? "";
      const text = str("text");
      const keys = str("keys");
      const detail = text ? `type "${clip(text, 120)}"` : keys ? `keys ${keys}` : "";
      return [action, detail].filter(Boolean).join(" ") || null;
    }
    case "browser_control": {
      const action = str("action") ?? "";
      const detail = str("url") ?? str("selector") ?? (str("text") ? `"${clip(str("text") ?? "", 120)}"` : "");
      return [action, detail].filter(Boolean).join(" ") || null;
    }
    default:
      return null;
  }
}

function RunnerJobs({ runnerId }: { runnerId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data, refetch, isFetching } = useRunnerJobs(runnerId);
  const stop = useStopRunnerCommand();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  // While the machine answers (up to 10 s), the section keeps its place with a
  // one-line note, so the blocks under it do not jump.
  const jobs = data ? orderJobs(data.jobs) : [];

  const handleStop = async (jobId: string) => {
    const ok = await confirm({
      title: t_i18n("Stop this job?"),
      description: t_i18n("The command and every process it started are ended. What it printed until then stays readable."),
      confirmLabel: t_i18n("Stop"),
      variant: "danger",
    });
    if (!ok) return;
    stop.mutate(jobId, {
      onSuccess: (answer) =>
        toast({
          title: answer.stopping ? t_i18n("Stopping the job") : t_i18n("The job had already ended"),
          variant: "success",
        }),
      onError: (err) => toast({ title: t_i18n("Could not stop the job"), description: err.message, variant: "error" }),
    });
  };

  const statusLabel = (s: string) =>
    s === "running" ? t_i18n("Running") : s === "stopped" ? t_i18n("Stopped") : t_i18n("Exited");

  return (
    <section className="space-y-1.5" aria-label={t_i18n("Background jobs")}>
      {ConfirmEl}
      <div className="flex items-center justify-between">
        <span className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Background jobs")}</span>
        {/* The label stays while the machine answers, a spinner before it, so the
            header does not shift; the ring shows on keyboard focus. */}
        <Button
          size="sm"
          variant="ghost"
          className="h-6 gap-1 px-2 content-caption focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
          disabled={isFetching}
          aria-busy={isFetching}
          data-runner-jobs-refresh
          onClick={() => refetch()}
        >
          {/* The spinner slot is reserved when idle, so the button keeps its width while it fetches. */}
          <Spinner size="sm" tone="inherit" className={isFetching ? undefined : "invisible"} data-runner-jobs-refresh-spinner />
          {t_i18n("Refresh")}
        </Button>
      </div>
      {!data ? (
        <p className="content-caption text-default-secondary">{t_i18n("Reading the jobs from the machine")}</p>
      ) : !data.available ? (
        <p className="content-caption text-default-secondary">{t_i18n(jobsUnavailableMessage(data.reason))}</p>
      ) : jobs.length === 0 ? (
        <p className="content-caption text-default-secondary">{t_i18n("No background jobs on this machine.")}</p>
      ) : (
        <ul className="space-y-1">
          {jobs.map((j) => (
            <li key={j.job_id} className="flex items-center gap-2 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px]">
              <code className="min-w-0 flex-1 truncate font-mono-plex" title={j.command}>{j.command}</code>
              {/* The state in default text with a coloured dot: the success
                  tone as 11 px text on the row fill is 4.42:1 in light. */}
              <span className="flex shrink-0 items-center gap-1 text-default-secondary">
                <span
                  aria-hidden
                  className={cn("h-1.5 w-1.5 rounded-full", j.status === "running" ? "bg-feedback-success-primary" : "bg-current")}
                />
                {statusLabel(j.status)}
                {j.status === "exited" && j.exit_code !== null ? ` (${j.exit_code})` : ""}
              </span>
              {/* What the job printed so far, the pid in the row's title. */}
              {formatActivityBytes(j.output_bytes) && (
                <span
                  data-runner-job-output-size
                  className="text-default-secondary shrink-0 tabular-nums"
                  title={j.pid !== null ? t_i18n("Process {pid}", { pid: j.pid }) : undefined}
                >
                  {formatActivityBytes(j.output_bytes)}
                </span>
              )}
              <span className="text-default-secondary shrink-0 tabular-nums">{formatTimeAgo(intl, j.started_at)}</span>
              {j.status === "running" && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 shrink-0 gap-1 px-2 content-caption text-feedback-error-primary hover:text-feedback-error-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  aria-label={t_i18n("Stop {command}", { command: clipCommandForLabel(j.command) || j.job_id.slice(0, 8) })}
                  disabled={stop.isPending && stop.variables === j.job_id}
                  onClick={() => handleStop(j.job_id)}
                >
                  <Square className="h-3 w-3" /> {t_i18n("Stop")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RunnerCommands({ runnerId, canManage }: { runnerId: string; canManage: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { data: commands } = useRunnerCommands(runnerId, 20);
  const approve = useApproveRunnerCommand();
  const reject = useRejectRunnerCommand();
  const { toast } = useToast();

  if (!commands || commands.length === 0) return null;

  const statusColor = (s: string) =>
    s === "completed"
      ? "text-feedback-success-primary"
      : s === "failed" || s === "timeout"
        ? "text-feedback-error-primary"
        : s === "pending_approval"
          ? "text-feedback-warning-primary"
          : "text-default-secondary";

  return (
    <div className="space-y-1.5">
      <span className="content-caption uppercase tracking-wider text-default-secondary font-medium">{t_i18n("Recent commands")}</span>
      <div className="space-y-1">
        {commands.map((c) => {
          const approvalDetail =
            c.status === "pending_approval" ? commandApprovalDetail(c.kind, c.payload) : null;
          return (
            <div key={c.id} className="rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px]">
              <div className="flex items-center gap-2">
                <span className="font-medium">{t_i18n(RUNNER_COMMAND_KIND_LABELS[c.kind] ?? c.kind)}</span>
                <span className={cn("shrink-0", statusColor(c.status))}>{c.status.replace(/_/g, " ")}</span>
                <span className="text-default-secondary ml-auto shrink-0 tabular-nums">{formatTimeAgo(intl, c.created_at)}</span>
                {c.status === "pending_approval" && canManage && (
                  <div className="flex items-center gap-1 shrink-0">
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={t_i18n("Approve command")}
                      className="h-6 w-6 text-feedback-success-primary hover:text-feedback-success-primary"
                      onClick={() => approve.mutate({ commandId: c.id }, { onSuccess: () => toast({ title: t_i18n("Command approved"), variant: "success" }) })}
                    >
                      <Check className="h-3 w-3" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={t_i18n("Reject command")}
                      className="h-6 w-6 text-feedback-error-primary hover:text-feedback-error-primary"
                      onClick={() => reject.mutate({ commandId: c.id }, { onSuccess: () => toast({ title: t_i18n("Command rejected"), variant: "success" }) })}
                    >
                      <Ban className="h-3 w-3" />
                    </Button>
                  </div>
                )}
              </div>
              {c.status === "pending_approval" && (
                // The operator must see WHAT they are approving - a command
                // summary is what makes require_approval a real security gate.
                <div className="mt-1.5 rounded-sm border border-feedback-alert-primary/20 bg-elevation-hover px-2 py-1.5">
                  {approvalDetail ? (
                    <code className="block whitespace-pre-wrap break-all font-mono-plex content-caption text-default-secondary">{approvalDetail}</code>
                  ) : (
                    <span className="content-caption italic text-default-secondary">{t_i18n("No preview available for this command - review it carefully before approving.")}</span>
                  )}
                  {/* Remember-the-decision shortcuts: persist this exact
                      command into the runner's allow/block list so it never
                      queues (allow) or never runs (block) again. Shell only -
                      shell is the only kind the approval queue holds. */}
                  {c.kind === "run_shell" && canManage && (
                    <div className="mt-1.5 flex items-center gap-1.5 border-t border-elevation-subtle-soft pt-1.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 gap-1 px-2 content-caption text-feedback-success-primary hover:text-feedback-success-primary"
                        onClick={() =>
                          approve.mutate(
                            { commandId: c.id, remember: true },
                            { onSuccess: () => toast({ title: t_i18n("Approved - added to the allow-list"), variant: "success" }) },
                          )
                        }
                      >
                        <ShieldCheck className="h-3 w-3" /> {t_i18n("Approve & always allow")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 gap-1 px-2 content-caption text-feedback-error-primary hover:text-feedback-error-primary"
                        onClick={() =>
                          reject.mutate(
                            { commandId: c.id, remember: true },
                            { onSuccess: () => toast({ title: t_i18n("Rejected - added to the block-list"), variant: "success" }) },
                          )
                        }
                      >
                        <ShieldAlert className="h-3 w-3" /> {t_i18n("Reject & always block")}
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


/* ── Multi-value list for the shell and path allow-lists ────────────────────────
   The shared TagInput, keeping the case as typed and without the platform
   tag list as suggestions: a command or a path is not a tag. */

function ListInput({
  value,
  onChange,
  placeholder,
  "aria-labelledby": ariaLabelledBy,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  "aria-labelledby"?: string;
}) {
  return (
    <TagInput
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      preserveCase
      suggestFromPlatformTags={false}
      aria-labelledby={ariaLabelledBy}
    />
  );
}
