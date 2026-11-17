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
 * The Dashboard Studio's inspector for a canvas (an absolute layout) and for
 * the dashboard as a whole:
 *
 * - `CanvasItemInspector`: the picked element's options (fill, stroke, dash,
 *   radius, arrows, icon, image and its alternative text, markdown and its
 *   styling) and its place in pixels; a line's ends say what they hold to.
 * - `CanvasSelectionSummary`: several items picked, and what to do with them.
 * - `CanvasSettings`: the canvas's size, how it fits the screen, its
 *   background colour and image.
 * - `DashboardSettings`: the dashboard's theme and its tab bar.
 *
 * Every field is one step of the designer's history: a value typed or dragged
 * is one step and one save once it pauses (`coalesce`). An image or an SVG is
 * uploaded to the dashboard (`POST .../assets`), checked and cleaned there.
 */
import { useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { Spinner } from "@filigran/design-system";
import { Image as ImageIcon, LayoutDashboard, Move, Palette, Shapes, Trash2, Upload } from "lucide-react";

import { IconField } from "@/components/presentation/SchemaFieldControls";
import { getDeckIconCatalog, resolveDeckIcon, subscribeDeckIconCatalog } from "@/components/presentation/deck-icons";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import {
  ColorField,
  NumberField,
  Section,
  SelectField,
  SwitchField,
  TextAreaField,
  TextField,
} from "@/components/video-studio/editor/fields";
import { useUploadDashboardAsset } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";

import {
  DISPLAY_MODES,
  MAX_CANVAS,
  MIN_CANVAS,
  blockMap,
  endPoint,
  isConnection,
  readElement,
  setBox,
  setCanvasOptions,
  setElementOptions,
  setLineEnd,
  setTabOptions,
  setTheme,
  type CanvasDisplay,
  type CanvasItem,
  type CanvasModel,
  type LineEnd,
} from "./canvas";
import type { DashboardDesigner } from "./useDashboardDesigner";

type Raw = Record<string, unknown>;

/** How each display mode is called. @i18n-keys */
const DISPLAY_LABELS = {
  auto: "Scale to the screen",
  "actual-size": "Actual size",
  "fit-to-width": "Fit to width",
} as const satisfies Record<CanvasDisplay, string>;

/** @i18n-keys */
const DASH_LABELS = { solid: "Solid", dashed: "Dashed", dotted: "Dotted" } as const;
/** @i18n-keys */
const FIT_LABELS = { contain: "Whole image", cover: "Fill the box", fill: "Stretch" } as const;
/** @i18n-keys */
const SIZE_LABELS = { auto: "Its own size", contain: "Whole image", cover: "Cover the layout" } as const;
/** @i18n-keys */
const FONT_SIZE_LABELS = { small: "Small", default: "Default", large: "Large", extraLarge: "Extra large" } as const;
/** @i18n-keys */
const FONT_FAMILY_LABELS = { sans: "Sans serif", serif: "Serif", mono: "Monospace" } as const;
/** @i18n-keys */
const ALIGN_LABELS = { left: "Left-aligned", center: "Centred", right: "Right-aligned" } as const;
/** @i18n-keys */
const PORT_LABELS = { n: "top", e: "right", s: "bottom", w: "left" } as const;

const IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp,image/svg+xml,.svg";

function options<T extends string>(labels: Record<T, string>, t: (key: string) => string) {
  return (Object.keys(labels) as T[]).map((value) => ({ value, label: t(labels[value]) }));
}

function rawOptions(definition: Raw | null, id: string): Raw {
  const viz = ((definition?.visualizations ?? {}) as Record<string, Raw>)[id];
  return (viz?.options ?? {}) as Raw;
}

/** Upload a file to the dashboard, then hand its id over. */
function UploadButton({
  dashboardId,
  accept,
  label,
  onUploaded,
}: {
  dashboardId: string;
  accept: string;
  label: string;
  onUploaded: (fileId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const upload = useUploadDashboardAsset(dashboardId);
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="flex">
      <input
        ref={input}
        type="file"
        accept={accept}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          upload.mutate(file, {
            onSuccess: (asset) => onUploaded(asset.id),
            onError: (error) => toast({ title: t_i18n("The file could not be added"), description: error.message, variant: "error" }),
          });
        }}
      />
      <Button variant="outline" size="sm" className="gap-1.5" onClick={() => input.current?.click()} disabled={upload.isPending} aria-busy={upload.isPending || undefined}>
        {upload.isPending && <Spinner size="sm" tone="inherit" />}
        {!upload.isPending && <Upload className="h-3.5 w-3.5" />}
        {label}
      </Button>
    </div>
  );
}

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;
const OPAQUE_HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

/**
 * A field whose value is saved only once it is one the definition accepts (a
 * whole colour, a known icon): what is being typed stays in the field, never
 * in a save the server would refuse.
 */
function useDraft<T>(value: T, commit: (next: T) => void, accepts: (next: T) => boolean): [T, (next: T) => void] {
  const [draft, setDraft] = useState<{ base: T; text: T } | null>(null);
  // A draft typed over another value (another element, an undo) is dropped.
  const shown = draft && draft.base === value ? draft.text : value;
  const change = (next: T) => {
    if (accepts(next)) {
      setDraft(null);
      commit(next);
    } else setDraft({ base: value, text: next });
  };
  return [shown, change];
}

/** A colour saved once it is whole; `alpha: false` for a visualization's options, which take no transparency (#rgb, #rrggbb). */
export function DraftColorField({
  label,
  value,
  placeholder,
  onChange,
  alpha = true,
}: {
  label: string;
  value: string | undefined;
  placeholder?: string;
  onChange: (next: string | undefined) => void;
  alpha?: boolean;
}) {
  const [shown, change] = useDraft(value, onChange, (next) => next === undefined || (alpha ? HEX_RE : OPAQUE_HEX_RE).test(next));
  return <ColorField label={label} value={shown} placeholder={placeholder} onChange={change} />;
}

function PositionFields({ designer, layoutId, item }: { designer: DashboardDesigner; layoutId: string; item: Extract<CanvasItem, { type: "block" }> }) {
  const { t_i18n } = useFormatter();
  const field = (key: "x" | "y" | "w" | "h", label: string, min: number) => (
    <NumberField
      label={label}
      value={item[key]}
      min={min}
      max={MAX_CANVAS}
      step={1}
      unit="px"
      onChange={(value) => {
        if (value === undefined) return;
        designer.change((definition) => setBox(definition, layoutId, item.id, { x: item.x, y: item.y, w: item.w, h: item.h, [key]: Math.round(value) }), `${item.id}.${key}`);
      }}
    />
  );
  return (
    <Section title={t_i18n("Position")} icon={Move}>
      <div className="grid grid-cols-2 gap-2">
        {field("x", "X", 0)}
        {field("y", "Y", 0)}
        {field("w", t_i18n("Width"), 1)}
        {field("h", t_i18n("Height"), 1)}
      </div>
    </Section>
  );
}

function LineEndField({ label, end, onDetach }: { label: string; end: LineEnd; onDetach: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center justify-between gap-2 content-compact">
      <span className="font-medium">{label}</span>
      {isConnection(end) ? (
        <span className="flex min-w-0 items-center gap-1.5 text-default-secondary">
          <span className="truncate">
            {t_i18n("Held to {item} ({side})", { item: end.item, side: t_i18n(PORT_LABELS[end.port]) })}
          </span>
          <Button variant="ghost" size="sm" className="h-6 px-1.5" onClick={onDetach}>
            {t_i18n("Let go")}
          </Button>
        </span>
      ) : (
        <span className="text-default-secondary">{`${end.x}, ${end.y}`}</span>
      )}
    </div>
  );
}

/** A Lucide icon's name, saved once the catalog knows it. */
function IconNameField({ id, value, onChange }: { id: string; value: string; onChange: (name: string) => void }) {
  const { t_i18n } = useFormatter();
  const catalog = useSyncExternalStore(subscribeDeckIconCatalog, getDeckIconCatalog, getDeckIconCatalog);
  const [shown, change] = useDraft(value, onChange, (next) => resolveDeckIcon(catalog, next) !== null);
  return <IconField id={id} label={t_i18n("Lucide icon")} value={shown} optional={false} onChange={change} />;
}

export function CanvasItemInspector({
  designer,
  layoutId,
  item,
  model,
  dashboardId,
  widgetPanel,
}: {
  designer: DashboardDesigner;
  layoutId: string;
  item: CanvasItem;
  model: CanvasModel;
  dashboardId: string;
  /** The widget inspector, for a block that is a widget. */
  widgetPanel?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const raw = rawOptions(designer.definition, item.id);
  const set = (key: string, coalesce = true) => (value: unknown) =>
    designer.change((definition) => setElementOptions(definition, item.id, { [key]: value }), coalesce ? `${item.id}.${key}` : undefined);
  const element = item.type === "line" ? item.element : item.element;

  let fields: ReactNode = null;
  if (element && (element.kind === "rectangle" || element.kind === "ellipse" || element.kind === "line")) {
    const stroke = readElement(`xtm.shape.${element.kind}`, raw) ?? element;
    const strokeFields = (
      <>
        <DraftColorField label={t_i18n("Line colour")} value={raw.strokeColor as string | undefined} placeholder={t_i18n("Accent")} onChange={set("strokeColor")} />
        <NumberField label={t_i18n("Line width")} value={raw.strokeWidth as number | undefined} fallback={1} min={0} max={50} step={1} unit="px" onChange={set("strokeWidth")} />
        <NumberField label={t_i18n("Line opacity")} value={raw.strokeOpacity as number | undefined} fallback={1} min={0} max={1} step={0.05} onChange={set("strokeOpacity")} />
        <SelectField
          label={t_i18n("Line style")}
          value={(raw.strokeDashStyle as keyof typeof DASH_LABELS | undefined) ?? undefined}
          options={options(DASH_LABELS, t_i18n)}
          unsetLabel={t_i18n("Solid")}
          onChange={set("strokeDashStyle", false)}
        />
      </>
    );
    fields = (
      <Section title={t_i18n("Style")} icon={Palette}>
        {element.kind !== "line" && (
          <>
            <DraftColorField label={t_i18n("Fill colour")} value={raw.fillColor as string | undefined} placeholder={t_i18n("Accent")} onChange={set("fillColor")} />
            <NumberField label={t_i18n("Fill opacity")} value={raw.fillOpacity as number | undefined} fallback={1} min={0} max={1} step={0.05} onChange={set("fillOpacity")} />
          </>
        )}
        {strokeFields}
        {element.kind === "rectangle" && (
          <NumberField label={t_i18n("Corner radius")} value={raw.rx as number | undefined} fallback={0} min={0} max={200} step={1} unit="px" onChange={set("rx")} />
        )}
        {stroke.kind === "line" && (
          <>
            <SwitchField label={t_i18n("Arrow at the start")} checked={stroke.fromArrow} onChange={(on) => set("fromArrow", false)(on || undefined)} />
            <SwitchField label={t_i18n("Arrow at the end")} checked={stroke.toArrow} onChange={(on) => set("toArrow", false)(on || undefined)} />
          </>
        )}
      </Section>
    );
  } else if (element?.kind === "icon") {
    fields = (
      <Section title={t_i18n("Icon")} icon={Shapes}>
        <IconNameField
          id={`icon-${item.id}`}
          value={(raw.icon as string | undefined) ?? ""}
          onChange={(name) => designer.change((definition) => setElementOptions(definition, item.id, { icon: name, fileId: undefined }), `${item.id}.icon`)}
        />
        <div className="flex items-center gap-2">
          <UploadButton
            dashboardId={dashboardId}
            accept="image/svg+xml,.svg"
            label={t_i18n("Upload an SVG")}
            onUploaded={(fileId) => designer.change((definition) => setElementOptions(definition, item.id, { fileId, icon: undefined }))}
          />
          {typeof raw.fileId === "string" && <span className="content-caption text-default-secondary">{t_i18n("An uploaded SVG")}</span>}
        </div>
        <DraftColorField label={t_i18n("Colour")} value={raw.color as string | undefined} placeholder={t_i18n("Accent")} onChange={set("color")} />
        <NumberField label={t_i18n("Opacity")} value={raw.opacity as number | undefined} fallback={1} min={0} max={1} step={0.05} onChange={set("opacity")} />
        <TextField label={t_i18n("Alternative text")} value={raw.alt as string | undefined} maxLength={300} onChange={set("alt")} />
      </Section>
    );
  } else if (element?.kind === "image") {
    fields = (
      <Section title={t_i18n("Image")} icon={ImageIcon}>
        <UploadButton
          dashboardId={dashboardId}
          accept={IMAGE_TYPES}
          label={typeof raw.fileId === "string" ? t_i18n("Replace the image") : t_i18n("Upload an image")}
          onUploaded={(fileId) => designer.change((definition) => setElementOptions(definition, item.id, { fileId }))}
        />
        <TextField
          label={t_i18n("Alternative text")}
          hint={t_i18n("What the image shows, for those who cannot see it.")}
          value={raw.alt as string | undefined}
          maxLength={300}
          onChange={set("alt")}
        />
        <SelectField label={t_i18n("Fit")} value={raw.fit as keyof typeof FIT_LABELS | undefined} options={options(FIT_LABELS, t_i18n)} unsetLabel={t_i18n("Whole image")} onChange={set("fit", false)} />
        <NumberField label={t_i18n("Opacity")} value={raw.opacity as number | undefined} fallback={1} min={0} max={1} step={0.05} onChange={set("opacity")} />
      </Section>
    );
  } else if (element?.kind === "markdown") {
    fields = (
      <Section title={t_i18n("Markdown")} icon={Shapes}>
        <TextAreaField label={t_i18n("Text")} value={raw.markdown as string | undefined} maxLength={5000} rows={6} onChange={(value) => set("markdown")(value ?? "")} />
        <DraftColorField label={t_i18n("Text colour")} value={raw.fontColor as string | undefined} placeholder={t_i18n("Theme")} onChange={set("fontColor")} />
        <DraftColorField label={t_i18n("Background colour")} value={raw.backgroundColor as string | undefined} placeholder={t_i18n("None")} onChange={set("backgroundColor")} />
        <SelectField label={t_i18n("Text size")} value={raw.fontSize as keyof typeof FONT_SIZE_LABELS | undefined} options={options(FONT_SIZE_LABELS, t_i18n)} unsetLabel={t_i18n("Default")} onChange={set("fontSize", false)} />
        <SelectField label={t_i18n("Font")} value={raw.fontFamily as keyof typeof FONT_FAMILY_LABELS | undefined} options={options(FONT_FAMILY_LABELS, t_i18n)} unsetLabel={t_i18n("Sans serif")} onChange={set("fontFamily", false)} />
        <SelectField label={t_i18n("Alignment")} value={raw.textAlign as keyof typeof ALIGN_LABELS | undefined} options={options(ALIGN_LABELS, t_i18n)} unsetLabel={t_i18n("Left-aligned")} onChange={set("textAlign", false)} />
      </Section>
    );
  }

  return (
    <div className="flex flex-col" data-testid="dashboard.canvas-inspector">
      {widgetPanel}
      {fields}
      {item.type === "block" ? (
        <PositionFields designer={designer} layoutId={layoutId} item={item} />
      ) : (
        <Section title={t_i18n("Ends")} icon={Move}>
          {(["from", "to"] as const).map((which) => (
            <LineEndField
              key={which}
              label={which === "from" ? t_i18n("Line start") : t_i18n("Line end")}
              end={item[which]}
              onDetach={() => {
                const at = endPoint(item[which], blockMap(model.items));
                designer.change((definition) => setLineEnd(definition, layoutId, item.id, which, at));
              }}
            />
          ))}
          <p className="content-caption text-default-secondary">{t_i18n("Drag an end onto the middle of a side of a block to hold it there.")}</p>
        </Section>
      )}
    </div>
  );
}

export function CanvasSelectionSummary({ count, onClear }: { count: number; onClear: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-col gap-3 p-4">
      <p className="content-base font-semibold">{t_i18n("{count, plural, one {# item selected} other {# items selected}}", { count })}</p>
      <p className="content-compact text-default-secondary">
        {t_i18n("Move them together, or align and distribute them from the design bar.")}
      </p>
      <Button variant="outline" size="sm" className="self-start" onClick={onClear}>
        {t_i18n("Clear the selection")}
      </Button>
    </div>
  );
}

export function CanvasSettings({
  designer,
  layoutId,
  model,
  dashboardId,
}: {
  designer: DashboardDesigner;
  layoutId: string;
  model: CanvasModel;
  dashboardId: string;
}) {
  const { t_i18n } = useFormatter();
  const { options: canvas } = model;
  const set = (key: string, coalesce = true) => (value: unknown) =>
    designer.change((definition) => setCanvasOptions(definition, layoutId, { [key]: value }), coalesce ? `${layoutId}.${key}` : undefined);
  const image = canvas.backgroundImage;
  const setImage = (patch: Raw | undefined, coalesce?: string) =>
    designer.change((definition) => setCanvasOptions(definition, layoutId, { backgroundImage: patch && image ? { ...image, ...patch } : patch }), coalesce);
  return (
    <div className="flex flex-col" data-testid="dashboard.canvas-settings">
      <Section title={t_i18n("Absolute layout")} icon={Shapes}>
        <div className="grid grid-cols-2 gap-2">
          <NumberField label={t_i18n("Width")} value={canvas.width} min={MIN_CANVAS} max={MAX_CANVAS} step={10} unit="px" onChange={(value) => value !== undefined && set("width")(Math.round(value))} />
          <NumberField label={t_i18n("Height")} value={canvas.height} min={MIN_CANVAS} max={MAX_CANVAS} step={10} unit="px" onChange={(value) => value !== undefined && set("height")(Math.round(value))} />
        </div>
        <SelectField
          label={t_i18n("Display")}
          value={canvas.display === "auto" ? undefined : canvas.display}
          options={DISPLAY_MODES.filter((mode) => mode !== "auto").map((mode) => ({ value: mode, label: t_i18n(DISPLAY_LABELS[mode]) }))}
          unsetLabel={t_i18n(DISPLAY_LABELS.auto)}
          onChange={set("display", false)}
        />
      </Section>
      <Section title={t_i18n("Background")} icon={Palette}>
        <DraftColorField label={t_i18n("Background colour")} value={canvas.backgroundColor} placeholder={t_i18n("None")} onChange={set("backgroundColor")} />
        <div className="flex items-center gap-2">
          <UploadButton
            dashboardId={dashboardId}
            accept={IMAGE_TYPES}
            label={image ? t_i18n("Replace the image") : t_i18n("Background image")}
            onUploaded={(fileId) => setImage({ fileId, sizeType: image?.sizeType ?? "cover", opacity: image?.opacity ?? 1 })}
          />
          {image && (
            <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={t_i18n("Remove the background image")} onClick={() => setImage(undefined)}>
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
        {image && (
          <>
            <SelectField
              label={t_i18n("Image size")}
              value={image.sizeType}
              options={options(SIZE_LABELS, t_i18n)}
              onChange={(value) => value && setImage({ sizeType: value })}
            />
            <NumberField
              label={t_i18n("Image opacity")}
              value={image.opacity}
              min={0}
              max={1}
              step={0.05}
              onChange={(value) => value !== undefined && setImage({ opacity: value }, `${layoutId}.backgroundImage.opacity`)}
            />
          </>
        )}
      </Section>
    </div>
  );
}

/** @i18n-keys */
const THEME_LABELS = { light: "Light", dark: "Dark" } as const;
/** @i18n-keys */
const BAR_LABELS = { top: "On top", left: "On the left" } as const;

export function DashboardSettings({
  designer,
  theme,
  showTabBar,
  barPosition,
}: {
  designer: DashboardDesigner;
  theme?: "light" | "dark";
  showTabBar: boolean;
  barPosition: "top" | "left";
}) {
  const { t_i18n } = useFormatter();
  return (
    <Section title={t_i18n("Dashboard")} icon={LayoutDashboard}>
      <SelectField
        label={t_i18n("Theme")}
        hint={t_i18n("A theme of its own is kept whatever the viewer's.")}
        value={theme}
        options={options(THEME_LABELS, t_i18n)}
        unsetLabel={t_i18n("The viewer's")}
        onChange={(value) => designer.change((definition) => setTheme(definition, value ?? null))}
      />
      <SwitchField label={t_i18n("Show the tab bar")} checked={showTabBar} onChange={(on) => designer.change((definition) => setTabOptions(definition, { showTabBar: on }))} />
      <SelectField
        label={t_i18n("Tab bar")}
        value={barPosition}
        options={options(BAR_LABELS, t_i18n)}
        onChange={(value) => value && designer.change((definition) => setTabOptions(definition, { barPosition: value }))}
      />
    </Section>
  );
}

export function NothingPicked({ children }: { children?: ReactNode }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-col gap-1 p-4">
      <p className="content-compact text-default-secondary">{t_i18n("Pick an element, or draw a box around several.")}</p>
      {children}
    </div>
  );
}
