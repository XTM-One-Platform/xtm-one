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
 * The Video Studio player's key sheet, opened with `?` or from the
 * player's settings: what `playerKeyAction` binds, in the deck sheet's form
 * (`DeckShortcutsDialog`). Rendered inside the player (`container`) so it
 * shows in fullscreen; a Radix dialog, it owns its keys while open.
 */
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { useFormatter } from "@/i18n";

interface ShortcutRow {
  label: string;
  /** Alternatives, each one key. */
  keys: string[];
}

export function PlayerShortcutsDialog({
  open,
  onOpenChange,
  container,
  captions,
  fullscreen,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  container: HTMLElement | null;
  /** The video has captions the player can show or hide. */
  captions: boolean;
  /** The player offers fullscreen. */
  fullscreen: boolean;
}) {
  const { t_i18n } = useFormatter();
  const groups: { title: string; rows: ShortcutRow[] }[] = [
    {
      title: t_i18n("Playback"),
      rows: [
        { label: t_i18n("Play or pause"), keys: ["Space", "K"] },
        { label: t_i18n("Back 10 seconds"), keys: ["J"] },
        { label: t_i18n("Forward 10 seconds"), keys: ["L"] },
        { label: t_i18n("Back or forward 5 seconds"), keys: ["←", "→"] },
        { label: t_i18n("Previous or next frame, paused"), keys: [",", "."] },
        { label: t_i18n("Slower or faster"), keys: ["<", ">"] },
        { label: t_i18n("Jump to a tenth of the video"), keys: ["0 - 9"] },
        { label: t_i18n("Start or end"), keys: ["Home", "End"] },
      ],
    },
    {
      title: t_i18n("Sound and view"),
      rows: [
        { label: t_i18n("Volume up or down"), keys: ["↑", "↓"] },
        { label: t_i18n("Mute or unmute"), keys: ["M"] },
        ...(captions ? [{ label: t_i18n("Captions on or off"), keys: ["C"] }] : []),
        ...(fullscreen ? [{ label: t_i18n("Full screen"), keys: ["F"] }] : []),
        { label: t_i18n("Keyboard shortcuts"), keys: ["?"] },
      ],
    },
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent container={container} className="max-w-3xl" data-testid="video.player.shortcuts">
        <DialogHeader>
          <DialogTitle>{t_i18n("Keyboard shortcuts")}</DialogTitle>
          <DialogDescription>{t_i18n("The player answers these keys while it has the focus.")}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-6 sm:grid-cols-2">
          {groups.map((group) => (
            <section key={group.title} aria-label={group.title}>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-default-secondary">{group.title}</h3>
              <dl className="divide-y divide-[var(--border-elevation-subtle)]">
                {group.rows.map((row) => (
                  <div key={row.label} className="flex items-center justify-between gap-4 py-2">
                    <dt className="shrink-0 whitespace-nowrap content-base text-default-primary">{row.label}</dt>
                    <dd className="flex min-w-0 flex-wrap items-center justify-end gap-1.5">
                      {row.keys.map((key, i) => (
                        <span key={key} className="inline-flex items-center gap-1">
                          {i > 0 && <span className="content-caption text-default-secondary">{t_i18n("or")}</span>}
                          <Kbd>{key}</Kbd>
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
