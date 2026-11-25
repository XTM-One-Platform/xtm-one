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
 * Standalone deck viewer — `/presentations/:id`.
 *
 * Rendered OUTSIDE the app shell (no sidebar, no header) so a deck can be
 * presented on a projector or shared as a link without the product chrome, and
 * so external channels have somewhere to point (the channel rewriter turns
 * `[[PRESENTATION:…]]` markers into links here).
 *
 * The page is a `DeckStage`: one toolbar along the top (navigation, overview,
 * fullscreen, the deck's actions) and the slide below it with nothing drawn over
 * it. Presenting is the stage's fullscreen, where the toolbar tucks itself away
 * until the pointer moves. Every operation has a key — `?` lists them.
 *
 * With `?print-pdf` in the URL the deck renders in reveal.js's print layout —
 * every slide becomes a printable page — and the browser print dialog opens on
 * its own. That is reveal's native, no-server PDF path. The Export menu is the
 * server-side one: the platform renders the deck in its own headless
 * browser and hands back a vector PDF or a PPTX that is pixel-identical to the
 * slides on screen.
 *
 * This is also where a SHARED deck is worked on: the toolbar offers
 * sharing, the manual editor and "Edit with my assistant", which hands the
 * deck to the user's own agent. The deck is polled while open so a
 * collaborator's edit — made from their chat, which no stream of ours
 * observes — shows up without a reload. While the editor panel holds unsaved
 * edits, the stage renders ITS draft in place of the stored deck (`draft`:
 * the spec plus the design a template change brings — palette, schemes,
 * template name), so a template or theme switch or a reorder is seen before
 * it is saved; the stored deck is back the moment the draft is.
 *
 * With the editor panel open, the STAGE is a slide editor: the
 * `useDeckEditor` session is shared by the panel (inspector, filmstrip, deck
 * settings) and the stage (`SlideEditLayer` - select a block, type in its
 * place, arrange), one draft with one undo history. Every change is saved on
 * its own (`useDeckAutosave`) and a collaborator's revision is merged
 * into the draft; leaving the editor waits for the last save, and asks only
 * when it failed.
 *
 * A PAGE document (A4 / letter — `kind: "page"`) is not presented but read, so
 * the same route renders it as a `PageDocument`: the pages stacked in a
 * scrolling surface with a page counter and zoom in the bar, and none of the
 * presenting apparatus (no fullscreen, no overview, no speaker view). The
 * `#/<index>` fragment, the editor panel, sharing and export work the same.
 *
 * Collaboration lives in the toolbar's right half: who else has the
 * deck open (presence avatars, a heartbeat while this page is mounted), review
 * COMMENTS anchored to slides, and the revision HISTORY with preview + restore.
 * One side panel is open at a time — editor, comments, history or the layout
 * check — and `?panel=comments|history|editor|layout` opens one on arrival,
 * which is how the chat card's comment badge lands here. A previewed revision
 * renders on the stage in place of the current deck (the same `shown` override
 * the editor's draft uses), with a banner saying so.
 *
 * The LAYOUT CHECK panel renders the deck headless and lists what the
 * audience would see go wrong — a slide shrunk to fit, text cut off, a label
 * running past its line — each finding a click from its slide. Its `Fix with
 * my assistant` opens the studio with the findings in the composer
 * (`studioPrimed`), so seeing and fixing are one gesture apart.
 *
 * The PRESENTATION STUDIO is the left side: a chat bound to the deck
 * (`PresentationStudioRail`), so editing with an assistant happens NEXT TO the
 * deck instead of on another page — the agent is told which deck is on the
 * stage, which slide the user is looking at and what they may do to it, and a
 * save lands on the stage as the tool returns. `?studio=1` opens it on arrival
 * (`&c=` names the conversation) and the URL follows the rail, so a reload
 * comes back to the same chat; collapsed, it leaves a thin handle along the
 * left edge that opens it again. The toolbar's `Edit with my assistant` is the
 * same toggle. The rail is outside the stage, so presenting covers it.
 *
 * The studio and the layout check are for whoever may EDIT the deck:
 * a read-only reader could act on neither, so neither is offered to them and
 * a `?studio=1` or `?panel=layout` link lands on the deck alone. Comments and
 * history stay theirs.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Copy,
  Eye,
  FileText,
  Pencil,
  Presentation as PresentationIcon,
  Printer,
  Ruler,
  Trash2,
} from "lucide-react";
import type { RevealApi } from "reveal.js";

import { useDeckController } from "@/components/presentation/deck-controller";
import { HolderClickerBar } from "@/components/presentation/ClickerControls";
import { DeckStage, type DeckStageHandle } from "@/components/presentation/DeckStage";
import { usePageController } from "@/components/presentation/page-controller";
import { PageDocument } from "@/components/presentation/PageDocument";
import { PresentationCommentsPanel } from "@/components/presentation/PresentationCommentsPanel";
import { PresentationDeck } from "@/components/presentation/PresentationDeck";
import { PresentationEditorPanel } from "@/components/presentation/PresentationEditorPanel";
import { PresentationExportMenu } from "@/components/presentation/PresentationExportMenu";
import { PresentationHistoryPanel } from "@/components/presentation/PresentationHistoryPanel";
import { PresentationLayoutPanel } from "@/components/presentation/PresentationLayoutPanel";
import { PresentationShareDialog } from "@/components/presentation/PresentationShareDialog";
import { PresentationSidePanel } from "@/components/presentation/PresentationSidePanel";
import {
  PresentationStudioHandle,
  PresentationStudioRail,
} from "@/components/presentation/PresentationStudioRail";
import { activeInlineEditor } from "@/components/presentation/inline-editor-registry";
import { specWithDraft, useDeckEditor } from "@/components/presentation/presentation-editor";
import { deckSettled, PRINT_SETTLE_MS } from "@/components/presentation/ready";
import { parseSlideFragment, resolveSlideRef } from "@/components/presentation/slide-links";
import { STUDIO_CONVERSATION_PARAM, STUDIO_PARAM } from "@/components/presentation/studio-url";
import { useDeckAutosave } from "@/components/presentation/useDeckAutosave";
import { useLayoutCheckRun } from "@/components/presentation/useLayoutCheckRun";
import { usePresentationActions } from "@/components/presentation/usePresentationActions";
import {
  STUDIO_BAR_BUTTON,
  STUDIO_BAR_TITLE,
  StudioBarLeading,
  StudioLoading,
  StudioShareButton,
  StudioToggle,
  StudioUnavailable,
} from "@/components/studio/StudioChrome";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { usePresentation, usePresentationPresence } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { cn, pluralize } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import type { PresentationDraftOverlay, PresentationSpec } from "@/types";
import { Spinner } from "@filigran/design-system";

/** The side panels, one open at a time, in one resizable slot (`PresentationSidePanel`). */
type SidePanel = "editor" | "comments" | "history" | "layout";

const SIDE_PANELS: readonly SidePanel[] = ["editor", "comments", "history", "layout"];

/** `?panel=comments` and friends — how a link (the chat card's comment badge)
 *  lands with a panel already open. Anything else is ignored. */
function parsePanelParam(value: string | null): SidePanel | null {
  return value && (SIDE_PANELS as readonly string[]).includes(value) ? (value as SidePanel) : null;
}

/**
 * Slide deep links use reveal's own `#/<index>` form (0-based, `#/` for the
 * first slide) so a link copied from any reveal deck reads the same here — and
 * `#/<slide id>`: the STABLE id the agent's slide links carry, so
 * a link to "the slide I changed" survives a reorder. The deck itself runs with
 * `hash: false` — inside the chat, reveal rewriting the fragment would
 * scroll-jack the conversation — so the standalone viewer, which owns its URL,
 * keeps the fragment in step by hand (`slide-links.ts` reads both forms).
 */
function parseSlideHash(hash: string, spec: PresentationSpec | undefined): number | null {
  const ref = parseSlideFragment(hash);
  return ref === null ? null : resolveSlideRef(spec, ref);
}

export default function PresentationViewer() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const isPrinting = searchParams.has("print-pdf");
  const { data, isLoading, isError } = usePresentation(id, { live: !isPrinting });
  // The layout check is off on a deployment without the headless
  // browser (`PRESENTATION_LAYOUT_CHECK_ENABLED=false`): its endpoint 404s, so
  // the button and the panel are not offered rather than opening on an error.
  const layoutCheckEnabled = useAuthConfig().data?.presentation_layout_check_enabled ?? true;
  // The check's run lives HERE, not in the panel: the panel is
  // unmounted when hidden, and a render that takes seconds must survive the
  // user closing it to look at the deck. The toolbar button spins while it
  // runs; the panel shows the report when it is opened again.
  const layoutCheck = useLayoutCheckRun(data);
  const actions = usePresentationActions();
  const controller = useDeckController();
  const pageController = usePageController();
  // The server's `kind` decides which stage renders; both controllers are
  // cheap, so both hooks run and the effects below follow the live one.
  const isPage = data?.kind === "page";

  const [panel, setPanel] = useState<SidePanel | null>(() => parsePanelParam(searchParams.get("panel")));
  const editing = panel === "editor";
  // The manual editing session: ONE draft for the panel's inspector
  // and the stage's in-place editing, alive while the editor panel is open on
  // a deck the user may edit. `null` until the deck has loaded.
  const editor = useDeckEditor(data, editing && !!data?.can_manage);
  // Every change saves itself: owned here, next to the session, so a
  // change made right before the panel closes is still written.
  const autosave = useDeckAutosave(editor, !!data?.can_manage);
  const [EditorConfirmEl, confirmEditor] = useConfirm();
  const autosaveRef = useRef(autosave);
  autosaveRef.current = autosave;
  // Leaving the editor saves what is left and waits for it: nothing to ask
  // when the work is saved or being saved. A text open in
  // place is committed first - leaving edit mode unmounts the field - and one
  // it refuses (markup the save would refuse) keeps the editor open, the
  // field saying why. The person is asked only when the save failed.
  const leaveEditor = useCallback(async (): Promise<boolean> => {
    const open = activeInlineEditor();
    if (open && !open.commit()) return false;
    if (await autosaveRef.current.flush()) return true;
    const ok = await confirmEditor({
      title: t_i18n("Leave without your latest changes?"),
      description: t_i18n(
        "Your latest changes to this presentation could not be saved. Stay to see why and try again, or leave and lose them.",
      ),
      confirmLabel: t_i18n("Leave anyway"),
      cancelLabel: t_i18n("Keep editing"),
      variant: "warning",
    });
    if (ok) autosaveRef.current.discard();
    return ok;
  }, [confirmEditor, t_i18n]);
  const togglePanel = useCallback(
    (next: SidePanel) => {
      void (async () => {
        if (panel === "editor" && !(await leaveEditor())) return;
        setPanel((current) => (current === next ? null : next));
      })();
    },
    [leaveEditor, panel],
  );
  const closePanel = useCallback(() => {
    void (async () => {
      if (panel === "editor" && !(await leaveEditor())) return;
      setPanel(null);
    })();
  }, [leaveEditor, panel]);
  const [sharing, setSharing] = useState(false);
  // The studio rail: out or collapsed to its handle, and the conversation it
  // is on. Both come from the URL on arrival and are written back below.
  const [studioOpen, setStudioOpen] = useState(() => searchParams.has(STUDIO_PARAM));
  const [studioConvId, setStudioConvId] = useState<string | null>(
    () => searchParams.get(STUDIO_CONVERSATION_PARAM),
  );
  const toggleStudio = useCallback(() => setStudioOpen((open) => !open), []);
  // A deck the user may not edit has no studio: the rail folds away and
  // its conversation is forgotten, which also takes both off the URL. This is
  // also where a share downgraded to read-only while the rail is out lands.
  const readOnly = !!data && !data.can_manage;
  useEffect(() => {
    if (!readOnly) return;
    setStudioOpen(false);
    setStudioConvId(null);
  }, [readOnly]);
  // A message the layout panel hands the studio composer: opening the
  // rail and priming it is one gesture; the rail clears it once it is in the box.
  const [studioPrimed, setStudioPrimed] = useState<string | null>(null);
  const fixWithAssistant = useCallback((message: string) => {
    setStudioPrimed(message);
    setStudioOpen(true);
  }, []);
  const clearStudioPrimed = useCallback(() => setStudioPrimed(null), []);
  // Whether the rail's assistant has a turn in flight — reported by
  // the rail, forwarded to presence; a collapsed rail reports `false`.
  const [studioBusy, setStudioBusy] = useState(false);
  // The editor panel's unsaved draft, rendered in place of the stored spec
  // while it exists (`null` = show the deck as stored).
  const [draft, setDraft] = useState<PresentationDraftOverlay | null>(null);
  // The revision the history panel is previewing (`null` = the current deck):
  // which one, and its spec once fetched.
  const [previewRevision, setPreviewRevision] = useState<number | null>(null);
  const [previewSpec, setPreviewSpec] = useState<PresentationSpec | null>(null);
  const initialHashApplied = useRef(false);

  // The deck title is the tab title: a presented deck usually ends up on a
  // second screen where the tab is the only label the presenter sees.
  useEffect(() => {
    if (!data?.title) return;
    const previous = document.title;
    document.title = data.title;
    return () => {
      document.title = previous;
    };
  }, [data?.title]);

  // The controller is a fresh snapshot every render; the effects below depend
  // on the pieces they read (`goTo` is stable, `index` / `ready` are values).
  // A page document reports the page under the reading line the same way a
  // deck reports its slide, so the fragment keeps working for both.
  const live = isPage ? pageController : controller;
  const { ready: deckReady, index: deckIndex, goTo } = live;
  // A collaborator's revision merged in may move the slide on stage (a slide
  // inserted before it): the session follows it by id through this.
  if (editor) editor.stageIndex.current = deckReady ? deckIndex : null;

  // A structural edit (a slide inserted, duplicated, deleted or moved) asks
  // the stage to land on a slide by index. Honoured HERE, after this render's
  // child effects - the stage renders the editor's slides in this same render
  // (see `liveSpec` below) and the deck has synced to them by the time a
  // parent's effect runs - because a `goTo` issued from the panel's click ran
  // against the sections reveal had before the change and landed one place
  // off.
  useEffect(() => {
    if (!editor || !deckReady || !editing) return;
    const request = editor.focusRequest.current;
    if (request === null) return;
    editor.focusRequest.current = null;
    goTo(Math.max(0, Math.min(request, editor.draft.slides.length - 1)));
  });

  // The stage's spec while the editor is open: the SLIDES come straight from
  // the editor session, in the same render as the edit - the stage's
  // outlines and floating bars measure the DOM right after a change, and a
  // move or an insert seen one render late would leave them on the element
  // that used to hold the path. Clean or not: the autosave saves
  // every few seconds, and swapping the stage between the draft and the
  // stored deck on each save would redraw every slide for nothing. The
  // design the panel resolves from the template (palette, schemes, chrome)
  // rides the overlay it publishes one render behind, which draws nothing
  // structural.
  const liveDraft = editing && data?.can_manage && editor ? editor.draft : null;
  const liveSpec = useMemo(() => (data && liveDraft ? specWithDraft(data.spec, liveDraft) : null), [data, liveDraft]);
  // The stored spec resolves slide ids in the fragment; kept in a ref for the
  // `hashchange` listener, which must not re-subscribe on every refetch.
  const spec = data?.spec;
  const specRef = useRef(spec);
  specRef.current = spec;

  // "I have this deck open" — and who else does. Not while printing:
  // the headless export browser is not a reader. "Editing" is a change on its
  // way or just made, never an open panel: the editor's
  // changes being saved (and a little after), or the rail's assistant
  // mid-turn — the latter only times the beat, the server reads the turn
  // itself. An open, idle rail or panel is viewing. While the editor is open,
  // the beat also says which slide and block are selected, so the others see
  // where the person works, and it runs FAST - a collaborator's save then
  // reaches the stage within seconds (the room answers the deck's revision).
  const stageSlideId = deckReady ? ((liveSpec ?? data?.spec)?.slides[deckIndex]?.id ?? null) : null;
  const selectedBlock =
    editing && editor?.selection && editor.selection.slide === deckIndex ? editor.selection.block : null;
  const viewers = usePresentationPresence(id, {
    enabled: !isPrinting && !!data,
    editing: autosave.active,
    assistantBusy: studioBusy,
    slideIndex: deckReady ? deckIndex : undefined,
    slideId: stageSlideId,
    block: selectedBlock,
    fast: editing,
  });

  // The studio's state is part of the viewer URL (`?studio=1&c=<conversation>`)
  // so a reload, or a link copied mid-session, comes back to the same rail on
  // the same chat. The conversation outlives a collapse: `?c=` alone means
  // "rail folded away, this chat behind the handle", so a reload while it is
  // collapsed still brings the same conversation back on the next expand
  // rather than whichever one is most recent. Written with `replaceState` like
  // the slide fragment below: the router's `setSearchParams` would drop that
  // `#/<n>` fragment, and a studio toggle is a change of view, not a
  // navigation to undo. (The share dialog copies the canonical deck URL, so
  // neither parameter travels with a shared link.)
  useEffect(() => {
    if (isPrinting) return;
    const url = new URL(window.location.href);
    if (studioOpen) url.searchParams.set(STUDIO_PARAM, "1");
    else url.searchParams.delete(STUDIO_PARAM);
    if (studioConvId) url.searchParams.set(STUDIO_CONVERSATION_PARAM, studioConvId);
    else url.searchParams.delete(STUDIO_CONVERSATION_PARAM);
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url.href);
    }
  }, [isPrinting, studioConvId, studioOpen]);

  // A reload mid-presentation, or a link shared to one slide, lands on that
  // slide instead of the title — applied once, when the deck first reports
  // ready.
  useEffect(() => {
    if (isPrinting || !deckReady || initialHashApplied.current) return;
    initialHashApplied.current = true;
    const initial = parseSlideHash(window.location.hash, specRef.current);
    if (initial !== null && initial > 0) goTo(initial);
  }, [deckReady, goTo, isPrinting]);

  // The URL fragment follows the slide on screen (`replaceState`: a 40-slide
  // walk must not leave 40 history entries behind the back button).
  useEffect(() => {
    if (isPrinting || !deckReady) return;
    const { pathname, search } = window.location;
    window.history.replaceState(window.history.state, "", `${pathname}${search}#/${deckIndex}`);
  }, [deckIndex, deckReady, isPrinting]);

  // A fragment edited in the address bar (or a `#/n` link followed inside the
  // open viewer) is a same-document navigation: no reload, so follow it here.
  // `replaceState` above fires no `hashchange`, so this cannot loop.
  const indexRef = useRef(deckIndex);
  indexRef.current = deckIndex;
  useEffect(() => {
    if (isPrinting) return;
    const onHashChange = () => {
      const index = parseSlideHash(window.location.hash, specRef.current);
      if (index !== null && index !== indexRef.current) goTo(index);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [goTo, isPrinting]);

  // Browser-print path. Print only once reveal reports ready AND the deck fonts
  // are loaded: print view is set up during initialization (asynchronously,
  // across animation frames), and a print captured before the self-hosted
  // fonts arrive falls back to system fonts — a different deck from the one on
  // screen. The wait is bounded: a figure that cannot finish never
  // keeps the dialog from opening, and the reader is told what printed. The
  // notice is read through a ref: the handler is handed to reveal once and
  // keeps its identity.
  const unfinishedPrintNotice = useRef(() => {});
  unfinishedPrintNotice.current = () =>
    toast({
      title: t_i18n("Part of the deck was not finished"),
      description: t_i18n(
        "The deck was printed as it stood on screen: a chart, diagram, icon, picture or font had not finished loading, or could not be drawn.",
      ),
      variant: "info",
      durationMs: 10_000,
    });
  const handlePrintReady = useCallback((deck: RevealApi) => {
    // Reveal builds its print layout asynchronously (`pdf-ready` fires once
    // every slide sits in its own `.pdf-page`), and the deck's lazy content
    // (fonts, images, icons, Mermaid) lands on its own schedule — printing
    // before either is done captures blank pages.
    const root = deck.getRevealElement();
    if (!root) return;
    const printWhenSettled = () => {
      // Reveal marks every non-current slide `hidden` and, in print mode,
      // relies on its own `display: block !important` to show them all —
      // but Tailwind's `[hidden]` reset is a LAYERED `!important`, which
      // outranks reveal's unlayered one, so every page prints empty unless
      // the attribute goes. Nothing re-adds it: print mode unbinds
      // navigation and this page never calls `slide()` while printing.
      root.querySelectorAll("section[hidden], section[aria-hidden]").forEach((section) => {
        section.removeAttribute("hidden");
        section.removeAttribute("aria-hidden");
      });
      // Reveal sizes each page 1px short of the paper (its `pdfPageHeightOffset`,
      // there so in-browser printing never spills a blank trailing page), so
      // the paper itself shows through between pages — and it is `<body>`,
      // white by default. Paint it the deck's own background instead.
      const paper = window.getComputedStyle(root).getPropertyValue("--deck-bg").trim();
      if (paper) document.body.style.background = paper;
      void deckSettled(root, { bound: PRINT_SETTLE_MS }).then(({ complete }) => {
        window.print();
        // `print()` holds the page until the dialog closes, so this is read on
        // the way back: what printed was the deck as it stood on screen.
        if (!complete) unfinishedPrintNotice.current();
      });
    };
    if (root.querySelector(".pdf-page")) {
      printWhenSettled();
    } else {
      deck.on("pdf-ready", printWhenSettled);
    }
  }, []);

  if (isLoading) return <StudioLoading />;

  if (isError || !data) {
    // The backend answers 404 both for a deleted deck and for one the caller
    // was never granted (it must not confirm that a deck exists to someone
    // who cannot read it), so this page cannot tell the two apart — and it is
    // where a pasted link lands. Name both causes: "no longer available"
    // alone sent recipients hunting for a deck that was simply never shared.
    return (
      <StudioUnavailable
        title={t_i18n("This presentation is not available.")}
        description={t_i18n(
          "It may have been deleted, or it has not been shared with you. The link alone does not grant access. Ask whoever sent it to share the presentation with you.",
        )}
        backLabel={t_i18n("Back to Presentations")}
        onBack={() => navigate("/presentations")}
      />
    );
  }

  if (isPrinting) {
    // Printing must not be confined to one viewport: reveal's print layout is
    // as tall as the deck has pages, and a `h-screen` wrapper would export
    // page 1 only.
    return (
      <div className="min-h-screen w-full bg-elevation-default">
        <PresentationDeck presentation={data} variant="print" onReady={handlePrintReady} />
      </div>
    );
  }

  // What the stage renders: a revision the history panel is previewing, else
  // the editor's live draft while it has unsaved edits, else the stored deck.
  // The toolbar title follows the spec on stage (a previewed revision or a
  // retitled draft reads as what it is); everything else (actions, sharing,
  // export) keeps working from the stored `data`.
  const previewed = panel === "history" && previewRevision !== null ? previewSpec : null;
  const shown = previewed
    ? { ...data, spec: previewed, title: previewed.title }
    : liveSpec
      ? { ...data, ...(draft ?? {}), spec: liveSpec, title: liveSpec.title }
      : data;

  // The side panels and the share dialog live OUTSIDE the stage, so a
  // fullscreen stage leaves it before opening either — otherwise they would
  // open behind the fullscreen element, invisible. (The stage does the same
  // for the toolbar's own comments / history buttons.)
  const leaveFullscreenThen = (stage: DeckStageHandle, fn: () => void) => {
    if (stage.fullscreen.isFullscreen) stage.fullscreen.exit();
    fn();
  };

  // The toolbar's collaboration half, identical for a deck and a page.
  const collaboration = {
    onToggleComments: () => togglePanel("comments"),
    commentsOpen: panel === "comments",
    openCommentCount: data.open_comment_count,
    onToggleHistory: () => togglePanel("history"),
    historyOpen: panel === "history",
    presence: viewers,
  };

  // The left side: the studio rail, or the handle that brings it out. Both
  // stay out of the print path (this branch is already past it) and under a
  // presented stage, which covers the viewport. Nothing at all on a deck the
  // user may not edit, from the first render (the effect above only tidies
  // the state and the URL behind it).
  const studio = !data.can_manage ? null : studioOpen ? (
    <PresentationStudioRail
      presentation={data}
      activeIndex={live.index}
      onSelectSlide={live.goTo}
      initialConversationId={studioConvId}
      onConversationChange={setStudioConvId}
      onCollapse={() => setStudioOpen(false)}
      primedMessage={studioPrimed}
      onPrimedMessageApplied={clearStudioPrimed}
      onBusyChange={setStudioBusy}
    />
  ) : (
    <PresentationStudioHandle onOpen={() => setStudioOpen(true)} />
  );

  // The layout check tells an editor what to trim; a reader could act
  // on none of it, and the deployment may have no headless browser.
  const layoutCheckOffered = layoutCheckEnabled && data.can_manage;

  // One resizable slot for whichever panel is open: the width is the
  // reader's and shared by all four, so switching panels keeps it.
  const panelOpen =
    (panel === "editor" && data.can_manage) ||
    panel === "comments" ||
    panel === "history" ||
    (panel === "layout" && layoutCheckOffered);
  const sidePanel = panelOpen ? (
    <PresentationSidePanel>
      {panel === "editor" && data.can_manage && editor && (
        <PresentationEditorPanel
          editor={editor}
          autosave={autosave}
          collaborators={viewers}
          activeIndex={live.index}
          onSelectSlide={live.goTo}
          onDraftChange={setDraft}
          onClose={closePanel}
        />
      )}
      {panel === "comments" && (
        <PresentationCommentsPanel
          presentation={data}
          activeIndex={live.index}
          onSelectSlide={live.goTo}
          onClose={closePanel}
        />
      )}
      {panel === "history" && (
        <PresentationHistoryPanel
          presentation={data}
          selected={previewRevision}
          onSelect={setPreviewRevision}
          onPreview={setPreviewSpec}
          onClose={closePanel}
        />
      )}
      {panel === "layout" && layoutCheckOffered && (
        // Keyed by the deck: the router reuses this page when only `:id`
        // changes (a duplicate lands on its copy), and the panel's run-once
        // guard belongs to the deck it was made for (the report itself is
        // scoped by `useLayoutCheckRun`).
        <PresentationLayoutPanel
          key={data.id}
          presentation={data}
          check={layoutCheck}
          activeIndex={live.index}
          onSelectSlide={live.goTo}
          onFixWithAssistant={fixWithAssistant}
          onClose={closePanel}
        />
      )}
    </PresentationSidePanel>
  ) : null;

  // The stage edits in place while the editor panel is open.
  const stageEditor = editing && data.can_manage ? editor : null;

  // The layout check toggle — the same icon button the toolbar draws for
  // comments and history, placed with the deck's actions (it is one for both
  // a deck and a page), offered to an editor where the deployment runs the
  // check. While a check runs — panel open or hidden — a spinner sits on the
  // icon's corner, where the comments button pins its count: closing the
  // panel hides the report, not the run, and the label says what is
  // running while nothing else on screen does.
  const layoutCheckLabel =
    panel === "layout"
      ? t_i18n("Hide the layout check")
      : layoutCheck.isPending
        ? t_i18n("Layout check: rendering {count} {unit}...", {
            count: data.slide_count,
            unit: t_i18n(pluralize(data.slide_count, isPage ? "page" : "slide")),
          })
        : t_i18n("Layout check");
  const layoutCheckButton = (onClick: () => void, portalContainer?: HTMLElement | null) =>
    layoutCheckOffered ? (
      <Tip content={layoutCheckLabel} container={portalContainer}>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClick}
          aria-label={layoutCheckLabel}
          aria-pressed={panel === "layout"}
          aria-busy={layoutCheck.isPending || undefined}
          className={cn(STUDIO_BAR_BUTTON, "relative", panel === "layout" && "bg-accent/15 text-accent")}
          data-testid="presentation.layoutCheck"
        >
          <Ruler className="h-4 w-4" />
          {layoutCheck.isPending && (
            <span
              aria-hidden="true"
              className="absolute -right-0.5 -top-0.5 flex h-[14px] w-[14px] items-center justify-center rounded-full layer-1 bg-elevation-default"
              data-testid="presentation.layoutCheck.running"
            >
              <Spinner size="sm" tone="inherit" className="text-accent" />
            </span>
          )}
        </Button>
      </Tip>
    ) : null;

  // Over the stage while an older revision is on it: the deck on screen is not
  // the deck that will be presented, and the reader must not forget that.
  const previewBanner = previewed && previewRevision !== null && (
    <div className="pointer-events-none absolute inset-x-0 bottom-4 z-30 flex justify-center print:hidden">
      <div className="pointer-events-auto flex items-center gap-2 rounded-sm border border-feedback-alert-primary/40 bg-elevation-default px-3 py-1.5 content-compact text-feedback-warning-primary shadow-overlay backdrop-blur-sm">
        <Eye className="h-3.5 w-3.5" aria-hidden="true" />
        <span>
          {t_i18n("Previewing revision {revision} - not the current presentation", {
            revision: previewRevision,
          })}
        </span>
        <button
          type="button"
          onClick={() => setPreviewRevision(null)}
          className="font-medium text-default-primary underline-offset-2 hover:underline"
        >
          {t_i18n("Back to current")}
        </button>
      </div>
    </div>
  );

  const leading = (
    <>
      <StudioBarLeading
        backLabel={t_i18n("Back to Presentations")}
        // Leaving the deck with unsaved manual edits asks first, like
        // closing the panel does.
        onBack={() => {
          void (async () => {
            if (editing && !(await leaveEditor())) return;
            navigate("/presentations");
          })();
        }}
        icon={isPage ? FileText : PresentationIcon}
      >
        <h1 className={STUDIO_BAR_TITLE}>{data.title}</h1>
      </StudioBarLeading>
      {isPage && (
        // `deck-toolbar-chip` leaves a narrow bar (presentation.css), so the
        // leading region never holds more than it has room for (#9375, B1 of
        // #10528 r1).
        <span className="deck-toolbar-chip hidden shrink-0 rounded-sm border border-elevation-subtle-soft px-1 font-mono-plex content-caption uppercase text-default-secondary sm:inline">
          {data.aspect_ratio}
        </span>
      )}
    </>
  );

  const menuItems: OverflowMenuItem[] = [
    {
      // reveal's own print layout, no server involved: the fallback when the
      // platform's headless browser is unavailable.
      id: "print",
      label: t_i18n("Print to PDF (browser)"),
      icon: Printer,
      onSelect: () => window.open(withBasePath(`/presentations/${data.id}?print-pdf`), "_blank", "noopener"),
    },
    {
      id: "duplicate",
      label: t_i18n("Duplicate"),
      icon: Copy,
      onSelect: () => void actions.duplicate(data),
    },
    ...(data.can_manage
      ? [
          {
            id: "delete",
            label: t_i18n("Delete"),
            icon: Trash2,
            destructive: true,
            onSelect: () =>
              void actions.remove(data, { afterDelete: () => navigate("/presentations") }),
          },
        ]
      : []),
  ];

  if (isPage) {
    // A document is never fullscreen, so its actions need no stage handle:
    // the panel and the dialog simply open next to it.
    const pageActions = (
      <>
        {data.can_manage && (
          <Tip content={t_i18n("Edit pages")}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => togglePanel("editor")}
              aria-label={t_i18n("Edit pages")}
              aria-pressed={editing}
              className={cn(STUDIO_BAR_BUTTON, editing && "bg-accent/15 text-accent")}
            >
              <Pencil className="h-4 w-4" />
            </Button>
          </Tip>
        )}
        {layoutCheckButton(() => togglePanel("layout"))}
        {data.can_manage && (
          <StudioToggle open={studioOpen} onToggle={toggleStudio} testId="presentation.studio" />
        )}
        {data.can_manage && <StudioShareButton onClick={() => setSharing(true)} />}
        <PresentationExportMenu presentationId={data.id} compact />
      </>
    );

    return (
      <div className="flex h-dvh w-screen overflow-hidden bg-elevation-default">
        {studio}
        <div className="relative flex min-w-0 flex-1">
          <PageDocument
            presentation={shown}
            controller={pageController}
            keyScope="document"
            leading={leading}
            actions={pageActions}
            menuItems={menuItems}
            className="min-w-0 flex-1"
            editor={stageEditor}
            {...collaboration}
          />
          {previewBanner}
        </div>

        {sidePanel}

        <PresentationShareDialog presentation={data} open={sharing} onOpenChange={setSharing} />
        {actions.ConfirmEl}
        {EditorConfirmEl}
      </div>
    );
  }

  const stageActions = (stage: DeckStageHandle) => (
    <>
      {data.can_manage && (
        <Tip content={t_i18n("Edit slides")} container={stage.portalContainer}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => leaveFullscreenThen(stage, () => togglePanel("editor"))}
            aria-label={t_i18n("Edit slides")}
            aria-pressed={editing}
            className={cn(STUDIO_BAR_BUTTON, editing && "bg-accent/15 text-accent")}
          >
            <Pencil className="h-4 w-4" />
          </Button>
        </Tip>
      )}
      {layoutCheckButton(() => leaveFullscreenThen(stage, () => togglePanel("layout")), stage.portalContainer)}
      {data.can_manage && (
        <StudioToggle
          open={studioOpen}
          onToggle={() => leaveFullscreenThen(stage, toggleStudio)}
          portalContainer={stage.portalContainer}
          testId="presentation.studio"
        />
      )}
      {data.can_manage && (
        <StudioShareButton
          onClick={() => leaveFullscreenThen(stage, () => setSharing(true))}
          portalContainer={stage.portalContainer}
        />
      )}
      <PresentationExportMenu presentationId={data.id} compact />
    </>
  );

  return (
    // On screen the viewer is exactly the visible viewport (`dvh`, so a mobile
    // browser's collapsing toolbar never leaves a strip below the deck) and
    // never scrolls. The studio rail and the side panels take their width from
    // the deck, which observes its own box and re-lays out.
    <div className="flex h-dvh w-screen overflow-hidden bg-elevation-default">
      {studio}
      <div className="relative flex min-w-0 flex-1">
        <DeckStage
          presentation={shown}
          variant="standalone"
          controller={controller}
          leading={leading}
          actions={stageActions}
          menuItems={menuItems}
          className="min-w-0 flex-1"
          editor={stageEditor}
          {...collaboration}
        />
        {previewBanner}
        {!isPage && <HolderClickerBar presentationId={data.id} total={data.spec.slides.length} />}
      </div>

      {sidePanel}

      <PresentationShareDialog presentation={data} open={sharing} onOpenChange={setSharing} />
      {actions.ConfirmEl}
      {EditorConfirmEl}
    </div>
  );
}
