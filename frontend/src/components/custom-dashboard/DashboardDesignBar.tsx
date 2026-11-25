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
 * The Dashboard Studio's toolbar, above the stage in edit mode (undo, redo and
 * whether everything is saved are the studio bar's, as in the Video Studio):
 * a chart (on sample data of its own,
 * `viz-config.addChart`) or an input to add on every tab; on a canvas, the
 * elements to add (rectangle, ellipse, line, icon, image, markdown),
 * layering, alignment and distribution of the selection, duplication (with
 * the same or a new data source), deletion and the zoom; then the data
 * sources, the source editor and the dashboard's theme. Each change is one
 * step of the designer's history.
 */
import {
  AlignCenterHorizontal,
  AlignCenterVertical,
  AlignEndHorizontal,
  AlignEndVertical,
  AlignHorizontalDistributeCenter,
  AlignStartHorizontal,
  AlignStartVertical,
  AlignVerticalDistributeCenter,
  ArrowDownToLine,
  ArrowUpToLine,
  BringToFront,
  BarChart3,
  Braces,
  Circle,
  ClipboardPaste,
  Copy,
  Database,
  ImageIcon,
  Moon,
  MoveUpRight,
  SlidersHorizontal,
  Sun,
  SendToBack,
  Square,
  Star,
  Trash2,
  Type,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

import { addElement, alignItems, distributeItems, removeItems, reorderItems, setTheme, type ElementKind } from "./canvas";
import type { DashboardDesigner } from "./useDashboardDesigner";
import { ADDABLE_TYPES, addChart, addInput, INPUT_TYPES, TYPE_LABELS, type AddableInput } from "./viz-config";

/** What each input added is called. @i18n-keys */
const INPUT_LABELS = {
  dropdown: "Dropdown",
  multiselect: "Multiselect",
  text: "Text",
  number: "Number",
  timerange: "Time range",
  button: "Button",
} as const satisfies Record<AddableInput, string>;

/** The zoom steps; `fit` shows the canvas as its display mode says. */
const ZOOMS = ["fit", "0.5", "0.75", "1", "1.5", "2"] as const;

/** What each element is called in the bar. @i18n-keys */
const ADD_LABELS = {
  rectangle: "Add a rectangle",
  ellipse: "Add an ellipse",
  line: "Add a line",
  icon: "Add an icon",
  image: "Add an image",
  markdown: "Add markdown",
} as const satisfies Record<ElementKind, string>;

const ADD_ICONS: Record<ElementKind, React.ElementType> = {
  rectangle: Square,
  ellipse: Circle,
  line: MoveUpRight,
  icon: Star,
  image: ImageIcon,
  markdown: Type,
};

function BarButton({
  label,
  icon: Icon,
  onClick,
  disabled,
  shortcut,
}: {
  label: string;
  icon: React.ElementType;
  onClick: () => void;
  disabled?: boolean;
  shortcut?: string;
}) {
  return (
    <Tip content={shortcut ? `${label} (${shortcut})` : label}>
      <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={label} onClick={onClick} disabled={disabled}>
        <Icon className="h-4 w-4" />
      </Button>
    </Tip>
  );
}

const Divider = () => <span className="mx-1 h-5 w-0 shrink-0 border-l border-elevation-subtle" aria-hidden="true" />;

export function DashboardDesignBar({
  designer,
  layoutId,
  canvasLayoutId,
  zoom,
  onZoom,
  theme,
  onAdded,
  onOpenData,
  onOpenSource,
}: {
  designer: DashboardDesigner;
  /** The open tab's layout, grid or canvas. */
  layoutId: string | null;
  /** The open tab's layout when it is a canvas; `null` on a grid. */
  canvasLayoutId: string | null;
  zoom: number | null;
  onZoom: (zoom: number | null) => void;
  theme: "light" | "dark" | undefined;
  /** A chart, a canvas element or an input was added: show its panel. */
  onAdded: (id: string, kind: "visualization" | "input") => void;
  onOpenData: () => void;
  onOpenSource: () => void;
}) {
  const { t_i18n } = useFormatter();
  const mod = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl+";
  const ids = designer.selection;
  const lid = canvasLayoutId;

  const add = (kind: ElementKind) => {
    if (!lid) return;
    let added = "";
    designer.change((definition) => {
      const layout = ((definition.layout as Record<string, unknown>)?.layoutDefinitions as Record<string, { structure?: unknown[] }>)?.[lid];
      // A little further down and right with each element, so a new one never hides the last.
      const offset = 40 + ((layout?.structure?.length ?? 0) % 10) * 24;
      const result = addElement(definition, lid, kind, { x: offset, y: offset }, t_i18n("## A heading\n\nSome **text**."));
      added = result.id;
      return result.definition;
    });
    if (added) {
      designer.select([added]);
      onAdded(added, "visualization");
    }
  };

  const addVisualization = (type: string) => {
    let added = "";
    designer.change((definition) => {
      const result = addChart(definition, type, layoutId, t_i18n("Sample data"));
      if (!result) return definition;
      added = result.id;
      return result.definition;
    });
    if (added) {
      designer.select([added]);
      onAdded(added, "visualization");
    }
  };

  const addAnInput = (kind: AddableInput) => {
    let added = "";
    designer.change((definition) => {
      const result = addInput(definition, kind, t_i18n(INPUT_LABELS[kind]));
      added = result.id;
      return result.definition;
    });
    if (added) onAdded(added, "input");
  };

  const dark = theme !== "light";

  return (
    <div
      role="toolbar"
      aria-label={t_i18n("Design")}
      className="flex min-h-10 flex-wrap items-center gap-0.5 border-b border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-1"
      data-testid="dashboard.design-bar"
    >
      <OverflowMenu
        label={t_i18n("Add a chart")}
        scroll
        trigger={
          <span className="flex items-center gap-1.5 px-2 content-compact" data-testid="dashboard.toolbar.add-chart">
            <BarChart3 className="h-4 w-4" aria-hidden="true" />
            {t_i18n("Chart")}
          </span>
        }
        items={ADDABLE_TYPES.map((type) => ({ id: type, label: t_i18n(TYPE_LABELS[type]), onSelect: () => addVisualization(type) }))}
      />
      <OverflowMenu
        label={t_i18n("Add an input")}
        trigger={
          <span className="flex items-center gap-1.5 px-2 content-compact" data-testid="dashboard.toolbar.add-input">
            <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
            {t_i18n("Input")}
          </span>
        }
        items={INPUT_TYPES.map((kind) => ({ id: kind, label: t_i18n(INPUT_LABELS[kind]), onSelect: () => addAnInput(kind) }))}
      />
      {!lid &&
        (Object.keys(ADD_LABELS) as ElementKind[]).map((kind) => (
          <BarButton
            key={kind}
            label={`${t_i18n(ADD_LABELS[kind])} - ${t_i18n("on a canvas tab")}`}
            icon={ADD_ICONS[kind]}
            onClick={() => undefined}
            disabled
          />
        ))}
      {lid && (
        <>
          {(Object.keys(ADD_LABELS) as ElementKind[]).map((kind) => (
            <BarButton key={kind} label={t_i18n(ADD_LABELS[kind])} icon={ADD_ICONS[kind]} onClick={() => add(kind)} />
          ))}
          <Divider />
          <BarButton
            label={t_i18n("Bring to front")}
            icon={BringToFront}
            disabled={ids.length === 0}
            onClick={() => designer.change((definition) => reorderItems(definition, lid, ids, "front"))}
          />
          <BarButton
            label={t_i18n("Send to back")}
            icon={SendToBack}
            disabled={ids.length === 0}
            onClick={() => designer.change((definition) => reorderItems(definition, lid, ids, "back"))}
          />
          <OverflowMenu
            label={t_i18n("Arrange")}
            items={[
              { id: "forward", label: t_i18n("Bring forward"), icon: ArrowUpToLine, disabled: ids.length === 0, onSelect: () => designer.change((d) => reorderItems(d, lid, ids, "forward")) },
              { id: "backward", label: t_i18n("Send backward"), icon: ArrowDownToLine, disabled: ids.length === 0, onSelect: () => designer.change((d) => reorderItems(d, lid, ids, "backward")) },
              { id: "left", label: t_i18n("Align left"), icon: AlignStartVertical, separatorBefore: true, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "left")) },
              { id: "center", label: t_i18n("Align centres"), icon: AlignCenterVertical, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "center")) },
              { id: "right", label: t_i18n("Align right"), icon: AlignEndVertical, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "right")) },
              { id: "top", label: t_i18n("Align top"), icon: AlignStartHorizontal, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "top")) },
              { id: "middle", label: t_i18n("Align middles"), icon: AlignCenterHorizontal, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "middle")) },
              { id: "bottom", label: t_i18n("Align bottom"), icon: AlignEndHorizontal, disabled: ids.length < 2, onSelect: () => designer.change((d) => alignItems(d, lid, ids, "bottom")) },
              { id: "horizontal", label: t_i18n("Distribute horizontally"), icon: AlignHorizontalDistributeCenter, separatorBefore: true, disabled: ids.length < 3, onSelect: () => designer.change((d) => distributeItems(d, lid, ids, "horizontal")) },
              { id: "vertical", label: t_i18n("Distribute vertically"), icon: AlignVerticalDistributeCenter, disabled: ids.length < 3, onSelect: () => designer.change((d) => distributeItems(d, lid, ids, "vertical")) },
            ]}
          />
          <Divider />
          <OverflowMenu
            label={t_i18n("Duplicate")}
            trigger={<Copy className="h-4 w-4" />}
            items={[
              { id: "duplicate", label: t_i18n("Duplicate"), shortcut: `${mod}D`, disabled: ids.length === 0, onSelect: () => designer.duplicate(lid) },
              { id: "duplicate-source", label: t_i18n("Duplicate with a new data source"), disabled: ids.length === 0, onSelect: () => designer.duplicate(lid, true) },
              { id: "copy", label: t_i18n("Copy"), shortcut: `${mod}C`, separatorBefore: true, disabled: ids.length === 0, onSelect: () => designer.copy(lid) },
              { id: "cut", label: t_i18n("Cut to the clipboard"), shortcut: `${mod}X`, disabled: ids.length === 0, onSelect: () => designer.cut(lid) },
            ]}
          />
          <BarButton label={t_i18n("Paste")} icon={ClipboardPaste} disabled={!designer.canPaste} onClick={() => designer.paste(lid)} shortcut={`${mod}V`} />
          <BarButton
            label={t_i18n("Delete")}
            icon={Trash2}
            disabled={ids.length === 0}
            onClick={() => {
              designer.change((definition) => removeItems(definition, ids));
              designer.select([]);
            }}
            shortcut="⌫"
          />
        </>
      )}
      <div className="ml-auto flex items-center gap-0.5">
        {lid && (
          <Select value={zoom === null ? "fit" : String(zoom)} onValueChange={(value) => onZoom(value === "fit" ? null : Number(value))}>
            <SelectTrigger className="mr-1 h-8 w-28" aria-label={t_i18n("Zoom")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ZOOMS.map((step) => (
                <SelectItem key={step} value={step}>
                  {step === "fit" ? t_i18n("Fit") : `${Math.round(Number(step) * 100)} %`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <BarButton label={t_i18n("Data sources")} icon={Database} onClick={onOpenData} />
        <BarButton label={t_i18n("Dashboard source")} icon={Braces} onClick={onOpenSource} />
        <BarButton
          label={dark ? t_i18n("Light theme") : t_i18n("Dark theme")}
          icon={dark ? Sun : Moon}
          onClick={() => designer.change((definition) => setTheme(definition, dark ? "light" : "dark"))}
        />
      </div>
    </div>
  );
}
