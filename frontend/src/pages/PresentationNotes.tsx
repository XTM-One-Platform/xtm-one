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
 * Notes handout: every slide with its speaker notes, as ONE printable
 * document — the thing a presenter puts on the lectern, or hands to the
 * colleague who presents the deck in their place.
 *
 * Paper-first: the page opens to be printed (browser print -> PDF), so the
 * header is screen-only, every row refuses to break across pages, and the
 * thumbnail column keeps the same width on screen and on paper. The slides are
 * the export's own `StaticSlide` through `SlidePreview`, so the handout shows
 * the pixels the room will see, not a redrawing of them.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { AlertTriangle, Presentation as PresentationIcon, Printer } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { SlidePreview } from "@/components/presentation/SlidePreview";
import { SpeakerNotes } from "@/components/presentation/SpeakerNotes";
import { slideHeading } from "@/components/presentation/slide-label";
import {
  deckSpeakingSeconds,
  estimateSpeakingSeconds,
  slideMissesNotes,
} from "@/components/presentation/speaking-time";
import { usePresentation } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { formatDurationShort } from "@/lib/utils";
// The handout is opened in a tab of its own, so nothing else has loaded the
// deck stylesheet the thumbnails (`StaticSlide` in a `.deck-root`) are drawn with.
import "@/components/presentation/presentation.css";
import "@/components/presentation/speaker.css";
import { Spinner } from "@filigran/design-system";

export default function PresentationNotes() {
  const { id } = useParams<{ id: string }>();
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = usePresentation(id);
  const [withNotesOnly, setWithNotesOnly] = useState(false);

  const slides = useMemo(() => data?.spec.slides ?? [], [data]);
  const rows = useMemo(
    () =>
      slides
        .map((slide, index) => ({ slide, index }))
        .filter(({ slide }) => !withNotesOnly || Boolean(slide.notes?.trim())),
    [slides, withNotesOnly],
  );
  const totalSeconds = useMemo(() => deckSpeakingSeconds(slides), [slides]);
  const missing = useMemo(() => slides.filter(slideMissesNotes).length, [slides]);

  useEffect(() => {
    if (!data?.title) return;
    const previous = document.title;
    document.title = `${data.title} - ${t_i18n("Notes")}`;
    return () => {
      document.title = previous;
    };
  }, [data?.title, t_i18n]);

  if (isLoading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-elevation-default">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base font-medium">{t_i18n("This presentation is not available.")}</p>
        <p className="max-w-md content-base text-default-secondary">
          {t_i18n("It may have been deleted, or you no longer have access to it.")}
        </p>
      </div>
    );
  }

  // A page has no speaker notes surface (no entry point offers this URL for
  // one — see `kind`); a typed URL lands on the page's own reader.
  if (data.kind !== "deck") {
    return <Navigate to={`/presentations/${data.id}`} replace />;
  }

  return (
    <div className="min-h-dvh bg-elevation-default text-default-primary print:bg-elevation-default">
      <div className="handout-root" data-testid="presentation.handout">
        <div className="handout-toolbar mb-6 flex flex-wrap items-center gap-3 print:hidden">
          <Button asChild variant="outline" size="sm" className="gap-1.5">
            <Link to={`/presentations/${data.id}`}>
              <PresentationIcon className="h-4 w-4" />
              {t_i18n("Open the deck")}
            </Link>
          </Button>
          <label className="flex items-center gap-2 content-base text-default-secondary">
            <Switch checked={withNotesOnly} onCheckedChange={setWithNotesOnly} />
            {t_i18n("Only slides with notes")}
          </label>
          <div className="flex-1" />
          <Button size="sm" className="gap-1.5" onClick={() => window.print()} data-testid="presentation.handout.print">
            <Printer className="h-4 w-4" />
            {t_i18n("Print")}
          </Button>
        </div>

        <header>
          <h1 className="handout-title">{data.title}</h1>
          <p className="handout-meta">
            {t_i18n("{count, plural, one {# slide} other {# slides}}", { count: slides.length })}
            {" · "}
            {t_i18n("about {time} spoken", { time: formatDurationShort(totalSeconds) })}
            {missing > 0 && (
              <>
                {" · "}
                {t_i18n("{count, plural, one {# slide} other {# slides}} without notes", { count: missing })}
              </>
            )}
          </p>
        </header>

        <ol className="handout-list">
          {rows.map(({ slide, index }) => {
            const seconds = estimateSpeakingSeconds(slide.notes);
            return (
              <li key={slide.id ?? index} className="handout-row" data-testid="presentation.handout.row">
                <div className="handout-row-head">
                  <strong>
                    {index + 1}. {slideHeading(slide) || t_i18n("Untitled slide")}
                  </strong>
                  <span>{slide.layout}</span>
                  {seconds > 0 && <span>{t_i18n("about {time}", { time: formatDurationShort(seconds) })}</span>}
                </div>
                <SlidePreview presentation={data} slide={slide} index={index} />
                <SpeakerNotes
                  notes={slide.notes}
                  empty={slideMissesNotes(slide) ? t_i18n("No speaker notes.") : t_i18n("No notes needed.")}
                />
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
