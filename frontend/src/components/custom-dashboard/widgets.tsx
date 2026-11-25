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
 * One component per widget kind of a custom dashboard (`DASHBOARD_WIDGET_KINDS`).
 *
 * An agent picks the widgets and fills their values; everything else is here,
 * drawn with the design system: the surfaces are `Card`s, every colour is a
 * token chosen from the widget's `tone`, every link goes through the router
 * (`isSafeDashboardHref` already kept to the web app's pages), and every text
 * is rendered as React text, inline markup tokenized into nodes, never as HTML.
 * The components only meet the shapes `normalizeDashboard` answers.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Blocks,
  Check,
  CheckCircle2,
  Circle,
  Ellipsis,
  Minus,
  Sparkles,
  X,
} from "lucide-react";

import { AnimatedNumber } from "@/components/ui/animated-number";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { parseInlineMarkup, type MarkupSpanKind } from "@/components/presentation/markup";
import { useFormatter } from "@/i18n";
import { cn, formatCompactNumber } from "@/lib/utils";

import { useDashboardAsk } from "./ask";
import MarkdownText from "./charts/MarkdownText";
import { InputControl } from "./DashboardTokens";
import { useDashboardTokens, usePageTokens } from "./token-state";
import { DataWidget, PlatformWidget } from "./PlatformWidget";
import { fillOrKeep } from "./tokens";
import {
  sparklinePoints,
  type DashboardAction,
  type DashboardStageState,
  type DashboardStat,
  type DashboardTone,
  type DashboardWidget,
  type DashboardWidgetKind,
} from "./spec";

// ── Tones ────────────────────────────────────────────────────────────────────

/** Text in the tone's colour. */
const TONE_TEXT: Record<DashboardTone, string> = {
  neutral: "text-default-primary",
  accent: "text-accent",
  ai: "text-filigran-ia-primary",
  success: "text-feedback-success-primary",
  warning: "text-feedback-warning-primary",
  danger: "text-feedback-error-primary",
  info: "text-feedback-info-primary",
};

/** A dot or a bar filled with the tone. */
const TONE_FILL: Record<DashboardTone, string> = {
  neutral: "bg-feedback-neutral-primary",
  accent: "bg-accent",
  ai: "bg-filigran-ia-primary",
  success: "bg-feedback-success-primary",
  warning: "bg-feedback-warning-primary",
  danger: "bg-feedback-error-primary",
  info: "bg-feedback-info-primary",
};

/** A chip: the status surfaces carry the meaning and the text the contrast,
 *  as `Badge` does; the accent and AI tints keep their own hue. */
const TONE_CHIP: Record<DashboardTone, string> = {
  neutral: "border-transparent bg-elevation-hover text-default-primary",
  accent: "border-accent/30 bg-accent/10 text-accent",
  ai: "border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary",
  success: "bg-alert-success border-alert-success text-default-primary",
  warning: "bg-alert-warning border-alert-warning text-default-primary",
  danger: "bg-alert-error border-alert-error text-default-primary",
  info: "bg-alert-info border-alert-info text-default-primary",
};

/** A whole card leaning on the tone (a callout, a decision). */
const TONE_SURFACE: Record<DashboardTone, string> = {
  neutral: "",
  accent: "border-accent/30 bg-accent/10",
  ai: "border-filigran-ia-primary/40 bg-filigran-ia-primary/10",
  success: "border-alert-success bg-alert-success",
  warning: "border-alert-warning bg-alert-warning",
  danger: "border-alert-error bg-alert-error",
  info: "border-alert-info bg-alert-info",
};

/** The `[[tone:text]]` spans of the inline markup grammar. */
const SPAN_TEXT: Record<MarkupSpanKind, string> = {
  accent: "text-accent",
  info: "text-feedback-info-primary",
  success: "text-feedback-success-primary",
  warning: "text-feedback-warning-primary",
  error: "text-feedback-error-primary",
  muted: "text-default-secondary",
  gradient: "text-filigran-ia-primary",
};

// ── Shared parts ─────────────────────────────────────────────────────────────

/** A text field with its inline markup, as React nodes. */
function DashboardText({ value }: { value: string }) {
  return (
    <>
      {parseInlineMarkup(value).map((segment, index) => {
        switch (segment.kind) {
          case "bold":
            return (
              <strong key={index} className="font-semibold text-default-primary">
                {segment.text}
              </strong>
            );
          case "em":
            return <em key={index}>{segment.text}</em>;
          case "code":
            return (
              <code key={index} className="rounded-sm bg-elevation-hover px-1 font-mono-plex text-[0.9em]">
                {segment.text}
              </code>
            );
          case "span":
            return (
              <span key={index} className={SPAN_TEXT[segment.tone]}>
                {segment.text}
              </span>
            );
          case "br":
            return <br key={index} />;
          default:
            return <span key={index}>{segment.text}</span>;
        }
      })}
    </>
  );
}

function ToneChip({ tone, children, className }: { tone: DashboardTone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono-plex text-[11px] font-medium whitespace-nowrap",
        TONE_CHIP[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

function Eyebrow({ children, tone = "neutral" }: { children: ReactNode; tone?: DashboardTone }) {
  return (
    <span
      className={cn(
        "font-mono-plex text-[11px] font-medium uppercase tracking-wider",
        tone === "neutral" ? "text-default-secondary" : TONE_TEXT[tone],
      )}
    >
      {children}
    </span>
  );
}

/** A control: a router link to a platform page, or a button that asks. An ask
 *  with no assistant around (a read-only surface) is not drawn. */
export function ActionControl({
  action,
  priority = "secondary",
  className,
}: {
  action: DashboardAction;
  priority?: "primary" | "secondary" | "link";
  className?: string;
}) {
  const ask = useDashboardAsk();
  const variant = priority === "primary" ? "default" : priority === "link" ? "link" : "outline";
  const content =
    priority === "link" ? (
      <>
        {action.label}
        <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
      </>
    ) : (
      action.label
    );
  if (action.href) {
    return (
      <Button asChild variant={variant} size="sm" className={cn("min-h-9 gap-1", className)}>
        <Link to={action.href}>{content}</Link>
      </Button>
    );
  }
  if (!action.prompt || !ask) return null;
  const prompt = action.prompt;
  return (
    <Button type="button" variant={variant} size="sm" className={cn("min-h-9 gap-1", className)} onClick={() => ask(prompt)}>
      {priority !== "link" && <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />}
      {content}
    </Button>
  );
}

function Actions({ actions, priority }: { actions: DashboardAction[]; priority?: "first" | "link" }) {
  if (actions.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {actions.map((action, index) => (
        <ActionControl
          key={`${action.label}-${index}`}
          action={action}
          priority={priority === "link" ? "link" : priority === "first" && index === 0 ? "primary" : "secondary"}
          className={priority === "first" ? "flex-1 basis-32" : undefined}
        />
      ))}
    </div>
  );
}

/** A row's whole area is its link when it has one. */
function MaybeLink({ href, className, children }: { href?: string; className: string; children: ReactNode }) {
  if (!href) return <div className={className}>{children}</div>;
  return (
    <Link to={href} className={cn(className, "transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus")}>
      {children}
    </Link>
  );
}

function StatTile({ stat }: { stat: DashboardStat }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-sm bg-elevation-hover px-3 py-2.5">
      <span className={cn("text-xl font-semibold leading-tight", TONE_TEXT[stat.tone])}>{stat.value}</span>
      <span className="content-caption text-default-secondary">{stat.label}</span>
      {stat.caption && <span className="content-caption text-default-disabled">{stat.caption}</span>}
    </div>
  );
}

function Sparkline({ series, label }: { series: number[]; label: string }) {
  const points = sparklinePoints(series, 320, 56);
  if (points.length === 0) return null;
  return (
    <svg
      viewBox="0 0 320 56"
      preserveAspectRatio="none"
      className="h-14 w-full text-accent"
      role="img"
      aria-label={label}
    >
      <polyline
        points={points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
        fill="none"
        stroke="currentColor"
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

const TREND_ICONS = { up: ArrowUp, down: ArrowDown, flat: Minus } as const;

/** @i18n-keys */
const STAGE_STATE_LABELS = {
  done: "Done",
  active: "In progress",
  pending: "Pending",
  blocked: "Blocked",
} as const;

const STAGE_BAR: Record<DashboardStageState, string> = {
  done: "bg-feedback-success-primary",
  active: "bg-accent",
  pending: "bg-accent",
  blocked: "bg-feedback-error-primary",
};

const STAGE_STATUS: Record<DashboardStageState, string> = {
  done: "text-feedback-success-primary",
  active: "text-accent",
  pending: "text-default-secondary",
  blocked: "text-feedback-error-primary",
};

/** The part of the bar a stage fills: its progress, else full when done or active. */
function stageFill(state: DashboardStageState, progress: number | undefined): number {
  if (progress !== undefined) return progress;
  return state === "pending" ? 0 : 1;
}

// ── Widgets ──────────────────────────────────────────────────────────────────

type WidgetOf<K extends DashboardWidgetKind> = Extract<DashboardWidget, { kind: K }>;

function sameText(a: string | undefined, b: string | undefined): boolean {
  return !!a && !!b && a.trim().toLowerCase() === b.trim().toLowerCase();
}

function ScoreWidget({ widget }: { widget: WidgetOf<"score"> }) {
  const { t_i18n } = useFormatter();
  const TrendIcon = widget.trend ? TREND_ICONS[widget.trend.direction] : null;
  return (
    <Card className="flex h-full flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          {/* The frame already shows a title that says the same. */}
          {!sameText(widget.title, widget.label) && (
            <span className="content-compact text-default-secondary">{widget.label}</span>
          )}
          <div className="flex items-baseline gap-1.5">
            <span className="text-5xl font-bold leading-none tracking-tight text-default-primary">
              <AnimatedNumber value={widget.value} formatter={formatCompactNumber} />
            </span>
            {widget.unit && <span className="content-base text-default-secondary">{widget.unit}</span>}
            {widget.max !== undefined && (
              <span className="content-base text-default-secondary">
                <span aria-hidden="true">/{formatCompactNumber(widget.max)}</span>
                <span className="sr-only">{t_i18n("out of {max}", { max: formatCompactNumber(widget.max) })}</span>
              </span>
            )}
          </div>
        </div>
        {widget.trend && TrendIcon && (
          <ToneChip tone={widget.trend.tone} className="shrink-0">
            <TrendIcon className="h-3 w-3" aria-hidden="true" />
            {widget.trend.label}
          </ToneChip>
        )}
      </div>
      {widget.series.length >= 2 && <Sparkline series={widget.series} label={widget.seriesLabel ?? widget.label} />}
      {widget.stats.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(5.5rem,1fr))] gap-2">
          {widget.stats.map((stat, index) => (
            <StatTile key={`${stat.label}-${index}`} stat={stat} />
          ))}
        </div>
      )}
    </Card>
  );
}

function StatsWidget({ widget }: { widget: WidgetOf<"stats"> }) {
  return (
    <div className="grid h-full grid-cols-[repeat(auto-fit,minmax(7rem,1fr))] gap-2">
      {widget.items.map((stat, index) => (
        <Card key={`${stat.label}-${index}`} className="flex flex-col gap-1 p-3">
          <span className={cn("text-xl font-semibold leading-tight", TONE_TEXT[stat.tone])}>{stat.value}</span>
          <span className="content-caption text-default-secondary">{stat.label}</span>
          {stat.caption && <span className="content-caption text-default-disabled">{stat.caption}</span>}
        </Card>
      ))}
    </div>
  );
}

function StagesWidget({ widget }: { widget: WidgetOf<"stages"> }) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="h-full p-4">
      <ol className="flex gap-1.5">
        {widget.items.map((stage, index) => {
          const fill = stageFill(stage.state, stage.progress);
          return (
            <li key={`${stage.label}-${index}`} className="flex min-w-0 flex-1 flex-col gap-1.5">
              <div
                className="h-1.5 w-full overflow-hidden rounded-full bg-elevation-hover"
                role="progressbar"
                aria-label={stage.label}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(fill * 100)}
              >
                <div className={cn("h-full rounded-full", STAGE_BAR[stage.state])} style={{ width: `${fill * 100}%` }} />
              </div>
              <span
                className={cn(
                  "truncate text-[11px]",
                  stage.state === "active" ? "font-semibold text-default-primary" : "text-default-secondary",
                )}
              >
                {stage.label}
              </span>
              <span className={cn("truncate text-[11px]", STAGE_STATUS[stage.state])}>
                {stage.status ?? t_i18n(STAGE_STATE_LABELS[stage.state])}
              </span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function DecisionWidget({ widget }: { widget: WidgetOf<"decision"> }) {
  return (
    <Card className={cn("flex h-full flex-col gap-3 p-5", TONE_SURFACE.ai)}>
      {(widget.eyebrow || widget.time) && (
        <div className="flex flex-wrap items-center gap-2">
          {widget.eyebrow && <Eyebrow tone="ai">{widget.eyebrow}</Eyebrow>}
          {widget.time && <span className="content-caption text-default-secondary">· {widget.time}</span>}
        </div>
      )}
      <p className="content-base leading-relaxed text-default-primary">
        <DashboardText value={widget.text} />
      </p>
      {widget.tags.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {widget.tags.map((tag, index) => (
            <ToneChip key={`${tag}-${index}`} tone="ai" className="font-sans-plex">
              {tag}
            </ToneChip>
          ))}
        </div>
      )}
      <Actions actions={widget.actions} priority="first" />
    </Card>
  );
}

function FeedWidget({ widget }: { widget: WidgetOf<"feed"> }) {
  return (
    <Card className="h-full overflow-hidden">
      <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]">
        {widget.items.map((item, index) => (
          <li key={`${item.title}-${index}`}>
            <MaybeLink href={item.href} className="flex gap-3 px-4 py-3.5">
              {item.badge && (
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-elevation-hover font-mono-plex text-[11px] font-medium text-filigran-ia-primary">
                  {item.badge}
                </span>
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate content-compact font-semibold text-default-primary">{item.title}</span>
                  {item.time && (
                    <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">{item.time}</span>
                  )}
                </div>
                {item.text && (
                  <span className="content-compact leading-snug text-default-secondary">
                    <DashboardText value={item.text} />
                  </span>
                )}
              </div>
            </MaybeLink>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function CalloutWidget({ widget }: { widget: WidgetOf<"callout"> }) {
  return (
    <Card className={cn("flex h-full flex-col gap-2.5 p-5", TONE_SURFACE[widget.tone])}>
      {widget.eyebrow && <span className="content-compact text-default-secondary">{widget.eyebrow}</span>}
      <p className="content-base leading-relaxed text-default-primary">
        <DashboardText value={widget.text} />
      </p>
      <Actions actions={widget.actions} priority="link" />
    </Card>
  );
}

function RankedListWidget({ widget }: { widget: WidgetOf<"ranked_list"> }) {
  return (
    <Card className="h-full overflow-hidden">
      <ol className="divide-y divide-[var(--border-elevation-subtle-soft)]">
        {widget.items.map((item, index) => (
          <li key={`${item.title}-${index}`}>
            <MaybeLink href={item.href} className="flex items-center gap-3 px-4 py-3">
              {widget.numbered && (
                <span className="w-4 shrink-0 font-mono-plex content-compact text-default-secondary">{index + 1}</span>
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="content-compact font-semibold text-default-primary">{item.title}</span>
                {item.meta && <span className="content-caption text-default-secondary">{item.meta}</span>}
              </div>
              {item.badge && <ToneChip tone={item.badge.tone}>{item.badge.label}</ToneChip>}
            </MaybeLink>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function StepsWidget({ widget }: { widget: WidgetOf<"steps"> }) {
  return (
    <Card className="flex h-full flex-col gap-3 p-5">
      {widget.caption && <span className="content-caption text-default-secondary">{widget.caption}</span>}
      <ol className="flex flex-col">
        {widget.items.map((step, index) => (
          <li key={`${step.label}-${index}`} className="flex gap-3">
            <div className="flex w-5 flex-col items-center" aria-hidden="true">
              <span className={cn("mt-1 h-3 w-3 shrink-0 rounded-full", TONE_FILL[step.tone])} />
              {index < widget.items.length - 1 && <span className="w-0.5 flex-1 bg-elevation-hover" />}
            </div>
            <div className="flex flex-1 flex-col gap-0.5 pb-3">
              <span className="content-compact font-medium text-default-primary">{step.label}</span>
              {step.detail && <span className={cn("content-caption", step.tone === "neutral" ? "text-default-secondary" : TONE_TEXT[step.tone])}>{step.detail}</span>}
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function ChecklistWidget({ widget }: { widget: WidgetOf<"checklist"> }) {
  const { t_i18n } = useFormatter();
  const done = widget.items.filter((item) => item.done).length;
  return (
    <Card className="flex h-full flex-col gap-3 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {widget.eyebrow && <Eyebrow tone="ai">{widget.eyebrow}</Eyebrow>}
        <span className="content-caption text-default-secondary">
          {t_i18n("{done} of {total} done", { done, total: widget.items.length })}
        </span>
      </div>
      <ul className="flex flex-col gap-2.5">
        {widget.items.map((item, index) => (
          <li key={`${item.label}-${index}`} className="flex items-start gap-2.5 content-compact leading-snug">
            {item.done ? (
              <CheckCircle2 className="mt-px h-4 w-4 shrink-0 text-feedback-success-primary" aria-label={t_i18n("Done")} />
            ) : (
              <Circle className="mt-px h-4 w-4 shrink-0 text-default-disabled" aria-label={t_i18n("Pending")} />
            )}
            <span className={item.done ? "text-default-primary" : "text-default-secondary"}>
              <DashboardText value={item.label} />
            </span>
          </li>
        ))}
      </ul>
      <Actions actions={widget.actions} priority="first" />
    </Card>
  );
}

const LINE_ICONS: Record<DashboardTone, typeof Check> = {
  neutral: Ellipsis,
  accent: Ellipsis,
  ai: Ellipsis,
  info: Ellipsis,
  success: Check,
  warning: X,
  danger: X,
};

function ProgressWidget({ widget }: { widget: WidgetOf<"progress"> }) {
  const percent = widget.progress === undefined ? undefined : Math.round(widget.progress * 100);
  return (
    <Card className="flex h-full flex-col gap-3 p-5">
      {widget.status && (
        <div className="flex items-center gap-2">
          <span className={cn("h-2.5 w-2.5 rounded-full", TONE_FILL[widget.status.tone])} aria-hidden="true" />
          <ToneChip tone={widget.status.tone}>{widget.status.label}</ToneChip>
        </div>
      )}
      {widget.text && (
        <p className="content-compact leading-relaxed text-default-secondary">
          <DashboardText value={widget.text} />
        </p>
      )}
      {percent !== undefined && (
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between gap-2 content-caption text-default-secondary">
            <span>{widget.progressLabel}</span>
            <span>{percent}%</span>
          </div>
          <div
            className="h-1.5 overflow-hidden rounded-full bg-elevation-hover"
            role="progressbar"
            aria-label={widget.progressLabel ?? widget.title}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
          >
            <div className="h-full rounded-full bg-accent" style={{ width: `${percent}%` }} />
          </div>
        </div>
      )}
      {widget.lines.length > 0 && (
        <ul className="flex flex-col gap-1.5 rounded-sm bg-elevation-hover px-3 py-2.5 font-mono-plex text-[12px] text-default-secondary">
          {widget.lines.map((line, index) => {
            const Icon = LINE_ICONS[line.tone];
            return (
              <li key={`${line.label}-${index}`} className="flex items-start gap-2">
                <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", TONE_TEXT[line.tone])} aria-hidden="true" />
                <span>{line.label}</span>
              </li>
            );
          })}
        </ul>
      )}
      <Actions actions={widget.actions} />
    </Card>
  );
}

function PromptsWidget({ widget }: { widget: WidgetOf<"prompts"> }) {
  const { t_i18n } = useFormatter();
  const ask = useDashboardAsk();
  if (!ask) return null;
  return (
    <div className="flex h-full flex-col gap-2">
      <button
        type="button"
        onClick={() => ask()}
        className="flex min-h-12 items-center gap-2.5 rounded-md border border-elevation-subtle layer-1 bg-elevation-default px-4 text-left content-base text-default-secondary transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <Sparkles className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <span className="flex-1 truncate">{widget.placeholder ?? t_i18n("Ask anything...")}</span>
      </button>
      <div className="flex flex-wrap gap-2">
        {widget.suggestions.map((suggestion, index) => (
          <button
            key={`${suggestion}-${index}`}
            type="button"
            onClick={() => ask(suggestion)}
            className="min-h-11 rounded-md border border-elevation-subtle layer-1 bg-elevation-default px-3.5 py-2 text-left content-compact text-default-primary transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}

/** An input placed among the widgets: drawn from the page's inputs. */
function InputWidget({ widget }: { widget: Extract<DashboardWidget, { kind: "input" }> }) {
  const input = useDashboardTokens()?.inputs.get(widget.inputId);
  if (!input) return <UnsupportedWidget kind="input" />;
  return (
    <Card className="flex h-full items-center p-4">
      <InputControl input={input} />
    </Card>
  );
}

function TextWidget({ widget }: { widget: WidgetOf<"text"> }) {
  // A text may name the page's tokens (`$team$`): written in, an unset one left visible.
  const tokens = usePageTokens();
  if (widget.format === "markdown") {
    return (
      <Card className="h-full overflow-auto p-6">
        <MarkdownText text={fillOrKeep(widget.body, tokens)} className="content-base text-default-primary" />
      </Card>
    );
  }
  return (
    <Card className="h-full p-5">
      <div className="flex flex-col gap-3 content-base leading-relaxed text-default-primary">
        {fillOrKeep(widget.body, tokens).split(/\n{2,}/).map((paragraph, index) => (
          <p key={index}>
            <DashboardText value={paragraph} />
          </p>
        ))}
      </div>
    </Card>
  );
}

/** A widget this build cannot draw: said, never guessed at. */
function UnsupportedWidget({ kind }: { kind: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="flex h-full items-center gap-3 border-dashed p-4">
      <Blocks className="h-4 w-4 shrink-0 text-default-secondary" aria-hidden="true" />
      <div className="flex min-w-0 flex-col">
        <span className="content-compact text-default-primary">{t_i18n("This widget cannot be shown in this version.")}</span>
        <span className="content-caption text-default-secondary">{t_i18n("Widget type: {kind}", { kind })}</span>
      </div>
    </Card>
  );
}

/** The component of each kind; `backend/tests/test_custom_dashboards.py` pins
 *  the kinds against the validator's catalog. */
export function WidgetBody({ widget }: { widget: DashboardWidget }) {
  switch (widget.kind) {
    case "score":
      return <ScoreWidget widget={widget} />;
    case "stats":
      return <StatsWidget widget={widget} />;
    case "stages":
      return <StagesWidget widget={widget} />;
    case "decision":
      return <DecisionWidget widget={widget} />;
    case "feed":
      return <FeedWidget widget={widget} />;
    case "callout":
      return <CalloutWidget widget={widget} />;
    case "ranked_list":
      return <RankedListWidget widget={widget} />;
    case "steps":
      return <StepsWidget widget={widget} />;
    case "checklist":
      return <ChecklistWidget widget={widget} />;
    case "progress":
      return <ProgressWidget widget={widget} />;
    case "prompts":
      return <PromptsWidget widget={widget} />;
    case "text":
      return <TextWidget widget={widget} />;
    case "platform":
      return <PlatformWidget widget={widget} />;
    case "data":
      return <DataWidget widget={widget} />;
    case "input":
      return <InputWidget widget={widget} />;
    default:
      return <UnsupportedWidget kind={widget.originalKind} />;
  }
}
