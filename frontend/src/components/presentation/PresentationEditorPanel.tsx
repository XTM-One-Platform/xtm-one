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
 * The editor panel: the side of the slide editor that is not
 * the stage.
 *
 * The stage is where a deck is edited by hand - select a block,
 * type in place, arrange (`SlideEditLayer`). This panel is its inspector and
 * its settings, in three tabs over ONE draft (`useDeckEditor`, owned by the
 * viewer and shared with the stage):
 *
 *   - SLIDES: the filmstrip - every slide with its thumbnail, its layout
 *     picker (the server-side deterministic conversion), move,
 *     duplicate, delete, and "Add slide" by layout.
 *   - SELECTION: what is selected on the stage - a block's content, style and
 *     items, or the slide's own fields, notes and background
 *     (`SlideInspector`). Opens by itself when something is selected.
 *   - DECK: the title, the transition, the DESIGN (the template is the
 *     deck's design, the theme its colour scheme, limited to what the template
 *     supports) and the HEADER & FOOTER (one deck setting drawn on
 *     every slide, the template's pattern filled from the deck).
 *
 * Every change SAVES ITSELF (`useDeckAutosave`, owned by the viewer):
 * a `PATCH /presentations/{id}` with the edited PIECES and
 * `expected_revision` a moment after the person pauses. The server
 * re-validates it exactly like an agent's call; a collaborator - or an agent
 * - who saved meanwhile is merged into the draft (`deck-merge.ts`) and the
 * save goes again. The footer says where the work stands (saved, saving,
 * offline, a refusal and why) in place of a Save button; Undo / Redo walk
 * every change (`Ctrl+Z` / `Ctrl+Shift+Z`, `Ctrl+S` saves now), the History
 * panel every revision. The filmstrip shows who else is on which slide and
 * flashes the slides a collaborator just changed.
 *
 * The draft is PREVIEWED LIVE: `onDraftChange` hands the host the design a
 * template change brings (or `null` once the draft is pristine again).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  CloudOff,
  CopyPlus,
  Layers,
  MousePointerClick,
  Palette,
  PanelBottom,
  Plus,
  Redo2,
  RefreshCw,
  Settings2,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useSwitchSlideLayout } from "@/hooks/api/usePresentationApi";
import { useTemplateContainer, useTemplateContainers } from "@/hooks/api/useTemplateContainerApi";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn, formatDurationShort } from "@/lib/utils";
import type {
  DeckChrome,
  DeckHero,
  DeckLogoPosition,
  PresentationDraftOverlay,
  PresentationPresenceEntry,
  PresentationSlide,
  PresentationTheme,
  PresentationThemeConfig,
  PresentationThemeScheme,
  PresentationTransition,
  SlideLayout,
  TemplateChrome,
  TemplateContainer,
  TemplateDesign,
} from "@/types";

import { LAYOUT_ICONS } from "./block-icons";
import { MAX_FIELD_VALUE_CHARS, chromeText, declaresPattern, templateChromeOf, undeclaredFields } from "./chrome-pattern";
import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import { draftTemplateDesign } from "./design-tokens";
import { EDITOR_PANEL_ATTR } from "./edit-context";
import { SHARED_HEROES, coverHero, slideHero, templateHeroDefaults, templateHeroes } from "./hero";
import { SLIDE_LAYOUTS, layoutLabel, supportedLayouts } from "./layout-options";
import {
  canAddSlide,
  deckSlideCap,
  normalizeChrome,
  normalizeSlideLook,
  specWithDraft,
  type DeckEditor,
} from "./presentation-editor";
import {
  DECK_LOGO_POSITIONS,
  DECK_THEMES,
  DECK_TRANSITIONS,
  fitThemeToSchemes,
  previewThemeConfigs,
} from "./previewSpec";
import { SlideInspector } from "./SlideInspector";
import { slideHeading } from "./slide-label";
import { SLIDE_SCHEMA } from "./slide-schema";
import type { SlidePalette } from "./SlideStylePickers";
import { SlideThumbnail, type ThumbnailDesign } from "./SlideThumbnail";
import { deckSpeakingSeconds } from "./speaking-time";
import { useSchemeLabel } from "./scheme-label";
import { slideScheme } from "./theme";
import { ThemeSchemePicker } from "./ThemeSchemePicker";
import { UserInitials } from "./PresenceAvatars";
import type { DeckAutosave } from "./useDeckAutosave";
import { Spinner } from "@filigran/design-system";

/** How long the filmstrip flags the slides a collaborator just changed. */
const REMOTE_CHANGE_FLASH_MS = 4_000;

/** Radix `Select` cannot hold `null`: the "no template" option's value. */
const NO_TEMPLATE = "__none__";

/** The `templates` filter the Design picker reads: presentation
 *  templates only — an asset library or a report skin dresses no deck. Kept
 *  as one constant so the query key is stable across renders. */
const PRESENTATION_TEMPLATES_FILTER = { kind: "presentation" } as const;

const EMPTY_PALETTE: PresentationThemeConfig = {};

type PanelTab = "slides" | "selection" | "deck";

/**
 * One chrome edit, normalised so an untouched-looking deck IS untouched: a
 * switch back at its default (`true`) and a blanked text drop their key
 * instead of storing the default, and an emptied `fields` map goes with them.
 * Otherwise every round-trip through the switches would save a revision that
 * draws exactly the same slides.
 */
function withChrome(chrome: DeckChrome, patch: Partial<DeckChrome>): DeckChrome {
  return normalizeChrome({ ...chrome, ...patch } as DeckChrome);
}

function withChromeField(chrome: DeckChrome, key: string, value: string): DeckChrome {
  return normalizeChrome({
    ...chrome,
    fields: { ...(chrome.fields ?? {}), [key]: value },
  });
}

export function PresentationEditorPanel({
  editor,
  autosave,
  collaborators = [],
  activeIndex,
  onSelectSlide,
  onDraftChange,
  onClose,
  className,
}: {
  editor: DeckEditor;
  /** The session's autosave (owned by the viewer): what the footer reports. */
  autosave: DeckAutosave;
  /** The others with the deck open (presence): shown on the slides they are on. */
  collaborators?: PresentationPresenceEntry[];
  /** reveal's current slide, highlighted in the list. */
  activeIndex?: number;
  /** Jump the deck to a slide when its row is clicked. */
  onSelectSlide?: (index: number) => void;
  /**
   * Live preview: the deck as the draft would save it while there are unsaved
   * edits — the spec plus the design a template change brings — and `null`
   * once the draft matches the stored deck again (after Save, Discard, or when
   * the panel closes). The host lays it over the stored deck.
   */
  onDraftChange?: (draft: PresentationDraftOverlay | null) => void;
  onClose: () => void;
  className?: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const { toast } = useToast();
  const { presentation, draft, dirty, selection, lastRemoteMerge } = editor;
  // A page document (A4 / letter) has pages, not slides, and no transition
  // between them: the panel speaks its language and hides what does not apply.
  const isPage = presentation.kind === "page";
  const unit = isPage ? "page" : "slide";
  // The logo placements the opening and closing slides accept; body
  // slides keep the corner whatever is picked, which the hint says.
  const logoPositionLabel = (position: DeckLogoPosition): string => {
    switch (position) {
      case "start":
        return t_i18n("Left, with the title");
      case "center":
        return t_i18n("Centered");
      case "end":
        return t_i18n("Right corner");
      default:
        return t_i18n("Auto (follows the title alignment)");
    }
  };

  const [tab, setTab] = useState<PanelTab>("slides");
  // A selection made on the stage brings the inspector forward; a slide
  // selected from the list keeps the Slides tab (that is where it was done).
  const lastBlock = useRef<string | null>(null);
  useEffect(() => {
    if (selection?.block && selection.block !== lastBlock.current) setTab("selection");
    lastBlock.current = selection?.block ?? null;
  }, [selection]);

  // ── Design: the template and the theme it allows ──────────────
  // The presentation templates this reader may pick from — the same list the
  // agent is offered when it asks "which template?" — plus the one the deck is
  // linked to, which an admin editing a colleague's deck keeps.
  const { data: visibleTemplates } = useTemplateContainers(PRESENTATION_TEMPLATES_FILTER);
  const { data: allTemplates } = useTemplateContainers(PRESENTATION_TEMPLATES_FILTER, { scope: "all" });
  const templates = useMemo(
    () => withBoundEntities(
      visibleTemplates,
      allTemplates,
      presentation.template_container_id ? [presentation.template_container_id] : [],
    ) ?? [],
    [visibleTemplates, allTemplates, presentation.template_container_id],
  );
  const templateChanged = draft.template_container_id !== presentation.template_container_id;
  // The palette of a template being TRIED needs its detail (`theme_config`,
  // `theme_variants`, logo assets); the stored template's palette already
  // rides on the deck response, so nothing is fetched until the pick changes.
  const { data: triedTemplate } = useTemplateContainer(
    templateChanged && draft.template_container_id ? draft.template_container_id : undefined,
  );
  const pickedRow: TemplateContainer | undefined = draft.template_container_id
    ? templates.find((row) => row.id === draft.template_container_id)
    : undefined;
  // The stored template may be invisible to this reader (shared deck, private
  // template): it is then shown as the current pick, disabled, by the name the
  // server resolved (or as "a template you cannot see").
  const storedTemplateHidden =
    !!presentation.template_container_id && !templates.some((row) => row.id === presentation.template_container_id);
  const templateName: string | null = draft.template_container_id
    ? templateChanged
      ? (pickedRow?.name ?? triedTemplate?.name ?? null)
      : (presentation.template_name ?? pickedRow?.name ?? null)
    : null;
  // Which schemes the Theme picker offers: the stored deck's own answer while
  // the template is unchanged; the tried template's row (the list carries
  // `supported_schemes`, so the restriction is immediate); both for no template.
  const supportedSchemes: PresentationThemeScheme[] = !draft.template_container_id
    ? [...DECK_THEMES]
    : !templateChanged
      ? (presentation.supported_schemes ?? [...DECK_THEMES])
      : (pickedRow?.supported_schemes ?? triedTemplate?.supported_schemes ?? [...DECK_THEMES]);
  // Which covers the hero picker offers: the stored deck's own answer
  // while the template is unchanged; the tried template's declaration (its
  // list row carries `extra_metadata`); the shared covers for no template.
  const supportedHeroes: readonly DeckHero[] = !draft.template_container_id
    ? SHARED_HEROES
    : !templateChanged
      ? (presentation.supported_heroes ?? SHARED_HEROES)
      : templateHeroes((pickedRow ?? triedTemplate)?.extra_metadata);
  // The palette per scheme the stage previews under the draft's theme.
  const themeConfigs: Record<PresentationThemeScheme, PresentationThemeConfig> = useMemo(() => {
    if (!draft.template_container_id) return { dark: EMPTY_PALETTE, light: EMPTY_PALETTE };
    if (!templateChanged) {
      return {
        dark: presentation.theme_configs?.dark ?? presentation.theme_config,
        light: presentation.theme_configs?.light ?? presentation.theme_config,
      };
    }
    // Until the tried template's detail arrives, the stage shows the plain
    // look rather than the OLD template's colours under the new name.
    return triedTemplate ? previewThemeConfigs(triedTemplate) : { dark: EMPTY_PALETTE, light: EMPTY_PALETTE };
  }, [draft.template_container_id, templateChanged, presentation, triedTemplate]);

  // ── Header & footer: what the template defines, what the deck fills ──
  // The stored template's pattern rides on the deck response; a template being
  // tried is read from its list row (which carries `extra_metadata`), so the
  // footer changes with the pick, before the detail arrives.
  // Memoised: the overlay effect below is keyed on it, and a fresh object per
  // render would hand the host a new draft every render.
  const templateChrome: TemplateChrome | null = useMemo(
    () =>
      !draft.template_container_id
        ? null
        : !templateChanged
          ? (presentation.template_chrome ?? null)
          : templateChromeOf(pickedRow ?? triedTemplate),
    [draft.template_container_id, templateChanged, presentation.template_chrome, pickedRow, triedTemplate],
  );
  // The template's design tokens, resolved the same way, so a tried
  // template's flat look (or its absence) shows before the save.
  const templateDesign: TemplateDesign | null = useMemo(
    () =>
      draftTemplateDesign({
        templateContainerId: draft.template_container_id,
        templateChanged,
        stored: presentation.template_design,
        tried: pickedRow ?? triedTemplate,
      }),
    [draft.template_container_id, templateChanged, presentation.template_design, pickedRow, triedTemplate],
  );
  // The design the filmstrip thumbnails draw with: the draft's palette and
  // footer pattern, so a template tried in the Deck tab recolours the strip
  // as it recolours the stage. Memoised: the thumbnails compare it.
  const thumbnailDesign = useMemo<ThumbnailDesign>(
    () => ({
      template_container_id: draft.template_container_id,
      template_name: templateName,
      theme_config: themeConfigs[draft.theme] ?? EMPTY_PALETTE,
      theme_configs: themeConfigs,
      template_chrome: templateChrome,
      template_design: templateDesign,
    }),
    [draft.template_container_id, draft.theme, templateName, themeConfigs, templateChrome, templateDesign],
  );
  // The palette ONE slide draws with - its resolved scheme (its own
  // theme over the draft's) and the tried template's tokens for that scheme -
  // so the hero colour chips of a dark cover on a light deck are painted in
  // the dark palette the cover will actually use.
  const paletteFor = useCallback(
    (slide: PresentationSlide): SlidePalette => {
      const scheme = slideScheme(draft.theme, slide);
      return { scheme, tokens: themeConfigs[scheme] ?? EMPTY_PALETTE };
    },
    [draft.theme, themeConfigs],
  );
  // The line every slide will carry, computed the way the stage computes it.
  const chromePreview = useMemo(
    () => chromeText(specWithDraft(presentation.spec, draft), templateChrome),
    [presentation.spec, draft, templateChrome],
  );
  const strayFields = undeclaredFields(draft.chrome, templateChrome);
  const showFooter = draft.chrome.show_footer ?? true;

  // A theme the picked template cannot dress is snapped to one it can — the
  // moment the pick lands when the list already knows the schemes, the moment
  // the detail arrives otherwise — so the preview never shows a theme the save
  // would silently change. The snap is SAID, not only shown (`snapped`): a
  // picker that flipped on its own is otherwise read as a bug.
  const [snapped, setSnapped] = useState<PresentationThemeScheme | null>(null);
  useEffect(() => {
    const fitted = fitThemeToSchemes(draft.theme, supportedSchemes);
    if (fitted !== draft.theme) {
      editor.setDeck({ theme: fitted });
      setSnapped(fitted);
    }
    // `supportedSchemes` is rebuilt every render; keyed on its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.theme, supportedSchemes.join("|")]);

  // The slides with a theme of their OWN: a deck theme change never
  // reaches them (the hint under the deck picker says how many), and a
  // template that cannot dress one snaps that slide like it snaps the deck -
  // said in the same status line, so a slide that flipped is never a mystery.
  const ownThemeCount = draft.slides.filter((slide) => slide.theme === "dark" || slide.theme === "light").length;
  const [snappedSlides, setSnappedSlides] = useState<number>(0);
  useEffect(() => {
    // A TRIED template only. Under the deck's own template a stored override
    // the template stopped supporting is kept as stored: the save keeps it
    // too (only a theme a write INTRODUCES is refused), and the picker greys
    // the scheme out - so opening the panel, or picking the stored template
    // back, never dirties the draft with a snap nobody asked for.
    if (!templateChanged) return;
    let count = 0;
    draft.slides.forEach((slide, index) => {
      if (slide.theme !== "dark" && slide.theme !== "light") return;
      const fitted = fitThemeToSchemes(slide.theme, supportedSchemes);
      if (fitted !== slide.theme) {
        editor.writeAt(index, "theme", fitted, "theme");
        count += 1;
      }
    });
    if (count > 0) setSnappedSlides((current) => current + count);
    // Keyed on the PICK and its SCHEMES, never on the slides: the snap answers
    // a template change (the pick, or its detail arriving), and an effect that
    // also watched the slides would re-snap the theme an undo just restored -
    // the person could never walk back past the snaps to the template step.
    // The pick itself is a key because a relink between two templates of the
    // same schemes is a template change the save snaps against all the same.
    // `supportedSchemes` is rebuilt every render; keyed on its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.template_container_id, supportedSchemes.join("|")]);

  // A cover the tried template does not offer (a THREAD cover on a
  // brand deck) takes that template's default for it, by the same rule and
  // with the same status line as the slide themes above.
  const [snappedHeroes, setSnappedHeroes] = useState<number>(0);
  useEffect(() => {
    if (!templateChanged) return;
    const defaults = draft.template_container_id ? templateHeroDefaults((pickedRow ?? triedTemplate)?.extra_metadata) : {};
    let count = 0;
    draft.slides.forEach((slide, index) => {
      const hero = slideHero(slide);
      if (hero === null || supportedHeroes.includes(hero)) return;
      const fitted = coverHero(slide.layout, supportedHeroes, defaults);
      editor.writeAt(index, "hero", fitted === "plain" ? undefined : fitted, "hero");
      count += 1;
    });
    if (count > 0) setSnappedHeroes((current) => current + count);
    // Keyed on the pick and its covers, never on the slides: see the slide
    // themes above. `supportedHeroes` is rebuilt every render; keyed on its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.template_container_id, supportedHeroes.join("|")]);

  // Hand the host the draft to render. Keyed on the draft (every edit is a new
  // object) and on `dirty`, which flips back to false when a save or a reload
  // re-seeds the draft — the moment the host must show the stored deck again.
  useEffect(() => {
    if (!dirty) {
      onDraftChange?.(null);
      return;
    }
    const spec = specWithDraft(presentation.spec, draft);
    onDraftChange?.({
      spec,
      title: spec.title,
      template_container_id: draft.template_container_id,
      template_name: templateName,
      theme_config: themeConfigs[draft.theme] ?? EMPTY_PALETTE,
      theme_configs: themeConfigs,
      supported_schemes: supportedSchemes,
      template_chrome: templateChrome,
      template_design: templateDesign,
    });
    // `supportedSchemes` is rebuilt every render; keyed on its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, dirty, presentation.spec, onDraftChange, templateName, themeConfigs, supportedSchemes.join("|"), templateChrome, templateDesign]);

  // Closing the panel drops its preview (the viewer decides about the draft).
  useEffect(() => () => onDraftChange?.(null), [onDraftChange]);

  // A collaborator's revision merged in: the slides it changed are
  // flagged in the filmstrip for a moment; where both sides changed the same
  // thing, the person is told their version was kept - once per merge.
  const [flashed, setFlashed] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (!lastRemoteMerge) return;
    setFlashed(lastRemoteMerge.changed);
    const timer = window.setTimeout(() => setFlashed(new Set()), REMOTE_CHANGE_FLASH_MS);
    const who = lastRemoteMerge.by?.trim() || t_i18n("A collaborator");
    const positionsOf = (slideIds: readonly (string | null)[]) =>
      [
        ...new Set(
          slideIds
            .map((slideId) => (slideId ? draft.slides.findIndex((slide) => slide.id === slideId) + 1 : 0))
            .filter((position) => position > 0),
        ),
      ].sort((a, b) => a - b);
    const positions = positionsOf(lastRemoteMerge.conflicts.map((clash) => clash.slideId));
    const restoredAt = positionsOf(lastRemoteMerge.restored);
    if (restoredAt.length > 0) {
      toast({
        title: t_i18n(isPage ? "{name} edited a page you deleted" : "{name} edited a slide you deleted", { name: who }),
        description: t_i18n(isPage ? "It is back with their changes: page {pages}." : "It is back with their changes: slide {pages}.", {
          pages: restoredAt.join(", "),
        }),
        variant: "info",
      });
    }
    if (lastRemoteMerge.conflicts.length > 0) {
      toast({
        title: t_i18n("{name} changed the same thing as you", { name: who }),
        description:
          positions.length > 0
            ? t_i18n(isPage ? "Your version was kept on page {pages}." : "Your version was kept on slide {pages}.", {
                pages: positions.join(", "),
              })
            : t_i18n("Your version was kept."),
        variant: "info",
      });
    } else if (lastRemoteMerge.closedText) {
      toast({
        title: t_i18n(
          isPage ? "{name} changed this page while you were typing" : "{name} changed this slide while you were typing",
          { name: who },
        ),
        description: t_i18n("Your text is saved. Click it to keep editing."),
        variant: "info",
      });
    }
    return () => window.clearTimeout(timer);
    // Once per merge: the draft and the wording are read as they stand then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastRemoteMerge]);

  // Who else is on each slide (by its stable id), for the filmstrip.
  const othersOn = useMemo(() => {
    const bySlide = new Map<string, PresentationPresenceEntry[]>();
    for (const viewer of collaborators) {
      if (!viewer.slide_id) continue;
      bySlide.set(viewer.slide_id, [...(bySlide.get(viewer.slide_id) ?? []), viewer]);
    }
    return bySlide;
  }, [collaborators]);

  const pickTemplate = (value: string) => {
    const nextId = value === NO_TEMPLATE ? null : value;
    const row = nextId ? templates.find((t) => t.id === nextId) : undefined;
    const schemes = nextId ? (row?.supported_schemes ?? [...DECK_THEMES]) : [...DECK_THEMES];
    const fitted = fitThemeToSchemes(draft.theme, schemes);
    setSnapped(fitted !== draft.theme ? fitted : null);
    setSnappedSlides(0);
    setSnappedHeroes(0);
    editor.setDeck({ template_container_id: nextId, theme: fitted });
  };

  const pickTheme = (theme: PresentationTheme) => {
    setSnapped(null);
    editor.setDeck({ theme });
  };

  // ── Layout: where a slide may be moved ─────────────────────────
  // The stored deck's own answer while the template is unchanged; the tried
  // template's advertised catalog otherwise (its list row carries `layouts`);
  // the whole catalog for a free deck. Never empty - a template that says
  // nothing about layouts restricts nothing.
  const layoutOptions: readonly SlideLayout[] = !draft.template_container_id
    ? SLIDE_LAYOUTS
    : !templateChanged
      ? (presentation.supported_layouts ?? SLIDE_LAYOUTS)
      : supportedLayouts(pickedRow?.layouts ?? triedTemplate?.layouts);
  const switchLayout = useSwitchSlideLayout();
  // The slide whose layout is being rebuilt, so its picker shows the work and
  // the other rows keep their pickers - one conversion at a time.
  const [switching, setSwitching] = useState<PresentationSlide | null>(null);
  // The draft as last committed, read after the conversion's round trip: the
  // closure's `draft` is the one the request left from.
  const draftRef = useRef(draft);
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  const changeLayout = async (slide: PresentationSlide, layout: SlideLayout) => {
    if (slide.layout === layout || switching) return;
    setSwitching(slide);
    try {
      const result = await switchLayout.mutateAsync({
        presentationId: presentation.id,
        slide,
        layout,
      });
      // The slide is found again by IDENTITY, never by its id or position: a
      // reorder keeps the object, so the converted slide lands where the row
      // went; anything that produced a NEWER slide meanwhile - an edit to it,
      // a Discard or a Save re-seeding the draft, a revision adopted from the
      // server, a delete - replaced the object, and a result built from the
      // older one must not land over it. It is dropped, and said so.
      if (!draftRef.current.slides.includes(slide)) {
        toast({
          title: t_i18n("Layout not changed"),
          description: isPage
            ? t_i18n("The page changed while its layout was being converted. Pick the layout again.")
            : t_i18n("The slide changed while its layout was being converted. Pick the layout again."),
          variant: "info",
        });
        return;
      }
      // The server materialises the look defaults on the converted slide; the
      // draft reads them in normal form like every other slide it holds.
      const converted = normalizeSlideLook(result.slide);
      editor.setSlides(draftRef.current.slides.map((s) => (s === slide ? converted : s)));
      const notes = [...result.derived, ...result.dropped];
      toast({
        title: t_i18n("Layout changed"),
        description: notes.length > 0 ? notes.join(" \u00b7 ") : undefined,
        variant: "success",
      });
    } catch (err) {
      toast({
        title: t_i18n("Could not change the layout"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setSwitching(null);
    }
  };

  /** The layout picker of one slide - in its row and in the inspector. */
  const layoutPicker = (index: number): ReactNode => {
    const slide = draft.slides[index];
    if (!slide) return null;
    const isSwitching = switching === slide;
    // The picker always lists the slide's OWN layout, so it never shows a
    // layout the slide is not: a layout the template does not advertise stays
    // listed on the slide that wears it (the list is a recommendation, not a rule).
    const rowLayouts: readonly SlideLayout[] = layoutOptions.includes(slide.layout)
      ? layoutOptions
      : [slide.layout, ...layoutOptions];
    const layoutTip = isPage ? t_i18n("Change the layout of this page") : t_i18n("Change the layout of this slide");
    // A long translated name (fr "Diapositive de titre") is cut only when wider
    // than the column: the tooltip always names the current layout first.
    const layoutTipContent = (
      <span className="flex flex-col">
        <span className="font-medium">{layoutLabel(slide.layout, t_i18n, unit)}</span>
        <span>{layoutTip}</span>
      </span>
    );
    return (
      <Select
        value={slide.layout}
        onValueChange={(value) => void changeLayout(slide, value as SlideLayout)}
        disabled={switching !== null}
      >
        <Tip content={layoutTipContent}>
          <SelectTrigger
            className="h-9 w-full min-w-0 max-w-full justify-between gap-1 rounded-sm px-2 py-0 content-compact [&>svg]:h-4 [&>svg]:w-4 [&>svg]:shrink-0"
            aria-label={layoutTip}
            aria-busy={isSwitching || undefined}
          >
            {isSwitching ? (
              <span className="flex items-center gap-1 text-default-secondary">
                <Spinner size="sm" tone="inherit" />
                {isPage ? t_i18n("Moving the page...") : t_i18n("Moving the slide...")}
              </span>
            ) : (
              /* The design-system value carries its own text-content-base: the
                 12 px scale goes on the value, a class on the trigger does not
                 reach it. No fixed cap: the trigger takes the whole width of
                 its own line under the title; a name longer than the column
                 is cut, and the tooltip names the layout. */
              <SelectValue
                className="min-w-0 truncate whitespace-nowrap font-content-compact text-content-compact leading-content-compact tracking-content-compact"
                data-part="layout-value"
              />
            )}
          </SelectTrigger>
        </Tip>
        <SelectContent>
          {rowLayouts.map((layout) => (
            <SelectItem key={layout} value={layout} className="content-compact">
              {layoutLabel(layout, t_i18n, unit)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  };

  // A row click moves the deck there and selects the slide (stable: the
  // memoised thumbnails compare it).
  const { select } = editor;
  const selectRow = useCallback(
    (index: number) => {
      onSelectSlide?.(index);
      select({ slide: index, block: null });
    },
    [select, onSelectSlide],
  );

  // Moving the stage to a slide that a structural change (insert, duplicate,
  // delete, move) creates or shifts: the selection moves now, the stage once
  // it has rendered the draft (`focusRequest`, consumed by the viewer after
  // the deck synced - see `DeckEditor.focusRequest`).
  const selectAfterRender = (index: number) => {
    editor.focusRequest.current = index;
    select({ slide: index, block: null });
  };

  // A row moved by one position swaps with its neighbour: whichever of the two
  // was on the stage stays on the stage. The moved row is selected at its new
  // place; a displaced neighbour keeps its selection (`moveSlide` retargets the
  // index, a selected block included) and the stage follows it.
  const moveSlideBy = (index: number, delta: number) => {
    const to = index + delta;
    editor.moveSlide(index, to);
    if (activeIndex === index) selectAfterRender(to);
    else if (activeIndex === to) editor.focusRequest.current = index;
  };

  // "Add slide": one entry per layout the deck may take, inserted after the
  // slide on the stage. Off at the deck's cap - its template's `max_slides`,
  // served on the response - like Duplicate: a deck that cannot take
  // one more slide says so here, not in a 422 at the save.
  const slideCap = deckSlideCap(editor.presentation);
  const slideCapReached = !canAddSlide(draft, slideCap);
  const slideCapNote = isPage
    ? t_i18n("A document holds at most {max, plural, one {# page} other {# pages}}.", { max: slideCap })
    : t_i18n("A presentation holds at most {max, plural, one {# slide} other {# slides}}.", { max: slideCap });
  const addSlideItems: OverflowMenuItem[] = layoutOptions.map((layout) => ({
    id: `add-${layout}`,
    label: layoutLabel(layout, t_i18n, unit),
    icon: LAYOUT_ICONS[layout],
    disabled: slideCapReached,
    onSelect: () => {
      const at = (activeIndex ?? draft.slides.length - 1) + 1;
      editor.insertSlide(at, SLIDE_SCHEMA[layout].create(at + 1));
      selectAfterRender(at);
    },
  }));

  const tabs: { id: PanelTab; label: string; icon: typeof Layers }[] = [
    {
      id: "slides",
      label: isPage ? t_i18n("Pages") : t_i18n("Slides"),
      icon: Layers,
    },
    { id: "selection", label: t_i18n("Selection"), icon: MousePointerClick },
    {
      id: "deck",
      label: isPage ? t_i18n("Document") : t_i18n("Deck"),
      icon: Settings2,
    },
  ];

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm",
        className,
      )}
      aria-label={t_i18n("Edit presentation")}
      // The panel's own keys (arrows through a select, Space on a reorder
      // button, Escape out of a field) must not also drive the deck next to it.
      {...DECK_KEYS_OFF_ATTR}
      // The stage's Ctrl+Z / Ctrl+Y reach into this panel's fields:
      // a field here writes to the draft on every keystroke, so the deck's
      // undo IS the field's undo - see `EDITOR_PANEL_ATTR`.
      {...EDITOR_PANEL_ATTR}
      data-testid="presentation.editorPanel"
    >
      <div className="flex items-center justify-between border-b border-elevation-subtle-soft px-4 py-3">
        <div>
          <h2 className="content-base font-semibold">{t_i18n("Edit presentation")}</h2>
          <p className="content-compact text-default-secondary">
            {isPage
              ? t_i18n("Click anything on a page to edit it; the assistant is a message away for the rest.")
              : t_i18n("Click anything on the slide to edit it; the assistant is a message away for the rest.")}
          </p>
        </div>
        <Button variant="ghost" size="icon" className="shrink-0" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div role="tablist" aria-label={t_i18n("Editor sections")} className="flex border-b border-elevation-subtle-soft px-2">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            id={`presentation-editor-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`presentation-editor-panel-${id}`}
            data-testid={`presentation.editorTab.${id}`}
            onClick={() => setTab(id)}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 border-b-2 px-2 py-2 text-[12px] font-medium transition-colors",
              tab === id ? "border-accent text-accent" : "border-transparent text-default-secondary hover:text-default-primary",
            )}
          >
            <Icon className="h-3.5 w-3.5" aria-hidden="true" />
            {label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`presentation-editor-panel-${tab}`}
        aria-labelledby={`presentation-editor-tab-${tab}`}
        className="flex-1 space-y-5 overflow-y-auto px-4 py-4"
      >
        {tab === "slides" && (
          <section className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="content-compact tabular-nums text-default-secondary">
                {isPage
                  ? t_i18n("{count, plural, one {# page} other {# pages}}", { count: draft.slides.length })
                  : deckSpeakingSeconds(draft.slides) > 0
                    ? t_i18n("{count, plural, one {# slide} other {# slides}} - about {time} spoken", {
                        count: draft.slides.length,
                        time: formatDurationShort(deckSpeakingSeconds(draft.slides)),
                      })
                    : t_i18n("{count, plural, one {# slide} other {# slides}}", { count: draft.slides.length })}
              </span>
              <OverflowMenu
                items={addSlideItems}
                label={isPage ? t_i18n("Add a page") : t_i18n("Add a slide")}
                trigger={
                  <span className="flex items-center gap-1 px-2 content-compact">
                    <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                    {isPage ? t_i18n("Add page") : t_i18n("Add slide")}
                  </span>
                }
                className="h-9 rounded-sm border border-elevation-subtle-soft text-default-secondary hover:border-accent hover:text-accent"
                header={isPage ? t_i18n("After the current page") : t_i18n("After the current slide")}
              />
            </div>
            <ol className="space-y-1.5">
              {draft.slides.map((slide, index) => {
                const heading = slideHeading(slide);
                const isActive = activeIndex === index;
                const isSelected = selection?.slide === index;
                const others = slide.id ? (othersOn.get(slide.id) ?? []) : [];
                const justChanged = !!slide.id && flashed.has(slide.id);
                return (
                  <li
                    key={slide.id ?? `slide-${index}`}
                    className={cn(
                      "rounded-sm border border-elevation-subtle-soft bg-elevation-default transition-colors duration-700",
                      isActive && "border-accent/50 shadow-[0_0_0_1px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)]",
                      justChanged && "border-feedback-alert-primary/60 bg-feedback-alert-primary/10",
                    )}
                    data-testid={`presentation.slideRow.${index}`}
                    data-remote-change={justChanged ? "true" : undefined}
                  >
                    <div className="flex items-start gap-2 px-2 py-1.5">
                      {/* The thumbnail is a sibling of the row button, not a
                          child: a chart legend inside it is made of buttons. */}
                      <SlideThumbnail
                        presentation={presentation}
                        draft={draft}
                        design={thumbnailDesign}
                        index={index}
                        className={cn("mt-0.5 w-[88px] shrink-0 cursor-pointer", isSelected && "ring-1 ring-accent")}
                        onSelect={selectRow}
                      />
                      <div className="min-w-0 flex-1 space-y-1">
                        {/* Line 1: the title alone, the full width of the
                            column. Beside the four row buttons it had 44 px at
                            1024 and "Key takeaways" read "Key..." (UX round 6
                            of #10302, B1). */}
                        <div className="flex w-full min-w-0 items-center" data-part="slide-row-head">
                        <button
                          type="button"
                          className="flex min-w-0 flex-1 items-center gap-2 text-left"
                          onClick={() => selectRow(index)}
                          aria-current={isActive ? "true" : undefined}
                        >
                          <span className="w-5 shrink-0 content-compact tabular-nums text-default-secondary">
                            {index + 1}
                          </span>
                          <SlideRowTitle
                            text={heading || (isPage ? t_i18n("Untitled page") : t_i18n("Untitled slide"))}
                            placeholder={!heading}
                          />
                          {others.length > 0 && (
                            <Tip
                              content={others
                                .map((viewer) =>
                                  viewer.activity === "viewing"
                                    ? t_i18n("{name} is here", { name: viewer.name?.trim() || t_i18n("Someone") })
                                    : t_i18n("{name} is editing", { name: viewer.name?.trim() || t_i18n("Someone") }),
                                )
                                .join(", ")}
                            >
                              <span className="flex shrink-0 -space-x-1" data-testid={`presentation.slideRow.${index}.others`}>
                                {others.slice(0, 3).map((viewer) => (
                                  <UserInitials
                                    key={viewer.user_id}
                                    userId={viewer.user_id}
                                    name={viewer.name}
                                    size="sm"
                                    className="ring-1 ring-[var(--bg-elevation-default)]"
                                  />
                                ))}
                              </span>
                            </Tip>
                          )}
                        </button>
                        </div>
                        {/* Line 2: the layout picker on its own full-width line
                            under the title, so its width never depends on the
                            buttons:
                            beside them at 1024 the trigger was 74 px and
                            "Contenu" and "Conclusion" both read "Co..." (UX round
                            5 of #10302, B1). Every row has the same lines
                            whatever the layout name. The picker sits outside
                            the row button: a select inside a button is not
                            valid HTML, and its own keys must not toggle the
                            row. The design-system Select draws its trigger
                            inside its own flex span, which the slot stretches
                            ([&>span]:flex-1). */}
                        <div className="flex w-full min-w-0 [&>span]:min-w-0 [&>span]:flex-1" data-part="slide-row-layout">
                          {layoutPicker(index)}
                        </div>
                        {/* Line 3: the row buttons, at the right end, which
                            never shrink and never wrap. */}
                        <div className="flex w-full shrink-0 flex-nowrap items-center justify-end gap-0.5" data-part="slide-row-actions">
                          <span className="ml-auto flex shrink-0 items-center gap-0.5">
                          <Tip content={t_i18n("Move up")} wrap={index === 0}>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 shrink-0 p-0"
                              disabled={index === 0}
                              onClick={() => moveSlideBy(index, -1)}
                              aria-label={t_i18n("Move up")}
                            >
                              <ArrowUp className="h-3.5 w-3.5" />
                            </Button>
                          </Tip>
                          <Tip content={t_i18n("Move down")} wrap={index === draft.slides.length - 1}>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 shrink-0 p-0"
                              disabled={index === draft.slides.length - 1}
                              onClick={() => moveSlideBy(index, 1)}
                              aria-label={t_i18n("Move down")}
                            >
                              <ArrowDown className="h-3.5 w-3.5" />
                            </Button>
                          </Tip>
                          <Tip
                            content={
                              slideCapReached ? slideCapNote : isPage ? t_i18n("Duplicate page") : t_i18n("Duplicate slide")
                            }
                            wrap={slideCapReached}
                          >
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 shrink-0 p-0"
                              disabled={slideCapReached}
                              onClick={() => {
                                editor.duplicateSlide(index);
                                selectAfterRender(index + 1);
                              }}
                              aria-label={isPage ? t_i18n("Duplicate page") : t_i18n("Duplicate slide")}
                            >
                              <CopyPlus className="h-3.5 w-3.5" />
                            </Button>
                          </Tip>
                          <Tip
                            content={isPage ? t_i18n("Delete page") : t_i18n("Delete slide")}
                            wrap={draft.slides.length <= 1}
                          >
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 shrink-0 p-0 text-default-secondary hover:text-feedback-error-primary"
                              // A deck needs at least one slide to validate.
                              disabled={draft.slides.length <= 1}
                              onClick={() => {
                                editor.removeSlide(index);
                                // The stage lands on the slide that takes the place
                                // of the deleted one (the last one when it was last).
                                if (isActive) selectAfterRender(index);
                              }}
                              aria-label={isPage ? t_i18n("Delete page") : t_i18n("Delete slide")}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </Tip>
                          </span>
                        </div>
                        {/* A slide's own LOOK (its theme, the hero of a cover
                            and its colour, the scrim over its image, the accent
                            line of a title) is the Selection tab's, with
                            every other setting of the slide: the row stays one
                            line - thumbnail, title, layout, arrangement. */}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>
        )}

        {tab === "selection" && (
          <SlideInspector
            editor={editor}
            activeIndex={activeIndex ?? 0}
            isPage={isPage}
            layoutPicker={layoutPicker}
            supportedSchemes={supportedSchemes}
            supportedHeroes={supportedHeroes}
            templateName={templateName}
            templateId={draft.template_container_id}
            paletteFor={paletteFor}
            templateDesign={templateDesign}
          />
        )}

        {tab === "deck" && (
          <>
            {/* Deck-level fields */}
            <section className="space-y-3">
              <div className="space-y-1.5">
                <Label className="content-base text-default-primary">{t_i18n("Title (heading)")}</Label>
                <Input aria-label={t_i18n("Title (heading)")}
                  value={draft.title}
                  onChange={(e) => editor.setDeck({ title: e.target.value }, "deck.title")}
                  maxLength={300}
                />
              </div>
              {!isPage && (
                <div className="space-y-1.5">
                  <Label className="content-base text-default-primary">
                    {t_i18n("Transition")}
                  </Label>
                  <Select
                    value={draft.transition}
                    onValueChange={(v) =>
                      editor.setDeck({
                        transition: v as PresentationTransition,
                      })
                    }
                  >
                    <SelectTrigger className="h-9">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {DECK_TRANSITIONS.map((transition) => (
                        <SelectItem key={transition} value={transition}>
                          {transition}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </section>

            {/* Design: the template the deck wears and the theme it allows */}
            <section className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
              <div className="flex items-center gap-2">
                <Palette className="h-3.5 w-3.5 text-accent" />
                <Label className="content-base font-medium text-default-primary">{t_i18n("Design")}</Label>
              </div>
              <div className="space-y-1.5">
                <Label className="content-base text-default-primary">{t_i18n("Template")}</Label>
                <SearchableSelect
                  aria-label={t_i18n("Template")}
                  options={templates.map((row) => ({
                    value: row.id,
                    label: row.name,
                    description: row.supported_schemes.map(schemeLabel).join(" / "),
                  }))}
                  pinned={[
                    { value: NO_TEMPLATE, label: t_i18n("No template - plain look") },
                    ...(storedTemplateHidden && presentation.template_container_id
                      ? [{
                          value: presentation.template_container_id,
                          label: presentation.template_name ?? t_i18n("A template you cannot see"),
                          disabled: true,
                        }]
                      : []),
                  ]}
                  value={draft.template_container_id ?? NO_TEMPLATE}
                  onValueChange={pickTemplate}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="content-base text-default-primary">{t_i18n("Theme")}</Label>
                <ThemeSchemePicker
                  value={draft.theme}
                  onChange={pickTheme}
                  supported={supportedSchemes}
                  templateName={templateName}
                />
                {snapped && (
                  <p role="status" className="content-compact leading-relaxed text-feedback-warning-primary">
                    {t_i18n("Theme set to {theme}: {template} supports {theme} only.", {
                      theme: schemeLabel(snapped),
                      template: templateName ?? t_i18n("this template"),
                    })}
                  </p>
                )}
                {snappedSlides > 0 && (
                  <p role="status" className="content-compact leading-relaxed text-feedback-warning-primary">
                    {t_i18n("{count, plural, one {# slide with a theme of its own was} other {# slides with a theme of their own were}} set to a theme {template} supports.", {
                      count: snappedSlides,
                      template: templateName ?? t_i18n("this template"),
                    })}
                  </p>
                )}
                {snappedHeroes > 0 && (
                  <p role="status" className="content-compact leading-relaxed text-feedback-warning-primary">
                    {t_i18n("{count, plural, one {# cover was} other {# covers were}} set to the default hero: the tiled covers come only with a template that offers them.", {
                      count: snappedHeroes,
                    })}
                  </p>
                )}
                {ownThemeCount > 0 && (
                  <p className="content-compact leading-relaxed text-default-secondary" data-testid="presentation.ownThemeHint">
                    {isPage
                      ? t_i18n("{count, plural, one {# page keeps its own theme} other {# pages keep their own theme}} - the deck theme reaches the others only.", { count: ownThemeCount })
                      : t_i18n("{count, plural, one {# slide keeps its own theme} other {# slides keep their own theme}} - the deck theme reaches the others only.", { count: ownThemeCount })}
                  </p>
                )}
              </div>
              <p className="content-compact leading-relaxed text-default-secondary">
                {draft.template_container_id
                  ? t_i18n(
                      "The template is this {kind}'s design: its colours, fonts and logos. Changing it restyles every {unit}; your content stays. The theme is the colour scheme, limited to what the template supports.",
                      {
                        kind: isPage ? t_i18n("page") : t_i18n("deck"),
                        unit: isPage ? t_i18n("page") : t_i18n("slide"),
                      },
                    )
                  : t_i18n(
                      "No template: the {kind} wears the built-in look in the theme you pick. Choose a template to dress it in a brand's colours, fonts and logos.",
                      { kind: isPage ? t_i18n("page") : t_i18n("deck") },
                    )}{" "}
                <Link to="/templates" className="text-accent underline-offset-2 hover:underline">
                  {t_i18n("Manage templates")}
                </Link>
              </p>
            </section>

            {/* Header & footer: one deck setting, drawn on every slide */}
            <section className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
              <div className="flex items-center gap-2">
                <PanelBottom className="h-3.5 w-3.5 text-accent" />
                <Label className="content-base font-medium text-default-primary">
                  {t_i18n("Header & footer")}
                </Label>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor="deck-chrome-logo" className="content-base text-default-primary">
                    {t_i18n("Show logo")}
                  </Label>
                  <Switch
                    id="deck-chrome-logo"
                    checked={draft.chrome.show_logo ?? true}
                    onCheckedChange={(checked) =>
                      editor.setDeck({
                        chrome: withChrome(draft.chrome, {
                          show_logo: checked,
                        }),
                      })
                    }
                  />
                </div>
                {(draft.chrome.show_logo ?? true) && (
                  <div className="space-y-1.5 pb-1">
                    <Label htmlFor="deck-chrome-logo-position" className="content-base text-default-primary">
                      {t_i18n("Logo position")}
                    </Label>
                    <Select
                      value={draft.chrome.logo_position ?? "auto"}
                      onValueChange={(v) =>
                        editor.setDeck({
                          chrome: withChrome(draft.chrome, {
                            logo_position: v as DeckLogoPosition,
                          }),
                        })
                      }
                    >
                      <SelectTrigger id="deck-chrome-logo-position" className="h-9">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {DECK_LOGO_POSITIONS.map((position) => (
                          <SelectItem key={position} value={position}>
                            {logoPositionLabel(position)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="content-compact text-default-secondary">
                      {t_i18n(
                        "On the opening and closing {unit}s only: Auto puts the logo bottom-left under a left-aligned title and on the axis of a centered one. The other {unit}s keep it in the corner, next to the number.",
                        { unit: isPage ? t_i18n("page") : t_i18n("slide") },
                      )}
                    </p>
                  </div>
                )}
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor="deck-chrome-footer" className="content-base text-default-primary">
                    {templateChrome?.header ? t_i18n("Show footer and header") : t_i18n("Show footer")}
                  </Label>
                  <Switch
                    id="deck-chrome-footer"
                    checked={showFooter}
                    onCheckedChange={(checked) =>
                      editor.setDeck({
                        chrome: withChrome(draft.chrome, {
                          show_footer: checked,
                        }),
                      })
                    }
                  />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor="deck-chrome-numbers" className="content-base text-default-primary">
                    {isPage ? t_i18n("Show page numbers") : t_i18n("Show slide numbers")}
                  </Label>
                  <Switch
                    id="deck-chrome-numbers"
                    checked={draft.chrome.show_slide_numbers ?? true}
                    onCheckedChange={(checked) =>
                      editor.setDeck({
                        chrome: withChrome(draft.chrome, {
                          show_slide_numbers: checked,
                        }),
                      })
                    }
                  />
                </div>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="deck-chrome-event" className="content-base text-default-primary">
                  {t_i18n("Event")}
                </Label>
                <Input aria-label={t_i18n("Event")}
                  id="deck-chrome-event"
                  value={draft.chrome.event_label ?? ""}
                  onChange={(e) =>
                    editor.setDeck(
                      {
                        chrome: withChrome(draft.chrome, {
                          event_label: e.target.value,
                        }),
                      },
                      "chrome.event_label",
                    )
                  }
                  placeholder={t_i18n("e.g. FIC 2026")}
                  maxLength={MAX_FIELD_VALUE_CHARS}
                />
              </div>
              {declaresPattern(templateChrome) ? (
                <>
                  {templateChrome.variables.map((variable) => (
                    <div key={variable.key} className="space-y-1.5">
                      <Label
                        htmlFor={`deck-chrome-field-${variable.key}`}
                        className="content-base text-default-primary"
                      >
                        {variable.label}
                      </Label>
                      <Input aria-label={variable.label}
                        id={`deck-chrome-field-${variable.key}`}
                        value={draft.chrome.fields?.[variable.key] ?? ""}
                        onChange={(e) =>
                          editor.setDeck(
                            {
                              chrome: withChromeField(draft.chrome, variable.key, e.target.value),
                            },
                            `chrome.fields.${variable.key}`,
                          )
                        }
                        placeholder={variable.default ?? ""}
                        maxLength={MAX_FIELD_VALUE_CHARS}
                      />
                      {variable.hint && (
                        <p className="content-compact leading-relaxed text-default-secondary">{variable.hint}</p>
                      )}
                    </div>
                  ))}
                  <p className="content-compact leading-relaxed text-default-secondary">
                    {t_i18n(
                      "{template} defines the footer: {pattern}. It is filled from the title, the opening {unit}'s subtitle, the event and the fields above - once, for every {unit}.",
                      {
                        template: templateName ?? t_i18n("The template"),
                        pattern: templateChrome.footer ?? templateChrome.header ?? "",
                        unit: isPage ? t_i18n("page") : t_i18n("slide"),
                      },
                    )}
                  </p>
                </>
              ) : (
                <>
                  <div className="space-y-1.5">
                    <Label htmlFor="deck-chrome-footer-text" className="content-base text-default-primary">
                      {t_i18n("Footer text")}
                    </Label>
                    <Input aria-label={t_i18n("Footer text")}
                      id="deck-chrome-footer-text"
                      value={draft.chrome.footer_text ?? ""}
                      onChange={(e) =>
                        editor.setDeck(
                          {
                            chrome: withChrome(draft.chrome, {
                              footer_text: e.target.value,
                            }),
                          },
                          "chrome.footer_text",
                        )
                      }
                      placeholder={t_i18n("e.g. Confidential")}
                      maxLength={MAX_FIELD_VALUE_CHARS}
                    />
                  </div>
                  <p className="content-compact leading-relaxed text-default-secondary">
                    {t_i18n(
                      "The footer reads the event and this text on every {unit}. A template can define the footer once for every {kind} it dresses, filled from the title and fields like these.",
                      {
                        unit: isPage ? t_i18n("page") : t_i18n("slide"),
                        kind: isPage ? t_i18n("page") : t_i18n("deck"),
                      },
                    )}
                  </p>
                </>
              )}
              <div className="space-y-1 rounded-sm border border-dashed border-elevation-subtle-soft px-2.5 py-2">
                <p className="content-compact font-medium text-default-secondary">{t_i18n("Footer preview")}</p>
                <p
                  className={cn("truncate text-[12px]", !chromePreview.footer.length && "italic text-default-secondary")}
                >
                  {chromePreview.footer.length
                    ? chromePreview.footer.join(" \u00b7 ")
                    : t_i18n("Nothing - every part of the footer is empty.")}
                </p>
                {chromePreview.header && (
                  <p className="truncate text-[12px]">
                    <span className="text-default-secondary">{t_i18n("Header")}: </span>
                    {chromePreview.header}
                  </p>
                )}
                {!showFooter && (
                  <p className="content-compact text-feedback-warning-primary">{t_i18n("Hidden: the footer switch is off.")}</p>
                )}
              </div>
              {templateChrome && strayFields.length > 0 && (
                <p className="content-compact leading-relaxed text-feedback-warning-primary">
                  {t_i18n("Fields the template does not read: {keys}. They are kept, but no footer shows them.", {
                    keys: strayFields.join(", "),
                  })}
                </p>
              )}
            </section>
          </>
        )}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-elevation-subtle-soft px-4 py-3">
        <div className="flex items-center gap-1">
          <Tip content={t_i18n("Undo (Ctrl+Z)")} wrap={!editor.canUndo}>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 shrink-0 p-0"
              disabled={!editor.canUndo}
              onClick={editor.undo}
              aria-label={t_i18n("Undo")}
              data-testid="presentation.undo"
            >
              <Undo2 className="h-3.5 w-3.5" />
            </Button>
          </Tip>
          <Tip content={t_i18n("Redo (Ctrl+Shift+Z)")} wrap={!editor.canRedo}>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 shrink-0 p-0"
              disabled={!editor.canRedo}
              onClick={editor.redo}
              aria-label={t_i18n("Redo")}
              data-testid="presentation.redo"
            >
              <Redo2 className="h-3.5 w-3.5" />
            </Button>
          </Tip>
        </div>
        <SaveStatus
          autosave={autosave}
          revision={presentation.revision}
          dirty={dirty}
          // A text open in place keeps the focus: the buttons below hand its
          // words over without a blur committing it.
          keepFocus={!!editor.editingText}
          onUndo={editor.canUndo ? editor.undo : undefined}
          formatTime={(at) => intl.formatTime(at, { hour: "2-digit", minute: "2-digit" })}
        />
      </div>
    </aside>
  );
}

/**
 * The slide title of a row. A title longer than the column is cut with an
 * ellipsis, and the design-system tooltip then names it whole (UX round 7 of
 * #10302, B1); measured again when the column or the text changes.
 */
function SlideRowTitle({ text, placeholder }: { text: string; placeholder: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [cut, setCut] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => setCut(node.scrollWidth > node.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
    // `cut` too: the Tip wrapper mounts a new span when it appears or goes.
  }, [text, cut]);
  return (
    <Tip content={cut ? text : undefined} side="right">
      <span
        ref={ref}
        data-part="slide-row-title"
        className={cn("min-w-0 flex-1 truncate text-[13px]", placeholder && "italic text-default-secondary")}
      >
        {text}
      </span>
    </Tip>
  );
}

/**
 * Where the work stands, in place of a Save button: the autosave's
 * state in words, the time of the last save, and the one thing to do when it
 * is stuck - retry now, undo the change the server refused. Announced
 * politely to assistive technology (`role="status"`), never on every keystroke:
 * "Saving..." and "All changes saved" are the two a person hears while typing.
 */
function SaveStatus({
  autosave,
  revision,
  dirty,
  keepFocus,
  onUndo,
  formatTime,
}: {
  autosave: DeckAutosave;
  revision: number;
  dirty: boolean;
  keepFocus: boolean;
  onUndo?: () => void;
  formatTime: (at: number) => string;
}) {
  const { t_i18n } = useFormatter();
  const { state, lastSavedAt, error, retryAt } = autosave.status;
  // The retry countdown reads the clock; nothing else here does.
  const now = useNow(state === "retrying" ? 1_000 : 60_000);
  const hold = (event: React.MouseEvent) => {
    if (keepFocus) event.preventDefault();
  };
  const savedTip = lastSavedAt
    ? t_i18n("Saved at {time} (revision {revision})", { time: formatTime(lastSavedAt), revision })
    : t_i18n("Revision {revision}", { revision });

  let icon: ReactNode;
  let label: string;
  let tone = "text-default-secondary";
  let action: ReactNode = null;
  switch (state) {
    case "saving":
    case "pending":
      icon = <Spinner size="sm" tone="inherit" />;
      label = t_i18n("Saving...");
      break;
    case "syncing":
      icon = <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />;
      label = t_i18n("Merging a collaborator's changes...");
      break;
    case "offline":
      icon = <CloudOff className="h-3.5 w-3.5" aria-hidden="true" />;
      label = t_i18n("Offline - your changes are kept and saved once you are back online");
      tone = "text-feedback-warning-primary";
      break;
    case "retrying": {
      const seconds = retryAt ? Math.max(1, Math.ceil((retryAt - now) / 1000)) : null;
      icon = <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
      label =
        seconds !== null
          ? t_i18n("Not saved yet - retrying in {seconds}s", { seconds })
          : t_i18n("Not saved yet - retrying");
      tone = "text-feedback-warning-primary";
      action = (
        <Button variant="ghost" size="sm" className="h-7 px-2 content-compact" onMouseDown={hold} onClick={autosave.saveNow}>
          {t_i18n("Retry now")}
        </Button>
      );
      break;
    }
    case "invalid":
      icon = <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
      label = t_i18n("Your last change could not be saved");
      tone = "text-feedback-error-primary";
      action = onUndo ? (
        <Button variant="ghost" size="sm" className="h-7 px-2 content-compact" onMouseDown={hold} onClick={onUndo}>
          {t_i18n("Undo it")}
        </Button>
      ) : null;
      break;
    case "denied":
      icon = <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
      label = t_i18n("You can no longer edit this presentation");
      tone = "text-feedback-error-primary";
      break;
    case "unauthorized":
      icon = <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
      label = t_i18n("Your session has expired. Sign in again to continue.");
      tone = "text-feedback-error-primary";
      break;
    default:
      icon = <CheckCircle2 className="h-3.5 w-3.5 text-accent" aria-hidden="true" />;
      label = dirty ? t_i18n("Saving...") : t_i18n("All changes saved");
  }

  return (
    <div className="flex min-w-0 items-center gap-1.5" data-testid="presentation.saveStatus" data-state={state}>
      <Tip content={error ? `${savedTip} - ${error}` : savedTip}>
        <span role="status" className={cn("flex min-w-0 items-center gap-1.5 content-compact", tone)}>
          {icon}
          {/* Wraps, never cut: "Toutes les modifications sont enregistrees"
              is wider than the footer at 1024 and the tooltip shows the save
              time, not this label. */}
          <span className="min-w-0 break-words" data-part="save-label">
            {label}
          </span>
        </span>
      </Tip>
      {action}
    </div>
  );
}
