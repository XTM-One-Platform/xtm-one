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
 * The chat surface for a `[[PRESENTATION:uuid]]` marker.
 *
 * A DECK renders inline as a `DeckStage`: one compact toolbar along the top
 * (title, slide counter, navigation, fullscreen, share, export) and the slide
 * under it with nothing drawn over it. Fullscreen takes the whole stage —
 * toolbar included, tucked away while the presenter is still — so a deck can be
 * presented straight from the conversation.
 *
 * A PAGE document (A4 / letter — a product brief, a one-pager) is not a slide
 * show and renders as a `PageDocument` instead: the same compact bar with a
 * page counter and zoom, and the pages stacked in a scrolling surface under it,
 * the way a PDF attachment previews. No fullscreen, no overview.
 *
 * Both stages (reveal.js + its CSS, ~1 MB; the slide renderer) are code-split
 * behind `React.lazy`: a conversation that never generated a deck must not pay
 * for the library, and the chunk is fetched the first time a marker renders.
 * The controller hooks are light (types only) and stay in the main bundle.
 *
 * The card is a preview, the viewer is the workplace: "Open in studio" lands
 * on `/presentations/:id` in THIS tab — the same page a click in the library
 * opens, with the editor panel, sharing, comments and history — with the
 * studio rail out, and the comment badge lands there with the comments panel
 * already open. Presence is deliberately not shown here: a conversation may
 * hold many decks, and a heartbeat per card would be a poll per deck for a
 * room nobody is looking at.
 *
 * "Open in studio" BINDS the conversation the card sits in to the deck before
 * it navigates, so the rail resumes THAT conversation and the agent keeps
 * everything it already knows about the work. Inside the rail itself the card
 * collapses to one line (`PresentationCardContext`): the deck is already on
 * the stage beside it, and a second copy of it in a 420px column would only
 * compete with the first.
 *
 * A marker that names the slide the agent wrote (`[[PRESENTATION:id#/<slide>]]`)
 * opens the embedded deck ON that slide, and the studio receipt says
 * which slide changed and offers to go there. The embedded stage also takes
 * slide links clicked in the transcript (`registerSlideStage`), so "see
 * [slide 4](...)" in the reply moves this card instead of opening a tab.
 */
import { Suspense, lazy, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowRight,
  Check,
  FileText,
  Presentation as PresentationIcon,
  Printer,
  Share2,
} from "lucide-react";

import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { Button } from "@/components/ui/button";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useBindConversationPresentation } from "@/hooks/api/useChatApi";
import { usePresentation } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";

import { useDeckController } from "./deck-controller";
import type { DeckStageHandle } from "./DeckStage";
import { deckAspectRatio } from "./geometry";
import { usePageController } from "./page-controller";
import { PresentationCardContext } from "./presentation-card-context";
import { PresentationExportMenu } from "./PresentationExportMenu";
import { PresentationShareDialog } from "./PresentationShareDialog";
import { useSchemeLabel } from "./scheme-label";
import { resolveSlideRef } from "./slide-links";
import { studioUrl } from "./studio-url";
import { Spinner } from "@filigran/design-system";

const DeckStage = lazy(() => import("./DeckStage").then((mod) => ({ default: mod.DeckStage })));
const PageDocument = lazy(() =>
  import("./PageDocument").then((mod) => ({ default: mod.PageDocument })),
);

/** Height of the embedded document's scrolling surface in the message flow. */
const PAGE_SURFACE_CLASS = "h-[560px]";

/** Shared shell so the loading, error and loaded states share one frame. */
function CardFrame({
  children,
  className,
  frameRef,
}: {
  children: React.ReactNode;
  className?: string;
  frameRef?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={frameRef}
      className={cn(
        "my-2 overflow-hidden rounded-sm border border-accent/25 layer-1 bg-elevation-default",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function PresentationCard({
  presentationId,
  focusSlideRef = null,
}: {
  presentationId: string;
  /** The slide the marker points at (`#/<slide id>`), if it names one. */
  focusSlideRef?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { conversationId, studioPresentationId, navigateToSlide, registerSlideStage } =
    useContext(PresentationCardContext);
  const { data, isLoading, isError } = usePresentation(presentationId);
  const controller = useDeckController();
  const pageController = usePageController();
  const bindConversation = useBindConversationPresentation();
  const [sharing, setSharing] = useState(false);
  const frameRef = useRef<HTMLDivElement>(null);

  const isPage = data?.kind === "page";
  const isReceipt = !!data && studioPresentationId === data.id;
  const spec = data?.spec;
  const schemeLabel = useSchemeLabel();
  const themeLabel = schemeLabel(spec?.theme === "light" ? "light" : "dark");
  // The stage this card mounts, whichever kind; both controllers are cheap.
  const { ready: stageReady, goTo } = isPage ? pageController : controller;

  // The embedded stage takes slide links for its deck: a link in the
  // reply moves THIS card and brings it into view, rather than opening a tab.
  // Only once the stage is up — before that the link is left to the viewer.
  const takeSlideLink = useCallback(
    (slideRef: string) => {
      if (!stageReady) return false;
      const index = resolveSlideRef(spec, slideRef);
      if (index === null) return false;
      goTo(index);
      frameRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return true;
    },
    [goTo, spec, stageReady],
  );
  // Registered ONCE per mount through a stable wrapper: the transcript ranks
  // its stages by mount order, and a re-registration on every refetch would
  // let an older card overtake the one the agent just wrote about.
  const takeSlideLinkRef = useRef(takeSlideLink);
  takeSlideLinkRef.current = takeSlideLink;
  const stage = useCallback((slideRef: string) => takeSlideLinkRef.current(slideRef), []);
  const stageDeckId = isReceipt ? null : data?.id;
  useEffect(() => {
    if (!registerSlideStage || !stageDeckId) return;
    return registerSlideStage(stageDeckId, stage);
  }, [registerSlideStage, stage, stageDeckId]);

  // A marker that names the slide the agent wrote opens the deck on it — once,
  // when the stage first reports ready; the reader's own navigation after that
  // is never overridden, and a refetch of the deck does not jump back.
  const focusApplied = useRef(false);
  useEffect(() => {
    if (focusApplied.current || focusSlideRef === null || !stageReady || !spec || isReceipt) return;
    focusApplied.current = true;
    const index = resolveSlideRef(spec, focusSlideRef);
    if (index !== null && index > 0) goTo(index);
  }, [focusSlideRef, goTo, isReceipt, spec, stageReady]);

  // Into the studio WITH this conversation: bind it to the deck first, so the
  // rail resumes it and the agent keeps what it already knows about the work.
  // A conversation that is not the user's own (a shared transcript) cannot be
  // bound — the studio then opens on the deck's own history instead.
  const openStudio = async () => {
    if (conversationId && data) {
      try {
        await bindConversation.mutateAsync({ conversationId, presentationId: data.id });
        navigate(studioUrl(data.id, conversationId));
        return;
      } catch {
        // Fall through to the unbound studio.
      }
    }
    if (data) navigate(studioUrl(data.id));
  };

  if (isLoading) {
    return (
      <CardFrame className="flex items-center gap-2 px-3 py-2.5 text-[13px] text-default-secondary">
        <Spinner size="sm" tone="inherit" className="text-accent" />
        {t_i18n("Loading presentation…")}
      </CardFrame>
    );
  }

  if (isError || !data) {
    return <UnavailableCard message={t_i18n("This presentation is no longer available.")} />;
  }

  // Inside the studio rail, a card for the deck on the stage is a receipt, not
  // a second copy of the deck: the save it announces is already on the right.
  // When the marker names the slide that changed, the receipt says which and
  // offers to put it on the stage — the change is one click away
  // instead of "saved" and a hunt through the deck.
  if (isReceipt) {
    const focusIndex = focusSlideRef === null ? null : resolveSlideRef(data.spec, focusSlideRef);
    const position = focusIndex === null ? null : focusIndex + 1;
    return (
      <CardFrame className="flex items-center gap-2 px-3 py-2 text-[12px] text-default-secondary">
        <Check className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate">
          {position === null
            ? t_i18n("Saved - the presentation on the stage shows the latest version.")
            : isPage
              ? t_i18n("Saved - page {position} changed.", { position })
              : t_i18n("Saved - slide {position} changed.", { position })}
        </span>
        {position !== null && focusSlideRef !== null && navigateToSlide && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigateToSlide(data.id, focusSlideRef)}
            className="h-6 shrink-0 gap-1 px-2 text-[12px] text-accent hover:text-accent"
          >
            {isPage
              ? t_i18n("Go to page {position}", { position })
              : t_i18n("Go to slide {position}", { position })}
            <ArrowRight className="h-3 w-3" aria-hidden="true" />
          </Button>
        )}
      </CardFrame>
    );
  }

  const viewerUrl = `/presentations/${data.id}`;
  const aspect = deckAspectRatio(data.spec.aspect_ratio);
  // Collaboration lives in the viewer: the card only opens it, on the right
  // panel. `onOpenEditor` opens the STUDIO (the chat beside the deck) for
  // whoever may edit the deck; a read-only reader has no studio, so
  // their bar keeps `Open in a new tab`, and comments still land in the viewer.
  const collaboration = {
    onOpenEditor: data.can_manage ? () => void openStudio() : undefined,
    onToggleComments: () => navigate(`${viewerUrl}?panel=comments`),
    openCommentCount: data.open_comment_count,
  };

  const leading = (
    <>
      {isPage ? (
        <FileText className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      ) : (
        <PresentationIcon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      )}
      <span className="truncate text-[13px] font-medium text-default-primary">{data.title}</span>
      {isPage && (
        <span className="hidden shrink-0 rounded-sm border border-elevation-subtle-soft px-1 font-mono-plex content-caption uppercase text-default-secondary sm:inline">
          {aspect}
        </span>
      )}
      {/* The design the deck wears: its template, or none, and its
          theme — so "which template did it use?" is answered on the card. */}
      <span className="hidden min-w-0 truncate text-[11px] text-default-secondary md:inline">
        {data.template_name
          ? t_i18n("{template} - {theme} theme", { template: data.template_name, theme: themeLabel })
          : t_i18n("No template - {theme} theme", { theme: themeLabel })}
      </span>
    </>
  );

  if (isPage) {
    // Sharing is a manage-tier action (owner / config grantee / admin); a
    // read-only grantee sees the document without the button.
    const pageActions = (
      <>
        {data.can_manage && (
          <Tip content={t_i18n("Share")}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSharing(true)}
              aria-label={t_i18n("Share")}
              className="h-7 w-7 px-0"
            >
              <Share2 className="h-3.5 w-3.5" />
            </Button>
          </Tip>
        )}
        {/* Server-side exports (vector PDF on real paper, PPTX). */}
        <PresentationExportMenu presentationId={data.id} compact className="h-7 w-7" />
      </>
    );
    return (
      <CardFrame frameRef={frameRef}>
        <Suspense
          fallback={
            <div className={cn("flex w-full items-center justify-center", PAGE_SURFACE_CLASS)}>
              <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
            </div>
          }
        >
          <PageDocument
            presentation={data}
            controller={pageController}
            compact
            leading={leading}
            actions={pageActions}
            viewerUrl={viewerUrl}
            surfaceClassName={PAGE_SURFACE_CLASS}
            {...collaboration}
          />
        </Suspense>
        {data.can_manage && (
          <PresentationShareDialog presentation={data} open={sharing} onOpenChange={setSharing} />
        )}
      </CardFrame>
    );
  }

  // Sharing is a manage-tier action (owner / config grantee / admin); a
  // read-only grantee sees the deck without the button. The dialog
  // lives outside the stage, so a fullscreen stage is left before it opens.
  const actions = (stage: DeckStageHandle) => (
    <>
      {data.can_manage && (
        <Tip content={t_i18n("Share")} container={stage.portalContainer}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              if (stage.fullscreen.isFullscreen) stage.fullscreen.exit();
              setSharing(true);
            }}
            aria-label={t_i18n("Share")}
            className="h-7 w-7 px-0"
          >
            <Share2 className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      )}
      {/* Server-side exports (vector PDF, PPTX) — pixel-identical to the deck. */}
      <PresentationExportMenu presentationId={data.id} compact className="h-7 w-7" />
    </>
  );

  const menuItems: OverflowMenuItem[] = [
    {
      // reveal's own print layout needs a page of its own — it lays every slide
      // out as a printable page there and opens the print dialog.
      id: "print",
      label: t_i18n("Print to PDF (browser)"),
      icon: Printer,
      onSelect: () => window.open(withBasePath(`${viewerUrl}?print-pdf`), "_blank", "noopener"),
    },
  ];

  return (
    <CardFrame frameRef={frameRef}>
      <Suspense
        fallback={
          <div
            className="deck-aspect-box flex max-h-[420px] w-full items-center justify-center"
            data-aspect={aspect}
          >
            <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
          </div>
        }
      >
        <DeckStage
          presentation={data}
          variant="embedded"
          controller={controller}
          compact
          leading={leading}
          actions={actions}
          menuItems={menuItems}
          viewerUrl={viewerUrl}
          // A box at the deck's own aspect (16:9 / 4:3) in the message flow;
          // fullscreen overrides it. Past the height cap (a wide column) the
          // stage letterboxes the deck in the app background.
          surfaceClassName="deck-aspect-box max-h-[420px] w-full"
          {...collaboration}
        />
      </Suspense>
      {data.can_manage && (
        <PresentationShareDialog presentation={data} open={sharing} onOpenChange={setSharing} />
      )}
    </CardFrame>
  );
}
