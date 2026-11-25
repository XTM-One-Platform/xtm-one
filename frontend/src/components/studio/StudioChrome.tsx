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
 * What the studios share around the work: the Presentation Studio viewer
 * (`PresentationViewer`, its deck and document bars), the Video Studio editor
 * (`VideoEditor`) and the Dashboard Studio (`DashboardStudio`) are one product, so the way back to the
 * library, the kind of work on the stage, its title, the studio toggle, the
 * share button and the screens shown before the work loads come from here,
 * never from a copy in each page.
 *
 * The bars speak the deck bar's vocabulary, the more crowded of the two (it
 * carries the slide navigation): the primary verb (`Present`, `Play`) and
 * whether the work is saved, beside its title (`StudioSaveState`), and
 * the studio toggle are labelled, every other action is an icon with its
 * tooltip, and the menu is `More actions`.
 */
import type { ReactNode } from "react";
import { AlertTriangle, ArrowLeft, Check, Pencil, Share2, Sparkles, type LucideIcon } from "lucide-react";

import { ToolbarVerb } from "@/components/presentation/DeckToolbar";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

/** An icon-only action of a studio bar: the 32px square every bar button lines up to. */
export const STUDIO_BAR_BUTTON = "h-8 w-8 px-0";

/** The stage's ground behind the work: a faint light at its centre, the video's preview and the dashboard's page alike. */
export const STUDIO_STAGE_BACKDROP =
  "bg-[radial-gradient(ellipse_at_center,color-mix(in_srgb,_var(--text-default-primary)_calc(0.05_*_100%),_transparent),transparent_70%)]";

/** The title's type, the deck's heading and the video's editable field alike. */
export const STUDIO_BAR_TITLE = "truncate px-1.5 content-base font-semibold text-default-primary";

/**
 * The title of the work, renamed in place in the bar: sized to its text like
 * the heading it replaces (`size` where `field-sizing` is not supported).
 */
export function StudioTitleField({
  value,
  onChange,
  onBlur,
  maxLength,
  testId,
}: {
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  maxLength?: number;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onBlur={onBlur}
      aria-label={t_i18n("Title (heading)")}
      maxLength={maxLength}
      size={Math.max(1, value.length)}
      className={cn(
        STUDIO_BAR_TITLE,
        "field-sizing-content min-w-24 max-w-[420px] rounded-sm bg-transparent py-1 hover:bg-elevation-hover focus:bg-elevation-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-focus",
      )}
      data-testid={testId}
    />
  );
}

/**
 * Whether the work on the stage is saved, beside its title: saved, unsaved,
 * being saved, or refused (its problems in the tooltip). Below 56rem of bar
 * (the bar is a `@container`) the word becomes screen-reader text and a
 * tooltip, so only the icon takes room.
 */
export function StudioSaveState({
  status,
  problems = [],
  testId,
}: {
  status: "saved" | "saving" | "unsaved" | "error";
  problems?: readonly { path: string; message: string }[];
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  const word = (text: string) => (
    <span className="@max-4xl:sr-only" data-visible-from="4xl" data-testid={`${testId}Word`}>
      {text}
    </span>
  );
  const line = "inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px] text-default-secondary";
  if (status === "saving")
    return (
      <Tip content={t_i18n("Saving...")}>
        <span className={line} role="status" data-testid={testId}>
          <Spinner size="sm" tone="inherit" /> {word(t_i18n("Saving..."))}
        </span>
      </Tip>
    );
  if (status === "unsaved")
    return (
      <Tip content={t_i18n("Unsaved changes")}>
        <span className={line} role="status" data-testid={testId}>
          <Pencil className="h-3 w-3 @4xl:hidden" aria-hidden /> {word(t_i18n("Unsaved changes"))}
        </span>
      </Tip>
    );
  if (status === "error")
    return (
      <Tip content={problems.map((p) => (p.path ? `${p.path}: ${p.message}` : p.message)).join("\n")}>
        <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap content-caption text-feedback-warning-primary" role="status" data-testid={testId}>
          <AlertTriangle className="h-3 w-3" aria-hidden /> {word(t_i18n("Not saved"))}
        </span>
      </Tip>
    );
  return (
    <Tip content={t_i18n("Saved")}>
      <span className={line} role="status" data-testid={testId}>
        <Check className="h-3 w-3 text-feedback-success-primary" aria-hidden /> {word(t_i18n("Saved"))}
      </span>
    </Tip>
  );
}

/**
 * The bar's leading region: the way back to the library, the kind of work on
 * the stage, then its title (`children`, typed with `STUDIO_BAR_TITLE`). The
 * back button and the icon never shrink: on a narrow bar the title is the part
 * that gives way, so the back button keeps its 32 px square (#9375, B2 of
 * #10528 r1).
 */
export function StudioBarLeading({
  backLabel,
  onBack,
  icon: Icon,
  children,
}: {
  backLabel: string;
  onBack: () => void;
  icon: LucideIcon;
  children: ReactNode;
}) {
  return (
    <>
      <Tip content={backLabel}>
        <Button variant="ghost" size="sm" onClick={onBack} aria-label={backLabel} className={`${STUDIO_BAR_BUTTON} shrink-0`}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
      </Tip>
      <Icon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      {children}
    </>
  );
}

/** The studio toggle: the rail out or folded, pressed while it is out. */
export function StudioToggle({
  open,
  onToggle,
  portalContainer,
  testId,
}: {
  open: boolean;
  onToggle: () => void;
  portalContainer?: HTMLElement | null;
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <ToolbarVerb
      icon={Sparkles}
      label={t_i18n("Studio")}
      tooltip={open ? t_i18n("Hide the studio") : t_i18n("Edit with my assistant")}
      onClick={onToggle}
      pressed={open}
      portalContainer={portalContainer}
      testId={testId}
    />
  );
}

export function StudioShareButton({
  onClick,
  portalContainer,
  testId,
}: {
  onClick: () => void;
  portalContainer?: HTMLElement | null;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={t_i18n("Share")} container={portalContainer}>
      <Button
        variant="ghost"
        size="sm"
        onClick={onClick}
        aria-label={t_i18n("Share")}
        className={STUDIO_BAR_BUTTON}
        data-testid={testId}
      >
        <Share2 className="h-4 w-4" />
      </Button>
    </Tip>
  );
}

/** While the work loads: the studio takes the whole screen, outside AppLayout. */
export function StudioLoading() {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex h-dvh items-center justify-center bg-elevation-default">
      <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
    </div>
  );
}

/**
 * The work cannot be opened. The backend answers 404 both for deleted work
 * and for work the caller was never granted, so `description` names both.
 */
export function StudioUnavailable({
  title,
  description,
  backLabel,
  onBack,
}: {
  title: string;
  description: string;
  backLabel: string;
  onBack: () => void;
}) {
  return (
    <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center">
      <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
      <p className="content-base font-medium">{title}</p>
      <p className="max-w-md content-base text-default-secondary">{description}</p>
      <Button variant="outline" size="sm" onClick={onBack} className="gap-1.5">
        <ArrowLeft className="h-3.5 w-3.5" /> {backLabel}
      </Button>
    </div>
  );
}
