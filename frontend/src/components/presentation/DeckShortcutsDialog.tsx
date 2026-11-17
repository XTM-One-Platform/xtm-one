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
 * The keyboard shortcut sheet for a deck — opened from the toolbar, `?`
 * or `F1`. Replaces reveal's unstyled help overlay, and lists what THIS app
 * binds (the toolbar's fullscreen, jump menu and sheet), not reveal's generic
 * defaults.
 *
 * Portaled into the stage while fullscreen (`container`) so it is visible
 * there; the dialog is a Radix dialog, so it owns its keys — the deck does not
 * turn a page while the sheet is open.
 *
 * While the editor panel is open (`editing`) a third group
 * lists the slide editor's keys - the ones `SlideEditLayer` binds - and the
 * description says which of them (undo, redo, save) also work from the
 * panel's fields, since the "shortcuts pause while you type" rule has that
 * one exception there.
 */
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { useFormatter } from "@/i18n";
import { isMac } from "@/lib/utils";
import type { PresentationKind } from "@/types";

import { useEscapeClosesFirst } from "./deck-escape";

interface ShortcutRow {
  label: string;
  /** Alternatives, each a sequence of keys pressed together. */
  keys: string[][];
}

export function DeckShortcutsDialog({
  open,
  onOpenChange,
  container,
  showSpeaker = false,
  kind = "deck",
  editing = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  container?: HTMLElement | null;
  /** The speaker view exists for a deck wherever it is staged; a page
   *  has none. */
  showSpeaker?: boolean;
  /** A page DOCUMENT is read, not presented: its sheet lists the scroll and
   *  zoom keys of `PageDocument`, not fullscreen / overview / black screen. */
  kind?: PresentationKind;
  /** The editor panel is open: the sheet lists the slide editor's keys too. */
  editing?: boolean;
}) {
  const { t_i18n } = useFormatter();
  // Escape closes the sheet, never the dialog hosting the deck underneath,
  // even when pressed before Radix has registered the sheet as its top layer.
  useEscapeClosesFirst(open, () => onOpenChange(false));
  // The modifier as the keyboard in front of the person names it.
  const mod = isMac() ? "Cmd" : "Ctrl";
  const unit = kind === "page" ? t_i18n("page") : t_i18n("slide");

  const editGroup: { title: string; rows: ShortcutRow[] } = {
    title: t_i18n("Edit"),
    rows: [
      { label: t_i18n("Undo"), keys: [[mod, "Z"]] },
      { label: t_i18n("Redo"), keys: [[mod, "Y"], [mod, "Shift", "Z"]] },
      { label: t_i18n("Save now"), keys: [[mod, "S"]] },
      { label: t_i18n("Edit the selected block's text"), keys: [["Enter"]] },
      { label: t_i18n("Next / previous block on the {unit}", { unit }), keys: [["Tab"], ["Shift", "Tab"]] },
      { label: t_i18n("Move the selected block up / down"), keys: [["Alt", "↑"], ["Alt", "↓"]] },
      { label: t_i18n("Duplicate the selected block"), keys: [[mod, "D"]] },
      { label: t_i18n("Delete the selected block"), keys: [["Del"], ["Backspace"]] },
      { label: t_i18n("In a text: line break, keep the change"), keys: [["Enter"], [mod, "Enter"]] },
      { label: t_i18n("In a text: bold, emphasis, code"), keys: [[mod, "B"], [mod, "I"], [mod, "E"]] },
      { label: t_i18n("Leave the text, then the selection"), keys: [["Esc"]] },
    ],
  };

  const groups: { title: string; rows: ShortcutRow[] }[] =
    kind === "page"
      ? [
          {
            title: t_i18n("Navigate"),
            rows: [
              { label: t_i18n("Next page"), keys: [["→"], ["N"], ["PgDn"]] },
              { label: t_i18n("Previous page"), keys: [["←"], ["P"], ["PgUp"]] },
              { label: t_i18n("First / last page"), keys: [["Home"], ["End"]] },
              { label: t_i18n("Go to page"), keys: [["G"]] },
            ],
          },
          {
            title: t_i18n("Read"),
            rows: [
              { label: t_i18n("Zoom in"), keys: [["+"], ["="]] },
              { label: t_i18n("Zoom out"), keys: [["-"]] },
              { label: t_i18n("Fit width"), keys: [["0"]] },
              { label: t_i18n("Keyboard shortcuts"), keys: [["?"], ["F1"]] },
            ],
          },
        ]
      : [
          {
            title: t_i18n("Navigate"),
            rows: [
              { label: t_i18n("Next slide"), keys: [["→"], ["Space"], ["N"], ["PgDn"]] },
              { label: t_i18n("Previous slide"), keys: [["←"], ["P"], ["PgUp"], ["Shift", "Space"]] },
              { label: t_i18n("First / last slide"), keys: [["Home"], ["End"]] },
              { label: t_i18n("Go to slide"), keys: [["G"]] },
              { label: t_i18n("Slide overview"), keys: [["O"]] },
            ],
          },
          {
            title: t_i18n("Present"),
            rows: [
              { label: t_i18n("Fullscreen"), keys: [["F"]] },
              { label: t_i18n("Leave fullscreen / overview"), keys: [["Esc"]] },
              { label: t_i18n("Black screen"), keys: [["B"], ["."]] },
              ...(showSpeaker
                ? [
                    { label: t_i18n("Speaker view"), keys: [["S"]] },
                    { label: t_i18n("Present to a meeting"), keys: [["A"]] },
                    { label: t_i18n("While presenting: next / previous slide"), keys: [["Click"], ["Shift", "Click"]] },
                  ]
                : []),
              { label: t_i18n("Keyboard shortcuts"), keys: [["?"], ["F1"]] },
            ],
          },
        ];
  if (editing) groups.push(editGroup);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent container={container} className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("Keyboard shortcuts")}</DialogTitle>
          <DialogDescription>
            {editing
              ? t_i18n(
                  "Everything in the toolbar has a key. Shortcuts pause while you type or use a menu - except Undo, Redo and Save, which also work from the editor panel's fields; a text you edit in place keeps its own undo until you leave it.",
                )
              : t_i18n("Everything in the toolbar has a key. Shortcuts pause while you type or use a menu.")}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-6 sm:grid-cols-2">
          {groups.map((group) => (
            <section key={group.title} aria-label={group.title}>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-default-secondary">
                {group.title}
              </h3>
              <dl className="divide-y divide-[var(--border-elevation-subtle)]">
                {group.rows.map((row) => (
                  <div key={row.label} className="flex items-center justify-between gap-4 py-2">
                    {/* A long label wraps onto a second line instead of running
                        under the keys (the fr "while presenting" row at 1080 wide);
                        a long alternative list wraps onto a second row of keys. */}
                    <dt className="min-w-0 flex-1 content-base text-default-primary">{row.label}</dt>
                    <dd className="flex min-w-0 max-w-[60%] flex-wrap items-center justify-end gap-1.5">
                      {row.keys.map((combo, i) => (
                        <span key={combo.join("+")} className="inline-flex items-center gap-1">
                          {i > 0 && <span className="content-caption text-default-secondary">{t_i18n("or")}</span>}
                          <span className="inline-flex items-center gap-0.5">
                            {combo.map((key) => (
                              <Kbd key={key}>{key}</Kbd>
                            ))}
                          </span>
                        </span>
                      ))}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
