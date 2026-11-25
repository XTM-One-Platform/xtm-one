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

import { useEffect, useState } from "react";
import { GitFork, Loader2, CheckCircle2, XCircle, X, Bot, Hourglass } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { ICON_MAP, REFETCH_INTERVAL_FAST, STATUS_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { SubAgentTask } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";
import { fitChipCount } from "./background-task-strip-fit";

const COMPLETED_RECENCY_MS = 60_000;
/** Below this bar width the label reads "4 tasks running" instead of wrapping. */
const SHORT_LABEL_BELOW_PX = 560;

interface BackgroundTasksBannerProps {
  activeBgTasks: SubAgentTask[];
  bgTasks: SubAgentTask[] | undefined;
}

/**
 * The width of the element the ref is attached to, kept current as it resizes (0 until measured).
 * A callback ref: the element may mount after the hook's owner (the bar renders only once a task
 * runs), so the observer follows the node itself, never a ref read once at the owner's mount.
 */
function useMeasuredWidth<T extends HTMLElement>() {
  const [node, setNode] = useState<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => setWidth(Math.round(entries[0]?.contentRect.width ?? 0)));
    observer.observe(node);
    return () => observer.disconnect();
  }, [node]);
  return { ref: setNode, width };
}

/**
 * Rich tooltip body for a background task — target agent, status, relative
 * time, a short task extract, and a result/error preview. Replaces the old
 * full-prompt tooltip (a wall of text). `SubAgentTask` already carries every
 * field used here, so no extra fetch is needed.
 */
function TaskTooltip({ task }: { task: SubAgentTask }) {
  const { t_i18n, intl } = useFormatter();
  const status = STATUS_CONFIG[task.status] ?? STATUS_CONFIG.pending;
  const StatusIcon = status.icon;
  const AgentIcon =
    (task.target_agent_icon && ICON_MAP[task.target_agent_icon]) || Bot;
  const when = task.completed_at || task.started_at || task.created_at;
  return (
    <div className="w-[240px] space-y-1.5 py-0.5">
      <div className="flex items-center gap-1.5">
        <AgentIcon className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="truncate font-medium text-default-primary">
          {task.target_agent_name || t_i18n("Agent")}
        </span>
      </div>
      <div className="flex items-center gap-1.5 text-[11px]">
        <StatusIcon
          className={cn(
            "h-3 w-3 shrink-0",
            status.color,
            task.status === "running" && "animate-spin",
          )}
        />
        <span className={status.color}>{t_i18n(status.label)}</span>
        {when && <span className="text-default-secondary">· {formatTimeAgo(intl, when)}</span>}
      </div>
      {task.task_description && (
        <p className="line-clamp-6 break-words text-[11px] leading-snug text-default-secondary">
          {task.task_description.trim()}
        </p>
      )}
      {task.status === "completed" && task.result && (
        <p className="line-clamp-4 break-words border-t border-elevation-subtle-soft pt-1 text-[11px] leading-snug text-default-secondary">
          {task.result.trim()}
        </p>
      )}
      {task.status === "failed" && task.error && (
        <p className="line-clamp-4 break-words border-t border-elevation-subtle-soft pt-1 text-[11px] leading-snug text-feedback-error-primary">
          {task.error.trim()}
        </p>
      )}
      {task.status === "incomplete" && task.result && (
        <p className="line-clamp-4 break-words border-t border-elevation-subtle-soft pt-1 content-caption leading-snug text-feedback-warning-primary">
          {task.result.trim()}
        </p>
      )}
    </div>
  );
}

const PILL_TONES: Record<"active" | "completed" | "failed" | "incomplete", string> = {
  active: "bg-accent/10 text-accent/70",
  completed: "bg-feedback-success-primary/10 text-feedback-success-primary/80",
  failed: "bg-feedback-error-primary/10 text-feedback-error-primary/80",
  incomplete: "bg-feedback-alert-primary/10 text-feedback-warning-tertiary",
};

type PillTone = "active" | "completed" | "failed" | "incomplete";

/**
 * One task chip. The chip itself is the tooltip trigger and a tab stop, so the
 * full task opens on hover and on keyboard focus; its accessible name is the
 * full title. The title truncates in CSS (`min-w-0` chip, `truncate` label):
 * the whole string stays in the DOM, never cut in JS.
 */
function TaskPill({ task, tone }: { task: SubAgentTask; tone: PillTone }) {
  const tabIndex = useTriggerTabIndex();
  const title = (task.task_description ?? "").trim();
  const Icon =
    tone === "active"
      ? Loader2
      : tone === "completed"
        ? CheckCircle2
        : tone === "incomplete"
          ? Hourglass
          : XCircle;
  return (
    <Tip content={<TaskTooltip task={task} />}>
      <span
        tabIndex={tabIndex}
        aria-label={title || undefined}
        data-testid="background-task-chip"
        className={cn(
          "inline-flex min-w-0 max-w-[180px] shrink items-center gap-1 rounded-sm border border-transparent px-2 py-0.5 content-caption outline-none focus-visible:ring-2 focus-visible:ring-focus",
          PILL_TONES[tone],
        )}
      >
        <Icon className={cn("h-2.5 w-2.5 shrink-0", tone === "active" && "animate-spin")} />
        <span className="min-w-0 truncate" data-testid="background-task-chip-title">
          {title}
        </span>
      </span>
    </Tip>
  );
}

/** The titles of the tasks folded into "+N more", each on one line. */
function HiddenTasksTooltip({ tasks }: { tasks: SubAgentTask[] }) {
  return (
    <ul className="w-[240px] space-y-1 py-0.5" data-testid="background-task-hidden-titles">
      {tasks.map((task) => (
        <li key={task.task_id} className="line-clamp-2 break-words text-[11px] leading-snug">
          {(task.task_description ?? "").trim()}
        </li>
      ))}
    </ul>
  );
}

/**
 * The chips of a banner on one line: as many as the strip's width holds, the
 * rest folded into "+N more", whose tooltip lists their titles.
 */
function TaskStrip({
  tasks,
  toneOf,
  counterClassName,
}: {
  tasks: SubAgentTask[];
  toneOf: (task: SubAgentTask) => PillTone;
  counterClassName: string;
}) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const { ref, width } = useMeasuredWidth<HTMLDivElement>();
  const shown = fitChipCount(width, tasks.length);
  const hidden = tasks.slice(shown);
  return (
    <div ref={ref} className="ml-1.5 flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden p-1">
      {tasks.slice(0, shown).map((task) => (
        <TaskPill key={task.task_id} task={task} tone={toneOf(task)} />
      ))}
      {hidden.length > 0 && (
        <Tip content={<HiddenTasksTooltip tasks={hidden} />}>
          <span
            tabIndex={tabIndex}
            data-testid="background-task-more"
            className={cn(
              "shrink-0 whitespace-nowrap rounded-sm content-caption outline-none focus-visible:ring-2 focus-visible:ring-focus",
              counterClassName,
            )}
          >
            {t_i18n("+{count} more", { count: hidden.length })}
          </span>
        </Tip>
      )}
    </div>
  );
}

export function BackgroundTasksBanner({
  activeBgTasks,
  bgTasks,
}: BackgroundTasksBannerProps) {
  const { t_i18n } = useFormatter();
  const [now, setNow] = useState(() => Date.now());
  // Per-task dismissal so acknowledging one completion notice never hides a
  // later one (and so completions surface even while other tasks are running).
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), REFETCH_INTERVAL_FAST);
    return () => clearInterval(id);
  }, []);

  // Keep the dismissed set from growing without bound — drop ids no longer
  // present in the fetched task window.
  useEffect(() => {
    if (!bgTasks) return;
    setDismissedIds((prev) => {
      if (prev.size === 0) return prev;
      const live = new Set(bgTasks.map((t) => t.task_id));
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [bgTasks]);

  const recent = (bgTasks ?? []).filter(
    (t) =>
      (t.status === "completed" || t.status === "failed" || t.status === "incomplete") &&
      t.completed_at &&
      now - new Date(t.completed_at).getTime() < COMPLETED_RECENCY_MS &&
      !dismissedIds.has(t.task_id),
  );
  const completedTasks = recent.filter((t) => t.status === "completed");
  const failedTasks = recent.filter((t) => t.status === "failed");
  const incompleteTasks = recent.filter((t) => t.status === "incomplete");

  const dismissRecent = () =>
    setDismissedIds((prev) => {
      const next = new Set(prev);
      recent.forEach((t) => next.add(t.task_id));
      return next;
    });

  const pillTone = (status: string): "completed" | "failed" | "incomplete" =>
    status === "completed" ? "completed" : status === "incomplete" ? "incomplete" : "failed";

  const bannerIsSuccess = completedTasks.length > 0 && failedTasks.length === 0 && incompleteTasks.length === 0;
  const activeRow = useMeasuredWidth<HTMLDivElement>();

  return (
    <>
      {/* Active background tasks */}
      {activeBgTasks.length > 0 && (
        <div className="border-b border-elevation-subtle-soft bg-accent/[0.02] px-4 py-1">
          <div ref={activeRow.ref} className="max-w-3xl mx-auto flex min-w-0 items-center gap-2" data-testid="background-tasks-bar">
            <GitFork className="h-3.5 w-3.5 text-accent shrink-0" />
            <p className="shrink-0 whitespace-nowrap content-caption text-accent/80 font-medium">
              {activeRow.width > 0 && activeRow.width < SHORT_LABEL_BELOW_PX
                ? t_i18n("{count, plural, one {# task running} other {# tasks running}}", { count: activeBgTasks.length })
                : t_i18n("{count, plural, one {# background task running} other {# background tasks running}}", { count: activeBgTasks.length })}
            </p>
            <TaskStrip tasks={activeBgTasks} toneOf={() => "active"} counterClassName="text-accent/70" />
          </div>
        </div>
      )}

      {/* Completed / failed / incomplete notification — fires per task, even
          while other tasks are still running, so a finished task is always
          surfaced. Incomplete is NOT "completed": the run died waiting. */}
      {recent.length > 0 && (
        <div
          className={cn(
            "border-b border-elevation-subtle-soft px-4 py-1",
            bannerIsSuccess
              ? "bg-feedback-success-primary/[0.02]"
              : incompleteTasks.length > 0 && failedTasks.length === 0
                ? "bg-feedback-alert-primary/[0.03]"
                : "bg-feedback-error-primary/[0.03]",
          )}
        >
          <div className="max-w-3xl mx-auto flex min-w-0 items-center gap-2">
            {bannerIsSuccess ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary shrink-0" />
            ) : incompleteTasks.length > 0 && failedTasks.length === 0 ? (
              <Hourglass className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
            ) : (
              <XCircle className="h-3.5 w-3.5 text-feedback-error-primary shrink-0" />
            )}
            <p
              className={cn(
                "min-w-0 truncate content-caption font-medium",
                bannerIsSuccess
                  ? "text-feedback-success-primary/80"
                  : incompleteTasks.length > 0 && failedTasks.length === 0
                    ? "text-feedback-warning-primary"
                    : "text-feedback-error-primary/80",
              )}
            >
              {completedTasks.length > 0 &&
                t_i18n("{count, plural, one {# background task completed} other {# background tasks completed}}", { count: completedTasks.length })}
              {completedTasks.length > 0 && (failedTasks.length > 0 || incompleteTasks.length > 0) && ", "}
              {incompleteTasks.length > 0 &&
                t_i18n("{count, plural, one {# background task did not finish} other {# background tasks did not finish}}", { count: incompleteTasks.length })}
              {incompleteTasks.length > 0 && failedTasks.length > 0 && ", "}
              {failedTasks.length > 0 &&
                t_i18n("{count, plural, one {# task failed} other {# tasks failed}}", { count: failedTasks.length })}
            </p>
            <TaskStrip
              tasks={recent}
              toneOf={(task) => pillTone(task.status)}
              counterClassName="text-default-secondary"
            />
            <button
              type="button"
              onClick={dismissRecent}
              aria-label="Dismiss completed task notifications"
              className="ml-auto shrink-0 text-default-secondary hover:text-default-primary transition-colors"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        </div>
      )}
    </>
  );
}
