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
 * A deck opened by its public link - `/public/presentations/:token`.
 *
 * The one deck page with no session: the token in the address is the
 * credential, and the page holds nothing else. It is the viewer stripped to
 * what an audience needs - the slides, navigation, the overview, fullscreen
 * presenting and the `#/<slide>` deep links (a page document reads as one) -
 * and nothing that would need an account or reveal who made the deck: no
 * sharing, export, studio, comments, history, presence, and no speaker view
 * or notes handout (the server serves the deck without its notes). The one
 * export is the PDF, and only when the link's managers allowed it:
 * rendered server-side from this very view, so it carries no notes either.
 *
 * Assets: `<img src>` cannot carry a credential, so `DeckAssetProvider`
 * resolves every file id to the token-gated asset route, which serves only the
 * files the deck itself shows. The deck is loaded once; a reload shows the
 * latest edit. A link that was turned off, replaced or never existed is one
 * answer (404), and the page says so without guessing which.
 */
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import {
  AlertTriangle,
  Download,
  FileText,
  Globe,
  Presentation as PresentationIcon,
  RefreshCw,
} from "lucide-react";

import { DeckAssetProvider } from "@/components/presentation/asset-context";
import { useDeckController } from "@/components/presentation/deck-controller";
import { DeckStage } from "@/components/presentation/DeckStage";
import { usePageController } from "@/components/presentation/page-controller";
import { PageDocument } from "@/components/presentation/PageDocument";
import { parseSlideFragment, resolveSlideRef } from "@/components/presentation/slide-links";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useDownloadPublicPresentationPdf, usePublicPresentation } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { API_BASE } from "@/lib/api";
import { publicAssetUrl, publicDeckAsPresentation } from "@/lib/public-deck";
import { Spinner } from "@filigran/design-system";

export default function PresentationPublic() {
  const { token } = useParams<{ token: string }>();
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError, error, refetch } = usePublicPresentation(token);
  const presentation = useMemo(() => (data ? publicDeckAsPresentation(data) : null), [data]);
  const controller = useDeckController();
  const pageController = usePageController();
  const isPage = presentation?.kind === "page";
  const live = isPage ? pageController : controller;
  const { ready, index, goTo } = live;

  const resolver = useMemo(
    () => (token ? (fileId: string) => publicAssetUrl(API_BASE, token, fileId) : null),
    [token],
  );

  // The PDF the link may offer: rendered on demand, so the button
  // spins until the file is saved, and a refusal says why in words.
  const { toast } = useToast();
  const pdf = useDownloadPublicPresentationPdf();
  const downloadPdf = useCallback(() => {
    if (!token || pdf.isPending) return;
    pdf.mutate(token, {
      onError: (err) =>
        toast({
          title: t_i18n("Could not download the PDF"),
          description:
            err.status === 429
              ? t_i18n("Too many downloads for now. Try again in a few minutes.")
              : err.status === 404
                ? t_i18n("This presentation can no longer be downloaded.")
                : t_i18n("The PDF could not be generated right now. Try again in a few minutes."),
          variant: "error",
        }),
    });
  }, [pdf, t_i18n, toast, token]);

  // The deck title is the tab title, as in the viewer.
  useEffect(() => {
    if (!presentation?.title) return;
    const previous = document.title;
    document.title = presentation.title;
    return () => {
      document.title = previous;
    };
  }, [presentation?.title]);

  // A link shared to one slide lands on it, once; the fragment then follows
  // the slide on screen (`replaceState`: no history entry per slide).
  const specRef = useRef(presentation?.spec);
  specRef.current = presentation?.spec;
  const landed = useRef(false);
  useEffect(() => {
    if (!ready || landed.current) return;
    landed.current = true;
    const ref = parseSlideFragment(window.location.hash);
    const initial = ref === null ? null : resolveSlideRef(specRef.current, ref);
    if (initial !== null && initial > 0) goTo(initial);
  }, [goTo, ready]);
  useEffect(() => {
    if (!ready) return;
    const { pathname, search } = window.location;
    window.history.replaceState(window.history.state, "", `${pathname}${search}#/${index}`);
  }, [index, ready]);

  if (isLoading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-elevation-default">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !presentation || !resolver) {
    const gone = !error || error.status === 404;
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base font-medium">
          {gone ? t_i18n("This link no longer opens a presentation.") : t_i18n("The presentation could not be loaded.")}
        </p>
        <p className="max-w-md content-base text-default-secondary">
          {gone
            ? t_i18n(
                "Whoever shared it may have turned the link off or replaced it with a new one, or the presentation was deleted. Ask them for a new link.",
              )
            : t_i18n("Check your connection and try again.")}
        </p>
        {!gone && (
          <Button variant="outline" size="sm" onClick={() => void refetch()} className="gap-1.5">
            <RefreshCw className="h-3.5 w-3.5" /> {t_i18n("Try again")}
          </Button>
        )}
      </div>
    );
  }

  const leading = (
    <>
      {isPage ? (
        <FileText className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      ) : (
        <PresentationIcon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      )}
      <h1 className="truncate content-base font-medium text-default-primary">{presentation.title}</h1>
      <Tip content={t_i18n("Anyone with this link can view this presentation")}>
        <span
          className="hidden shrink-0 items-center gap-1 rounded-sm border border-elevation-subtle-soft px-1.5 py-0.5 content-caption font-medium uppercase tracking-wide text-default-secondary sm:inline-flex"
          data-testid="presentation.public.badge"
        >
          <Globe className="h-3 w-3" aria-hidden="true" />
          {t_i18n("Public")}
        </span>
      </Tip>
    </>
  );

  // Only when the link offers it; inside the fullscreen stage its tooltip is
  // portaled into the stage, which is all that paints there.
  const pdfLabel = pdf.isPending ? t_i18n("Preparing the PDF...") : t_i18n("Download PDF");
  const pdfButton = (portalContainer?: HTMLElement | null) =>
    data?.pdf_download ? (
      <Tip content={pdfLabel} container={portalContainer}>
        <Button
          variant="ghost"
          size="sm"
          onClick={downloadPdf}
          disabled={pdf.isPending}
          aria-label={pdfLabel}
          aria-busy={pdf.isPending || undefined}
          className="h-8 gap-1.5 px-2"
          data-testid="presentation.public.downloadPdf"
        >
          {pdf.isPending && <Spinner size="sm" tone="inherit" />}{!pdf.isPending && <Download className="h-4 w-4" />}
          <span className="hidden content-compact sm:inline">{t_i18n("PDF")}</span>
        </Button>
      </Tip>
    ) : null;

  return (
    <DeckAssetProvider value={resolver}>
      <div className="flex h-dvh w-screen overflow-hidden bg-elevation-default" data-testid="presentation.public">
        {isPage ? (
          <PageDocument
            presentation={presentation}
            controller={pageController}
            keyScope="document"
            leading={leading}
            actions={pdfButton()}
            className="min-w-0 flex-1"
          />
        ) : (
          <DeckStage
            presentation={presentation}
            variant="standalone"
            controller={controller}
            leading={leading}
            actions={(stage) => pdfButton(stage.portalContainer)}
            presenterTools={false}
            className="min-w-0 flex-1"
          />
        )}
      </div>
    </DeckAssetProvider>
  );
}
