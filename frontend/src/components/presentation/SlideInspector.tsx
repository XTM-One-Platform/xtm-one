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
 * The inspector: what is selected on the stage, as controls.
 *
 * A BLOCK selected: its type, the path it sits at, its fields from
 * `BLOCK_SCHEMA` (content first, then style), its item list, the table cell
 * grid, and the arrangement actions (move, duplicate, insert, delete) - the
 * same operations the floating bar offers on the stage, here for whoever
 * prefers the panel. A SLIDE selected (or nothing on it): the layout picker,
 * the layout's fields from `SLIDE_SCHEMA`, its item list, the speaker notes
 * with their speaking time, and the background image.
 *
 * Nothing is written here that the stage would not show at once: every
 * control goes through the editor session, the stage previews the draft.
 */
import { ChevronDown, ChevronUp, CopyPlus, ImageOff, MousePointerClick, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn, formatDurationShort } from "@/lib/utils";
import type {
  DeckBlockType,
  DeckHero,
  PresentationSlide,
  PresentationThemeScheme,
  SlideLayout,
  TemplateDesign,
} from "@/types";

import { BLOCK_ICONS, LAYOUT_ICONS } from "./block-icons";
import { blockPaletteItems } from "./block-palette";
import { BLOCK_SCHEMA, LOGO_ROLES, QUOTE_PHOTO_ROLES, blockRunCapacity, type SchemaField } from "./block-schema";
import { butterflySymmetric } from "./blocks/chart-format";
import { quoteLookDefault, resolveQuoteLook } from "./blocks/quote-style";
import { slideFieldOffered } from "./design-tokens";
import { blockPlaces, blockSlot, childPath, getAt, joinPath, parsePath, type BlockPlace } from "./edit-paths";
import {
  blockFieldInactiveReason,
  blockItemFieldInactiveReason,
  fieldInactiveReason,
  fieldInactiveValues,
} from "./field-applicability";
import { heroHostsBackgroundImage, slideHero, slideHeroTone, slideOverlay } from "./hero";
import { logoGridAlign } from "./logo-sizing";
import { TEXTAREA_CLASS } from "./markup-problems";
import { addableRuns, blockAt, canGrowRun, canRemoveBlock, type DeckEditor } from "./presentation-editor";
import { ItemsEditor, SchemaFieldControl, TableEditor } from "./SchemaFieldControls";
import { SLIDE_PALETTE_FIELD } from "./slide-palette";
import { SLIDE_SCHEMA } from "./slide-schema";
import {
  HeroPicker,
  HeroTonePicker,
  OverlayPicker,
  QuoteVariantPicker,
  SlideThemePicker,
  type SlidePalette,
} from "./SlideStylePickers";
import { countWords, estimateSpeakingSeconds, slideMissesNotes } from "./speaking-time";
import { TemplateAssetField } from "./TemplateAssetField";

const SECTION_LABEL = "content-base text-default-primary";

/** One step of a block's position, in words (`blockPlaces`). */
function placeLabel(place: BlockPlace, t_i18n: (id: string, values?: Record<string, number>) => string): string {
  switch (place.kind) {
    case "block":
      return t_i18n("Block {n}", { n: place.n });
    case "column":
      return t_i18n("Column {n}", { n: place.n });
    case "panel":
      return t_i18n("Panel {n}", { n: place.n });
    case "footer":
      return t_i18n("Footer");
    case "left":
      return t_i18n("Left side");
    case "right":
      return t_i18n("Right side");
  }
}

export function SlideInspector({
  editor,
  activeIndex,
  isPage,
  layoutPicker,
  supportedSchemes,
  supportedHeroes,
  templateName,
  templateId,
  paletteFor,
  templateDesign,
}: {
  editor: DeckEditor;
  /** The slide on the stage, inspected when nothing is selected. */
  activeIndex: number;
  isPage: boolean;
  /** The layout picker of the Slides tab, rendered for the inspected slide. */
  layoutPicker: (index: number) => React.ReactNode;
  /** The schemes the deck's template can dress: what a slide may pick as its own theme. */
  supportedSchemes: readonly PresentationThemeScheme[];
  /** The covers the deck's template offers: what a title / closing slide may pick as its hero. */
  supportedHeroes: readonly DeckHero[];
  templateName: string | null;
  /** The draft's template: where a block's pictures are picked from. */
  templateId: string | null;
  /** The palette a slide draws with (its resolved scheme + the template's tokens for it), for the hero colour chips. */
  paletteFor: (slide: PresentationSlide) => SlidePalette;
  /** The draft's template design: a field the template's composition ignores is not offered. */
  templateDesign?: TemplateDesign | null;
}) {
  const { t_i18n } = useFormatter();
  const unit = isPage ? t_i18n("page") : t_i18n("slide");
  const selection = editor.selection;
  const slideIndex = selection?.slide ?? activeIndex;
  const slide = editor.draft.slides[slideIndex];
  if (!slide) return null;

  const block = selection?.block ? blockAt(slide, selection.block) : undefined;

  if (selection?.block && block) {
    return (
      <BlockInspector
        editor={editor}
        slideIndex={slideIndex}
        blockPath={selection.block}
        type={block.type}
        templateId={templateId}
      />
    );
  }

  const schema = SLIDE_SCHEMA[slide.layout as SlideLayout];
  const LayoutIcon = LAYOUT_ICONS[slide.layout as SlideLayout];
  const notes = slide.notes ?? "";
  // The slide's own look: its theme, the hero of a title / closing
  // slide, the scrim over its background image. Each picker writes only a
  // CHANGE (choosing the value the slide already renders with is a no-op), and
  // a picker put back on the model's default UNSETS the key - the draft is
  // read in normal form (`normalizeSlideLook`), like a `flag` control - so a
  // deck stored before or after the fields existed is never dirtied by a look
  // it already has.
  const hero = slideHero(slide);
  const overlay = slideOverlay(slide);
  const bodyRuns = addableRuns(slide, null);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {LayoutIcon && <LayoutIcon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />}
          <h3 className="truncate content-base font-semibold">
            {t_i18n("{unit} {n}", { unit: isPage ? t_i18n("Page") : t_i18n("Slide"), n: slideIndex + 1 })}
          </h3>
        </div>
        {layoutPicker(slideIndex)}
      </div>
      <p className="flex items-start gap-1.5 content-compact leading-relaxed text-default-secondary">
        <MousePointerClick className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
        {isPage
          ? t_i18n("Click a text on the page to type in it; click a block to select it.")
          : t_i18n("Click a text on the slide to type in it; click a block to select it.")}
      </p>

      {schema && (
        <section className="space-y-2">
          {/* A field that acts on another form of the slide than the one it
              has (a column ratio on a single flow, a spread with one block)
              says so under its control instead of flipping to no effect. */}
          {schema.fields.filter((field) => slideFieldOffered(slide.layout, field.key, templateDesign)).map((field) => (
            <SchemaFieldControl
              key={field.key}
              field={field}
              scope={{ editor, slide: slideIndex, base: "", templateId }}
              inactive={fieldInactiveReason(slide, field.key)}
              inactiveValues={fieldInactiveValues(slide, field.key)}
            />
          ))}
        </section>
      )}

      {schema?.items && (
        <section>
          <ItemsEditor schema={schema.items} scope={{ editor, slide: slideIndex, base: "", templateId }} />
        </section>
      )}

      {/* Adding a block by hand: to the end of the body, or of one of
          its columns - the stage offers the same under each run ("+"). */}
      {bodyRuns.length > 0 && (
        <section className="space-y-1.5">
          <Label className={SECTION_LABEL}>{t_i18n("Add a block")}</Label>
          <div className="flex flex-wrap gap-1.5">
            {bodyRuns.map((run) => (
              <OverflowMenu
                key={run.list}
                items={blockPaletteItems(editor, slideIndex, run.list, run.length, t_i18n)}
                label={run.column ? t_i18n("Add a block to column {n}", { n: run.column }) : t_i18n("Add a block")}
                trigger={
                  <>
                    <Plus className="h-3 w-3" aria-hidden="true" />
                    {run.column ? t_i18n("Column {n}", { n: run.column }) : t_i18n("At the end")}
                  </>
                }
                scroll
                className="h-7 rounded-sm border border-elevation-subtle-soft content-compact text-default-secondary hover:text-accent"
              />
            ))}
          </div>
        </section>
      )}

      <section className="space-y-1.5">
        <Label className={SECTION_LABEL}>{t_i18n("Theme")}</Label>
        <SlideThemePicker
          value={slide.theme ?? null}
          deckTheme={editor.draft.theme}
          supported={supportedSchemes}
          templateName={templateName}
          onChange={(next) => editor.writeAt(slideIndex, "theme", next ?? undefined, "theme")}
        />
      </section>

      {/* The slide's own palette inside its template: unset keeps the template's. */}
      <section className="space-y-1.5">
        <Label className={SECTION_LABEL}>{t_i18n("Palette")}</Label>
        <SchemaFieldControl
          field={SLIDE_PALETTE_FIELD}
          scope={{ editor, slide: slideIndex, base: "", templateId }}
          sectionLabelled
        />
      </section>

      {hero !== null && (
        <section className="space-y-1.5">
          <Label className={SECTION_LABEL}>{t_i18n("Hero")}</Label>
          <HeroPicker
            value={hero}
            offered={supportedHeroes}
            onChange={(next) => editor.writeAt(slideIndex, "hero", next === "plain" ? undefined : next, "hero")}
          />
          <p className="content-compact leading-relaxed text-default-secondary">
            {hero === "split" && slide.background_image_file_id
              ? t_i18n("The background image fills the right-hand panel.")
              : t_i18n("Drawn in this {unit}'s theme: set the theme above for a dark cover on a light deck.", { unit })}
          </p>
          {/* The colour of the backdrop: the template's gradient or one
              palette colour alone - each chip painted in the colour THIS slide
              draws it in, dark or light. A plain hero has nothing to colour. */}
          {hero !== "plain" && (
            <>
              <Label className={cn(SECTION_LABEL, "block pt-1")}>{t_i18n("Hero colour")}</Label>
              <HeroTonePicker
                value={slideHeroTone(slide)}
                palette={paletteFor(slide)}
                onChange={(next) =>
                  editor.writeAt(slideIndex, "hero_tone", next === "brand" ? undefined : next, "hero_tone")
                }
              />
              <p className="content-compact leading-relaxed text-default-secondary">
                {t_i18n(
                  "Brand gradient runs from the template's first gradient colour to its second; every other choice uses that one palette colour alone.",
                )}
              </p>
            </>
          )}
        </section>
      )}

      {slide.background_image_file_id && (
        <section className="space-y-1.5">
          <Label className={SECTION_LABEL}>{t_i18n("Overlay")}</Label>
          <OverlayPicker
            value={overlay}
            onChange={(next) => editor.writeAt(slideIndex, "overlay", next === "none" ? undefined : next, "overlay")}
          />
          <p className="content-compact leading-relaxed text-default-secondary">
            {heroHostsBackgroundImage(slide)
              ? t_i18n("A scrim over the panel's picture.")
              : t_i18n("A scrim between the background image and the text, in the {unit}'s theme.", { unit })}
          </p>
        </section>
      )}

      <section className="space-y-1">
        <div className="flex items-center justify-between gap-2">
          <Label className={SECTION_LABEL}>{isPage ? t_i18n("Notes") : t_i18n("Speaker notes")}</Label>
          {/* What the presenter will actually be reading: the same
              word count and pace the speaker view and the agent's tool result
              report. A content slide with nothing to say is named as the gap it is. */}
          {!isPage &&
            (slideMissesNotes(slide) ? (
              <span className="content-compact text-feedback-warning-primary">{t_i18n("No notes yet")}</span>
            ) : (
              countWords(notes) > 0 && (
                <span className="font-mono-plex content-compact tabular-nums text-default-secondary/80">
                  {t_i18n("{words} words - {time}", {
                    words: countWords(notes),
                    time: formatDurationShort(estimateSpeakingSeconds(notes)),
                  })}
                </span>
              )
            ))}
        </div>
        <textarea
          className={TEXTAREA_CLASS}
          rows={4}
          value={notes}
          onChange={(event) => editor.writeAt(slideIndex, "notes", event.target.value || undefined, "notes")}
          placeholder={
            isPage
              ? t_i18n("Never printed - for you and your assistant.")
              : t_i18n("What you say over this slide - shown in the speaker view (S), never on the slide.")
          }
        />
      </section>

      {slide.background_image_file_id && (
        <section className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft px-2.5 py-2">
          <span className="text-[12px]">{t_i18n("Background image")}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 content-compact"
            onClick={() => editor.writeAt(slideIndex, "background_image_file_id", undefined)}
          >
            <ImageOff className="h-3 w-3" /> {t_i18n("Remove")}
          </Button>
        </section>
      )}

      <p className="content-compact leading-relaxed text-default-secondary">
        {t_i18n("Anything structural the palette does not offer - a chart, an image, a diagram - is your assistant's: ask it in the studio and it lands on this {unit}.", { unit })}
      </p>
    </div>
  );
}

function BlockInspector({
  editor,
  slideIndex,
  blockPath,
  type,
  templateId,
}: {
  editor: DeckEditor;
  slideIndex: number;
  blockPath: string;
  type: DeckBlockType;
  templateId: string | null;
}) {
  const { t_i18n } = useFormatter();
  const schema = BLOCK_SCHEMA[type];
  const Icon = BLOCK_ICONS[type];
  const slide = editor.draft.slides[slideIndex];
  const block = slide ? blockAt(slide, blockPath) : undefined;
  const quote = block?.type === "quote" ? block : undefined;
  // A picture or a look written back at its default is UNSET, like a `flag`
  // control: the draft is read in normal form (`normalizeSlideLook`).
  const writeBlockField = (key: string, value: unknown) =>
    editor.writeAt(slideIndex, childPath(blockPath, key), value, childPath(blockPath, key));
  // A dashboard panel's block sits at `panels.N.block`.
  const inPanel = slide?.layout === "dashboard" && parsePath(blockPath)[0] === "panels";
  const inactive = (field: SchemaField) => (block ? blockFieldInactiveReason(block, field.key, { inPanel }) : null);
  const itemInactive = (field: SchemaField) => (block ? blockItemFieldInactiveReason(block, field.key) : null);
  const slot = blockSlot(blockPath);
  const siblings = slot ? getAt(slide, slot.list) : undefined;
  const count = Array.isArray(siblings) ? siblings.length : 0;
  const listPath = slot ? joinPath(slot.list) : null;
  const canDelete = canRemoveBlock(slide, blockPath);
  // The run's cap (`MAX_BLOCKS_PER_*`): Duplicate and Insert stop here, not at a 422.
  const runFull = !!listPath && !canGrowRun(slide, listPath);
  const runFullNote = listPath
    ? t_i18n("This part of the slide holds at most {max} blocks.", { max: blockRunCapacity(listPath) })
    : null;
  const insertItems: OverflowMenuItem[] =
    listPath && slot ? blockPaletteItems(editor, slideIndex, listPath, slot.index + 1, t_i18n) : [];
  const scope = { editor, slide: slideIndex, base: blockPath, templateId };
  // What a look left unset draws, named in its "Default" option: a quote's
  // looks follow its variant, a logo grid's alignment its columns,
  // a butterfly's sides one extent.
  const unsetReads = (field: SchemaField) =>
    quote
      ? quoteLookDefault(quote, field.key)
      : block?.type === "logo_grid" && field.key === "align"
        ? logoGridAlign({ columns: block.columns })
        : block?.type === "chart" && field.key === "symmetric"
          ? butterflySymmetric({})
          : undefined;
  // Content (text) first, then everything else: what one reads before how it reads.
  const isContent = (field: SchemaField) => field.kind === "text" || field.kind === "strings" || field.kind === "group";
  const textFields = schema.fields.filter(isContent);
  const styleFields = schema.fields.filter((field) => !isContent(field));

  return (
    <div className="space-y-4">
      {/* The header wraps instead of cutting: "Back to the slide" drops to its own
          row when the title and its position line do not fit beside it (fr at
          1024, a 340 px panel), and the position line wraps rather than being
          truncated (UX round 2 of #10269, B1). */}
      <div data-testid="block-inspector-header" className="flex flex-wrap items-start justify-between gap-x-2 gap-y-1">
        <div className="flex min-w-0 items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
          <div className="min-w-0">
            <h3 className="break-words content-base font-semibold">{t_i18n(schema.label)}</h3>
            <p className="break-words content-compact text-default-secondary" title={blockPath}>
              {[t_i18n("Slide {n}", { n: slideIndex + 1 }), ...blockPlaces(blockPath).map((place) => placeLabel(place, t_i18n))].join(" \u00b7 ")}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="shrink-0 content-compact text-default-secondary underline-offset-2 hover:underline"
          onClick={() => editor.select({ slide: slideIndex, block: null })}
        >
          {t_i18n("Back to the slide")}
        </button>
      </div>

      <div className="flex items-center gap-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-1">
        <Tip content={t_i18n("Move up (Alt+Up)")} wrap={!slot || slot.index === 0}>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" disabled={!slot || slot.index === 0} onClick={() => editor.moveBlock(slideIndex, blockPath, -1)} aria-label={t_i18n("Move up")}>
            <ChevronUp className="h-3.5 w-3.5" />
          </Button>
        </Tip>
        <Tip content={t_i18n("Move down (Alt+Down)")} wrap={!slot || slot.index >= count - 1}>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" disabled={!slot || slot.index >= count - 1} onClick={() => editor.moveBlock(slideIndex, blockPath, 1)} aria-label={t_i18n("Move down")}>
            <ChevronDown className="h-3.5 w-3.5" />
          </Button>
        </Tip>
        <Tip content={runFull && runFullNote ? runFullNote : t_i18n("Duplicate (Ctrl+D)")} wrap={!slot || runFull}>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" disabled={!slot || runFull} onClick={() => editor.duplicateBlock(slideIndex, blockPath)} aria-label={t_i18n("Duplicate")}>
            <CopyPlus className="h-3.5 w-3.5" />
          </Button>
        </Tip>
        {insertItems.length > 0 && (
          <OverflowMenu
            items={insertItems}
            label={t_i18n("Insert a block after this one")}
            trigger={<Plus className="h-3.5 w-3.5" aria-hidden="true" />}
            scroll
            className="h-7 w-7 rounded-sm text-default-secondary hover:bg-elevation-hover hover:text-accent"
            header={t_i18n("Insert after")}
          />
        )}
        <span className="flex-1" />
        <Tip content={canDelete ? t_i18n("Delete (Delete)") : t_i18n("The last block of its run cannot be deleted - delete the parent instead.")} wrap={!canDelete}>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0 text-default-secondary hover:text-feedback-error-primary" disabled={!canDelete} onClick={() => editor.removeBlock(slideIndex, blockPath)} aria-label={t_i18n("Delete")}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      </div>

      {textFields.length > 0 && (
        <section className="space-y-2">
          <Label className={SECTION_LABEL}>{t_i18n("Content")}</Label>
          {textFields.map((field) => (
            <SchemaFieldControl
              // A `whenSet` control decides once per selected block whether it
              // is shown: keyed by the block, it never carries to another one.
              key={field.whenSet ? `${field.key}@${slideIndex}:${blockPath}` : field.key}
              field={field}
              scope={scope}
              inactive={inactive(field)}
            />
          ))}
        </section>
      )}

      {quote && (
        <section className="space-y-2">
          <Label className={SECTION_LABEL}>{t_i18n("Pictures")}</Label>
          <TemplateAssetField
            label="Portrait"
            value={quote.photo_file_id}
            templateId={templateId}
            roles={QUOTE_PHOTO_ROLES}
            round
            onChange={(next) => writeBlockField("photo_file_id", next)}
          />
          <TemplateAssetField
            label="Company logo"
            value={quote.logo_file_id}
            templateId={templateId}
            roles={LOGO_ROLES}
            onChange={(next) => writeBlockField("logo_file_id", next)}
          />
        </section>
      )}

      {schema.items && !schema.table && (
        <section>
          <ItemsEditor schema={schema.items} scope={scope} inactive={itemInactive} />
        </section>
      )}

      {schema.table && (
        <section className="space-y-3">
          {/* The headers' WORDS and icons here; their number is the table's
              columns, added and removed by the table editor so every row
              keeps a cell per header (the validator refuses a ragged row). */}
          {schema.items && <ItemsEditor schema={schema.items} scope={scope} fixed />}
          <TableEditor scope={scope} />
        </section>
      )}

      {(styleFields.length > 0 || quote) && (
        <section className="space-y-2">
          <Label className={SECTION_LABEL}>{t_i18n("Style")}</Label>
          {/* The quote's four looks as thumbnails; every look field
              left at "Default" below follows the one picked here. */}
          {quote && (
            <QuoteVariantPicker
              value={resolveQuoteLook(quote).variant}
              onChange={(next) => writeBlockField("variant", next === "card" ? undefined : next)}
            />
          )}
          {styleFields.map((field) => (
            <SchemaFieldControl
              key={field.key}
              field={field}
              scope={scope}
              inactive={inactive(field)}
              unsetReads={unsetReads(field)}
            />
          ))}
        </section>
      )}

      {(type === "callout" || type === "comparison" || type === "columns") && (
        <p className="content-compact leading-relaxed text-default-secondary">
          {t_i18n("The blocks inside are edited on the stage: click one to select it, or use the + above to add a sibling.")}
        </p>
      )}
    </div>
  );
}
