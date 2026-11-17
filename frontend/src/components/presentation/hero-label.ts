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
 * The user-facing names of a hero variant and an overlay strength -
 * the ONE place "gradient" becomes "Gradient", so the inspector's thumbnails,
 * its colour chips and every tooltip spell them alike (the counterpart of
 * `scheme-label.ts` for the two new catalogs).
 */
import { useFormatter } from "@/i18n";
import type { DeckHero, DeckHeroTone, DeckOverlay } from "@/types";

export function useHeroLabel(): (hero: DeckHero) => string {
  const { t_i18n } = useFormatter();
  return (hero) => {
    switch (hero) {
      case "gradient":
        return t_i18n("Gradient");
      case "split":
        return t_i18n("Split");
      case "spotlight":
        return t_i18n("Spotlight");
      case "mosaic":
        return t_i18n("Mosaic");
      case "cascade":
        return t_i18n("Cascade");
      case "horizon":
        return t_i18n("Horizon");
      case "pillar":
        return t_i18n("Pillar");
      default:
        return t_i18n("Plain");
    }
  };
}

/**
 * The name of a hero colour: the template's gradient, or the palette entry
 * used alone - spelled by what the entry IS in the template (`Gradient start`,
 * never `gradient_from`).
 */
export function useHeroToneLabel(): (tone: DeckHeroTone) => string {
  const { t_i18n } = useFormatter();
  return (tone) => {
    switch (tone) {
      case "accent":
        return t_i18n("Accent");
      case "gradient_from":
        return t_i18n("Gradient start");
      case "gradient_to":
        return t_i18n("Gradient end");
      case "info":
        return t_i18n("Info");
      case "success":
        return t_i18n("Success");
      case "warning":
        return t_i18n("Warning");
      case "error":
        return t_i18n("Error");
      case "muted":
        return t_i18n("Muted");
      default:
        return t_i18n("Brand gradient");
    }
  };
}

export function useOverlayLabel(): (overlay: DeckOverlay) => string {
  const { t_i18n } = useFormatter();
  return (overlay) => {
    switch (overlay) {
      case "soft":
        return t_i18n("Soft");
      case "strong":
        return t_i18n("Strong");
      default:
        return t_i18n("None");
    }
  };
}
