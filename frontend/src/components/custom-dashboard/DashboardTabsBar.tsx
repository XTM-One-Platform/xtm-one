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
 * A dashboard's tab bar: on top or on the left (`layout.tabs.options.barPosition`).
 *
 * In the studio it also edits the tabs, each change one step of the
 * definition (`canvas.ts`): add a tab (a grid or a canvas), and for the open
 * tab rename it, move it left or right, switch its layout between a grid and a
 * canvas (refused, and said, while a canvas holds elements a grid cannot
 * draw), and remove it with what only it shows.
 */
import { useRef, useState } from "react";
import { Tabs, TabsList, TabsTrigger } from "@filigran/design-system";
import { ArrowLeft, ArrowRight, Grid3x3, PenLine, Plus, Shapes, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { MAX_TABS, addTab, moveTab, removeTab, renameTab, setTabType } from "./canvas";
import type { DashboardTab } from "./spec";

type Definition = Record<string, unknown>;

/** How the studio edits the tabs: a change of the definition, and the tab to open after it. */
export interface TabsEditing {
  onChange: (update: (definition: Definition) => Definition) => void;
  /** The definition as it is now: what a change that may be refused (a tab too many) is computed from. */
  definition: Definition;
}

export function DashboardTabsBar({
  tabs,
  value,
  onValueChange,
  position,
  editing,
  className,
}: {
  tabs: DashboardTab[];
  value: string;
  onValueChange: (id: string) => void;
  position: "top" | "left";
  editing?: TabsEditing | null;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [removing, setRemoving] = useState(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const current = tabs.find((tab) => tab.id === value) ?? tabs[0];
  const index = tabs.indexOf(current);

  const add = (type: "grid" | "absolute") => {
    if (!editing) return;
    const added = addTab(editing.definition, type, type === "absolute" ? t_i18n("Absolute layout") : t_i18n("New tab"));
    if (!added) return;
    editing.onChange(() => added.definition);
    onValueChange(added.layoutId);
  };

  const switchType = () => {
    if (!editing) return;
    const next = setTabType(editing.definition, current.id, current.type === "grid" ? "absolute" : "grid");
    if (!next) {
      toast({
        title: t_i18n("This tab cannot become a grid"),
        description: t_i18n("It holds shapes, lines, icons, images or markdown, which only an absolute layout draws. Remove them first."),
        variant: "info",
      });
      return;
    }
    editing.onChange(() => next);
  };

  const actions = editing ? (
    <div className={cn("flex items-center gap-0.5", position === "left" && "flex-col items-stretch")}>
      <OverflowMenu
        label={t_i18n("Add a tab")}
        trigger={<Plus className="h-4 w-4" />}
        items={[
          { id: "grid", label: t_i18n("Add a grid tab"), icon: Grid3x3, onSelect: () => add("grid"), disabled: tabs.length >= MAX_TABS },
          { id: "canvas", label: t_i18n("Add an absolute layout tab"), icon: Shapes, onSelect: () => add("absolute"), disabled: tabs.length >= MAX_TABS },
        ]}
      />
      <OverflowMenu
        label={t_i18n("Tab options")}
        items={[
          {
            id: "rename",
            label: t_i18n("Rename"),
            icon: PenLine,
            onSelect: () => {
              setLabel(current.label);
              setRenaming(current.id);
            },
          },
          {
            id: "left",
            label: position === "left" ? t_i18n("Move up") : t_i18n("Move left"),
            icon: ArrowLeft,
            disabled: index <= 0,
            onSelect: () => editing.onChange((definition) => moveTab(definition, current.id, -1)),
          },
          {
            id: "right",
            label: position === "left" ? t_i18n("Move down") : t_i18n("Move right"),
            icon: ArrowRight,
            disabled: index >= tabs.length - 1,
            onSelect: () => editing.onChange((definition) => moveTab(definition, current.id, 1)),
          },
          {
            id: "type",
            label: current.type === "grid" ? t_i18n("Use an absolute layout") : t_i18n("Use a grid"),
            icon: current.type === "grid" ? Shapes : Grid3x3,
            separatorBefore: true,
            onSelect: switchType,
          },
          {
            id: "remove",
            label: t_i18n("Delete tab"),
            icon: Trash2,
            destructive: true,
            separatorBefore: true,
            disabled: tabs.length <= 1,
            onSelect: () => setRemoving(true),
          },
        ]}
      />
    </div>
  ) : undefined;

  return (
    <>
      <Tabs
        value={current.id}
        activationMode="manual"
        onValueChange={onValueChange}
        orientation={position === "left" ? "vertical" : "horizontal"}
        panels="external"
        className={className}
      >
        <TabsList aria-label={t_i18n("Dashboard tabs")} actions={actions}>
          {tabs.map((tab) => (
            <TabsTrigger key={tab.id} value={tab.id} icon={editing && tab.type === "absolute" ? <Shapes /> : undefined}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <Dialog open={renaming !== null} onOpenChange={(open) => !open && setRenaming(null)}>
        <DialogContent
          className="max-w-sm"
          onOpenAutoFocus={(event) => {
            // The name, ready to be typed over, rather than the close button.
            event.preventDefault();
            nameInput.current?.focus();
            nameInput.current?.select();
          }}
        >
          <DialogHeader>
            <DialogTitle>{t_i18n("Rename the tab")}</DialogTitle>
          </DialogHeader>
          <form
            className="flex flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (editing && renaming && label.trim()) {
                const id = renaming;
                editing.onChange((definition) => renameTab(definition, id, label));
              }
              setRenaming(null);
            }}
          >
            <Input ref={nameInput} aria-label={t_i18n("Tab name")} value={label} maxLength={60} onChange={(event) => setLabel(event.target.value)} />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setRenaming(null)}>
                {t_i18n("Cancel")}
              </Button>
              <Button type="submit" disabled={!label.trim()}>
                {t_i18n("Rename")}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDeleteDialog
        open={removing}
        onOpenChange={setRemoving}
        name={current.label}
        description={t_i18n("The tab and what only it shows are removed. Undo brings them back.")}
        onConfirm={() => {
          if (!editing) return;
          const next = removeTab(editing.definition, current.id);
          if (!next) return;
          editing.onChange(() => next);
          onValueChange(tabs[index === 0 ? 1 : index - 1].id);
        }}
      />
    </>
  );
}
