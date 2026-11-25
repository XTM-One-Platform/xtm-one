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
 * Hand the clicker (xtm-one #10552, slice B2), the two sides of it.
 *
 * - `SpeakerClickerControl` sits in the presenter's speaker view: "Hand the
 *   clicker" opens a dialog that searches the people directory; the server
 *   refuses anyone who cannot already read the deck (404), so a public-link
 *   viewer, who has no account, can never be handed it. While someone holds
 *   it the control names them and offers "Take back" (one click), and the
 *   speaker view obeys the holder's commands relayed by the server
 *   (`useClickerRelay`). The presenter's own keys never stop working.
 * - `HolderClickerBar` is the co-presenter's remote on their own machine: it
 *   shows while they hold the clicker of the deck they have open, with the
 *   presenter's current slide, previous / next / go to slide, and "Give back".
 *   A clicker taken back or expired makes it disappear at the next read.
 */
import { useId, useState } from "react";
import { Avatar, Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useUserDirectory } from "@/hooks/api/useAuthApi";
import { usePresentationPresence } from "@/hooks/api/usePresentationApi";
import {
  handClickerRefusal,
  sendClickerCommand,
  useClickerRelay,
  useHandClicker,
  usePresentationClicker,
  useReleaseClicker,
  type ClickerCommand,
  type ClickerCommandFrame,
} from "@/hooks/api/usePresentationClicker";
import { useFormatter } from "@/i18n";

interface SpeakerClickerControlProps {
  presentationId: string;
  /** The slide the speaker view is on (0-based): the holder's bar shows it. */
  slideIndex: number;
  /** The speaker view's own moves: the relayed commands go through them. */
  onCommand: (frame: ClickerCommandFrame) => void;
}

export function SpeakerClickerControl({ presentationId, slideIndex, onCommand }: SpeakerClickerControlProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const clicker = usePresentationClicker(presentationId);
  const hand = useHandClicker(presentationId);
  const release = useReleaseClicker(presentationId);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const directory = useUserDirectory(query);
  const pickerId = useId();

  const state = clicker.data;
  const handedByMe = !!state?.holder && state.is_presenter;
  useClickerRelay(presentationId, handedByMe, onCommand);
  // The holder's bar reads the presenter's slide off the presence beat. The
  // speaker view is where the presenter drives, so while the clicker is handed
  // it beats too, fast, with its own slide: the bar never says "Slide unknown"
  // and never offers Next on the last slide.
  usePresentationPresence(presentationId, { enabled: handedByMe, slideIndex, fast: true });

  const options = (directory.data ?? []).map((person) => ({
    value: person.id,
    label: person.full_name ? `${person.full_name} (${person.email})` : person.email,
  }));

  const submit = () => {
    if (!picked) return;
    hand.mutate(picked, {
      onSuccess: () => {
        setOpen(false);
        setPicked(null);
        setQuery("");
      },
      onError: (error) => {
        const reason = handClickerRefusal(error);
        toast({
          title: t_i18n("The clicker was not handed"),
          description:
            reason === "cannot_open"
              ? t_i18n("This person cannot open the presentation. Share it with them first.")
              : reason === "unavailable"
                ? t_i18n("The clicker is unavailable right now. Try again in a moment.")
                : t_i18n("This person cannot open the presentation, or the clicker is unavailable. Try again."),
          variant: "error",
        });
      },
    });
  };

  if (handedByMe && state?.holder) {
    // The same footprint as "Hand the clicker" (one 32 px control), so the
    // header never grows mid-talk: the holder's design-system Avatar (its one
    // fill passes contrast in both themes; aria-hidden, the button carries the
    // name), the full name in the tooltip and the accessible name.
    const holderName = state.holder.name || t_i18n("Someone");
    const label = t_i18n("Clicker with {name}", { name: holderName });
    return (
      <Tip content={label}>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5"
          onClick={() => release.mutate()}
          disabled={release.isPending}
          aria-label={`${label}. ${t_i18n("Take back")}`}
          data-testid="presentation.speaker.clicker"
        >
          <Avatar name={state.holder.name || undefined} size="sm" aria-hidden />
          {t_i18n("Take back")}
        </Button>
      </Tip>
    );
  }

  return (
    <>
      <Tip content={t_i18n("Let a person who can open this presentation move the slides from their own computer")}>
        <Button
          variant="ghost"
          size="sm"
          className="h-8"
          onClick={() => setOpen(true)}
          data-testid="presentation.speaker.clicker"
        >
          {t_i18n("Hand the clicker")}
        </Button>
      </Tip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n("Hand the clicker")}</DialogTitle>
            <DialogDescription>
              {t_i18n(
                "The person opens this presentation on their computer and moves your slides. You keep control and can take it back at any time.",
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label id={`${pickerId}-label`} className="text-default-primary">
              {t_i18n("Person to hand the clicker to")}
            </Label>
            <SearchableSelect
              aria-labelledby={`${pickerId}-label`}
              aria-describedby={`${pickerId}-help`}
              options={options}
              value={picked}
              onValueChange={(value) => setPicked(value || null)}
              onSearchChange={setQuery}
              placeholder={t_i18n("Search by name or email")}
              emptyMessage={query.trim().length < 2 ? t_i18n("Type at least 2 characters") : t_i18n("No one found")}
            />
            <p id={`${pickerId}-help`} className="content-compact text-default-secondary">
              {t_i18n("Only people who can already open this presentation can take the clicker.")}
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" className="h-9" onClick={() => setOpen(false)}>
              {t_i18n("Cancel")}
            </Button>
            <Button className="h-9" onClick={submit} disabled={!picked || hand.isPending}>
              {t_i18n("Hand the clicker")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

interface HolderClickerBarProps {
  presentationId: string;
  total: number;
}

export function HolderClickerBar({ presentationId, total }: HolderClickerBarProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const clicker = usePresentationClicker(presentationId);
  const release = useReleaseClicker(presentationId);
  const state = clicker.data;
  if (!state?.is_holder) return null;

  const current = state.slide_index;
  const send = (command: ClickerCommand, index?: number) => {
    sendClickerCommand(presentationId, command, index).then(
      () => void clicker.refetch(),
      () => {
        toast({
          title: t_i18n("You no longer hold the clicker"),
          description: t_i18n("The presenter took it back or the connection was lost."),
          variant: "info",
        });
        void clicker.refetch();
      },
    );
  };

  return (
    <div
      className="pointer-events-none absolute inset-x-0 bottom-4 z-30 flex justify-center print:hidden"
      data-testid="presentation.clicker.remote"
    >
      <div
        role="group"
        aria-label={t_i18n("Clicker")}
        className="pointer-events-auto flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-default px-3 py-1.5"
      >
        <span className="content-compact text-default-primary">{t_i18n("You hold the clicker")}</span>
        <Button
          variant="outline"
          size="sm"
          className="h-9 w-9 px-0"
          onClick={() => send("prev")}
          disabled={current !== null && current <= 0}
          aria-label={t_i18n("Previous slide")}
        >
          <Icon name="chevron-left" size={16} />
        </Button>
        <span className="min-w-24 text-center content-compact text-default-secondary" aria-live="polite">
          {current === null
            ? t_i18n("Slide unknown")
            : t_i18n("Slide {current} of {total}", { current: current + 1, total })}
        </span>
        <Button
          size="sm"
          className="h-9 w-9 px-0"
          onClick={() => send("next")}
          disabled={current !== null && current >= total - 1}
          aria-label={t_i18n("Next slide")}
        >
          <Icon name="chevron-right" size={16} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-9"
          onClick={() => release.mutate()}
          disabled={release.isPending}
        >
          {t_i18n("Give back")}
        </Button>
      </div>
    </div>
  );
}
