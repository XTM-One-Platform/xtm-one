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
 * "Preview" dialog for a template container.
 *
 * Renders a lorem-ipsum deck built from the template's own theme, aspect ratio
 * and layout catalog, through the same renderer a real deck uses — so an admin
 * can judge a palette by looking at it instead of reading hex values, before any
 * agent generates anything. Nothing is persisted.
 *
 * The deck is a `PreviewDeck` (code-split: the Templates page must not pay for
 * reveal until someone actually clicks Preview) — the same compact toolbar as a
 * real deck, so the layouts are walked with the same controls and keys.
 *
 * A Dark / Light switch in the toolbar previews the template on a deck of the
 * other theme. A template that carries a palette for it
 * (`extra_metadata.theme_variants`) is drawn with that palette — without the
 * switch the one palette an admin could not inspect here would be exactly the
 * one they added. A template that carries none is drawn the way production
 * WOULD draw it (`previewThemeForDeck`) and the dialog says so: that theme is
 * not in `supported_schemes`, so no deck wearing the template is
 * offered it until a palette is added in Configuration — which is how an admin
 * sees whether their brand needs a variant before a user asks for a light deck.
 */
import { useMemo, useState } from "react";
import { Moon, Sun } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PresentationTheme, TemplateContainerDetail } from "@/types";

import { useDeckController } from "./deck-controller";
import { previewEscapeGuard } from "./deck-keys";
import { PreviewDeck } from "./PreviewDeck";
import { buildTemplatePreview, type ThemeScheme } from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";

const SCHEME_OPTIONS: { key: ThemeScheme; label: string; icon: React.ElementType }[] = [
  { key: "dark", label: "Preview the Dark theme", icon: Moon },
  { key: "light", label: "Preview the Light theme", icon: Sun },
];

const schemeOf = (theme: PresentationTheme): ThemeScheme =>
  theme === "light" ? "light" : "dark";

export function TemplatePreviewDialog({
  template,
  open,
  onOpenChange,
}: {
  template: TemplateContainerDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  // `null` = the scheme the template's own deck defaults produce.
  const [scheme, setScheme] = useState<ThemeScheme | null>(null);
  // Rebuilt whenever the template changes so an edit in the Configuration tab is
  // reflected the next time the dialog opens.
  const preview = useMemo(() => {
    const own = buildTemplatePreview(template);
    if (scheme === null || schemeOf(own.theme) === scheme) return own;
    // The other theme: a light deck for a dark-first template, a dark one for a
    // light-first template.
    return buildTemplatePreview(template, { theme: scheme });
  }, [template, scheme]);
  const layoutCount = preview.spec.slides.length;
  const activeScheme = schemeOf(preview.theme);
  const schemeLabel = useSchemeLabel();
  // Previewing a theme the template has no palette for: say what a deck would
  // get (nothing — the theme is not offered), not just how it would look.
  const unsupportedPreview = !template.supported_schemes.includes(activeScheme);
  // Held here so Escape can be arbitrated: it leaves the overview / fullscreen
  // first, and closes the dialog only when the deck has nothing to leave.
  const controller = useDeckController();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl" onEscapeKeyDown={previewEscapeGuard(controller)}>
        <DialogHeader>
          <DialogTitle>{t_i18n("Template preview")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Placeholder content in this template's design: one slide per layout it declares. Nothing is saved.",
            )}
          </DialogDescription>
        </DialogHeader>
        {unsupportedPreview && (
          <p className="rounded-sm bg-feedback-alert-primary/10 px-3 py-2 content-caption text-feedback-warning-tertiary">
            {t_i18n(
              "This template has no {scheme} palette, so decks wearing it are not offered the {scheme} theme. This is how the built-in {scheme} look would draw it; add a {scheme} palette in Configuration to support it.",
              { scheme: schemeLabel(activeScheme) },
            )}
          </p>
        )}
        <PreviewDeck
          presentation={preview}
          controller={controller}
          leading={
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate text-[11px] text-default-secondary">
                {t_i18n("{count} layouts previewed", { count: layoutCount })}
              </span>
              <span
                role="group"
                aria-label={t_i18n("Preview theme")}
                className="flex shrink-0 items-center gap-0.5 rounded-sm bg-elevation-hover p-0.5"
              >
                {SCHEME_OPTIONS.map(({ key, label, icon: Icon }) => {
                  const isActive = key === activeScheme;
                  return (
                    <Tip key={key} content={t_i18n(label)}>
                      <button
                        type="button"
                        aria-pressed={isActive}
                        aria-label={t_i18n(label)}
                        onClick={() => setScheme(key)}
                        className={cn(
                          "inline-flex h-6 w-6 items-center justify-center rounded-sm transition-colors",
                          isActive
                            ? "bg-elevation-highlight text-default-primary"
                            : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                        )}
                      >
                        <Icon className="h-3 w-3" />
                      </button>
                    </Tip>
                  );
                })}
              </span>
            </span>
          }
        />
      </DialogContent>
    </Dialog>
  );
}
