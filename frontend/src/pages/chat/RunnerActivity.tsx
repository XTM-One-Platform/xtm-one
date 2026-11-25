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
 * Machine activity in the transcript - one card per `computer_use`
 * command the agent ran on a machine, the way Cursor and Claude Desktop show
 * their tool calls.
 *
 * A shell command is a small terminal window: the prompt and the command, a
 * live timer while it runs, then its exit code, duration and output (stderr in
 * its own colour, ANSI codes stripped), with copy buttons and a larger view. A
 * file read or written shows its path and a preview; a listing shows the
 * folder's entries; a capture shows its thumbnail; an input or browser action
 * says what was done. Held, refused, busy and unreachable commands say so.
 *
 * The same cards render live (the `runner_command_*` progress frames, merged by
 * `useTurnFrames`) and from the persisted message (`runner_activity`), so a
 * reload, a relayed turn and a shared viewer see what the author saw. Inside
 * XTM One Desktop, the paths of a card whose command ran on THIS machine get
 * Open / Show in folder (`LocalPathActions`).
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Ban,
  Camera,
  CheckCircle2,
  ChevronDown,
  Clock,
  FilePen,
  FileText,
  FolderTree,
  Globe,
  Hourglass,
  Laptop,
  Maximize2,
  MousePointerClick,
  ShieldAlert,
  ShieldX,
  Square,
  Terminal,
  XCircle,
} from "lucide-react";
import { LocalPathActions, LocalPathLine } from "@/components/desktop/LocalPath";
import { CardActionButton } from "@/components/ui/card-action-button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useStopRunnerCommand } from "@/hooks/api/useRunnerApi";
import { useAuth } from "@/hooks/useAuth";
import { useAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { useFormatter } from "@/i18n";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { getDesktopBridge } from "@/lib/desktop";
import { joinLocalPath, localPathName } from "@/lib/local-paths";
import { isRunnerCardOpen, isRunnerJobStopped, markRunnerJobStopped, setRunnerCardOpen } from "@/lib/runner-card-open";
import {
  backgroundJobState,
  clipCommandForLabel,
  formatActivityBytes,
  formatActivityDuration,
  isLiveRunnerActivity,
  isRunningBackgroundJob,
  runnerActivityErrorLine,
  runnerActivityStoppedBy,
  runnerActivityTone,
  safeHttpUrl,
  shellPrompt,
  sortListingEntries,
  stripAnsi,
  summarizeRunnerActivity,
  type RunnerActivityStoppedBy,
  type RunnerActivityTone,
} from "@/lib/runner-activity";
import { cn } from "@/lib/utils";
import { WARNING_ON_HOVER_TEXT } from "@/lib/warning-text";
import type { RunnerActivity, RunnerActivityEntry } from "@/types";
import { Spinner } from "@filigran/design-system";

type Translator = (id: string, values?: Record<string, string | number>) => string;

/**
 * The tone of a card and its badge. A background job the reader stopped from
 * this page reads muted at once, like a job the machine reports as stopped:
 * one label, one tone.
 */
function cardTone(record: RunnerActivity, stoppedHere: boolean): RunnerActivityTone {
  return stoppedHere && isRunningBackgroundJob(record) ? "muted" : runnerActivityTone(record);
}

/** Entries a listing shows before "Show all". */
const LISTING_PREVIEW_ENTRIES = 40;

const TONE_BORDER: Record<RunnerActivityTone, string> = {
  live: "border-accent/25",
  success: "border-elevation-subtle-soft",
  warning: "border-feedback-alert-primary/25",
  error: "border-feedback-error-primary/25",
  muted: "border-elevation-subtle-soft",
};

const TONE_ICON: Record<RunnerActivityTone, string> = {
  live: "text-accent",
  success: "text-feedback-success-primary",
  warning: "text-feedback-warning-primary",
  error: "text-feedback-error-primary",
  muted: "text-default-secondary",
};

/** The icon of a card's kind of command. */
function KindIcon({ record, className }: { record: RunnerActivity; className: string }) {
  switch (record.kind) {
    case "run_shell":
      return <Terminal className={className} aria-hidden />;
    case "read_file":
      return <FileText className={className} aria-hidden />;
    case "write_file":
      return <FilePen className={className} aria-hidden />;
    case "list_dir":
      return <FolderTree className={className} aria-hidden />;
    case "screenshot":
      return <Camera className={className} aria-hidden />;
    case "computer_action":
      return <MousePointerClick className={className} aria-hidden />;
    default:
      return record.input.action === "screenshot" ? (
        <Camera className={className} aria-hidden />
      ) : (
        <Globe className={className} aria-hidden />
      );
  }
}

/** The card's title: what the agent did, said while it happens and once it is done. */
function activityTitle(record: RunnerActivity, t: Translator): string {
  const live = isLiveRunnerActivity(record);
  const done = record.status === "completed";
  switch (record.kind) {
    case "run_shell":
      return live ? t("Running a command") : done ? t("Ran a command") : t("Command");
    case "read_file":
      return live ? t("Reading a file") : done ? t("Read a file") : t("File read");
    case "write_file":
      return live ? t("Writing a file") : done ? t("Wrote a file") : t("File write");
    case "list_dir":
      return live ? t("Listing a folder") : done ? t("Listed a folder") : t("Folder listing");
    case "screenshot":
      return live ? t("Capturing the screen") : done ? t("Captured the screen") : t("Screen capture");
    case "computer_action":
      return live ? t("Using the mouse and keyboard") : t("Mouse and keyboard");
    default:
      return live ? t("Driving the browser") : t("Browser");
  }
}

/** One line saying what an input / browser action did. */
function actionDetail(record: RunnerActivity, t: Translator): string | null {
  const { input } = record;
  const at = input.x !== undefined && input.y !== undefined ? `${input.x}, ${input.y}` : null;
  if (record.kind === "computer_action") {
    switch (input.action) {
      case "click":
        return at ? t("Click at {position}", { position: at }) : t("Click");
      case "double_click":
        return at ? t("Double-click at {position}", { position: at }) : t("Double-click");
      case "right_click":
        return at ? t("Right-click at {position}", { position: at }) : t("Right-click");
      case "move":
        return at ? t("Move to {position}", { position: at }) : t("Move the pointer");
      case "drag":
        return at && input.x2 !== undefined && input.y2 !== undefined
          ? t("Drag from {from} to {to}", { from: at, to: `${input.x2}, ${input.y2}` })
          : t("Drag");
      case "scroll":
        return t("Scroll {amount}", { amount: input.amount ?? 0 });
      case "type":
        return t("Type {count, plural, one {# character} other {# characters}}", { count: input.text_length ?? 0 });
      case "key":
        return input.keys ? t("Press {keys}", { keys: input.keys }) : t("Press a key");
      default:
        return input.action ?? null;
    }
  }
  if (record.kind === "browser_control") {
    switch (input.action) {
      case "navigate":
        return input.url ?? t("Open a page");
      case "click":
        return input.selector ? t("Click {selector}", { selector: input.selector }) : t("Click");
      case "type":
        return input.selector
          ? t("Type {count, plural, one {# character} other {# characters}} into {selector}", {
              count: input.text_length ?? 0,
              selector: input.selector,
            })
          : t("Type {count, plural, one {# character} other {# characters}}", { count: input.text_length ?? 0 });
      case "get_content":
        return t("Read the page");
      case "screenshot":
        return record.output?.title ?? record.output?.url ?? t("Capture the page");
      case "list_tabs":
        return t("List the open tabs");
      case "select_tab":
        return record.output?.title ?? record.output?.url ?? t("Switch tab");
      default:
        return input.action ?? null;
    }
  }
  return null;
}

/** What the header shows after the title: the command, the path, the action. */
function headerDetail(record: RunnerActivity, t: Translator): string | null {
  switch (record.kind) {
    case "run_shell":
      return record.input.command?.split("\n")[0] ?? null;
    case "read_file":
    case "write_file":
    case "list_dir":
      return record.input.path ? localPathName(record.input.path) || record.input.path : null;
    case "screenshot":
      return record.output?.width && record.output?.height ? `${record.output.width} x ${record.output.height}` : null;
    default:
      return actionDetail(record, t);
  }
}

/**
 * Seconds since `active` last turned true: a command that waited for its
 * approval starts counting when it starts running, not when its card appeared.
 */
function useElapsedSeconds(active: boolean): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!active) return;
    const startedAt = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return active ? elapsed : 0;
}

/** The badge of a stopped card: who stopped it, as the reader sees it. */
function stoppedLabel(stoppedBy: RunnerActivityStoppedBy, t: Translator): string {
  if (stoppedBy.who === "you") return t("Stopped by you");
  if (stoppedBy.who === "named") return t("Stopped by {name}", { name: stoppedBy.name });
  return t("Stopped");
}

function StatusBadge({
  record,
  stoppedBy,
  stoppedHere = false,
  t,
}: {
  record: RunnerActivity;
  stoppedBy: RunnerActivityStoppedBy;
  /** A background job the reader stopped from this page: settled before the record says so. */
  stoppedHere?: boolean;
  t: Translator;
}) {
  const { locale } = useFormatter();
  const tone = cardTone(record, stoppedHere);
  const elapsed = useElapsedSeconds(record.status === "running");
  const duration = formatActivityDuration(record.duration_ms, locale);
  const iconClass = cn("h-3.5 w-3.5 shrink-0", TONE_ICON[tone]);

  let icon: ReactNode;
  let label: string | null = null;
  let title: string | undefined;
  const job = backgroundJobState(record);
  switch (record.status) {
    case "running":
      icon = <Spinner size="sm" tone="inherit" className={cn("shrink-0", TONE_ICON[tone])} />;
      label = elapsed >= 2 ? formatActivityDuration(elapsed * 1000, locale) : null;
      break;
    case "awaiting_approval":
      icon = <ShieldAlert className={iconClass} aria-hidden />;
      label = t("Awaiting your approval");
      break;
    case "completed":
      if (job?.state === "running" && stoppedHere) {
        icon = <Square className={`${iconClass} fill-current`} aria-hidden />;
        label = t("Stopped by you");
      } else if (job?.state === "running") {
        // A background job still running on the machine: a short label
        // that fits a narrow card, the job id in its title.
        icon = <Spinner size="sm" tone="inherit" className={cn("shrink-0", TONE_ICON[tone])} />;
        label = t("Background");
        title = record.output?.job_id
          ? t("In the background, job {id}", { id: record.output.job_id.slice(0, 8) })
          : t("In the background");
      } else if (job?.state === "stopped") {
        icon = <Square className={`${iconClass} fill-current`} aria-hidden />;
        label = t("Stopped by you");
      } else if (job?.state === "exited") {
        // A background job that ended: its exit, not a loader.
        icon =
          job.exitCode === null || job.exitCode === 0 ? (
            <CheckCircle2 className={iconClass} aria-hidden />
          ) : (
            <XCircle className={iconClass} aria-hidden />
          );
        label = job.exitCode === null ? t("Ended") : t("exit {code}", { code: job.exitCode });
      } else if (record.kind === "run_shell" && typeof record.output?.exit_code === "number") {
        icon = record.output.exit_code === 0 ? (
          <CheckCircle2 className={iconClass} aria-hidden />
        ) : (
          <XCircle className={iconClass} aria-hidden />
        );
        label = t("exit {code}", { code: record.output.exit_code });
      } else {
        icon = <CheckCircle2 className={iconClass} aria-hidden />;
      }
      break;
    case "failed":
      icon = <XCircle className={iconClass} aria-hidden />;
      label = t("Failed");
      break;
    case "timeout":
      icon = <Clock className={iconClass} aria-hidden />;
      label = t("Timed out");
      break;
    case "rejected":
      icon = <Ban className={iconClass} aria-hidden />;
      label = t("Rejected");
      break;
    case "not_approved":
      icon = <Hourglass className={iconClass} aria-hidden />;
      label = t("Not approved yet");
      break;
    case "refused":
      icon = <ShieldX className={iconClass} aria-hidden />;
      label = t("Blocked by the machine's policy");
      break;
    case "busy":
      icon = <Hourglass className={iconClass} aria-hidden />;
      label = t("Machine busy");
      break;
    case "unavailable":
      icon = <AlertTriangle className={iconClass} aria-hidden />;
      label = t("Machine unavailable");
      break;
    case "stopped":
      icon = <Square className={`${iconClass} fill-current`} aria-hidden />;
      label = stoppedLabel(stoppedBy, t);
      break;
    default:
      icon = <Ban className={iconClass} aria-hidden />;
      label = t("Interrupted");
  }

  // The label is bounded: a long stopper name truncates with the
  // full label in its title, so the badge never pushes the duration and the
  // chevron past the card's edge; the icon and the duration keep their size.
  // The badge itself does not shrink: the command detail
  // beside it gives way first, so a short status ("Failed", "exit 137") always
  // reads in full and only the bounded label truncates.
  return (
    // The badge never shrinks: a short status label and its duration keep
    // their width on one line; the command and the machine chip give way instead.
    <span
      data-runner-status-badge
      data-fit="fixed"
      className="ml-auto flex shrink-0 items-center gap-1.5 whitespace-nowrap text-[11px] tabular-nums text-default-secondary"
      title={title}
    >
      {icon}
      {label && (
        // The error label is the full error token: at 90% it painted
        // 4.02:1 on the dark card at 11 px, under the 4.5:1 a small text needs.
        <span
          title={label}
          className={cn(
            "min-w-0 max-w-[10rem] truncate @xl:max-w-[16rem]",
            tone === "error" && "text-feedback-error-primary",
            tone === "warning" && WARNING_ON_HOVER_TEXT,
          )}
          data-runner-status-label
          data-tone={tone}
          data-fit="truncate"
        >
          {label}
        </span>
      )}
      {record.status !== "running" && duration && (
        <span data-runner-status-duration data-fit="fixed" className="shrink-0 whitespace-nowrap text-default-secondary">
          {duration}
        </span>
      )}
    </span>
  );
}

/**
 * Stop on the line of a running shell command: the machine ends the
 * command with every process it started, and the card settles as "Stopped by
 * you" from the tool's result, with what the command printed until then. On a
 * narrow card the word goes and the icon stays, so the command line
 * keeps its room; the tooltip still says "Stop the command" and the accessible
 * name carries the command, clipped.
 *
 * A background job is stopped the way the Machines page stops it:
 * a confirm first, then a toast from the stop answer ("Stopping the job", or
 * "The job had already ended"), and the card settles as "Stopped by you" at
 * once, remembered for the session so a remount does not offer Stop again.
 * After a reload the record still says running: a second Stop answers "The
 * job had already ended" and settles the card the same way.
 */
function StopButton({
  commandId,
  command,
  background,
  onStopped,
  t,
}: {
  commandId: string;
  command: string | null | undefined;
  background: boolean;
  onStopped: () => void;
  t: Translator;
}) {
  const stop = useStopRunnerCommand();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const [asked, setAsked] = useState(false);
  const onClick = async () => {
    if (background) {
      const ok = await confirm({
        title: t("Stop this job?"),
        description: t("The command and every process it started are ended. What it printed until then stays readable."),
        confirmLabel: t("Stop"),
        variant: "danger",
      });
      if (!ok) return;
    }
    setAsked(true);
    stop.mutate(commandId, {
      onSuccess: (answer) => {
        if (!background) return;
        toast({
          title: answer.stopping ? t("Stopping the job") : t("The job had already ended"),
          variant: "success",
        });
        markRunnerJobStopped(commandId);
        onStopped();
      },
      onError: (e) => {
        setAsked(false);
        toast({ title: e.message || t("The command could not be stopped"), variant: "error" });
      },
    });
  };
  const clipped = clipCommandForLabel(command);
  return (
    <>
      {ConfirmEl}
      <CardActionButton
        icon={<>{asked && <Spinner size="sm" tone="inherit" />}{!asked && <Square className="h-3 w-3 fill-current" aria-hidden />}</>}
        label={clipped ? t("Stop {command}", { command: clipped }) : t("Stop the command")}
        text={t("Stop")}
        textClassName="hidden @xl:inline"
        tone="danger"
        onClick={() => void onClick()}
        disabled={asked}
        className="mr-2"
      />
    </>
  );
}

/**
 * The machine a card's command ran on: its OS mark and its name ("This machine"
 * inside the app). The command has priority over the chip: the
 * chip never shrinks below its icon, is capped at 40% of the header, and on a
 * narrow card (header under 28rem, the `@container` on the card row) it shows
 * the icon alone, the full name in its tooltip and for screen readers. It is
 * never an empty pill.
 */
function MachineChip({ record, t }: { record: RunnerActivity; t: Translator }) {
  const bridge = getDesktopBridge();
  const here = !!bridge?.runner_id && record.runner_id === bridge.runner_id;
  const name = record.runner_name ?? t("Unknown machine");
  const label = here ? t("This machine") : name;
  const platform = record.platform ? RUNNER_PLATFORM_CONFIG[record.platform] : undefined;
  return (
    <Tip content={name}>
      <span
        data-runner-machine-chip
        data-fit="fixed"
        className="hidden max-w-[40%] shrink-0 items-center gap-1 rounded-sm border border-transparent bg-elevation-hover px-2 py-0.5 content-caption text-default-secondary sm:inline-flex"
      >
        {platform && record.platform ? (
          <ProviderLogo provider={record.platform} logo={platform.logo} className="h-3 w-3 shrink-0" />
        ) : (
          <Laptop className="h-3 w-3 shrink-0" aria-hidden />
        )}
        <span data-runner-machine-name data-fit="truncate" data-shown-from="md" className="hidden min-w-0 truncate @md:block" aria-hidden>
          {label}
        </span>
        <span className="sr-only">{label}</span>
      </span>
    </Tip>
  );
}

/**
 * A monospace window: the terminal of a shell card, the preview of a file
 * card. Its buttons (copy, larger view) sit on its top-right corner, outside
 * the scrolling area so they stay put while the text scrolls.
 */
function OutputWindow({
  children,
  tall = false,
  actions,
}: {
  children: ReactNode;
  tall?: boolean;
  actions?: ReactNode;
}) {
  return (
    <div className="relative">
      <div
        className={cn(
          "overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default px-3 py-2 font-mono-plex text-[12px] leading-5",
          tall ? "max-h-[65vh]" : "max-h-60",
          actions && "pr-20",
        )}
      >
        {children}
      </div>
      {actions && (
        <div className="absolute right-1.5 top-1.5 flex items-center gap-0.5 rounded-sm bg-elevation-default backdrop-blur-sm">
          {actions}
        </div>
      )}
    </div>
  );
}

function ShellOutput({
  record,
  t,
  tall = false,
  actions,
}: {
  record: RunnerActivity;
  t: Translator;
  tall?: boolean;
  actions?: ReactNode;
}) {
  const stdout = stripAnsi(record.output?.stdout ?? "");
  const stderr = stripAnsi(record.output?.stderr ?? "");
  const prompt = shellPrompt(record.platform, record.input.cwd);
  return (
    <OutputWindow tall={tall} actions={actions}>
      {record.output?.background && typeof record.output.pid === "number" && (
        // The background job's process id, the way the machine reports it.
        <div data-runner-job-pid className="mb-1 text-[11px] text-default-secondary">
          {t("Process {pid}", { pid: record.output.pid })}
        </div>
      )}
      <div className="whitespace-pre-wrap break-words text-default-secondary">
        <span className="select-none text-accent/70">{prompt} </span>
        {record.input.command}
      </div>
      {stdout && <pre className="m-0 mt-1 whitespace-pre-wrap break-words text-default-secondary">{stdout}</pre>}
      {stderr && <pre className="m-0 mt-1 whitespace-pre-wrap break-words text-feedback-error-primary">{stderr}</pre>}
      {record.status === "completed" && !stdout && !stderr && (
        <div className="mt-1 italic text-default-secondary">{t("(no output)")}</div>
      )}
      {record.status === "running" && (
        <div className="mt-1 flex items-center gap-1.5 text-default-secondary">
          <span className="inline-block h-3.5 w-1.5 animate-pulse rounded-sm bg-accent/70" aria-hidden />
        </div>
      )}
    </OutputWindow>
  );
}

function PreviewText({ text, tall = false, actions }: { text: string; tall?: boolean; actions?: ReactNode }) {
  return (
    <OutputWindow tall={tall} actions={actions}>
      <pre className="m-0 whitespace-pre-wrap break-words text-default-secondary">{text}</pre>
    </OutputWindow>
  );
}

function Listing({ record, t }: { record: RunnerActivity; t: Translator }) {
  const [showAll, setShowAll] = useState(false);
  const entries = useMemo(() => sortListingEntries(record.output?.entries ?? []), [record.output?.entries]);
  const shown = showAll ? entries : entries.slice(0, LISTING_PREVIEW_ENTRIES);
  const total = record.output?.entry_count ?? entries.length;
  const dir = record.input.path ?? "";
  if (total === 0) {
    return <p className="content-compact italic text-default-secondary">{t("The folder is empty.")}</p>;
  }
  return (
    <div className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default">
      <ul className="divide-y divide-[var(--border-elevation-subtle)]">
        {shown.map((entry: RunnerActivityEntry) => (
          <li key={entry.name} className="group/entry flex items-center gap-2 px-2.5 py-1 content-compact">
            {entry.is_dir ? (
              <FolderTree className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" aria-hidden />
            ) : (
              <FileText className="h-3.5 w-3.5 shrink-0 text-accent/60" aria-hidden />
            )}
            <span className="min-w-0 truncate font-mono-plex text-default-secondary">{entry.name}</span>
            {!entry.is_dir && formatActivityBytes(entry.size) && (
              <span className="shrink-0 tabular-nums text-default-secondary">{formatActivityBytes(entry.size)}</span>
            )}
            {dir && (
              <LocalPathActions
                path={joinLocalPath(dir, entry.name, record.platform)}
                isDir={entry.is_dir}
                runnerId={record.runner_id}
                className="ml-auto opacity-0 transition-opacity group-hover/entry:opacity-100 focus-within:opacity-100"
              />
            )}
          </li>
        ))}
      </ul>
      {(entries.length > shown.length || total > entries.length) && (
        <div className="flex items-center gap-2 border-t border-elevation-subtle-soft px-2.5 py-1.5 text-[11px] text-default-secondary">
          {entries.length > shown.length && (
            <button type="button" onClick={() => setShowAll(true)} className="text-accent/80 hover:text-accent">
              {t("Show all {count}", { count: entries.length })}
            </button>
          )}
          {total > entries.length && <span>{t("{count} more not shown", { count: total - entries.length })}</span>}
        </div>
      )}
    </div>
  );
}

function CaptureThumbnail({ fileId, label, t }: { fileId: string; label: string; t: Translator }) {
  const src = useAuthenticatedSrc(`/api/v1/chat/files/${encodeURIComponent(fileId)}/download`);
  const [open, setOpen] = useState(false);
  if (!src) {
    return (
      <div className="flex h-24 w-40 items-center justify-center rounded-sm border border-elevation-subtle-soft bg-elevation-default">
        <Spinner size="sm" tone="inherit" className="text-accent/60" label={t("Loading...")} />
      </div>
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("Enlarge the capture")}
        className="block overflow-hidden rounded-sm border border-elevation-subtle-soft transition-colors hover:border-accent/30"
      >
        <img src={src} alt={label} className="max-h-48 w-auto object-contain" loading="lazy" />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-[92vw] p-2">
          <DialogHeader className="sr-only">
            <DialogTitle>{label}</DialogTitle>
            <DialogDescription>{label}</DialogDescription>
          </DialogHeader>
          <img src={src} alt={label} className="max-h-[85vh] w-full rounded-sm object-contain" />
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Why a card that did not run says so, beyond its badge. */
function statusNote(record: RunnerActivity, t: Translator, stoppedBy: RunnerActivityStoppedBy = { who: "unknown" }): string | null {
  switch (record.status) {
    case "awaiting_approval":
      return t("The machine holds this command for your decision - approve or reject it above the message box.");
    case "not_approved":
      return t("Held for approval: it runs once someone approves it on the XTM One Desktop page.");
    case "rejected":
      return t("The command was rejected, so it never ran.");
    case "busy":
      return t("The machine was already running as many commands as it allows.");
    case "unavailable":
      return record.local
        ? t("This machine is not connected right now - XTM One Desktop is reconnecting or its runner is stopped.")
        : t("No machine could be reached for this command.");
    case "interrupted":
      return t("The turn ended before the machine reported back.");
    case "stopped":
      if (stoppedBy.who === "you") {
        return t("You stopped this command and every process it started. Its output until then is shown above.");
      }
      if (stoppedBy.who === "named") {
        return t("{name} stopped this command and every process it started. Its output until then is shown above.", {
          name: stoppedBy.name,
        });
      }
      return t("This command was stopped with every process it started. Its output until then is shown above.");
    default:
      return null;
  }
}

function CardBody({
  record,
  stoppedBy,
  t,
  tall = false,
  actions,
}: {
  record: RunnerActivity;
  stoppedBy: RunnerActivityStoppedBy;
  t: Translator;
  tall?: boolean;
  /** The copy / larger-view buttons, drawn on the card's output window. */
  actions?: ReactNode;
}) {
  const note = statusNote(record, t, stoppedBy);
  const errorLine = runnerActivityErrorLine(record);
  const output = record.output;
  const truncated =
    !!record.input.command_truncated ||
    !!output?.stdout_truncated ||
    !!output?.stderr_truncated ||
    !!output?.content_truncated ||
    !!record.input.content_truncated;

  let content: ReactNode;
  switch (record.kind) {
    case "run_shell":
      content = <ShellOutput record={record} t={t} tall={tall} actions={actions} />;
      break;
    case "read_file":
      content = (
        <div className="space-y-1.5">
          {record.input.path && <LocalPathLine path={record.input.path} runnerId={record.runner_id} />}
          {output?.binary ? (
            <p className="content-compact text-default-secondary">
              {t("Binary content ({size}), attached to the reply.", { size: formatActivityBytes(output.size) ?? "?" })}
            </p>
          ) : record.status === "completed" ? (
            output?.content ? (
              <PreviewText text={output.content} tall={tall} actions={actions} />
            ) : (
              <p className="content-compact italic text-default-secondary">{t("The file is empty.")}</p>
            )
          ) : null}
        </div>
      );
      break;
    case "write_file":
      content = (
        <div className="space-y-1.5">
          {record.input.path && <LocalPathLine path={record.input.path} runnerId={record.runner_id} />}
          {record.input.content ? <PreviewText text={record.input.content} tall={tall} actions={actions} /> : null}
          {typeof record.input.content_bytes === "number" && (
            <p className="text-[11px] text-default-secondary">
              {formatActivityBytes(record.input.content_bytes)}
            </p>
          )}
        </div>
      );
      break;
    case "list_dir":
      content = (
        <div className="space-y-1.5">
          {record.input.path && <LocalPathLine path={record.input.path} isDir runnerId={record.runner_id} />}
          {record.status === "completed" && <Listing record={record} t={t} />}
        </div>
      );
      break;
    case "screenshot":
      content = output?.file_id ? (
        <CaptureThumbnail fileId={output.file_id} label={activityTitle(record, t)} t={t} />
      ) : null;
      break;
    case "browser_control": {
      const pageUrl = safeHttpUrl(record.input.url);
      content = (
        <div className="space-y-1.5">
          {pageUrl ? (
            <a
              href={pageUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="block truncate font-mono-plex content-compact text-accent/90 underline decoration-accent/30 underline-offset-2 hover:decoration-accent/70"
            >
              {record.input.url}
            </a>
          ) : (
            record.input.url && <p className="truncate font-mono-plex content-compact text-default-secondary">{record.input.url}</p>
          )}
          {output?.title && <p className="truncate content-compact text-default-secondary">{output.title}</p>}
          {typeof output?.content_chars === "number" && (
            <p className="content-compact text-default-secondary">
              {t("Read {count} characters of page content.", { count: output.content_chars })}
            </p>
          )}
          {output?.file_id && <CaptureThumbnail fileId={output.file_id} label={activityTitle(record, t)} t={t} />}
        </div>
      );
      break;
    }
    default:
      content = null;
  }

  return (
    <div className="space-y-1.5 px-3 pb-2.5 pt-2">
      {content}
      {errorLine && (
        <p className="flex items-start gap-1.5 content-compact text-feedback-error-primary">
          <XCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          <span className="break-words">{errorLine}</span>
        </p>
      )}
      {note && <p className="content-compact text-default-secondary">{note}</p>}
      {truncated && (
        <p className="text-[11px] text-default-secondary">
          {t("Shortened here - the reasoning details of the reply hold more of it.")}
        </p>
      )}
    </div>
  );
}

/** Text a card offers to copy: the command, or its output. */
function copyValues(record: RunnerActivity): { command: string | null; output: string | null } {
  if (record.kind === "run_shell") {
    const output = [record.output?.stdout, record.output?.stderr].filter(Boolean).map((s) => stripAnsi(s!)).join("\n");
    return { command: record.input.command ?? null, output: output || null };
  }
  if (record.kind === "read_file") return { command: null, output: record.output?.content || null };
  if (record.kind === "write_file") return { command: null, output: record.input.content || null };
  return { command: null, output: null };
}

function hasBody(record: RunnerActivity): boolean {
  if (runnerActivityErrorLine(record) || statusNote(record, (s) => s)) return true;
  return record.kind !== "computer_action";
}

function RunnerActivityCard({ record }: { record: RunnerActivity }) {
  const { t_i18n, locale } = useFormatter();
  // Collapsed on its one line until the reader opens it; the opening
  // outlives this card (the live card hands over to the saved one).
  const [open, setOpenState] = useState(() => isRunnerCardOpen(record.command_id));
  const toggleOpen = () => {
    setRunnerCardOpen(record.command_id, !open);
    setOpenState(!open);
  };
  const [expanded, setExpanded] = useState(false);

  const { user } = useAuth();
  const stoppedBy = runnerActivityStoppedBy(record, user?.id);
  const [stoppedHere, setStoppedHere] = useState(() => isRunnerJobStopped(record.command_id));
  const tone = cardTone(record, stoppedHere);
  // The Stop that opened the confirm unmounts once the job settles: focus goes
  // back to the card's header button, never to <body>.
  const headerRef = useRef<HTMLButtonElement>(null);
  const focusHeaderAfterStop = useRef(false);
  useEffect(() => {
    if (stoppedHere && focusHeaderAfterStop.current) {
      focusHeaderAfterStop.current = false;
      headerRef.current?.focus();
    }
  }, [stoppedHere]);
  const title = activityTitle(record, t_i18n);
  const detail = headerDetail(record, t_i18n);
  const body = hasBody(record);
  // A shell command still running on a real queue entry can be stopped.
  // A background job stays stoppable after the turn, while it runs.
  const stoppable =
    record.kind === "run_shell" &&
    (record.status === "running" || (isRunningBackgroundJob(record) && !stoppedHere)) &&
    !record.command_id.startsWith("local-");
  const copies = copyValues(record);
  const canExpand = record.kind === "run_shell" || !!copies.output;
  const windowActions = (withExpand: boolean) =>
    copies.command || copies.output || (withExpand && canExpand) ? (
      <>
        {copies.command && <CopyIconButton value={copies.command} label={t_i18n("Copy command")} />}
        {copies.output && <CopyIconButton value={copies.output} label={t_i18n("Copy output")} />}
        {withExpand && canExpand && (
          <CardActionButton
            icon={<Maximize2 className="h-3.5 w-3.5" aria-hidden />}
            label={t_i18n("Larger view")}
            onClick={() => setExpanded(true)}
          />
        )}
      </>
    ) : undefined;

  const header = (
    <>
      <KindIcon record={record} className={cn("h-3.5 w-3.5 shrink-0", TONE_ICON[tone])} />
      {/* Last to give way: on the narrowest card (fr title next to Stop) it truncates before the command floor. */}
      <span data-runner-card-title className="min-w-0 truncate content-compact font-medium text-default-secondary" title={title}>
        {title}
      </span>
      {detail && (
        // The command keeps a floor and takes the free space: on a narrow
        // card the machine chip collapses to its icon before the command disappears.
        <span
          data-runner-card-detail
          data-fit="fill"
          className={cn(
            "min-w-[6ch] flex-1 truncate content-compact text-default-secondary/80",
            (record.kind === "run_shell" || record.kind === "read_file" || record.kind === "write_file" || record.kind === "list_dir") &&
              "font-mono-plex",
          )}
          title={detail}
        >
          {detail}
        </span>
      )}
      <MachineChip record={record} t={t_i18n} />
      <StatusBadge record={record} stoppedBy={stoppedBy} stoppedHere={stoppedHere} t={t_i18n} />
      {body && (
        <ChevronDown
          className={cn("h-3.5 w-3.5 shrink-0 text-default-secondary transition-transform duration-200", open && "rotate-180")}
          aria-hidden
        />
      )}
    </>
  );

  return (
    <div
      data-testid="runner-activity-card"
      data-kind={record.kind}
      data-status={record.status}
      className={cn("overflow-hidden rounded-sm border layer-1 bg-elevation-default backdrop-blur-sm transition-colors", TONE_BORDER[tone])}
    >
      <div className="@container flex w-full min-w-0 items-center">
        {body ? (
          <button
            ref={headerRef}
            type="button"
            onClick={toggleOpen}
            aria-expanded={open}
            className="group/hover-row flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
          >
            {header}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2">{header}</div>
        )}
        {stoppable && (
          <StopButton
            commandId={record.command_id}
            command={record.input.command}
            background={isRunningBackgroundJob(record)}
            onStopped={() => {
              focusHeaderAfterStop.current = true;
              setStoppedHere(true);
            }}
            t={t_i18n}
          />
        )}
      </div>
      {body && open && (
        <div className="border-t border-elevation-subtle-soft">
          <CardBody record={record} stoppedBy={stoppedBy} t={t_i18n} actions={windowActions(true)} />
        </div>
      )}
      {canExpand && (
        <Dialog open={expanded} onOpenChange={setExpanded}>
          <DialogContent className="max-w-4xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 text-base">
                <KindIcon record={record} className={cn("h-4 w-4", TONE_ICON[tone])} />
                {title}
              </DialogTitle>
              <DialogDescription className="flex items-center gap-2">
                <span className="truncate">{record.runner_name ?? t_i18n("Unknown machine")}</span>
                {formatActivityDuration(record.duration_ms, locale) && <span>{formatActivityDuration(record.duration_ms, locale)}</span>}
              </DialogDescription>
            </DialogHeader>
            <CardBody record={record} stoppedBy={stoppedBy} t={t_i18n} tall actions={windowActions(false)} />
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}

/**
 * The cards of one message (or of the turn in flight). Every card arrives
 * collapsed on its one line - live or saved, running, done or failed, the line
 * carries the status - and opens on a click only, so a card that
 * finishes never changes height and a turn that ends does not reshuffle the
 * page.
 */
export function RunnerActivityBlock({ records, className }: { records: RunnerActivity[]; className?: string }) {
  const { t_i18n } = useFormatter();
  const summary = useMemo(() => summarizeRunnerActivity(records), [records]);
  if (records.length === 0) return null;

  const chips: string[] = [];
  if (summary.commands) chips.push(t_i18n("{count, plural, one {# command} other {# commands}}", { count: summary.commands }));
  if (summary.reads) chips.push(t_i18n("{count, plural, one {# file read} other {# files read}}", { count: summary.reads }));
  if (summary.writes) chips.push(t_i18n("{count, plural, one {# file written} other {# files written}}", { count: summary.writes }));
  if (summary.listings) chips.push(t_i18n("{count, plural, one {# folder listed} other {# folders listed}}", { count: summary.listings }));
  if (summary.captures) chips.push(t_i18n("{count, plural, one {# capture} other {# captures}}", { count: summary.captures }));
  if (summary.actions) chips.push(t_i18n("{count, plural, one {# action} other {# actions}}", { count: summary.actions }));

  return (
    <div data-testid="runner-activity" className={cn("space-y-1.5", className)}>
      {records.length > 1 && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-0.5 text-[11px] text-default-secondary">
          <Laptop className="h-3 w-3 shrink-0 text-accent/70" aria-hidden />
          {/* No machine name at all: every command failed to reach one. */}
          {summary.machines.length > 0 && (
            <span className="font-medium text-default-secondary">
              {summary.machines.length === 1
                ? t_i18n("On {machine}", { machine: summary.machines[0] })
                : t_i18n("On {count} machines", { count: summary.machines.length })}
            </span>
          )}
          {chips.map((chip, index) => (
            <span key={chip} className="flex items-center gap-2">
              {(index > 0 || summary.machines.length > 0) && (
                <span aria-hidden className="text-default-disabled">
                  {"\u00b7"}
                </span>
              )}
              {chip}
            </span>
          ))}
        </div>
      )}
      {records.map((record) => (
        <RunnerActivityCard key={record.command_id} record={record} />
      ))}
    </div>
  );
}
