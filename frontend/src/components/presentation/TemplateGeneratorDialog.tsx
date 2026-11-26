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
 * "Generate with AI" dialog for template containers.
 *
 * A plain-language brief ("a light corporate deck for quarterly reviews, navy
 * and gold") becomes a complete template candidate - name, labels, palette,
 * layout catalog and agent guidance - previewed through the SAME deck renderer
 * a real presentation uses, before anything is saved. The candidate can be
 * refined in follow-up rounds ("warmer", "swap the fonts") and is only written
 * when the caller applies it to the form.
 *
 * The server runs the model's proposal through the template validators, so the
 * palette shown here is one the renderer honours; whatever was pruned is listed
 * as warnings rather than silently dropped.
 *
 * The preview is a `PreviewDeck` (code-split, so the Templates pages do not pay
 * for reveal until someone actually generates something).
 */
import { useId, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, ImageIcon, Plus, RefreshCw, Sparkles, X } from "lucide-react";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useGenerateTemplate } from "@/hooks/api/useTemplateContainerApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn, pluralize } from "@/lib/utils";
import type {
  GenerateTemplateResponse,
  PresentationThemeConfig,
  TemplateAsset,
  TemplateCandidate,
} from "@/types";

import { useDeckController } from "./deck-controller";
import { previewEscapeGuard } from "./deck-keys";
import { PALETTE_SWATCH_KEYS } from "./palette-keys";
import { PreviewDeck } from "./PreviewDeck";
import { buildTemplatePreview, candidateToTemplate, templateSupportedSchemes } from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";
import { Spinner } from "@filigran/design-system";

/** Quick-pick palette hints. Words, not hex: the model reasons better about
 *  "navy and gold" than about two hex codes with no relationship. */
const HINT_PRESETS = [
  "dark",
  "light",
  "navy",
  "corporate blue",
  "emerald",
  "warm earth tones",
  "monochrome",
  "high contrast",
] as const;

const NO_LOGO = "__none__";

/** The default for a caller with no assets (the New Template page). One shared
 *  array, not a fresh `[]` per render: the preview is memoised on `assets`, and
 *  a new preview each render made the deck re-sync, whose event re-rendered
 *  this dialog through its controller — an update loop. */
const NO_ASSETS: TemplateAsset[] = [];

/** Colour swatches for a palette (only the keys it sets). */
export function PaletteStrip({
  themeConfig: palette,
  size = "md",
}: {
  themeConfig: Record<string, unknown> | PresentationThemeConfig;
  size?: "sm" | "md";
}) {
  const themeConfig = palette as Record<string, unknown>;
  // Reading order: surfaces and ink, brand, tones, then the chart series and brand hues.
  const swatches = PALETTE_SWATCH_KEYS.filter((key) => typeof themeConfig[key] === "string");
  if (swatches.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {swatches.map((key) => (
        <Tip key={key} content={`${key}: ${String(themeConfig[key])}`}>
          <span
            className={cn(
              "inline-block rounded-sm border border-elevation-subtle-soft",
              size === "sm" ? "h-4 w-4" : "h-6 w-6",
            )}
            // Data-driven colour: the value comes from the palette itself.
            style={{ backgroundColor: String(themeConfig[key]) }}
          />
        </Tip>
      ))}
    </div>
  );
}

interface TemplateGeneratorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Template kind the candidate is generated for ("presentation", "report", ...). */
  kind: string;
  /** Name already typed in the form - kept over the model's suggestion. */
  name?: string;
  /** The template being edited, when refining an existing one. */
  current?: Partial<TemplateCandidate> | null;
  /** Assets of the template being edited - image ones can seed the palette. */
  assets?: TemplateAsset[];
  onApply: (candidate: TemplateCandidate) => void;
}

export function TemplateGeneratorDialog({
  open,
  onOpenChange,
  kind,
  name,
  current,
  assets = NO_ASSETS,
  onApply,
}: TemplateGeneratorDialogProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const generate = useGenerateTemplate();

  const fieldId = useId();
  const briefId = `${fieldId}-brief`;
  const hintId = `${fieldId}-hint`;
  const logoId = `${fieldId}-logo`;
  const [brief, setBrief] = useState("");
  const [hints, setHints] = useState<string[]>([]);
  const [hintInput, setHintInput] = useState("");
  const [logoFileId, setLogoFileId] = useState<string>(NO_LOGO);
  const [result, setResult] = useState<GenerateTemplateResponse | null>(null);
  const [refinement, setRefinement] = useState("");
  const roundRef = useRef(0);
  // The candidate preview's deck; held here so Escape leaves its overview /
  // fullscreen before it is allowed to close the dialog.
  const previewController = useDeckController();

  const hasCurrentDesign =
    !!current &&
    (Object.keys(current.theme_config ?? {}).length > 0 || (current.layouts?.length ?? 0) > 0);
  const [startFromCurrent, setStartFromCurrent] = useState(true);

  const imageAssets = useMemo(
    () => assets.filter((asset) => (asset.content_type ?? "").startsWith("image/")),
    [assets],
  );

  const preview = useMemo(
    () => (result ? buildTemplatePreview(candidateToTemplate(result.candidate, assets, roundRef.current)) : null),
    [result, assets],
  );
  // The themes the candidate would let a deck pick — read from its palettes,
  // the same way the saved template will be read.
  const schemeLabel = useSchemeLabel();
  const candidateSchemes = result
    ? templateSupportedSchemes({
        theme_config: result.candidate.theme_config,
        extra_metadata: result.candidate.extra_metadata,
      })
    : [];

  const reset = () => {
    setBrief("");
    setHints([]);
    setHintInput("");
    setLogoFileId(NO_LOGO);
    setResult(null);
    setRefinement("");
    roundRef.current = 0;
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const addHint = (value: string) => {
    const cleaned = value.trim();
    if (!cleaned || hints.includes(cleaned) || hints.length >= 8) return;
    setHints([...hints, cleaned]);
    setHintInput("");
  };

  const runGeneration = (text: string, base: Partial<TemplateCandidate> | null | undefined) => {
    generate.mutate(
      {
        brief: text,
        kind,
        palette_hints: hints.length > 0 ? hints : undefined,
        logo_file_id: logoFileId === NO_LOGO ? undefined : logoFileId,
        current: base ?? undefined,
        name: name?.trim() || undefined,
      },
      {
        onSuccess: (data) => {
          roundRef.current += 1;
          setResult(data);
          setRefinement("");
        },
        onError: (error) => {
          toast({
            title: t_i18n("Generation failed"),
            description: error.message,
            variant: "error",
          });
        },
      },
    );
  };

  const handleGenerate = () => {
    if (!brief.trim()) return;
    runGeneration(brief.trim(), hasCurrentDesign && startFromCurrent ? current : null);
  };

  const handleRefine = () => {
    if (!result || !refinement.trim()) return;
    runGeneration(refinement.trim(), result.candidate);
  };

  const handleApply = () => {
    if (!result) return;
    onApply(result.candidate);
    handleOpenChange(false);
  };

  const busy = generate.isPending;
  // FR-018: with AI off the generate controls are disabled and explained, not
  // left to fail on click.
  const aiOff = useAiActionDisabled();
  const candidate = result?.candidate;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-6xl" onEscapeKeyDown={previewEscapeGuard(previewController)}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent" />
            {t_i18n("Generate a template with AI")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Describe the style you want. The assistant drafts the palette, typefaces, layout catalog and agent guidance; you preview it here and apply it to the form. Nothing is saved until you do.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          {/* ── Brief ─────────────────────────────────────────────────── */}
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor={briefId}>{t_i18n("Design brief")}</Label>
              <textarea
                id={briefId}
                className="flex min-h-[110px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
                placeholder={t_i18n(
                  "e.g. A light, editorial deck for a quarterly business review at a fintech. Navy and gold, serif headings, calm and data-heavy.",
                )}
                disabled={busy}
                autoFocus
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor={hintId}>{t_i18n("Palette hints")}</Label>
              <div className="flex flex-wrap gap-1.5">
                {HINT_PRESETS.map((preset) => {
                  const active = hints.includes(preset);
                  return (
                    <button
                      key={preset}
                      type="button"
                      disabled={busy}
                      onClick={() => (active ? setHints(hints.filter((h) => h !== preset)) : addHint(preset))}
                      className={cn(
                        "rounded-sm border px-2 py-0.5 text-[11px] transition-colors",
                        active
                          ? "border-accent/40 bg-accent/15 text-accent"
                          : "border-elevation-subtle text-default-secondary hover:border-accent/30 hover:text-default-primary",
                      )}
                    >
                      {preset}
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center gap-2">
                <Input id={hintId} aria-label={t_i18n("Palette hints")}
                  value={hintInput}
                  onChange={(e) => setHintInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addHint(hintInput);
                    }
                  }}
                  placeholder={t_i18n("Add a colour or a mood, e.g. #0b1020 or terracotta")}
                  className="h-8 content-base"
                  disabled={busy}
                />
                <Button type="button" variant="outline" size="sm" className="h-8 gap-1" onClick={() => addHint(hintInput)} disabled={busy || !hintInput.trim()}>
                  <Plus className="h-3.5 w-3.5" /> {t_i18n("Add")}
                </Button>
              </div>
              {hints.some((hint) => !(HINT_PRESETS as readonly string[]).includes(hint)) && (
                <div className="flex flex-wrap gap-1.5">
                  {hints
                    .filter((hint) => !(HINT_PRESETS as readonly string[]).includes(hint))
                    .map((hint) => (
                      <Badge key={hint} variant="cyan" className="gap-1 pr-1 text-[11px] font-normal">
                        {hint}
                        <button
                          type="button"
                          className="rounded-full hover:bg-accent/20"
                          onClick={() => setHints(hints.filter((h) => h !== hint))}
                          aria-label={t_i18n("Remove")}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </Badge>
                    ))}
                </div>
              )}
            </div>

            {imageAssets.length > 0 && (
              <div className="space-y-1.5">
                <Label htmlFor={logoId}>{t_i18n("Sample colours from a logo")}</Label>
                <SearchableSelect
                  id={logoId}
                  aria-label={t_i18n("Sample colours from a logo")}
                  options={imageAssets.map((asset) => ({
                    value: asset.id,
                    label: asset.filename,
                    description: asset.asset_role ?? undefined,
                    icon: <ImageIcon className="h-3 w-3 text-default-secondary" />,
                  }))}
                  pinned={[{ value: NO_LOGO, label: t_i18n("No logo") }]}
                  value={logoFileId}
                  onValueChange={setLogoFileId}
                  disabled={busy}
                />
                <p className="content-caption text-default-secondary">
                  {t_i18n("The dominant colours of the image are handed to the assistant so the palette sits naturally around it.")}
                </p>
              </div>
            )}

            {hasCurrentDesign && !result && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                <div>
                  <p className="content-base font-medium">{t_i18n("Start from the current configuration")}</p>
                  <p className="text-[11px] text-default-secondary">
                    {t_i18n("Keep what the brief does not mention; turn off for a fresh design.")}
                  </p>
                </div>
                <Switch aria-label={t_i18n("Start from the current configuration")} checked={startFromCurrent} onCheckedChange={setStartFromCurrent} disabled={busy} />
              </div>
            )}

            {!result ? (
              <AiDisabledTip wrapClassName="block">
                <Button type="button" className="w-full gap-1.5" onClick={handleGenerate} disabled={busy || aiOff || !brief.trim()} aria-busy={busy || undefined}>
                  {busy && <Spinner size="sm" tone="inherit" />}{!busy && <Sparkles className="h-4 w-4" />}
                  {busy ? t_i18n("Generating...") : t_i18n("Generate")}
                </Button>
              </AiDisabledTip>
            ) : (
              <div className="space-y-2 rounded-sm border border-accent/20 bg-accent/5 p-3">
                <label className="content-compact font-medium text-accent">{t_i18n("Refine this result")}</label>
                <textarea
                  className="flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  value={refinement}
                  onChange={(e) => setRefinement(e.target.value)}
                  placeholder={t_i18n("e.g. Warmer accent, swap the heading font for a geometric sans, add a roadmap layout.")}
                  disabled={busy}
                />
                <div className="flex flex-wrap gap-2">
                  <AiDisabledTip>
                    <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={handleRefine} disabled={busy || aiOff || !refinement.trim()} aria-busy={busy || undefined}>
                      {busy && <Spinner size="sm" tone="inherit" />}{!busy && <RefreshCw className="h-3.5 w-3.5" />}
                      {t_i18n("Refine")}
                    </Button>
                  </AiDisabledTip>
                  <AiDisabledTip>
                    <Button type="button" variant="ghost" size="sm" className="gap-1.5" onClick={handleGenerate} disabled={busy || aiOff || !brief.trim()}>
                      <Sparkles className="h-3.5 w-3.5" /> {t_i18n("Regenerate from the brief")}
                    </Button>
                  </AiDisabledTip>
                </div>
              </div>
            )}
          </div>

          {/* ── Result + live preview ─────────────────────────────────── */}
          <div className="space-y-3">
            {!candidate ? (
              <div className="flex aspect-video w-full flex-col items-center justify-center gap-2 rounded-sm border border-dashed border-elevation-subtle bg-elevation-hover text-center">
                {busy ? (
                  <>
                    <Spinner size="lg" tone="inherit" className="text-accent" />
                    <p className="content-base text-default-secondary">{t_i18n("Designing the palette and layout catalog...")}</p>
                  </>
                ) : (
                  <>
                    <Sparkles className="h-6 w-6 text-default-secondary" />
                    <p className="content-base text-default-secondary">
                      {t_i18n("The live preview of the generated template appears here.")}
                    </p>
                  </>
                )}
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate content-base font-semibold">{candidate.name}</p>
                    {candidate.description && (
                      <p className="line-clamp-2 content-compact text-default-secondary">{candidate.description}</p>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {candidateSchemes.map((scheme) => (
                      <Badge key={scheme} variant="cyan" className="content-caption">{schemeLabel(scheme)}</Badge>
                    ))}
                    {candidate.aspect_ratio && <Badge variant="outline" className="content-caption">{candidate.aspect_ratio}</Badge>}
                    <Badge variant="outline" className="content-caption">
                      {candidate.layouts.length} {pluralize(candidate.layouts.length, "layout")}
                    </Badge>
                  </div>
                </div>

                <PaletteStrip themeConfig={candidate.theme_config} />

                {preview && (
                  <PreviewDeck
                    presentation={preview}
                    controller={previewController}
                    className={cn(busy && "opacity-60")}
                  />
                )}

                {candidate.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {candidate.tags.map((tag) => (
                      <Badge key={tag} variant="secondary" className="content-caption font-normal">{tag}</Badge>
                    ))}
                  </div>
                )}

                {typeof candidate.extra_metadata.guidance === "string" && (
                  <p className="rounded-sm bg-elevation-hover px-3 py-2 text-[11px] leading-relaxed text-default-secondary">
                    <span className="font-medium text-default-primary">{t_i18n("Agent guidance")}: </span>
                    {candidate.extra_metadata.guidance}
                  </p>
                )}

                {result && result.palette_from_logo.length > 0 && (
                  <div className="flex items-center gap-2 text-[11px] text-default-secondary">
                    <span>{t_i18n("Colours read from the logo")}:</span>
                    {result.palette_from_logo.map((color) => (
                      <Tip key={color} content={color}>
                        <span className="inline-block h-3.5 w-3.5 rounded-sm border border-elevation-subtle-soft" style={{ backgroundColor: color }} />
                      </Tip>
                    ))}
                  </div>
                )}

                {result && result.warnings.length > 0 && (
                  <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3">
                    <p className="mb-1 flex items-center gap-1.5 content-compact font-medium text-feedback-warning-primary">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      {t_i18n("{count, plural, one {# adjustment} other {# adjustments}} made to the proposal", {
                        count: result.warnings.length,
                      })}
                    </p>
                    <ul className="max-h-28 space-y-0.5 overflow-y-auto content-caption text-feedback-warning-primary">
                      {result.warnings.map((warning, index) => (
                        <li key={index}>· {warning}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-elevation-subtle pt-4">
          <p className="text-[11px] text-default-secondary">
            {result
              ? t_i18n("Applying fills the form; you can still edit every field before saving.")
              : t_i18n("Tip: mention the audience, the mood and two or three colours.")}
          </p>
          <div className="flex items-center gap-2">
            <Button type="button" variant="ghost" onClick={() => handleOpenChange(false)}>{t_i18n("Cancel")}</Button>
            <Button type="button" className="gap-1.5" onClick={handleApply} disabled={!result || busy}>
              <Check className="h-4 w-4" /> {t_i18n("Apply to form")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
