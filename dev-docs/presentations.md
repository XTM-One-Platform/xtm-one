# Presentations v2 — spec, renderer, brand templates, sharing, export

Developer reference for the agent-generated slide decks. The user-facing
documentation is `docs/docs/usage/presentations.md` and
`docs/docs/usage/resources/templates.md`; this page is the map of how the
feature is built, the invariants that hold it together and the checklists for
extending it. Predecessors: template containers and the v1 decks.

## Module map

| Concern | Backend | Frontend |
| --- | --- | --- |
| Spec contract | `app/services/presentation_spec.py` (models, `validate_spec`, `layout_catalog`, `block_catalog`, `spec_reference`) | `types/index.ts` (`PresentationSpec`, `PresentationSlide`, block unions), `components/presentation/layout-options.ts` (`SLIDE_LAYOUTS`, `supportedLayouts`, `layoutLabel`) |
| Retired layouts | `app/services/presentation_legacy.py` (`RETIRED_LAYOUTS`, `upgrade_spec`, `upgrade_retired_slide`, `upgrade_report`, `retired_layout_note`, `is_retired_layout`); `SpecJSON` in `app/models/presentation.py` (the read-time upgrade on the `spec` columns); `validate_spec` (the write-time upgrade) | — (no v1 renderer: `layouts/legacy.tsx` is gone and `tests/test_presentation_spec_v2.py` fails the build if it comes back) |
| Inline markup | `app/services/presentation_markup.py` (`parse_inline_markup`, `check_markup`, `MARKUP_SUMMARY`) | `components/presentation/markup.ts` (tokenizer), `inline-markup.tsx` (React renderer) |
| Slide links inside a deck | `presentation_markup.py` (the `link` token, `SLIDE_LINK_REF_PATTERN`, `slide_link_targets`); `builtin_tools/presentation.py` (`slide_links_line`, on every save and in the `get_presentation` head); `presentation_export.py` (`SLIDE_LINK_BOXES_JS`, `RenderedDeck.slide_links`); `pptx_writer.add_slide_link_hotspots` | `markup.ts` (the `link` segment), `inline-markup.tsx` (`SlideLink`), `slide-link-context.ts` (`SlideLinkContext`, `slideIndexer`, `slideAnchorId`), the providers in `PresentationDeck.tsx` / `PageDocument.tsx` / `pages/PresentationStaticRender.tsx`, `StaticSlide.tsx` (the export page's anchors), `inline-runs.ts` (`applyLink`, `rangeLinkTarget`, `linkAround`) + `inline-runs-dom.ts`, `SlideEditLayer.tsx` (`SlideLinkPicker`) |
| Icon allow-list | `app/services/presentation_icons.py` (GENERATED: `backend/scripts/refresh_lucide_icons.py`) | `components/presentation/deck-icons.ts` + `DeckIcon.tsx` (one lazy catalog chunk) |
| Renderer | — | `components/presentation/PresentationDeck.tsx`, `layouts/`, `blocks/`, `chrome.tsx`, `geometry.ts`, `theme.ts`, `presentation.css` |
| Persistence + sharing | `app/models/presentation.py`, `app/services/presentations.py`, `app/services/presentation_sharing.py` (`apply_sharing`, the one sharing write), `app/api/v1/presentations.py` | `hooks/api/usePresentationApi.ts`, `pages/Presentations.tsx`, `pages/PresentationViewer.tsx`, `PresentationShareDialog.tsx`, `PresentationEditorPanel.tsx` |
| Public link | `app/services/presentation_public_links.py` (token, `enable_public_link` / `disable_public_link`, `resolve_public_presentation`, `public_presentation_payload`, `public_asset_ids`, the tool wording), `PresentationPublicLink` in `app/models/presentation.py`, `GET` / `POST` / `DELETE /presentations/{id}/public-link` in `app/api/v1/presentations.py`, `app/api/v1/presentation_public.py` (the unauthenticated `GET /public/presentations/{token}`, its `/assets/{file_id}` and, when the link offers it, its `/pdf` - `public_pdf` with its cache and budgets, `render_public_deck_pdf` in `presentation_export.py`), `PATCH /presentations/{id}/public-link` (`allow_pdf_download`), the `presentation_public_links_enabled` security option + `access.presentation_public_links` control | `pages/PresentationPublic.tsx` (`/public/presentations/:token`, the `Download PDF` button), `pages/PresentationStaticRender.tsx` at `/public/presentations/:token/static` (what the PDF is printed from), `useUpdatePresentationPublicLink` / `useDownloadPublicPresentationPdf`, `lib/public-deck.ts` (`publicDeckAsPresentation`, `publicAssetUrl`, `isPublicDeckPath` - read by the 401 handler of `lib/api.ts`), `PublicLinkSection` in `PresentationShareDialog.tsx`, `usePresentationPublicLink` / `useEnablePresentationPublicLink` / `useDisablePresentationPublicLink` / `usePublicPresentation`, `DeckStage`'s `presenterTools`, the globe on the library card |
| Agent tools | `app/services/builtin_tools/presentation.py` (`generate_presentation`, `edit_presentation`, `get_presentation`, `search_presentation_text`, `duplicate_presentation`, `delete_presentation`, `capture_presentation_slides`, `share_presentation`, `set_presentation_public_link`, `export_presentation`) | `PresentationCard.tsx` (the `[[PRESENTATION:<id>]]` marker surface) |
| Looking at a deck | `app/services/builtin_tools/presentation.py` (`_capture_presentation_slides`, `MAX_SLIDE_CAPTURES`); `app/services/builtin_tools/document_vision.py` (`render_source_pages`, `inspect_pptx_identity`, `compare_palette`); `app/agents/seed_data/agent_persona/deck-importer.md`, `skill_content/deck-import-review.md` | — (the captures are conversation files, rendered by the chat's own file surface) |
| Surgical edits | `app/services/presentation_patch.py` (`OPERATIONS`, `apply_operations`, `reconcile`, `verify_untouched`, `summarize_patch`, `parse_slide_selection`, `canonical`, `coerce_revision`, `LAYOUT_DECK_FIELDS`); `builtin_tools/presentation.py` (`_edit_presentation` with `expected_revision`, `_render_edit_account`, `_slide_payloads`, the `slides` selector of `get_presentation`); `presentations.load_writable_presentation(for_update=True)` (the row lock on the agent write paths) | — (the deck queries are invalidated on the `presentation_saved` progress frame both write paths emit right after the commit — see "Live refresh" below — with the `tool_done` frame as the fallback) |
| Live refresh | `app/tools/tool_events.py` (`emit_tool_event`, `bind_tool_event_sink`, `TOOL_PROGRESS_STATUS`, `PRESENTATION_SAVED_EVENT`); `agent_loop.py` binds the per-call sink (`_progress_sink`) around every tool call; `builtin_tools/presentation.py` emits after `own_db.commit()` in both `generate_presentation` and `_edit_presentation` | `hooks/api/useChatApi.ts` (`handleToolProgress` on the `tool_progress` frame, top-level and under `consult_progress.inner`) |
| Slide links | `builtin_tools/presentation.py` (`slide_link`, `focus_marker`, `focus_slide_id` — the `link:` per written slide in the edit account, the focus in the result marker and in the `presentation_saved` payload, the address shape in the `get_presentation` head); `channels/base.py` (`_PRESENTATION_MARKER_RE` group 2 reads the `#/<slide id>` fragment, the channel link keeps it; `_relative_viewer_link_re` absolutizes the agent's relative slide links for in-scope decks in the same pass); `presentation_spec.SLIDE_ID_PATTERN` (the fragment grammar IS the slide id grammar) | `components/presentation/slide-links.ts` (`parsePresentationLink`, `parseSlideFragment`, `resolveSlideRef`), `presentation-card-context.ts` (`navigateToSlide`, `registerSlideStage`), `components/chat/ChatMarkdown.tsx` (`MarkdownLink` — the in-place click), `components/chat-panel/markers.tsx` (the registry of mounted cards, `focusSlideRef` on the card), `pages/chat/answerMarkers.ts` (`markerRefs`, the panel's marker grammar), `PresentationCard.tsx` (opens on the marker's slide; the receipt's `Go to slide N`), `PresentationStudioRail.tsx` (`onSelectSlide`, the follow on `presentation_saved`), `hooks/api/useChatApi.ts` (`handleToolProgress`, which hands the frame to `onToolProgress`), `pages/chat-panel/useTurnFrames.ts` (`onToolProgress`), `pages/PresentationViewer.tsx` (`#/<slide id>` in the hash) |
| Tool guidance | `app/services/presentation_guidance.py` (built tool description), `app/agents/seed_data/skill_content/presentation-design.md` (global skill), `app/agents/seed_data/agent_persona/template-designer.md` | — |
| Brand templates | `app/services/template_container_seed.py` (metadata), `app/services/template_asset_seed.py` (binaries), `app/agents/seed_data/brand_assets/<brand>/manifest.json`, `app/services/svg_sanitizer.py`, `presentation_chrome.py` (`WATERMARK_KEY`, the body-slide watermark; `EMBLEM_KEY`, the corner emblem) | `TemplatePreviewDialog.tsx`, `previewSpec.ts`, `pages/Templates.tsx`, `pages/TemplateDetail.tsx`, `layouts/index.tsx` (`SlideWatermark`), `chrome.tsx` (the corner emblem), `chrome-pattern.ts` (`chromeWatermark`, `chromeEmblem`, `declaresPattern`) |
| Generate with AI | `app/services/template_generation.py`, `POST /ai/generate-template` in `app/api/v1/ai.py` | `TemplateGeneratorDialog.tsx` |
| Export | `app/services/presentation_export.py` (render token, static render, Playwright, `EXTRACT_PRIMITIVES_JS` incl. the `chart` primitive), `app/services/pptx_writer.py` (`_add_chart` — native PowerPoint charts), `app/services/chart_export.py` (`NATIVE_CHART_KINDS` / `PICTURE_CHART_KINDS` / `TWO_AXIS_PICTURE_SERIES_KINDS`, `axis_reading`, Excel number formats, `picture_note`), `ExportedFile.notes` -> the `X-Export-Notes` header (`api/v1/presentations.py`) and the tool result's `Export notes:` | `pages/PresentationStaticRender.tsx`, `asset-context.tsx`, `PresentationExportMenu.tsx`, `export-notes.ts` (the header reader behind the `Exported with notes` toast), `blocks/chart-format.ts` (`chartExportPayload` -> `data-deck-chart`), `blocks/ChartCanvas.tsx` (`.deck-chart-facts`) |
| Ready signal | — (the exporter waits for `data-deck-ready` under `READY_TIMEOUT_MS`, and raises `data-deck-error`) | `components/presentation/ready.ts` (`deckSettled` with its optional `bound`, `FIGURE_WAIT_MS`, `PRINT_SETTLE_MS`), `pages/PresentationStaticRender.tsx` (an unfinished figure -> `deckError`), `visible-time.ts` (`createVisibleClock`, `settlesWithin`, `FRAME_STEP_MS`), `figure-wait.ts` (`useWaitBound` / `useGiveUp`, never on the export page), `FigureBoundary.tsx` (a lazy drawing's own error boundary), `useDeckIconCatalog.ts`, `blocks/diagram-states.tsx`; unit tests `visible-time.test.ts`, spec `tests_e2e/presentation-print-settle.spec.ts` |
| Default template | `PlatformSettings.default_presentation_template_id`, `presentations.resolve_default_template_id`, `PUT /settings/platform` | `pages/settings/ParametersSection.tsx` |
| Collaboration | `app/services/presentation_versions.py` (snapshot on save / list / restore / retention), `app/services/presentation_comments.py` (slide-anchored review notes, the agent-facing formatters, `resolve_slide_anchor`), the `list_` / `add_` / `resolve_presentation_comment` tools in `builtin_tools/presentation.py`, `app/services/presentation_comment_drafts.py` (the composer's AI draft behind `POST /presentations/{id}/comments/draft`), `app/services/presentation_presence.py` (Redis heartbeat rooms) | `PresentationSidePanel.tsx` (the one resizable right-hand slot, up to a third of the screen), `PresentationHistoryPanel.tsx`, `PresentationCommentsPanel.tsx` (+ `PromptGenerator` with a `generate` callback -> `useDraftPresentationComment`), `PresenceAvatars.tsx`, `DeckToolbar.tsx` / `PageToolbar.tsx` entry points |
| Presenting | — | `components/presentation/fullscreen.ts` (`useElementFullscreen`, `presentedElement`, the viewport fallback), `DeckStage.tsx` |
| Speaker notes | `app/services/presentation_notes.py` (`count_words`, `estimate_speaking_seconds`, `format_speaking_time`, `notes_plain_text`, `notes_coverage`, `format_notes_coverage`, `WORDS_PER_MINUTE`, `NOTES_OPTIONAL_LAYOUTS`); `builtin_tools/presentation.py` (`notes_line`, the `(no notes)` outline flags); `pptx_writer._set_notes` | `pages/PresentationSpeaker.tsx` (the window), `pages/PresentationNotes.tsx` (the handout), `components/presentation/speaker-sync.ts` (the `BroadcastChannel` bridge), `speaking-time.ts`, `notes-text.ts`, `speaker-url.ts`, `SlidePreview.tsx`, `SpeakerNotes.tsx`, `useSpeakerTimer.ts`, `speaker.css`; `deck-keys.ts` (`S`), `DeckToolbar.tsx` (`More` entries), `PresentationEditorPanel.tsx` (words / time per slide) |
| Portable packages | `app/services/presentation_package.py` (`build_presentation_package`, `parse_presentation_package`, `import_presentation_package`), `GET /presentations/{id}/package`, `POST /presentations/import` | `PresentationExportMenu.tsx` (the `JSON package` entry), `pages/Presentations.tsx` (`Import`), `useExportPresentationPackage` / `useImportPresentation` |
| Studio (chat bound to a deck) | `app/services/presentation_studio.py` (`StudioContext`, `load_studio_context`, `ensure_studio_tools`, `format_studio_prompt`, `parse_focused_index`), `Conversation.presentation_id`, `presentations.agent_deck_permission`, `PUT /chat/conversations/{id}/presentation`, `?presentation_id=` on `GET /chat/conversations`, `studio_slide` on a chat turn | `PresentationStudioRail.tsx` (the rail + the collapsed `PresentationStudioHandle`; its width comes from `hooks/useResizableWidth.ts`, the shared drag / keyboard / remembered-width hook), `studio-url.ts`, `presentation-card-context.ts`, `useStudioConversations` / `useBindConversationPresentation`, `StudioToggle` (`components/studio/StudioChrome.tsx`, a `ToolbarVerb` from `DeckToolbar.tsx`) for the viewer's `Studio` toggle, with the rest of the bar the Video Studio editor shares |
| Charts + dashboards | `presentation_spec.py` (`ChartBlock`, `ChartSeries`, `CHART_KINDS`, `MetricItem.sparkline`, `DashboardSlide`, `DashboardPanel`, `_PANEL_BLOCK_MODELS`, `_grid_rows`, the `MAX_CHART_*` / `DASHBOARD_GRID_*` caps), `presentations.CHART_THEME_CONFIG_KEYS` (`chart_1` .. `chart_6` in `THEME_CONFIG_KEYS`), `presentation_legacy.is_retired_slide` (the v1 `dashboard` shape), `presentation_layout_switch._dashboard_figures` (a `dashboard` as a conversion target), the chart palettes in `template_container_seed.py` | `blocks/chart.tsx` (`ChartBlockView`: figure, legend toggles, the target named in the legend; `useMeasuredBox` from `measured-box.ts` - the room a figure lays itself out for, shared with the architecture stage - `useChartMotion`, `useSlideEntry` - the drawing keyed on the slide's entry count, `data-deck-pending` per drawing, `data-deck-animate` while a build runs), `blocks/ChartCanvas.tsx` (the Recharts drawing: zero-safe stacked totals, the value labels drawn by `ValueText` with their role / series / category, the target line drawn by hand with `data-axis`), `blocks/chart-layout.ts` (`axisChartLayout`, pure: explicit axis widths from `valueAxisWidth()` / `CATEGORY_AXIS_MAX_SHARE`, width-aware `categoryAxisInterval`, Recharts' bar slots (`barSlot`), how every value is written - upright, smaller, on its side, inside its segment - and the count of those that cannot be), `blocks/chart-locale.ts` (`chartLocale` / `useChartLocale`: the active i18n locale, the page's language outside a provider), `blocks/chart-settle.ts` (`createSettleClock`: when `data-deck-pending` drops - `BUILD_MS` since the first start, the animations counted per series, the `STALL_MS` / `MAX_BUILD_MS` bound in visible time; `growsIn`: a drawing Recharts draws nothing in is a still), `blocks/chart-format.ts` (`formatChartValue`, `axisSurface`, `legendShown`, `hasTargetLine`, `targetLegendName`, `legendIdentities` / `resolveHidden` / `toggleHidden`), `blocks/metrics.tsx` (`Sparkline`), `layouts/dashboard.tsx` (`DashboardLayout`), `theme.ts` (`--deck-chart-N`, `CHART_SERIES_VARIABLES`), the `.deck-chart-*` / `.deck-panel` rules and the `deck-chart-*-in` enter animations in `presentation.css`; specs `tests_unit/chart-format.test.ts` (the pure reading rules) and `tests_e2e/presentation-charts.spec.ts` (every kind live, in both themes), unit tests `blocks/chart-settle.test.ts` (the settle clock), `blocks/chart-locale.test.ts` (the chart locale) and `blocks/chart-fill.test.tsx` (a `fill` chart in a column handed the room, never 0; the export page never waits on a roomless chart) and `blocks/chart-surface.test.tsx` (the card a chart stands on, steady through a build) |
| Org charts | `presentation_spec.py` (`OrgChartBlock`, `OrgChartNode`, `OrgChartLegendItem`, `MAX_ORG_CHART_*`, `ORG_CHART_READABLE_NODES`, `ORG_CHART_HINT`, `architecture_reads_as_org_chart`, `org_chart_hints`, `org_chart_hint_note`), `presentation_guidance.py` (`ORG_CHART_LEADERSHIP_EXAMPLE`, `ORG_CHART_HEADCOUNT_EXAMPLE`, `format_org_chart_guidance`), `presentation_layout.py` (`_org_chart_findings`, `_org_hint_findings`, the `org_*` kinds), `presentation_export.py` (`emitOrgChart` in `EXTRACT_PRIMITIVES_JS`, the `org_chart` observation of `LAYOUT_AUDIT_JS`), `pptx_writer.py` (`_add_oval`, `_add_connector`, `_subgroup_runs`) | `blocks/org-chart-layout.ts` (`layoutOrgChart`, the one engine; `buildTree`, `ownCount`, `rollupText`, `countMismatches`, `bubbleDiameter`, `fitLine`, `orgNeighbours`), `blocks/OrgChart.tsx` (`OrgChartBlockView`), the `.deck-org-*` rules in `presentation.css`, `figure-build.ts` (`FIGURE_PARTS.org_chart`, `levelGroups`, `countAt`), `PresentationLayoutPanel.tsx` (the `org_*` kinds); tests `blocks/org-chart-layout.test.tsx`, `tests/test_presentation_org_chart.py` |
| In-place editing | `app/services/presentation_versions.py` (`describe_slide_changes`, `summarize_manual_edit(previous, current)` - the history line names the slides a manual save touched); `tests/test_presentation_editor.py` (the schema mirror, the default validation, the annotation and key-handling tripwires) | `components/presentation/presentation-editor.ts` (`useDeckEditor`: ONE draft + undo history + selection for the panel and the stage, `canonicalJson`, `blockAt`, `blockPathsOf`, `canRemoveBlock`, `focusRequest`), `edit-paths.ts` (spec paths: `getAt` / `setAt` / `insertAt` / `moveAt` / `removeAt` / `blockPathOf`), `edit-context.ts` (`EditModeContext`, `SlideIndexContext`, `BlockPathContext`, `useBlockEditAttrs`, `useEditTextPath`), `inline-markup.tsx` (`InlineMarkup` swaps in the editor for the text under edit, publishes `data-edit-text`), `InlineTextEditor.tsx` (the uncontrolled contentEditable), `text-history.ts` (the text session's own undo / redo: run + caret snapshots coalesced by input kind), `inline-runs.ts` + `inline-runs-dom.ts` (the runs model between the grammar and the DOM), `inline-editor-registry.ts` (the handle the text bar drives), `SlideEditLayer.tsx` (pointer + keyboard on the stage, outlines, the block bar and the text bar; Ctrl+Z / Ctrl+Y routed to the session from the panel's fields too), `block-schema.ts` / `slide-schema.ts` (the field catalogs, pinned to the Pydantic models), `block-icons.ts`, `SchemaFieldControls.tsx`, `SlideInspector.tsx`, `SlideThumbnail.tsx`, `PresentationEditorPanel.tsx` (`Slides` / `Selection` / `Deck` tabs, the explicit Save), `markup-problems.ts`; the `data-edit-*` rules at the end of `presentation.css`; `tests_e2e/presentation-editor.spec.ts` |
| Per-slide themes + heroes | `presentation_spec.py` (`SlideTheme`, `Hero`, `HeroTone`, `Overlay`, `_SlideBase.theme` / `overlay`, `TitleSlide.hero` / `hero_tone` / `hero_accent_word`, `ClosingSlide.hero` / `hero_tone`, `resolved_slide_scheme`, `slide_theme_override`, `slide_theme_overrides`), `presentations.py` (`unsupported_slide_themes`, `introduced_slide_themes`, `fit_slide_themes_to_schemes`, `unsupported_slide_theme_message`, `slide_theme_snap_note`, `deck_theme_change_note`, `DECK_SLIDE_DEFAULT_KEYS` in `template_deck_defaults` / `apply_deck_defaults`, `mark_file_id` in `THEME_FILE_ID_KEYS`), `presentation_export.py` (`EXPORT_ISOLATE_ATTR`, `_ISOLATE_BACKDROP_JS`, `_capture_backdrop`, `backdrop: true` in the extractor), the cover defaults in `template_container_seed.py`, the `deck_defaults` clause of `template_authoring.py`, the `theme_config` remap in `api/v1/template_containers.py` | `theme.ts` (`slideScheme`, `resolveSlideScheme`, `slideRootAttributes`, `SlideSchemeContext`, `mark_file_id` in `THEME_VARIABLES`), `hero.ts` (the catalogs, `slideHero` / `slideHeroTone` / `slideOverlay` / `heroHostsBackgroundImage`), `hero-label.ts`, `HeroBackdrop.tsx`, `layouts/opening.tsx` (`data-hero`), `layouts/index.tsx` (`SlideOverlay`), `inline-markup.tsx` (`accentLastLine`) over `accent-line.ts` (`splitAccentLine`), `template-deck-defaults.ts` (`withDeckDefault`, the Design editor's `deck_defaults` writer that carries the per-cover objects through), `SlideStylePickers.tsx`, `SlideInspector.tsx` (the look pickers), `presentation-editor.ts` (`normalizeSlideLook`), `previewSpec.ts` (`applyCoverDefaults`), `field-applicability.ts`, the `[data-scheme]` token blocks, the hero / overlay / isolation rules in `presentation.css`; `theme.test.ts`, `hero.test.ts`, `tests_e2e/presentation-slide-theme.spec.ts` |
| Layout: fit + audit | `app/services/presentation_layout.py` (`check_presentation_layout(expected_revision=)`, `findings_from_observations`, `format_layout_report`, `narrow_layout_report` (the edit-scoped report), `LayoutReport` / `LayoutFinding`, the per-revision Redis cache, `FIT_FLOOR`), `LAYOUT_AUDIT_JS` + `RENDERED_REVISION_JS` in `presentation_export.py` (`render_deck(want_layout=True)`, `RenderedDeck.revision`), `layout_check_note` in `builtin_tools/presentation.py`, `POST /presentations/{id}/layout-check`, `PRESENTATION_LAYOUT_CHECK_ENABLED` | `components/presentation/fit.ts` (`useFitToHeight`, `fitContent`), the `.deck-fit` / `.deck-fit-content` pair in `layouts/frame.tsx` + `presentation.css` (a fitted host clips), `components/presentation/spread.ts` + `spread-math.ts` (`useSpreadCap`, `capSpread`, `spreadCap`, `SPREAD_ROW_MAX_EXTRA_PX` / `SPREAD_BLOCK_MAX_EXTRA_PX` - the bounded `fill` / `distribute` spread, wired on the five row-spreading grid roots and on `BlockList`; `SPREAD_CARD_MAX_EXTRA_PX` + `measureCardRows` on `NumberedCards`, the takeaways body), `components/presentation/flow-fit.ts` (`fitFlowTitles` / `useFlowTitleFit`: a horizontal flow's titles one size down when their longest word asks and that fits, else the step widens, never a word broken), `blocks/shared.tsx` (`IconText`: the one icon + text row, the icon on the label's first line), `PresentationLayoutPanel.tsx` (`currentSlideIndex`), `useLayoutCheckRun.ts` (the run, held by the viewer) over `useCheckPresentationLayout`, `data-slide-id` on `StaticSlide.tsx`, `data-deck-revision` on `PresentationStaticRender.tsx` |

Migrations: `f7p1res2637_add_presentations`, `h2p3358share_add_presentation_sharing_columns`,
`f1p3358dflt_add_platform_settings_default_presentation_template`,
`j4p3358collab_add_presentation_collaboration` (versions, comments, `updated_by_user_id`),
`k5p3358studio_add_conversation_presentation_id` (the studio binding, on `conversations`),
`p5396link01_add_presentation_public_links` (the public link),
`p5430pdf01_add_presentation_public_link_pdf_download` (its PDF download).

## The spec (v2)

A deck is one JSON document, validated by `validate_spec()` into the stored
`Presentation.spec`. Models are `extra = "forbid"`: an invented field is a
precise, field-addressed error (`slides.3 (layout=content): ...`) rather than
silently dropped content, so an agent patches one slide instead of regenerating
the deck.

**A blank optional string is absent**. v1 typed its optional strings as
`str | None` with only a ceiling, so every stored v1 chapter divider is a title
slide carrying `"subtitle": ""`; v2 requires a present string to be non-empty.
Every route that edits a deck re-validates the WHOLE stored spec — a theme
change, a duplicate, a restore, a package import — so those blanks failed decks
on fields nobody had touched. `_Strict` (the base of every spec model) now runs
a `before` validator that reads `""` / whitespace-only values of *optional*
fields as `None`: they validate, and `exclude_none` drops them from the
normalised spec. Required strings are untouched and still refuse a blank; an
after-validator that needs "at least one of" (a callout's title / content /
children) sees the normalised `None`, so an all-blank callout is still refused
as empty.

**Two readers of one error.** `validate_spec` raises `PresentationSpecError`
whose `str()` is the tool error the agent repairs from: 0-based paths as the
agent built them, `(layout=..., block=...)` notes, "call the tool again", the
layout / block catalog. The same failures are kept structured on `.problems`
(`SpecProblem`: `path`, `layout`, `blocks`, `message`) and rendered for a person
by `human_message()` / `human_details()` — slides numbered from 1 as the editor
numbers them, the block as an `in list` note, no instruction meant for a model,
no catalog dump. Lifting the union tags Pydantic inserts into an error path
(`slides.2.content.blocks.0.callout.title`) is structural (`_is_union_tag`):
only `slides` / `blocks` / `children` lists, the `columns` grid and the
`footer_block` field hold unions, so a FIELD that happens to spell a tag
(`items.1.text`, `items.0.title`) stays in the path for both readers.
`verify_file_references` raises the same structured problems for an invented or
unreadable file id.

Top level: `title`, `theme` (`PRESENTATION_THEMES`: `dark` / `light` — a
theme IS a colour scheme; the v1 `filigran` value is a `LEGACY_THEME_ALIASES`
entry that `normalize_theme` reads as `dark` on every validation, so a stored
v1 deck loads without an edit and no writer can create a new one), `transition`,
`aspect_ratio` (`16:9` / `4:3` for slides, `A4` / `letter` for portrait pages —
`PRESENTATION_ASPECT_RATIOS`), `chrome`, `slides`.

**Layouts** (`SLIDE_LAYOUTS` — the ONE catalog): `title`, `section`,
`agenda`, `content`, `dashboard`, `roadmap`, `takeaways`, `closing`. `content`
is the workhorse: either a single flow of `blocks` or 2-4 `columns` of leaf
blocks (never both), with `ratio`, `vertical_align`, an opt-in `distribute`
(below) and an optional `footer_block` pinned at the bottom. `dashboard` is a KPI tile row over
a grid of chart / table / metric panels — see "Charts and dashboards" below.
The v1 layouts (`two_columns`, `comparison`, the v1 `dashboard`, `generic`) are
RETIRED: they have no Pydantic model, no renderer, no place in any list an
agent or a person reads. A stored deck that still carries one is upgraded to v2
slides the moment it is read — see "Retired layouts" below.

**Agenda grid** (`AgendaSlide`, mirrored by
`components/presentation/agenda-layout.ts`). The agenda is a column of
EXPLICIT rows, not a stack of two lists and not one flat grid: `columns` is
`1 | 2 | 3` (omitted = `auto_agenda_columns`: 1 up to 4 items, 2 for 5-6, 3
for 7-9, 2 for 10 - two columns of five), the renderer groups the items into
rows (`agendaRowGroups`) and draws one `li.deck-agenda-row` per row - a grid
of `columns` `minmax(0, 1fr)` tracks and TWO shared row tracks, head and
description, that every cell (`div.deck-agenda-item`, `grid-template-rows:
subgrid; grid-row: span 2`, children on the tracks' top edges) joins. That is
what puts the titles of a row on one line AND the descriptions under them on
another whatever a neighbour wraps to, and makes every cell of a row the same
height (a `current` tint is one even card). `distribute` (default `true`,
`justify-content: space-between` on the column of rows) spreads the ROWS over
the whole body height instead of packing them under the title with a hole
below - the `.deck-fit-content > *` stretch rule is what gives the list that
height - and, because the free height lands between rows and never between a
title and its own description, a single grid distributing its tracks could not
have promised the same. The spread is bounded like the content slide's
(`spread.ts`): `useSpreadCap` caps the list at the rows' natural height
plus `SPREAD_AGENDA_ROW_MAX_EXTRA_PX` (136px) per gap, so a row gap reaches
160px at most; a list capped before the floor (two or three rows on a 16:9
body) is centred as a group by `margin-block: auto`, and four rows and more
normally stay under the cap and fill the body. `distribute: false` still packs
the rows on top. The list chip (icon or number) is centred on the cell's text
block, never on the title line: on the title plus its description, or on the
title alone for an item without one (`data-description="none"`, the chip held
inside the shared title track by negative block margins so the row's titles
stay level); the grid's `N.` is centred on its title. Pinned on a live deck by
`tests_e2e/presentation-agenda-centre.spec.ts` (2px). Three columns is a two-to-three-rows rule:
`_grid_holds_full_rows` refuses `columns: 3` for fewer than
`AGENDA_GRID_MIN_ITEMS` (6) or more than `AGENDA_GRID_MAX_ITEMS` (9) items
with a field-addressed error (`slides.2 (layout=agenda): columns=3 needs 6 to
9 items ...`): below six the grid has no second row it fills (`1 2 3 / 4 5`
is a chip list with a hole), above nine it needs a fourth row no board slide
holds. Six and nine fill their rows; seven and eight leave the last row short
(seven does in every column count, eight only fills up as four rows of two,
one row more than the grid wants), so the fewest rows wins there. The
orphaned "09" bottom-left of the old
two-column agenda (nine items in `1 2 / 3 4 / ... / 9`) is what the 3 x 3
exists to remove. `flow` (`row`, default,
`1 2 3 / 4 5 6 / 7 8 9`; `column` runs down the first column first, `1 4 7 /
2 5 8 / 3 6 9`, decided when the rows are grouped) is about reading order
only. `style` picks the cell: `grid` (the default at 3 columns,
`auto_agenda_style`) draws `N. | TITLE` - the number in the accent, a 1px
accent separator stretched to the line, the title in the heading font,
uppercase, letter-spaced like a section slide - with the description under it
in the body colour at body size, clamped to two lines; `list` is the numbered
chip the 1- and 2-column agendas always drew (the chip spans both tracks, the
title and the description stack beside it), so an existing deck renders as
before and only gains the shared lines and the distribution. When ANY item
carries a `description`, every cell reserves the description line
(`data-empty` placeholder, `aria-hidden`) so the rows keep one height when one
item has nothing to say; when none does, the second track is empty and its gap
is not drawn. The rows are `li`s demoted to `role="presentation"` and the
cells `role="listitem"`, so the `ol` stays a list for assistive technology
while the DOM carries the row a subgrid needs; with `flow: column` the DOM
rows are the transposed grid (`1 4 7 / 2 5 8 / 3 6 9`), so the `ol`
`aria-owns` its cells in item order and the accessibility tree reads `1, 2,
3...` down the columns the way the eye does - no hidden duplicate list, which
the layout audit (it skips `aria-hidden` subtrees) and the DOM-walking PPTX
export would both have had to be taught about. `current` keeps working in both
styles: the current cell is tinted and bordered, the others go to the muted
colour (the grid never fades a cell - a muted `N.` still reads on a projector).
Everything is drawn from the deck tokens (`--deck-accent`, `--deck-fg`,
`--deck-muted`, `--deck-line`, the `--deck-fs-*` scale, stepped down one size
at 3 columns), pinned by `test_frontend_agenda_layout_mirrors_the_backend_rule`,
which also holds the frontend `autoAgendaColumns` and `agendaRowGroups` to the
backend rule. The editable PPTX needs no agenda code: the number, the
separator, the title and the description are separate DOM boxes, so
`EXTRACT_PRIMITIVES_JS` already emits one text box per part on the same 3 x 3
grid the viewer shows.

**Filling a content slide**. A `content` slide holding one grid block
(a six-item `feature_list`) packed the rows under the title and left the
bottom third of the slide bare, and the audit saw nothing wrong: it measured
excess only. Two opt-in switches now say where the free height goes, both OFF
by default so a deck that never sets them renders byte-identically (the
attribute is only emitted when true; the validated spec merely reads back
`fill: false` / `distribute: false`). `fill: bool` on the grid blocks
(`FILL_BLOCK_TYPES`: `list`, `metrics_grid`, `key_value`, `feature_list`,
`numbered_cards`, `personas`, and `table` - the figures and cards
already had it) is the
BLOCK spreading its ROWS: the root emits `data-fill`, the run's existing
`.deck-blocks > [data-fill="true"] { flex: 1 1 auto }` hook hands the block
the free height, and one `[data-fill="true"] { align-content: space-between }`
rule per grid root spreads the rows over it (`.deck-cards` keeps its
takeaways `stretch` rule, where the rows share the height instead);
`numbered_cards` passes the field to the `NumberedCards` prop the takeaways
slide already used. Inside a column the block stretches within the height the
column HAS - whether the column itself stretches stays the `equal_height` /
`vertical_align: stretch` decision. `distribute: bool` on the `content` slide
(mirroring `AgendaSlide`, opposite default) is the SLIDE spreading its BLOCKS:
`data-distribute` on the body host, `.deck-blocks-slide[data-distribute="true"]
{ justify-content: space-between }` - written AFTER the `vertical_align` rules
at the same specificity, so `distribute` wins over `center` / `end` for the run,
as the schema says; on a columns slide the columns take the body height
(`align-items: stretch`) and each column's run spreads its own blocks. Which
to reach for: `fill` spreads rows INSIDE one block, `distribute` spreads
BETWEEN blocks - a lone grid block wants `fill`, a multi-block body wants
`distribute`; setting both is legal and redundant. The
export needs no code for either: the writer places every box where the render
measured it (`EXTRACT_PRIMITIVES_JS` never reads the fields, pinned). The
`sparse` audit finding below is the other half: it names the switch the slide
can actually use (`fill` on a grid block it holds, `distribute` on a run of two
or more blocks), and once the switch is on and the slide is still thin it says
so instead of naming it again (below). **The layout switch carries the look, not the field**
(`presentation_layout_switch.py`): agenda and content have opposite defaults,
so a packed content run moved to an agenda writes `distribute: false` (the
agenda would spread it silently) and a spread agenda — or a takeaways slide,
whose cards always share the height — moved to content sets `fill: true` on
the ONE grid block the body becomes (`distribute` there would have no second
block to spread from); a lone `fill` block reads back as the slide's spread
(`_lone_block_fills`), so the round trip lands where it started. Pinned by the
`fill` / `distribute` sections of `test_presentation_spec_v2.py` (schema,
reference, tool schema, renderer + CSS + TS tripwires),
`test_presentation_export.py` (PPTX y-positions),
`test_presentation_layout_switch.py` (the four directions and the round trips)
and, on a live deck, `tests_e2e/presentation-fill.spec.ts` /
`presentation-distribute.spec.ts` (rendered geometry, packed vs spread, and
`false` equal to absent).

**The spread is bounded** (`components/presentation/spread.ts` +
`spread-math.ts`). Both switches are `space-between`, which divides the free
height over the gaps whatever the content is: three one-line bullets with
`fill` on a 1080px canvas ended up ~250px apart, a callout and two arrow items
under `distribute` sat at opposite ends of the body, and the soft budgets told
the agent to do exactly that on every lone grid block - the switches made a
THIN slide look stretched, not finished, and the audit saw nothing because the
block reached the floor. The fix is the `fit.ts` shape, measured and
deterministic: `useSpreadCap(ref, enabled, maxExtra, key)` writes an inline
`max-height` on the spreading element equal to its NATURAL height (the rows at
their content size - grouped by shared top edge, `rowHeights()` - plus the
element's own row gap, plus the padding and border a `border-box` element
counts inside its max-height) plus one `maxExtra` per gap and a 2px rounding
slack (`spreadCap()`), and publishes it as `data-spread-cap` (canvas px). The
flex layout does the rest: a room SMALLER than the cap still hands the element
every pixel it has and `space-between` spreads exactly as before (a cap taller
than the room never binds), a room larger than the cap stops it there and the
rest stays BELOW the last row. `SPREAD_ROW_MAX_EXTRA_PX = 48` for the ROWS of
a `fill` grid block (a list's 16px gap grows to 64px at most, ~1.6 lines at
the body size), `SPREAD_BLOCK_MAX_EXTRA_PX = 64` for the BLOCKS of a
`distribute` run (32px grows to 96px at most). Wired on the five row-spreading
roots (`list`, `metrics_grid`, `key_value`, `feature_list`, `personas`, keyed
on `block.fill`) and on `BlockList` (keyed on `distribute` - the slide's run
AND each column's run, which is why `ContentLayout` now passes
`distribute={slide.distribute}` to the column lists). `numbered_cards` is
bounded its own way (below): its cards SHARE the height (the
takeaways `stretch` rule) - the cards grow, not the gaps. Not on the figures,
which take the room by design. A run whose child is such a filler
(`flex-grow > 0` with no cap of its own: a `fill` chart / image / diagram /
architecture / timeline / callout / phase card, a grow spacer, the
`equal_height` only-child) is not capped at all - a cap read off a filler would freeze the
run at whatever size the room happened to give it, and the filler leaves
nothing to spread anyway. A capped child inside a `distribute` run is measured
at ITS cap (read off `data-spread-cap`), never at the height the room gave it.
The hook runs before the first paint, then on every resize of the element,
its parent or one of its children (fonts landing, a hidden reveal slide being
shown - everything measures zero until then and the element is left alone -
an edit from the studio; `key` re-arms the observers when the children are
replaced), writes deferred to an animation frame so a pass that changes
nothing ends the cycle; `fill: false` / `distribute: false` clear the cap.
Nothing here scales: `offsetTop` / `offsetHeight` are layout metrics, which
neither the canvas transform nor the fit ratio touch, and the export reads the
capped boxes like any other. The fit does change the layout the rows are
measured on (it widens the body, and holds a `metrics_grid` at its size), so
its search re-measures the caps on every layout it probes rather than waiting
for this observer ("The spread caps are part of the ruler" below). The audit's `sparse` measure is the other half of
the bound: it reads the ROOM (the `.deck-fit` host), never the run's box - a
capped run's box ends at its cap - and no longer exempts `distribute` (a
`vertical_align` of center / end still does, but not when `distribute` is set
too: the switch wins over the placement in the renderer, so the guard reads
it the same way); and a
slide that already spreads and is still sparse is, by construction, at the cap
(had it reached the floor there would have been nothing to measure), so
`sparse_remedy()` says `Fill already spreads the rows as far as reads well -
the rest wants content: add content to the body, add a footer_block, or merge
with the next slide.` instead of naming the switch again. The agent-facing
words say the bound in one clause each, and no more - the rest is the tool
result above: `FILL_ROWS_DESCRIPTION` ("spread the rows over the available
height, up to a readable gap") on the five capped roots, `FILL_CARDS_DESCRIPTION`
("stretch the cards to share the available height, up to a readable card
height") on `numbered_cards`, the one root whose cards rather than its
gaps take the extra (`fill_description()` picks per block, and the
import-time invariant holds each model to its own wording, so the contract can
never promise a gap the renderer does not keep), the `distribute` field and tool
schema ("each gap up to a readable maximum"), and the soft budget, which no
longer presents `fill` as the answer to a thin slide ("a body that leaves the
bottom third empty wants content or a footer_block" - and says nothing about
how `fill` spreads, so it holds for `numbered_cards` too). Pinned by
`spread.test.ts` (the arithmetic, under Node's runner),
`test_the_spread_is_bounded_by_the_renderer_not_by_the_agent` (the constants,
the wiring, the filler opt-out),
`test_the_sparse_measure_reads_the_room_and_only_a_deliberate_placement_silences_it`,
`test_a_slide_that_already_spreads_is_told_the_spread_is_at_its_limit`,
`test_the_reference_documents_fill_on_every_grid_block_in_the_wording_its_renderer_keeps`,
and on a live deck by the two e2e specs above, which assert the published cap
against the packed geometry and branch on whether it binds - reading the ROOM
(`.deck-fit`, the box the audit measures) apart from the run that carries the
cap, since a binding cap is exactly what makes the two differ. A nested pair
(a `fill` grid inside a `distribute` run) settles in two observer rounds - the
block's cap first, the run reading it next - which `deckSettled()` (`ready.ts`)
waits out before the export page signals ready.

**Takeaway cards are bounded too**. The takeaways body and a
`numbered_cards` block with `fill` give every row of cards `1fr` of the height
they are handed, and the poster rule pushes the title to the card's foot.
Left unbounded, ONE row - two or three takeaways, the example the skill and
the tool guidance give - became three pillars of the body's full height: a
306px card stretched to 764px (625px over a `footer_block`), the number at the
top, the text at the foot, 60% of the card empty; two rows (four to six
takeaways) grew only 60-95px each and read fine. `NumberedCards` now runs
`useSpreadCap(rootRef, fill, SPREAD_CARD_MAX_EXTRA_PX, items, measureCardRows)`:
`measureCardRows` reads the cards with the grid at its content height (its
`flex` and cap lifted for one synchronous layout, as `measureTableRows` does -
a stretched card's box is not its natural one, and the `fill` card's 96px
numeral means the packed slide cannot stand in for it), and the cap adds one
`SPREAD_CARD_MAX_EXTRA_PX = 96` per ROW (`spreadCap(..., grows: "rows")`), not
per gap. One row of three cards stops near 400px; two rows still reach past
the body, so four to six takeaways render as before. Where the freed height
goes depends on who owns the body: on the `takeaways` slide the cards ARE the
body, so `.deck-fit-content > .deck-cards[data-fill="true"] { margin-block:
auto }` shares it above and below the row; on a `content` slide the block
keeps its run's placement like every other capped `fill` block, the rest stays
below it and the `sparse` audit reports it (the stretched cards used to hide
it: the block reached the floor). Pinned by `spread.test.ts` (the per-row
arithmetic), `test_the_spread_is_bounded_by_the_renderer_not_by_the_agent` (the
constant, the wiring, the centring rule), the wording test above, and
`tests_e2e/presentation-fill.spec.ts` (the `numbered_cards` cap read off the
natural cards; a one-row takeaways slide capped and centred; a two-row one
spanning the room).

**Blocks** (`BLOCK_TYPES`): `text`, `heading`, `quote`, `list`, `callout`,
`metric`, `metrics_grid`, `table`, `key_value`, `badges`, `feature_list`,
`phase_card`, `numbered_cards`, `pricing_tiers`, `personas`, `profiles`,
`timeline`, `process_flow`, `comparison`, `section_header`, `banner`,
`challenge_answer`, `image`, `logo_grid`, `logo_wall`, `diagram`,
`architecture`, `org_chart`, `chart`, `columns`, `spacer`, `divider`. Nesting is
capped by construction (`CONTAINER_BLOCK_TYPES`): a `columns` block holds leaf
blocks, a `callout` / `comparison` holds atomic blocks, atomic blocks hold
nothing.

**A row's picture** (`list` and `feature_list` items, issue #10256): an
item's `logo_file_id` (a platform file, an SVG flag or a small logo) is drawn
in the row's `IconText` slot in place of its bullet, style glyph, icon or
tile (`RowLogo`, `blocks/shared.tsx`), so the shared rule centres it on the
row's first line. In a list it is `1cap` tall, in a feature list half the
tile, its aspect ratio kept and its width capped (`.deck-row-logo`,
`presentation.css`); `logo_border` adds a 1 px `--deck-line` hairline so a
white flag reads on a white card. One medium per row (an `icon` with a
`logo_file_id` is refused; `FeatureItem.icon` is optional only when the
picture is set; `logo_border` without a picture is refused). A list with one
picture row carries `data-row-logos`, which gives every row's marker a slot of
the picture's width so mixed rows keep one text edge, and spaces its rows one
`--deck-space-*` step more than a text-only list of the same density (normal
24 px, compact 16 px, tight 8 px); a list without one is unchanged. The
wrapper is a `data-export-picture`, so the editable PPTX lays
the picture (hairline included) where the page drew it; the PDF is the page.
Pins: `tests/test_presentation_row_logo.py`,
`blocks/list-row-logo.test.tsx`.

**Collateral blocks** (the sales-content pass): these exist because the
marketing collateral could not be reproduced without them, and each one is a
controlled shape rather than free layout. `table` has a `tone` per header
column (the accented "us" column of a feature matrix), boolean cells drawn as
check / cross icons and a `footnote` rendered OUTSIDE the clipped, shrinkable
frame (rows clip from the bottom on an over-full page; the fine print never
does). `badges` is a row of pills (certifications, hosting, community numbers).
`key_value` is label / value fact rows in one or two columns. `pricing_tiers` is
2-4 edition cards with exactly one `highlighted` tier and a CTA each. `banner`
is a hero header or a closing call to action with up to `MAX_BANNER_ACTIONS`
`https:` / `mailto:` actions (`gradient` / `solid` / `outline` / `soft`).
`personas` is 2-4 audience cards — the role, `pains` today, `gains` after —
with relabelable `pains_label` / `gains_label` headings, a toned top rule and
`density` (`normal` / `compact` / `tight`).

### A title slide's speakers and icon strip

`TitleSlide.speakers` lists who presents, in speaking order: up to
`MAX_COVER_SPEAKERS` (4) `CoverSpeaker`s - the closing slide's `Speaker`
(name, title, up to three credentials) plus a `photo_file_id` portrait. The
title slide no longer has `speaker` / `speaker_photo_file_id` fields, but the
one-speaker form stays accepted and always reads as the LEAD speaker
(`fold_title_speaker`): on every write the model folds it before validation
(`TitleSlide`'s `mode="before"` validator - the agent's tools, the REST editor,
a package import), on every read the spec column folds a stored deck
(`SpecJSON.process_result_value` -> `presentation_legacy.fold_title_speakers`,
copy-on-write), so no reader - the editor, the agent, the exporter, the
layout switch - meets two shapes. `speaker` written on a slide that lists
speakers replaces `speakers[0]` and keeps its portrait unless the write names
one; a lone `speaker_photo_file_id` lands on `speakers[0]`, or is dropped when
there is no speaker to wear it (it never drew). The closing slide keeps its
one `speaker`. The layout switch carries the list whole to a title slide, the
lead (words only) to a closing slide - the others and the portraits said in
the account - and one text block per speaker to a body layout. The portraits
are pictures like any other (`walk_file_references` sees every `*_file_id`),
named `speaker 2 photo "Ada"` in a tool result. `TitleLayout` draws the list
as `.deck-speakers` (`data-count`; three or four take 88 px portraits), the
in-place editor's fields `speakers.<n>.name` / `.title` / `.credentials.<i>`.

The inspector edits both by hand: the title slide's `items` is `speakers`
(`unsetWhenEmpty`, the model's `None`; the portrait an `asset` field over
`SPEAKER_PHOTO_ROLES`, the template's `people_photo` assets - slide-level
fields and items now carry the deck's `templateId`), and `icon_strip` is an
`icons` descriptor - a list of `IconField`s, each checked against the catalog
as it is typed, reordered, removed, added up to `MAX_ICON_STRIP`, an emptied
strip written `[]` as the model's default factory stores it. The tripwire
reads `icons(...)` like `strings(...)` (a list of str, its max the model's
`max_length`).

### People: the `profiles` block and `image.padding`

On 25 Sep 2026 an agent spent 80+ tool calls changing one logo and one padding
on a founders slide, because the slide was a browser-rendered PNG placed as an
`image`: every fix meant a rebuild, a re-render and a re-read of the picture.
The `profiles` block is the native form of that slide, so each of those is a
one-field `edit_presentation`. `ProfilesBlock` is 1-`MAX_PROFILES` (4)
`Profile`s — `name`, optional `title`, `photo_file_id` (a `people_photo`
template asset; the initials are drawn without one), `tone`, and 1-
`MAX_PROFILE_LINES` (5) `ProfileLine`s of `text` (<= `MAX_PROFILE_LINE_CHARS`,
a bare string is coerced) with an optional `logo_file_id` or `icon` — plus
`columns` (auto = `min(items, 4)`), `density` and `photo_size` (`sm` 96 px,
`md` 112 px, `lg` 128 px). The renderer (`blocks/profiles.tsx`) draws one card
per person on the surface tokens (never flush), the portrait as a covered
circle with a toned ring, then one row per line: a 32 px tile (the logo on a
surface plate, else the icon, else the tone dot) and the fact on ONE line —
`white-space: nowrap` + `text-overflow: ellipsis`, the run shrinking one type
step first (`profiles-fit.ts`, `data-fit="tight"`, measured after layout and
on resize) and marked `data-truncated` when the ellipsis still cuts it. The
layout audit measures every `.deck-profile-line-text` explicitly
(`scrollWidth - clientWidth`, like the chrome bars; the generic clip pass skips
the run so the cut is reported once) and files it as `clipped` on
`profile-line` with the text and a remedy naming `lines[].text`; the card's
scaffolding classes are `STRUCTURAL`, so a finding names `profile-line` /
`profile-name` / `profile-title` / `profile`, never the grid. The editable
PPTX needs nothing new: the DOM extractor already captures every `IMG` as a
picture and every text node as runs, so a profile exports as picture + text
runs by construction. In the inspector (`block-schema.ts`) a person's `name`,
`title` and `tone` are fields, the lines are edited in place on the stage
(each is an `InlineMarkup` run at `items.N.lines.M.text`) and the portrait /
logos need a file (the assistant's); the layout switch reads a person as one
point (name, then title and facts). The seeded Filigran Brand Deck carries the
two founders' official portraits (`team-samuel-hassine.jpg`,
`team-julien-richard.jpg`, role `people_photo`).

`image.padding` (`none` default / `sm` / `md` / `lg`) puts the picture on a
card with the surface's own inner padding (`--deck-space-2` / `-3` / `-4`)
instead of edge to edge — `.deck-figure[data-padding]`; `none` keeps the
historical flush look and writes no attribute.

### The showcase image and the `challenge_answer` block

A keynote deck showed the product through a window chrome drawn by hand
(`render_html_to_image`) and placed as an `image`, and said "the challenge /
our answer" with two callouts and a spacer. Two native compositions replace
the workarounds.

**`image.frame`** (`ImageFrame`: `none` default / `browser` / `device`, the
`IMAGE_FRAMES` tuple mirrored by `IMAGE_FRAMES` in `block-schema.ts` and
`DeckImageFrame` in `types/index.ts`). `browser` draws a title bar
(`--deck-showcase-bar` 36 px: three dots and an address pill) on the surface
token under a hairline, `device` a bezel (`--deck-showcase-bezel`, a mix of
the foreground into the paper, so a dark slab on the dark scheme and silver on
paper) with a 28 px top band and a camera dot; both sit on a glow in the brand
gradient (`.deck-showcase-glow`: a `radial-gradient` of `--deck-gradient-from`
/ `-to` at `--deck-showcase-glow`, 44% dark / 22% light, bleeding a little
past the window and more below it). The window takes the PICTURE's shape,
never a letterboxed box: the `<img>` inside is `width: 100%; height: auto`,
and the `.deck-showcase` box is the one element sized - by the size bucket
(`--deck-image-h * --deck-image-ratio + the chrome's width`) or, with `fill`,
as the largest box of the picture's ratio the room holds
(`min(--deck-room-w, (--deck-room-h - the chrome's height) * ratio + the
chrome's width)`). The ratio is the one `MeasuredImage` already measures,
handed up through `SlideImage`'s `onRatio` (an `<img>` publishes it on
itself and an ancestor cannot read it); the room is `.deck-showcase-room`,
measured by `useShowcaseRoom` (`blocks/showcase.ts`, the `useMeasuredBox`
shape: layout metrics, a `ResizeObserver`) and published as
`--deck-room-w` / `--deck-room-h` in canvas px - the height only from
`SHOWCASE_MIN_ROOM_PX` (120) up, so a room whose height is its own content's
(a figure in a run its container hugs) publishes its width alone and the
stylesheet falls back to the bucket instead of collapsing. The room centres
the window in the height it did not use; `data-align` places it across. The
glow and the bar are decoration (`aria-hidden`, so the audit never reads the
bleed as an overflow) drawn as real elements with backgrounds: the editable
PPTX exports the bar and its dots as shapes and the glow as a picture,
captured alone through `data-export-backdrop` so the window is never
baked into it; a `box-shadow` or a pseudo-element would have
been dropped. A `fill` image is now a filler like a `fill` chart:
`.deck-columns > .deck-column:has(> .deck-blocks > .deck-figure[data-fill="true"])`
stretches its column to the row and `.deck-blocks > .deck-columns:has(...)`
hands a columns block the free height - in the slide's own columns
(`.deck-columns-slide`, stretched to the body by `.deck-fit-content > *`)
the column rule alone is what lets the showcase of a `wide_left` slide take
the body height; an unframed `fill` image gains the same room (its picture
was letterboxed in a column that hugged it before). An unframed image writes
no attribute and keeps its earlier DOM.

**`challenge_answer`** (`ChallengeAnswerBlock`, atomic): `challenge` and
`answer` (`LongMarkup`), an optional `icon` (leads the challenge card), the
eyebrows `challenge_label` / `answer_label` (`Label`, "Challenge" / "Answer"
by default, mirrored by `CHALLENGE_DEFAULT_LABELS` in `blocks/challenge.tsx`)
and `fill` ("stretch the two cards to share the available height" - a card
stretch like a callout's, NOT a `FILL_BLOCK_TYPES` row spread). The renderer
is ONE single-column grid (`.deck-challenge`): the grid is what gives the two
cards one width and one left edge whatever their text, which two callouts in
a run never had; the arrow between them (`.deck-challenge-arrow`) is a grid
row of its own, decoration: ONE SVG as tall as the gap it sets (66 canvas
px), a single stroke from the challenge card's bottom edge (a butt end) to
the answer card's top edge ending in a chevron, both drawn on the 24-unit
grid of Lucide's `arrow-down` at 32 px so the stroke (`DECK_ICON_STROKE_WIDTH`)
and the head are that icon's, in the success tone (`ChallengeArrow`:
a 2 px line over the icon had read thinner at the top than at the bottom).
Touching both cards, it carries `data-export-alone`: the PPTX captures it
alone on a transparent page ("Pictures captured alone", under Export). The
tones are fixed - `error` on the challenge card, `success` on the answer
card, the two meanings every deck already maps them to - and the
cards are cards of the catalog (the card light's `:is()` list), with a 6 px
left bar in their tone and an uppercase eyebrow in it. `fill` is
`grid-template-rows: minmax(0, 1fr) auto minmax(0, 1fr)` under the generic
`.deck-blocks > [data-fill="true"]` hook; `spread.ts` reads it as a filler
(`flex-grow` with no cap), like a callout. The layout switch reads the two
cards as two points (the eyebrow, the sentence as the description, the
block's icon on the first and a check on the second). The inspector offers
every field; the palette inserts a placeholder pair.

Taught once: the `SHOWCASE:` paragraph of the tool description (with
`SHOWCASE_IMAGE_EXAMPLE`), the `challenge_answer` entry of `BLOCK_EXAMPLES`,
and compositions 3.21 (the framed screenshot in the wide column) and 3.22
(the challenge and its answer, alone with `fill` and a footer callout) of the
design skill. Pinned by `tests/test_presentation_showcase.py` (schema,
defaults, reference, guidance, the layout switch, the frontend halves),
`blocks/showcase.test.tsx` (the cascade and the published variables through
the real stylesheet, the connector's one stroke and head) and
`tests_e2e/presentation-showcase.spec.ts` (the geometry on a live deck at
1920 x 1080, both frames, the bare `fill` image, the bucket, both block
placements, the connector filling the gap, and the layout check).

### Formula and configuration cards

Two leaf blocks draw a packaging or licensing slide natively (issue #10193), where a static picture was the only way before.

- `formula` (`FormulaBlock`, `blocks/formula.tsx`): 2-5 `terms` (`MIN_FORMULA_TERMS` / `MAX_FORMULA_TERMS`), each ONE of `icon`, `logo_file_id` or `logos` (up to `MAX_FORMULA_TERM_LOGOS`; each a `FormulaLogo` `{file_id, logo_light_file_id}`, a bare file id coerced, so every module wordmark of the row has its light-slide variant, picked by `rowLogoForScheme`), a `title`, an optional `subtitle`, a `tone` for the bar on the card's top edge, and `variant` `card` or `result`. At most one term is the `result` (usually the last; a term the editor's Add appends after it stays valid). `operators` holds one of `+ = - x` per gap; omitted, `resolved_operators()` (and `formulaOperators` on the frontend) draws `+` then a final `=`. `align` (`start` | `center`, default `center`, #10323) centres each card's mark, title and subtitle (`data-align` on `.deck-formula-row`). `show_title: false` on a term with `logo_file_id` or `logos` hides the title, the wordmark names the term and the title becomes its alt text (refused without a logo). A term's logo row stays on one line (`flex-wrap: nowrap`), its marks shrinking alike to the card's width.
- `config_cards` (`ConfigCardsBlock`): 2-4 `cards`, each a `title`, `subtitle`, `tone`, 1-6 `rows` and an optional `footer`. A row is a `logo_file_id` or a `label` (one is required), a `chip`, a `chip_tone` and a `state`: `dimmed` inks the logo in the muted colour (`LogoMark` tone `muted`, with `data-logo-tone="muted"` on the row, the ancestor its ink layer keys on) and draws the chip with a dashed border. The inspector picks a term's logo and its light variant from the template's logo assets. A chip is solid in its colour with `--deck-on-brand` text (white; the `success` and `warning` tones keep their `--tone-ink`, and on a dark slide every tone's chip takes its `--tone-ink`, the page background, because the tones run light there); on a dark slide the result term's text is `--deck-bg` too, as the gradient banner's; `tinted: true` washes the row in the chip's colour and draws the chip as text ("Included"). Card `tone`, a formula term's `tone` and `chip_tone` take a tone or a template palette colour (`ToneOrColor`: `brand_1`-`brand_4`, `chart_1`-`chart_6`, drawn as `data-color`). A term's or a row's `logo_light_file_id` is the logo's variant for a light slide (a white wordmark's dark twin), picked by `logoForScheme` from the slide's resolved scheme. Since #10323: `heading` draws a label over every card with a rule running to the edge (`.deck-config-cards-heading`, a fourth grid row); `row_style` `outlined` (default) frames an active row in the card's `--tone`, puts a dimmed row on `--deck-surface-solid` and keeps a tinted row's wash, `plain` draws the earlier unframed rows; the rows of a card are a subgrid of two columns (name, chip), so the chips sit at the end in one column of one width; the `footer` is a band washed in the card's tone with `footer_value` (refused without `footer`) at its end in the page's ink; the card title is `--deck-fg`, stepped down to `sm` at 4 cards.
- Both collect their logos in `logo_file_ids`, so the geometry of every mark is served as `asset_meta` like a wall's.
- Export: every card is one PowerPoint group (`data-export-group`). The result card carries `data-export-gradient`: the extractor's `gradientOf` reads its two-stop `linear-gradient` and emits ONE `rect` with a `gradient` (`stops`, CSS `angle`), which `pptx_writer._set_gradient_fill` writes as a native gradient fill (DrawingML angle = CSS angle - 90); the card's text is still exported as text. A rect whose uniform border is `dashed` or `dotted` now carries `dash`, written as PowerPoint's dash preset (the dimmed chip).
- The editor edits the terms (title, subtitle, icon, logo and its light variant, each row logo and its light variant, tone or palette colour, look) and the cards (title, subtitle, tone or palette colour, footer, and their rows: label, logo and its light variant, chip, chip colour, tinted, state, through the nested `objects` field kind). Adding a logo to a term's row stays the assistant's (it needs a file).

### Logo walls and callouts

A `logo_grid` inside a callout used to render tiny logos lost in empty space:
`size` changed the cell, not the logo, `framed` drew tall empty tiles around
it, the callout indented every child under its icon column, and every tone
painted a tinted box with a heavy left bar that read as an alert. Three
changes. Callouts and every other block are opt-in: a stored deck that sets
none of the new fields renders as before (a callout keeps its earlier DOM).
A stored `logo_grid` visibly changes, by design: its `size` now names
the height of the MARK (28 / 40 / 56 px) instead of a 64 / 104 / 160 px box,
its rows are balanced, and `framed` tiles hug their logo.

**A logo is sized by its mark** (`blocks/logos.tsx`, `logo-sizing.ts`).
`logo_height` (`LOGO_HEIGHT_PX`: sm 28 / md 40 / lg 56 / xl 72 canvas px;
`size` is its alias, `logo_height ?? size ?? md`) is the height the renderer
draws the MARK at - the box of the file's non-transparent pixels, not the
file. That box is read ONCE from the bytes (`services/image_geometry.py`:
`{v, w, h, trim: [x0, y0, x1, y1], ink}`, the trim with one pixel of slack,
`ink` the mean alpha inside it, no `ink` for an opaque file, `None` for an
unreadable or empty one) and stored on the file (`FileAttachment.image_meta`,
migration `b3818logo001`). The bytes are hostile input, read on the parse
executor and bounded at every step: a raster is decoded only as the one type
its magic number spells among the four a slide carries (`sniff_raster_type`:
PNG / JPEG / WebP / GIF - a TIFF, a PSD or an EPS never reaches Pillow's
decoder set), under `MAX_ANALYSIS_BYTES` and `MAX_SOURCE_PIXELS`; an SVG is
cleaned by the template sanitizer first (`sanitize_svg`, defusedxml: no
entity, no external reference), refused when its shapes are unbounded (a
marker, an embedded image, or more than `MAX_SVG_EXPANDED_ELEMENTS` shapes
once every `<use>` / `url(#id)` reference is expanded) and rasterized by
resvg INTO a 512 px box, never to a fixed width. It is written where a logo
enters the platform, always from the bytes actually stored: a template asset
upload / replace / package import, `attach_template_asset` (the copy's own
bytes, never the source row's box - the sanitizer may have changed an SVG), a
deck package import, the brand asset seed (existing rows adopt it on the next
boot), `fetch_image_asset`. A file stored before that - an older upload - is
read by `save_presentation` for the logos the deck references
(`backfill_image_geometry`, at most `MAX_BACKFILL_PER_SAVE` files a save,
`BACKFILL_CONCURRENCY` at a time within `BACKFILL_BUDGET_SECONDS` - the save
holds its deck's row lock meanwhile, and a file left over is read by a later
save - never raises, a non-image marked read so it is not downloaded again).
Its write lands only on a row still unread AND still naming the object that
was read: an in-place replacement committed meanwhile keeps the geometry of
its own bytes. Every path judges a file by its BYTES, never its declared type:
a PNG stored as `application/octet-stream` is still trimmed. A deck read
serves the geometry of its logos as `asset_meta` (`load_logo_asset_meta`,
keyed by the id AS THE SPEC SPELLS IT; the geometry is a property of the
bytes, served to whoever may read the deck, which already names the file),
template assets serve theirs as `image_meta`, and the mark box
crops the image to it (`--logo-crop-*`): the file's padding never shrinks a
logo. A logo without geometry is drawn from the size the image loaded at.
Every logo carries a `rev` (a digest of the file's storage key, never the
key), with or without a box: a file replaced in place keeps its id - and its
URL - but not its storage key, so the renderer versions the logo's on-screen
URL by it (`useDeckAsset(file_id, rev)`) and an open deck fetches the new
bytes with their new trim box instead of cropping the cached image with it;
the logo's ready signal belongs to the source it was measured for
(`logoPending`), so a replacement holds the print until its own image loads.

**Optical balance** (`optical_balance`, default on): a seal next to a
wordmark of the same height reads a fraction of its weight, so a squarer
mark grows by the square root of how much squarer it is than
`LOGO_BALANCE_REFERENCE_RATIO` (3:1, equal areas), weighted by its ink
(`sqrt(0.5 / ink)` clamped to 0.8-1.25: a solid badge grows less than a
line-art emblem), never below `logo_height` and never past
`LOGO_BALANCE_MAX_BOOST` (1.5x) in a cell that hugs its logo - the row's
protection against one tall tile. A wordmark is drawn at exactly the height
asked for.

**One tile height for sibling grids** (`tile_height`, `logo_grid` and
`logo_wall`): the height of every cell in canvas px
(`LOGO_TILE_HEIGHT_MIN_PX` 32 to `LOGO_TILE_HEIGHT_MAX_PX` 400), independent
of `logo_height`, so four grids on one slide holding different logo mixes
render identical tiles whatever each holds - a tile's height used to be its
padding plus its tallest mark, and the grid whose seals needed `xl` to read
right stood 156 px tall next to its 92 px siblings. The renderer writes it as
`--logo-tile-h` on the figure with `data-tile` (and `data-labels` when labels
are shown); the stylesheet gives every cell that height and bounds the mark by
the cell's inner box, `--logo-cell-h` (the tile less a framed tile's
`--logo-tile-pad-y` twice and a shown label's line, which is kept to one line
so the allowance is exact). The height bound is written as a WIDTH bound -
`max-width: min(100%, calc(var(--logo-cell-h) * var(--logo-ratio)))` - so the
box keeps the mark's ratio (a `max-height` next to an explicit width would
break it) and a row too narrow for its logos still shrinks them in
proportion. In a fixed tile the balance is not capped at 1.5x
(`logoMaxBoost()` hands `logoHeightPx()` `Infinity`; the cell is the bound):
a seal grows to the height its equal ink area asks for and stops at the inner
height, while a wordmark takes the cell's width - which is how a round seal
and a wordmark in one 96 px tile read at one weight where `logo_height` alone
could not make them. A block without `tile_height` matches none of these
rules and renders as before. Unset by default, never materialized.

**Rows are cut by the renderer** (`logoRows`): `columns` (a grid) or
`per_row` (a wall) logos a row, else `rows` balanced rows, else balanced rows
of at most six (a grid) or of what the card width holds (a wall: 6 / 4 / 3
for one / two / three cards a row). `rows` with `columns` is exactly N rows
of M logos, the last one possibly shorter (7 logos in 2 x 4 is 4 + 3, not a
refusal an agent must work around); the validator refuses rows that cannot
hold the logos or would leave a row empty. Each row is one flex line of
cells, N logos under `columns: N` always one row. `align` says how the cells
share it: `even` gives every cell the width of one of a full row's
equal columns (`--logo-columns`, the gaps taken out) and centres its logo,
so a grid under a timeline of as many steps lines up with
them (each logo within 4 px of its step's node at the default gap);
`stretch` spreads cells at their logo's own width edge to edge
(`space-between`, the wall-of-logos look); `start` / `center` pack them -
except that `start` under `columns` keeps the row's equal cells and packs
them from the start (`logoGridEqualCells()`, `data-equal-cells`: the
one selector both `even` and this case key the equal `flex-basis` on), so
four grids of `columns: 4` holding 2, 3 and 4 logos line up as four columns
where hugging cells left each short row its own widths; `start` without
`columns` and `center` keep hugging cells.
Unset (the stored default, never materialized), it is `even` when
the grid sets `columns` and `stretch` when it does not (`logoGridAlign()` in
`logo-sizing.ts`, the inspector naming it in its `Default (...)` option).
`stretch` was briefly the default stored on every grid saved from `main`
(never in a release); such a grid keeps the `stretch` it spells until someone
picks `even`. A short row - fewer logos
than the configured `columns` / `per_row`, else than the longest row, or a
single logo - is centred (`data-short`, `shortRows`: two logos under
`columns: 4` are never pinned to the edges; an even short row keeps a full
row's cell width, `fullRowLength()`), and
a row too narrow for its logos shrinks all of them in proportion - one height
per row, never the widest wordmark alone (a grid's equal tracks shrank only
the widest: Mandiant at 20 px next to 35 px wordmarks). The even cell is a
`flex-basis`, not a grid track, for the same reason, and so that the grid's
minimum width stays its logos' own (what a `columns` block measures, below).
`gap` sm / md / lg is the space between packed logos, between rows, and the
gutter between the framed tiles of even cells; where the free space is shared
out - a spread row, unframed even cells, rows spread by `fill` - it is only a
minimum, and the inspector says so under Gap (`logoGridGapInactiveReason()`
in `field-applicability.ts`). `fill` spreads the rows over the height the
block is given, each centred in an equal band (`space-around`). `framed` is a
tile behind each logo: the whole cell when the cells are equal, a tile that
HUGS its logo otherwise (padding by `logo_height`, the tiles of a row as tall
as its tallest, or `tile_height` tall), white on a light slide, the surface on a dark one. `tone` inks every logo in one
colour through a mask of its own alpha (`mono` = the text colour, `muted` =
the muted colour at 70%) - the "trusted by" look, and the way a white brand
file reads on a light slide; hover and the editor show the original.
`show_labels` (default off) draws the label under the logo; it is otherwise
alt text. An older logo grid drew its labels, so the READ path
(`SpecJSON`, a package import) sets `show_labels` on a stored grid with
labels and no choice written down (`keep_stored_logo_labels`); a grid an
agent writes today draws none unless it asks.

**`logo_wall`** is 1-`MAX_LOGO_WALL_GROUPS` (6) groups of
1-`MAX_LOGO_WALL_GROUP_LOGOS` (12) logos, each group a card (`card` box, the
icon and title on one line in the group's tone) on a grid of `columns` (1-3,
default all up to three, 2 x 2 for four) whose rows share one height, `fill`
(default on) taking the height the slide leaves; inside a card the logos use
its full width and their rows spread over its height. `logo_height` (md),
`optical_balance`, `tile_height`, `tone`, `framed` and `show_labels` read
like a grid's. A card is a `div` (reveal reads a nested `<section>` as a
vertical slide).

**The callout's box and header.** `variant` is the BOX: `tinted` (the tone
wash, the default and the historical look), `card` (surface, line, shadow),
`outline` (line), `plain` (nothing); the tone colours the icon and the title
only, so an accent card is a white card with an accent header.
`accent_bar` is the left tone bar (default: on for `tinted` only). `header`
moves the icon out of its column: `inline` (icon and title on one line),
`stacked` (icon over title), `none` (no icon) - everything below uses the
callout's full width; without `header` the icon keeps its column. `size`
sets `icon_size` (20 / 28 / 36 px for the gutter icon and a stacked head's;
an inline head's icon is 0.6 / 0.8 / 1.05 of the title's type, the same
pixels at the default title) and `title_size`; `padding` and
`children_gap` come from the surface spacing. A filling callout passes its
height to a filling logo grid or wall child (a `:has()` rule scoped to that
case, so no other callout moves). Every default draws the callout as before:
the seed decks and a deck of every legacy callout shape render
pixel-identical before and after (48 slides, both schemes, layout audit
identical).

**Checked and exported.** The layout audit emits one `logo` observation per
logo block - its smallest mark's painted height and its emptiest field's
coverage (the widest row's span times the rows' packed height, each row its
tallest cell and one gap, over the field's box) - graded
`LOGO_MIN_HEIGHT_PX` (24, `dense`: raise `logo_height`, fewer a row, more
room) and `LOGO_MIN_COVERAGE` (35%, `advisory`, floored so 34.9% never reads
as the bar: "logos too small for their space"), dropped on a slide with a
`fit` finding (the cut is the remedy) and told once when two blocks read
alike. A mark is ONE picture for the audit and the editable PPTX
(`data-export-picture`: the cropped image inside is never measured or
captured on its own), and a wall card is one PowerPoint group shape
(`data-export-group` -> the extractor's `group` id -> `_grouped_runs` in
`pptx_writer.py`), every picture where the DOM painted it. Tests:
`tests/test_presentation_logos.py`, `logo-sizing.test.ts`,
`tests_e2e/presentation-logos.spec.ts` (sm / md / lg heights and a
screenshot diff, the 2 x 2 wall in both schemes, the inline header);
`tests/test_presentation_logo_layout.py` and
`tests_e2e/presentation-logo-layout.spec.ts` for the equal cells, the fixed
tile and the step logos.

**Logos on timeline steps.** A `TimelineItem` carries its own
`logo_file_id` (a deck picture like every `*_file_id`: vouched for on write,
granted to the deck's readers, listed by `get_presentation` as `block N
(timeline) item K logo_file_id`), `logo_height` (`sm` / `md` / `lg`, 28 / 40
/ 56 px, default `sm`) and `framed` (default on) - the funding-round slide
(round, date, amount, lead investor) with the fund's logo tied to its step
instead of a separate grid under the timeline that drifts from it. The logo
is a `LogoMark` (trimmed mark, optical balance, its geometry read and served
with the grid's - `logo_file_ids()` walks the steps too), decorative like an
unlabelled grid logo. On `phases` the logo is the fifth of the rows every
step shares (below), so the logos sit on ONE row under the lowest
description, each centred on its node (`data-logos` says one is drawn); a
framed logo is a tile as tall as that row. On `events` the logo sits under
the description, at the body's start. The editable PPTX places the mark as a
picture where the DOM painted it, centred on the node. The inspector picks it from the
template's logo assets per step (an `asset` descriptor in `block-schema.ts`,
drawn by `TemplateAssetField`), and a layout switch to a layout that lists
words says it dropped them (`The timeline's N step logos`).

**Four columns side by side.** A `columns` block of four drew them
2 x 2 on every slide: the stylesheet mapped three columns to three tracks and
left four on the two-track default. Four tracks are now the rule, and the
block wraps to two rows of two only when its WIDEST column's minimum width -
its content's min-content width: its longest word, a table's columns, a row
of logos at their own size - is wider than one track (`columns-fit.ts`,
`columns-fit-math.ts`: a column sized `min-content` for one synchronous
layout, put back before paint, re-measured from a `ResizeObserver` like
`fit.ts`). The room is the block's width as if the body were not shrunk
(`fit.ts` widens a shrunk body by `1 / k`; a decision read off the widened
width would loop). Two and three columns never wrap - three have no balanced
wrap - and keep drawing as before. A wrapped block carries `data-wrapped`
(the columns a row holds), `data-column-need` and `data-column-room`, which
the layout audit reports as a `columns` finding (`dense`, both widths, the
remedy being less side by side in the widest column or fewer columns).

**Rows of equal cards (`equal_rows`).** On a `content` slide's columns
and on a `columns` block, `equal_rows` (default off) lines the blocks up as
ROWS: each column and its run become subgrids of one row per block of the
longest column (`--deck-column-rows`, `equalRowsAttributes()` in
`columns-fit.ts`), so the Nth block of every column shares the Nth row's
height and a card stretches to it - a 2 x 2 of phase cards whose bullets
differ is two rows of equal cards. `equal_height` only ever stretched a
column's SINGLE child; with several blocks per column the cards of a row kept
their own heights. The rows keep their content height and sit where the
vertical alignment puts them (`align-content`); `stretch` / `equal_height`
shares the room between them; rows are a run's block gap apart. The rows take
over from `distribute` on those columns (it would spread each column on its
own and undo the rows): the renderer drops it there, the inspector says so
under `Spread the blocks`, and the audit's sparse remedy no longer names it.
Four columns wrapped 2 x 2 fall back to their plain columns.
Tests: `tests/test_presentation_logo_layout.py`, `columns-fit.test.ts`,
`field-applicability.test.ts`, `tests_e2e/presentation-logo-layout.spec.ts`
(each step logo within 4 px of its node on one row, an even grid under four
steps, every alignment on one row, four columns kept and wrapped with the
audit's finding, the PPTX pictures centred on the nodes, rows of equal cards,
a block added from a run's "+").

**Columns and rows shared by a block's items.** Five blocks lined
their pieces up per item, and the 1920 x 1080 renders showed it. A vertical
`process_flow` is now ONE grid of icon / title / description columns
(`data-icons` says whether the first exists) that every step and its card
subgrid into, so the descriptions start at one x: the icon column is the
icon's, the title column `fit-content(40%)` - the widest title's up to 40% of
the flow, so a title that fits never wraps while the description column,
`minmax(0, 1fr)`, still can - and each piece is placed on its column
explicitly, so a step without an icon among steps with one keeps its title on
the title column. A `phases` timeline is a grid of five shared rows - node,
label, title, description, logo - its steps and their bodies subgrid into
(the margins of the pieces are the spacing, never the grid's gutters, so a row
no step uses costs nothing; the description row is `1fr`, where a `fill`
timeline's height goes), so a title wrapping on one step holds the title row
for all and the descriptions start on one line. A `pull` quote aligned
`center` drops the side bar for `.deck-quote-rule`, a short rule centred over
the text (`centredPullRule` in `quote-style.ts`; a real element the PPTX
extractor paints as a shape, named as the quote's scaffolding by the audit's
`STRUCTURAL`). An inline-headed callout aligns its head at the top and gives
the icon a box one title line tall (`--deck-callout-title-fs` /
`--deck-callout-title-lh`, the pair the title reads too), so the icon sits on
the first line of a title that wraps. A metric tile pulls its icon's box back
by the Lucide inset (`--deck-icon-inset`: the paths 2 units in on the 24
grid, less half the 1.75 stroke), so the icon's INK, the value and the label
share one left edge; a centred tile keeps the box centred. Tests:
`tests_e2e/presentation-block-alignment.spec.ts` (flows of 3 to 5 steps with
and without icons, one- and two-line timeline titles, both quote alignments,
one- and two-line callout titles, the icon ink measured off `getBBox`),
`tests/test_presentation_logo_layout.py`, `tests/test_presentation_quote.py`.

**An icon beside a label.** Twelve places drew "an icon next to a
text label" twelve ways - px gaps of 6 to 32, px icons, `align-items:
center` on the whole label (an icon floating between the lines of a wrapped
one) or `flex-start` (an icon on the text's top edge); a table header icon
touched its text, because the "icon-only cell" rule matched a header whose
text is bare text nodes. They now share ONE row: `IconText`
(`blocks/shared.tsx`) and the `.deck-icon-text` / `-icon` / `-label` rules
in `presentation.css`. The slot is one line box tall (`1lh`: the row carries
the label's first line's `font-size` and `line-height`, the label inherits
them) with the icon centred in it, so the icon sits on the centre of the
FIRST line whatever the label wraps to; a bigger anchor (a feature's tile,
an events timeline's node, a profile's plate - `--deck-icon-text-slot`)
pushes the label down by half the difference instead, so its first line
meets the anchor's centre and nothing overflows the row. The gap is the
token `--deck-icon-text-gap` (0.5em of the label's type; a quarter of an
anchor wider than two ems), the icon's size the token
`--deck-icon-text-size` (1.15em; a table 1.25em, a list marker 0.95em, a
flow icon 1.4em, an inline callout head 0.6 / 0.8 / 1.05em by `icon_size`),
`flex: none` on the slot, `vertical-align: top` so a table cell with an icon
is one line tall, and the label gives the row its baseline so a key / value
row still aligns the value on the label's text. The pairs: table header and
body cells (an icon-only cell draws no label box), list items, badges, the
inline callout head, comparison side labels,
`key_value` labels, features, a metric's trend arrow and delta, `events`
timeline items (the row's type is the label's when the event has one, else
the title's; the node diameter is `--deck-timeline-node`, which the rails
read too), profile lines (the row carries the run's type and steps down
with its `data-fit`), and a vertical `process_flow`, whose icon, title AND
description carry the slot and label classes on the shared columns (the row
class would undo them) so the three start on one line. Not converted yet,
same pattern: `heading`, `section_header`, pricing tier features, persona
points, banner actions, the tier / persona / phase heads.

A horizontal `process_flow` title never breaks inside a word (it did:
`overflow-wrap: break-word` with `min-width: 0` on the steps gave "Meetin /
g"). `overflow-wrap: normal`, `hyphens: none`; a step is no narrower than
its content (`min-width: auto`, a flex item's min-content), and
`flow-fit.ts` (`fitFlowTitles` / `useFlowTitleFit`, the `profiles-fit.ts`
shape) measures the titles at the steps' EQUAL shares (`data-measuring` pins
the minimum back to 0 for the reads): a longest word that does not fit steps
every title down one size (`data-title-fit="tight"`, `--deck-flow-title-fs`
/ `-tight`, `sm` to `xs` under the narrow-flow container query) when that
fits it, and otherwise leaves the size alone and lets the step widen to the
word while its siblings give. Wrapping between words stays. Tests:
`tests_e2e/presentation-icon-text.spec.ts` (every pair above with a one- and
a two-line label where the block lets one wrap - badges and profile lines
are one line by design - the gap read against the row's em, the icon centre
against the first text fragment's; a flow of short words, one whose word
fits one size down, one whose word widens its step; neutral fixture text),
`tests_e2e/presentation-fill.spec.ts` (five features, not six: a feature row
is taller now that its title centres on the tile).

**A horizontal `process_flow` is the equal-columns grid.** A step was
a flex item holding its card AND the arrow after it (64px, 40px in a narrow
flow): the steps shared the flow equally but the cards did not - the last
step has no arrow, so its card read 64px wider than every other - and two
cards were an arrow apart, not a column gap, so a three-step flow under a
`columns` block of three sat on none of its column edges, on screen, in the
PDF and in the editable PPTX alike (the extractor reads the rendered boxes).
Now the steps are the columns: one width each, `column-gap` the `columns`
block's default (`md`) gap, declared once as `--deck-columns-gap`
(`--deck-space-5`, 48px) and read by both rules so the two grids cannot
drift, and no room outside the steps - the first card starts on the block's
left edge, the last ends on its right. The arrow is drawn IN the gutter:
absolutely positioned off its step's right edge (`.deck-flow-step` is its
containing block), as wide as the gutter, as tall as the step, its icon
centred - so the arrow is centred between two cards and takes nothing off
either, and the step's min-content is its title's longest word alone, which
is what `flow-fit.ts` measures and what widens a step (unchanged). A
loop's return arrow has no gutter after the last step, so the flow keeps one
(`padding-inline-end`): every step stays equal, each a gutter's share
narrower than the columns. The gutter does not narrow in a narrow flow (the
container query still steps the arrow's icon down with the titles): it is the
columns grid's at every width, so a flow in a column still lines up under a
`columns` block in the same column. The vertical direction is
untouched. The PPTX export needs nothing of its own: the cards are equal
shapes and the arrows pictures in the gutters because the DOM says so.
Tests: `tests/test_presentation_export.py`
(`test_a_horizontal_flow_exports_equal_cards_on_the_columns_grid_with_its_arrows_in_the_gutters`,
Chromium on the real stylesheet: the extracted cards one width on the
columns' edges, the arrow pictures centred in the gutters, the loop's
trailing gutter - it fails on the old rules with `[509.3, 509.3, 573.3]`),
`tests_e2e/presentation-block-alignment.spec.ts` (three and four steps under
as many columns, a loop of three, read off the live viewer).

### Quotes that carry a slide: `quote` variants and the `plain` metric

The `quote` block had one look - a boxed italic line - so a customer statement
slide ("If I took OpenCTI away from my team, they would walk out.") was two
grow spacers around a card, the speaker's role and company
were typed into the one attribution string, and the proof figures under it
were `metrics_grid` cards. `QuoteBlock` now carries, all optional:

- `variant`: `card` (default, the historical rendering), `statement` (the
  quote IS the slide: no surface, heading font at 700, an oversized accent
  mark), `pull` (beside other content: a 12px accent rule on the left,
  heading-size text at 500; centred, a short rule over the text instead) or `testimonial` (a framed card, the credit row under the text).
- `size`: `md` (34px, the card's historical size), `lg` (44px), `xl` (56px),
  `hero` (`--deck-fs-display`, the title-slide headline), which steps down ONCE
  to `--deck-fs-h1` rather than wrap past `HERO_QUOTE_MAX_LINES` = 3 - measured
  after layout and again once the fonts land by `fitHeroQuote`
  (`blocks/quote-style.ts`, `data-quote-fit="step"`); `align` (`start` /
  `center`); `italic`; `quote_marks` (`none` / `inline` curly marks in the
  text / `decorative` one glyph).
- An omitted look field follows the VARIANT: `QUOTE_VARIANT_DEFAULTS` in
  `presentation_spec.py` and its mirror in `quote-style.ts`, pinned together by
  `tests/test_presentation_quote.py`; the reference words the defaults from the
  table (`quote_variant_default_text`). The stored spec spells only `variant`,
  so a re-saved deck gains one key per quote, and a card switched to a
  statement in the editor does not keep the card's size or italics.
- The credit: `author` / `role` / `organization`, drawn "Author, Role,
  Organization" with the author at 500, or the legacy one-line `attribution` -
  never both (`_one_credit`, a field-addressed refusal; the inspector says so
  under both fields). `quote_credit()` is the one reader of it.
- `logo_file_id` / `photo_file_id`: a company logo after the credit, a
  portrait before it - deck pictures like every `*_file_id` (verified on write,
  shared with the deck's readers; named after the credit in the picture list
  `picture_placements` gives `replace_image` - the portrait by the author, the
  logo by the organization, either by a legacy attribution). Neither needs a credit to be saved ("Name,
  Role" beside a company logo is a valid quote). Their alt text is decided on
  render by `quotePictureAlt` (`blocks/quote-style.ts`): empty when the visible
  credit names the picture (the author the portrait, the organization the
  logo, the legacy `attribution` both - a redundant image stays silent);
  otherwise never empty - the file's description, else a name read off its
  filename, else a localized "Portrait" / "Logo". The deck response serves
  those file facts as `picture_labels` (`load_quote_picture_labels` in
  `presentation_pictures.py`, the `load_picture_facts` rules: only the quote
  pictures the spec references, a withheld file left out), provided to the
  blocks by both slide hosts (`DeckPictureLabelProvider` in
  `asset-context.tsx`), so the viewer and the static export page announce the
  same alt; the PPTX extractor carries an `<img>` alt to the picture's
  `descr`.
- `figures`: 0-`MAX_QUOTE_FIGURES` (4) `QuoteFigure`s drawn as `plain` tiles
  in a row under the credit, aligned with the quote, the label in small caps
  above the value. A figure is a `value`, a `label` and a `tone` and nothing
  else: a plain tile draws nothing else, and a figure has no card look to come
  back to, so a sublabel, an icon, a delta, a trend or a sparkline on one is an
  unknown key the validator refuses rather than a field that shows nothing.

Rendering (`blocks/quote.tsx`). The card's rules in `presentation.css` are
untouched and a quote at the card's values carries NO look attribute
(`quoteLookAttributes` writes only what differs from the card, and a legacy
attribution renders into the bare `figcaption` it always did), so every
existing deck renders the DOM it rendered before; every other look is an
override keyed by `data-variant` / `data-size` / `data-align` / `data-italic` /
`data-marks`. Every mark is a real glyph (`.deck-quote-glyph`, `aria-hidden`;
`.deck-quote-open` / `.deck-quote-close`), never a pseudo-element: the editable
PPTX export reads text nodes, so a statement and a pull quote export as native
text boxes at their size and colour, the mark as a text run in the accent
colour and the figures as text boxes, with no change to the writer. Inside a
statement, `**bold**` inherits the 700 weight (the deck's bold, 600, would
read lighter than the text around it) and an `[[accent:...]]` span only
recolours. PowerPoint only knows bold / not bold, so the author's 500 exports
as regular.

A `content` slide whose single-flow body is ONE statement is centred
vertically (`bodyVerticalAlign`; `vertical_align: end` still pins it to the
bottom, and `distribute`, which spreads nothing over one block, is ignored for
it), so the recipe needs no spacer blocks; the run publishes
`data-valign="center"`, which the `sparse` measure reads as a deliberate
placement. The layout check counts every statement's lines at the size it is
drawn in and reports one past `STATEMENT_MAX_LINES` = 4 as a `lines` finding
(see the layout section).

`metric` and `metrics_grid` gain `variant` (`card` / `plain`: no surface,
border, radius, padding or accent bar - the value in its tone, the label in
small caps) and `align` (`start` / `center`), neither writing an attribute at
its default. A plain tile draws its value and label ONLY (`tileAdornments` in
`blocks/metric-look.ts`, the one rule for the metric, the grid's tiles and the
quote's figures): an icon, a sparkline, a sublabel or a delta a metric carries
stays in the spec for the card look - like a slide field that acts on another
form of the slide (`field-applicability.ts`), it shows again the moment the
tile is a card - and is never rendered on the plain one, so the editable PPTX
and the layout check, which read the rendered DOM, see the tile the screen
shows. The inspector says so under those fields (`blockFieldInactiveReason`,
`blockItemFieldInactiveReason` for a grid's tiles).

`metrics_grid` gains `size` (`sm` / `md` / `lg`, default `md`, written
as `data-size` like the text and list blocks). The density keeps the value
size, the padding and the gaps (`--deck-metric-value-fs`, `-pad-y`, `-pad-x`,
`-gap`: 72 / 56 / 44px values), and `md` is exactly that look. The size scales
the value, the padding and the tile's inner spacing (`--deck-metric-scale`:
`sm` 0.7, `lg` 1.25) and steps the label along the type scale (`sm` to
`--deck-fs-xs`, `lg` to `--deck-fs-md`); the sublabel and the delta never go
under `--deck-fs-xs`, the fine-print floor, and step up to `--deck-fs-sm` at
`lg`. The grid is a held block of the fit (above), so a size reads the same on
every slide of the deck. A `metric` block, the dashboard tiles and a quote's
figures keep their own rules. The guidance's soft budget asks that a KPI row
recurring across a deck keeps one size and one density. `blocks/metrics.test.tsx`
snapshots a grid at the three sizes against the real stylesheet and checks the
hold and its release; `test_presentation_spec_v2.py` pins the field, the
reference and the guidance, and `test_presentation_layout.py` the hold's
contract across `fit.ts`, the stylesheet and the audit, and the `size` finding.

**A table's `size` and `fill`**. A 6 x 6 budget table on a content
slide left ~110px bare above its footer callout: `density` only stepped the
padding DOWN, and the table was the one grid block without `fill`. `table`
gains `size` (`sm` / `md` / `lg`, default `md`, `data-size` on the
`.deck-table-block` root, `TABLE_SIZE_DESCRIPTION`): the root declares the
cell type (`--deck-table-fs`), the type `tight` steps down to
(`--deck-table-fs-tight`), the footnote's (`--deck-table-footnote-fs`) and the
padding scale (`--deck-table-scale`); `md` is exactly the look before the
field (cells and headers at `--deck-fs-sm`, footnote at `--deck-fs-xs`,
18 x 28px at normal density, `tight` at `--deck-fs-xs`), `sm` steps the cells
down to `--deck-fs-xs` (the fine-print floor - the footnote stays there) at
80% of the padding, `lg` steps the cells up to `--deck-fs-md` (tight to
`--deck-fs-sm`), the footnote to `--deck-fs-sm`, at 125%; the cell icons scale
with the padding. The table is NOT a held block: it shrinks with its body like
a list, and `lg` on a table that does not fit is reported by the ordinary
`fit` / `clipped` findings. `fill` is the bounded row spread of the other grid
roots with one difference: a stretched `<table>` grows its ROWS, not the gaps
between them. Under `.deck-blocks > .deck-table-block[data-fill="true"]` the
wrap becomes a flex column, the table `flex: 1 1 auto`, the header row is
given a set height (a constrained row, so the table's excess goes to the
unconstrained body rows first, in proportion to their height - evenly for
rows of a line each) and the cells centre vertically; the footnote stays
outside the wrap, right under it, and what is left stays below it. The cap is
`useSpreadCap` with a measurer of its own (`measureTableRows`): the natural
`<tr>`s cannot be read off a stretched table, so the root is put at its
content height for one synchronous layout (`flex: none`, the stale cap
lifted), the rows read, and everything put back before paint - the
`columns-fit.ts` recipe; the frame is what the root holds besides the rows
(the wrap's border, the footnote), so the cap is natural height plus one
`SPREAD_ROW_MAX_EXTRA_PX` per body row. The rules are scoped to a run: in a
dashboard panel, which sizes its table itself, `fill` neither stretches nor
caps (`measureTableRows` answers a filler there). `capSpread` /
`useSpreadCap` / `recapSpreadsWithin` take the measurer (`SpreadMeasurer`,
`SpreadMeasurement`), `measureChildren` being the default every other root
keeps. The export writes nothing new: the cells are rectangles and text runs
at the measured geometry. Pinned by `blocks/table.test.tsx` (the size x
density matrix against the real stylesheet, `md` byte for byte, the `fill`
root), the "a table's `size` and `fill`" section of
`test_presentation_spec_v2.py` (the fields, the defaults as the only
difference, the reference, the guidance, the `sparse` remedy naming the table,
the renderer / stylesheet / types / inspector tripwires, the run-scoped fill
rules and the measurer), the `useSpreadCap`
wiring in `test_presentation_layout.py`, and on a live deck by
`tests_e2e/presentation-table-size-fill.spec.ts` (the defaults equal to no
field, the type and padding per size, the body rows grown by one amount under
the header kept, the published cap, no clipping, and the layout check's `fit`
on fourteen `lg` rows).

Every KPI tile (`MetricItem`: a `metric` block, a grid's tiles, the dashboard
tiles) gains `delta_tone` (`DeltaTone`: `success` / `error` /
`warning` / `muted`, optional). `trend` is the DIRECTION of the change and
colours the delta by it (`up` success, `down` error, `flat` muted), which reads
a fall in a loss, a burn or churn as bad news; `delta_tone` names the news.
The tile writes it as `data-tone` on `.deck-metric-delta` (absent when unset, so
a deck without it renders the DOM it always did), the shared tone rules set
`--tone` there, and `.deck-metric-delta[data-tone] { color: var(--tone); }`
follows the trend rules at the same specificity, so it wins. The arrow is a
Lucide icon in `currentColor`: it takes the tone and keeps pointing the way
`trend` says. The tile's own `tone` still colours its value and its light. A
`plain` tile draws no delta, so the field is kept for the card like `trend`
(`PLAIN_TILE_UNDRAWN`). The PDF prints the stylesheet, the pixel PPTX is the
slide screenshot and the editable PPTX reads the delta's computed colour into
its text run and captures the arrow as a picture, so the three exports need no
code of their own. The guidance states the rule once (`DELTA_TONE_RULE`: for
costs, loss, churn and burn, a fall is `success`) and its `metric` example is
such a fall; the design skill's KPI wall says the same. `replace_text` never
rewrites the field (`_TEXT_SKIP_KEYS`). Pinned by `blocks/metrics.test.tsx`
(a snapshot of the Loss tile, every tone on every trend, the default
unchanged, the same on the three surfaces) and the `delta_tone` section of
`test_presentation_spec_v2.py` (field, refusals, reference, guidance, the
frontend mirror).

A `dashboard` slide gains `tile_size` (`DashboardTileSize`: `sm` /
`md`, default `md`, one size for the whole row - a tile carries none). The
tile row reads at a fixed tight size whatever it holds, and only a crowded row
(`data-count` 7 or 8) stepped down, so in a row of 5-6 tiles a comparison
sublabel ("Q3 25 EUR 4.5M · Q2 EUR 6.7M", 28 characters, 283px at 20px in a
217px tile) wrapped and pushed the panel grid down. `DashboardLayout` writes
`data-size` on `.deck-dashboard-tiles` (`md` when the field is absent, as a
grid does), and `[data-size="sm"]` joins the crowded row's selectors one for
one - padding `12px var(--deck-space-2)`, value 36px, sparkline 84x30 - and
adds the notes: label, sublabel and delta at `calc(var(--deck-fs-xs) * 0.8)`,
16px on a slide and 20.8px on a portrait page, whose fine print is larger.
16px, not 17: measured with IBM Plex Sans, that sublabel is 240.8px at 17px
against the 232.7px a sixth of the 1720px body leaves inside the small
padding, and 226.6px at 16px. `md` has no rule of its own, so an unset or
`md` row renders exactly as before at every count (byte-identical captures
against the renderer without the field, 5 to 8 tiles), and 7 or 8 tiles keep
their step under either size. The PDF prints the stylesheet; the editable PPTX
reads the computed sizes (18pt value, 8pt notes on a 16:9 slide at 0.5pt/px)
and writes a one-line box with `word_wrap` off, so the sublabel stays on one
line there too. `replace_text` never rewrites the field (`_TEXT_SKIP_KEYS`);
the inspector offers it (`slide-schema.ts`, "Tile size"). Pinned by the
`tile_size` section of `blocks/metrics.test.tsx` (the row as it always drew at
2-8 tiles, `sm` at every count, the portrait scale, a snapshot), the
`tile_size` section of `test_presentation_spec_v2.py` (field, refusals at the
field, reference, tool schema, skill, the frontend mirror),
`test_presentation_patch.py` (set in place, never rewritten as text) and the
`sm` case of `presentation-charts.spec.ts` (every note on one line at
1920x1080, the default row wrapping, a clean layout check).

In the inspector the four looks are drawn thumbnails (`QuoteVariantPicker`),
then `size`, `align`, `quote_marks` and a tri-state `italic`, whose unset
option names what the variant picked makes of it ("Default (Extra large)",
"Auto (On)" - `quoteLookDefault`, the `unsetReads` of `SchemaFieldControl`)
while a value picked stays a pin that survives a variant change; the credit fields
(the legacy `attribution` only while a quote carries one - the descriptor's
`whenSet`); the portrait and the logo, picked from the deck template's assets
(`TemplateAssetField`: `people_photo` for the portrait; the customer, partner,
integration, product and brand logos, each tile drawn from the asset's bounded thumbnail - `GET /template-containers/{id}/assets/{asset_id}/thumbnail`, `services/template_thumbnails.py`: a raster resized server-side to 64 / 128 / 256 px, an SVG as stored, the download's visibility and a row-derived ETag with `private, no-cache`, like the Assets tab) and removable - its asset query opts out
of the app-wide `throwOnError` (a 404 on a template the reader cannot see is
said in the field, never thrown to the viewer's error boundary with the
unsaved draft) and its search box carries `OWN_UNDO_ATTR`, so `Ctrl+Z` there
is the browser's, not the deck's (`routesUndo`); and the figures as a list
of value / label / tone fields, whose last removal writes the key back unset
(`unsetWhenEmpty`: the model stores no key for no figures, so adding then
removing one reads clean). The layout switch lists a quote's credit and
figures on a list target and names its pictures as dropped; a `metric` /
`metrics_grid` moved to a dashboard becomes `MetricItem` tiles, and a plain
or centred look a tile cannot keep is said as dropped. `normalizeSlideLook` also drops these block
looks at their defaults wherever the block sits, and every picker of one writes
its default back UNSET - the quote's thumbnails by hand, the metric and grid
`variant` / `align` pickers through the descriptor's `defaultValue`
(`enumShown` / `enumWritten` in `block-schema.ts`: the picker shows the default
while the field is unset and offers no separate "Default"; the backend pins
the value to the model's default and a unit test pins every such look to the
normaliser) - so a look picked away and back reads clean against the stored
spec, with no Save that would write nothing.

### Charts and dashboards

The board deck reproduces a dozen Metabase dashboards, and every
chart in it used to be a code-interpreter PNG in an `image` block: not interactive,
not on brand, not editable once exported, and stale the moment the theme
changed. The `chart` block and the `dashboard` layout replace that with
STRUCTURED data the platform draws itself — the agent authors series and
categories, never pixels. The schema is PR 1 of the issue (this section), the
renderer PR 2 ("Room is computed" and "Motion" below), and the export PR 3 —
native, editable PowerPoint charts, vector charts in the PDF, and the export
log that says what stayed a picture — is "Charts in the exports" under Export.

**The `chart` block** (`ChartBlock` in `presentation_spec.py`): `kind` is one
of `CHART_KINDS` (`bar`, `stacked_bar`, `line`, `area`, `pie`, `donut`,
`gauge`, `combo`, `waterfall`, `butterfly`, `map`), `series` is 1-`MAX_CHART_SERIES` (6) `ChartSeries` — `name`
(<= 40 chars), `values` (finite numbers), an optional `tone` and, on a `combo`
only, a `kind` of `bar` / `line` per series — and `categories` (<= 16 -
`MAX_CHART_CATEGORIES`, held by `_categories_within_the_cap`; a map's up to
`MAX_MAP_CATEGORIES`, one per country - <= 24
chars each; a number typed as a category is its text) whose length every
series must match: the error the acceptance criterion asks for reads
`slides.14 (layout=dashboard): panels.2.block.series.1.values has 7 value(s)
but the chart has 8 categories (length mismatch)`, addressed to the one field
the agent patches. `unit` (<= 8 chars, a suffix) and `format` (`number` /
`currency` / `percent`) say how a value reads; `stacked` (bar / area only,
implied by `stacked_bar`), `horizontal` (bar / stacked_bar only), `show_values`
(default on) and `show_legend` (default: on when there is more than one series
or a target line; a pie / donut always, a gauge never) shape the drawing;
`legend_position` (`LegendPosition` = `top` / `bottom` / `right`, unset
= `top`, never materialised) says where the legend sits — see "Where the
legend sits" below — and is a placement only: a chart that draws no legend
carries it without a word;
`target` is a reference line on an axis chart and the MAXIMUM of a gauge
(`goal_label` names it) — a pie / donut has neither, so `target` there is
refused rather than dropped. Pie, donut and gauge plot exactly ONE series
(shares are never negative); a gauge has one category and one value and needs
its maximum — a `percent` gauge fills `target` in at 100. A `combo` mixes bars
and lines and must have both; `series.kind` belongs to it alone. `caption`
(markup), `size` (`MediaSize`) and `fill` ride along like an `image`; on a
gauge, `size` is also the dial's diameter (`GAUGE_DIAMETERS`, in a dashboard
panel too) and `fill` the largest dial the canvas holds - see "The gauge's
box" below.
`framed` (default on) stands the chart on a card, as a dashboard panel
does; `false` draws it straight on the slide, and a chart in a panel draws no
card of its own (see "Surface" below).

**The waterfall** is a bridge — opening ARR, new logos, expansion,
churn, ending ARR — and the one check a board member makes on it is that the
bars add up, so the validator makes it. ONE series of changes; `totals` are
the indices of the categories drawn from zero (the opening, subtotals, the
ending), every other value a step floating from the running total. Each
total is held to the running sum of the steps since the previous total
(`waterfall_gaps`): it may miss it by `tolerance` percent (default
`DEFAULT_WATERFALL_TOLERANCE` = 0.5, at most `MAX_WATERFALL_TOLERANCE` = 5)
of the MOVEMENT since that total — the steps' sizes added up — never of the
level reached: 0.5% of a 19M ending is 0.10M, enough to swallow the 0.06M a
board member catches by adding the bars, while 0.5% of the 3.5M the steps
moved is the rounding of slide figures. A total is the level from there on,
so a miss is reported once; the first category has nothing to miss. Two
totals with no step (or only zero steps) between them have nothing to absorb
a difference and must be equal - a quarter's ending and the next one's
opening - and a miss between them names both totals and says that no step
between them explains it. A miss is
refused with the total, both figures and the gap written the way the chart
writes a value, in ONE compact tier with the decimals the gap needs (the
gap in a tier of its own when that one would round it to zero, `€10` beside
`€2.5B`)
(`"Closing ARR" (categories.4) €19.14M vs running sum €19.2M, gap €0.06M
(tolerance 0.5% of the €3.5M the steps moved)`), or — with `allow_gap` —
saved as authored and drawn with an `Unexplained` gap step before the total
(`waterfallSteps` in `chart-format.ts`, the same rule as `bridgeMisses`,
mirrored constants pinned by `test_chart_export.py`). The validator
materialises `totals` (sorted, unique), `tolerance` and `allow_gap` on a
waterfall and DROPS them on any other kind: a waterfall switched to a bar in
the inspector keeps the bridge it was saved with, and `totals` has no control
there. Increases take `success`, decreases `error`, totals `accent`, a gap
`warning`; a toned series gives its tone to every bar but a gap. The viewer
draws range bars (`[low, high]`, keyed by position so two gap bars do not
share a band, at least `WATERFALL_MIN_BAR_PX` = 3 tall when the change is not
zero: a 0.06M gap on a 20M axis is one pixel), signed labels over every bar
(`+€1.8M`, `-€400k` — a fall too: a fall to the bottom of the axis would write
under its bar over the category ticks), planned like every axis chart's
values by `axisChartLayout` from the bars drawn (`steps`: set smaller or
turned on its side when wider than its bar, the top margin solved for them,
any that still cannot be written counted for the layout check; a step of
zero, which Recharts draws no bar and no label for, writes its `€0` over its
band from the axis scales, `WaterfallLevels`), a tooltip with the running
level, and dashed connectors (`.deck-chart-connector`) in the gap between two
bars at the level the first leaves, from the axis scale and the planner's bar
width (`barSize`). `target` draws a plan line like any axis chart.

**The butterfly** compares two populations rung by rung -
individual contributors against managers per seniority level, women
against men per age band: a population pyramid. `kind` `butterfly`
(`MIRRORED_CHART_KINDS`, beside `SINGLE_SERIES_CHART_KINDS` and
`AXIS_CHART_KINDS` in the import-time contract) plots exactly
`BUTTERFLY_SERIES` (2) series, the categories its rungs from top to bottom
in the given order: the first series drawn leftwards from a central spine,
the second rightwards. Both are given and written as positive sizes - the
series order says the side, so a sign would say it twice and be written on
every label it reaches. `ChartBlock._settle_the_butterfly` refuses a series
count other than two (`series has 3 entries but a butterfly plots exactly
2`), a negative value by its field (`series.1.values.2 is -18: a butterfly
reads both sides as positive sizes, the series order saying which side -
write 18.`) and a `target`; `_axis_ranges_hold` refuses `y_min` / `y_max`
(the sides read from the spine, `symmetric` is the extent's switch);
`stacked`, `horizontal` (worded for a butterfly: its bars always run
sideways), `series.N.axis` and `series.N.kind` are refused as on any kind
they do not apply to. A series' own `show_values` is a side's. Three fields
of its own, none materialised, so a butterfly that names none stores none:
`spine` (`ButterflySpine`: `center`, unset, writes the category labels in a
column on the spine between the two sides; `left` on the left axis, the
sides meeting at a rule), `symmetric` (unset true: one scale, both sides to
the same extent; `false` ends each side at its own largest value on that
scale, the spine moving toward the smaller side - one scale either way, so
bar lengths always compare) and `show_totals` (each side's total under its
legend entry). On any other kind they are kept and ignored, like a
`unit_scale` on a pie, and the inspector says so (`blockFieldInactiveReason`;
the target and the axis range it refuses on a butterfly are said as
refusals). `show_values`, `label_size`, `value_label_size`
(`VALUE_LABEL_SIZE_KINDS`), `title`, `caption`, `framed`, `fill`, `format` /
`unit` / `decimals` / `unit_scale`, tones and the template palette behave
as on a bar chart; `value_label_fit` does not apply.

The room is `blocks/chart-butterfly.ts` (`butterflyLayout`, pure, like
`chart-gauge.ts`), the drawing `ButterflyChart` in `ChartCanvas.tsx`.
Recharts draws ONE horizontal bar chart whose value axis runs in positions:
the left series stored negative - as the PPTX stores it - the two sides
stacked with Recharts' `sign` offset so they share a row (two value axes
would put them in two lanes), and a centre spine's column held by an
invisible spacer either side of zero (`gap` value units: half the column at
the plot's scale). Every position is written as the size it stands for:
the ticks (`tickLabels`, a zero on each edge of the column, one shared zero
on a left spine), the values and the tooltip (`ButterflyTooltip`, every
digit). One step serves both sides (`valueAxisScale`'s round ladder, never
a percent dial: a share of a population is drawn to its own size), cut in
up to `MAX_SIDE_INTERVALS` (4) per side while the ticks stay
`LABEL_GAP` apart. The category labels are drawn by `ButterflyMarks` with
the category ticks' class (`deck-chart-tick-category`, which the extractor
reads the chart's text off): centred on the spine's column, whose width is
the longest name plus `SPINE_PAD` (12) each side within
`CATEGORY_AXIS_MAX_SHARE`, or right-aligned against the left side's room on
a left spine, where a value axis' tick would stand. A rung is read by its
name, so where the rows are too close for the tick size the names step
down the text scale, and only past its smallest is every other one left
out (`categoryStep`). Each value is written past the outer end of its bar,
reading away from the spine, at the largest size of the values' scale
(`chartValueScale`) whose row holds it (`size + LABEL_HALO` against the
band) and whose run past the longest bars (`labelRun`) leaves
the two sides `MIN_PLOT_SHARE` / `MIN_PLOT_PX` of the width: that room is
solved in the plot's margins, pass by pass on the scale each leaves, so the
plot - and its grid, the extractor's inner box - is exactly the two sides
and the spine. What cannot be written is counted (`narrow`: the rows;
`short`: the sides), published on `.deck-chart-facts` with the axis as the
PPTX writes it (`data-min` the left side's extent negative, `-max`, `-step`)
and the bars' spacing (`data-bar-*`, no gap between the sides), and worded
by the layout check for a butterfly (`_BUTTERFLY_VALUES_REMEDIES`: more
height, or more width and `spine left`, never `value_label_fit`). Bars are
rounded at their outer end only (Recharts mirrors a bar drawn leftwards).
A zero is a rung that side lacks: it draws no bar and writes no
value - the layout's `marks`, which `ButterflyMarks` draws, leave it out,
so it is neither asked nor written in the facts - while its tooltip still
reads 0 and the side's total counts it for nothing, as a bar chart's zero
draws no bar and no value on screen; no other kind changes (a line or an
area writes its 0, and a bar chart's PPTX labels keep theirs). A hidden
side (a legend click) draws no bar and writes no value; the axis keeps
both sides. The payload (`chartExportPayload`)
publishes the sizes as given, the chart as `horizontal` and its `spine`.

The legend of a butterfly that names no `legend_position` is SPLIT over its
two sides - the first series at the left end, the second at the right end -
on a row of its own between the head and the canvas (`data-split` on the
list, `data-legend-split` on the figure; a titled head keeps the title
alone across both columns); in a dashboard panel that row spans the panel
under its title and over the canvas (`grid-template-rows: auto auto
minmax(0, 1fr)`). A `legend_position` places the usual legend. With
`show_totals` each entry writes `Total <size>` under it
(`.deck-chart-legend-total`, `butterflyTotals`, in the run its values take
their decimals from), right-aligned under the right-hand entry of a split
legend, wherever the legend sits; a chart that draws no legend writes no
totals, and the inspector says so. Pinned by
`blocks/chart-butterfly.test.tsx` (the issue's pyramid through the real
`ChartCanvas` on a content slide's canvas and two dashboard panels: bars
mirrored from the spine's two edges on one row and one scale, every value
and tick unsigned, the rungs on the spine in order, values inside the
canvas and clear of the rungs on a left spine; `symmetric: false`; the
names stepped down in a short panel; the counted reasons; the legend and
totals on the real stylesheet, slide and panel), `field-applicability.test.ts`,
`tests/test_presentation_spec_v2.py`, `test_presentation_guidance.py`
(`BUTTERFLY_CHART_EXAMPLE`, the acceptance block, drawn by the catalog
test) and `test_presentation_layout.py`.

**The map** is a choropleth: a figure by country. Its `categories`
are ISO 3166-1 alpha-2 codes - `ISO_3166_ALPHA2` in `country_codes.py`, the
249 officially assigned, no user-assigned `XK`, no exceptionally reserved
`UK` / `EU` - stored upper-case, each once, up to `MAX_MAP_CATEGORIES` (249);
every category that is not one is refused in ONE message naming each index
(`categories.1 "UK", categories.3 "EU" are not ISO 3166-1 alpha-2 country
codes: ...`), a code given twice names both (`categories.2 "FR" repeats
categories.0`). ONE series; no value axis (`y_min` / `y_max` refused), no
`target`, no stack, no series `show_values` or `axis`. `region`
(`MapRegion` = `world` / `europe` / `americas` / `apac`, unset = `world`,
never materialised) zooms it, and is dropped on any other kind like the
bridge fields. `unit_scale` and `value_label_*` do not act on it.
`legend_style` (`MapLegendStyle` = `ramp` / `classes`), `classes`
(`MapClassCount`, 2-7) and `markers` (bool) are dropped on any other kind the
same way, and `classes` on a map whose legend is the ramp, so switching the
kind or the legend in the inspector still saves; `ramp` is stored as unset
(`_ramp_is_unset`, like a unit scale's `auto`), and `markers` is stored as
given, a tri-state in the inspector like a butterfly's `symmetric`.

The drawing is `MapCanvas.tsx`, a lazy chunk of its own that carries Natural
Earth's 1:50m countries (`world-atlas`, decoded once with `topojson-client`,
projected with `d3-geo`): a deck with no map never downloads the atlas, a
map never downloads Recharts. The 1:50m scale keeps the small countries a
deck names (Singapore, Malta, Bahrain), which the 1:110m drops.
`chart-map.ts` plans it, pure, as `chart-layout.ts` does an axis chart:

- **Projection.** Natural Earth for the world, Antarctica left out unless the
  map names `AQ`; Lambert azimuthal equal-area on 10E 52N (LAEA Europe) for
  Europe; Natural Earth recentred on 100W and 125E for the Americas and
  Asia-Pacific, so the antimeridian cuts neither. The region's box of
  longitudes and latitudes (`REGION_VIEWS`) is fitted into the canvas;
  whatever else the canvas holds is drawn around it and clipped at its edge.
  Outlines are decimated to 0.75 canvas px and islets under 1 px squared
  dropped (a world map's path is ~270 KB, not a megabyte); a country left
  with no outline is still marked.
- **Colour.** A country with a value is filled on the ramp
  (`mapRampColor`): `color-mix(in oklab, <series colour> p%,
  var(--deck-map-paper))`, p from `MAP_RAMP_FLOOR` (30) at the lowest value to
  100 at the highest (the ramp runs from zero when every value is one; 30 keeps
  the faintest country apart from those without a value on the dark scheme), the
  series colour the template's first chart colour - its accent by default - or
  the series' tone. A country without one is `--deck-map-empty` (13% of the
  text colour over the paper), and the borders are the paper itself. The
  paper is the slide's (`--deck-bg`) on a card too: every fill is opaque, so
  a value's ink is read off what is painted, and a template's card may be
  translucent - a border a shade under it still reads as a gap.
- **Classes.** With `legend_style: "classes"`, `mapClasses` cuts the
  distinct values by natural breaks (`naturalBreaks`: Fisher-Jenks, exact - a
  dynamic programme over the cut points on the values centred on their mean,
  the earliest cut on a tie) into `classes` runs, else up to
  `MAP_DEFAULT_CLASSES` (5), never more than there are distinct values. Class
  i of k is the ramp's flat step at i / (k - 1) (`mapRampColor`: the floor's
  wash for the lowest, the series colour for the highest; one class is the
  series colour), and every country, its hover swatch and its marker take
  its class's colour (`mapClassOf`). A class is written as the chart writes
  a value: the top class `<min> +`, open-ended even when it holds one value,
  any other class of one value that value (`43`), the rest `<min> - <max>`.
- **Markers.** A country whose mainland is drawn under `MARKER_BELOW` (10 px)
  across, or that has no shape of its own on the atlas
  (`UNSHAPED_COUNTRY_POINTS`: the overseas regions Natural Earth draws inside
  France, Svalbard, Bonaire, Christmas Island, and a few atolls - a vitest
  pins the two halves against the atlas), is also a dot in its colour, so it
  can be seen and hovered. No code the validator accepts is silently dropped.
  `markers: false` (`mapMarkersShown`) drops the dot of every country
  the atlas has a shape for, which keeps its fill alone however few pixels
  that is (Singapore, Malta and Bahrain have none left at world scale on a
  slide); a country in `UNSHAPED_COUNTRY_POINTS` keeps its dot, since its
  value would otherwise be drawn nowhere, or in the colour of the country the
  atlas draws it inside (French Guiana in France).
- **Values.** With `show_values`, a value is written inside its country when
  its box (`labelWidth`, padded 3 / 2 px) fits wholly inside the country's
  largest outline - at its centroid, else at the nearest point of a grid of
  up to 24 x 24 that holds it - at the largest of the `label_size` scale's
  sizes it fits at, down to the smallest, as an axis chart's values are set
  smaller before they are left out. Each country takes its own: a map
  has no axis to read its values against, a large country writes its value
  large as a map labels it, and one small country never shrinks the others'
  (a single size for the map drove France's value down to Belgium's). It is
  inked white or near-black from its country's fill
  (`data-role="country"`, the stack segment's relative-colour rule). A value
  too large for its country is read on hover, and is not a finding.
- **Region.** Values and markers stay inside the region's frame (the box of
  its edges as drawn): a country with a value beyond it - the US at the edge
  of a wide map of Europe - is drawn in its colour but counted `outside`, and
  the drawing publishes it as a hidden value with reason `region` and the
  codes (`data-values-countries`), which the layout check reports ("values
  hidden on chart N (map ...): 2 of its 6 countries (US, AU) lie outside
  region europe" - drop the region, or another map).

The hover writes the country's name in the reader's language
(`Intl.DisplayNames`, the atlas' English name for a shape no code names -
Kosovo, Somaliland) and its value, or `No data`, in a `.deck-chart-tooltip`
placed beside the pointer in canvas px, the country outlined over its
neighbours. The legend is the ramp (`.deck-chart-ramp` in a
`.deck-chart-legend`, `mapRampLegend`: the series' name, the two ends written
as the chart writes a value, an oklab gradient between their colours), shown
unless `show_legend` is false, a row wherever `legend_position` puts it and
upright beside the plot when it sits `right`. A classed map's legend
is its classes instead (`.deck-chart-legend.deck-chart-classes`: one
entry per class, a round swatch in its colour and its label, nothing to
toggle), placed by `legend_position`, whose default there is `right`
(`legendPosition`; the figure then carries `data-legend-position="right"`
although the block names none): a column beside the plot with the highest
class on top, a row lowest first where it runs over or under the plot. The
PPTX writes the entries as text and the dots as shapes beside the picture,
like any picture chart's legend. The figure is drawn whole: it
is complete two frames after it is painted (`growsIn` is false) and fades in
on a slide entry. Its `.deck-chart-facts` carry `data-region`,
`data-values-asked` / `-written` (the countries, and those inside the
region), `data-values-labelled`, `-marked`, `-outside` and
`data-ramp-min` / `-max`.

**Two units on one figure.** A board dashboard plots a count next to a
price, millions next to a ratio ("New logos and ASP", "Burn and net new ARR" -
the two Metabase charts the Q3 2026 Quarterly Review had to drop). A
`bar`, `line` or `combo` chart (`SECONDARY_AXIS_CHART_KINDS`) reads the series
whose `axis` is `secondary` (`SeriesAxis`; unset is `primary`, the left-hand
axis) against a right-hand value axis that `secondary_format` /
`secondary_unit` write the way `format` / `unit` write the left-hand one, with
an optional reference line of its own, `secondary_target`. The rules are
`ChartBlock._secondary_axis_reads`, each refusal addressed to the field the
agent patches: `series.N.axis` on any other kind (`primary` included, like
`series.kind` outside a combo); a secondary series without `secondary_format`
(`series.1.axis secondary needs secondary_format or reads with format`: the
right-hand axis must say how it writes its numbers, or the series goes back to
the left-hand one); every series on the secondary axis (a right-hand axis
reads a second unit NEXT TO a first one); a secondary series in a `stacked`
or `horizontal` bar chart (a stack adds up against one axis, a horizontal
chart has no right-hand one); and a `secondary_*` field with no series on that
axis. `goal_label` keeps naming `target` alone; the right-hand target is named
by its value.

**Pinned axes and a series' own values.** `y_min` / `y_max` pin the
value axis' range (along the bottom of a horizontal chart), `secondary_y_min`
/ `secondary_y_max` the right-hand one's - the author's way to part a line
from the bars on purpose (a higher `y_max` lowers the bars, a negative
`secondary_y_min` lifts the line). They are optional and unset by default, and
a deck that sets none stores none and draws exactly as before.
`ChartBlock._axis_ranges_hold` refuses them on a pie, a donut or a gauge, a
low end at or over the high one, and a range that would cut off zero or a
value its axis plots - a bar, a point, a stack's total each way, a bridge
level, the axis' target (`_axis_reach`, the mirror of `axisFloor` /
`axisCeiling`) - naming the value: `y_max 20 is under series.0.values.2
(24.9): the value axis keeps zero and every value it plots - set y_max to
24.9 or more, or drop it.` The `secondary_` pair needs a series on the
right-hand axis like every `secondary_*` field. In the viewer `axisView` hands
each axis its own pair, and `valueAxisScale` / `dualAxisScales` keep a pinned
end exactly (`pinnedScale` / `pinnedIn`): both ends pinned, the step count
nearest the chart's that cuts the range into round steps (`0-60M` in three,
`0-50M` in five), else equal steps between the ends; one end pinned, round
steps from it past the values; two axes keep one grid, the count that gives
the pinned ranges round steps winning first. The axis is published on
`.deck-chart-facts` as before, so the editable PPTX pins the same range.
`ChartSeries.show_values` (unset follows the chart) writes or leaves one
series' values; `_series_values_apply` refuses it on a stack, a waterfall, a
pie, a donut or a gauge, which write their values as one. `seriesValuesAsked`
/ `anyValuesAsked` (`chart-format.ts`) read it, the planner still keeps every
value clear of the bars of a series that writes none, and the export payload
marks such a series `show_values: false` - only when the chart writes others,
so a stored deck's payload does not change.

**How precise a figure reads.** `decimals` (`0`-`3`, `ChartDecimals`)
writes every value read against the left-hand axis - labels, ticks, tooltips,
the target's legend name - with exactly that many decimals, of the compact
amount for a currency (`€19.14M`); `secondary_decimals` does the same for the
right-hand axis and, like the other `secondary_*` fields, is refused without a
series on it. Unset, the decimals are decided once per RUN, the values one axis
writes together (`chartValueWriter` over `writtenValues` for the labels,
`axisTick` over the ticks): enough for two significant digits on its smallest
amount under 10, one decimal at most from 10 on, and one more while two
different values would read the same, up to `MAX_AUTO_DECIMALS` (6, past which
a difference is float noise). The burn multiple 0.96 .. 1.57 therefore reads
`0.96x`, `1.57x`, `1.56x` with or without `secondary_decimals: 2`, never `1x`
twice or `1.5x` twice. The editable PPTX mirrors both rules in its number
formats (`chart_export.run_decimals` / `view_values`, `MAX_AUTO_DECIMALS`
pinned by `test_chart_export.py`); one format code serves a whole band, so a
band writes as many decimals as its most precise value shows.

**One unit on a currency axis.** A currency amount is compacted by its
own size (`compactTier`), so one chart can read `€480k` beside `€1.2M`, or a
bridge `-€250.00k` beside `+€2.50M`. `unit_scale` (`ChartUnitScale`: `auto`,
`k`, `M`, `B`) pins every amount of the left-hand axis - labels, ticks,
tooltips, the target's legend name - to one letter, with the chart's
`decimals` (`-€0.25M`, and `€0.00M` for an amount that rounds to zero there,
never dropped); `secondary_unit_scale` does the same for the right-hand axis
with `secondary_decimals`, and joins the `secondary_*` fields refused without
a series there. `auto` is what an unset scale draws, so the validator stores it
as unset (`_auto_is_unset`) and a deck that names it saves, renders and exports
byte for byte as one that does not. The scale applies to a `currency` axis of
a bar, stacked bar, line, area, combo or waterfall chart only
(`unitScaleApplies` / `pinnedTier` in `chart-format.ts`, `pinned_tier` in
`chart_export.py`); a pie, a donut, a gauge, a `number` and a `percent` keep
their own rule, the value stays saved (a chart switched to a pie in the
inspector keeps it), and the inspector says why it has no effect
(`blockFieldInactiveReason`). Unset decimals keep the run rule, read at the
pinned letter (`0.48M` takes two decimals: `€0.48M`). The payload publishes
`unit_scale` / `secondary_unit_scale` only for an axis that pins one, so a
stored deck's payload does not change, and the PPTX writes such an axis as ONE
number-format section at that tier (`"€"#,##0.00,,"M"`, which Excel applies
to the sign too) instead of the conditional bands; a waterfall's signed labels
ride as the viewer's text, as before.

**A combo's line writes its values.** They used to be left to the
tooltip, on the ground that a line's value lands on the bars it overlays; a
board slide whose point IS the line (the burn multiple next to burn and net
new ARR) then said its key figure nowhere in the PDF or the PPTX.
`axisChartLayout` plans them once the bars' values are: upright, in the
largest `LABEL_SIZES` step no larger than the bars' that clears the next
point's value, the plot giving up the room over the highest point. Each value
then sits over its point; under it when a bar's value (or a line's already
placed) holds that room; else raised straight up in its point's column, clear
of the values in its way, while the canvas has room; and past that it is not
written but counted (`ValuePlan.placed`, reason `overlap`, its own remedy in
`presentation_layout._VALUES_REMEDIES`). The drawing marks the moved ones
(`data-placement` `below` / `lifted`) and publishes their size
(`data-point-size`), which the PPTX writer reads.

**A value on a point keeps clear of everything.** A line's value was
placed against the bars' VALUES only, so a point inside a bar wrote its
value on the bar's fill, under the bar's own value; a ratio dipping between
two bars was written across both; and a line chart checked nothing at all - a
first value ran into the value axis' tick at its height (`€20.0M€18.3M`), and
two series' values could print over each other. Every value on a point - a
combo's line, a `line` or an `area` chart, a stacked area's total over its
point - is now placed by ONE rule in `axisChartLayout` (`placeOnPoints`):
over its point, under it, else raised straight up in its column clear of what
is in its way (one raise per thing in its way, at most, and never less than
`MIN_RISE_STEP` 0.01px: a raise lands the value exactly at its clearance, and
the rounding sliver left there once froze the tab in a loop that raised it by
zero), each keeping `LABEL_CLEARANCE` (4px) past its halo from every
value placed before it and the value axes' ticks (as Recharts writes them,
`TICK_TEXT_GAP` 8px from the plot) - two halos that merely met read as one
block of figures, hence the clearance. Only text is in a value's way, never
a bar: a value stays next to its point, on the bar's fill and across
its edge where the point falls in a bar, its halo keeping it legible there.
A value was once kept clear of a bar's edge; on a slide a bar is
narrower than its value (about 45px against 60px), so every value whose
point fell in a bar was raised up its column over the bar's own value, far
from its point, which read worse than the value on the bar. A value that would run into a tick is slid along the
category axis back into the plot (`shift`, `data-placement="shifted"`). When
a raised value finds no room under the top of the canvas, the plot gives up
the height it lacks there, as it does for the bars' values - while it keeps
`minPlot` and the bars' values still clear each other on the shorter plot,
in at most `MAX_ROOM_STEPS` steps, since the bars carry the values over them
down by a share of the room given up - and a value still without room is
counted (`overlap`). A value that touches nothing is written exactly where it
was (`pointLabelPlacement` with no rise and no shift is `outerLabelPlacement`
over a point), so every snapshot of a chart without a collision is unchanged.
Bar values, sizes and turned labels keep their own rules.

**A line's values alternate above and below their points.** A `line`
or an `area` used to accept a size only when its widest value plus `LABEL_GAP`
fit the run between two neighbouring points - every value in ONE row over
its point - and hid every value (`narrow`) when no size did: eight `€246.8k`
in a one-third dashboard panel (48px a point) wrote none. `axisChartLayout`
now tries, right after each size's one-row candidate and before the next
size down, the same size ALTERNATING (`Candidate.alternate`): the even
categories' values over their points, the odd ones' under them
(`writtenUnder`), so two neighbours in a row stand two points apart. Such a
candidate clears when the widest value plus the gap fits TWICE the pitch and
every value under its point - hanging `POINT_LABEL_OFFSET` below it, halo
included - stays above the category axis on the plot the candidate leaves
(`underStaysInPlot`); the values under their points claim no room over the
plot (`highRoom` skips them). The placement is the point rule unchanged, the
odd points trying under first (`placePoint(..., underFirst)`), then over,
then the raise, with the same clearance from the ticks and the other values:
on a line steep enough that a value under its point would meet the one over
the point before it, that value goes over its point like the others (a
rising `€123k`-`€268k` line in a 520px-tall panel reads as one diagonal row
at 22px; a flat line alternates exactly). The plan is published
(`ValuePlan.alternate`, `data-values-alternate` on `.deck-chart-facts`) and
read by the extractor (`values_alternate`): the PPTX writer then labels every
such series point by point (`_add_point_labels`), PowerPoint's own label
above the even points and below the odd ones, so the export writes the same
positions. A chart that fits in one row at its largest size is untouched, as
is every bar chart (the candidate exists for `pointsOnly` charts only); a
line that fit one row only at a smaller size now alternates at the larger
one, which is the asked-for order. A line still too narrow alternating at the
smallest size (sixteen points in a third of the width, or a zero at an odd
point) stays hidden and the layout check says so, its `narrow` wording for a
`line` / `area` naming both tries (`_POINT_CHART_KINDS` in
`presentation_layout.py`). Tests: `chart-layout.test.tsx` (the alternating-values block),
`test_presentation_export.py`, `test_presentation_layout.py`.

**How the values past the bars are fitted: `value_label_fit`.** The
default rule - every size of the scale upright, then turned on its side - gives
the author no say: two bar series over seven quarters in one of two columns
end at 14px under `md`. A `ChartBlock` may say `value_label_fit`: `auto`
(unset, never materialised: that rule, unchanged), `stagger` or `upright`. It
acts on the values of the bars of a `bar`, `stacked_bar` or `combo`
(`valueLabelFit` in `chart-format.ts`, `VALUE_LABEL_FIT_KINDS`), upright or
horizontal (below); a line, an area, a pie or a waterfall draws as
`auto` (the inspector says so, `field-applicability.ts`), and so does a chart
whose values fit upright at the size asked in one row (`auto`'s first
candidate): the three differ only when they do not. Then `axisChartLayout`
sets every value at `scale.values[0]`, upright, and:
- **the bars give way first.** A group's bars stand just far enough apart for
  a value to clear the bars beside it, and the groups far enough apart for the
  first value of a group to clear the last of the one before - two values that
  never stand side by side need no lift and leave nothing out - narrowing
  when they must, never under `GIVE_WAY_MIN_SHARE` (40%) of their width nor
  further apart than an even share of the band; when the groups cannot be
  parted that way the bars only clear the values beside them. Recharts is then
  given the width and the gap (`barSize`, `barGap`), mirrored by
  `fixedBarSlot` (Recharts' `barSize` path: the group centred, its offset cut
  to a whole pixel). In the reported chart the bars go from 35px to 28px.
  **A group of upright bars does not give way** - a `bar` chart,
  neither stacked nor horizontal, two bars or more a category
  (`groupedUpright`): parted for its widest value, three bars a quarter fell
  to `GIVE_WAY_MIN_SHARE` and spread across the band, and the quarters no
  longer read as groups. Its bars stand where `auto` puts them
  (`barCategoryGap` `BAR_CATEGORY_GAP_DENSE`, `BAR_GAP` between two bars),
  and no bar is in its values' way below: a value wider than its bar
  overhangs the gap and the bar beside it, written over a taller
  neighbour's fill in its halo as a value on a point is; only a
  value or a tick lifts it (`stagger`, and `upright` one row) or leaves it
  out (`upright`). A combo keeps the rule above, its line being in the way
  of its values;
- **the near row first, each value in its place on demand.** Along the
  axis, a value is written just past its bar unless a value already on the
  near row, a bar but its own, a value axis' tick or a combo's line is in its
  way there; of two neighbours that would touch, the second waits. A combo's
  line is its drawn path - `monotoneCurve`, d3's `curveMonotoneX` that
  Recharts draws `type="monotone"` with, through each point at the centre of
  its band, re-drawn on each plot tried - cut into runs of `LINE_SAMPLE` (4px),
  each a box of the run grown by half the stroke (`LINE_WIDTH`, which
  `ChartCanvas` draws) and `LABEL_CLEARANCE` (`strokeBoxes`), standing in the
  value's column as a bar does. A chart where nothing touches lifts nothing;
- **`stagger`** then writes the values that wait on the far row, along the
  axis, one row out (`rowStep`: the figures' height, the halo and
  `LABEL_CLEARANCE`, 27px at `md`), raised just clear of what is still in
  their way - a value, a bar, a tick, the line (one step per thing in its way,
  bounded). They come after the whole near row, so a value raised past its
  taller neighbour never pushes the next one off the near row - the
  staircase writing them one by one along the axis made of a level series.
  Where the line climbs through a value's column, the value goes past the
  line, above the bar it reads;
- **`upright`** writes the same near row - exactly the values `stagger`
  writes in their place - and then tries each value that waits one row out
  and no further: `rowStep`, or just past the values it would touch
  in its place, where `stagger`'s far row would start it (`pastIt`). It is
  written there when nothing is in its way (a value, a bar, a tick, the
  line) and the room the plot already gives the values above the bars holds
  it (`fitClaim` against the plot's claim): the plot gives up no height for
  a lifted value, so no bar moves for it. Otherwise it is counted, reason
  `upright` (room: the pitch the bars stand at; need: the widest value with
  its halo). On a horizontal chart, which has no far row, a value that waits
  is counted as before. A stack's total now written a row out carries its
  thin segments' values past it, which may have the plot give up
  room as they do above a total in its place.
Under `stagger` the plot gives up the height the lifted values need
(`fitHigh`, the same equation as `highRoom` with each value's lift) - from
the room the values in their place need, re-planned until it settles, never
past `minPlot`: a value still over the canvas' top is counted (`short`).
`upright` lifts within the room the values in their place claim, so that
room settles at once. A combo's line places its
values against the fitted ones. The plan is `ValuePlan.fit` and `ValuePlan.lifts` (by
`pastBarKey`; a value with no entry is not written), drawn with
`outerLabelPlacement(..., labelOffset + lift)` and published as `data-lift` on
each value lifted (none on a value in its place, which the PPTX writer leaves
on its series' own label), `data-values-fit` and, when the bars gave way or
stand as a group of upright bars (`AxisChartLayout.barSpacing`: that
group's `barSlot`, Recharts drawing it through its own `barCategoryGap`
path, unchanged), `data-bar-width` / `-gap` / `data-category-gap` on
`.deck-chart-facts`. On the
reported chart, with the line hidden only `€2.1` beside `€2.1`, `€3.2`
beside `€3.1` and `€4.1` beside `€3.9` rise, one row each, under `stagger`
and `upright` alike; with it drawn, the values it runs through rise past it
under `stagger`, and `upright` leaves out the ones the line or another value
holds one row out. Under `auto` the figure
is byte-identical to the one drawn before the field (pinned on the chart
shapes of the suite, the field unset and set to `auto`). Tests:
`chart-value-label-fit.test.tsx`, `test_presentation_export.py`,
`test_presentation_layout.py`, `test_presentation_spec_v2.py`.

**The values' own size: `value_label_size`.** `label_size` set ONE
text scale for the whole chart - its ticks and the ladder its values are
tried at - so there was no size between `sm` and `md`, and the values could
not be set a step under the axis text without the ticks and the category
labels following. A `ChartBlock` may say `value_label_size`: `sm` / `md` /
`lg` (that text scale's `values` and `segmentFloor`), or a whole number of
canvas px from `VALUE_LABEL_SIZE_MIN` (10) to `VALUE_LABEL_SIZE_MAX` (32),
whose ladder steps as the scales do - the number, then 2px at a time down to
8px under it, never under 10 (20 tries 20, 18, 16, 14, 12), its segments'
floor 6px under it, never under 10 (`chartValueScale` in `chart-layout.ts`).
Unset - never materialised - it is `label_size`'s own scale, so the plan of
a chart that names none is the one drawn before the field, and its payload
carries no new key (pinned on the chart shapes of the suite: naming
`label_size`'s own scale draws the same markup). The ticks and the category
labels keep `label_size`'s tick size (`chartTextScale`), and so do the axis
widths; everything the values are set in reads the value ladder: the
candidates of `auto`, the size `value_label_fit` stagger / upright keep
(`scale.values[0]`, its `rowStep` included), a combo line's values, the
values inside and past a stack's segments (capped by the totals, or by the
ladder's first size when no total is written), a waterfall's labels. It acts
on the axis kinds (`VALUE_LABEL_SIZE_KINDS` / `valueLabelSize` in
`chart-format.ts`); a pie, a donut or a gauge keeps its tick-sized values
(the inspector says so, `field-applicability.ts`), and the validator keeps
the field there as written, for an author switching kinds. A value outside
the domain - a fraction, a numeral in a string, a bool - is refused once by
name (`_value_label_size_in_domain`). The payload publishes it as written
(the writer's fallback, see "PPTX export"); the measured `data-label-size`
already carries the drawn size. The layout check names it in place of
`label_size` when the chart sets one: a rotated chart is offered the value
size one step down - a text scale's, or the number 2px under, only while
its ladder reaches under the smallest size the values were tried upright in
(`_smaller_value_label_size`, reading `upright` off the observation) - and
the `upright` reason "a smaller value_label_size"
(`_UPRIGHT_REMEDY_OWN_VALUE_SIZE`); `LAYOUT_AUDIT_JS` reads the field off
the payload. In the inspector it is **Value text size**, one picker over the
three scales and the 23 sizes in px (`VALUE_LABEL_SIZES`; a picker mixing
words and numbers writes each option in its own type). Tests:
`chart-value-label-size.test.tsx`, `field-applicability.test.ts`,
`test_chart_export.py`, `test_presentation_export.py`,
`test_presentation_layout.py`, `test_presentation_spec_v2.py`,
`test_presentation_editor.py`.

**`value_label_fit` on a horizontal chart.** A horizontal `bar` or
`stacked_bar` set its values smaller until its rows held them - all hidden
(`narrow`) when no size did, four bars a region in an 836 x 300 column - and
the values inside a stack's segments in whichever size wrote the most
segments: one 5% segment set every segment value of its chart at 16px under
`md` while the chart beside it, with none, kept 22px. The word now acts on
them too. It acts when the values past the bars do not fit as asked (`auto`'s
first candidate) or when a segment's value does not hold inside its segment at
`scale.values[0]` on the plot that candidate leaves (`segmentsAsAsked`); a
chart that fits both ways draws as `auto`. Then:
- **past the bars**, the same plan as upright bars, read on the rows: the
  rows give way first (what a value must clear across the bars is the height
  of its figures with its halo, `fitAcross`, not its width); a lift runs along
  the value's row (`outerLabelPlacement(..., horizontal, ...)`), so a value
  that waits on the near row has no far row to start on under
  `stagger` (`rowStep` 0) and moves along its row just past what is in its
  way, `LABEL_CLEARANCE` clear, a value claiming its run (`labelRun`, so the
  next one starts past its `%`); only values and bars are in the way, the
  ticks standing outside the plot. A value past the canvas'
  right edge (its `labelRun`, the wide glyph's excess kept) is short of room,
  not crowded: the plot gives up width (`fitHigh` with each value's run) as it
  does for the totals, and a value with none left is counted `short`;
  `upright` leaves out a value that would touch another or a bar, counted
  `upright` (room: the pitch of two rows; need: a value's height with its
  halo);
- **inside the segments** (`segmentFit`), one size, the one asked: a segment
  holds its value there or not (`segmentHolds`, the inside rule). Under
  `stagger` one that does not is written past its stack (the annotation),
  at that size rather than capped at `segmentFloor`; one with no room there
  either is counted `segments` with its `thin` facts. Under `upright` none is
  written past the stack: it is counted `segments`, and the observation's
  `fit` (`data-values-fit`, read by `LAYOUT_AUDIT_JS`) has the layout check
  say the size was kept and offer `stagger` first (`_segments_detail`,
  `_segments_remedy`).
The facts are the same as upright (`data-values-fit`, `data-lift`, the bars'
spacing, `data-segment-size` and `data-annotation-size` at the size asked),
so the PPTX writer draws the same: a lifted value's point label is moved by
its lift along `c:manualLayout/c:x`, a share of the chart's width
(`_add_fitted_bar_labels(..., horizontal=True)`), and a stack's totals,
segment labels and annotations are set at the sizes published. The layout
check words a horizontal chart's findings for its rows
(`_HORIZONTAL_VALUES_REMEDIES`, `_HORIZONTAL_CHART_KINDS`): `narrow` and
`upright` say what the rows lack and offer `stagger` or more height, `short`
more width. Upright charts are untouched, in every mode: 1,728 renders (12
shapes x 4 modes x 3 widths x 4 heights x 3 label sizes) are byte-identical
before and after, as are 864 renders of 12 horizontal shapes with the field
unset or `auto`. Tests: `chart-value-label-fit.test.tsx` (the acceptance pair
of share stacks under each mode, grouped bars on their side, the drawn
figure checked for sizes and overlaps), `field-applicability.test.ts`,
`test_presentation_export.py`, `test_presentation_layout.py`,
`test_presentation_spec_v2.py`.

**A thin stacked segment writes its value past its stack.** A stacked
bar wrote each segment's value inside the segment or not at all, and a series
that is 1 to 3% of each stack is 4px long on a 400px plot, 6px at `size: lg`,
where an 18px value needs 24px: hidden at any height, while the layout check
told the agent to take more height. `axisChartLayout` now records every
direction of every stacked bar (`StackRun`: its category, its total, its
segments from the baseline out) and, after the inside pass (`insideOn`, the
bar rule unchanged), writes each value with no room inside PAST the stack
(`annotate`): after the total - above an upright stack, right of a horizontal
one, under or left of a negative one - centred on an upright stack, read from
the total on a horizontal one, in its series' colour (`--deck-annotation` on
the label, `data-role="annotation"`), with a leader from the segment's side
(`SegmentAnnotation.leader`, drawn by `Leader` as one `<line
class="deck-chart-leader">` per leg, `LEADER_WIDTH` 2px, square caps). The
values of one stack are stacked outward, the segment nearest the end first,
each leader's lane `LEADER_STEP` further out than the one before - a leader
that starts further from the end reaches a value further out, so no two
leaders of a stack cross. The lanes run in the gap the stack owns (over a
horizontal stack, the first row's being the plot's top margin; right of an
upright one, the last stack's being the plot's right margin); the gap on the
other side is the neighbour's own, taken only when that neighbour writes
nothing past its stack - taking it greedily pushed every row's leaders into
the next row's gap until the last row had none. Each value (halo included)
and each leg keeps clear of every bar, every value written and every other
stack's leaders, inside the plot across; a value running past the canvas past
the bars has the plot give up the room it lacks (`segmentsOn` in a loop of
`MAX_ROOM_STEPS`, while `minPlot` holds and the totals still clear each
other - `barsStillClear`, which under `value_label_fit` stagger / upright
gives up no room that leaves more fitted values out - never for
fewer values written). Past a fitted total, a value comes after the total
where the fit wrote it, lifted a row or not (`fitPlanAt(claimed).painted`),
and the fit is re-planned on the plot the room left. They are set in one size for the
chart, the largest - never above the values inside the segments, the totals
or the scale's `segmentFloor` - that writes the most. A value with no room
there either is counted (`segments`) and the drawing says what more room
would do (`ValuePlan.thin`): the thinnest hidden segment's share of the axis,
its length (`room`) and what its value needs inside it (`need`), and whether
the SAME chart planned at `TALLEST_CHART_CANVAS` (640px, `size: lg`) high
(`taller`) or at `FULL_ROW_CANVAS` (1670px, a full row or a span 3 panel) wide
(`wider`) writes every value - the planner's own answer (`planAxisChart`
probing itself once, a probe asks no further), with the length the thinnest
would reach there (`reach`). A chart whose segments all hold their value
plans nothing past them and draws byte-identical markup. A series' own
`show_values` now reaches a stacked bar (`_series_values_apply`): false
leaves its segments' values to the tooltip, the stack's totals following the
chart and still counting it; a stacked area stays refused. Tests:
`chart-layout.test.tsx` (the thin-segment block: the acceptance chart at span 2 and 3,
upright and on its side, every value written and nothing touching; three
values past one stack, nested; the facts), `ChartCanvas.test.tsx`,
`test_presentation_export.py`, `test_presentation_layout.py`,
`test_presentation_spec_v2.py`, `test_chart_export.py`.


**Sparklines**: a `MetricItem` (the KPI tiles of `metrics_grid` and of a
dashboard) takes `sparkline` — up to `MAX_SPARKLINE_POINTS` (24) numbers
drawn as a bare shape beside the value, no axis, no labels.

**The `dashboard` layout** (`DashboardSlide`): `title`, `subtitle`, `tiles` —
`MIN_DASHBOARD_TILES` (2) to `MAX_DASHBOARD_TILES` (8) `MetricItem` on one KPI
row — and `panels` — 1 to `MAX_DASHBOARD_PANELS` (6) `DashboardPanel`, each an
optional `title` (markup <= 120), a `span` of 1 / 2 / 3 (1 / 2 on two
`panel_columns`) and ONE `block` from
`_PANEL_BLOCK_MODELS` (`chart`, `table`, `metric`, `metrics_grid`; a text block
is not a figure and is refused) — plus the `footer_block` / `hide_footer` pair
of a `content` slide. The panels fill a `DASHBOARD_GRID_COLUMNS` (3) by
`DASHBOARD_GRID_ROWS` (2) grid under CSS auto-placement, and the validator
simulates that placement (`_grid_rows`): six single panels or two full-width
ones fit exactly, three half-width ones need a third row the 16:9 canvas has no
height for and are refused with the spans spelled out (`panels need 3 rows ...
spans 2, 2, 2`). The acceptance slide of the issue — five KPI tiles over a line
across eight quarters, a stacked bar by version and a bar of trials per quarter
— is one dashboard slide that fits without the shrink warning.

**Panel columns.** `panel_columns` (`DashboardPanelColumns`, 2 or 3,
default `DASHBOARD_GRID_COLUMNS`) draws the grid on two equal columns: the
half-and-half split - a chart beside a table - that no span of the three
columns makes. The slide validator checks every `span` against it first (a
span of 3 on two columns is refused naming `panels.N.span` and
`panel_columns`), then runs `_grid_rows` on the slide's own column count, so
four single panels fill the two rows and a fifth is refused with the
three-column grid as a remedy. The renderer reads the same count
(`DEFAULT_GRID_COLUMNS` in `layouts/dashboard.tsx`, pinned to the backend by
`test_the_renderer_draws_the_columns_the_validator_counts`), clamps a span to
it, and publishes it as `data-columns` on `.deck-dashboard-panels`;
`[data-columns="2"]` is the one rule it adds, so a slide that leaves the field
out keeps the grid it always had (the snapshot in `layouts/dashboard.test.tsx`
was recorded on the renderer before the field existed). The PDF and both PPTX
variants measure the rendered DOM, so they follow the two columns with no
export code of their own. The inspector edits it as **Panel columns** on the
dashboard slide.

**One id, two shapes.** The v1 `dashboard` shares its id with this
layout, so `presentation_legacy` tells them apart by SHAPE, not by id
(`is_retired_slide`): a dashboard slide carrying neither `tiles` nor `panels`
is the retired v1 shape and is upgraded to `content` with a `metrics_grid`;
one carrying either is v2 and untouched. `is_retired_layout` stays an ID check
and answers `False` for `dashboard`, which is what lets the layout switch
accept it as a destination.

**Palette.** A template's `theme_config` carries six ordered chart series
colours, `chart_1` .. `chart_6` (`CHART_THEME_CONFIG_KEYS`, part of
`THEME_CONFIG_KEYS`; the seeded Filigran Brand Deck declares them for its dark
palette and its `light` variant, the XTM One Brand Deck for its own, and the
Template Designer persona plus `build_template_system_prompt` name them). The
primary series is the template's accent (`#0015a8` on the light Filigran
palette); tones map to the semantic colours. The frontend mirrors the keys in
`THEME_VARIABLES` (`--deck-chart-1` .. `--deck-chart-6`, `CHART_SERIES_VARIABLES`
in `theme.ts`) with CSS defaults per scheme in `presentation.css`, so a deck
with no template still gets a distinguishable, on-theme series order. The
brand hues `brand_1` .. `brand_4` sit beside them in `THEME_CONFIG_KEYS`
and fall back to them rank for rank; an architecture node or zone names either
set through `color` (see "Palette colours on a node or a zone").

**Renderer contract (PR 1 ships the components, PR 2 makes them move and
pins them).** The backend tripwires read the frontend, so the schema PR
carries the rendering side it pins: `blocks/chart.tsx` (`ChartBlockView`:
figure, caption, the legend with click-to-toggle series — the last visible
series cannot be hidden, and an edit that would leave none visible shows them
all again, the toggles kept by entry name and never by position so an edit
that removes or reorders a series hides no other one (`legendIdentities` /
`resolveHidden` / `toggleHidden` in `chart-format.ts`) — and
the target line NAMED in the legend with a
dashed swatch, never as a label on the plot where it collided with the
values), `blocks/ChartCanvas.tsx` (the Recharts drawing — the SVG chart
library already in the repo, no second one — sized from a `ResizeObserver` on
`.deck-chart-canvas`, `data-deck-pending` until the figure is complete so the
static render's ready signal waits for it, animation off under
`[data-deck-static="true"]`, `[data-deck-preview="true"]` and
`prefers-reduced-motion`), and `blocks/chart-format.ts` (value formatting:
`€16.5M` / `€527.1k`, `86%`, locale thousands separators in the app's active
language (`useChartLocale()` in `blocks/chart-locale.ts`, read from the i18n
context in the render a language switch arrives in - `<html lang>` is
set only in an effect after it, so it is the fallback outside a provider),
the unit suffix; `axisSurface` decides ONCE
per axis whether every tick is compact — a ceiling at or above
`AXIS_COMPACT_FROM` (10 000) reads `13k / 6.5k / 0`, never a mix;
`legendShown` / `hasTargetLine` / `targetLegendName` are the auto rules
above). Density is size-aware and deterministic (`DENSE_TICKS_BELOW` = 260:
three ticks instead of five; how each value
is written is "Values on the figure" below - a stack's total is hung on the
top-most visible series that has a value IN THAT CATEGORY, since Recharts
draws no rectangle for a zero and a total hung on the top series alone went
missing wherever that series was zero), and value labels wear a halo in
`--deck-chart-halo` (the card the chart stands on - a dashboard panel, or a
framed chart's own - and the slide background for a bare chart) so they stay
legible over the bars - except a value
inside a segment, inked white or near-black from its segment's OKLCH
lightness (`--deck-segment` on the label, a relative colour; a browser
without them keeps a thin halo). The gauge is a grey track plus a reading arc -
square where the dial starts, flush with the track's end, and rounded at its
tip (`gaugeArcPath`, drawn as the reading Pie's `shape`; a reading that fills
the dial ends square on the track's other end too) - its reading scaled to the
dial and centred in the canvas (see "The gauge's box" below). The `dashboard` layout
(`layouts/dashboard.tsx`) is the tile row over a 3-column panel grid (two
columns when `panel_columns` is 2, published as `data-columns`) with
equal panel heights; the panel is itself a grid (`.deck-panel`: title, legend
in the top-right cell, the canvas stretched across the second row) so a
chart's legend never steals the height its plot needs. Every colour comes from
`--deck-*` tokens; the only inline styles are measured values (the gauge
reading's position and size, a legend swatch's series colour).

**The gauge's box.** `chart-gauge.ts` (`gaugeLayout`, pure, like
`chart-slices.ts` for a pie) decides the dial. The gauge is measured by what
it draws: the half disc standing on its baseline, with the reading - the value
over the goal label - written inside it on that baseline. That box, the half
disc or the reading where a dial too small for it stands shorter than its
reading (`gaugeReadingHeight`, the stylesheet's line metrics), is centred in
the canvas, on a content slide and in a dashboard panel alike: a dial in a
third of the panel grid stands in the middle of a panel much taller than a
half disc, never on its floor under an empty half. `size` sets the diameter -
`GAUGE_DIAMETERS`, sm 240 / md 360 / lg 480 canvas px, mirrored in
`presentation_spec.py` for the tool description and pinned by a test - so
gauges of three sizes read as three sizes wherever they stand, a panel
included (where an axis chart's `size` does nothing). `fill`, or
`size: "full"`, draws the largest dial the canvas holds. Either way the
radius is at most half the canvas width less `GAUGE_INSET` (8px) and the box
at most the canvas height less two insets, so the reading is never cut; the
ring is 26% of the radius up to 56px and the value 30% of it, 36-112px. A
deck saved before says nothing new: its gauges draw at `md`, centred. The
other kinds are untouched, pinned by a snapshot of every mark they draw
(`chart-gauge.test.tsx`). The PPTX export needs nothing of its own: the gauge
is a picture of its canvas (`PICTURE_CHART_KINDS`), so the dial stands in the
picture where it stands on the slide, and the e2e export test holds each
picture to its canvas's box.

**Surface.** A chart's surface is the block's choice and never follows
its build. `framed` (the default) marks the figure `data-framed="true"`, and
`.deck-chart[data-framed="true"]` is a dashboard panel's card - padding,
`--deck-card-bg`, the hairline, `--deck-radius`, `--deck-chart-halo` - lit by
the card light with the bloom and no beam; legend, plot and caption sit on it.
In a panel the figure is `display: contents`, so the panel is its card, and
`.deck-panel-body > .deck-chart::after` drops the card light it would add over
the panel's. `framed: false` draws the figure straight on the slide. The
waiting placeholder (`--deck-surface` behind the canvas) is painted only on a
canvas that holds no drawing - `:not(:has(> .recharts-wrapper))` on the pending
or unfinished canvas: the chunk on its way, no room yet, a drawing that gave up
or failed. Keyed on `data-deck-pending` alone, it was painted under the drawing
for the whole grow-in on every slide entry, legend click and resize, and
vanished as the build settled. Pinned by `blocks/chart-surface.test.tsx` (the
canvas background at every change of a build, on the real stylesheet),
`tests/test_presentation_chart_surface.py` and `presentation-charts.spec.ts`.

**Title.** `ChartBlock.title` (a `Label`, the type of
`DashboardPanel.title`) names the figure at the top INSIDE its frame, the way
a dashboard panel titles its chart. A chart anywhere else had no title of its
own: a `caption` sits under the plot, a `heading` stands outside the card, and
a `callout` wrapped around the chart put a title on top but kept the plot at
its readable minimum over an empty band (the callout handed its height to a
logo grid only), so an eight-point line chart on a 2 x 2 content slide hid its
values. With a title the figure opens with a head row - `.deck-chart-head`,
the `h3.deck-chart-title` first and the `.deck-chart-legend` INSIDE it - laid
out as the panel's first row is: `grid-template-columns: fit-content(60%)
minmax(0, 1fr)`, the title left, the legend right and wrapping in its column
(a legend row of its own is 40px a 2 x 2 cannot spare). The head is `flex:
none` in the figure's column, so a `fill` chart's canvas takes the rest of the
frame (the 160px floor still applies); the caption stays under the plot. The
title's type is the panel title's by construction: ONE rule,
`.deck-root .deck-panel-title, .deck-root .deck-chart-title`. Without a title
the figure's DOM and cascade are the ones it always had (the legend a direct
child). A dashboard panel titles its chart itself, so `DashboardPanel`
REFUSES a chart title (`PANEL_CHART_TITLE_REFUSAL`: "a panel chart is titled
by the panel's `title`..."), the inspector says so under the control
(`blockFieldInactiveReason`, `inPanel`) and the stage draws such a draft as
the panel the save keeps (`.deck-panel-body > .deck-chart > .deck-chart-head`
is `display: contents`, the title `display: none`). The content -> dashboard
layout switch moves a chart's title onto its panel (`_figures_from_blocks`;
a heading right above the chart still wins, and the chart's own title is then
dropped and said in the account). The export visits the title as ordinary
text BEFORE the chart primitive, over the framed card and under the chart's
(transparent) frame, and the chart's frame stays the union of the legend and
canvas boxes: PowerPoint's plot is under the title and its legend to the
right, as in a panel. The audit names a chart by its title first, then its
caption, then the heading above. A `fill` callout now hands its height to a
`fill` chart child too (`.deck-callout[data-fill="true"] > .deck-callout-body
> .deck-blocks:has(> .deck-chart[fill])`, next to the logo rule), and the
column rules have a twin one wrapper deeper, so a card around a chart
no longer leaves the band. The soft budget and the seeded skill tell the agent
a chart's one message goes in its `title`, not in a callout around it. Pinned
by `blocks/chart-title.test.tsx` and the 2 x 2 case of `chart-fill.test.tsx`
(the real stylesheet; happy-dom matches a four-deep child chain inside
`:has()` on its prefix, so the row-level negative lives in the e2e spec),
`tests/test_presentation_chart_title.py`, the title text box test in
`tests/test_presentation_export.py`, and on a live deck - the 2 x 2, the
callout form, the layout check and the PPTX - by
`tests_e2e/presentation-chart-title.spec.ts`.

**Where the legend sits.** `legend_position` is the block's word on
the legend's place; `legendPosition()` in `chart-format.ts` reads it (`top`
unset). `ChartBlockView` renders the one legend list before the canvas for
`top` - the historical DOM (a direct child, or inside the titled head beside
the title), so a block that says nothing renders as it always did - and
after it for `bottom` and `right`, the head then holding the title alone
across both of its columns, and marks the figure `data-legend-position`
while a legend is drawn (a placement without a legend changes nothing). The stylesheet keys on that attribute alone. On a slide,
`bottom` is the same flex column with the legend centred under the canvas
(wrapping centred when it does not fit), and `right` turns the figure into a
two-column grid (`:is(.deck-blocks, .deck-slide-footer-block) > .deck-chart`,
so a panel's `display: contents` figure is untouched): the head across both
columns when there is one (`:has(.deck-chart-head)` adds its `auto` row),
then the canvas and a column of entries centred on the plot's height, the
caption under both - every part auto-placed, so the rows follow the DOM. In a panel
(`.deck-panel:has(.deck-chart[data-legend-position=...])`, the descendant
form - happy-dom mis-parses the relative `:has(> ...)`, and a panel holds one
block), `bottom` gives the title its row alone (`grid-column: 1 / -1`), the
canvas the next and the legend a centred line on a third row, the caption
after it, with the rows packed at `--deck-space-1`: a plot whose legend
wrapped on the title's row ends up taller, not shorter, once that legend is a
row of its own under the plot (the canvas' own top margin and the axis band
part the plot from what stands above and below it); a legend that fit on the
title's row costs the plot that row, which is the trade the block asked for.
`right` makes the panel `minmax(0, 1fr) auto`, the title across both, the
canvas and the column of entries side by side. The entries, their toggles,
swatches and font are the same wherever the legend goes. The export payload
carries `legend_position` as written (`null` unset) and the PPTX writer puts
PowerPoint's legend on the same side (see "Charts in the exports"). Pinned by
`blocks/chart-legend-position.test.tsx` (the cascade on the real stylesheet,
both hosts, the unchanged default), `tests/test_presentation_spec_v2.py`,
`tests/test_presentation_export.py` and the `s-legends` slide of
`presentation-charts.spec.ts` (the boxes: one line under the axis, centred,
the plot taller than the wrapped default; a column beside the ring).

**Two value axes.** `hasSecondaryAxis()` (a bar / line / combo chart,
not horizontal, with a series on `axis: "secondary"`) turns on a second
Recharts `YAxis` on the right (`SECONDARY_AXIS_ID`; the left-hand axis keeps
Recharts' default id, the one the grid and every primary series read), and
each series, its value labels and its tooltip row read in the format of the
axis it sits on (`axisReading()`). `axisView(block, axis)` is the chart as one
axis reads it - its series, its target, its format - so every one-axis rule
(`axisCeiling`, `axisSurface`, `valueAxisWidth`) reads either axis unchanged.
The two axes share ONE set of gridlines, so `dualAxisScales()` cuts both into
the same number of round steps - the count, from `tickCount - 1` down to two,
at which the two plots leave the least empty room (New logos and ASP reads
`0 / 10 / 20 / 30` against `€0 / €100k / €200k / €300k`); a percent axis under
100 still tops out at 100. Each axis writes its unit on its ticks
(`formatChartValue(..., { axisUnit: true })`: `0.5x / 1x / 1.5x`, never a bare
`1.5`) and, when its series wear one colour, takes that colour as its tick ink
(`axisInk()`; muted otherwise), so the reader knows which axis a series reads
against. The right-hand target is a dotted line (`data-value-axis="secondary"`)
named by its value in the legend with a dotted swatch. Recharts draws tick
labels in a layer apart from their axis, so each axis' labels carry a class of
their own (`deck-chart-tick-category` / `-primary` / `-secondary`) that the
extractor and the tests read. Between two axes, a line chart's first and last
points get half the widest value label as their inset (`POINT_INSET` at
least), or those labels run into the axes' ticks. Bars on both axes sit side
by side, one lane per series, as Recharts groups bars by category axis.
`ChartFacts` publishes the right-hand axis too (`data-secondary-min` /
`-max` / `-step`). A dashboard panel's title keeps up to 60% of its row
(`grid-template-columns: fit-content(60%) minmax(0, 1fr)`) and the legend wraps
in the rest: three series and a target used to take the row and leave the title
a column of no width, which the PPTX export then dropped.

**Room is computed, never measured (PR 2).** Recharts can size a value axis
itself (`width="auto"`), but it measures the ticks in a hidden span OUTSIDE
the deck, in whatever font the page has loaded at that instant — on the export
page that is the fallback font, and the `€22.5M` column then paints half a
glyph past its room (the first live run of the spec caught it as clipped
ticks). So both axes are given their width outright, from a glyph-count
estimate (`textWidth`: `GLYPH_EM` = 0.58 of the tick font, 22px at `label_size` md): the value
axis from the widest tick it may write (`valueAxisWidth()` — every eighth of
the ceiling, a quarter past it for a domain the scale rounds up, 100 for a
percent chart, the negatives when a value is negative, plus `AXIS_PADDING`),
a horizontal chart's category axis from its longest category within
`CATEGORY_AXIS_MAX_SHARE` (40%) of the canvas. The same estimate drives
density across the width: each category owns a band of the plot (the run
between two points for a line), and a category label wider than its band thins
the axis to `preserveStartEnd` (eight `Q4 24` ticks across a one-third
dashboard panel keep the first and the last). Cheap, deterministic, and the
same on the export page. All of it lives in `blocks/chart-layout.ts`
(`axisChartLayout`, pure), which `ChartCanvas.tsx` draws from.

**Values on the figure.** `show_values` (on by default) is a promise:
a value is not dropped while another way to write it exists, and one that
truly cannot be written is COUNTED. `axisChartLayout` mirrors Recharts' bar
slots (`barSlot`, its `getBarPositions`: `barCategoryGap` 24% a side for a
lone series, 12% for a group or a stack - three bars of a group in one column
of a 16:9 slide are 11px wide at 24%, 18px at 12% - and `barGap` 6) and tries,
for each bar's own value (or each stack's total, or each line point), every
size of the chart's text scale upright, largest first, then - bars only - the
same sizes turned on its side (read bottom to top, its thickness `INK_EM` 0.74
of its size plus the 6px halo against the pitch to the next bar). The text
scale is `label_size` (`CHART_TEXT_SCALES`): `md` sets the ticks at 22
and the values from 22 down to 12, `sm` - a dashboard card's, one step down -
18 and 18 to 10, `lg` 26 and 26 to 14; the stylesheet sets every glyph of the
canvas at the scale's tick size (`--deck-chart-text` on
`.deck-chart[data-label-size]`) and `chart_export.CHART_TEXT_SCALES` mirrors
the table, all three pinned by `test_chart_export.py`. The values may take
another ladder than the ticks (`value_label_size`, see "The values'
own size" below): `axisChartLayout` reads its tick size from `chartTextScale`
and the sizes its values are tried at from `chartValueScale`. On an upright bar
chart a size is not held to the pitch: each value is placed where the drawing
puts it - centred `LABEL_OFFSET` (6) past its bar, on the plot that size
leaves - and its ink plus halo must clear every other value and every bar but
its own, inside the plot across. Two values at different heights do not touch
however near their bars stand: the ARR of a quarter stands far above the net
new ARR beside it, so a 2 x 8 group in one column of a slide is written
upright at 12px where the pitch test turned it on its side at 22px, which is
how the Metabase card it replaces writes it. Lines, areas and horizontal bars
keep the pitch test; a value over a point sits `POINT_LABEL_OFFSET` (10) above
it, clear of the dot. The first size that clears is kept if the plot can give
the labels their room past the bars:
the top margin (the right one on a horizontal chart) is SOLVED from every
label's length and the ratio its bar reaches (`r * plot + L <= plot + margin`,
`plot = length - margin`), and must leave the plot at least `MIN_PLOT_SHARE`
(55%) of its length and `MIN_PLOT_PX` (96). On a horizontal chart that
length is `labelRun`: the glyph-count estimate plus the excess of
the body face's widest glyph over the digit the estimate counts
(`WIDE_GLYPH_EM` 0.96 em, the `%` of IBM Plex Sans semibold, against
`LABEL_GLYPH_EM` 0.6) - a written number holds at most one such glyph (its
`%`, its `M`), and the run of a value past a horizontal bar is what meets
the canvas edge, where the estimate's shortfall was a cut `%` on every
`100%` total of a 100% stack rather than the near miss it is between two
neighbours. An upright chart's labels keep the bare estimate (their height
is what runs toward the edge), so nothing moves there. A value past a
negative bar runs toward the category axis and is drawn just inside its bar
when the plot has no room for it there. Labels on an axis chart are compact once its axis is
(`labelSurface`: `€19.1M`, `2.2M users`; the tooltip keeps every digit).
A stack also writes each segment's value INSIDE it (`role: segment`): one
size for the chart - the one that fits the widest value across the bar and
holds the most segments along it, never above the totals' size and never
under the scale's `segmentFloor` (16 at `md`) while that fits - each segment
holding its value only when it is long
enough; a segment alone in its direction IS the total and is not written
twice. A line or an area keeps `LABELLED_MIN_HEIGHT` (220): under it a label
row sits on the line it describes. Every label is a `<text
class="deck-chart-value">` drawn by `ValueText` (a `LabelList` content: its
`valueAccessor` returns the row's category index, since Recharts hands a
label its position among the bars it DREW and draws none for a zero) with
`data-role` (`value` / `total` / `segment` / `annotation`, a thin segment's
value past its stack), `data-series`, `data-category`
and `data-rotate`, its size an inline style (the stylesheet sets every glyph
of the canvas at the scale's tick size). The drawing publishes the plan on
`.deck-chart-facts`: `data-values` (the own values or totals are written),
`data-label-size`, `data-label-rotate`, `data-segment-size`,
`data-annotation-size` / `data-values-annotated`, and
`data-values-asked` / `-written` / `-reason` (`narrow`, `short`, `segments`,
`dense` - a pie past `MAX_LABELLED_VALUES` slices, which a pie keeps) /
`-room` / `-need` - for `segments`, with `-share` / `-reach` / `-taller` /
`-wider` / `-series` (`ValuePlan.thin`) - which the layout audit turns into a `values` finding;
values turned on their side add `data-upright-size` / `-room` / `-need`
(`ValuePlan.turned`: the smallest size tried upright, the pitch, and what the
widest value needs there), which it turns into a `rotated` finding. Pinned by
`ChartCanvas.test.tsx` (vitest snapshots of grouped and stacked bars over
eight quarters at the `sm` size in one column of a 16:9 slide) and
`chart-layout.test.tsx` (the reported deck's charts - 2 x 8, 1 x 8,
2 x 7 - upright in one column at 300 / 420 / 460px, each value checked
against the bars Recharts drew: centred on its own, just past it, clear of
every other value and bar; the 3 x 8 group turned, with its `turned` facts;
the three `label_size` scales; a horizontal 100% stack's four
`100%` totals, drawn by Recharts in a content slide's fill and in dashboard
panels of span 3 and span 1 at every `label_size`, each ending inside the
canvas at the 2.76 em the browser sets `100%` in, the upright stack's top
margin and a horizontal chart short of its axis top unchanged) and
`chart-collision.test.tsx`
#(5451: a combo whose last point falls inside a bar narrower than its value,
snapshot included - its value under its dot on the bar, within one label
height of it, every other value and the plot where they were; a ratio
dipping between two bars a quarter, written at its point; a line's first
value slid off its axis' tick; two lines meeting; a series' own
`show_values`; the pinned scales - every drawn value checked against every
other value and every tick Recharts wrote, and every line value against its
own dot).

**A pie's and a donut's values.** `blocks/chart-slices.ts`
(`sliceChartLayout`, pure) decides a slice chart's disc and where its values
go, as `chart-layout.ts` does for an axis chart and with its measuring. A
value is written outside its slice, on the end of a `LEADER` (20px, Recharts'
`offsetRadius`) out along the slice's middle, anchored there and reading away
from the disc - where Recharts writes a pie's label (`sliceArcs` mirrors its
sectors: from 12 o'clock, clockwise, `paddingAngle` between non-zero slices);
a slice whose middle is vertical writes its value centred past the leader's
end. The ring is sized from the values: each one's run (`labelRun`, the run
that meets the canvas edge) at its own angle across and its height along,
never more than the 64px `VALUE_RING` every pie kept before, so a pie whose
values clear the edge keeps its radius and its label positions (a value
within the run estimate's margin of the edge - 0.6 em a glyph plus the excess
of its one wide glyph - is drawn a few px further in). While that ring leaves
the disc under its least (half the radius it has with no values,
`MIN_DISC_SHARE`, and 48px, never more than the `VALUE_RING` radius), or
while two values (their halos) or a value and another's leader would touch,
the values are tried a size down the chart's text scale (`label_size`; a
pie's values never follow `value_label_size`), the larger slices placed
first; the first size that writes every value wins, else the one that writes
the most. With no size leaving the disc its least, the disc takes the canvas
(`BARE_RING` 12) and the values go INSIDE the slices at the smallest size,
centred on each slice's middle at the first of a few radii where the value
and its 4px clear room fit (`INSIDE_PIE` / `INSIDE_RING`), as a stack's
segment values are inked (`data-role="segment"`, `--deck-segment` the
slice's colour); a slice too small keeps none. No value crosses the canvas
edge. `ChartCanvas.tsx` draws each value through `ValueText` (size inline,
`data-category` the slice's category) and each leader as a
`<line class="deck-chart-slice-leader">` from Recharts' `label` /
`labelLine` render props, and publishes on `.deck-chart-facts` the counts
and `data-label-size` as an axis chart does, `data-values-reason` `narrow`
with the smallest hidden slice's `-share` and `-room` / `-need` (outside:
the room along the rim beside it and its value's extent there; inside: the
disc's radius and the radius at which that slice would hold its value),
`data-values-placement` (`outside` / `inside`) and, for values set under the
size asked, `data-shrunk-size` / `-room` / `-need` (the size asked, the
radius the disc would keep with the values outside at it, the least it
keeps) - the `values` and `shrunk` findings. Pinned by
`chart-slices.test.tsx` (the reported pie in the canvases Chromium gives a
dashboard panel at span 1, 2 and 3 and a full-width slide, every value's run
inside the canvas, today's radius and label positions where the values fit,
the size step-down, six slices with three at 5% in narrow panels, the values
inside a sliver of a panel).

**Motion contract (PR 2).** A figure builds when its SLIDE ENTERS, not when it
is drawn. reveal lays the next few slides out ahead of time, so a chart is
usually drawn while its slide is still off screen — drawn SETTLED, the
complete figure the overview or a transition shows — and `useSlideEntry`
(a `MutationObserver` on the slide's `<section>` class, counting reveal's
`present`) remounts the drawing on every entry (`key={drawingKey}`) so the
series grow in (Recharts, `ANIMATION_MS` = 700), the target line is drawn
across the plot along its own axis (`.deck-chart-target[data-axis]`, drawn by
hand as the `ReferenceLine`'s `shape` so it owns a class; the stylesheet's
`deck-chart-target-in` / `-vertical` keyframes) and the gauge reading rises
(`deck-chart-reading-in`), keyed on `data-deck-animate` that the canvas
carries only while such a build runs. Outside a live deck the chart is on
screen from its first frame and counts as entered once; `useChartMotion` is
false on the static page, in a preview (the speaker view's panes and the
handout's thumbnails are stills that remount on every move) and under
`prefers-reduced-motion` (the stylesheet has a `reduce` belt too), and then
`drawingKey` is pinned at `0` so nothing ever redraws under the export.
**`data-deck-pending` is honest about a build**: settled is a property of ONE
build - a drawing on one slide entry, rebuilt in place whenever what it draws
changes (an edit, a legend click, a resize, a new locale: Recharts grows the
series again),
so the settled key is the entry count plus the build count and a rebuilt
drawing is pending again until it settles; a canvas that lost its room keeps
its state and is rebuilt when it gets room back - and an animated drawing signals it two
frames after its last series has grown in (`blocks/chart-settle.ts`: each
series reports its growth through Recharts' `onAnimationStart` /
`onAnimationEnd` and counts its animations, since Recharts reports an end on
completion and again on teardown; Recharts writes the value labels only then)
and never before `BUILD_MS` (1000 ms) since the first one STARTED, so the
stylesheet's enter animations run their course. The growth is the clock, never
the mount: the lazy chunk and Recharts' layout passes come first, and Recharts
keys a bar on its position, replacing every bar on every frame it moves.
A series that starts again (a resize, an edit) holds the signal until
its last animation has ended, whichever order the reports arrive in. A drawing
Recharts draws nothing in - a pie or donut whose visible slices add up to
nothing, an axis chart whose every series is hidden (a guard: the legend keeps
one series visible, and an edit that would leave none visible shows them all
again, `resolveHidden`) - settles as a still. And the wait is bounded,
because the reports are Recharts' to send and a drawing may send none (Recharts
renders an area with no points, or a series with no plot to draw in, with no
animation at all): a build that reports nothing for `STALL_MS` (5 s) - no
series starting, none ending - is complete as it stands, and none holds the
signal past `MAX_BUILD_MS` (10 s), however its series keep restarting. Both
count VISIBLE time, frame by frame: a hidden page draws no frame, so its growth
waits for the page, and the signal with it. The drawing is memoized (`memo(lazy(...))`)
so the settle itself, or a parent re-rendering the slide, never re-renders it:
every render hands Recharts fresh formatters, which it takes for a new chart
and grows in again, hiding the value labels meanwhile. So the marker never
says "complete" over a figure whose values are not written yet. That is what
the export page, `deckSettled()` and a test all wait on; a test that read
`.deck-chart-value` right after the pending attribute cleared used to read `[]`.

**Specs.** `frontend/src/components/presentation/blocks/chart-catalog.test.tsx`
(vitest, `yarn test:unit`) draws one block of every `ChartKind` through the
real `ChartCanvas` (a `satisfies Record<DeckChartKind, ...>` catalog, its keys
pinned to `CHART_KINDS`) and reads the waterfall off the SVG: the Q2 to Q3
2026 ARR bridge floating step by step in its roles' tones, signed labels,
category ticks, connectors in the gaps, the `Unexplained` bar of `allow_gap`,
the export payload; `tests_e2e/presentation-waterfall.spec.ts` imports the
bridge through the real route and reads it in the viewer (fills from the
deck's tokens, the tooltip's running level), and checks that the import of a
bridge that does not add up is a 422 naming the gap. A map is drawn there by
`MapCanvas`, as `chart.tsx` draws it; `blocks/chart-map.test.tsx` reads the
map off the real atlas - every ISO code shaped or marked, the ramp's fills,
the world inside a slide's, a panel's and a narrow canvas, the values
written at the largest size each country holds and apart, the markers, the regions and what they leave
out, the hover in the reader's language, the facts, and the ramp legend on a
slide and in a panel through the stylesheet, then the classes: the
natural breaks, the labels and steps, the class fills, `markers: false`, and
the classed legend beside the plot on a slide and in a panel;
`tests_e2e/presentation-map.spec.ts`
imports a map through the real route (a code ISO does not assign is a 422
naming its index), reads it in the viewer on a content slide and in a
dashboard panel (fills, values, ramp, hover), and exports it: a PDF, and a
PPTX where the map is a picture placed where the viewer draws its canvas, no
PowerPoint chart, its title and caption text, the export note said - and the
issue's classed map without markers, its class rows beside the plot and as
text in the PPTX.
`frontend/src/components/presentation/blocks/chart-settle.test.ts`
(`yarn test:unit`) replays against the settle clock, on a virtual page, the
orders in which Recharts can report a growth (a restart before the previous
end, the second end of a torn-down animation, a start during the two frames),
a drawing Recharts draws nothing in, and the bound counted in visible time.
`tests_unit/chart-format.test.ts` pins the pure reading rules
(formatting per surface and locale, palette and tone colours, the legend /
target / stacked / values auto rules, the gauge reading, the legend's hidden
set). `tests_e2e/
presentation-charts.spec.ts` imports one deck with every kind on its own slide
plus the issue's board dashboard, and reads each figure live: values, ticks,
legend names, the target line's axis, the palette fill under both themes
(`--deck-chart-1` resolved and compared to the bar's computed fill after a
`PATCH {theme}`), the tooltip rows, legend toggling (never the last series),
the slide-enter build (`data-deck-animate` on the entered slide only, the
target's `animationName`, and the commit that drops `data-deck-pending`: every
value label written, no bar redrawn), the speaker-view stills and the
reduced-motion context. It runs in `serial` mode: `beforeAll` runs once per worker, so a
fully parallel run would log in and import the decks once per test, and a
burst of logins is what the auth rate limit is there to refuse. A read of the
figure is always an auto-retrying assertion (`toHaveText` / `toContainText` /
`toHaveCount` on a locator), never a one-shot `allTextContents()`, unless a
retrying assertion has just proved the final drawing. A computed style (a
bar's `fill`, the target line's `animationName`) is read in the evaluation that
finds its element (`evaluateAll`), never through a handle resolved one round
trip earlier: the node may have been replaced since, and a detached node has
no computed style at all.

**A chart that fills is handed the room, never 0.** The canvas has no
content height of its own (the drawing is absolutely positioned in it), so a
`fill` / `size: full` chart draws in the free height its flex column hands it,
and in nothing else. A `columns` block in a run of blocks hugs its content, so
a column holding a heading over a `fill` chart had no free height and the
chart drew 0px high: the legend and the caption over no plot. Two rules in
`presentation.css` hand it the room. A `columns` block that holds a filling
chart in one of its columns (`:has()`) takes the free height of the run it sits
in (`flex: 1 1 auto`, like a `fill` block, which also makes it a filler that
`distribute`'s cap leaves alone). The chart's column stretches to the row
whatever `align` / `vertical_align` says, on the block and on a `content`
slide's own columns. The chart then takes what its column leaves after its
siblings. Below that, a floor:
`.deck-blocks > .deck-chart:is([data-fill], [data-size="full"]) > .deck-chart-canvas`
is never laid out under `min-height: 160px` (`CHART_MIN_HEIGHT_PX` in
`presentation_layout.py`, pinned by a test). A run that leaves less overflows,
`fit.ts` shrinks the body and the layout check reports it. A dashboard panel
sizes its own canvas and is not floored. Only charts take part: an image or a
diagram has an intrinsic height and never collapsed, so a `columns` block that
holds no filling chart renders as before.

The export page hides no slide, so a canvas that measures 0 there has no room
for good, and waiting cannot change that: `ChartBlockView` reads it as
`roomless` (`exportPage && box === null`) and settles at once. It is neither
pending, which would hold `deckReady` until the exporter times out (this is how
the layout check once came back skipped), nor unfinished, which would fail
the export. The layout check reports it. `LAYOUT_AUDIT_JS` observes every chart canvas
(`chart`: its painted height, its kind, `fill`, `panel`, and its caption or
the heading right above it), and `_chart_finding` grades it against
`CHART_MIN_HEIGHT_PX`. Under half of it, 0 included, the finding is
`overloaded`; otherwise it is `dense`. The remedy depends on where the chart
sits: a filling chart is told to cut the blocks around it or take a slide of
its own, a dashboard panel to have fewer panels or key figures, any other
chart to cut what shares the slide. `chart` is an excess kind, so it silences
`sparse` (the `Chart too short` row in the Layout check panel). Specs:
`blocks/chart-fill.test.tsx` renders a fill chart in a columns block and in a
fill callout against the real stylesheet. It reads the cascade that hands the room, checks that each drawing
gets a height > 0, and checks that the export page does not wait on a
roomless chart. `tests/test_presentation_chart_fill.py` covers the grading,
the remedies and the renderer contracts.

**Portrait pages.** `A4` and `letter` share the deck's spec, storage, sharing
and export pipeline, authored on a portrait canvas with the paper's own type
scale (`data-deck-format="page"`: 10pt body / 8pt fine print, half-inch margins
— a page is read, not projected). The canvas (px), the paper (in) and the PPTX
slide (EMU) come from ONE table (`CANVAS_SIZES_PX` / `SLIDE_SIZES_EMU` on the
backend, `DECK_SIZE` / `PDF_PAGE_SIZE` on the frontend) so an A4 brief exports
on real A4 and its PowerPoint opens at A4. `dev-docs/presentation-template-
package/deck-product-brief.json` (A4, 2 pages) and `deck-sales-collateral.json`
(16:9, 6 slides) exercise every collateral block in both themes.

What they do NOT share is the on-screen experience. A presentation has a
**`kind`** — `deck` or `page` — DERIVED from its aspect ratio by
`presentation_kind()` (`PRESENTATION_KINDS`; `spec_kind()` reads it off a
stored spec), never stored and never chosen separately: the paper is what makes
it a page, so the two cannot disagree. It is serialised on every response
(`kind`, next to the normalised `aspect_ratio`), filterable on the list
(`GET /presentations?kind=page`), and used by the tool results (`unit_noun()`:
"2 pages" / "A4 page", never "slides" for a brief). The frontend mirrors the
derivation in `presentationKind()` (`geometry.ts`) and picks the surface from
it: a `deck` mounts `DeckStage` (reveal runtime, fullscreen, speaker view,
transitions); a `page` mounts `PageDocument` — the toolbar over a SCROLLING
reading surface, every page stacked as a sheet of paper, fit-to-width plus zoom
steps (`PAGE_ZOOM_STEPS`, `usePageController`), the reading line
(`READING_LINE`) deciding which page is current, no fullscreen and no
transitions because a document is read, not presented. Each sheet is the
export's own `StaticSlide` (shared with `PresentationStaticRender`, which the
headless browser prints) laid out at its full canvas width and scaled as a
whole, so the screen shows exactly what the PDF prints. The chat card, the
library (`Decks` / `Pages` filter pills, a paper badge on page cards, "Open"
instead of "Present"), the editor panel (no transition selector, "Pages",
"Notes" that are never printed) and the shortcuts dialog (`Navigate` / `Read`
instead of `Navigate` / `Present`) all key on the same `kind`.

**Slide ids** (`_assign_slide_ids`): every slide gets a stable server-assigned
`id` when omitted; agents keep it when re-submitting so unchanged slides keep
their React key and the viewer does not remount them.

**Chrome** (`Chrome`): `show_logo`, `logo_position`, `show_footer`,
`show_slide_numbers`, `event_label`, `footer_text`, `fields` — the persistent
furniture drawn around every slide by `chrome.tsx`. Slide numbers are drawn
INSIDE the canvas (never with reveal's own overlay, which sits outside the
scaled slide and would be missing from every export).

**Logo placement** (`Chrome.logo_position`, mirrored by
`DeckLogoPosition` and resolved per slide by `resolveLogoPosition()` in
`logo-position.ts`): the logo sits in the bottom-right corner of every body and
section slide, where the footer bar and the slide number share its row and the
mark must not move between two slides. The `title` and `closing` slides are the
two whose bottom row carries nothing else (no footer bar; the title slide has no
number either), so the corner is a CHOICE there: `auto` (the default) follows
the slide's own composition — bottom-left under a left-aligned headline
(`TitleSlide.align = "start"`, on the slide's left padding edge, the edge the
title starts on), on the slide axis under a centred one and on the closing
slide — and `start` / `center` / `end` pin it. The stored value is deck-wide;
the resolution is per slide, so a body slide is never asked. The bar publishes
the resolved value as `data-logo-position`: `start` is DOM order alone (the
logo is the row's first child, `.deck-chrome-right` still pushes the number to
the corner), `center` is a three-column grid (`1fr auto 1fr`) whose equal side
columns keep the logo on the axis whatever the number on the right measures —
a flex row centring with `margin: auto` would offset it by half that width.
The exports read the rendered boxes, so the PDF and both PPTX flavours follow
without export code; the chrome stays `aria-hidden` and outside the layout
audit. The deck editor's Header & footer section offers the choice under the
Show logo switch (`auto` drops its key like a switch at its default, so an
untouched deck stays clean); the agent sets it through `chrome` on
`generate_presentation` or `edit_presentation`'s `update_deck`, which merges
`chrome` key by key.

**Template header and footer** (`presentation_chrome.py`, mirrored by
`components/presentation/chrome-pattern.ts`): what the footer SAYS is decided
by the deck's template, not written on the slides. A template declares
`extra_metadata.chrome = {footer, header, variables}` — a PATTERN such as
`{{event}} - {{title}} - {{classification}}` — where `{{title}}` (the deck
title), `{{subtitle}}` (the opening slide's subtitle) and `{{event}}`
(`chrome.event_label`) are built in and every other placeholder must be a
declared variable (`key`, `label`, `hint?`, `default?`, `MAX_CHROME_VARIABLES`
= 8) the deck fills once in `chrome.fields`. `render_chrome_pattern` /
`renderChromePattern` collapse an empty placeholder WITH its separator (a
literal is kept only between two filled slots, or at either end when any slot
is filled), so a deck with no event never reads `- Title - Internal`; both
sides implement the same rule from the same regex, pinned by
`test_the_frontend_mirror_renders_the_same_rule_from_the_same_pattern` and
checked case by case against ONE shared table,
`frontend/tests_e2e/fixtures/chrome-pattern-cases.json` (rendering, every
problem code the validators can report, deck filling), consumed by both
`tests/test_presentation_chrome.py` and `tests_e2e/chrome-pattern.spec.ts`.
`{{subtitle}}` is the opening subtitle reduced to PLAIN text
(`markup_to_plain_text_lenient` / `markupToPlainText`, folded onto one line),
so an inline marker never prints in a footer. A field key has one spelling:
`canonical_field_key()` (`.strip().lower()`) is run by the validator
(`Chrome._clean_fields`), by the `update_deck` merge (`merge_deck_fields`,
where `null` or a blank value unsets a key) and by the reader (`deck_fields`),
so `{"Classification": null}` clears `classification` instead of leaving two
spellings. A key no variable declares is kept as stored (the template may
declare it later, the deck may change template) and reported, never drawn:
`footer_status_line()` lists it as ignored and the editor panel shows it as a
stray field.
`template_chrome()` is the lenient READER (what a stored declaration means,
however it got there); `normalize_template_chrome()` is the strict WRITER
(`validate_chrome()` in `template_containers.py` runs it on the REST create /
update / import routes and in the `template_authoring` tool, so an undeclared
placeholder, an unused variable, a bad key or a missing label is a 422 / a
tool refusal listing the available placeholders — never a literal `{{x}}` on
every slide; a canonical `None` drops the key, which is how "no pattern, v1
footer" is stored; on any kind but `presentation` the key is dropped without
a problem, and a change of kind ALONE re-reads the stored metadata against the
new kind on both surfaces - the REST update and `update_template` - so a
deck-only declaration never survives on a report template whose read-back
would keep describing a footer no deck draws; the tool result names the keys
the change dropped, and records `extra_metadata` as changed only when the
stored bytes moved - an echo of the stored metadata, kind or scheme list is
"No change", never a phantom revision in the audit log). The deck response carries `template_chrome` (resolved on
every read from the template facts, like the palette, `None` for a free deck
or a pattern-less template); `chromeText()` turns spec + template chrome into
the footer segments and the header line the component draws — with no
pattern the footer is `[event_label, footer_text]` exactly as before. The
header rides the `show_footer` switch and the same body-slide gate
(`slideShowsFooter`); there is deliberately no `show_header` field. The
built-in brand decks declare `{{event}} - {{title}} - {{classification}}`
(`_BRAND_CHROME` in `template_container_seed.py`); like the event, the
classification is optional and has no default, so a deck that never
sets one draws none. Because a pattern reading `{{title}}` makes
the deck TITLE a field drawn on every slide of THAT deck, the post-edit
layout check widens per deck through `template_chrome_redraws()` (title, and
the opening slide's subtitle when the pattern reads it - decided on the
rendered `{{subtitle}}` value before and after the edit, so a delete or a
move that changes which title slide opens the deck counts, and a rewrite that
leaves the subtitle alone does not) instead of adding `title` to the global
`LAYOUT_DECK_FIELDS`. `footer_status_line()` puts what
the footer now reads into the `generate_presentation` / `edit_presentation`
results, so "mark the deck Confidential" is one `update_deck` on
`chrome.fields` and the reply says what every slide reads; in the edit result
it is read back only when the edit could have changed it - a template change,
a `chrome` edit, or the SAME `template_chrome_redraws()` reading that decides
the redraw (a title or subtitle change the pattern actually reads) - so a
rename on a free deck or on a fixed-pattern template, or a delete under a
pattern that never reads the subtitle, never sends the agent to look at a
footer the edit did not touch. The `Edit slides`
panel's `Header & footer` section (switches, `Event`, one input per declared
variable or the free `Footer text`, live preview) saves the complete `chrome`
object through the same `PATCH` only when it changed; the template's
`Configuration > Design` card holds the pattern and the variables with the
same validation run live (`validateTemplateChrome`, the TS mirror of the
strict writer) and a preview with sample values.

**Brand watermark** (`WATERMARK_KEY` in `presentation_chrome.py` /
`chrome-pattern.ts`): the same declaration may carry `"watermark": true`, and
the body slides of every deck wearing the template then carry its MARK "en
filigrane" - `SlideWatermark` in `layouts/index.tsx`, the first child of the
frame on the `content` / `agenda` / `roadmap` / `takeaways` / `dashboard`
layouts (`WATERMARK_LAYOUTS`; title, section and closing slides keep their
own backdrops): the slide palette's `mark_file_id` cropped into the top-right
corner at `--deck-watermark-opacity`, inked by `--deck-hero-mark-filter` on a
light slide, over a bloom of the brand gradient from the bottom corners
(`--deck-body-glow`). Off unless declared: only a literal `true` turns it on
(the reader), a non-boolean is refused by name (the writer), off is stored
as the ABSENCE of the key (`as_dict()` / the canonical shape write it only
when on, so every declaration stored before keeps its bytes), and a
watermark alone is a declaration that draws no pattern (`declares_pattern`
stays false, `declaresPattern()` on the client, so the deck keeps its v1
footer and the editor its free footer field). The deck decides nothing of
its own: `chromeWatermark(spec, template_chrome)` is the template's switch
AND the deck's `chrome.show_logo` - the watermark is the same brand furniture
as the logo, so hiding one hides both - and both slide hosts (`DeckSlide`,
`StaticSlide`) pass it to `SlideBody`. A template with no `logo_mark` asset
draws nothing. It is a backdrop exactly like the cover's: negative
z-index in the frame's stacking context, `aria-hidden` (outside the layout
audit), and `data-export-backdrop` on the element painting the bloom, so the
capture loop isolates the slide and the PPTX carries ONE picture under the
body with every block editable above it - the same bytes on every body slide
of a scheme, which python-pptx stores once. `describe_template_chrome` reads
it back to the agent, the authoring tool's `extra_metadata` text names it and
the Design editor's `Header & footer` card has a `Brand watermark` switch.
The Filigran Brand Deck declares it (`{**_BRAND_CHROME, "watermark": True}`);
the XTM One deck does not.

**Corner emblem** (`EMBLEM_KEY` in `presentation_chrome.py` /
`chrome-pattern.ts`): the second piece of brand furniture, `"emblem": true`,
validated, stored and read exactly like the watermark (`_FURNITURE_KEYS`: a
literal `true` on, a non-boolean refused by name, off stored as the absent
key, an emblem-only declaration draws no pattern). `DeckChrome` draws the
palette's `mark_file_id` small (60 px high, `.deck-chrome-emblem`) in the
top-right corner of every slide but the title and closing ones
(`EMBLEM_FREE_LAYOUTS`: a cover already carries its hero), so a section
divider wears it too, unlike the watermark. It is part of the chrome layer:
`aria-hidden`, a picture atom in the PPTX like the chrome logo, isolated with
the rest of the chrome by the export capture rule, and a header moves left
of it (`.deck-chrome-emblem + .deck-chrome-header`). `chromeEmblem(spec,
template_chrome)` is the template's switch AND the deck's `chrome.show_logo`,
as for the watermark; both slide hosts pass it to `DeckChrome`. A template
with no `logo_mark` draws nothing. `describe_template_chrome` reads it back
to the agent and the Design editor has a `Corner emblem` switch next to
`Brand watermark`. The THREAD Conference Deck declares it; the two other
seeded decks do not.

**Inline markup** (`presentation_markup.py`) replaces HTML for every
human-readable string: `**bold**`, `_emphasis_` (word-bounded, so `snake_case`
stays literal), `` `code` ``, `[[tone:text]]` with a tone from `MARKUP_TONES`
plus `gradient`, `[text](#/<slide id>)` for a link to another slide of the
deck and `[text](https://...)` / `[text](mailto:...)` for a link out (see
"Slide links inside a deck"), and `\n` for a line break. Flat and
escape-first: no nesting,
and a tag-like sequence is refused at generation time with a pointer to the
grammar, because `<b>` would otherwise print literally on the slide. The
frontend tokenizer mirrors the same token set and renders to React nodes, never
through an HTML sink; `test_frontend_markup_tokenizer_mirrors_the_backend` pins
the two. A deck has NO HTML field at all: the v1 `rich_text` went
with the retired layouts (its words survive the upgrade as plain `text`
blocks), so `rich_text_guard.py` and the DOMPurify profile in `sanitize.ts` now
serve reports only, and `test_no_field_in_the_spec_carries_html` fails the
build if a slide field ever carries markup again.

**Icons**: every `icon` field is a Lucide kebab-case id checked against
`LUCIDE_ICON_NAMES` (+ `LUCIDE_ICON_ALIASES`); `normalize_icon_name` accepts
`ShieldCheck` / `shield_check` / `Shield Check` and canonicalises, an unknown
name is refused with close-match suggestions. The allow-list is generated from
`lucide-react/dist/esm/icons/` — re-run the script after bumping `lucide-react`
(`test_icon_allow_list_matches_the_installed_catalog_module` fails otherwise).

**Diagrams**: `diagram` blocks carry Mermaid source, bounded by
`MAX_DIAGRAM_CHARS` and `check_diagram_source` (no `%%{init}` directives, no
`click` / `href` / `javascript:`); the frontend `blocks/Diagram.tsx` is lazy
(Mermaid is ~1 MB) and runs with `securityLevel: "strict"`.

**The deck theme on a diagram** (`blocks/diagram-theme.ts`). Mermaid
draws from its own theme variables and stylesheet, baked into the SVG it
returns, so `deckDiagramTheme(host)` reads the deck's tokens off the host's
resolved CSS custom properties at render time (every colour painted through a
canvas pixel first: Mermaid throws on the `color-mix()` the dark surface and
both schemes' line are) and hands them over three ways. `themeVariables`
carry the palette where Mermaid's base theme has a variable for it (node fill
`--deck-surface-solid`, node border `--deck-line`, edges `--deck-muted`,
edge-label background the surface, the body font). `themeCSS` carries what it
has no variable for - node labels and subgraph titles in `--deck-heading-font`
at weight 600, the `--deck-border` weight on every node and edge, edge labels
at `--deck-fs-xs` weight 500 on an opaque surface pill with the line as its
border, arrowheads (`.marker`) in `--deck-accent` - compiled by Mermaid under
the SVG's id after its own rules, so it wins by source order and travels with
the SVG into the export. `fitDeckGeometry(root, geometry)` then writes what no
stylesheet can set on a laid-out drawing, as attributes: every rectangular
node (`rect.label-container`, square and rounded alike) gets `--deck-radius`
corners, every labelled edge's `rect.background` is grown by the pill padding
(14 x 4, the architecture block's) around the text Mermaid measured and given
round ends; a stadium, circle, diamond or cylinder keeps its shape.
`MERMAID_CONFIG` (the invariant half - strict mode, SVG-only labels - lives in
the same module) sets `nodeSpacing` / `rankSpacing` to 80 (Mermaid's 50 was
for 16px text; the deck's 26px type scales it, and it is what keeps a padded
pill clear of the nodes on either side). The whole theme is what the palette
observer compares, so a scheme switch re-renders the drawing. Mermaid exposes
no minimum node width: `drawRect` takes `node.width` as a floor but nothing
public sets it before layout (the layout loader that could is reachable only
through Mermaid's private dagre chunk, and widening after layout would leave
every edge routed to the old border), so a row of short labels keeps its
natural widths. Pinned by `blocks/diagram-theme.test.tsx` (the theme and the
geometry on `flowchart LR A-->|x|B-->|y|C-->|z|D`, dark and light snapshots)
and in a real browser by `tests_e2e/presentation-diagram-dark.spec.ts`.

**Architecture diagrams** (`architecture`): the figure a platform or
strategy deck actually needs — product logos in the nodes, labeled directional
flows between modules, the core everything runs on, a hub band, a zone panel, a
legend — which Mermaid cannot draw and which never looked like the brand. It is
authored as DATA, not as a drawing: 2-12 `nodes` placed on a 6x6 grid (`row`,
`col`, `col_span`; `shape` `card` / `pill` / `circle`, `emphasis` `normal` /
`core` / `ghost`, `label`, `sublabel`, `description`, `icon` OR `logo_file_id`
— a template asset drawn in place of the icon — `icon_tile: false` to
draw a card's icon inline before the label instead of in the tile above it, so
the label stays vertically centred and two ghost cards on one row align
(refused without an icon, or on a pill / circle, which draw their icon bare
already); `frame: false` (default `true`) to draw the node as its
content alone; up to 4 `chips` inside a
card), up to 24 `edges` (`source` / `target` by node id, or
`zone:<id, label or index>` for a zone's border, `source_side` /
`target_side` `top` / `right` / `bottom` / `left`, `lane` 1-6, `label`, `arrows`
`end` / `both` / `none`, `dashed`, `tone`, `route` `auto` / `straight` /
`orthogonal` / `curve`), up to 6 `zones` behind a rectangle of cells, each
headed by a `label`, a `logo_file_id` or both, and up to 6 `chips`
on that head row (`chips_align` `end` / `start` / `center`), an
optional `id` naming it for an edge end, and
up to 6 `legend` items. The validator refuses what could not be drawn honestly:
duplicate node ids or zone ids, an edge naming no node or its own node, a `zone:` end naming no zone or a label two zones share, an edge between two zones, an edge between a zone and a node inside it, a `lane` on an edge with a zone end, two edges of one pair of nodes on one lane, the same pair declared
twice in the same direction (a zone named two ways is one end), two nodes on one cell, a node or a zone running
past the grid, a zone covering no node or cutting through a spanning node,
overlapping zones, a circle spanning columns, chips on a pill, a node or a zone setting both an icon and a logo_file_id, a zone with neither a label, a logo_file_id nor chips, a `chips_align` other than `end` on a zone with no chips, a graph with neither edges nor zones, a node with neither an edge nor a zone (a zone may span empty cells, it just has to group at least one node). A refusal names an unlabelled zone by its place (`zones.0`).
`logo_file_id`, on a node or a zone, is a regular `*_file_id` reference, so the generic walks verify
it against the caller (an invented id is refused on save in the same words for both), serve it to the export, and embed it in a package. The
frontend (`blocks/architecture-layout.ts` + `blocks/Architecture.tsx`) lays the
grid out deterministically on the fixed canvas (rows sized by content, a zone's
title band reserved above the rows it heads, a circle node measured for its
own text, a label token still wider than its box at the minimum font size broken into runs that fit) and routes the edges: a tie to or from a `core` node is a
straight line when no other node sits on the chord (a blocked chord falls back to the elbows) - it meets a rectangular node on the CENTRE of the side facing the other end (the top edge centre of a card under the core, so a fan of three ties reads symmetric) and leaves a circle radially toward that point; everything else orthogonal elbows whose
candidate routes are scored (a corridor through a non-endpoint node costs
most, then a detour outside the grid, then a crossing with an already-routed
edge or a run along a straight tie, then a side another pair's edge already
uses, then bends, then length) and assigned parallel lanes — an exchange in both directions between the
same two sides runs as two lanes 44px apart that never cross, and a tie's rectangular end takes a lane like any other anchor on that side. Two nodes that share neither a row nor a column have, beyond the two plain elbows, the free corridors BETWEEN them - the row gaps and the column gaps the grid leaves (`vhv` / `hvh`) - so a diagonal pair boxed in by its neighbours is still drawn from its declared source through no third node instead of through the one in the way. Edge labels sit
ON the arrow (a pill knocked out of the line) at the middle of the longest
segment that holds them, slid along the path (every segment, then the other
segments) when they would meet a node, a zone title or an earlier label - two
edges sharing a segment put their labels at different stops of it - and only
then moved beside the line and further out, step by step, until clear: a node
or another label is a hard constraint, never a least-bad pick; a
label is kept off a zone's dashed border and inside the grid when a spot there
exists. Everything is drawn as
inline SVG through React — `<rect>` / `<circle>` / `<path>` / `<text>` and
`<image>` for logos — from the deck's own tokens and fonts, so the export's
layout audit reads the glyphs, the PDF stays vector and the "structured JSON,
never HTML / SVG" contract holds: `test_frontend_architecture_block_draws_through_react_only`
fails the build on `foreignObject`, `innerHTML` or `mermaid` in the renderer. The router's guarantees (no edge through a third node, every path starting on its source and ending on its target, no two edges crossing, lanes 44px apart, labels on the canvas and off nodes and each other, a legend wrapped inside the drawing, determinism) are pinned per fixture in `frontend/tests_unit/architecture-layout.test.ts`, which runs without a browser; the 3-row "hub / core / three products" picture at 3 and at 5 columns is a vitest snapshot (`blocks/architecture-layout.test.tsx`).

**Zone logos and frameless nodes.** A zone's `logo_file_id` is the
platform's mark drawn where the zone icon would be - the top-left corner of
the panel, inside its padding, centred on the title row - as tall as that row
leaves it (`ARCH.ZONE_LOGO_H`, 40px, less on a compacted drawing) and as wide
as the mark's ratio makes it; the label, optional with a logo, follows the
mark on the same row, 12px after it, and the panel, its tint and its colour do
not change. The ratio is the mark's, read before any byte of the file arrives:
`logo_file_ids` names the architecture's node and zone logos, so their stored
geometry is read on save and served as `asset_meta`, which the
renderer hands to `layoutArchitecture(block, { room, assets })`; the zone
logo is drawn cropped to its trim box (`SvgLogo` with a `crop`: the file at
its stored size inside a nested `<svg>` whose viewBox is the mark), SVG and
raster alike, and without stored geometry it takes a wordmark's ratio and is
drawn whole. Edge labels keep off the whole head (`zoneHeadBox`: icon or logo,
then label). A node with `frame: false` keeps the box a framed one has - the
grid, the row heights and the corridors the router scores do not move, and
other edges and labels keep away from it - but draws its content alone (logo
or icon with its tile, label, sublabel, chips): no shape, no surface, no glow,
for every shape and emphasis (a `core` keeps its size, a `ghost` its dimming).
`LayoutNode.content` is the box of what the node paints (`mediaRect` - the
tile, the bare icon or the logo as its file's ratio fits it - the text as
`SvgText` draws it, the chips) and `anchor` is where its edges meet it: the
node's box, or for a frameless node that content box, a plain rectangle
whatever the shape, so an arrow ends on what is drawn rather than on a border
nobody sees. The renderer publishes `data-frame="false"` and puts a
transparent `.deck-arch-node-hit` around the content, which takes the pointer
and draws a dashed ring on keyboard focus only; a video figure never breathes
around such a node (`FIGURE_PARTS.architecture.unframed`). Both deck tools
quote `ARCHITECTURE_BARE_HINT`. Pinned by `blocks/architecture-bare.test.tsx`
(the zone head alone and with a label, compacted, without geometry; every
shape x emphasis frameless in both schemes, its box unchanged and its edges on
its content; the 5-column fixture), the `frameless nodes in a zone headed by
its logo` case of `tests_unit/architecture-layout.test.ts`, and
`tests/test_presentation_architecture_bare.py` (the contract, the save-time
refusal, the agent's words, and in Chromium the renderer's own drawing -
`tests/data/architecture/zone-logo-frameless.html` and its geometry - through
the layout check and the editable PPTX).

**Zone chips.** A zone's `chips` (0-6, markup of at most 24
characters each) are the capabilities of the platform it stands for, drawn as
ONE row of pills on its title row, centred on the logo / label row, as tall as
a node's chips (`ARCH.CHIP_H`) and in the same `.deck-arch-chip` style - the
pills sit in the zone's `<g>`, so they read its `--tone`: the zone's tone or
palette colour, tinted. `chips_align` places the row: `end` (the default) in
the top-right corner inside the padding, `start` 16px after the logo / label,
`center` on the zone's middle; a zone may be headed by its chips alone. The
row takes no grid cell - the title band it sits in is the one every zone
reserves above its first row - so the nodes, the logo, the label and the
panel are where they are without it. It is never wrapped and never cut:
`layoutZoneChips` tries it at a node chip's size, then one step down (15px
type, 26px pills), and a row that still meets the logo / label or runs past
the padding is drawn whole where its alignment puts it, `LayoutZone.chipsOverflow`
saying by how many canvas px. The renderer publishes it as
`data-chips-overflow` on the zone and the layout check reports one
`zone_chips` finding per such zone, named as a refusal names it
(`architecture 1: the chips of zones.0 ('Platform') need 42px more...`). The
row is an obstacle for the router and the label placer (`Bounds.chipRows`): a
corridor through it costs what a corridor through a node costs, an auto
straight tie through it falls back to the elbows, and an edge label never
sits on it. In the editable PPTX the drawing stays one picture, but each chip
is a rounded rectangle and a text box at the page's position, one group per
zone and one per chip (`emitArchitecture` in `EXTRACT_PRIMITIVES_JS`): the
chips carry `data-export-lifted` and the picture `lifted: true`, which the
capture loop answers with `_capture_lifted` (`data-export-lifting` on the
picture for the one shot, the stylesheet hiding the lifted pieces), so a chip
is never drawn twice; a chip's translucent tint is composited over the zone's
panel before the writer blends it against the slide. Pinned by
`blocks/architecture-zone-chips.test.tsx` (every head - chips alone, with a
logo, with a logo and a label - each alignment in both schemes, every tone and
`brand_1`, the step down, an overflow drawn whole and published, a compacted
drawing, the router around the row, the 5-column grid's edges unchanged by the
chips), the `a zone headed by its logo and a row of chips` case of
`tests_unit/architecture-layout.test.ts`, and
`tests/test_presentation_architecture_zone_chips.py` (the contract, the
agent's words, the finding's words, and in Chromium `tests/data/architecture/zone-chips.html`
in both schemes and every tone, the layout check silent on it and naming the
zone of `zone-chips-overflow.html`, and the editable PPTX within 2px at
1920x1080, the pills' fill the page's and the picture under them chip-free).

**Edge sides, lanes and zone ends.** An edge's `source_side` /
`target_side` pin the side it leaves and enters. The side is honoured, never
corrected, even when it faces away from the other end: such an edge, and any
edge with a zone end, is routed through `via` candidates (`viaCandidatesFor` in
`architecture-layout.ts`) - a route spelled as the corridors it runs along, out
of the source along its side's normal and into the target against its side's
normal: an elbow or a straight / Z run when the two sides allow it, else one
detour longer, out past the source, along any free row or column gap or just
outside either end, in past the target. `viaFits` keeps a route that never
doubles back and whose first and last runs are 16px at least; the scorer counts
a run back through the edge's own node as a conflict, and a `via` corridor may
run to 4px from the canvas edge (the margin past the outer column is the only
way round to a side facing out of the grid; the off-grid cost still keeps it
there only when nothing else fits). A pinned side turns an `auto` tie to a core
into elbows; an explicit `route: "straight"` stays a chord between the pinned
sides. Unpinned edges between two nodes keep the legacy candidates, so a
drawing without the new fields routes as before. **Aligned pairs**
(`alignPairs`): the edges between one pair of nodes that run between two
FACING sides (a right side to the left side of a node beside it, a bottom to
the top of a node under it) take one offset each, the SAME on both nodes, in
`lane` order and then in the order they sorted to on the side of the node
whose id sorts first, spread symmetrically - the -22 / +22 of an exchange -
whatever else meets either side; the other anchors of those sides line up past
them 44px apart (`spreadAnchors`), and a straight or `via` route of the pair
runs from the line both nodes share on the cross axis, so it is exactly level
even between anchors whose centres differ. A lone edge with a `lane` is
aligned too; without lanes or a partner, a side spreads as before. **Zone
ends**: `zone:<ref>` is resolved by id, else by 0-based index, else by label
as written or as plain text (`resolve_zone_ref` in `presentation_spec.py`,
mirrored by `resolveZoneRef`). The zone's border is met as a flat end
(`zoneEnd`) on the side facing the node (or the pinned one), at the point of
that side nearest the node's anchor centre, 40px off the panel's rounded
corners; the arrowhead sits on the border. The route never crosses the panel it
ends on (a conflict for the scorer), a zone end follows its node's lane offset
when the two face each other, so a vertical arrow stays vertical, and the end
takes no grid cell - the zone's first row is the nodes' row, the invisible
anchor node and its blank band are gone. The editable PPTX carries the drawing
as its picture, so it follows the same routes. Both deck tools quote
`ARCHITECTURE_EDGE_HINT`. Pinned by `blocks/architecture-edge-sides.test.tsx`
(every pair of pinned sides between two nodes, honoured, orthogonal and through
no node; a single pinned side; a pinned core tie; lanes 1 / 2 and swapped, the
default -d / +d pair straight, orthogonal and auto with an extra anchor on one
side, a lone laned edge, a vertical pair; zone ends from above, beside, pinned,
as a source, in an exchange, never through the panel; the fixture's two squared
loops mirrored about the core, its level pair with centred labels, its zone
arrow, nothing overlapping, the same in both schemes), the `pinned sides, a
pair on lanes and a zone end` case of `tests_unit/architecture-layout.test.ts`
(its source / target invariant reads a zone end on the zone's border), and
`tests/test_presentation_architecture_edge_sides.py` (the contract and every
refusal, the agent's words, the frontend mirror, and in Chromium
`tests/data/architecture/edge-sides.html` in both schemes - every route where
the layout put it within 2px at 1920x1080, the level pair 44px apart, the zone
arrow's head painted on the border - the layout check silent on it, and the
editable PPTX one picture at the drawing's box whose pixels along every route
are the page's).

**Sizing.** The drawing is NEVER magnified: a row is as tall as its
content, whatever the column count. The stage is the drawing's own box - as
wide as the column up to the drawing's canvas width (`--deck-architecture-w`),
as tall as that makes it at the drawing's natural ratio
(`--deck-architecture-ratio`, width over the UNCOMPACTED height), never taller
than the size bucket (`sm` / `md` / `lg` = 380 / 600 / 800px); in `fill` and
`size: full` the flex column hands the stage its height, as it does a `fill`
chart's canvas (a column or a `fill` callout holding one stretches for it, the
same `:has()` rules as the chart's, and the stage keeps the chart's 160px
floor). The `<svg>` is absolutely positioned inside the stage and meets it, so
the drawing's own size never feeds back into the box it is measured from -
which is what let a 3-column diagram in `fill` grow to 1.6x the body's width
and then grow further as `fit.ts` widened the box to probe it (186% overflow,
the body at the 35% floor with 314px cut off). The renderer reads the stage
back with `useMeasuredBox` (`measured-box.ts`, the chart's hook) and hands it
to `layoutArchitecture(block, { room })`, whose fit pass compacts the
VERTICAL metrics - row gap, card padding, minimum heights, circle diameters,
zone head slack, legend gap, never the type - to the largest density in
`[DENSITY_MIN, 1]` whose layout fits the room (bisected on the monotone
height; `density` and `naturalHeight` on the layout, `data-density` on the
figure), so a drawing a little too tall for its bucket closes its rows up
before the slide's shrink-to-fit scales anything; past the floor it is handed
over as it stands and the drawing meets the box. A room narrower than the
drawing shrinks it, which is that much more height for the rows, so a diagram
in a narrow column is not compacted for a height it has.
Live decks are interactive — hover or focus a node to light its neighbourhood
and dim the rest, click / Enter to pin it and read its `description` in a
detail card placed from the node's measured position (correct at any scale
and in letterboxed `fill` mode), Escape to release; Space / Enter on a node
never advance the deck. Editable PPTX exports the diagram as one image (the
primitive extractor does not read inside an `<svg>` yet): the zone logo is in
it where the page drew it and a frameless node is its content with no shape
around it, and nothing of the drawing but a zone's chips (above) is
written as a shape or a text box of its own (`test_the_editable_pptx_carries_the_drawing_where_the_page_painted_it_with_no_stray_shape`
compares the picture's box and pixels with the page, to 2px at 1920x1080).

**Palette colours on a node or a zone.** A `tone` is a MEANING
(success, warning...), and a platform figure needs something no tone means:
each product box in its product's colour. `ArchitectureNode.color` and
`ArchitectureZone.color` take a `PaletteColor` - a `theme_config` key of the
deck's template, `brand_1` .. `brand_4` (`BRAND_COLOR_THEME_CONFIG_KEYS`, the
brand hues a template declares) or `chart_1` .. `chart_6` - and replace the
tone wherever it is read: the border wears the resolved colour whole, the body
a tint of it (`--deck-color-tint`, 18% on a dark slide root and 10% on a light
one, declared in the two scheme token blocks), and the icon, its tile, the
chips inside a node and a `core` node's glow follow because `data-color`
points `--tone` at the palette variable (`[data-color]` rules
next to the `[data-tone]` ones in `presentation.css`; the renderer publishes
`data-color` INSTEAD of `data-tone`). Anything else is refused by the Literal,
naming the allowed values; `PALETTE_COLORS` is pinned to the two key tuples by
an import-time contract in `presentations.py`, so every colour the validator
accepts is one the renderer resolves. The brand hues are brand identity, not
scheme-bound (`SCHEME_BOUND_THEME_KEYS` leaves them out): they carry to either
scheme and a `theme_variants` palette may re-tune them. A palette that names
none falls back to its chart series of the same rank (`--deck-brand-N:
var(--deck-chart-N)`, re-declared per scheme). The seeded Filigran Brand Deck
declares the product colours - `brand_1` XTM One purple (the product's own
accent, `#5e1ad5` on paper), `brand_2` OpenCTI blue (`#0fbcff`, `#0079a8`
on paper), `brand_3` OpenAEV orange (`#f2933a`, `#e6700f` on paper), `brand_4`
OpenCRQ blue (`#7587ff`, Filigran blue `#0015a8` on paper) - and the XTM One
Brand Deck the same four for its dark palette. An absent `color` renders
byte-identical to before. The PPTX carries the same colours by construction:
both flavours capture the drawing from the static page, which publishes the
same slide-root palette as the screen (`tests_e2e/presentation-architecture-colors.spec.ts`
reads the colours on screen in both schemes and finds them in the pictures of
the exported PPTX).

**Org charts** (`org_chart`). Reporting lines, teams and headcount are a
TREE of seats, never a grid of boxes, and the agent is told so in both deck
tools ("reporting lines, teams and headcount are an org_chart, never an
architecture block, stacked pills or a drawn picture", `ORG_CHART_HINT`,
quoted by `format_org_chart_guidance()` with its two validated examples, a
leadership team and a headcount plan). The contract (`OrgChartBlock`,
`OrgChartNode`, `OrgChartLegendItem`): up to `MAX_ORG_CHART_NODES` (120)
seats, each `{id, kind, manager?, dotted_to?, name?, role?, photo_file_id?,
icon?, chips? (<= 3), tone?, color?, count?, count_label?, period?,
location?, note?, collapsed?, emphasis?}`. A KIND has one meaning and one
card deck-wide: `person` (a name is required, no count), `vacant` (open today:
a role, no name, `period` its target date), `planned` (a future hire: a role,
`period`, `count` > 1 drawn as one card, "+N" before the role), `group` (a team as one
card: `role` the team, `count`, `name` its leader), `bubble` (a team as a
circle whose AREA is its `count`, required), `external` (contractor, partner,
agency). The block takes `orientation` top_down / left_right / radial, `style`
cards / compact / bubbles / hybrid (people as cards over their teams as
bubbles), `depth_limit` and `collapse_below` (1-based levels, the root as 1),
`expand`, `show_counts`, `count_mode` current / target / both, `group_by_branch`,
`legend` auto / none / custom (+ `legend_items`), `roots`, `size`, `fill`,
`label_size`, `photo_size`, `show_dotted`, `spine` auto / always / never and a
`caption`. It is a leaf block (columns, callout children) and a dashboard
panel (`PANEL_BLOCK_TYPES`). The validator refuses, naming the seat: a cycle
(`nodes a -> b -> a ...`), an unknown manager or dotted manager, a dotted line
to itself or to its own manager, a bubble without a count, several roots that
`roots` does not list (or a listed root with a manager), more seats than the
cap (before the seats are parsed, naming the first one past it), duplicate
ids, a kind holding what its card does not show (a name on an open seat, a
count on a person, a period on a filled seat), a `collapsed` leaf, an unknown
`expand` id, a custom legend without entries. `emphasis` unset is `core` on a
root (drawn larger, glowing) and normal elsewhere. `photo_file_id` is a
regular `*_file_id` reference (verified, served to the export, packaged).

ONE engine (`blocks/org-chart-layout.ts`, pure and deterministic, the
architecture's glyph table for text) lays it out for the viewer, the exports,
the layout check and the video: the roll-up of every subtree (`buildTree`,
`ownCount`: a person 1, an external seat its count, a team its count unless it
has reports - then its `count` is the DECLARED total `countMismatches` holds
to them - open and planned seats apart, `target` all of them); what is drawn
(`collapse_below` folds a manager's reports into one card, the manager's id + `~fold`, a
collapsed team absorbs them in its own count, `depth_limit` writes "+N below"
on the last level, `expand` and the seats the viewer opened keep them open);
then a tidy tree - Reingold-Tilford on the boxes each subtree draws (contours
as rects absolute on the level axis, so a subtree is pushed clear of a
neighbour at ANY depth), Walker's spreading of the small subtrees between two
large ones, the parent over the middle of its first and last child, one band
per level with every shape centred on one line, every CARD of a level as wide
and as tall as the widest and tallest (between `CARD_MIN_W` and `CARD_MAX_W`;
a name or a role on ONE line, cut at a word with an ellipsis, inside a word
only when one word alone is too wide - `broken`), one bus per parent (a stub,
one line along the middle of the gap, a drop per child). A bubble's diameter
is on the sqrt scale between `BUBBLE_MIN_D` and `BUBBLE_MAX_D` (its number
never under `BUBBLE_NUMBER_MIN_FS`), its label inside when it fits and under it
otherwise, counted in its footprint so it never meets a neighbour. `spine`:
when the drawing is wider than its room (`auto`) the leaves of every parent
whose reports are all leaf CARDS stack into one column under one rail (a row
of bubbles stays a row). `radial`: the root as a card in the middle, each ring
as far out as its widest seat's arc needs, every label upright beside its
circle on the ring's clockwise tangent, spokes from edge to edge. Dotted lines
are routed LAST: an orthogonal shortest path (a heap-backed Dijkstra over the
lines beside every seat, a bend worth 40px) that never enters a seat's
footprint; on a wheel, a curve bowed to whichever side is clear. The fit: the
room is the column's width and the stage's height (the bucket, or what `fill`
/ `full` / a dashboard panel hands it); the drawing is never magnified, and a
drawing whose names, roles or numbers would be seen under `ORG.MIN_TYPE_PX`
(12px) at the scale the room allows is REFUSED (`refused`): the slide shows a
notice instead of unreadable type, and the layout check says why. `facts` is
the engine's account of its own picture (seats, drawn, folded, scale,
`minType`, `wide`, `broken`, `unequal`, `crossings`, `overlaps`, `counts`,
`undated`).

`blocks/OrgChart.tsx` draws it as inline SVG through React only (pinned by
`test_the_org_chart_is_drawn_through_react_only_by_one_pure_engine`), the
architecture's sizing contract (the stage is the drawing's box, the `<svg>`
absolutely positioned inside it; `--deck-org-w`, `--deck-org-ratio`,
`--deck-org-h` per size bucket), the system words through the viewer's i18n
("Open role", "+3 open", "3 people") and the counts in its locale. Live, a
seat hovered or focused shows its detail card (full name and role, kind,
location and period, every chip, the note, its dotted managers, its team's
headcount) and dims what is not its line (its managers up to the root, its
reports, its dotted managers); a folded seat opens in place on click / Enter /
Space (the viewer's state, `opened` - the spec is unchanged); the arrow keys
walk the tree (`orgNeighbours`: the manager, the first report, the neighbours
on the level), Home goes to the root; `prefers-reduced-motion` drops the
transitions. Each kind's card is the stylesheet's (`.deck-org-*` in
`presentation.css`: the tone wash, the dashed open seat, the dotted external
seat, the dashed fold card, the core glow).

What the other readers find on the drawing: `data-deck-org-chart` and every
seat's `data-org-node` (the PPTX extractor), `data-points` on every line (the
engine's polylines), `data-org-facts` and `data-max-w` on every line of text
(the layout check), `data-level` on seats and buses and `data-count-to` on
every number (the video). The **editable PPTX** writes the chart as the shapes
it is drawn with, never as a picture (`emitOrgChart` in
`EXTRACT_PRIMITIVES_JS`, `_add_oval` / `_add_connector` / `_subgroup_runs` in
`pptx_writer.py`): a card a rounded rectangle, a circle an oval of its size,
an open seat dashed and an external one dotted (`dash`), every word a text box
in its font and size, every line straight connectors along the engine's
polylines, each seat a group inside the chart's group; a photo or an icon is
a picture captured alone on a transparent page. The PDF is the vector page.
The **layout check** reads one `org_chart` observation per chart - the facts
plus what the page PAINTED (a line through a seat it does not join, one
manager's cards at different heights, glyphs past `data-max-w`, a circle's
label over a seat) - into `org_scale` (overloaded), `org_width`,
`org_label`, `org_siblings`, `org_crossing`, `org_bubble`, `org_counts`
(dense) and the advisories `org_crowded` (more than
`ORG_CHART_READABLE_NODES` seats drawn with nothing folded) and
`org_undated` (a level of planned seats none of which says when). An
`architecture` block drawn as a reporting tree (one node spanning the top
row, every other node one row below, every tie `arrows: none`,
`architecture_reads_as_org_chart`) still validates; the deck tools' result
carries a hint naming the slide (`org_chart_hint_note`, narrowed to the
slides an edit wrote) and the layout check an `org_hint` advisory. In a
**video figure** (`FIGURE_PARTS.org_chart`, `levels` / `late` / `grow` in
`figure-build.ts`) it is built a level at a time - the root, each level's
cards, then its bubbles growing to their size, each bus with the level it
reaches, the dotted lines on a last step of their own - every `data-count-to`
number counting up (`countAt`), the camera following as for an architecture.
Pinned by `blocks/org-chart-layout.test.tsx` (the engine on balanced,
unbalanced, single-child and forty-seat trees, hybrid bubbles, radial, left to
right, folding and opening, the spine, the bubble scale, the roll-up, the
facts, keyboard navigation; the renderer in every orientation and style, both
schemes; the fixture the backend tests draw), `figure-build.test.ts` (the
level order, the count-up) and `tests/test_presentation_org_chart.py` (the
contract, the guidance and the skill, the hint, every finding, the PPTX
writer, and in Chromium the renderer's own drawing - `tests/data/org_chart` -
through the extractor and the audit).

**Bounds** (`MAX_*`): 12 blocks per slide, 8 per column, 6 per
container, 4 columns, 12 list items, 8 grid items, 14x6 tables, 4 roadmap
phases, 6 takeaways, 10 badges, 10 key-value rows, 4 tiers of 8 features, 4
personas of 4 points, 16 logos... Generous enough for a real deck, small enough that a
runaway generation cannot produce an unusable artifact or an oversized JSON
column. `presentation_guidance.soft_budgets()` quotes these caps but advertises
tighter practical budgets.

**The slide cap is the template's** (a flat 60 before). How long a deck
may run is a property of the kind of deck a template is for - a board pack or
a QBR that splits its dense dashboards as the layout rules ask outgrows a pitch
deck's budget - so `extra_metadata.max_slides` on the template says it, read by
`presentations.template_slide_cap()` into `TemplateFacts.slide_cap` /
`TemplatePalette.slide_cap` (the one visibility-checked loader, like the
schemes). `presentation_spec.DEFAULT_SLIDE_CAP` (100) holds for a free deck
and for a template that sets none; `MAX_SLIDE_CAP` (300) is the ceiling no
template may raise - the model's `max_length`, past which the export's render
budget stops being credible. The seeded Filigran Brand Deck ships with
`max_slides: 150`; the XTM One deck takes the default. The write surface
(`template_containers.validate_slide_cap`: the REST create / update / import,
`create_template` / `update_template`, `coerce_metadata_to_kind`) stores a
whole number from 1 to the ceiling, drops the key on `null` (the default) and
on a non-deck kind, and refuses anything the reader would clamp or ignore.
Enforcement: `validate_spec(payload, slide_cap=...)` refuses one slide over
with a field problem on `slides` that names the cap and how it is raised
(`slide_cap_hint`); `save_presentation(..., slide_cap=)` is held to the linked
template's cap on the authoring paths (the agent's rewrite, the editor's
PATCH - which reads the template only when the save grows the deck) and to
the ceiling on the copies (duplicate, restore, package import: content already
accepted). A deck already over its cap (its template tightened or swapped)
stays saveable at its size and cannot grow (`effective_slide_cap`).
`apply_operations(..., slide_cap=)` makes `insert_slide` refuse at the cap
with the deck's size, the cap and the hint, and `edit_presentation` reads the
template's facts BEFORE the operations run whenever one of them is an
`insert_slide`, sharing the load with the theme rules. The deck response
serves `max_slides` (the palette's `slide_cap`; the default for an invisible
template, as its palette reads as none), and the editor's `Add slide` /
Duplicate stop there (`deckSlideCap()`, `canAddSlide(draft, cap)`), falling
back to the TS `DEFAULT_SLIDE_CAP` (pinned to the Python one by the tripwire)
on a response that predates the field. `search_templates` names the cap of a
template that sets one; the `generate_presentation` template question
(`format_template_choice`) does too.

`layout_catalog()` / `block_catalog()` are derived from the models and drive the
seeded template `layouts`, the `validate_layouts()` template guardrail and the
frontend preview; `spec_reference()` renders the whole model tree as text for
the tool description.

### Retired layouts

The v1 layouts — `two_columns`, `comparison`, `dashboard`, `generic` — were
kept next to the block-based v2 "so every stored deck keeps validating". That
kept four Pydantic models, four React components, a raw HTML field
(`rich_text`) with its own sanitizer and a "(v1)" label on every surface that
lists layouts, and it left the platform with two answers to "which layouts
exist": the brand deck advertised eleven, the editor's picker offered seven.
The promise to stored decks is now kept by CONVERSION instead
(`app/services/presentation_legacy.py`): a v1 slide is rewritten as the v2
slide that says the same thing, deterministically, so nothing downstream ever
meets a v1 slide again.

**Two boundaries, nowhere else.** On READ, `Presentation.spec` and
`PresentationVersion.spec` are `SpecJSON` columns (`app/models/presentation.py`,
a `TypeDecorator` over `JSON` whose `process_result_value` runs `upgrade_spec`),
so the viewer, the studio prompt, the outline in a tool result, the exporter, a
package and a version preview all see v2 without touching one of the ~25
places that read the column; the column has no bind hook — the WRITE boundary
is `validate_spec`, which runs `upgrade_spec` before validating, so a stored
deck re-saved through any path (theme change, patch, restore, import,
duplicate) is written back as v2, an agent that still emits a v1 layout from an
old instruction gets the upgraded slide, and a v1-era package still imports.
`upgrade_spec` is copy-on-write: a deck with no retired slide comes back as the
same object, so the read hook costs one scan of the layouts on the common path,
and a read never raises (anything not deck-shaped passes through untouched).
There is deliberately NO data migration — the rules forbid an unbounded
in-transaction backfill, the upgrader is needed at the boundaries anyway, and a
row is rewritten on its next intentional save, the same shape as
`LEGACY_THEME_ALIASES` reading the v1 `filigran` theme as `dark`.

**What travels, what does not.** Every word travels; the `id`, the speaker
`notes` and the background image travel unchanged, so an anchored comment, a
pasted slide link and the React key keep working. The mapping: `two_columns`
-> `content` with two `columns` (per side `heading` -> a level-3 `heading`,
`bullets` -> a `list`, `rich_text` -> `text` blocks, `image_file_id` -> an
`image`; one empty side -> a single-flow body); `comparison` -> `content` with
a `comparison` block when both sides carry a label and points,
`section_header` + `list` blocks otherwise, the `verdict` as a closing `text`;
the v1 `dashboard` -> `content` with a `metrics_grid` plus the `rich_text`
words (the id belongs to a v2 layout again, so `is_retired_slide`
recognises the v1 slide by its SHAPE — no `tiles`, no `panels` — while
`RETIRED_LAYOUTS` holds the three ids that are gone for good);
`generic` -> `content` with `list` / `text` / `image` blocks. A slide that
carried nothing but a title becomes a `section` divider; one that carried
nothing at all (v1 allowed it) becomes a `section` titled with the deck's
title, then `FALLBACK_SECTION_TITLE` — the one invented word in the whole
upgrade. The ONE thing that does not survive is the formatting of a
`rich_text` fragment: v2 has no HTML field, so its words (read by a real
`HTMLParser`, never a tag-stripping regex — an attribute value stays an
attribute value, `script` / `style` / embeds are dropped whole) become plain
`text` blocks packed line by line under the v2 caps, and the note says so.

**The agent is told, once, when it matters.** `retired_layout_note(spec)`
(`UpgradeReport.note()`) rides the `generate_presentation` result and the
`edit_presentation` account whenever a call carried a retired layout: which
positions were upgraded from which layouts, and how many fragments lost their
formatting. Nothing about the retired ids lives in a system prompt, the seeded
skill or the persona — naming them there, even as "do not use", is the prompt
inflation the invariants ban; the tool result is where the fact belongs.

**Pinned.** `tests/test_presentations.py` (every v1 shape upgraded with its
words, the read-through upgrade, the generation note, the `SpecJSON` contract
with `process_bind_param` deliberately not overridden, no field in the model
tree an HTML sink) and `tests/test_presentation_spec_v2.py` (`SLIDE_LAYOUTS`
is the seven ids, `spec_reference` lists no retired layout, `layouts/legacy.tsx`
gone, `layout-options.ts` one list) fail the build if a v1 model, `rich_text`,
a v1 renderer or a frontend `LEGACY_LAYOUTS` / `CURRENT_LAYOUTS` split comes
back; `tests/test_presentation_guidance.py` fails it if the skill or the
persona names a retired id again.

### Per-slide themes and hero backdrops

The Filigran PowerPoint opens and closes on a dark, gradient-washed cover with
the brand mark as a watermark ("en filigrane") over a light body. A deck had
one scheme for every slide and one flat look for its covers; both
axes are added without touching a stored deck.

**A slide may name its own scheme.** `_SlideBase.theme: SlideTheme | None`
(`"dark"` / `"light"`, normalised like the deck theme, absent = inherit). The
precedence rule, in one place per side: `resolved_slide_scheme(spec, slide)`
(backend) / `resolveSlideScheme(presentation, slide)` (frontend, `theme.ts`) -
`slide.theme` wins over `spec.theme` whenever it is set. A deck theme change
(`update_deck`, the Design panel, a template snap) reaches ONLY the slides that
inherit; an overridden slide is not touched, not rewritten, not normalised -
`update_deck {theme}` leaves it byte-identical, and the tool result says how
far the change went (`deck_theme_change_note`: "3 slides inherited the
change; 2 kept their own theme ... slides 1 (dark), 5 (light)"). The override
is removed only by an explicit `update_slide {theme: null}` or the editor's
**Inherit**. The outline marks an overridden slide (`[theme: dark]`), so an
agent never promises a light deck the cover will not follow.

**Held to the template like the deck theme.** `unsupported_slide_themes` /
`fit_slide_themes_to_schemes` / `unsupported_slide_theme_message` /
`slide_theme_snap_note` (`presentations.py`) are the slide-level twins of
`theme_fits_schemes` / `fit_theme_to_schemes` / `unsupported_theme_message`:
SETTING a slide theme the deck's template cannot dress is refused with the
slide and the field (`edit_presentation`, REST `PATCH` 422 - "Slide 3 theme
'light': not supported by the template ..."); a TEMPLATE CHANGE that cannot
dress an overridden slide snaps that slide to the template's scheme the way it
snaps the deck, counts it as changed (so `verify_untouched` knows it moved) and
names it in the result; `generate_presentation` snaps with a note like it does
the deck. Under the deck's EXISTING template only what a write INTRODUCES is
refused (`introduced_slide_themes`, shared by the tool and the PATCH): an older
deck may carry an override the template stopped supporting after the save, and
a title edit or a move of that slide is not a request for the scheme - the
comparison is by the slide's stable id, never its position (a reorder moves the
slide, a new id-less slide at the same position is new; a row older than slide
ids is compared by position). The panel mirrors both: the slide Theme picker
greys out an unsupported scheme, and a TRIED template snaps the overridden
slides in the draft with a status line - the snap effect is gated on
`templateChanged` and keyed on the pick and its schemes (never on the slides,
which re-snapped what an undo restored; never on the schemes alone, which
skipped a relink between two dark-only templates the save snaps against), so
opening a deck whose template stopped supporting a stored override, or picking
the stored template back, dirties nothing. `tests_e2e/presentation-slide-theme.spec.ts`
drives the pickers on a light deck with real clicks and reads the slide root's
`data-scheme` / `data-scheme-override`, the backdrop and its `data-hero-tone`,
the accent span, the stored spec and the fresh render after a reload.

**Rendering: the tokens move to the slide root.** Every slide root - the reveal
`<section>` (`DeckSlide`) and the static canvas (`StaticSlide`) - publishes
`data-scheme` (keying the stylesheet's token block, declared for
`.deck-root [data-scheme="dark"]` / `[data-scheme="light"]` next to the deck
root's) and lays the template palette for THAT scheme inline
(`slideRootAttributes`); the deck root keeps the deck default. A slide that
inherits declares the deck's own values again - the pixels it always had; a
slide that overrides (`data-scheme-override`) paints its own paper on the
section and carries its background image itself (reveal's background layer
would be hidden under the paper), so a light cover on a dark deck slides in as
one sheet under every transition and never flashes the deck's paper. The chrome
draws the logo variant of the SLIDE's scheme (`logo_light` on a light slide);
charts, tones, tables and Mermaid read the slide root's tokens (the Mermaid
observer watches the slide root as well as the deck root); the in-place
editor's layer resolves the SELECTED slide's scheme so its tone swatches are
the slide's. `SlideSchemeContext` hands the resolution to the components drawn
inside the slide (the hero reads the mark asset off the slide's own palette).
The PPTX writer and the layout check measure the static page, so they see
exactly what the screen shows: per-slide background fill, run colours, logo
variant and chart colours - nothing is resolved a second time.

**Heroes.** `TitleSlide.hero` / `ClosingSlide.hero`: `plain` (the flat paper
every deck had, byte-identical), `gradient` (the brand gradient washed
diagonally over the paper, `--deck-gradient-from` top-left to
`--deck-gradient-to` bottom-right with a radial bloom, and the template's MARK
- its `logo_mark` asset, `mark_file_id` on the palette, the third file-id
theme key next to the two logos - as a faint watermark bottom right: the
PowerPoint cover), `split` (the text keeps the left 55%, the right 45% is a
gradient panel hosting `background_image_file_id` as a cover or a soft radial
glow; the slide root does NOT paint the image full-bleed then -
`heroHostsBackgroundImage`), `spotlight` (one large radial accent glow behind
the headline, two thin concentric arcs bottom right, inline SVG), `mosaic`
(square tiles in six colour slots with a fine grain, inline SVG).
`mosaicTiles` (`mosaic.ts`, pure and unit-tested) lays the tiles on a 60 px
grid of the deck's canvas (`DeckCanvasContext`, provided by both slide hosts
from the aspect ratio, so a 4:3 deck gets its own grid) from a seeded
generator: the seed is the slide id, so a slide keeps its composition on
every render, in the PDF and in the PPTX picture, and two covers of a deck
differ. The composition follows the slide's alignment - `start` clusters the
tiles top right and bottom right, `center` (a centred title, every closing)
in the four corners - and keeps out of the zones where the headline, the
chrome logo and the slide number sit. A slot's colour is
`--deck-hero-mosaic-1..6`: the hero tone's pair and four `color-mix` steps
between them, or, for the `brand` tone, the template's six chart series, so
a template's mosaic wears its own palette with no hero-specific key. The
backdrop element carries `data-export-backdrop` (one picture in the PPTX). On
the `brand` tone every tile is one of the chart series EXACTLY: a
template's colour set, never a shade of it - the tint rules are scoped
`:not([data-hero-tone="brand"])`. On a single-colour tone a tinted tile is
mixed towards `--deck-hero-mosaic-tint` - the ink on a dark slide, the paper
on a light one - so a tint is a lighter step on either scheme. Three more heroes are SHAPES of the same drawing
(`MOSAIC_HEROES` in `hero.ts` maps each tiled hero to a `mosaicTiles`
pattern; `mosaic` is the original `scatter`, its tiles and seed unchanged and
pinned by `mosaic.test.ts`): `cascade` (48 px tiles stepping down from the
top-right corner along a diagonal, a run along the top edge and down the
right one - the cover of the THREAD marketing kit), `horizon` (a ragged
skyline rising from the bottom edge, one smoothed random height per column)
and `pillar` (a ragged column down the right edge; both edges on a centred
cover). `cascade` and `horizon` also lay the cover out: they HOST the brand
logo (`heroHostsLogo`, `LOGO_HOSTING_HEROES`) - `CoverLogo` in
`layouts/opening.tsx` draws the slide scheme's logo variant (`logo_light_file_id`
on a light slide) large in the top-left corner, or on the axis of a centred
cover, under the deck's `chrome.show_logo` (`DeckShowsLogoContext`, provided by
both slide hosts), and `DeckChrome` leaves its own logo out on that slide. The
`cascade` title anchors its content low (`justify-content: flex-end`), draws
the eyebrow plain in `--deck-gradient-from` and lays `.deck-title-meta` - the
speakers and the footer line, a `display: contents` group on every other
cover - as one row under a rule of the same colour, the speakers left and the
footer right. Each shape keeps out of its own zones (the hosted logo, the low
headline of a cascade, the headline band of a horizon, the headline column of
a pillar), unit-tested per pattern and composition.

**The tiled covers are a template's own.** `plain`, `gradient`,
`split` and `spotlight` (`SHARED_HEROES`) dress every deck; `mosaic`,
`cascade`, `horizon` and `pillar` (`TEMPLATE_HEROES`) are the THREAD marketing
kit's artwork, so a deck wears one only under a template that declares it in
`extra_metadata.heroes` (`template_heroes` in `presentations.py`, mirrored by
`templateHeroes` in `hero.ts`; validated by
`template_containers.validate_template_heroes` on every write path, which
refuses a shared or unknown name and drops an empty list). The THREAD
Conference Deck declares all four; the Filigran and XTM One brand decks declare
none and open and close on `gradient`. A cover naming a hero its template does
not offer is drawn as the template's `hero_defaults` for that cover when it
offers it, else `gradient` (`cover_hero`): on read, in the spec a reader is
served (`served_hero_spec` from `serialize_presentation`, the public link and
`get_presentation`; the stored row is left alone, the next save writes what was
shown), and on a write by the slide-theme rule - a write that SETS one under
the deck's existing template is refused (`unsupported_slide_hero_message`,
compared by slide id through `introduced_slide_heroes`), a new deck or a
template change snaps it with a note (`fit_slide_heroes`,
`slide_hero_snap_note`). `hero_defaults` are read against the template's
covers (`template_deck_defaults`), the deck response serves
`supported_heroes`, and the inspector's `HeroPicker` lists only those; a
template tried in the editor snaps the draft's covers the same way
(`PresentationEditorPanel`). `get_template` / `search_templates` name a
template's own covers. Every
variant is scheme-agnostic: it reads the slide root's tokens, so "a dark hero
in a light deck" is `theme: "dark"` plus any hero on that slide, never a
variant of its own. `TitleSlide.hero_accent_word` draws the title's LAST LINE
(after the last line break; the last word of a one-line title) in the accent
tone with no markup in the value (`InlineMarkup accentLastLine`, a rendering
choice - the in-place editor still opens the plain title). Where that line or
word begins is decided by `splitAccentLine` (`accent-line.ts`, pure over the
tokenizer's segments, unit-tested): the last word is found ACROSS the runs
(`Take **Action**.` accents the bold run and the full stop), a run is cut at
the word boundary with its wrapper kept on both halves (`Launch **Take
Action.**` accents `Action.` in bold, never the whole run), a `code` run is a
chip and stays whole, and trailing line breaks are skipped then drawn plain
after the accent. `_SlideBase.overlay`
(`none` / `soft` / `strong`) is the scrim between a background image and the
content, the slide's paper colour at a soft or strong opacity, heavier at the
bottom; the `split` panel scrims its own picture. `HeroBackdrop.tsx` is the
first child of `.deck-title` / `.deck-closing` (which carry `data-hero`),
`SlideOverlay` the first child of the frame: both are absolutely positioned
against the slide root (the frame is not positioned, so `inset: 0` covers the
padding and escapes the clip) at a NEGATIVE z-index inside the frame's own
stacking context - `.deck-slide { isolation: isolate }` is what makes that
possible: above the canvas paper the context sits on, below every block
whatever its z-index. `presentation.css` implements the variants with deck
tokens only (per-scheme wash / glow / watermark opacity / scrim tokens in the
two token blocks; the arc line's opacity is the one literal, and a test greps
the hero rules for a hard-coded colour). `aria-hidden` keeps the backdrops out
of the layout audit's measurements, like the ghost section number.

The mark is a TEMPLATE asset, never a slide reference: it rides the palette
(`resolve_template_theme`, filled from the `logo_mark` role when `theme_config`
names no `mark_file_id`) and is fetched under the container's own three-tier
visibility, exactly like the chrome logo (`authorize_file_access`, the
template-asset path). A reader who may read the deck but not its template gets
no palette at all - no colours, no logo, no mark - so the `gradient` hero draws
its wash in the default tokens with no watermark: a silent degrade, never a 403
or a broken image, and the same one the logo has always had. A package import
remaps the palette's file ids (`THEME_FILE_ID_KEYS`, the mark with the two
logos) onto the imported rows the way it remaps `layouts` / `extra_metadata`,
so an explicit id never dangles after a round trip.

**Colour.** `hero_tone` (`HeroTone`, title and closing slides, default
`brand`) says which colour of the slide's palette the backdrop is drawn in. The
brand gradient of a template often runs from its primary into a SECONDARY colour
(the Filigran deck: `#42caff` into `#00f0bc`), and the gradient hero blooms in
the second stop, so a cover that must stay on the primary needed a choice. Every
backdrop is drawn from ONE pair of custom properties, `--deck-hero-from` /
`--deck-hero-to`, and `data-hero-tone` on the backdrop root picks what the pair
holds (`presentation.css`): the template's `gradient_from` -> `gradient_to` for
`brand`, one palette token for both otherwise - `accent`, `gradient_from`,
`gradient_to`, a tone (`info` / `success` / `warning` / `error`) or `muted`. The
value is a palette KEY, never a colour: the spec stays template-agnostic, a
relink recolours the covers with the rest of the deck, and the export needs no
code (the backdrop picture is captured as drawn). A test pins that the drawings
read the pair and never a palette token directly, or a tone would leave part of
a backdrop in the brand colours. The inspector's colour chips
(`HeroTonePicker`) are painted through the SAME mapping: they sit in a
`.deck-palette` carrier - a span declared in the two token blocks like
`.deck-edit-layer`, carrying `data-deck-theme` and the slide's inline palette
(`SlidePalette`, resolved by the panel's `paletteFor` through `slideScheme` and
the tried template's per-scheme tokens) - so a chip shows the exact colour the
slide draws, dark or light.

**Export.** The PDF is the static page's own CSS, per slide. The PPTX gets the
backdrop as a PICTURE and the text as editable runs in the slide's colours: the
element painting the wash (`data-export-backdrop`) is what the extractor
screenshots, and because an element screenshot is a crop of the page - the
headline drawn over the wash would be baked in and then written a second time
as runs - the capture loop puts the slide in its isolation state first
(`EXPORT_ISOLATE_ATTR` = `data-export-isolate`, answered by the stylesheet:
content and chrome `visibility: hidden`, the backdrop and everything inside it
visible), shoots, and restores it (`_capture_backdrop`; the extractor tags the
item `backdrop: true`). Verified by hand on a light deck wearing the seeded
Filigran Brand Deck: slide 1 with
`{theme: "dark", hero: "gradient"}` exports a `070D18` background, a full-slide
wash picture with the watermark and no text, white runs and the light logo;
slide 2 stays `F2F2F3`. A picture drawn INSIDE a backdrop is the backdrop's
(the tiled heroes paint nothing on their wrapper, the extractor reaches
their SVG): `el.closest("[data-export-backdrop]")` tags it, or the headline and
the speakers were baked into the tile picture and written twice.

**Pseudo-element decorations**. The walk visits elements, so a
decoration drawn by a `::before` / `::after` - a slide title's gradient rule,
a card's beam and bloom (`.deck-metric`, `.deck-card`, `.deck-phase`...), a
callout's bar, a list's bullet, an eyebrow's corner marks, a timeline's rail -
never reached the editable PPTX. `materializePseudos` (in
`EXTRACT_PRIMITIVES_JS`, per slide, before the walk) gives every
pseudo-element that PAINTS (an empty `content` with a fill, a gradient or a
border; a text `content` - a counter, a separator glyph - is not a shape and is
left alone) a STAND-IN: a span carrying its computed style, measured by a probe
that takes the pseudo-element's place while it is switched off
(`data-export-measure-*`, one layout per slide), then laid over it absolutely at
the slide root and hidden (`[data-export-pseudo]` in the stylesheet: the
pseudo-element keeps drawing the screen and the PDF, which are byte-identical).
The walk writes a host's stand-ins with the host - those painted under its
content (a `::before`, a negative z-index layer such as the card light) right
after the host's own box, the others after its content - a fill or a border as
a native shape, a gradient as a picture tagged `backdrop` + `alpha`. An alpha
capture (`_capture_backdrop(alpha=True)`) shows that one stand-in on a
transparent page (`data-export-capturing`, `html[data-export-alpha]`: no other
backdrop, no paper, no page background) and keeps the transparency
(`omit_background`), so the picture lays over the host's own fill. A host that
is itself a picture keeps its pseudo-elements in that picture.

**Pictures captured alone**. An element screenshot is a crop of the
page, and Playwright rounds the crop OUT to whole CSS pixels of the document
(`enclosingIntRect`). Two consequences, both handled in `emitImage`:

- Every `image` primitive takes the box its capture covers (`captureBox`: the
  element's box rounded out the same way), never the element's own
  fractional box - the writer used to squeeze the extra pixel into the box
  and moved the picture by up to a canvas pixel.
- A picture drawn edge to edge between two boxes carries `data-export-alone`
  (the `challenge_answer` connector, from one card's border to the next):
  the rounding would bake a row of the card's border into its crop, so it is
  tagged `backdrop` + `alpha` and captured like a stand-in - the stylesheet
  shows whatever element is being captured (`[data-export-capturing]`), on a
  transparent page.

Pinned in Chromium by
`test_a_challenge_connector_reaches_the_editable_pptx_alone_and_where_the_page_drew_it`.

**Editor.** The Selection tab has a **Theme** picker (Inherit / Dark / Light -
Inherit clears the field; an unsupported scheme is greyed out with the
reason), a **Hero** picker (four drawn thumbnails, title and closing slides)
with the **Hero colour** chips under it for a non-plain hero, an **Overlay**
picker (any slide with a background image) - the four pickers of
`SlideStylePickers.tsx` - and the `hero_accent_word` switch, an ordinary
`slide-schema.ts` field of the title slide (the tripwire allow-lists
`title.hero` / `title.hero_tone` / `closing.hero` / `closing.hero_tone` as
inspector-own controls and `theme` / `overlay` as common fields). A picker put
back on the model's default UNSETS the key and the pristine draft is read
through the same normal form (`normalizeSlideLook`, like `normalizeChrome`), so
a deck stored before or after the fields existed - the validator spells the
defaults out on every save - is never dirtied by a look it already has; the
same normaliser reads a slide the layout switch converted. Every slide-level choice
is reachable there without an agent; the Slides tab's rows deliberately stay
ONE line (thumbnail, title, layout picker, arrangement) - a row carrying the
look as compact selects was tried and removed as noise, the Selection tab is
one click away. Choosing the value a slide already renders with is a no-op
everywhere, so a deck stored before the fields existed is never dirtied by a
look. The Design panel's deck theme control says how many slides keep their
own theme. Fields that act on another FORM of the slide than the one
it has (a column ratio or equal height on a single flow of blocks, a spread
with one block, `stretch` on a single flow - which reads as `start`) say so
under their control (`field-applicability.ts`, an amber note in place of the
hint) instead of flipping to no visible effect; the value stays editable and
takes effect once the slide takes that form.

**Template defaults.** `extra_metadata.deck_defaults` gains three OBJECT keys
next to the string ones (`DECK_SLIDE_DEFAULT_KEYS`): `hero_defaults`
(`{"title": "gradient", "closing": "gradient"}`), `hero_tones`
(`{"title": "accent"}`) and `slide_themes` (`{"title": "dark", "closing":
"dark"}`), filled by `apply_deck_defaults` on the title / closing slides the
agent left unset - so a brand opens and closes dark on a light deck by
default; an explicit `hero: "plain"` or a stored slide's own value wins, a body
slide is never touched, an unknown layout or value is dropped. Filled on a
NEW deck only: a wholesale rewrite (`generate_presentation(presentation_id)`)
has no slide identity to read a stored cover from, so it strips the per-cover
keys and a re-emitted cover left unset stays plain and inheriting. The seeded
Filigran Brand Deck declares `hero_defaults` and `slide_themes` (its covers
are the PowerPoint's; the colour stays the brand gradient) while its deck
default `theme` stays `dark` - a new deck from it is dark throughout, and the
covers keep their `dark` on the light decks made from it; the XTM One deck
declares the hero defaults (dark only). The template preview
(`previewSpec.ts`, `applyCoverDefaults`) lays the same defaults on its title /
closing skeletons, so a brand previews on the cover its decks open with. See
`dev-docs/presentation-template-package/README.md`.

**Pinned.** `tests/test_presentation_spec_v2.py` (defaults leave a stored
spec's slides without a `theme` key and reading the deck's scheme; every unknown
value refused by slide and field; the catalogs mirrored by `hero.ts`; the
reference carries the fields with the precedence rule; the frontend renders
every variant, publishes `data-hero`, isolates for the export, and resolves
every slide root through the one resolver), `tests/test_presentation_patch.py`
(`update_deck {theme}` leaves overridden slides byte-identical and reports its
reach; `update_slide {theme: null}` re-inherits; `replace_text` never rewrites
a hero or an overlay; the template helpers; the tool refuses an unsupported
slide theme, marks the outline, snaps on a template change; the template's
slide defaults), `tests/test_presentations.py` (the REST `PATCH` holds a slide
theme to the template like the deck theme) and
`tests/test_presentation_export.py` (the backdrop capture contract).

## Rendering

**Fixed canvas.** Every deck is authored on a reference canvas — 1920x1080 for
16:9, 1440x1080 for 4:3 (`geometry.ts` / `DECK_SIZE`, `deckSize()`) — and reveal scales
that canvas to whatever box it is given (a chat card, a fullscreen element, a
projector, the headless export page). Every size in `presentation.css` is an
absolute pixel value on that canvas; that is what makes the on-screen deck, the
PDF and the PPTX pixel-identical — they all rasterize the same layout. Never
add a viewport-relative unit to the deck stylesheet.

**Tokens.** Only reveal's structural CSS is imported; the visual identity is
`presentation.css`, driven by `--deck-*` custom properties. `theme.ts`
(`THEME_VARIABLES`) maps every key of `presentations.THEME_CONFIG_KEYS` to a
variable (`test_frontend_maps_every_theme_key` fails when the two drift), and
the v2 additions — `surface`, `line`, the four semantic tones `info` /
`success` / `warning` / `error`, `gradient_from` / `gradient_to`, `mono_font` —
are what the block catalog reads through `data-tone`
(`test_frontend_stylesheet_has_a_rule_for_every_tone`). A block `tone` colours
the block as a whole: a heading's icon takes the heading's OWN tone (read off
the heading, so an untoned heading inside a toned card keeps the accent icon
rather than inheriting the card's) — it used to be pinned to the accent, which
left a `success` heading green with a blue icon. And because the
natural way to write "a green heading" is `[[success:...]]` around the whole
text, a heading, section header or list item whose entire text is ONE tone span
renders as if that tone were set on it (`soleSpanTone()` in `markup.ts`, at
render time, so stored decks are fixed without a re-save): the icon / marker
follows the text; an explicit `tone` always wins, a `gradient` span or a span
over part of the text stays a text decoration, and table cells are excluded
(`td[data-tone]` also bolds, a toned header emphasises its column)
(`test_frontend_toned_heading_colours_its_icon_too`). Fonts are self-hosted
(`@fontsource/geologica`, `@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-mono`),
so a deck renders identically offline and in the headless exporter, whose
network policy blocks every external request.

**The body-slide design language**. The blocks draw the Filigran
surfaces of the design system and filigran.io, not a generic card kit, and
the rules below hold for every deck whatever its template:

- *Hairlines, not slabs.* A card (metric, numbered card, phase, flow step,
  comparison side, pricing tier, persona, dashboard panel, `card` callout,
  framed key / value, table frame) is a `--deck-card-bg` surface drawn with a
  `--deck-hairline` (1px) border at `--deck-radius` (12px; `--deck-radius-sm`
  8px), no shadow on paper but the `card` callout's soft one.
  `--deck-border` (2px) stays the weight of the rules that SEPARATE (a
  section header, the table header's accent rule, a timeline ring).
- *The card light.* Every card of that family is lit by its tone (the
  card's `data-tone`, or its block's, the accent otherwise): a beam along the
  top edge fading to the right (`--deck-beam`, 3px; 0 on a dashboard panel
  and on a `card` callout that carries its bar) and, on the dark scheme
  only, a bloom rising from the bottom edge (`--deck-card-glow`, 14% dark,
  0% on paper) - the filigran.io card surface. One `::after` rule draws it
  for the whole family under the content (`isolation` on the card): the PPTX
  writer walks elements, never pseudo-elements, so the card exports as the
  plain editable shape it is, and the layout audit never measures it.
- *Tone on the figure.* A KPI value is drawn in its tone (the accent by
  default) - which the PPTX keeps as the run colour - instead of a stripe on
  the tile's left edge, which no export ever carried.
- *Bars are rules.* A callout bar and a quote card's bar are straight 4px
  rules (the two corners they run into stay 4px), and a callout bar fades as
  it runs down from the title (a `::before` over the solid border, which the
  PPTX keeps). The semantics are untouched: a `tinted` callout has its bar,
  `accent_bar` toggles it, `plain` drops the card.
- *No pastel slabs.* Tints are lighter (a callout wash 6%), the table header
  is a neutral band over an accent rule (a toned column keeps its tint),
  badges are hairline pills.
- *Rings and numerals.* An icon sits in a hairline ring (`.deck-icon-tile`,
  the phase and persona icons, the list agenda's number) - the circle of
  the brand mark, the one the timeline nodes are drawn in (now a 2px ring on
  a 2px rail). A numbered card's number is an OUTLINED numeral
  (`-webkit-text-stroke`, editable outlined text in the PPTX like the
  section slide's ghost number), 96px on the takeaways slide, whose cards
  read as posters: the number on top, the text at the foot - a poster of a
  readable height, never a pillar of the body's ("Takeaway cards are
  bounded too" above).
- *Eyebrows are detection boxes*. A cover's `event_label` and a
  section slide's `label` (`.deck-eyebrow`) are set as typed in the mono face,
  framed by two accent corner marks (top-left, bottom-right) - never the
  dash and the spaced capitals of a generated deck. The box hugs its text
  (`align-self: flex-start`, `center` on a centred cover) so the second mark
  closes the line. The `section_header` block wears the same box around its
  label (`.deck-section-header-label`: icon + title, in its tone) with a
  hairline of its tone running on to the column edge, fading out; the
  banner's eyebrow is set as typed in the mono face, without marks. The
  marks and the rule are pseudo-elements: the PPTX carries every one of
  these lines as editable mono text.

What a template adds on top - the watermark on its body slides - is its
choice (`chrome.watermark`, see "Template header and footer").

**Deck lifecycle.** `PresentationDeck.tsx` renders one `<Slide>` per spec entry
through `layouts/index.tsx` (`SlideBody`, one component per layout — a layout
missing there falls back to the generic one, which is why
`test_frontend_renders_every_layout_in_the_catalog` and its block twin exist).
The component is keyed on the deck `id` only — NEVER on `revision`: an in-place
edit bumps `revision`, and the effect calls `Reveal.sync()` + `layout()` so the
slide list reconciles without remounting or resetting the viewer to slide 1.
A `ResizeObserver` on the container re-lays out on any box change (the chat
card growing, an editor panel opening, a fullscreen transition), and embedded
decks only take the keyboard once clicked or fullscreen (`keyboardCondition`).
The icon catalog and Mermaid are separate lazy chunks; a conversation that
never generated a deck pays for neither.

**reveal under Tailwind.** reveal marks every non-current slide `hidden` (for
assistive tech) and shows the neighbours it has loaded through an INLINE
`display: block` — the outgoing slide of a transition, every slide of the
overview. Tailwind's preflight turns `[hidden]` into `display: none !important`
(in `@layer base`), which outranks a plain inline style: the overview drew one
slide and N-1 empty frames and a `slide` transition pushed nothing out. The fix
is reveal's own `display: "block !important"` config knob (an important inline
declaration beats every stylesheet one), not a stylesheet rule: an unlayered
`!important` in `presentation.css` would lose to the layered one (important
declarations sort earlier layers first), and a rule inside `@layer base` would
have to pick block or none for every hidden slide, losing reveal's load/unload
(`test_frontend_neighbour_slides_survive_the_hidden_preflight`).

**Variants.** `embedded` (chat card), `standalone` (`/presentations/:id`,
outside the app shell, keyboard always active), `print` (`?print-pdf`, reveal's
own print layout; the browser print dialog opens once fonts are ready). The
static export page is not a variant of the deck: it renders every slide as one
unscaled canvas with no reveal runtime (see Export).

## Layout: nothing overlaps, and the agent is told what does not fit

`validate_spec` guarantees a deck is well-formed; it cannot tell whether six
cards and a callout FIT on a 1920x1080 slide — that is a property of pixels,
fonts and wrapping, not of the JSON. Formerly the first one to find out was
the presenter: the body painted under its footer block and the chrome, a table
lost its last rows, a badge ran past its pill. Prompting the agent to "keep
slides light" does not change that (it cannot see the slide it wrote), and
rule 16 of `AGENTS.md` forbids fixing it with prompt text anyway. Two
deterministic layers instead, neither of which adds a character to any prompt
beyond the one sentence announcing the check in the tool description.

**Layer 1 — shrink-to-fit (`fit.ts`, the renderer).** `SlideBodyArea` in
`layouts/frame.tsx` wraps every slide body in a `.deck-fit` (the room the body
has: it fills the body and SHRINKS to it through the body's `flex: 1 1 auto;
min-height: 0` child rule, so its client height is always the space available)
/ `.deck-fit-content` (the body at its natural height, `min-height: 100%` so a
single block still fills a short slide) pair, and `useFitToHeight` keeps the second
fitted inside the first for as long as both are mounted. One pass measures
`host.clientHeight` against `content.offsetHeight`, scales the content by the
ratio that makes it fit (`transform: scale(k)`, `transform-origin: top left`)
and widens it by `1/k` so the scaled box still spans the full width — text
reflows into the wider box first, which is why the ratio is SEARCHED for
(`searchFit`, a pure function every layout read of which goes through a
`measure(k)` callback) rather than computed once. The obvious fixed-point step
`k' = available / height(k)` assumes the height holds while the box widens; it
does for fixed-height cards (one probe, exact) but not for text, whose height
tracks the ratio — a true fit of 0.8 turns into `1 -> 0.64 -> 1 -> 0.64`, and a
belt applied at the end over-shrinks readable content into a false
`overloaded`. So the step is taken once, to land on the other side of the fit
(from a ratio that does not fit it always reaches one that does, and vice
versa, for any body that is not taller in a wider box; a body that IS — a
full-width image — makes the step from above lean in by the overflow left, so
it cannot chase the fit without reaching it), and from then on the fit is
BRACKETED: `lo` fits, `hi` does not, the next probe interpolates on the visual
height (regula falsi) and bisects after two moves of the same end so a
stepwise reflow cannot stall it. The answer is always the fitting end, so
running out of probes (`FIT_MAX_PROBES = 8`) costs precision, never an
overlap; `FIT_SLACK_RATIO = 0.01` says when the precision is enough (a body
that fits and leaves less than 1% of its room unused is fitted — which is also
what makes a second pass over a fitted body a no-op), ratios are quantized to
a thousandth and never rounded UP (a ratio rounded up by a hair is one that
does not fit). The floor is `FIT_MIN = 0.35`: below that the text is
unreadable anyway, so the pass stops there and the host CLIPS what still does
not fit (`.deck-fit[data-fit] { overflow: hidden }` — only a host that had to
shrink clips, so a body within budget keeps its shadows and focus rings). A body
at the floor is therefore cut at the bottom, never painted under the footer
block or the chrome, and the audit reports the cut with the ratio (below). The
pass runs in `useLayoutEffect` before the first paint (no flash of overlap) and
then from a `ResizeObserver` on both boxes, coalesced to one animation frame:
fonts landing, a diagram resolving, a hidden reveal slide being shown, an edit
landing from the studio. Content that fits is left EXACTLY as before — no
attribute, no transform — so a deck within budget renders pixel-for-pixel as
it did; the ratio of a shrunk one is published as `data-fit` / `--deck-fit` on
the host, which is the contract the two consumers below read. The PPTX
primitive extractor (`EXTRACT_PRIMITIVES_JS`, `fitOf`) corrects every measured
font size, padding and border of a scaled subtree by that ratio — a transform
changes what is painted, not what `getComputedStyle` reports — so the editable
export matches the screen (a 26px body at `k = 0.36` lands as 9.36px, and the
header outside the host stays 56px).

**Held blocks.** One ratio for the whole body drew identical KPI rows
at different sizes on the slides of one deck: 44px values on the slide that
fits, 34.6px on one shrunk to 0.787. A block that sets its own size - today the
`metrics_grid` - is therefore HELD at it: its root carries `data-fit-hold`
(its block type, `FIT_HOLD_ATTR` in `fit.ts`), `.deck-fit-content` publishes
`--deck-fit-hold: calc(1 / var(--deck-fit, 1))`, and every length the grid
draws (value, label, sublabel and delta type, tile padding and gap, the grid
gap, icon, sparkline, hairline, radius) is multiplied by it. The scaled body
draws the grid at its size, and the search, which measures after every probe,
takes the room the grid keeps from the rest of the body. Its computed lengths are
`size / k`, which the PPTX extractor's `fitOf` multiplies back to the size, so
the export needs nothing of its own. `fitContent` lets go only when the body
does not fit even at `FIT_MIN` with the hold: it sets `data-fit-released` on
the host (the hold back to 1), fits again, and the held blocks shrink with the
body. The audit then reports each of them (`size`, below). A pass over a
released body takes the hold back first and ends where it started unless the
content changed, so the resize observer still settles. Outside a fitted body
the hold is 1, so a slide that fits renders exactly as before.

**The spread caps are part of the ruler.** Every probe of the search
changes the layout a `fill` / `distribute` cap (`spread.ts`) was
measured on: the body widens by `1 / k` and text reflows, and a held grid's
tiles are rescaled by the hold. Left to the spread's resize observer, a cap
landed one frame after the layout it describes, so the search read a grid
whose box a stale cap clipped short of its grown tiles: the body "fitted"
with the hold kept, the cap then grew with the tiles, the search shrank again
down to the floor, released, and started over - the tiles blinked on every
frame and the last row was cut off under the footer block. `fitContent`
therefore re-measures the caps synchronously on each layout it reads
(`recapSpreadsWithin(content)`, innermost first so a `distribute` run reads
the cap its `fill` block has just taken, over the elements `useSpreadCap`
registers). A shrunk body has no free room to spread, so `fill` then never
changes what the fit decides: the slide settles in one pass at the ratio and
hold of the same slide without `fill`
(`tests_e2e/presentation-fill-fit.spec.ts`).

**Layer 2 — the audit (`presentation_layout.py`, the backend).** Right after
`save_presentation`, `layout_check_note` renders the deck through the export's
own headless Chromium (`render_deck(want_layout=True)`, the same static page
the PDF comes from, so what is measured is what is shown) and runs
`LAYOUT_AUDIT_JS` over the real DOM. Per slide it reports the kinds below —
those that measure EXCESS, `sparse` that measures DEFICIT, and `values`, a
chart's numbers that found no room:

| Kind | Measured how | Graded |
| --- | --- | --- |
| `fit` | `data-fit` on any `.deck-fit` host, plus the glyphs cut below the host's padding edge once the ratio sits at the floor (the host is then left out of the `clipped` pass — one cause, one observation) | `dense` below `FIT_DENSE_BELOW = 0.92`, `overloaded` below `FIT_OVERLOADED_BELOW = 0.75`; at `FIT_FLOOR = 0.35` (the mirror of `FIT_MIN`, pinned by a test) the message says the excess is a lower bound and how many px are cut off |
| `size` | every held block (`[data-fit-hold]`) of a fitted host that had to let go (`data-fit-released`): its type (`data-fit-hold`), its `data-size` and the ratio it is drawn at | `dense`: the block fits, but reads smaller than the same size anywhere else in the deck. `amount` is the share of its size drawn, in percent. The remedies cut what shares the slide, move part of it to a new slide, or name the next smaller size for every such block of the deck (none at `sm`). An excess kind, so the slide is never also told it is sparse; the body's own `fit` finding is kept |
| `clipped` | glyph rectangles (`Range.getClientRects`) accumulated on every ancestor, compared with the padding box of any element that clips (`overflow: hidden / clip`); the chrome footer by `scrollWidth - clientWidth` | `overloaded` from `CUT_OVERLOADED_PX = 40` (a table row, a line of text), `dense` below |
| `nowrap` | a `white-space: nowrap` element whose glyphs run past its own box | same cut grading |
| `overflow` | a visual element (has glyphs, `<img>`, `<svg>`) whose box lies past the canvas edge; the worst one per slide | same |
| `lines` | `statement` quotes only: the text's `clientHeight` over its computed line height, at the size it is drawn in (a `hero` after its one-step shrink, which the observation says - `stepped` - so the message names the h1 size the lines were counted at, not `hero`) - layout metrics, so a shrunk body changes nothing; reported raw for every statement | `dense` past `STATEMENT_MAX_LINES = 4`: the quote fits, but a statement is read in one breath. The remedy cuts it to its strongest sentence, names the next smaller `size` (none at `md`) and the `pull` variant beside the context |
| `columns` | a `columns` block of four the renderer wrapped 2 x 2: `data-wrapped`, with the width its widest column's content needs and the width a column gets side by side (`data-column-need` / `data-column-room`, canvas px, the body unshrunk) - the renderer's own measure, read as written | `dense`: the wrap is a fallback, not the layout the deck asked for; `amount` is the width needed. An excess kind, so the slide is never also told it is sparse |
| `values` | a `chart` that writes fewer values than its block asked for: its drawing counts them on `.deck-chart-facts` (`data-values-asked` / `data-values-written`, with `data-values-reason` / `-room` / `-need` - `chart-layout.ts` decides them, see "Values on the figure"), and the audit names the chart by its position among the slide's charts (`chart`, 1-based) with its kind and shape, read off `data-deck-chart` | `dense`, the message opening `values hidden on chart N (kind, S series x C categories)` with the room the tightest label had and what it needed, and the remedy its reason names (`_VALUES_REMEDIES`: more width or fewer categories / series for `narrow` - whose detail for a `line` / `area` says the values were tried in one row and alternating above and below the points, and for a `pie` / `donut` (`_slice_values_detail`) names its smallest hidden slice's share (`data-values-share`) with the room along the rim it had outside and what its value needed there, or - its values inside the slices (`data-values-placement`) - the radius the disc would need for that slice to hold its value against the radius it has, the remedy then naming the legend under the chart (`legend_position` bottom, only where it stands on the right), more width, an `Other` slice or `show_values: false` - more height for `short`, an `Other` slice for `dense`, for `overlap` (a value on a point with no room left) the pinned ranges `y_max` / `secondary_y_min` that part a line from the bars and a series' own `show_values: false`, `show_values: false` to leave the numbers to the tooltip; for `upright` (values kept upright at the size asked on one row, `value_label_fit` upright, with the pitch once the bars gave way and the widest value's need) `value_label_fit` stagger, a smaller `label_size` - a smaller `value_label_size` when the chart sets one - more width; on a horizontal `bar` / `stacked_bar` (`_HORIZONTAL_CHART_KINDS`) `narrow` and `upright` name the pitch of two rows and a value's height and offer `value_label_fit` stagger or more height, `short` more width, `_HORIZONTAL_VALUES_REMEDIES`); `advisory` for `segments` (a stack's thin segments with no room inside nor past the stack, its totals written: the detail names the thinnest one's share of the axis, its length and its need; more height - `size lg`, or `fill` with at least `CHART_TALLEST_CANVAS_PX`, its panel's height in a dashboard - or more width is recommended only where the drawing's own plan at that size writes every value (`taller` / `wider`), else the message says it would not, with the length the thinnest would reach, and points at the series' own `show_values: false` (named, read off `data-values-series`) or leaving it, `_segments_remedy`; a horizontal stack fitted `upright`, read off `data-values-fit` as `fit`, says it kept the size and offers `value_label_fit` stagger first). `amount` is the count not written. Not an excess kind: "give the chart more room" never contradicts `sparse` |
| `rotated` | a `chart` whose values are turned on their side: even at the smallest size of its text scale an upright value would touch another value or another bar (`data-label-rotate`, with `data-upright-size` / `-room` / `-need` on `.deck-chart-facts`, and the block's `label_size` off the figure's `data-label-size`) | `advisory`, every value being written: `values rotated on chart N (kind, S series x C categories): the values are turned on their side at Xpx - set upright at Ypx, the widest needs Npx across and the bars stand Rpx apart` (or, when the widest fits the pitch, that a value would touch the value or the bar beside it), then the remedies: `value_label_fit` stagger on a bar, stacked bar or combo chart (`_VALUE_LABEL_FIT_KINDS`), `label_size` one step down (`_SMALLER_LABEL_SIZE`, never offered at `sm`) - or, when the chart sets `value_label_size` (read off the payload), that size one step down instead (`_smaller_value_label_size`, never offered once its ladder reaches no lower) - more width, fewer categories or series. `amount` is the size the values are drawn in. Kept beside a `values` finding of the same chart: the remedies differ |
| `shrunk` | a `pie` / `donut` whose values are set under the size its `label_size` asks (`chart-slices.ts`): `data-shrunk-size` (the size asked), `-room` (the radius the disc would keep with the values outside at that size) and `-need` (the least it keeps) on `.deck-chart-facts`, with `data-label-size`, `data-values-placement` and the figure's `data-legend-position` | `advisory`, every value it writes being on the figure: `values shrunk on chart N (kind, ...): the values are set at Xpx outside the pie (or inside the slices), under the Ypx of label_size L - at Ypx the values outside it would leave the pie a Rpx radius, under the Npx it keeps` (or, the radius kept, that two values would touch; inside, that even the smallest size has no room outside it), then the remedies (`_slice_remedies`): the legend under the chart where it stands on the right, more width, `label_size` one step down. `amount` is the size the values are drawn in |
| `sparse` | `content` slides only: the share of the body's ROOM - the `.deck-fit` host around the body host (`.deck-blocks-slide` / `.deck-columns-slide`), the space between the header and the footer block - left empty below the bottom edge of the last block: the run's children on the single-flow form, each column's blocks on the columns form (the columns themselves stretch), never a `scrollHeight`, which reads a stretched box and hides the gap. The room, not the run's box, because a `fill` block or a `distribute` run stops at a readable gap and its box ends at its cap. Not measured when the slide placed its run on purpose: `data-valign` center / end WITHOUT `data-distribute` - the switch wins over the placement in the renderer, so it wins in the guard too. `distribute` silences nothing - its spread is capped, so the floor it used to reach can be bare | `advisory` above `SPARSE_ABOVE = 0.30` of the body; the sentence states the share of the SLIDE, which is what the room sees. Never reported next to an excess finding on the same slide — the remedies contradict — decided on the GRADED findings, so a fit ratio within a reader's forgiveness does not silence it. The remedies are READ OFF THE SLIDE (`sparse_remedy()`), never recited: `fill` only on a grid block the slide holds without it (named by type), `distribute` only on a run of two or more blocks (a column's own run on the columns form), a `footer_block` only where there is none, a merge with the neighbour that exists (next, else previous); a slide with none of them is told to add content. A slide that ALREADY spreads (every grid block filled, or `distribute` on its run) and is still sparse is at the cap by construction: the sentence leads with `Fill / distribute already spread(s) ... as far as reads well - the rest wants content:` and offers content, the footer block and the merge, never the switch again |

Decorative text is excluded THREE times — element pass, glyph walker, cut-text
excerpt — because `aria-hidden` marks what bleeds by design: the ghost section
number of the `section` layout paints past the frame on purpose, and its glyphs
would otherwise accumulate as the FRAME's extents and every section slide
would report "the slide is cut off at its bottom edge"
(`test_decorative_text_is_left_out_of_the_measurements`). The chrome footer is
aria-hidden too and keeps its own explicit check, which is what still catches a
footer text too long for its box. A body that overflows the canvas is reported
once, as the element that overflows, not also as the frame's own cut
(`test_an_overflow_is_not_also_reported_as_the_frames_own_cut`).

Observations become `LayoutFinding`s named by the nearest deck block the agent
can find in its spec (`blockOf`: `callout`, `card`, `table wrap`, `badge`... —
structural wrappers like `deck-blocks` or `deck-column` are skipped), with the
first cut text quoted, sorted worst first then in deck order, and bounded
(`MAX_REPORTED_FINDINGS = 8`, the count of the rest is stated). The
`format_layout_report` lines ride the `generate_presentation` result:

```
Layout check: 2 slide issue(s) the audience would see. Fix them and save again:
- slide 7 [content] "Six modules" (overloaded): the body needed 61% more height than the slide has and was shrunk to 62% so nothing overlaps - the text is too small to read from the room. Cut content: ...
- slide 12 [content] "Certifications" (dense): the badge "SOC 2 Type 2 and ISO 27001" does not wrap and runs 38px past its box, over what sits next to it. Shorten the label or use fewer of them on the line.
- slide 15 [content] "Features" (advisory): the body leaves 29% of the slide empty below the last block. Set fill on the feature_list block, add a footer_block, or merge with the next slide.
```

so the agent that just saved a too-dense slide is told so in the same turn and
trims it before answering; a clean deck gets one reassuring line. Every line
speaks the deck's kind: `LayoutReport.unit` comes from `unit_noun()`, so a
portrait brief reads "page 3" and "every one of the 2 pages", never "slide" —
the same rule every other tool result already follows. The check is ADVISORY
and BOUNDED: it never fails the save. It is skipped — with the reason in its
own words, never an exception — after `LAYOUT_CHECK_TIMEOUT_SECS = 45` (longer
than a turn should wait, shorter than the render's own ready timeout), when the
browser is missing (a deployment message), or on any render error; and it is
off entirely under `PRESENTATION_LAYOUT_CHECK_ENABLED=false`, in which case
nothing is rendered and nothing is said — and the REST route 404s, so the
switch travels to the frontend as `presentation_layout_check_enabled` on the
public `/auth/config` payload (the `runners_enabled` pattern) and the viewer
offers neither the `Layout check` button nor the `layout` panel on such a
deployment, instead of a control whose only content is that 404.

A check is about ONE revision. The static page fetches the deck's CURRENT row
(`render-spec`), not the caller's snapshot, so a save landing from a
collaborator between the save and the render would have revision N+1's findings
read against revision N's titles — and the agent sent to fix the wrong slide.
The page therefore writes the revision it rendered next to the slides
(`data-deck-revision` on `.deck-root`, read back by `RENDERED_REVISION_JS`), the
callers pass the revision they hold (`expected_revision`: the save's new
revision in `generate_presentation`, the row just read in `get_presentation`
and the REST route), and a mismatch is a skipped check — "the presentation
changed while it was being measured (revision 1 -> 2); check again" — never a
report about a deck nobody holds. The same revision keys a short-lived cache: a
successful report is kept in Redis for `LAYOUT_CHECK_CACHE_TTL_SECS = 900`
under `presentation:layout:{deck}:{user}:{revision}` (per user, because the
render runs with the caller's visibility) and served to the next check of that
revision — the `get_presentation` read-back a Studio turn makes right after its
save, the panel's re-check of an unchanged deck — without a second browser
launch against the export's two render slots. A skipped report is never cached,
a check without a revision never looks, and Redis being down costs a render,
not the check (fail-open, like presence).

`get_presentation` runs the same check for whoever can EDIT the deck (`config`
and up — a read-only grantee is never told to fix what they cannot), and `POST
/presentations/{id}/layout-check` exposes it to the human on the same terms
(`_load_manageable`: a reader gets the manage-tier 403 and no render is spent,
and the viewer offers them neither the button nor the panel,
`layoutCheckOffered`): the viewer's Layout
panel (toolbar `Ruler` button, the `layout` value of the one side-panel slot)
lists the findings per slide, marks the report stale against the revision the
response names, and `Fix with my assistant` primes the Studio composer with the
same bounded list (`MAX_PRIMED_FINDINGS` mirrors `MAX_REPORTED_FINDINGS`) so
both sides read the same report. Each row locates its slide by the STABLE id
the render published (`currentSlideIndex`): a slide moved by an edit since the
check is still the one opened, and a slide that is gone is shown disabled with
a tooltip rather than sending the reader to whatever now sits at its old index
(the same anchoring rule as review comments). The check's RUN is held by the
viewer, not by the panel (`useLayoutCheckRun`): the panel is unmounted
when hidden, and a render that takes seconds must survive the user closing it
to look at the deck — so the toolbar's `Ruler` button spins a small indicator
on the icon's corner (where the comments button pins its count) for as long as
a check is in flight, panel open or hidden, with `aria-busy` and a label saying
what is rendering, and re-opening the panel shows the report that arrived
meanwhile instead of rendering the deck again. Opening the panel still asks for
a check when there is nothing current to show: no report, a stale one, a
skipped one. The hook scopes everything it exposes to the deck it was asked
about and the panel is keyed by the deck's id, because the router reuses the
page when only `:id` changes (a duplicate lands on its copy) and neither a
report nor the panel's run-once guard may be read against another deck. A `run`
while that deck's check is already in flight is a no-op — `isPending` only
flips on the render after `mutate`, so two callers in one tick would otherwise
render the deck twice and only the latest call's report would be recorded. The
report is recorded with the revision it describes, in one state update from the
success callback and never read off the mutation's `data`, which lands in a
separate update, so no render can pair a report with another run's revision and
flag the wrong one stale. Its clean state says nothing is cut off or crowded
enough to need a change — not that nothing was shrunk, since a body fitted
between `FIT_DENSE_BELOW` and 1 is deliberately not reported. The route takes no
body and writes nothing, which is why it is exempt from
`test_presentation_api_never_accepts_a_raw_spec`.

The unit suite never launches Chromium: `tests/conftest.py` turns the check off
for every test (autouse) and the `layout_enabled` fixture turns it back on for
the tests that mock `render_deck`. The real-browser behaviour (the fit ratio,
scaled PPTX runs, all four kinds, the two exclusions) was verified with a
Playwright harness kept out of the repository.

## Brand templates

**Theme resolution.** `resolve_template_theme()` returns the whitelisted,
CSS-safe subset of the linked container's `theme_config` — re-checked against
the READER's visibility on every read, so a container the reader can no longer
see contributes nothing rather than leaking its brand or 404-ing the deck.
`sanitize_theme_config()` keeps a value only when `theme_value_problem()` finds
nothing wrong with it. A value lands inside a single CSS declaration, so
anything that could leave it is refused (`;`, braces, a comment, a backslash
escape, a control or non-ASCII character, over `MAX_THEME_VALUE_CHARS` = 120
characters). And a value lands in CSS every viewer's browser paints
(`background: var(--deck-bg)`), so a value that loads a resource would report
each viewer's address and the time they opened the deck to whoever hosts it:
a browser loads only through a CSS FUNCTION, never through a keyword
or a quoted string, so every function a value calls must be on the
`SAFE_THEME_FUNCTIONS` allow-list - colour functions (`rgb`, `hsl`, `hwb`,
`lab`, `lch`, `oklab`, `oklch`, `color`, `color-mix`, `light-dark`), `calc` /
`min` / `max` / `clamp`, `var`, and the gradients (standard, `repeating-`,
`-webkit-` / `-moz-` prefixed, and the legacy `-webkit-gradient()` with its
`from()` / `to()` / `color-stop()` arguments). `url()`, `image-set()`,
`cross-fade()`, `image()`, `element()`, `paint()` and any unknown function are
refused, and the names are read the way a browser reads them: CSS escapes
decoded (`\75 rl(` is `url(`), case folded, no space between a name and its
`(`, a dashed author-defined function (`--1(`) read as the call CSS reads it,
every parenthesis closed. A quoted string is text, never a call, so it is
skipped (`"Foo (Bar)", sans-serif` is a font family) and one left open is
refused: an accepted value carries no backslash and no newline, so its strings
end exactly where a browser ends them. Both sides trim the same characters off
the ends of a value before judging and serving it (`THEME_VALUE_TRIM_CHARS`:
Python's `str.strip()` set plus U+FEFF, which JavaScript's `trim()` strips
too). The same guard refuses such a value on
every write - the authoring tool, the import / duplicate and the generator
through `validate_theme_config()`, the REST create / update through
`unsafe_theme_value_problems()` (a 422 naming the key; the routes still store
what merely does nothing, an unknown key or a blank value) - and the template
preview filters its client-built palettes through the mirror
`components/presentation/theme-value.ts`, which the sinks apply once more
(`themeStyle()` and `slidePaper()` in `theme.ts`, `ReportView`'s variables), so
a palette that reached them by another road still loads nothing. A template stored before the guard
needs no migration: the next read simply stops serving the refused value, and
the deck paints that key from its scheme's own token. Both sides are checked
against one case table, `frontend/tests_e2e/fixtures/theme-value-cases.json`.
`THEME_FILE_ID_KEYS` (`logo_file_id`, `logo_light_file_id` and,
`mark_file_id` - the mark the `gradient` hero watermarks) name platform FILES
rather than tokens,
are validated as UUIDs in their canonical 8-4-4-4-12 form (the frontend builds a
download URL from them, so braces, a `urn:uuid:` prefix or a missing dash are
refused rather than served as written), and fall
back to the container asset with the matching role (`THEME_FILE_ID_ROLES`) when
the config names none — which is how the seeded logo is drawn without a theme
key.

**Template and theme — one meaning per word.** The **template** is
the deck's linked DESIGN (palette per scheme, fonts, logos, predefined assets,
deck defaults, guidance); the **theme** is the deck's colour scheme, `dark` or
`light` and nothing else; a **palette** is a template's colours for one
scheme. A template's free-text `TemplateContainer.theme` label is LEGACY: it
never changed a pixel (only palettes do), so it is gone from the UI and the
agent tools (`search_templates` / `create_template` / the generator's
`TemplateCandidate` carry no style label), kept as a column and as an exact
fallback in `apply_structured_filters` for saved agent instructions that still
filter by it, shown by `ConfigTab` only when set (`Search label (legacy)` with a
`Clear` action — `PUT` with `theme: ""` clears it). The `theme` filter of a
template search has two forms, decided by `is_scheme_filter` and stated once in
`theme_filter_matches` (REST `GET /template-containers?theme=` and the
`search_templates` tool both go through `apply_structured_filters` +
`filter_by_theme`, never a re-spelled copy): `dark` / `light` use
supported-scheme semantics ONLY — they match the presentation templates whose
`supported_schemes` include the scheme, and the legacy label is never consulted
for a scheme (a light-only template still labelled `dark` cannot dress a
dark deck, so it does not come back for `theme=dark`); a non-presentation kind
has no supported scheme and is never returned for one; a presentation template
with no readable palette dresses either scheme and is returned for both. Any
other value is an exact, case-insensitive match on the legacy label. Because the
label form is a legal value, the tool schema keeps `theme` a plain string (an
enum of the two schemes would make that form unreachable from the tool).

**The link is live, not a copy** (documented on `Presentation.template_container_id`
in `models/presentation.py`): the palette is resolved on EVERY read
(`resolve_template_theme` / `resolve_template_themes` /
`resolve_template_palette`, re-checked against the reader's visibility), so
editing a template restyles every deck wearing it; a deck can be relinked or
unlinked at any time (Design panel → `PATCH /presentations/{id}` with
`template_container_id`, or the tool's `template_id`); content, slides, the
deck's own uploaded images and `spec.theme` belong to the deck; the FK is
`ondelete="SET NULL"`, so deleting a template turns its decks into free decks
rather than refusing or cascading.

**Supported schemes** (`template_supported_schemes`, serialised as
`supported_schemes` on templates AND on decks, `[]` for non-deck kinds). The
template DECLARES them: `extra_metadata.supported_schemes`
(`SUPPORTED_SCHEMES_KEY`, read by `declared_supported_schemes`) is the
designer's decision — the Dark / Light switches of `NewTemplate` and of the
`ConfigTab` `TemplateDesignEditor`, the `supported_schemes` argument of the
authoring tool's `create_template` / `update_template`, and what the generator
writes on every candidate (`normalize_template_candidate` derives it from the
palettes it drafted). `validate_supported_schemes` (`template_containers.py`)
holds every write path (REST create / update, authoring tool, package import)
to the same contract: a list, only `THEME_SCHEMES` values, at least one — "a
template that supports no theme cannot dress any deck" — stored canonical and
de-duplicated, and dropped silently on a non-presentation kind, where it
means nothing. A row that declares nothing (an older row) keeps the
derived rule as a FALLBACK, `derived_supported_schemes`: the base palette supports
the scheme its `background` reads as (`palette_scheme`, WCAG luminance); the
other scheme only through an `extra_metadata.theme_variants[<scheme>]` palette
whose own background reads as that scheme (a variant with no background is
taken at its word); a base with no readable background supports both. The
derivation is also what the form PROPOSES: the editor pins the derived list
into the declaration the first time a legacy row is opened for editing, so the
next save writes what the switches show. The client mirrors both halves
(`declaredSupportedSchemes` / `derivedSupportedSchemes` /
`templateSupportedSchemes` in `previewSpec.ts`). This list is what the deck's
theme picker is restricted to (`ThemeSchemePicker`), what a template link
snaps the deck's theme against (`fit_theme_to_schemes`: keep the deck's scheme
when the template can dress it, else the template's first — "template wins"),
and why a direct change to an unsupported theme is refused with
`unsupported_theme_message` (REST 422 and tool error alike — one sentence,
every surface). A deck whose STORED template the editor cannot see reads as a
free deck (`resolve_template_palette` contributes no palette, no name, and
`supported_schemes` serialises as both), so a `PATCH` theme change on it is
accepted for either scheme: the picker that editor sees offers both,
the link is kept as stored, and no visibility bypass reads the hidden
template's schemes on the editor's behalf
(`test_patch_theme_is_free_when_the_stored_template_is_invisible`). Declaring a
scheme ON does not conjure a palette: a supported
scheme with no palette of its own is dressed with the base palette's brand half
over the renderer's built-in surfaces (the `scheme_palette` composition below),
and the editor says so in the empty variant section.

**One kind of template.** `TemplateContainer.kind` is the closed choice
`TEMPLATE_KINDS` = `presentation` / `report` (`normalize_kind` — blank means
presentation, case-insensitive; `kind_problem` is the one refusal sentence
behind the REST 422, the authoring tool's refusal, the generator route's 422,
the package import's 422 - an export carrying a retired kind is refused by
name, never coerced; the in-app duplicate, which has no export to edit, copies
a legacy row as a presentation template - its metadata reduced first to what a presentation honours (`coerce_metadata_to_kind`: honoured palette keys and slide layouts kept, the rest - an `extra_metadata` that is not an object included - listed under `metadata_dropped` in the audit details next to `kind_mapped_from`), so the strict pipeline stores the copy instead of refusing it over a layout of the old kind, while a source of a supported kind is held to the pipeline as is - and the `search_templates` enum). On UPDATE an echo of the
stored kind is a no-op whatever the value (`resolve_kind_update`, shared by the
REST `PUT` and the tool's `update_template`): a legacy row saves its other
fields, and only a CHANGE is held to the choice. The `asset-library` kind is
RETIRED: a
template's assets are readable by any deck whatever its kind, so a container
that carried logos and nothing else was a second place to look for the same
brand. The seed (`template_container_seed.py`) declares only the two brand
decks; `RETIRED_ASSET_HOMES` maps each retired library slug to the container
that inherits its assets (`filigran-brand`, so only in Copilot mode, where it
ships - every other mode has no heir and drops the libraries with their logos),
and the three-phase boot (`_rehome_container_assets`)
MOVES the existing `FileAttachment` rows — same ids, so every `file_id` a
stored deck already references stays valid — matched by seed tail
(`seed_tail()`, the `<hash16>-<filename>` part of the object name after `seed/`) before the
asset sync runs, then re-counts every row still bound to the library (whatever its `source`: the set the FK cascade would destroy) and deletes it only when nothing is left, the retired row held `SELECT ... FOR UPDATE` from before the move to the one commit so an asset insert racing the boot is either seen by the count or refused by its FK check, never cascaded away. `MAX_ASSETS_PER_CONTAINER`
went from 100 to 400 for the ~180 assets the brand deck now holds, and
`get_template(template_id, role=, query=)` filters the asset list server-side
(`_asset_matches` on role, filename and description - and on the asset's role label itself, so `query="mockup"` lists a custom-labelled group whatever its files are called) so an agent asks for "the
partner logos" or "AWS" instead of reading the inventory, and the unfiltered
call is bounded too (`_DETAIL_ASSET_LINES` = 60: above that the largest roles
are collapsed, biggest first, to their count in the header plus one `role=`
pointer - or a `query="<label>"` one for an unlabelled or custom-labelled group, which `role=` cannot name and which the label in the haystack makes reachable - while the small roles stay listed in full). A legacy row of a retired
kind keeps working: displayed under its own label (`templateKindLabel`, offered
once in `TemplateKindSelect` so it can be moved off), described generically by
`get_template` ("not a presentation template ... its assets are still
referenced by file id from any deck"), refused as a deck's `template_id` like
before, and never creatable again.

**Which template a deck gets** (`resolve_deck_template_id`): a UUID names it
(found through `search_templates`); one of `TEMPLATE_NONE_SENTINELS` (`none`,
`null`, `no`, `false`, `-`) opts out — and on an edit UNLINKS the template the
deck carried; omitted means "keep the current one" on an edit (re-defaulting
mid-revision would restyle the deck under the user). On a NEW deck the template
is the USER'S choice before it is anything else: on an interactive turn
(`is_interactive_turn` — a `conversation_id` and no `assignment_run_id`) the
tool lists the templates the user can see (`list_template_options`) and, when
there are two or more, creates the deck ONLY when the choice is demonstrably the
user's — `template_choice_settled(request_text, template_id, options)`: the
text the agent acts on (the `request_text` every chat entry point passes) names
the chosen template (its name, folded case- and diacritics-insensitively by
`fold_text`, or its id) or asks for no template in so many words
(`_NO_TEMPLATE_PHRASES`, checked only for a `TEMPLATE_NONE_SENTINELS` opt-out) —
or the question was already put to the user in this conversation
(`template_choice_asked`, a Redis marker `presentation:template_asked:{conv}`
written by `mark_template_choice_asked` whenever the list is returned, TTL
`TEMPLATE_CHOICE_ASKED_TTL_SECS`, fail-open both ways: no Redis means one
question more at worst, never a deck the user did not pick). Anything else —
a blank `template_id`, OR one the model picked on its own after a
`search_templates` — returns `format_template_choice(options, proposed=...)` as
a non-error result and creates NOTHING: the agent relays the list (it may
recommend what it picked, the text says so) and the user's answer is the next
call's `template_id`. The `proposed` branch is the loophole this closes: the
blank-only check let a model look the templates up, like one and pass its id,
and the user got a branded deck they were never asked about. When several
option names appear in the text (`Filigran Brand Deck` is a substring of
`Filigran Brand Deck (copy)`) only the LONGEST match counts, so a call picking
the base out of a request that named the copy is refused too. Exactly one
visible template is used with a note in the result; none means a free deck. A
non-interactive run (assignment, sub-agent, API) takes the platform default,
because nobody is there to answer. Edits never ask. The New presentation
dialog's primed message (`Using the "<name>" template (<theme> theme), create
a presentation about ...`) passes the text rule, so the dialog path creates in
one turn. So that the agent can act on the answer, every deck-capable entry
point unions the template lookup into the turn's tools: `ensure_deck_tools`
adds `DECK_COMPANION_TOOL_IDS` (`template_search`) whenever
`generate_presentation` is carried (`agent_chat`, `general_chat`,
`assignment_engine`). Anything else raises `TemplateChoiceError`, which the
tool returns verbatim so the agent knows what is accepted.

**Platform default** (`resolve_default_template_id`): the admin-set
`PlatformSettings.default_presentation_template_id` while it still points at a
company-managed container, else the first seeded brand container in
`DEFAULT_TEMPLATE_SLUGS` (`filigran-brand` where it ships - Copilot mode only -
then `xtm-one-brand`, which ships in every mode), else `None` (the renderer's
built-in theme). A NULL setting deliberately means "the shipped
brand", not "no template": the seed never writes the setting, so an admin who
clears it gets the brand back rather than unstyled decks. The PUT validates the
choice — a UUID, an existing container, `kind = presentation`, company-managed
— because the default applies to every user and a private container would be
invisible to everyone but its owner.

**Deck defaults** (`extra_metadata.deck_defaults`, keys in
`DECK_DEFAULT_KEYS`: `theme`, `transition`, `aspect_ratio`) fill whatever the
agent left unset (`apply_deck_defaults`, absent-or-null only — an explicit
choice, or a value already stored on a deck being revised, always wins). Each
value is then validated by `PresentationSpec` like any agent value, so a
template cannot smuggle an invalid deck. The same call also fills
the per-cover objects `hero_defaults` / `hero_tones` / `slide_themes`
(`DECK_SLIDE_DEFAULT_KEYS`) on the title / closing slides that name none - on a
NEW deck only; a wholesale rewrite strips them first - see "Per-slide themes
and hero backdrops". `extra_metadata.guidance` is returned
by `get_template` to whoever generates from the template - on a line of its
own, served whole up to `_GUIDANCE_BUDGET`: it is the prose the
composing agent reads, and inside the compact-JSON dump of `extra_metadata`
(cut at `_EXTRA_METADATA_BUDGET`) the brand deck's palettes pushed it past the
cut mid-sentence.

**Theme variants** (`extra_metadata.theme_variants`, `THEME_VARIANTS_KEY`): a
template's `theme_config` is ONE palette and it always applies, but a deck also
names a theme (`dark` / `light`), and a dark-palette template on a
deck the user asked to be `light` used to yield a dark deck that merely CALLED
itself light — the chrome then drew the dark-on-light `logo_light` asset onto a
dark background, invisibly. A template may therefore carry a second palette per
colour scheme (`{"theme_variants": {"light": {<theme_config keys>}}}`, schemes
in `THEME_SCHEMES`); `resolve_template_theme(..., deck_theme=...)` composes the
palette through `scheme_palette(base, variant, scheme)`: the variant for
`theme_scheme(deck_theme)` laid over the base (`template_theme_variant`,
sanitized with the same whitelist as the base so a variant can never forward a
key or a value the base could not). A template with NO variant for the deck's
scheme does not get to keep the surfaces of the other one either — that was the
residual half of "switch to light, everything stays dark": the seeded XTM One
deck ships no variant, so under `theme: light` its dark `background` /
`foreground` / `surface` still landed inline and outranked the stylesheet's
light tokens. `palette_scheme` reads which scheme the base is designed for off
the WCAG luminance of its `background` (`css_color_luminance` — hex, `rgb()`,
`hsl()`, `white` / `black`; anything unreadable means "apply as authored"), and
when it is the other scheme the base's `SCHEME_BOUND_THEME_KEYS` (surfaces,
ink, feedback tones) stay behind so only the brand half — accent, gradient,
fonts, logo ids — travels and the renderer's own tokens supply paper and ink. A
PARTIAL variant inherits nothing scheme-bound from the base either. Reports pass
no `deck_theme` and get the one palette. The variants are validated on every
write path (`validate_theme_variants` — authoring tool, package import) against
exactly the base's contract, and the REST routes refuse an unsafe value in them
as in the base (`unsafe_theme_value_problems`). The seeded `filigran-brand` ships a `light` variant
read from the design system's `.light` token block; the template preview dialog
exposes the dark / light switch for EVERY template (`previewThemeForDeck`
mirrors `scheme_palette` client-side, key list pinned to the backend's by
`test_frontend_preview_mirrors_the_scheme_bound_keys`), which is how an admin
sees whether their brand needs a variant of its own before a user asks for a
light deck. A variant is what makes the other scheme
supported for a template that DECLARES nothing (`derived_supported_schemes`,
above); otherwise the declaration decides, and the `ConfigTab`
`TemplateDesignEditor` keeps the two in step: switching a scheme ON whose
storage slot is the variant (`paletteSlotFor` mirrors `scheme_palette`: the
base palette serves the scheme its background reads as, or the first declared
scheme when it reads as neither) seeds a starter palette from `VARIANT_STARTERS`
that reads as the scheme, switching it OFF drops the dead variant and snaps
`deck_defaults.theme` onto a supported scheme (every `deck_defaults` write of
the editor - the three selects and that snap - goes through `withDeckDefault`
in `template-deck-defaults.ts`, which changes ONE string key and carries the
per-cover objects through; rebuilt from the selects' strings, the
object lost a brand's covers on its first transition change); the base palette
as ONE card, always first (the full colour grid + fonts, labelled with the
scheme its background reads as, and turned into the amber "Base palette" card
when the base reads as a scheme the template no longer supports - only its
brand half reaches a deck), then a variant card per other supported scheme
(the scheme-bound keys + accent + gradient), and an amber note when a
palette's background reads as the OTHER scheme. The base card never moves or
remounts: a background typed key by key passes through values that read as the
other scheme (`#0a1` on the way to `#0a1220`), and a card keyed by that reading
unmounted the focused field and closed an open colour picker. The raw JSON
stays reachable under `Advanced (JSON)`.

**Chart series and brand hues in the form.** Every palette card also
edits its chart series (`chart_1` .. `chart_6`, over a bar preview of what its
decks draw) and its brand hues (`brand_1` .. `brand_4`). The groups are cut
from `palette-keys.ts` (`THEME_CONFIG_KEYS`, pinned to the backend whitelist by
`test_template_palette_form.py`), so the form never offers a key the renderer
ignores. An empty field shows, dashed, what a deck draws instead
(`paletteFallback`): what a variant inherits from the base
(`inheritedThemeForScheme`, the preview's own rule), what a variant stored for
the base palette's OWN theme sets over the base card, else the stylesheet's
chain (`brand_N` -> `chart_N`, `chart_1` -> `accent`). That same-theme variant
is valid JSON every deck in the theme lays over the base, so the base card
names its keys and a base edit takes the key back from it
(`withBaseKeyTakenBack`) - never `background`, which decides the theme and, typed
key by key, reads as the other one on the way. `Preview the unsaved design`
(`draft-template.ts`) previews the form as saving would write it; a name, an
aspect ratio or layouts the save would refuse fall back to the saved value,
with the limits counted in code points as `max_length` counts them.

**Live theme preview.** The resolved palette lands on the deck root as inline
`--deck-*` variables, which OUTRANK the `[data-deck-theme]` tokens of the
stylesheet — so a deck read serving only the SAVED theme's palette made the
editor's theme switch a lie: pick `light` and the root said `light` while the
inline dark palette kept every surface dark until the save round-trip. A deck
read therefore carries both `theme_config` (the saved theme's palette, what a
reader renders) and `theme_configs` (the palette per scheme in `THEME_SCHEMES`,
one container load through `resolve_template_themes`), and every renderer —
`PresentationDeck`, `PageDocument`, `StaticSlide`, the static export page —
resolves its inline palette through `themeConfigFor(presentation, spec.theme)`
(`components/presentation/theme.ts`) with the theme it actually draws, falling
back to `theme_config` for a response cached before the map existed. The editor
panel hands its unsaved draft to the viewer (`onDraftChange`), which renders the
draft spec in place of the stored one while the panel is open, so a theme
switch, a retitle or a reorder is seen before Save and the stored deck is back
on Discard / Close. Mermaid diagrams bake the palette into their SVG at render
time, so `Diagram` watches the deck root's `data-deck-theme` / `style` and
re-renders only when the variables it consumed actually changed (a page
document's zoom variables live on the same root and must not cost a re-render).

**Seeding.** `template_container_seed.BUILTIN_TEMPLATE_CONTAINERS` declares the
`filigran-brand` deck (`platform_targets: ["copilot"]` - Copilot only:
its assets include Filigran's customer and partner logos and team photos; its
chrome turns the body-slide watermark on), the `thread-brand` deck
(`["copilot"]` as well: the THREAD conference identity - both palettes
built from `THREAD_COLOR_SET`, the design kit's Color Set by name:
every colour is one of the set, a neutral step between its Black Anthracite
and Alabaster, or one of the two light-scheme tones the set has no readable
colour for (`_THREAD_LIGHT_SHADES`: success and warning, the set's Mint and
Orange darkened to 3:1), pinned by a test with the contrast floors - dark
first, with a `light` variant on Alabaster whose accent is Cobalt Blue
(the lockup in dark ink as its `logo_light`),
Elms Sans headings, the `cascade` covers (the four tiled covers are its own,
`extra_metadata.heroes`), the corner emblem and a footer
signing every slide with the edition, the talk and the classification - with
every Filigran figure included from the `filigran` pack, see below) and
the `xtm-one-brand` deck (`["global"]` - every mode, the working example no
platform goes without), full-synced on every boot by slug tag (`slug:<slug>` +
`builtin`), never touching an admin-created container, and removing built-ins
no longer declared for the mode with every asset they hold (rows and S3
objects; linked decks lose only the link, `ON DELETE SET NULL`, and a picture a
slide placed from the removed built-in goes with it). The rows go in the
removal's transaction (`file_cleanup.delete_file_attachment_rows`, which hands
back their object keys) and the objects only after its commit
(`purge_storage_objects`), a purge that stops at the first error from a store
that does not answer: the first start after an upgrade removes a whole brand
deck, and one connect timeout per object, under the bootstrap lock, would keep
every replica from starting. What that one pass leaves - a store down at that
start, a process killed between the commit and the purge, a delete the store
refused (`file_storage.delete` swallows an S3 error answer) - the scheduler's
hourly `template_asset_seed.purge_orphaned_seed_objects()` finds in the bucket
itself: every object of the exact seeded shape
`template-asset/<container>/seed/<hash16>-<file>` whose container no longer
exists and that no row names is deleted, every version and filename a seed
ever wrote and the re-homed logo libraries' keys included, with no manifest to
read. It lists `template-asset/` one level deep and only a gone container's
`seed/` group in full (the hourly cost does not grow with the assets of the
templates that exist), reads the database on short sessions of its own so no
transaction is open while the store is called, deletes nothing when the store
is down, and returns before any I/O in Copilot mode, which ships every
built-in. Every scheduler tick asks (`purge_orphaned_seed_objects_if_due()`,
after the tick's own session closed) and a Redis lease
(`template_asset_seed:orphan_sweep`, `ORPHAN_SWEEP_INTERVAL_SECS`) lets it run
once an hour install-wide: the hourly block's tick counter is per process,
advances only on the ticks a process wins and restarts at zero, so it is no
fleet-wide hour. A
slide that placed a removed picture keeps its dead file id and draws nothing
there (the static export page too: a failed image renders nothing, never a
broken-image box). No write is refused over it: the REST editor passes
the stored spec as `verify_file_references`' baseline, and the assistant's
edit and rewrite, a version restore and a package import pass the ids they
carry over from a stored spec as `carried` - a carried id with no row at all is
returned instead of refused, named in the tool result (`gone_pictures_line`) and
counted in the history line (`gone_pictures_note`); an id the write introduces
is still refused, and a carried id that still has a row is still checked
against the writer. A package reference the exporter flagged `missing` gets a
fresh id on import, so the destination is never probed for the exported id.
The removal half is also
`remove_unshipped_builtin_containers()`, which `main._bootstrap_platform_data`
runs on EVERY start outside the AI gate: `provision_builtin_content` - and with
it the full seed - only runs when AI is on, and an AI-off XTM One platform must
lose the Filigran deck all the same. The seeded decks' `layouts` are DERIVED
from `layout_catalog()` so a seeded container can never advertise a layout the
validator would reject. Binaries come from
`template_asset_seed`: each `brand_assets/<brand>/manifest.json` maps files to
`asset_role`s (`logo`, `logo_light`, `logo_mark`, `product_logo`, ...). A
manifest may reuse another pack's files without copying them:
`"include": [{"pack": "<folder>", "except_roles": [...]}]` adds every entry of
that pack's OWN manifest minus the listed roles (never what that pack itself
includes, so includes do not chain), and an entry with `"pack": "<folder>"`
reads its file from that folder, which is how a deck takes one file under a
role of its own. An include may name the roles it takes instead
(`"only_roles": [...]`): the XTM One templates, which ship on every
platform, take the Filigran pack's `product_logo`, `icon`, `diagram` and
`screenshot` families that way, so a family added to that pack later stays on
Copilot until a manifest names it; a role either list names must be an asset
role, or the include is refused (a misspelt exclusion would let its family
through), and so is an include whose two lists cancel out. Every template
holds at most one asset per chrome role (`logo`, `logo_light`, `logo_mark`),
pinned by `test_every_template_holds_at_most_one_asset_per_chrome_role`. A file the manifest lists itself is never taken again from an
include, silently: its own entry is how a template keeps its own logo over a
pack's file of the same name, or re-captions a file it includes. A manifest
with no `container` is a LIBRARY, fed only to the templates whose packs
include it. The badge pack and the partner, integration and customer logo
packs (`filigran-badges`, `filigran-partner-logos`, `filigran-integration-logos`,
`filigran-customer-logos`) are PRIVATE libraries: the Copilot templates
include them, but they hold logos and badges without release permission
(#11458), so the repository does not ship them; the Copilot reads them from
XTM-One-Platform/release (`copilot/brand-assets/`) through its brand asset
source. `test_the_private_libraries_never_ship_and_feed_copilot_templates_only`
keeps them out of `brand_assets/` and out of every template that ships
outside Copilot. Packs may also come from the folders `BRAND_ASSETS_EXTRA_DIRS`
names, read after the bundled root (a pack folder name loads once, the first
root wins; an entry's `"pack"` borrow reads its own root, an include reaches
any root). An administrator may also name a GitHub folder of packs in
Settings > Platform (`brand_asset_source`): every asset seed (boot, a save,
`Sync now`) downloads the repository's archive with a GitHub integration's
token, extracts the packs into a temporary root read after the others, and
seeds; a download that fails (or reads no pack) is logged and the seed runs
without it, retiring nothing in any template, since a source pack may feed a
template directly. A
pack that did not load is skipped with a warning, never a boot failure, and a
template fed by a pack that did not load WHOLE (an include naming a pack no
root holds, an entry that did not load) retires none of its seeded rows that
boot (`incomplete_containers`): its files are missing, not removed, and the
slides placing them keep them. A manifest that cannot be read at all, or a root
that is not there, names no container, so no template retires anything that
boot (`LoadedManifests.unreadable`). No template that
ships outside Copilot may hold a Copilot-only file, by role or by name
(`test_the_templates_of_every_platform_ship_the_product_material_and_nothing_copilot_only`). Every
seeded asset is stored under a DETERMINISTIC object key derived from the
container id, the content hash and the filename, so idempotency needs no hash
column — a boot compares expected keys with the `FileAttachment` rows and only
touches S3 when something changed. A changed file (same filename, new bytes)
is refreshed IN PLACE: the row keeps its id and takes the new object,
size, type and labels - a deck places a template asset by row id, so a new row
would have left a hole on every slide showing the old version, and
`get_template` promises ids "stable across a binary replacement" (the admin
replace route already kept them). A file the manifests no longer ship at all
(renamed or removed) loses its row; a deck that placed it keeps a dead id,
repaired with `replace_image`. Every object a row stops naming - the refreshed
file's previous object, a removed file's object - is deleted only once the
rows are committed, never by the sync itself: a rolled-back sync leaves every
row naming an object that still exists, and a failed delete leaks an object
(logged), never an asset. The asset seed
runs after the metadata sync has committed and is failure-isolated per
container (an absent object store degrades to a deck without a logo, never a
boot failure). SVGs go through `svg_sanitizer.sanitize_template_asset` on the
way in (defusedxml; scripts, `foreignObject`, handlers, external `href`s
dropped; an entity-declaring DOCTYPE refused) — the same entry point every
other asset write path uses (REST upload / replace, package import, the
`attach_template_asset` tool), so the stored bytes are the clean ones.
Seeded templates carry released brand assets and deck-agnostic guidance only:
never screenshots of unreleased features, a deck id, issue numbers of other
repos or guidance written for one talk.

**Filigran Brand Deck: one title ink and the contrast table.** The
`filigran-brand` seed opts into `title_ink: brand`. On light, every
title of the deck is Royal Blue `#001BDA`: the body slide title, the agenda
header (an agenda is a body slide, its title is the slide header) and the
section title (`.deck-section h1`, which once drew the near-black body ink
`#00020C`). On dark every title is the foreground `#FBFBFB`. The
cover and the closing are always dark (`cover: brand`), so their headline is
the foreground in both schemes. The rule lives in `presentation.css` next to
the `title_ink: brand` selectors and is pinned by
`slide-compositions.test.ts`. The measured pairs (WCAG 2, on the light page
`#E6F7FF` and the dark paper `#0A0C1C`):

| Element | Scheme | Ink on paper | Ratio | Status |
|---|---|---|---|---|
| Every title (body, agenda header, section) | light | `#001BDA` on `#E6F7FF` | 8.8:1 | passes 4.5:1 |
| Every title | dark | `#FBFBFB` on `#0A0C1C` | 18.8:1 | passes 4.5:1 |
| Body text | light | `#00020C` on `#E6F7FF` | 18.9:1 | passes 4.5:1 |
| Plain agenda description | light | `#00020C` at 0.65 on `#E6F7FF` | 6.6:1 | passes 4.5:1 |
| Section outline number | light / dark | accent at 30% / 45% | 1.8:1 / 2.6:1 | decorative, never text |

**Filigran Brand Deck: every agenda item alike (Brand 4 Nov 2026).**
The plain agenda (`design.agenda: plain`) has no highlighted item: under it
`AgendaLayout` publishes no `data-current` / `data-dimmed`, so an agenda
slide that still stores `current` (an older deck, an API caller) draws every
item in the same state, light and dark. `current` stays valid in the spec and
keeps its highlight under every other template. The seed advertises the
agenda entry of such a template without the `current` field
(`template_container_seed._layouts_for_design`), so `get_template` does not
offer it. No text of the deck sits under the contrast floors any more.

**Read-only built-ins.** A container tagged `builtin` is refused on every
mutating REST route and by the `template_authoring` tools
(`is_builtin_container`), and the UI shows `Built-in` + `Read-only` badges with
a `Duplicate` CTA: the seed full-syncs every field on boot, so an edit would be
silently reverted by the next deploy.

## Sharing

`Presentation` carries the complete three-tier visibility column set
(`is_company_managed`, `shared_group_ids`, `shared_group_permissions`,
`shared_user_ids`, `shared_user_permissions`, `access_restricted`) and is
registered in the `CompanyManagedEntity` union, so `user_visible_filter()` /
`user_visible_query()` apply unchanged (see rule 19 in `AGENTS.md` — a partial
column set is a 500 for every non-admin).

`deck_permission()` folds the grants into a tier: `owner`, `config` (edit),
`read_only`, or `None`. Two loaders build on it:

* `load_visible_presentation()` — may READ. Evaluated in SQL through
  `user_visible_filter`, so an invisible deck is indistinguishable from a
  missing one. No admin bypass here on purpose: this is the loader behind the
  agent tools, which act for a user from inside a conversation.
* `load_writable_presentation()` — may EDIT through an agent: owner or
  `config` grantee (directly or via a group); a company-managed deck only by an
  admin. A `read_only` grantee gets `None` exactly like a stranger, and the
  tool says the deck cannot be edited (`get_presentation` tells them to
  duplicate instead). No blanket admin bypass: an admin's agent must never
  rewrite a colleague's artifact from a chat — the REST surface, where the
  admin acts in person, is where `can_manage` applies.

REST (`api/v1/presentations.py`): `GET /presentations` (visible decks, summary
rows with `can_manage` — the three-tier predicate for EVERYONE, admins
included: the list is what the library page shows behind its `Mine` /
`Shared` / `Company` pills, and `is_admin=True` would degenerate it to
`WHERE true`, listing every user's private deck under pills that cannot
account for them; an admin finds a colleague's deck in Settings > Objects
instead, whose union now carries presentations — by `title` — and template
containers), `GET /{id}` (the by-id reads keep the admin bypass, all of them through
`_load_visible`, so a deck reached from Objects opens — the same LIST-strict /
DETAIL-open split every other entity API makes), `PATCH /{id}` (sharing fields and/or the
edited PIECES — `title`, `theme`, `transition`, `aspect_ratio`, `chrome`,
`slides`, plus `template_container_id` to re-link — merged into the stored spec
and re-validated exactly like an agent call; `expected_revision` mismatches are
a 409 carrying `current_revision`, field errors a 422 whose `detail` is
`PresentationSpecError.human_message()` — `Slide 4 (title): subtitle - ...`,
one line per problem, written for the person behind the editor and never the
agent wording; never a raw spec),
`DELETE /{id}`, `POST /{id}/duplicate` (a copy the caller owns, revision 1, no
grants, template link kept, no conversation), `POST /{id}/export`. Sharing
PATCHes never carry the spec and never bump `revision`, so opening the share
dialog while a collaborator's agent is mid-edit cannot clobber their slides.

The agent's counterpart of `POST /{id}/duplicate` is the
`duplicate_presentation` tool, for a recurring deck (the quarterly
update) that must keep its approved layout instead of being generated again.
Both call ONE service, `presentations.duplicate_presentation()`, so what a
copy keeps (the stored spec - slides with their ids and notes, theme, aspect
ratio, chrome - and the template link) and drops (grants, comments, history)
cannot differ between the library and the chat. The tool:

* loads the source with `load_visible_presentation()` - READ is the bar, as in
  the UI (a `read_only` grantee, a company-managed deck), with no admin bypass,
  and an unreadable deck gets the unknown id's answer;
* takes a required `title` (the deck title only: the opening slide's own title
  and subtitle are slide text, changed afterwards with `edit_presentation`);
* binds the copy to the turn's conversation or assignment run (the service's
  optional `conversation_id` / `assignment_run_id`; the library copy passes
  neither), writes the `create` audit entry with `via: agent`, emits
  `presentation_saved` after the commit;
* renders the layout check only when the new title is drawn on every slide
  (`template_chrome_redraws(chrome, ["title"])`, the edit tool's rename rule):
  otherwise the slides are byte-identical to a deck already measured;
* returns the outline with slide ids, the `Template:` and `Footer:` lines (the
  footer read with the NEW title) and the new deck's marker, so the agent goes
  on with `edit_presentation` on the copy.

It is also the way out of a read-only deck: the refusals of
`generate_presentation(presentation_id=...)` and `edit_presentation` on a deck
shared read-only, and `get_presentation`'s access line for such a reader, name
`duplicate_presentation` and an edit of the copy. They used to say to generate
a new deck from the content read back, which re-emitted every slide.

Frontend: `/presentations` (library, newest change first, `Mine` / `Shared` /
`Company` filter, a page at a time - see "The library, a page at a time"
below), the viewer toolbar (`Edit slides` panel for reorder / delete
/ title / subtitle / notes / layout / theme / transition with the 409 reload flow,
`Share`, `Studio` — the chat rail bound to the deck, see "Presentation Studio"
below; `Edit with my assistant` on the library card lands there too — `Export`,
`Duplicate`, `Delete`), and the chat card. The viewer polls the deck while open
(`usePresentation(id, { live: true })`) because a collaborator's edit is made
from THEIR chat, which no chat stream of ours observes; an edit made from the
studio rail (or the main chat) refreshes the moment it is committed — see
"Live refresh" just below.

### The library, a page at a time

Agents keep adding decks, so the library is never read whole. `GET
/presentations` answers a page, `{items, total}`: `limit` (24 by default,
`LIST_PAGE_SIZE`; at most `MAX_LIST_RESULTS`) and `offset`, the last changed
first (`updated_at DESC, id DESC`, so no deck shows on two pages), and `total`
counts every deck the filters match. Before, the list stopped at the 100 most
recently updated and searched only those.

The filters run in SQL, on the whole library:

* `visibility` (`all` / `mine` / `shared` / `company`) and `search` (every
  word, any case, in the title, the owner's name or the name of a template the
  reader can see) are `services/library_filters.library_filters()`, the rule
  the Video Studio's Videos tab applies too: never re-spell it for a library.
  Like every library list, it is three-tier for an admin.
* `kind` (`deck` / `page`), `conversation_id` and `mine` (the caller's own,
  published ones included) stay the route's.

The page (`pages/Presentations.tsx`) keeps the search, the visibility filter
(`vis`), the kind (`kind`) and the page (`page`) in the URL through
`useLibraryParams()` (`hooks/useLibraryParams.ts`, shared with the Video
Studio), sends the search once the typing settles, starts a new search or
filter from the first page, and shows the shared `Pagination` bar above and
below the grid. `useClampPage()` goes back to the last page when the one shown
empties. The Work hub reads the first six decks and shows the `total`.

### Sharing through the agent

`share_presentation` (the `generate_presentation` suite, offered on every
turn) lets an assistant share a deck it built: "share the Q3 review deck
read-only with the EMT" is one call. Its arguments are `presentation_id`,
`users` (user ids or emails), `groups` (ids or exact names), `permission`
(`read_only` | `config`) and `action` (`add` | `remove` | `replace`).

* **One write.** The sharing block of `PATCH /{id}` moved to
  `services/presentation_sharing.apply_sharing()`, and the tool calls it with
  the lists it planned (`plan_sharing()`), so the Share dialog and the
  assistant apply the same rules: canonical ids, existing accounts, an active
  account for a new grant, known tiers (`apply_user_share_fields`), and
  publication admin-only (`handle_company_managed_toggle`). The tool never
  sets `is_company_managed` and refuses a company-managed deck: its audience
  is managed by admins from the dialog.
* **As the calling user.** The deck is loaded through
  `load_visible_presentation` (no admin bypass, like every deck tool), and
  only the owner or a `config` grantee (`MANAGE_TIERS` through
  `agent_deck_permission`) may share; a `read_only` grantee is refused in
  words, a stranger gets the not-found answer.
* **Existing accounts only.** `resolve_principals()`
  (`services/share_principals.py`, the reading the agent builder's per-user
  agent sharing shares) matches a user id, or an
  email case-insensitively (an `Ada <ada@x.com>` form is read for its
  address), and a group id or exact name. One entry that matches nothing
  refuses the WHOLE call and is named: sharing with nine of the ten people
  asked for is the failure an access change must not have, and the tool
  invites nobody. Granting a deactivated account is refused the same way.
* **add / remove / replace.** `add` grants the tier asked for, and changes the
  tier of someone already shared with; `remove` revokes (the tier is not
  read); `replace` makes the audience exactly the people and groups named,
  every other grant of either kind removed. The owner named is reported and
  never granted. A call that changes nothing writes nothing.
* **Lock order.** A user deletion (`prune_user_grants`) locks the account,
  then every deck naming it; `normalize_user_grants` locks the accounts a
  write names. `lock_for_sharing()` therefore locks the named and granted
  accounts first (`FOR KEY SHARE`, id order), then re-reads the deck
  `FOR UPDATE`, and the tool re-checks the caller's tier on that row before
  planning: a concurrent change to the sharing, or to the caller's own grant,
  is what the call plans from.
* **Audit and report.** One `log_audit` row per call that changed something
  (`entity_type` `presentation`, action `update`, details `via: agent`, the
  action, the permission, `added_` / `removed_` / `retiered_` users and
  groups). The result lists who was given access, changed, removed, already
  had it or was not shared with, then the `Shared with:` line.
  `get_presentation` prints that line too (`audience_line()`: names, emails,
  `can view` / `can edit`, a deactivated account flagged; no query for a deck
  no grant names), so "who has access?" is answered from the grants.
* **When.** The description says to share only when the user asks and to name
  who gets or loses access before calling. Unlike `export_presentation`, the
  tool is not gated on the request text: the turn that confirms a proposed
  audience ("yes, go ahead") carries no sharing word.

The chat refreshes the deck queries on the tool's `tool_done` frame, so an
open Share dialog or library card shows the new audience.

### Public link

Everything above keeps a deck behind its grants: its address alone opens
nothing. A **public link** is the one deliberate exception - a second address,
a capability URL, that opens the deck read-only for anyone who holds it,
without an account (a customer, a board, a conference audience). One module
owns it, `services/presentation_public_links.py`, and both surfaces that
change it call it: the Share dialog (`GET` / `POST` / `DELETE
/presentations/{id}/public-link`) and the `set_presentation_public_link`
tool.

* **The record.** `PresentationPublicLink` (`presentation_public_links`), its
  own table so the deck row stays a deck: at most one per deck
  (`presentation_id` unique, `ON DELETE CASCADE`), so a deleted deck takes its
  link with it and a duplicate or a package never carries one (neither copies
  the table). `regenerate` rewrites the token in place (`created_at` /
  `created_by_user_id` then name the CURRENT token); `disable` deletes the row.
  Enabling upserts on `presentation_id` (`ON CONFLICT DO NOTHING`, then reads
  the winner), so two managers enabling at once are handed the same link.
* **The token.** `secrets.token_urlsafe(32)` - 256 bits, 43 URL-safe
  characters (`well_formed_token`, checked before any query). A public request
  is looked up by `security.hash_token` (SHA-256, the invitation idiom:
  `token_hash`, unique); the token itself is stored Fernet-encrypted
  (`token_enc`, invariant 2) so a manager can copy the link again. A
  `SECRET_KEY` that no longer decrypts it leaves the link working (the hash
  still matches) but unshowable: the dialog and the tool say to replace it.
* **Who.** The deck's managers, exactly the sharing rule: REST through
  `_load_manageable(action="share")` (owner, `config` grantee, an admin as on
  every REST write - and only an admin on a company-managed deck); the tool
  through `load_visible_presentation` + `agent_deck_permission` in
  `MANAGE_TIERS`, no admin bypass, and like `share_presentation` it refuses a
  company-managed deck outright (its audience is the admins', from the
  dialog). A `read_only` grantee is told the deck is public
  (`has_public_link` on both deck shapes, every reader) and never reads the
  link: `GET /{id}/public-link` is a 403 for them.
* **The admin switch.** `presentation_public_links_enabled`
  (`PRESENTATION_PUBLIC_LINKS_ENABLED`, on by default; a security option in
  Settings > Security, control `access.presentation_public_links`, hardened
  `false`) is read on EVERY public request (`resolve_public_presentation`):
  off, every link is the unknown token's 404 at once and `POST` is a 403
  (`PUBLIC_LINKS_OFF_DETAIL`); `DELETE` stays allowed, taking a link down is
  never refused. The rows are kept, so turning it back on restores what
  nobody turned off. `has_public_link` reads `false` while it is off - a
  card must not call a deck public that no link opens.
* **What a link serves.** `api/v1/presentation_public.py`, mounted under
  `/api/v1/public` (the public CORS layer applies; `NoIndexMiddleware` marks
  it `noindex` like every response). `GET /public/presentations/{token}` is
  `public_presentation_payload`: the deck resolved as its OWNER reads it
  (`resolve_template_palette(user_id=owner)` - an owner who can no longer see
  the template, or a deleted owner, gives the built-in look, the owner's own
  answer), its quote pictures labelled by description only (never a
  filename), served through `PublicPresentationResponse` - a WHITELIST: no
  owner, editor, grant, conversation or comment field, and a field added to
  `serialize_presentation` later stays out until listed - with the spec
  minus every slide's `notes` (`public_spec`). `GET
  /public/presentations/{token}/assets/{file_id}` serves a file iff
  `public_asset_ids` lists it: the shown spec's structural references plus
  the logos of the template palette in EVERY scheme (a slide may override
  the deck's theme), the same walk `render-asset` uses
  (`referenced_file_ids`) - a link is a capability over one deck's pictures,
  never a file-read credential, and it trusts the stored references the way
  the deck-reference grant does. No `authorize_file_access`: the
  token is the grant, chosen by a manager.
* **One answer for every refusal, never cached.** An unknown, regenerated or
  disabled token, a deleted deck and the switch off are the same 404; a file
  the deck does not show and a malformed id (`parse_path_id`) the unknown
  file's. Every answer carries `Cache-Control: no-store` (the bytes through
  `file_response_headers(store=False)`) and `Referrer-Policy:
  no-referrer`, so a link turned off stops at once, shared caches included,
  and the token never rides a slide's outbound link. Rate limits per client
  address are generous on purpose (`PUBLIC_DECK_RATE_LIMIT` 300 / min,
  `PUBLIC_ASSET_RATE_LIMIT` 3000 / min): a room behind one NAT address opens
  the link together and every page fetches every picture.
* **Audit.** One `log_audit` row per change (`entity_type` `presentation`,
  action `update`, details `public_link`: `enabled` / `regenerated` /
  `disabled`, `sharing_changed`, and `via: agent` from the tool); handing
  back the existing link writes nothing.
* **The page.** `/public/presentations/:token` (`pages/PresentationPublic.tsx`)
  sits outside `ProtectedRoute` and `AppLayout`, and wins over the `/public/*`
  catalog redirect by specificity. It loads the deck with a plain `fetch`
  (`usePublicPresentation`, no retry on a 404), maps it to the stage's shape
  (`lib/public-deck.ts` `publicDeckAsPresentation`: nobody owns it, nothing to
  manage), resolves every asset to the token-gated route through
  `DeckAssetProvider`, and renders `DeckStage` (or `PageDocument` for a page)
  with no actions and `presenterTools={false}`: no speaker bridge, view,
  handout or `S` (no notes are served, and both windows are session pages).
  Slide fragments work as in the viewer. A browser that still holds a DEAD
  session is not bounced to sign-in by the providers' `/auth/me`: the 401
  handler of `lib/api.ts` drops the session on a public deck page
  (`isPublicDeckPath`) instead of redirecting.
* **The dialog.** `PublicLinkSection` sits last in the Share dialog, apart
  from the grants draft: its switch applies at once, turning it off and
  `Replace link` ask first (both break a link people hold), and while the
  switch is off platform-wide it says so instead of offering a refused
  toggle. The library card carries a globe for every reader while a link is on.
* **The tool.** `set_presentation_public_link(presentation_id, action)`,
  `enable` (default; hands back the existing link unchanged) / `regenerate` /
  `disable`, people's words read too (`normalize_public_link_action`: `on`,
  `rotate`, `revoke`, ...). Refused before any query while the switch is off
  (`disable` excepted). The result carries the ABSOLUTE URL
  (`public_link_url`: `FRONTEND_URL` or `BASE_URL`, like every link the
  platform sends out) and the sentence the agent repeats - anyone with the
  link can view it without signing in, slides only. Like
  `share_presentation` it is offered on every turn, not gated on the request
  text (the turn confirming "yes, make it public" names no link), and its
  description tells the agent to call it only when asked and to say who can
  open it; `share_presentation`'s description points people without an
  account to it. `get_presentation` prints `Public link: on - ...` for a deck
  that has one (one indexed read; nothing for a deck with none), and the chat
  refreshes the deck queries on the tool's `tool_done` frame.

Tests: `tests/test_presentation_public_links.py` (token shape and hashing, no
query for a malformed token or with the switch off, the notes-free whitelist
payload resolved as the owner, the tool's enable / keep / regenerate /
disable / refusals and audit rows, the `get_presentation` line) and
`tests/test_presentation_public_links_postgres.py` (end to end against
PostgreSQL: the owner turns it on through REST, an anonymous client opens the
deck and its picture but no other file and no note, a read-only grantee
cannot manage it, regenerate kills the old URL, the switch hides every link
and refuses new ones, the assistant turns it off, deleting the deck deletes
the link); `frontend/src/lib/public-deck.test.ts`.

#### PDF download

A public link may also offer the PDF of what it shows. Off by default: many
decks are meant to be shown, not handed over.

* **The option is the link's.** `PresentationPublicLink.allow_pdf_download`
  (migration `p5430pdf01`, a constant-default boolean, metadata-only). Set by
  the deck's managers with `PATCH /presentations/{id}/public-link
  {"allow_pdf_download": bool}` (`set_pdf_download`: `None` for a deck with no
  link - a 404 - `False` when unchanged, one audit row `pdf_download_enabled`
  / `pdf_download_disabled` when it changed; turning it on is refused while
  the admin switch is off, turning it off never is) or by the
  `set_presentation_public_link` tool's `pdf_download` (read by
  `normalize_download_flag`: a boolean, or `true` / `off` and the like; a
  guess is refused before any query; ignored by `disable`; its audit row
  carries `via: agent`). A regenerate keeps it (the rotating `UPDATE`
  returns it): replacing a leaked link must not change what its next holders
  are offered. Turning the link off deletes the row, so a link turned on
  again starts without it. `get_presentation` and the tool's result say
  whether visitors may download.
* **What is printed.** `GET /public/presentations/{token}/pdf` renders the
  STATIC page at `/public/presentations/{token}/static` (`PresentationStaticRender`
  in its public mode: the deck through `GET /public/presentations/{token}`,
  the pictures through the public asset route, with the link's own token) -
  exactly what a visitor of the link sees, no speaker notes, no account's
  visibility borrowed and no render token minted. Same paper and print
  scale as the authenticated export (`_print_pdf`). The payload's
  `pdf_download` tells the public page to show its `Download PDF` button.
* **Bounded, because anyone may ask.** `presentation_public_links.public_pdf`:
  the PDF is cached per worker under `pdf_fingerprint` - the SHA-256 of the
  whole public payload, so any visible change (an edit, a template palette,
  a logo replaced in place, whose geometry carries its storage key) is a new
  key and never a stale file; a picture replaced in place under the same id
  is the one change it cannot see, and shows within
  `PUBLIC_PDF_CACHE_SECONDS` (600). The cache is an LRU capped at
  `PUBLIC_PDF_CACHE_MAX_BYTES` (64 MB); a PDF over
  `PUBLIC_PDF_MAX_CACHED_BYTES` (16 MB) is served, not kept. Concurrent asks
  for one fingerprint wait on one lock (single flight), so a room
  downloading at once costs one render. A miss is counted against the
  link's render budget (`PUBLIC_PDF_RENDERS_PER_LINK`, 10 per 10 minutes,
  Redis, bucket = the link id) and a download against the client address
  (`PUBLIC_PDF_DOWNLOADS_PER_ADDRESS`, 60 per 10 minutes); past either, a 429.
  The render runs in the worker's ONE public render slot
  (`MAX_CONCURRENT_PUBLIC_RENDERS`), taken before one of the
  `MAX_CONCURRENT_RENDERS` shared slots, so anonymous downloads never hold
  the slot an authenticated export waits for. The pooled DB connection is
  released before the wait.
* **Answers.** 404 for an unknown or dead link and for a link that offers no
  download (`PDF_DOWNLOAD_OFF_DETAIL`); 429 past a budget; 502 when the page
  could not render and 503 when the platform cannot render at all, both with
  `PDF_UNAVAILABLE_DETAIL` (the operator detail goes to the log, never to an
  anonymous visitor); every answer `no-store` and `no-referrer`, the file
  under `export_filename()`.
* **The UI.** The Share dialog's `Allow PDF download` switch sits under an
  active link and applies at once (`useUpdatePresentationPublicLink`); the
  public page's `Download PDF` button (`useDownloadPublicPresentationPdf`, a
  plain `fetch`, saved with `saveBlobAs`) spins while the render runs and
  says in words why a refusal happened.

Tests: `tests/test_presentation_public_pdf.py` (the option's lifetime and the
tool's argument, one render for a room, a new render for any visible change,
the fingerprint, the byte-capped LRU, expiry, the render printed from the
public page in one public slot) and the second test of
`tests/test_presentation_public_links_postgres.py` (end to end, the headless
browser stubbed); `frontend/tests_e2e/presentation-public-link.spec.ts`
downloads a real PDF.

### Switching a slide's layout from the editor panel

Each row of the `Edit slides` panel's SLIDES list carries a layout picker in
place of the old static badge. Three questions had to be settled before it
could exist, and their answers are the design:

* **Where does the list of layouts come from?** The platform has ONE layout
  catalog, `SLIDE_LAYOUTS` in `presentation_spec.py` (`title`, `section`,
  `agenda`, `content`, `dashboard`, `roadmap`, `takeaways`, `closing`; there is
  no legacy half - a stored v1 slide is upgraded when the deck
  is read, see "Retired layouts"). A template's `layouts` column is an ADVERTISED
  subset of that catalog, and it is advisory: it says which layouts the
  designer had in mind, it does not add layouts the renderer lacks and it does
  not make a slide on another layout invalid. So every deck response carries
  `supported_layouts` (`template_supported_layouts()` in `presentations.py`:
  the template's advertised ids restricted to `SLIDE_LAYOUTS`, in catalog
  order; the WHOLE catalog when the template advertises nothing renderable),
  resolved live off the linked template like the palette and the chrome, and
  the picker offers exactly that list.
* **What about a deck without a template?** The whole catalog. A free deck is
  not blocked from switching layouts - the layouts are the platform's, not the
  template's - and a template that says nothing about layouts restricts
  nothing. The frontend mirrors the rule in
  `components/presentation/layout-options.ts` (`SLIDE_LAYOUTS`,
  `supportedLayouts()`, pinned to the backend list by
  `test_frontend_layout_options_mirror_the_backend_catalog`) for a template
  being TRIED in the Design section before its deck response exists.
* **What does "changing the layout" do to the slide's content?** Layouts are
  different Pydantic models with different fields, so a picker that only
  rewrote `layout` would produce a slide `validate_spec` refuses. The
  conversion is SERVER-SIDE and deterministic:
  `POST /presentations/{id}/slide-layout` (`presentation_layout_switch.py`,
  `switch_slide_layout()`) takes ONE slide as the panel's draft holds it plus
  the target layout, reads the slide into a layout-neutral source (title,
  subtitle, notes, speaker, footer, list items, visual blocks, the layout's
  own defaulted title kept apart), rebuilds it as the target and re-validates
  it through the target model. The account comes back in the person's words
  as `kept` / `derived` / `dropped` - `Title`, `3 items from the 4 blocks`,
  `1 visual block (image) - an agenda slide lists words` - and the panel puts
  the converted slide into its draft (the stage previews it at once) and reads
  the account in a toast. The slide is found again by IDENTITY, never by id or
  position: a reorder keeps the object, so the result lands where the row
  went, while an edit to that slide, a Discard, a Save or an adopted revision
  replaced it meanwhile - a result built from the older slide is then dropped,
  and said so, never applied over the newer draft. Nothing is written: the
  save is the ordinary PATCH
  with its `expected_revision`, so a conversion that is not what the person
  wanted costs a Discard, never a revision. A slide that cannot HONESTLY
  become the target is refused with a 422 and the reason (`This title slide
  has nothing to list; an agenda slide needs at least 2 items.`), never padded
  with placeholders. The route converts between the eight layouts of the
  catalog and nothing else (a `dashboard` target, reads its KPI tiles
  off the body's metrics and its panels off the chart / table figures, and is
  refused when the body has no figure to show - no placeholder figure is
  invented): a retired v1 layout is refused as a TARGET with the reason, and a slide the panel still holds in a v1 shape (a draft opened
  before the deck was re-read) is first upgraded by `presentation_legacy` and
  the account says so - so the conversion itself never meets one. A defaulted title (`Agenda` on an agenda slide nobody titled)
  is the model's word, not the person's: it is not carried into a layout that
  has its own default, and only kept, said so, where the target has none. The
  defaults it tells apart are read off the models themselves
  (`_DEFAULT_TITLES`), so a renamed default cannot slip past the check; and a
  title read off the first heading block is not ALSO listed as the first item
  - the paragraph under that heading is listed on its own, so nothing is lost
  and nothing is said twice. The agenda's grid options travel the same way:
  `columns` is carried between agenda and takeaways when the target
  accepts it, and `3` is carried INTO an agenda only when the slide holds 6
  to 9 items - otherwise the account drops it (`3 columns - an agenda grid
  needs 6 to 9 items and this slide lists 4`) rather than letting the target
  model refuse the whole conversion; a `style` that OVERRODE the look
  (`_agenda_style_override`: `grid` on one or two columns, `list` on the
  3-column grid - the columns, explicit or auto, decide what "override"
  means), `flow: column` or `distribute: false` leaving the agenda is listed
  as dropped in the person's words, and the defaults - including a `style`
  that only restates what `auto_agenda_style` draws for those columns - are
  dropped silently because they said nothing.

The picker lists the slide's OWN layout always - a layout the template does
not advertise stays listed and selectable on the slide that wears it - so it
never shows a layout the slide is not. The route is gated
on the manage tier (`_load_manageable(..., action="edit")`) like the PATCH,
runs no query beyond that and commits nothing.

### Sharing the deck shares the pictures it shows

A picture an agent generates for a slide (`generate_image`, or a chat upload
the agent placed) is a `FileAttachment` OWNED by the person whose agent
produced it and bound to THEIR conversation. Sharing the deck shares the spec,
and the spec only NAMES the file — every surface then fetches it through
`GET /chat/files/{id}/download`, the viewer, the chat card, the speaker window
and the export's `render-asset` alike, and all of them end in
`authorize_file_access` (`services/file_access.py`). Formerly that
contract knew owner / admin, the conversation- and run-scoped fallbacks (both
only for a file with NO owner), and the template-asset path — nothing said
"this file is shown by a deck the requester may read", so every other reader
of a shared deck got a 403 on every generated image and a slide with a hole
where the picture was. Brand logos never failed, because a template asset is
granted through its CONTAINER's visibility.

The fix is one more path in the same contract, tried last: a file is readable
by whoever may read a presentation whose STORED spec names it on a slide.
Three properties make it safe and cheap:

* **The grant is the deck's own three-tier visibility.** The probe is
  `user_visible_filter(Presentation, user_id, group_ids)` — the exact
  predicate behind the library list — so an invisible deck grants nothing,
  and fetching a picture a slide shows is no more privileged than reading the
  slide. No admin bypass is needed: an admin already reads every file.
* **A reference is structural, never a mention.** SQL narrows the readable
  decks to those whose `CAST(spec AS TEXT) ILIKE` the id — in both spellings
  a UUID may take, hyphenated and `hex`, case-insensitively; Python then
  confirms with `spec_references_file`, the same `*_file_id` / `file_id` walk
  over `slides` that `verify_file_references` vouched for when the reference
  was written. An id pasted into a text block's prose matches the pre-filter
  and grants nothing. The scan is EXHAUSTIVE: candidates are read
  `PRESENTATION_SCAN_PAGE_SIZE` (25) at a time in id order (keyset, so a save
  landing mid-scan cannot shift a page) and the walk stops at the first deck
  that names the file — a page is a round-trip size, never a cap on
  correctness, so a picture one deck shows is granted however many other
  readable decks merely mention its id. Only the CURRENT spec is consulted,
  never the version history: a snapshot that still names a picture the deck
  has since dropped does not keep it readable, so the History panel's preview
  of that slide shows the owner the image and a reader a hole — a bounded,
  documented gap chosen over multiplying the scan by the retained history.
* **Evaluated on read, so it is retroactive.** Nothing is stored, migrated or
  backfilled: a deck shared before the fix serves its pictures the moment the
  fix is deployed, with no re-save by its author.

The scan runs only on the last-resort path — the owner, an admin, the
conversation- and run-scoped files and the template assets all answer before
it — and it is scoped to the reader's own visible decks, so its cost is
proportional to how many decks that person can see, not to the table. The
one caller that fetches EVERY picture of a deck in a row, the static render
page behind the export and the post-save layout check (`render-asset`),
passes the deck it renders as `authorize_file_access(..., presentation_id=)`:
the contract tries that deck FIRST, by primary key under the same visibility
predicate, and only a miss falls through to the scan (a hint never refuses on
its own, so a wrong hint costs a query, not a picture). The interactive
surfaces fetch through `GET /chat/files/{id}/download` with no deck in hand
and pay one scan per picture per page load; a reverse-reference table written
at save time is the follow-up if that ever shows in a profile. For scale: the
pre-filter is a sequential `ILIKE` pass over the text of the reader's visible
decks - a few dozen decks of a few tens of KB is milliseconds, and it grows
linearly with both, so at ten thousand visible decks of 200 KB it is a 2 GB
read per picture and that table stops being a follow-up. A positive cache
keyed on (user, file) inside `authorize_file_access` is NOT that follow-up:
`verify_file_references` calls the same function, so a cached "yes" would let
a writer re-introduce a picture whose deck was un-shared seconds earlier for
the cache's whole lifetime.

**What the grant trusts, and the one thing it cannot check.** The grant reads
the STORED spec, and a stored reference is trusted because the write that
introduced it was verified against its writer. That is true of every current
write and of every earlier agent write (`generate_presentation`, the
package import and `edit_presentation` always verified); it was NOT true of
the REST `PATCH` and the version restore of older releases, which saved without
`verify_files_for`. A deck row written through those two paths by such a
release may therefore carry a file id its writer could not download, and the
deck-reference path would now serve that file to the deck's readers. No
backfill can sort those rows: the spec records no provenance (who introduced
which reference), and re-checking every stored reference against the deck's
OWNER would strip the legitimate pictures a `config` grantee's agent placed —
the collaboration case, and the bug this section exists to fix. What such a
row requires is a UUID the editor could not read, learned out of band, and
pasted into a slide field by hand at a time when doing so granted nothing
(the editor got the same 403 as everyone else): a random 122-bit id with no
payoff at the time. The exposure is bounded to those rows and is accepted,
and recorded here so it is a known residual rather than a surprise; a
reverse-reference table carrying `vouched_by` at save time is the design that
would retire it, together with the per-picture scan above.

Two things narrow that residual further, and one consequence follows from
the current-spec rule. First, the grant follows the DECK, not the picture's
owner - the scan never consults `file_attachments` - so a picture a third
party generated is served to the deck's readers exactly like the owner's own
(pinned by `test_the_grant_follows_the_deck_not_the_picture_s_owner`).
Second, the only in-band surface that ever handed a non-admin the id of a
file they could not read was a deck - or a retained revision of one - they
could read at the time, which the grant now covers by design; an older
`PATCH` or restore therefore added nothing its writer could not have added
through the verified tool while that deck was visible, and the residual is
the out-of-band UUID above and nothing else. The consequence: a `config`
grantee restoring a revision that brings back a picture the deck has since
dropped is refused when they cannot read it (the restore verifies what it
re-introduces against the restorer), while the owner can.

What keeps the grant honest is the OTHER half of the file contract, hardened
in the same change: every write that can INTRODUCE a reference vouches for it
against the writer, so a foreign file id typed into a deck is refused before
it is stored and can never be laundered into a grant. `generate_presentation`
and the package import already verified the whole spec and `edit_presentation`
its written slides (`verify_files_for=user_uuid`); the REST `PATCH` (an editor
pasting an id into a slide field) and a version RESTORE (bringing back a
reference the deck since dropped) now do too, passing the stored spec as
`verify_baseline` so only what the write ADDS is checked — a reference the
deck already carried was vouched for when it was written, and re-checking it
against a `config` grantee who cannot read the owner's upload would refuse an
edit that touches no picture. A restore therefore needs the acting user
(`restore_presentation_version(..., user_id=...)` raises without one). A
duplicate copies the spec verbatim and verifies nothing: the copy is a deck
the duplicator could already read, so it names nothing new to them.

Tests: `test_presentations.py` ("Files a readable deck shows") pins
the production failure, the structural-not-textual rule, the shape of the
scan (three tiers, both spellings, page size, id order), that it walks past a
full page of mentions to the deck that shows the file, that the hinted deck is
tried first by primary key and a miss falls through, that the owner and an
admin never reach it, that the grant follows the deck and not the picture's
owner, that no deck-level field can name a file (the `slides` walk is the
whole walk and agrees with the export's render allow-list), and - through the
real route, verifier and authorizer - that the REST edit and the restore vouch
for what they introduce; `test_presentation_export.py` drives a read-only reader's
picture through `render-asset` and checks the grant is the hinted deck, not a
scan; `test_agent_generated_files.py` keeps the orphan-file refusals.

### Live refresh

The stage refreshes on the COMMIT of a revision, not on the return of the tool
that wrote it. The two are far apart: both write paths commit and then run the
post-save layout check synchronously before returning — a fresh
headless Chromium, the full render, up to `LAYOUT_CHECK_TIMEOUT_SECS`, queued
behind the export render slots (`MAX_CONCURRENT_RENDERS`) — and the loop only
emits `tool_done` once the handler has returned. Gating the refresh on
`tool_done` (the first cut) left every saved revision invisible for the whole
render, so a one-word `edit_presentation` was displayed no sooner than a full
rewrite, and the 15 s poll was in practice what the studio's "real time" meant.

The fix is a channel for a tool to speak from INSIDE its execution
(`app/tools/tool_events.py`): the loop binds a sink per tool call — a
`ContextVar`, like the Langfuse tool span, so a parallel batch keeps each call's
events on its own call — and `emit_tool_event(PRESENTATION_SAVED_EVENT,
presentation_id=..., revision=...)` is called right after `own_db.commit()` in
`generate_presentation` and `_edit_presentation`, BEFORE `layout_check_note`.
The loop forwards it as `{"status": "tool_progress", "tool": <LLM-visible
name>, "tool_call_id": <id>, "event": "presentation_saved", ...}` on the same
`on_event` every status frame rides (web chat SSE, platform SSE, consult wrapper),
and `useChatApi.ts` (`handleToolProgress`) invalidates the `["presentations"]`
queries on it — top-level and nested under `consult_progress.inner`, keyed on
the EVENT rather than the tool name so an aliased tool name cannot hide a save.
The frame is data, not agent state: it never touches the status bubble. The
`tool_done` invalidation stays as the fallback (a `tool_progress` frame lost to
a reconnect), and the poll stays for the surfaces with no chat stream of their own.

`emit_tool_event` never raises and is a no-op outside a bound call (a REST
route reusing a tool function, a handler under test): a progress signal is
never worth failing the work it reports on. The loop owns the tagging keys — a
payload cannot override `status` / `tool` / `tool_call_id`.

Two more guarantees sit at that single funnel, because the frame rides every
consumer and the consumers differ in where they serialise it. The loop's
`finally` CLOSES the binding (`ToolEventBinding.closed`) rather than merely
unbinding it: a task the handler spawned inherits the binding through its
copied context and may outlive the call, and its late report is a silent no-op
instead of a frame tagged with a call that already finished. And the payload is
made JSON-safe once, in `emit_tool_event` (`json.dumps(default=str,
allow_nan=False)` round-trip, one frame bounded by
`MAX_TOOL_EVENT_PAYLOAD_CHARS`): a `UUID` arrives as its string form, while a
payload that still cannot be serialised - or is oversized - is dropped with a
warning naming the tool and the event, never raised into the handler. The web
chat's runner would have caught a bad value inside `_emit` and
lost the frame; the platform SSE proxy serialises in its response generator,
where the same value would have ended the stream. A frame is a pointer to state
the consumer refetches (an id, a revision), never the content itself.

### Slide links: the agent points at what it changed

A saved edit used to end in a receipt — `Saved` — and a reply that said
"done": the tool result carried no address a link could be built from, so the
user was left to find slide 4 by hand. Every write now names the slides it
wrote by their ADDRESS, `/presentations/<deck id>#/<slide id>`, anchored on
the slide's STABLE id (`SLIDE_ID_PATTERN`, URL-fragment safe by construction)
rather than its position — a position is stale the moment a collaborator's
insert lands, an id follows the slide. The all-digits form `#/<index>` stays
what the viewer always wrote (0-based, reveal's convention) and is tried after
the id lookup fails (`resolveSlideRef`: id first, then index clamped to the
last slide).

Writer side (`builtin_tools/presentation.py`): `_render_edit_account` gives
every `Changed` / `Inserted` / `Moved` entry its `link:` and closes with one
line telling the agent to put it in the reply (`[slide 4](/presentations/…#/…)`
— the one clause folded into `_EDIT_DESCRIPTION` (`each with its link: put
the link in your reply`) says the same, so the behaviour is carried by the
tool result, not by a system prompt), and the
`get_presentation` head states the shape once so an agent can link a slide it
merely read about. The result's trailer is `focus_marker(deck_id,
focus_slide_id(outcome))` — `[[PRESENTATION:<id>#/<slide id>]]`, the focus
being the first changed, else the first inserted, else the first moved slide,
and the plain marker for a deck-field-only edit, a delete or a no-op — and the
same focus rides the `presentation_saved` frame as `focus_slide_id` (only when
set). The channel resolver (`_PRESENTATION_MARKER_RE`, group 2) keeps the
fragment on the absolute viewer link it builds, so a Slack or Teams user lands
on the slide too. Links are relative on purpose: the web chat resolves them
against its own origin, and the channel rewriter is the one place that knows
the public base URL — which is why the SAME pass absolutizes the relative
slide links the agent pasted into its reply (`_relative_viewer_link_re`: an
in-scope deck's `/presentations/<uuid>#/<slide>` becomes the absolute URL,
fragment kept, bare or inside a Markdown link; an out-of-scope path is left as
written, a path already inside an absolute URL is never touched, and no scope
means no rewrite). Without it a Slack reply would carry the marker's working
link next to a `[slide 4](/presentations/…)` that resolved nowhere.

Reader side — one shape (`slide-links.ts`: `parsePresentationLink` accepts
only a same-origin viewer path, `parseSlideFragment`, `resolveSlideRef`), four
surfaces:

- **The reply's link, in place.** `ChatMarkdown`'s `MarkdownLink` intercepts a
  plain left click (no modifier, not already handled) on a slide link and asks
  the nearest `PresentationCardContext.navigateToSlide`; if a stage takes it
  the click is `preventDefault`ed, otherwise the link keeps its default
  `target="_blank"` and the viewer opens on that slide. The studio rail answers
  for ITS deck (the parent context wins), the transcript keeps a registry of
  its mounted inline cards (`registerSlideStage`, one stable registration per
  mount, the newest card of a deck asked first and a card whose stage is not
  up yet passing to the one before it) for every other deck.
- **The card.** `PresentationCard` receives `focusSlideRef` from the marker
  (`extractOrderedPresentationRefs`) and opens on that slide once its
  controller is ready — once, so a user who moved on is not dragged back by a
  refetch; a registered card also scrolls itself into view when a link lands
  on it. In the studio the card is a receipt: `Saved - slide 4 changed.` with
  a `Go to slide 4` action that moves the stage.
- **The stage follows the agent's pen.** The rail passes `onToolProgress` to
  `useTurnFrames` (through `StudioRail`, the SAME frame `handleToolProgress`
  refreshes the deck on — the two can never key on different events) and
  remembers `{revision, focus_slide_id}`; it moves the stage once
  `presentation.revision >= revision` is on screen — an INSERTED slide has no
  position until the refetched spec arrives — and never while
  `presentedElement()` is set: a projected deck must not jump under the
  audience because the studio is open beside it. `PresentationViewer` wires
  `onSelectSlide={live.goTo}`.
- **The viewer's hash.** `parseSlideHash(hash, spec)` reads `#/<slide id>` as
  well as `#/<index>`, on load and on `hashchange`; the viewer keeps WRITING
  the index, so nothing that ever bookmarked a deck breaks.

The agent decides whether to link (the account tells it to, the description
says why); the frontend decides where the click lands (the stage beside the
rail, the card in the transcript, or the viewer). A link to a slide that no
longer exists resolves to nothing in place and falls through to the viewer,
which opens the deck on its first slide.

### Slide links inside a deck

The links above live in the CHAT; this one lives ON a slide, for the
audience: an agenda that jumps to its sections, a roadmap whose every item
opens its detail slide. Before it, an agent asked for that could only print
"-> slide 19" next to each item, a number the next insert made wrong.

**Grammar** (`presentation_markup.py`, mirrored by `markup.ts`):
`[text](#/<slide id>)` in any markup field. The ref is
`SLIDE_LINK_REF_PATTERN`, the slide id grammar (`SLIDE_ID_PATTERN` without
its anchors, pinned), and it is an id ONLY: an in-deck link never falls back
to reading its ref as a position (`slideIndexer`, unlike `resolveSlideRef`),
so `#/19` is the slide whose id is `19` - an agent writing a 1-based number
where reveal's `#/<n>` is 0-based would otherwise land one slide off, and
silently move with every reorder. The label is plain text (no marker inside
it, no link inside a span or a bold run - the grammar stays flat), and `](#/`
is the link's marker: left over in plain text (no label, a ref with a space)
it is refused as unbalanced, with the shape to write.

**Links out**: the same token takes an address instead of `#/<ref>` -
`LINK_URL_PATTERN`, the banner action's two schemes (`https://`, `mailto:`)
without parentheses, quotes, angle brackets, backticks or whitespace, so
`javascript:`, `data:` and `http:` are never links. A slide id holds no `:`,
so one `target` carries either and `isLinkUrl` tells them apart (the runs
model, the DOM's `data-slide-link` / `data-link-href`, the renderer). Drawn
as `<a target="_blank" rel="noopener noreferrer">` (the public link's token
never rides a referrer), inert while editing. Any other Markdown link
(`[x](http://...)`, `[x](/path)`) stays literal text as it always did - no
stored string that parsed before is refused now - and the save's `Not links:`
line (`literal_markdown_links`) names each one with its slide, so the agent
fixes it in the same turn instead of shipping raw brackets.

**A target that is not a slide** (a typo, a deleted slide) is not a refusal:
deleting a slide would otherwise refuse every later save of the deck. The
renderer draws such a link as its plain label, and `slide_links_line(spec)`
rides on every save (`generate_presentation`, the `edit_presentation`
account) and in the `get_presentation` head: `Slide links: N links, every one
lands on a slide of this deck`, or the broken ones with their slide and the
reminder that a link names an id, never a number (`_BROKEN_SLIDE_LINKS_LISTED`
listed, the count complete). The agent fixes them in the same turn; no prompt
asks it to be careful (invariant 16). The head also states the shape next to
the chat link's, so the two are never confused.

**Rendering** (`inline-markup.tsx` `SlideLink`, `slide-link-context.ts`): the
deck host provides `SlideLinkContext` - `PresentationDeck` moves reveal
(`deck.slide(index)`; none for the `print` variant), `PageDocument` scrolls
to the page (`controller.goTo`), the export page gives each link
`href="#deck-slide-<n>"` and each canvas that `id` (`slideAnchorId`, only
there: two decks on one transcript would repeat ids). A click on a deck host
is `preventDefault`ed so the chat route's fragment is never touched; in edit
mode a click places the caret and never navigates. No host (a thumbnail, the
speaker view's previews) draws the link inert. Style: the text's own colour,
an accent underline (`.deck-slide-link`).

**Editing**: a link is a run style of its own (`style: "link"`, `target`) in
the runs model - flat like a span - so it survives an edit around it, splits
when part of it is relinked, and `Clear formatting` takes it off. A run the
grammar cannot carry (a `]` in the label, a `[` glued before it, a target
that is not a slide id) is written as its plain label, never as refused
markup. The text bar's `Link to a slide` opens `SlideLinkPicker`, a list
INSIDE the bar rather than a menu (a menu takes the focus, and a blur commits
the text), with every slide but the edited one, and `Remove link` first when
the caret or the selection is a link (`linkAround`: the whole link, not the
word under the caret).

**Exports**: the PDF needs nothing more - Chromium writes the export page's
in-document anchors as internal links. `SLIDE_LINK_BOXES_JS` measures every
`a[data-slide-link-index]` (and every `a[data-link-href]`, with its address)
per line box (a wrapped label is two boxes),
clipped to the slide, whenever slide images or primitives are rendered, and
`pptx_writer.add_slide_link_hotspots` lays a rectangle over each box in BOTH
PowerPoint variants, with PowerPoint's "jump to slide" action
(`click_action.target_slide`) or a hyperlink to the address (re-checked
against `LINK_URL_PATTERN`: a measured value is not trusted), and a fill at
0% opacity - a shape with no fill
is hit on its outline only in a slide show. A shape rather than a hyperlink on
the run because PowerPoint recolours and underlines hyperlinked text, and the
picture variant has no run to carry one. A link to its own slide, past the
deck or with no box is skipped. The browser print fallback keeps no link.

Tests: `tests/test_presentation_slide_links_in_deck.py` (grammar, refusals,
literal outbound links, the ref grammar pin, the save line on the real edit
account, both writers' hotspots, the anchors the exporter measures, the
frontend tokenizer mirror); `slide-links-in-deck.test.ts` (tokenizer, strict
reading, runs model, id-only resolution); `slide-link-render.test.tsx` (the
click on a host, in edit mode, on the export page, a dangling target, no
host).

## Surgical edits

`generate_presentation(presentation_id=...)` takes the WHOLE spec: to change
one word the model had to re-emit every slide, and a re-emitted slide is never
the same generation — wording drifted, blocks moved, a 39-slide deck was
rewritten to fix its subtitle. Worse, `get_presentation` cut the spec at a
20 000-character budget mid-JSON, so past slide ~15 the agent could not even
read what it was about to rewrite. Both are fixed deterministically, with no
prompt text asking the model to "be careful" (rule 16 of `AGENTS.md`).

**`edit_presentation`** (`builtin_tools/presentation.py::_edit_presentation`)
takes `presentation_id` plus an ordered list of small operations and applies
them SERVER-SIDE to the stored spec through `presentation_patch.py`:

| Operation | Addresses | Payload |
| --- | --- | --- |
| `update_slide` / `replace_slide` | one slide (1-based position or stable `id`) | `fields` merged key by key (identity and `layout` cannot change here) / `content`, a complete slide (the id is kept) |
| `insert_slide` / `delete_slide` / `move_slide` | position `at` or `after` a slide / one slide / one slide + `to` | a complete slide / — / — |
| `update_block` / `replace_block` / `insert_block` / `delete_block` | one block: `"N"`, `"column.N"` on a columns slide, `"footer"`, `"N.M"` a callout's child, `"N.M.K"` inside a `columns` BLOCK (block N, column M, block K — `"N.M"` alone names the column and is refused), `"N.left.K"` / `"N.right.K"` in a comparison; on a `dashboard`, `"N"` is panel N (below) | `fields` merged / `content`, a complete block (`type` cannot change with `update_block`) / a complete block at `at` / — |
| `replace_text` | every human-readable string of one slide (`slide`) or of the whole deck (title and chrome included) | `find` / `replace` / optional `count` |
| `replace_image` | every picture field (`file_id` / `*_file_id`) of the deck, one slide (`slide`) or one block (`slide` + `block`), nested lists included | `find` (the file id shown now) / `replace` (the new file id) / optional `count` |
| `update_deck` | `title`, `theme`, `aspect_ratio`, `transition`, `chrome` (chrome merged key by key) | `fields` |

The engine (`apply_operations`) deep-copies the baseline, resolves every
reference against the CURRENT state (an insert shifts what "slide 5" means for
the operations after it — the agent is told so in the tool description),
records what each operation touched by slide **id** — never by position — and
raises `PresentationPatchError` with the failing operation's ordinal and the
op name in front (`operation 2 (replace_text): ...`) so a burst of edits fails
as one unit with a precise cause. `replace_text` is exact and case-sensitive;
`_TEXT_SKIP_KEYS` keeps it away from ids, layouts, block types, tones, sizes
and every `*file_id`, so a find that happens to equal a structural string is
"not found" rather than a rewritten structure. Unscoped (no `slide`), it
refuses a phrase that occurs more than once unless `count` says exactly how
many occurrences are meant — the refusal lists them (`slide 2 (1), slide 3 (1),
deck title (1)`), so the agent scopes with `slide` or passes `count` to change
them all on purpose — and a `count` that does not match is refused the same
way. Scoped to one slide, every occurrence on THAT slide is replaced without a
count: the slide is the pointer. The guarantee is that a word changes where the
user pointed, not wherever it also appears.

**Dashboard panels.** A `dashboard` slide has no `blocks` list: its
body is `panels`, each a `{title, span, block}` holding ONE figure.
`resolve_block_slot` once refused every address on it ("a dashboard slide has no
blocks"), so a layout-check remedy on a panel's chart (`value_label_fit`,
`legend_position`, `label_size` are block fields) could only be applied by
re-sending the whole `panels` list - every chart's data re-emitted to change
one field of one. On a dashboard, `"N"` now names panel N
(`_dashboard_panels`, `_panel_position`): the slot is the panel's `block`, and
`_Slot.panel` is the panel. `update_block` splits its `fields` - the panel's
own keys (`_PANEL_FIELDS`, read off `DashboardPanel.model_fields`: `title`,
`span`) go to the panel, the rest to the figure; the split is exact because a
panel's chart takes no `title` (`PANEL_CHART_TITLE_REFUSAL`) and no figure has
a `span`. `replace_block` swaps the figure under the same title and span,
`insert_block` adds a panel `{block}` at `at` (or appends; `"footer"` still
sets the footer), `delete_block` removes the panel with its figure; a deeper
address names the figure's nesting, which no figure has, and is refused by
name. The validator still holds the panel count and the grid rows. A layout
whose content is slide fields (`title`, `section`, `agenda`, `roadmap`,
`takeaways`, `closing`) is refused with `_no_body_blocks`, which says whether
the layout takes a `footer_block` (`slide_model(layout).model_fields`).

**Pictures.** A picture is a platform file id in a `file_id` /
`*_file_id` field - an `image` block, one logo of a `logo_grid`, a `profiles`
photo or line logo, an `architecture` node logo, a slide's background image or
a title slide's speaker photo. Asked to replace "the outdated screenshot", an
agent used to see bare UUIDs, could not open one (`read_file_content` resolves
files of the CURRENT conversation only, and a deck's pictures are template
assets or files of the conversation that built it), and could change a logo
inside a list only by re-sending the list (`update_block` merges top-level
fields). Three pieces, all tool-result grounding, no prompt text:

- `presentation_pictures.py` holds THE walk of the file contract
  (`is_file_reference_key` / `walk_file_references` / `slide_file_references`),
  shared by the writer check, the reader grant, the export's render
  allow-list, `replace_text`'s skip rule and `replace_image`, and names every
  place a picture is shown in the edit grammar (`picture_placements` /
  `describe_location`: `slide 9 block 1 (logo_grid) item 3 "Acme"`,
  `slide 4 block 2.1.1 (image)`, `slide 1 speaker photo`) - the address it prints
  is the one `resolve_block_slot` resolves (pinned by a test).
  `load_picture_facts` says what each file is in one query (and one for the
  visible templates) - but a deck grants its readers the PIXELS its slides
  show, not every word written about them: a template asset's
  filename, caption and template are told under the reader's three-tier
  visibility of that template (an asset of a private template keeps its role
  only), and a conversation file's filename and the notes an agent saved on
  it (`annotate_file`) only to the file's owner - to anyone else it is "a file
  generated (uploaded) by another user", with its size and date. Nothing else
  shows a deck reader those notes, and a raster picture is served inline,
  without its filename.
- `get_presentation` appends the pictures of the slides it SERVES, grouped by
  file, a file that no longer exists flagged `MISSING`;
  `capture_presentation_slides` lists the pictures of the slides it captured,
  so what the agent sees maps to ids - read off the revision the page RENDERED
  (`RenderedDeck.revision`): a save landing between the load and the render (a
  parallel `edit_presentation` of the same response) is re-read, and a deck
  that moved again lists nothing rather than pairing the PNGs with stale ids.
  For an editor the list says how to swap one and where to pick the new one
  (`get_template(template_id=..., role=...)`). Bounded: 16 files described
  (32 when every picture is on one slide), 6 places each, names clipped; past
  the cap a list over several slides names the slides whose pictures it left
  out, and a list of one slide - where reading fewer slides cannot help - names
  every remaining file by id and place. A picture-free deck costs no query.
- `replace_image {find, replace, slide?, block?, count?}` swaps a picture at
  every place in scope without re-sending anything, with `replace_text`'s
  safety: a file not shown there is a "No change" naming the pictures that ARE
  there (never a failure the loop's brake counts), several places
  without a `slide` need `count`, a wrong `count` is refused, the refusal lists
  the places (12 named, the count exact). `find` / `replace` accept the
  `[[FILE:<id>]]` / `file:<id>` spellings a model copies; `replace` must be a
  UUID and is vouched for by `verify_file_references` against the WRITER like
  any reference an edit writes, so a swap cannot hand the deck's readers a file
  the writer could not download. The result names each place, what the new
  file is, and the `capture_presentation_slides` call that shows them - from
  the SAVED deck, not the operation log: a swap a later operation of
  the same call undid (a `replace_block` / `replace_slide` / `delete_*`, a
  second swap) is neither reported nor counted, a place several swaps wrote
  is the last one's, a place back to the picture it showed when the call
  started (`A -> X`, then `X -> A`) was not swapped at all, each place is
  addressed where the picture ends up in the VALIDATED spec (a retired v1
  slide an operation wrote is named as its stored v2 upgrade), and only a
  file the stored or the saved deck names is described (the replacement a
  chained swap overwrote was never vouched for, so it is named by id alone).
  `replace_text` rewrites strings where they are
  (counted first, applied after every check), so a caption fixed in the same
  call keeps the swapped slot, and its account, intact. An edit
  refused over a picture that was ALREADY on a slide it wrote (a removed
  template asset: `... is not a platform file`) spells out the `replace_image`
  that gets past it (the slide by id, the dead file id); a refused reference
  the edit introduced - an invented or unreadable `replace` - gets no such
  hint.

**Concurrent editors.** Two assistants (or two turns of one) editing the same
deck could both load revision N and both save N+1, the second silently dropping
the first one's change and re-using its version number. The edit path (and the
wholesale rewrite in `generate_presentation`) therefore loads the row
`load_writable_presentation(..., for_update=True)` — a `SELECT ... FOR UPDATE`
that holds the row for the transaction. The lock spans the in-memory patch, the
file-reference check and `save_presentation`, all of it in this transaction; the
second editor waits and, under READ COMMITTED, is handed the row as the first
one committed it, so it patches the fresh revision. The layout render happens
after the commit, outside the lock.

**The guarantee is verified, not promised.** The tool normalises the stored
spec first (`baseline = validate_spec(stored)`) so the validator's own
canonicalisation (assigned ids, dropped `None`s, coerced list items) can never
be mistaken for a change, applies the operations, re-validates the result like
a fresh deck (same `PresentationSpecError`, same 0-based paths — a `replace_text`
that pushes a string over `MAX_TEXT_CHARS` is refused before anything is
written), and then `verify_untouched()` compares the canonical JSON
(`canonical()` — sorted keys, compact separators) of every slide the
operations did NOT name against the baseline. A drift there means an engine
bug, and the answer is a refusal ("the edit would have altered slides no
operation named ... Nothing was saved"), never a silent save. The same
canonical comparison is what the tests use, so "byte-identical" in the tool
result is the tested claim, not a figure of speech. File references are
re-verified (`verify_file_references`) for the WRITTEN slides only — the
untouched ones are masked to `{id, layout}` — so an editor who cannot download
a colleague's upload still cannot inject it, while an existing image on an
untouched slide is not re-authorised against the editor (the owner already
vouched for it when the deck was created).

**Accounting.** `PatchResult` carries `changed`, `named_unchanged` (an
operation named the slide but left it byte-identical — reported as "already
as requested", not as a change), `inserted`, `deleted`, `moved`, `deck_fields`
and `replacements`. The tracker classifies a slide the moment an operation
writes it, but the validator canonicalises afterwards (a title with a trailing
space is stripped, a whitespace-only optional field is dropped), so the tool
re-derives the account against the VALIDATED spec — `reconcile(outcome,
baseline, patched)` — before it decides anything: a slide that normalised back
to its baseline is "already as requested", a deck field that stripped back to
itself is not a deck change, and neither reaches the result, the history line
or `touched_ids` (so `verify_untouched` covers it too). Without that pass a
mixed edit — one real change plus one the validator undid — would save a
revision whose history names a slide that is byte-identical to what it was. A
no-op result is NOT a revision: nothing is saved and the
tool says so with the revision kept. Otherwise the deck goes through the
ordinary `save_presentation(presentation=existing, author_id=<editor>,
change_summary=...)`, so `revision` bumps by one, `updated_by_user_id` is the
editor and the history panel gets a `PresentationVersion` whose summary is the
agent's `change_summary` or the derived one (`summarize_patch`: "Edited slides
1, 5; Inserted slide 4; Deleted slide 5; Moved slide 4 to 3", "Changed title",
"1 text replacement"). The tool result (`_render_edit_account`) states the
count of changed and unchanged slides "verified byte-identical to revision
N-1", lists each changed / inserted / deleted / moved slide by position and
title, carries the outline WITH ids (so the next edit can address a slide by
its stable id) and the `[[PRESENTATION:<id>]]` marker, which is why the chat
card and the studio stage refresh in place. Permissions are the deck tool's:
`load_writable_presentation` (owner, `config` grantee, admin on a
company-managed deck); a read-only grantee is told the deck is read-only, an
invisible deck is indistinguishable from a missing one.

**Layout check, scoped to the edit.** The post-save layout check (above)
runs after a real edit exactly as after a `generate_presentation` save — same
`layout_check_note`, pinned to the revision just saved, served from the
per-revision cache when the studio reads the deck back — but the report is
NARROWED to the slides the edit wrote (`narrow_layout_report(report,
slide_ids=written, slide_indexes=...)`: matched by the stable id the render
published, by position in the measured revision when it published none). The
agent is told what ITS change did to the slide it touched ("the body needed
40% more height" → cut content on that slide, same turn) and only how many
pre-existing findings sit on the slides it did not touch ("Not listed: 2
pre-existing finding(s) on slides this edit did not touch"). Listing those
would send the agent off to fix slides the user never mentioned, which is the
deck-wide drift the tool exists to prevent; the human sees the full report in
the Layout panel, the agent in `get_presentation`. Two edges of the scoping
are deliberate: a change to a deck-level field that is DRAWN on every slide
(`LAYOUT_DECK_FIELDS` in `presentation_patch.py` — `theme`, `aspect_ratio`,
`chrome`; the single constant the scoping reads) re-renders every slide, so
there the full report is the edit's consequence and nothing is narrowed
(`written_slide_ids=None`); an edit that rewrote no slide's rendering — a
move, a delete, and the two `update_deck` fields no slide draws BY ITSELF,
the deck `title` (the library / tab / chat-card name; with no template
pattern the chrome footer is `event_label` + `footer_text`) and the
`transition` (motion BETWEEN slides) — has nothing new to measure, so nothing
is rendered and the result carries no layout line — never "the 0 slides this
edit wrote fit". A title or transition change made in the same call
as a slide edit narrows to that slide like any other edit. A no-op saves
nothing and therefore measures nothing. The one exception is decided PER
DECK, never by widening the constant: a deck whose template footer pattern
reads `{{title}}` (or `{{subtitle}}`) does draw the title (the opening
slide's subtitle) on every slide, so `template_chrome_redraws()`
(`presentation_chrome.py`) makes a rename - or an edit that changed
the `{{subtitle}}` VALUE - re-render the whole deck for that deck alone. That
value is compared rendered (`opening_subtitle()` on the baseline and on the
saved spec), never inferred from the written slides: a `delete_slide` of the
opening slide or a `move_slide` that puts another title slide first changes
what every header says while writing no slide at all, and a rewrite of the
opening slide that leaves its subtitle alone changes nothing the footer shows,
so it stays a surgical edit measured on that slide alone. The footer / header
read-back (`footer_status_line()`) in the same result is gated on that ONE
reading too (plus a template change and a `chrome` edit), so the two can
never disagree: an edit that redraws nothing is not read back either.

**Stale slide numbers: `expected_revision`.** The row lock (above) makes two
editors take turns, but it cannot make a slide NUMBER authored against
revision N mean the same slide once revision N+1 inserted or reordered slides
— `{"slide": 12, ...}` would land on whatever now holds position 12. The tool
therefore takes an optional `expected_revision`: the revision the agent read
the numbers from (every `get_presentation` result, every edit result and the
studio context state `revision N`). When it is given and the locked row is at
another revision, the call is refused before any operation is resolved
("moved from revision 1 to 3 since you read it ... read it again"), and the
agent re-reads. The tool description asks for it whenever a slide is addressed
by number; an edit that addresses every slide by id does not need it, because
an id is stable across inserts and reorders. Read with the same lenience as a
slide number (`presentation_patch.coerce_revision`, built on `_as_position`):
an int, an integer-valued float or a digit string — some providers send
`"12"` for an `integer` field, others `12.0` — from 1 up; `0` and anything
else is refused as not a revision number, never as a stale one. The
refusal of a stale revision names a later save, not "someone else": the usual
cause is the agent's own earlier edit in the conversation, and the wording is
relayed to the user.

**At most `MAX_OPERATIONS` operations per call** (50, also the schema's
`maxItems`). An edit is a handful of pointed changes; a burst past that is a
rewrite in disguise and is refused before anything is applied, pointing at
`generate_presentation`.

**One JSON type per schema property.** The `edit_presentation` schema declares
a slide reference (`slide`, `at`, `after`) as a `string` — `"12"` or a slide id
— never as a `["integer", "string"]` union: Gemini's function declarations
refuse a list-valued `type` at `FunctionDeclaration` construction, and one
refused tool fails EVERY request of an agent that carries the deck tools, on
every turn. The engine (`_as_position` / `resolve_slide`) still reads a JSON
integer as a position for a provider that ignores the declared type.
`test_edit_schema_declares_one_type_per_property_so_gemini_accepts_it` builds
every deck tool's declaration through the real Gemini cleaner and SDK types.

**Reading any slide in full.** `get_presentation` no longer truncates: when the
full spec exceeds `_SPEC_READBACK_BUDGET` it is served WHOLE SLIDE BY WHOLE
SLIDE — deck settings, then as many complete slides as fit (the first always),
then "Not shown: slides X-Y. Read them with `get_presentation(presentation_id,
slides="X-Y")`". The `slides` selector (`parse_slide_selection`: `"20"`,
`"16-25"`, `"30-"`, `"1,4,9-11"`, `"all"`, a slide id) returns exactly the
named slides as complete JSON objects, one per line under a
`--- slide N [layout] id=... ---` header, so a 100-slide deck is readable in a
handful of calls and a single slide in one. Nothing is ever cut mid-slide, and
the tool says so in the result rather than leaving the agent to infer it from a
dangling brace.

**Routing.** The built `generate_presentation` description, the
`presentation-design` skill, the `Presentation Generation` built-in agent and
the studio prompt all route a change to an existing deck to
`edit_presentation`; `generate_presentation(presentation_id=...)` remains for
a WHOLESALE rewrite (new outline, new arc), and the REST `PATCH /{id}` remains
the human editor's path. The one thing the model still has to do is read the
slide it edits first (`get_presentation(slides="N")`), because a `replace_text`
is exact — the studio prompt says so in one line.

### Searching a deck's text: `search_presentation_text`

"Does the old product name still appear anywhere?" is a read. Without a tool
for it, an agent probed with `edit_presentation` and a `replace_text` whose
`find` equals its `replace`; the engine refused each probe, every refusal
counted toward the loop's failure brake, and a few of them withdrew every tool
of the turn.

* **The tool.** `search_presentation_text(presentation_id, query,
  case_sensitive?, whole_word?)` on a deck the user can READ
  (`load_visible_presentation`, like `get_presentation`). It returns every
  occurrence with the slide number, layout and id, the block address
  `edit_presentation` takes (`"3"`, `"2.1"`, `"4.left.1"`, `"footer"`, a
  dashboard panel `"2"` - none for a slide field such as the title, the notes
  or a dashboard tile) and the field path inside it (`rows[1][0].text`,
  `right.label`, `caption`; list indexes 0-based), then the stored text around
  the match with the match between « » exactly as stored - what `replace_text`
  must be given. It writes nothing, is read-classified by name and is in the
  shipped HITL whitelist.
* **The same fields as `replace_text`.** `presentation_patch.iter_text_fields()`
  is the one walk behind `replace_text`'s count and `search_text()`: every
  string under each slide and under the deck's `title` and `chrome`, never a
  key of `_TEXT_SKIP_KEYS` (ids, layouts, block types, icons, tones, ...) or a
  file reference. A case-sensitive substring search therefore finds exactly
  what `replace_text` counts (`test_search_finds_exactly_what_replace_text_counts`).
  Matching is literal on the stored string, inline markup included.
* **Every slide, whatever the length.** The search reads the whole stored spec;
  `get_presentation`'s per-call budget limits a read-back, not a search. The
  result lists at most `_SEARCH_LIST_LIMIT` occurrences; the count and the
  slides they are on are always complete.
* **Whole words.** `whole_word` puts a word boundary on each side of a query
  that starts or ends with a word character (`text_query_pattern`): `core`
  never matches inside `scores`, `Q3:` still matches before a space. A zero
  result says what a looser search would find (another case, inside longer
  words), so the next call is the right one.
* **The probe answered.** `replace_text` with `find == replace` raises
  `PresentationPatchNoChangeError`: the tool answers `No change` (`no_op`, not
  an error) and names `search_presentation_text`. Its "not found" answer names
  it too, since matching there is case-sensitive.

### Deleting a deck: `delete_presentation`

An agent that copied a deck (because the original is read-only, typically)
could not remove the copy once the user no longer wanted it.

* **One delete.** `delete_presentation(presentation_id)` calls
  `presentations.remove_presentation()`, the function behind the library's
  `DELETE /presentations/{id}`: the audit row (`via: agent`), then the row.
  There is no trash and no restore: the database cascades the version history,
  the review comments and the public link (`ON DELETE CASCADE`), the grants
  were columns of the row, a studio conversation bound to the deck stays the
  user's (`SET NULL`) and a `[[PRESENTATION:<id>]]` card reads "no longer
  available". The cached layout report is keyed by the deck and served only to
  a caller that loaded it; the public PDF only through the link row.
* **The owner only.** The row is loaded `FOR UPDATE` through the visibility
  predicate, then `agent_deck_permission` must say `owner`: the owner, or an
  administrator on a company-managed deck. A `config` grantee may edit a deck
  but not make it disappear for its owner, a `read_only` grantee and a
  stranger are refused, and an administrator's agent never deletes a
  colleague's personal deck (the agent-side admin rule of every deck write).
  The REST route keeps its own bar (owner, `config` grantee, admin), where a
  person acts in their own name.
* **Only when asked.** The description says the delete is irreversible from
  the agent's side and runs only on the user's explicit request - never to
  tidy up, never to start over. The tool is write-classified, so the approval
  gate treats it like every other write.

### Refusals are not failures

Every validation refusal of the deck tools - a malformed argument, a spec that
does not validate, an operation that cannot be applied, a deck the user may
not change - goes through `builtin_tools/presentation._refusal` and carries
`ToolResult.refused`. The loop leaves such results out of the tool-wide
repeated-failure nudge, cooldown and brake (`_track_tool_result`), which only
runtime errors (a crashed save, an exception) feed; the loop breaker still
counts an identical refused call, so asking it again is warned, then refused.
See `backend/app/tools/AGENTS.md` for the general contract.

## In-place manual editing

The agent edits a deck by prompt; a person fixes a word, a spacing, a style by
HAND, the way a slide editor is expected to work. With the editor panel open
(`?panel=editor`, the toolbar's `Edit slides`, a manage tier), the stage IS the
editor: a click selects a block, a click on a text opens it in place, the
keyboard arranges, a floating bar carries the block's actions and the text's
formatting, the panel's inspector carries every field the spec has for the
selection. Every change **saves itself** ("Autosave and merge" below)
and a collaborator's revision is merged into the draft. The structured spec is
the point, not a constraint worked around: a block is selected as a block, a
list stays a list, and the platform's layouts keep doing the layout - there is
no free positioning, because a deck the agent can no longer read or fit is not
a deck this platform can keep editing.

**One session, two surfaces.** `useDeckEditor` (`presentation-editor.ts`)
lives in the VIEWER and is handed to both the panel and the stage: one draft,
one undo history (`Timeline` of `Step`s - the draft AND the selection as they
were, so an undo puts the selection back on the block a move carried away),
one selection (`{ slide, block: path | null }`), one text under edit. Every
mutation is a pure copy-on-write update of the draft through `apply()`, keyed
so consecutive keystrokes into one field fold into one undo step
(`COALESCE_MS`), bounded by `HISTORY_LIMIT`. The stage renders the editor's
slides in the SAME render as the edit (`liveSpec` in `PresentationViewer.tsx`)
- the design the panel resolves from the template rides its overlay one render
behind, which draws nothing structural - so the outlines and the floating bars
measure the DOM the edit produced, never the one before it. A slide the panel
inserts, duplicates or deletes asks the stage to land on it through
`focusRequest`, consumed by the viewer AFTER the deck synced: a `goTo` issued
from the click ran against the sections reveal had before the change and
landed one place off.

**Addressing: spec paths.** Everything is addressed by the path of the field
in the slide (`blocks.2.items.0.text`, `columns.1.0.content`,
`panels.0.block.tiles.2.label`, `footer_block.content`) - `edit-paths.ts`
reads, writes, inserts, moves and removes at a path, copy-on-write, and
`blockPathOf()` knows which prefixes are block roots (`blocks.N`,
`children.N`, `columns.N.M`, `panels.N.block`, `footer_block`). The renderer
publishes the addresses ONLY in edit mode (`EditModeContext` set by the stage;
`useBlockEditAttrs()` -> `data-edit-block`, `InlineMarkup` -> `data-edit-text`,
`data-slide-index` on the slide frame) so a presented deck, a chat card, a
thumbnail and the export page render byte-identical DOM. `SlideEditLayer`
listens on the stage surface in the capture phase, resolves what is under the
pointer from those attributes, and sets `data-edit-hover` /
`data-edit-selected` imperatively on the block element - the outlines are CSS
in canvas pixels (`presentation.css`), so they scale with the deck and cost no
re-render of the slides. Click model: a click ON a text opens it at once with
the caret where the pointer is (the glyphs are the target, `onGlyphs`); a click
on the rest of a block selects the block, the next click on the same block
opens the one text its row / tile / card holds (`impliedText`: the nearest
ancestor holding exactly one text - two texts are ambiguous and open nothing);
a double-click does both at once; empty slide selects the slide.

**Adding a block.** The selected block's bar inserts after it, but
that needs a block selected first and never reaches an empty body. Every run
of the slide in focus therefore carries a round "+" just under its last block
(`SlideEditLayer`, `runAnchor`): the body - its flow, even empty (the "+" then
sits at the top of the body), or each of its columns - plus the runs inside
the selected container block or the container a selected block sits in (a
callout's children, a comparison's sides, a columns block's columns), which
is what `addableRuns()` (`presentation-editor.ts`) lists. The run is the one
`BlockList` publishes as `data-edit-list` in edit mode; a block added there
lands after the run's last one and is selected. The slide inspector offers
the same runs as an `Add a block` menu (`At the end`, `Column N`). Every entry
point builds its menu with ONE palette (`blockPaletteItems()` in
`block-palette.ts`): what the run takes (`blockAllowedIn`), every entry
disabled once the run is at its cap (`canGrowRun`), a menu that opens above a
"+" low on the stage and scrolls (`OverflowMenu` `side` / `scroll`). Nothing
is drawn outside edit mode.

**Typing in place.** `InlineTextEditor` is an UNCONTROLLED `contentEditable`
React never diffs during a session: it renders the text's runs once
(`runsToNodes`, `createElement` only - no `innerHTML`), lets the browser edit,
and reads the DOM back (`domToRuns` -> `runsToMarkup`) - on commit, and while
the person types: a pause of `LIVE_TEXT_DELAY_MS` hands the text as it
stands to the session (`onLiveText`) when its markup is one the save accepts,
so the autosave writes it without the field being closed, and `flush()` on the
registered handle hands it over at once (a save, a merge). The live writes and
the commit of one opening of the field share one sticky history key
(`EditingText.session`, `WriteOptions.sticky`): the text session is one undo
step, and a burst that ends where it began is none. Escape puts the opening
value back. The runs
model (`inline-runs.ts`) is the bridge between the inline grammar and the DOM:
`applyStyle` restyles exactly the selected range (a word at a collapsed caret),
`normalizeRuns` splits newlines out of styled runs and hoists edge whitespace so
`** a **` is never written, and `runsToMarkup` writes a run the grammar cannot
carry (a marker inside it, an emphasis glued to a word, a bold glued to a `*`)
as PLAIN text rather than as markup the validator would refuse - what the
person sees after a commit is what the spec can hold (`canonicalRuns`).
Keys inside the field are the field's (`stopPropagation` on every key): Escape
cancels, Enter inserts a line break, Ctrl+Enter / Tab / blur commit,
Ctrl+B / I / E toggle bold / emphasis / code, paste inserts the clipboard's
PLAIN text (`execCommand("insertText")` - the DOM is never handed a string of
HTML, and a `<font>` from Word has no room in the grammar anyway), and so does
a drop, at the drop point (`caretOffsetAtPoint`); a drag of the field's
own text is a MOVE done in the runs model (`moveRange`: the stretch leaves its
place and lands at the drop point once, as plain text - the drop is reported
to the browser as a copy, so no engine also deletes the source). A key
pressed while an input method composes (`event.isComposing` - Enter confirming
a Japanese conversion, Escape dropping it) is the composition's and is left to
the browser. The text bar (`presentation.textBar`) drives the same handle
through `inline-editor-registry.ts` and takes no focus (`onMouseDown`
prevented) - a blur is a commit. `markupProblem()` (`markup.ts`) mirrors the
validator's refusals (HTML, unbalanced markers, nesting, unknown tone) under
the panel's text fields, live, and at the commit of a text edited in place
(`inlineCommit`): the grammar has no escape, so a stray backtick,
`**`, `[[` or `<` would otherwise surface as the save's 422. An explicit
commit (Ctrl+Enter, Tab, Enter in a one-row field, the bar's Done, the commit
a save asks for) of such a text is refused - the field stays open with the
text as typed and a toast says why in the inspector's words, once per
attempt (a held key's auto-repeats ask nothing more: `event.repeat`) - while
a blur keeps the words, committed as typed and flagged the
same way: a field held open without the focus would either take the focus
back or lose the text at the next click. Escape still cancels. The editable
field and the runs model load
LAZILY, the first time a text is opened (`inline-markup.tsx`), and so does the
layer (`DeckStage` / `PageDocument`): a chat card, a presented deck and the
export page never download the editor. `PlainText` and `InlineMarkup` wrap
nothing by default - a call site that had a bare text node keeps it outside
edit mode (the export extractor reads a text node and a `<span>` through
different paths) and gets the inline `<span>` carrying the target only in edit
mode; a block-level label drawn on several cards (a persona's `pains_label`)
publishes its target on ONE of them, or a click would open N editors.

**Keyboard on the stage** (`SlideEditLayer`, document capture, never while a
field has the focus - `TYPING_TARGETS` - nor inside one of the deck's owned
regions - `OWNED_REGIONS` / `ACTIVATION_TARGETS` shared with `deck-keys.ts`,
so a menu, a dialog, the panel, and Enter / Space on a focused toolbar button
keep their keys; the selection's keys act only while the focus is ON the
stage: the body after a click on a slide, the stage surface, the layer's own
bars): Enter edits the selected block's main text (`primaryTextField`), Tab /
Shift+Tab walk the blocks of the slide (`blockPathsOf`), Delete / Backspace
remove the block (never the last one of a run the validator wants non-empty -
`canRemoveBlock`), Ctrl+D duplicates, Alt+Up / Alt+Down move within the slot
(`blockSlot`), Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y undo / redo, Ctrl+S saves now
(`requestSave`: the words of a text open in place are handed to the draft
first and the field stays open, a text the field cannot hand over saves
nothing and says why, then the autosave's registered `onSaveRequest`; once
per press, never once per auto-repeat),
Escape leaves the
text, then the selection, then falls through to the deck. Reveal's own keys
keep working when nothing is selected.

**Undo and redo are the session's, from everywhere**. Three gaps made
the keys work only on the stage and only until a save; each is closed
deterministically, in the session, never in a prompt:

- **From the panel's fields.** The `TYPING_TARGETS` guard is right for every
  other shortcut (Delete in an input deletes a character, Enter submits), but
  Ctrl+Z in an inspector field ran the BROWSER's undo of that one `<input>`,
  a history the session knows nothing about - so a word typed into the title
  field could not be taken back with the keys that take back a block move.
  The panel marks itself (`EDITOR_PANEL_ATTR` on the `<aside>`,
  `EDITOR_PANEL_SELECTOR` in `edit-context.ts`) and the layer's capture
  handler routes Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z from a typing target INSIDE
  the panel to the session (`routesUndo`), `preventDefault` included, so the
  browser's per-field history never diverges from the deck's; a field's value
  follows the draft, so the undone text leaves the input too. The same route
  takes the `beforeinput` a menu sends without a key (`historyUndo` /
  `historyRedo` from the Edit menu or the field's context menu), and
  a key pressed while an input method composes (`isComposing`) is left to
  the composition, as the in-place editor leaves it. A text under edit IN
  PLACE (`[data-edit-editing]`) is excluded - it walks its own history, below.
  The burst a field's keystrokes coalesce into is per field ON ONE SLIDE
  (`writeAt` qualifies the caller's slide-relative key with the slide index),
  so `title` typed on slide 2 right after `title` on slide 1 is
  another step, never the tail of the first. The shortcuts sheet
  (`DeckShortcutsDialog`, `?` / `F1`) lists the editor's keys in an `Edit`
  group while the panel is open (`editing`, passed by `DeckStage` and
  `PageDocument`) - with `Cmd` on a Mac (`isMac()` in `lib/utils.ts`, shared
  with the command palette) - and its description then names the one
  exception to "shortcuts pause while you type": undo, redo and save from the
  panel's fields.
- **Across a save.** A save used to CUT the history (`timelineFrom(saved)`)
  so a person could not undo past what they had just written - the surprise
  Google Slides and PowerPoint never give. `timelineAfterSave` now keeps the
  whole timeline and makes the save a CHECKPOINT: the saved deck becomes the
  present (or, when the draft moved on during the round trip, a step the
  dirty draft sits after), every past AND future draft is carried through
  `withSavedSlideIds` so the ids the server assigned survive an undo (a slide
  inserted, saved, undone and redone keeps the id the history panel and the
  comments anchor on; each draft's slides take THEIR ids - by identity, in
  the order of their time, by position only while the order is POSITIVELY
  established (exactly one slide without an identity match, every other one
  matched at its written place, so that slide has exactly one place to be -
  two moved-and-edited slides could have swapped, and would swap their ids)
  AND the slide written at that place was itself id-less, so a legacy
  slide deleted from where an already-named slide was then inserted never
  takes that slide's id - and a slide the server never named stays
  id-less), and an undo past the checkpoint simply reads dirty
  again - the next Save writes what the person went back to as a new revision
  and the saved one stays in `History`. A written draft the timeline no
  longer holds (a keystroke coalesced over it) is put back where it belongs
  in time, once: behind the current draft, or - after an undo during the
  round trip, which leaves the work since the save in the redo stack - as the
  first thing to redo, never as a step behind a draft older than it. This
  deliberately does not end the history at a save.
- **Discard is a step.** `timelineAfterDiscard` pushes the dropped draft as
  the step before the stored deck (when they differ under `canonicalJson`)
  and clears the redo stack; `reload` on the session is that discard - what
  "Leave anyway" does once a save failed (there is no Discard button)
  - EXCEPT onto a deck that moved on under the draft
  (`presentation.revision` past the seeded one): every step of that history
  was built on the old revision, and an undo into one of them saved with the
  new `expected_revision` would hand the server the old slides over the
  collaborator's with no warning. That discard is a `reset` - a fresh history.
- **Across a collaborator's revision**. A revision saved elsewhere is
  merged into the draft AND into every step of the history
  (`timelineAfterRemote`, each step merged three ways against the base it was
  built on), so the history only ever holds drafts of ONE base without being
  thrown away: an undo walks back the person's own changes, never the
  collaborator's ("what I had then, plus what they did since"). Leaving the
  deck, and the discard above, are what start it over.
- **Inside a text under edit.** The browser's own undo stack for a
  `contentEditable` is reset by every DOM rebuild (`applyFormat`, the line
  break), so Ctrl+Z after a Ctrl+B did nothing or something else. The editor
  keeps a SESSION history of its own (`text-history.ts`: `TextSnapshot` =
  runs + selection offsets, `recordSnapshot` coalescing a burst of one
  `InputEvent.inputType` within `TEXT_COALESCE_MS` as long as each keystroke
  BEGINS where the last one left the caret - a word typed at the end and a
  word typed at the start are two steps - never across a `DISCRETE_KINDS`
  boundary - a formatting, a line break (the editor's and the browser's own
  `insertLineBreak` / `insertParagraph`), a paste, a drop, a replacement, a
  cut, a drag out of the field - bounded by `TEXT_HISTORY_LIMIT`;
  `undoSnapshot` / `redoSnapshot` restore
  the runs AND the selection). Every record carries the selection as it was
  right BEFORE the change (`before`: read in `beforeinput` for the `input`
  that follows, the styled range for a formatting, the caret for a line
  break; for a composition, whose updates replace the text being composed,
  read ONCE on `compositionstart` and handed to the first
  `insertCompositionText` snapshot only, so the composed word is undone to
  where it was typed and its updates coalesce into that step): the
  present snapshot takes it without a step, so an undo puts
  the caret where the change began, not where the field was when the
  previous snapshot was taken - a caret moved or a word selected between two
  edits is remembered, never a step. Recorded from the `input` event, from
  every rebuild and once more after a paste (a no-op when the `input` fired);
  Ctrl+Z / Ctrl+Y inside the field walk it (`stopPropagation`, so the deck's
  history is untouched), and so do the `historyUndo` / `historyRedo` a
  context menu or the Edit menu dispatch through `beforeinput` - cancelled,
  since the browser's stack never saw the rebuilds. The session history
  stays one step for the whole text - a commit is what the deck's timeline
  records.

**What the panel knows about a block: the schema catalogs.** `block-schema.ts`
declares, for every one of the 28 block types, its fields (enum / flag /
number / text{markup} / icon / asset - a picture from the template's assets
of the roles it names / strings / group), its item list (key, label,
bounds, item fields, an item factory) and a palette default (`create`);
`slide-schema.ts` does the same per layout (`SLIDE_SCHEMA[layout].create(n)`
is what `Add slide` inserts). The catalogs are mirrored on the Pydantic models
by `tests/test_presentation_editor.py`, both ways: every descriptor names a
field of the model with the right shape, every enum's options are the
`Literal`'s values, every `num` range is the model's `ge` / `le`, every
`strings` and items bound its `min_length` / `max_length`; and every model
field with NO descriptor is named in the test's allow-lists (edited on the
stage: `blocks`, `children`, `columns`, `rows`, `panels`, `footer_block`; the
assistant's: a `file_id`, a `sparkline`, a chart's `series`, an architecture's
graph) - an omission is a decision, never silence. Every palette and `Add
slide` default is validated through node (`scripts/dump-presentation-schema.mts`;
the Backend CI job pins Node and the test FAILS there when the dump cannot
run, it only skips on a developer machine), so a model change fails the build
until the picker knows about it. The caps travel too: `DEFAULT_SLIDE_CAP` in
`slide-schema.ts` (the fallback; the deck's own cap is the response's
`max_slides`), `MAX_BLOCKS_PER_SLIDE` / `_COLUMN` / `_CONTAINER` and
`MAX_TABLE_COLUMNS` / `_ROWS` in `block-schema.ts` (`blockRunCapacity()` names
the cap of a run), pinned to `presentation_spec.py` and enforced in the
session (`canAddSlide`, `canGrowRun` - `insertSlide`, `duplicateSlide`,
`insertBlock`, `duplicateBlock` are no-ops at the cap) and in the UI (`Add
slide`, Duplicate, the insert palette disabled with the reason), so the cap
is met in the editor, not in a 422 at the save. `blockAllowedIn()` carries the
nesting rules (a callout's children and a comparison side take atomic blocks;
a column takes no `columns`); blocks that need a file, numbers or a graph
(image, logo grid, logo wall, chart, diagram, architecture) have no palette
entry - the panel says so and points at the assistant - and a logo grid's
items (a logo wall's groups) have no item factory either (a `{file_id: ""}`
would fail the save): the list is
edited, reordered and trimmed, never grown by hand. An enum offers `Default`
(unset, the model's default, which is what the agent writes too) unless the
model has none (`required`: `chart.kind`, a contact's `kind`).

**Autosave and merge.** People forgot to click Save and lost their
work; a text typed in place did not even count as a change until its field
was committed; and a collaborator's save over unsaved edits left only "reload
and lose them". So the editor saves on its own, and a revision saved
elsewhere is merged, never a reason to reload.

- **When** (`useDeckAutosave`, owned by the VIEWER next to the session, so a
  change made right before the panel closes is still written; the policy is
  the pure `autosave-policy.ts`): `AUTOSAVE_IDLE_MS` after the last change,
  never later than `AUTOSAVE_MAX_WAIT_MS` after the first one still unsaved
  (a long burst of typing is written as it goes), one save at a time; at once
  on Ctrl+S, when the tab is hidden, on `pagehide` and when the viewer
  unmounts - the saves made as the page goes away (`requestSave({ leaving:
  true })`: hidden, `pagehide`, the close prompt) ride a `keepalive` request,
  which outlives the page, whenever the body fits the browser's 64 KiB cap
  (`keepaliveFits`, `KEEPALIVE_BODY_MAX_BYTES`). A save that did not reach the server (`transport`: offline, a
  5xx, a 429) is retried after `retryDelay` (doubling to
  `AUTOSAVE_RETRY_MAX_MS`, never sooner than a 429's `Retry-After`), at once
  when the browser is back online (a 401 never gets here: the API client
  ends the session and sends the page to sign-in, the close prompt standing in
  front of that while a change is unsaved); one the
  server refused for what the draft holds (`invalid`, a 422) waits for the
  draft to change; one refused because the person lost the right (`denied`,
  403 / 404) stops. The panel footer (`SaveStatus`) says which, in place of
  Save and Discard, with `Retry now` and `Undo it` where they help; a tab
  closed over work not saved yet asks first (the wording is the browser's).
  Leaving the editor commits a text open in place, then awaits `flush()` -
  the last save and the merges it leads to, bounded by
  `LEAVE_FLUSH_TIMEOUT_MS` - and asks "Leave anyway" only when it failed.
  The stage renders the session's draft for as long as the editor is open,
  saved or not: swapping it for the stored deck at every save would redraw
  every slide for nothing.
- **Merge** (`deck-merge.ts`, `mergeDrafts`): `base` is the revision the
  draft was seeded from (`baseDraft`, the very object the draft was built
  from, so an untouched slide compares by reference), `ours` the draft,
  `theirs` the revision that arrived. What only one side changed is taken from
  that side; slides are matched by their stable id (inserts, deletes, moves
  and edits on both sides survive; the order follows the side that reordered;
  a slide one side deleted and the other edited STAYS, edited - an edit is
  work, a deletion is cheap to redo - reported as a conflict when ours edited
  it and in `restored` when theirs did, the panel's toast saying the slide is
  back); inside a slide
  objects merge key by key and a list (blocks, items, rows) is aligned with a
  diff3 over its elements, bounded by `MAX_ALIGNMENT_CELLS`, so a block added
  on one side and a word typed in another block on the other both land, and
  a block both edited merges field by field (same `type` at the same place).
  Where both changed the same thing differently OURS is kept - the draft is
  saved after theirs, so it is the last writer - and reported
  (`MergeConflict`, the panel's toast names the slide); a list whose two
  editions cannot be aligned without guessing keeps ours whole, never a mix
  that could repeat or drop a block. Unchanged pieces keep their identity.
- **The revision watch** merges a NEWER revision of the seeded deck
  (`revisionArrives`) into the draft and every step of the history
  (`timelineAfterRemote`), re-seeds on it (`rebase(from, theirs)`) and keeps
  the person where they were: the selection, the text under edit and the
  stage follow their slide by id (`slideIndexAfter`, `stageIndex` ->
  `focusRequest`). A text open in place hands its words over first
  (`flush()`: the merge then waits one render, `mergeTick`); one that cannot
  yet (markup the save would refuse) holds the merge (`revisionArrival` ->
  `hold`, `outOfDate` set: no save is sent, the footer says "Merging") until
  its next valid words or its close. The text stays open through a revision
  that changed its slide elsewhere - a collaborator typing in another block
  must not drop the caret mid-word - and is closed after the hand-over only
  when the revision moved or rewrote what it is written into
  (`pathUntouched`: every list on its path kept its length, and the deepest
  list element holding it - or the slide's own field - is unchanged; the
  field writes by index), the toast saying so; the selected block follows the
  same reading. The panel flashes the slides the revision changed in the filmstrip
  (`lastRemoteMerge.changed`, `REMOTE_CHANGE_FLASH_MS`).
- **How fast** a collaborator's revision arrives: the presence heartbeat
  answers the deck's revision (below), and runs every
  `PRESENCE_FAST_HEARTBEAT_MS` while the person's editor is open or another
  reader is in the editor (an agent turn does not speed the room up); a newer
  revision than the cached deck is refetched at once. The 15 s deck poll
  stays the floor.

**Saving.** Every save is the REST `PATCH /{id}` with `expected_revision`
(`saveBody`: the edited pieces, the template only when the pick changed, the
chrome only when it differs). The route locks the deck row before it checks
the revision (`_load_manageable(..., for_update=True)`, like the agent's
write paths; a restore too): two editors' autosaves of one revision
meet one 200 and one 409, never two writes of the same next revision
(`tests/test_presentation_autosave_postgres.py`). A save the server finds
unchanged (`changed_spec_fields` empty) writes nothing - no revision, no
history line, no audit line. A 409 (a collaborator saved first) refetches the
deck (`useUpdatePresentation`'s `onError`) and `saveRefused` sets `outOfDate`
with the draft kept; until the revision that won is in the query
(`awaitingLatest`) nothing is merged or saved over the refused one, and the
merge then rebases the draft on it - the next save extends it. A refusal or
a saved copy answered after the session left the base the save extended -
the viewer moved to another deck, which the router does on the same page (a
duplicate opened, Back), or a newer revision merged in during the round trip
- is not the session's: `refusalOf` and `adoptSaved` judge the answer by
`answersBase`, the deck AND the revision the PATCH named. Otherwise
`adoptSaved(saved, sent, draft)` re-seeds the session
from what the server stored - `draft` being the one the PATCH body was built
from: the whole history is KEPT and rebased on the saved deck
(`timelineAfterSave`, the checkpoint described above), a draft that moved on
WHILE the save was in flight (a field typed into during the round trip)
sitting after it, so nothing typed is dropped with the response and only what
came after the save reads dirty; the selection stays where the person was
working. A save answered LATE - after the poll loaded a collaborator's newer
revision - never rolls the cached deck back (`newestDeck` in
`lib/deck-revision.ts`, used by the update and restore mutations: a newer
cached revision is kept, the same revision replaced since a sharing change
does not bump it), and the session, rebased on its save, is compared with
the newer revision at once: the revision watch keys on the query's revision
AND the seeded one (`baseRevision`), so it merges the newer revision right
after. The newest
deck wins whatever order the answers land in: the deck query's
structural sharing keeps the newest revision against ANY write, a read
included (`shareNewestDeck` - TanStack writes a read's answer when it lands,
so a poll sent before the save committed and answered after it would put
the pre-save deck back), and the revision watch adopts only a NEWER revision
of the seeded deck (`revisionArrives`: an older copy is stale, never an
arrival). While a save is in flight (the autosave brackets every PATCH with
`beginSave` / `endSave`) nothing is merged (`defersToSave`): the poll may
bring the save's own commit before its answer, and merged as a
collaborator's it would be folded into the very draft being saved; the
answer rebases the session and the watch merges then. Leaving the editor
during a save waits for its answer (`whenSaveSettles`, inside `flush()`): a
change being saved is not an unsaved change. "Dirty" is
`canonicalJson(draft) !== canonicalJson(stored)`
with the KEYS SORTED (serialised once per draft, not per render): the query
cache keeps the deck it already held for every part a save left equal
(TanStack structural sharing) and the stored spec and a PATCH response spell
the same slide with `id` in different places - a byte comparison read that as
an edit that never was. The history line names the slides the save touched
(`describe_slide_changes`: `Edited slides 3, 7`, `Inserted slide 4`, `Deleted
slide 5`, `Moved slide 5 to 1`, by stable slide id with a
longest-increasing-run for the moves, plus `Changed theme` for the deck
fields). The stored side is read as today's validator would store it
(`_as_stored_today`) and a stored slide with no id is matched by position, so
an older deck - a v1 layout upgraded on read, a `"subtitle": ""`, slides
written before ids were assigned - names only the slides the person touched,
never every slide a re-validation spells out. One person's run of saves is
one history entry, an edit session (Collaboration > Versions).

Tests: `deck-merge.test.ts` and `autosave-policy.test.ts` (the merge
rules - one side's change, slides by id through insert / delete / move, an
edited slide surviving its deletion, a list by element, a clash kept as ours
and reported, nothing duplicated, the open text left alone by a change
elsewhere (`pathUntouched`) - and the save timing, retries, `Retry-After`,
`keepalive` size, status and body), `tests/test_presentation_autosave_postgres.py`
(the PATCH's row lock: two saves of one revision, one 200 and one 409; one
entry and one audit line per session; the spec-less presence beat),
`tests/test_presentation_editor.py` (backend tripwires), the node unit
tests `edit-paths.test.ts`, `inline-runs.test.ts` (with the commit check,
`inlineCommit`, and in-field move, `moveRange`), `block-schema.test.ts`,
`presentation-editor.test.ts` (the seed, the canonical JSON, the timeline a
save leaves behind - history kept, ids carried through a move / insert /
delete history with no slide under a neighbour's id, a redo stack surviving,
an undo during the round trip - the step a discard pushes, and what a
Discard does after a 409: waiting for the revision that won, deferred
and then abandoned for work started since, a refusal or a save of another
deck ignored; an answer about a base the session left, and a
revision older than the seeded one, never adopted),
`text-history.test.ts` (coalescing by kind, pause and contiguity, the
discrete boundaries - a cut, a drag, the browser's line break included - the
selection an undo restores, a redo branch dropped, the bound),
`lib/deck-revision.test.ts` (a late answer never rolls the cached deck back,
Nor a read answered after the save, through a real `QueryClient`), and
`tests_e2e/presentation-editor.spec.ts` (real input events
end to end: select, type, format, arrange, undo, palette, delete, save,
history line, reload, the byte-identical DOM once the panel is closed, and
Undo / redo from a panel field, across a save and after a discard, and
the text session walked through a formatting; the menu's
`historyUndo` / `historyRedo` routed from a panel field and walked by a text
under edit, and - under a frozen `Date.now()` - the same field on two slides
being two steps; a 409 raising the banner at once with the reload held
until the revision that won is in - Save off and Ctrl+S sending nothing for
the whole conflict - then Discard landing on it, a refused
in-place commit kept open in the inspector's words - one toast per press,
none per auto-repeat - and a drop inserting plain text - and an in-field drag
moving its text - once; a save answered after the poll adopted a
newer revision, a save answered on its base after a newer revision came in
over the draft it wrote (adopted at once, the next save extending
it), and a read answered after the save, all leaving the editor and the
stage on the newest deck; no banner while a save is in flight - its own
revision polled first, a collaborator's revision that is only a conflict
once the answer lands - and a close during a save waiting for its answer,
asking only when it failed).

## Collaboration

Three independent pieces, all keyed on the deck and all gated by the same
`deck_permission()` tiers as sharing — nothing here introduces a second
permission model.

**Versions** (`presentation_versions.py`, modelled on `flow_versioning`). Every
`save_presentation` records the spec it just wrote as a `PresentationVersion`
numbered by the deck's own `revision`, so the history panel, the editor's 409
message and the agent's tool result all speak about the same number. The row
carries `source` (`agent` / `manual` / `restore` / `duplicate` —
`VERSION_SOURCES`), `author_id` and a `change_summary`; `Presentation.
updated_by_user_id` is set on the same write so the library can say "Edited by".
Three invariants:

* **Append-only.** A restore never rewinds the counter: it saves the chosen
  version's spec as a NEW revision (`source = "restore"`), so a restore is
  itself undoable and "what happened to this deck" is a straight line. It goes
  through `save_presentation`, so a version written under an older validator
  that no longer passes is a 422 naming the field, never a blind write-back.
* **Fixed retention.** `PRESENTATION_VERSION_RETENTION` (30) newest entries per
  deck, pruned on every write; deliberately not configurable — an agent
  rewriting a deck ten times in one conversation must not evict the version the
  presenter wants back.
* **Never raises into the save.** Snapshotting is bookkeeping around the deck
  write; a failure is logged and the deck still lands.
* **Edit sessions**. The editor autosaves every few seconds, and one
  entry per save would evict the 30 kept revisions within minutes of typing.
  A content save from the editor is part of an edit session
  (`save_presentation(edit_session=True)`): it continues the author's open
  session - `open_edit_session`, the author's newest entry with `updated_at`
  set, last written less than `MANUAL_EDIT_SESSION_IDLE_SECONDS` ago, begun
  less than `MANUAL_EDIT_SESSION_MAX_SECONDS` ago, with no entry of another
  kind after it (other people's sessions above it do not close it: two people
  editing at once are one entry each) - which is MOVED to the new revision
  (spec, title, slide count, `updated_at`; its summary recomputed against the
  entry it now sits on), or opens one. The lookup runs once the write is
  vouched for, so a refused write reads no history. `updated_at` is NULL on
  every entry that is not a session (an agent edit, a restore, a template
  change - always an entry of its own - and every entry from before the
  column), and such an entry is never continued. The revision numbers in the
  list skip what a session folded in, and with two people editing at once a
  continued session moves above the other person's entry - each entry keeps
  the deck as it stood at its own revision, which is what a restore brings
  back. Its summary is diffed against the entry it now sits on, whoever wrote
  it: like every entry's, it says what changed since the one below. Only the save that OPENS a session is
  audited (`update`); the saves that continue it are in the entry itself - a
  sharing change in the same PATCH is audited as always.

The manual `PATCH` sends the whole spec on every save, so the summary is
derived from what CHANGED, not what was sent: `changed_spec_fields()` diffs the
stored spec against the freshly validated one (both are `validate_spec`
output, so a top-level comparison is exact) and `summarize_manual_edit()`
turns it into `Edited slides, title`. An unchanged spec is a no-op — no
revision bump, no empty history line. A template re-link alone still bumps,
because the deck LOOKS different afterwards and a collaborator's stale edit
must be told so.

**Comments** (`presentation_comments.py`). A `PresentationComment` is anchored
to a slide's STABLE `id` (assigned server-side, preserved across edits), never
to its position: a reorder or a deletion must not move a reviewer's note onto
another slide. A comment whose slide is gone is kept and served `detached`
(the panel labels it `Slide removed`); `slide_id = null` is a deck-level note.
Flat threads (`parent_id`), `resolved_at` / `resolved_by_user_id`, bodies
capped at `COMMENT_MAX_CHARS` (4000). Who may do what lives in ONE place and is
shared by the routes and the agent formatter:

* comment / reply — anyone who can READ the deck (`deck_permission` is not
  `None`): reviewing is exactly what a read-only grantee is for;
* resolve / reopen, edit / delete — the author, or a manager (`owner` /
  `config`, `MANAGE_TIERS`).

The agent works the notes **as the user**. "Implement this comment and
close it" is one sentence in the studio, so the assistant has the whole loop:

* `get_presentation` still appends the OPEN comments
  (`format_open_comments_for_agent`), now each prefixed with its id — the
  handle every other comment tool takes, so closing a note never means
  describing it;
* `list_presentation_comments(presentation_id, status=open|resolved|all,
  slide_position? | slide_id?)` is the wider read (`format_comments_for_agent`,
  bounded three ways — `_COMMENT_LIST_LIMIT` roots, the `MAX_REPLIES_PER_THREAD`
  most recent replies under each, `MAX_DIGEST_CHARS` overall — every bound
  reported as a count, never a silent cut), one slide at a time on request. The
  agent addresses a slide the way the outline shows it — 1-based
  `slide_position` — or by the stable `slide_id`, the same two selectors on
  every comment tool; `resolve_slide_anchor()` is the ONE translation between
  the two, and a position outside the deck is a precise error, never a note
  born detached. Every label follows the presentation's kind (`unit_noun` /
  `whole_noun`): a page brief reads "Page 2" and "Whole presentation", never
  "slide" or "deck";
* `add_presentation_comment(presentation_id, body, slide_position? | slide_id?,
  reply_to?)` posts a note or a reply through `create_comment`, under the
  user's name (the panel shows it as theirs — there is no "agent" author, the
  assistant is the user's pen); a reply inherits its thread's slide;
* `resolve_presentation_comment(presentation_id, comment_id, resolved=true,
  reply?)` closes or reopens a thread, optionally with the reply that says
  what was done, through the SAME `can_manage_comment()` gate the panel uses
  (`agent_deck_permission()` + author check), so an assistant can never close
  a note its user could not close by hand — a read-only grantee's agent gets
  a refusal that tells it to reply instead. Replies are not resolvable, as in
  the service.

Every agent write goes to the audit log as `presentation_comment` with
`details.via = "agent"`. The "only when the user asked for it, AFTER the change
it asked for is saved with `edit_presentation` (or `generate_presentation` for
a wholesale rewrite)" clause lives in the tool description and the digest
header — no system prompt text was added for it (rule 16). Resolving a thread
that is already resolved (or reopening an open one) changes nothing: no write,
no audit row, no closing reply — the tool says so and points at
`add_presentation_comment` with `reply_to` for a note on the thread anyway, and
the REST `PATCH` answers 200 with the unchanged row and audits only the fields
that changed. That holds under a race too: both paths load the thread with
`get_comment(..., for_update=True)` (a `SELECT ... FOR UPDATE` held to the
commit), so of two first-time resolves in flight at once the second waits,
reads the thread resolved and is that same no-op — one closing, one audit row.
The open-thread count the tool reports after a write is the same
grouped SQL COUNT (`count_open_comments()`) as the toolbar badge, run on the
tool's own session before its commit — never a Python recount over a re-read
list. The tools are registered unconditionally (unlike `export_presentation`): reading,
leaving and closing a note is part of editing a deck with an assistant. The
chat's `tool_done` frame for `add_presentation_comment` /
`resolve_presentation_comment` invalidates the `presentations` queries, so the
Comments panel shows the agent's note the moment it is saved.

New rows set `created_at` and `updated_at` from the SAME timestamp, or every
fresh comment reads as "(edited)". `count_open_comments()` feeds
`open_comment_count` on the list and detail serializers, which is what the
toolbar badge and the library card show.

**AI-drafted notes** (`presentation_comment_drafts.py`). The Comments composer
and the reply box carry the platform's `PromptGenerator` (the same collapsed
sparkle / expanded bar as agent personas and assignment prompts), fed by a
deck-scoped endpoint instead of the generic `/ai/generate-prompt`: `POST
/presentations/{id}/comments/draft` takes a `brief` (what the note should
say, `DRAFT_BRIEF_MAX_CHARS`), the `slide_id` on screen, an optional
`parent_id` (drafting a reply) and the `current_text` ("Improve with AI"),
requires READ access (drafting IS reviewing) plus `require_ai_enabled`, and
builds the context server-side — `slide_context()` renders the slide under
review as bounded JSON with its neighbours' titles (or the deck outline for a
whole-deck note), the thread's quoted messages for a reply, and the person's
own draft to improve. `clean_draft()` undoes a model's habits (fences, a
`Comment:` label, wrapping quotes, in either order) and clamps to
`COMMENT_MAX_CHARS`; `CommentDraftError` maps to a 502. Nothing is stored: the
draft lands in the textarea and the person posts it, or does not.

**Presence** (`presentation_presence.py`). A Redis hash per deck
(`presence_key`), one field per viewer carrying name, `activity`, `editing`,
`slide_index` and `last_seen_at`. The viewer heartbeats every
`PRESENCE_HEARTBEAT_SECONDS` (15, mirrored by `PRESENCE_HEARTBEAT_MS` in
`usePresentationApi.ts`), the whole hash expires `PRESENCE_TTL_SECONDS` (45)
after the last one, and the read side filters entries older than the TTL so a
closed tab is never shown as present even before the key expires.
`DELETE /presence` on unmount is the polite exit; the TTL is the guarantee.
Nothing is persisted, and a Redis failure degrades to "nobody else here", never
to an error in the viewer.

An entry's `activity` is what the other readers are told, and it is a
change on its way, never an open panel: `viewing` (the deck is open — an open
studio rail or editor panel with nothing happening in it is viewing),
`editing` (the person is changing the deck in the manual editor - since the
editor autosaves, the autosave's `active`: changes pending or being
saved, a text open in place, or a save in the last `EDITING_LINGER_MS`, so the
badge does not flicker with every save; client-asserted), `assistant` (an agent turn
is in flight in one of the person's conversations bound to the deck). The
assistant half is SERVER-DERIVED: the heartbeat route calls
`presentation_studio.studio_turn_active` — the caller's
`Conversation.presentation_id == deck` rows probed through the chat turn marker
(`chat_turn_guard.any_turn_active`, one `EXISTS`) — so it can neither be claimed
by a client nor get stuck in one: the marker goes when the turn does, and the
next beat says so. Nothing is remembered across beats. `editing` (the boolean)
stays on every entry as `activity != "viewing"` — what the sort order and the
badge key on — and an entry written before `activity` existed decodes from it.
On the client, the rail's `onBusyChange` (its `chatWs.isSending`) is forwarded
to `usePresentationPresence({ assistantBusy })`, which only uses it to TIME a
beat: a flip beats after `PRESENCE_ANNOUNCE_SETTLE_MS` (1.5 s — `isSending` is
optimistic and the server acquires the marker after its `thinking` ack, so a
beat on the same tick would read "no turn"). The avatars draw a pencil for
`editing` and a spark for `assistant`, with "{name} is editing with an
assistant" as the tooltip. This replaced `editing = panel open || studio open`,
which kept "X is editing" on a collaborator for a quarter of an hour with no
revision in History.

Where an editor works: while someone's editor is open, their beat
also carries `slide_id` (the stable id of the slide on their stage) and
`block` (the spec path of the block selected on it), both validated by the
schema to the shapes the editor writes (an id of the server's alphabet, a
dotted path of names and indexes) and announced once the selection settles
(`PRESENCE_SELECTION_SETTLE_MS`). The others' stages outline that block in the
person's colour with their name (`SlideEditLayer`, colours shared with the
avatars in `presence-colors.ts`), and the filmstrip shows their initials on the
slide they are on. The beat runs every `PRESENCE_FAST_HEARTBEAT_MS` (tab
visible) while the caller's editor is open or another reader is in the
editor (an agent turn does not count) - inside the TTL - and the response
carries the deck's `revision`: a newer one than the cached deck is refetched
at once, which is how a collaborator's autosave reaches an editor within
seconds (Autosave and merge, above). A beat reads no spec (`_load_visible(...,
without_spec=True)`, the column deferred with `raiseload`) and is budgeted per
person and deck (`PRESENCE_BEATS_PER_MINUTE`, `enforce_rate_limit`,
fail-open like the room: past the budget the room is just not refreshed).

REST: `GET /{id}/versions`, `GET /{id}/versions/{revision}`,
`POST /{id}/versions/{revision}/restore` (managers only — a restore is an edit;
409 when the revision is already current), `GET / POST /{id}/comments`,
`PATCH / DELETE /{id}/comments/{comment_id}`, `POST / DELETE /{id}/presence`.

Frontend: the viewer owns ONE `panel` slot (`editor` / `comments` / `history` /
`layout`, initialised from `?panel=` so the library's comment badge can land on a panel
directly; the chat card's `Open in studio` lands on the studio rail instead,
see below). The history panel's selected revision is lifted into the viewer,
which renders the selected version's spec in place under a `PreviewBanner` —
one source of truth, so `Back to current` on the banner and the panel's
selection cannot disagree. `DeckStage` exits presentation mode before opening
any side panel, since a panel beside a fullscreen stage is invisible.

## Presentation Studio (a chat bound to the deck)

Editing a deck through an assistant used to mean leaving it: `Edit with my
assistant` primed the main chat composer with a message naming the deck by id,
and the user then flipped between `/chat` and the viewer to see what changed.
The studio folds the chat into the viewer: a rail on the LEFT of the stage,
holding a real conversation (agent picker, transcript, attachments, steering,
`load earlier`) that is **bound to the deck**, while the deck on the right is
the live preview. The user never has to say which presentation they mean, and
the assistant never has to be told which tool to use.

**The binding** is a nullable `Conversation.presentation_id` FK (`SET NULL` on
deck deletion, `use_alter` because `presentations` already references
`conversations`). It is set at creation (`POST /chat/conversations` with
`presentation_id`) or after the fact (`PUT /chat/conversations/{id}/presentation`,
which is how the chat card's `Open in studio` carries the conversation that
PRODUCED the deck into the studio, so the agent keeps everything it already
knows about the work). Owner only, and the deck must be visible to the caller
— binding can no more probe for a deck's existence than reading it can. A
conversation is bound to at most one deck; re-binding moves it. Bound
conversations are the rail's history for that deck (`GET /chat/conversations
?presentation_id=`) but remain ordinary conversations in the main chat list.

**The turn** (`presentation_studio.py`). Every chat entry point (the streamed web
chat and REST, agent chat and General Assistant) calls `load_studio_context()` when the
conversation is bound. It resolves the deck through the CALLER's visibility
and the caller's tier through `agent_deck_permission()` — the same reading the
deck tools enforce (`load_writable_presentation`), so the prompt can state
exactly what an `edit_presentation` / `generate_presentation` call will be allowed to do: an
administrator is one on a company-managed deck only, never on a colleague's
personal deck. A deck that has become invisible (grant revoked, deck deleted)
degrades the turn to an ordinary chat rather than failing it. The context then
does two things:

* `ensure_studio_tools()` adds `STUDIO_TOOL_IDS` (`generate_presentation`,
  `template_search`) to the agent's built-in tool set for THIS turn only — a
  union, never a replacement, and never persisted on the agent. The rail's
  picker only offers agents that already carry `generate_presentation` (see
  below), so for a picked agent the union adds `template_search` at most; the
  guarantee exists for conversations bound by another route — the one that
  produced the deck, an older studio conversation, an explicit
  `PUT /chat/conversations/{id}/presentation` — whose agent never carried, or
  has since lost, the tool.
* `format_studio_prompt()` appends a `[Presentation Studio]` block BEFORE the
  security anchor (`insert_before_safety_anchor`) naming the deck, its kind,
  count, revision, theme, ratio, the caller's access tier, the rule "every
  change goes to THIS presentation: call `edit_presentation` with
  `presentation_id=…` (`generate_presentation` with that id only to rewrite it
  wholesale); never create a new one unless asked for a copy", the reminder to
  read the slide being changed first, and the compact outline
  (`summarize_slides`). The block ROUTES, it does not restate: what
  `edit_presentation` guarantees (the untouched slides stay byte-identical) is
  the tool's own description, so the studio prompt is the same size it was
  before `edit_presentation` existed (rule 16 of `AGENTS.md`). The client sends
  `studio_slide`
  (the 1-based index on screen) with each turn; `parse_focused_index()`
  validates it against the deck and the prompt then reads "the user is looking
  at slide N — *this slide*, *here*, *it* mean that one". The noun follows the
  kind (`slide` / `page`), so a portrait document is never called a slide.

**The rail** (`PresentationStudioRail.tsx`) is mounted by `PresentationViewer`
next to the stage, driven by `?studio=1` and `&c=<conversation>` — one URL
shape built everywhere by `studio-url.ts` (`studioUrl()`), so a studio link
can be pasted and reopened on the same conversation. `c=` outlives a collapse
(`?c=` alone is "folded away, this chat behind the handle"), so a reload while
collapsed re-expands onto the same conversation. A `c=` is only a claim: one
the caller cannot drive — a colleague's conversation (404 unless shared, then
`access: "read"`), or one deleted since the link was copied — is recovered
from, not crashed on: `useConversation(id, { notFoundIsOrdinary: true })` opts
that query out of the app-wide `throwOnError`, the rail toasts once, drops the
id and re-arms the auto-resume onto this deck's own most recent conversation.
Until the agents, the deck's history and (when resuming) the conversation have
loaded, the agent row shows a skeleton rather than a General Assistant label
that the resolved default would replace a beat later. It is the shared studio
rail (`components/studio/StudioRail.tsx`): the `@filigran/chatbot` panel
through `LazyXtmChatPanel` over `studioPanelEndpoints("presentation", deckId)`,
without the panel's conversation list (`hideConversationList`; the rail's own
history menu lists the deck's conversations). It carries
`DECK_KEYS_OFF_ATTR`, so arrows typed in the composer
never flip slides, and it passes the deck's id to the panel (`studioCards`,
published as `PresentationCardContext`) so a chat card for THIS deck inside the rail collapses to a one-line
receipt ("Saved — the presentation on the stage shows the latest version"):
the stage IS the preview, a second copy of it in the rail would be noise.

**The transcript and the composer are the panel's**: where the
rail opens (the end of its conversation), how it follows a growing transcript,
"Load earlier messages", attachments and the upload limit are
`@filigran/chatbot`'s, the same as on `/chat`. A run's result page
(`RunDetail`) draws a stored answer's cards outside the panel
(`InlineFileContent`).

**The rail's width is the reader's**. A ~380px column is right for a
short exchange and wrong for what the studio produces most of the time — the
outline of a whole deck, a slide-by-slide review — so the `<aside>` no longer
carries a fixed width class: `useResizableWidth` (`hooks/useResizableWidth.ts`,
the ONE implementation behind every user-resizable panel) drives an inline
`width`, and a divider on the rail's right edge (`presentation.studio.resize`)
drags it between `STUDIO_RAIL_MIN_WIDTH` (320px — header verbs, agent row and
composer stay usable) and `STUDIO_RAIL_MAX_FRACTION` (half the viewport — the
deck keeps the rest). The stage follows live because `PresentationDeck`
observes its root with a `ResizeObserver`. The hook stores the reader's
PREFERENCE (`localStorage`, `xtm-studio-rail-width`) and renders the preference
clamped to the CURRENT viewport, so a width chosen on a large screen survives a
detour through a laptop instead of being overwritten by the narrower clamp it
got there; it is a reading preference, never part of the `?studio=1&c=` URL
everyone shares. The divider is a WAI-ARIA window splitter — `role="separator"`,
vertical, `aria-valuenow/min/max`, focusable; Left / Right nudge it (Shift: four
steps), Home / End go to the bounds, a double-click resets — and it lives INSIDE
the `DECK_KEYS_OFF_ATTR` aside, so its arrows never flip slides. The drag is
captured on the handle (`setPointerCapture`), so the pointer keeps driving the
width while it crosses the stage, the body carries `col-resize` / no text
selection for the duration, and `Escape` mid-drag restores the preference from
before the drag. A live drag only updates the state — `localStorage` is written
ONCE, when the drag ends, and only when the drag moved the width (a synchronous
write per `pointermove` is drag jank on a slow device; a plain click on the
divider is not a choice). For the same reason a keyboard command or `setWidth`
that lands where the rail already is — `End` at the widest, an arrow pressed
against a bound — is a no-op: it must not pin the clamped width over a wider
preference the viewport is currently hiding (900px stored, 600px rendered:
`ArrowRight` keeps the 900px). The press focuses the handle (explicitly, with
`preventScroll` — a touch or a pen does not focus natively), so the arrows can
continue what the pointer started; it deliberately does NOT `preventDefault()`
the pointerdown: the body's `user-select: none` already stops a selection, and
Radix skips the tooltip trigger's own close-on-press on a prevented event, which
would leave the `Tip` floating beside the divider for the whole drag. While a
drag is on, the window also listens for `pointerup` / `pointercancel` / `blur`,
so a release the handle cannot see (capture refused) or a drag the window loses
(Alt-Tab, a native dialog) never leaves a resize cursor on the body. The default
is the width the rail shipped with (380px, 440px from `2xl`, via `matchMedia`)
and is resolved at RENDER whenever no preference is stored, so it keeps
following the viewport exactly as the responsive class did — a `reset` (the
double-click) returns to that live default rather than pinning it. When the
viewport leaves no room between the bounds (`max <= min`, a phone-sized window)
the rail leaves the divider out: a focusable splitter that cannot move, with a
"drag to resize" tip, is a promise the screen cannot keep. The trace peek drawer
still carries its own older drag implementation and is the candidate to migrate
onto the hook.

**The right-hand slot resizes too**. Comments, editor, history and the
layout check no longer each carry `w-[400px]`: `PresentationSidePanel.tsx` is
the one wrapper the viewer mounts around whichever panel `?panel=` opens
(`tests/test_presentation_studio.py` fails on a panel mounted outside it), and it owns the
width through the same hook with `edge: "start"` — the handle sits on the
slot's LEFT edge, so the hook inverts the drag and the arrow keys to keep
their screen direction. Bounds: `SIDE_PANEL_MIN_WIDTH` (320px, the composer
and the tabs stay usable) to `SIDE_PANEL_MAX_FRACTION` (a THIRD of the
viewport, not half: a review note is read next to the slide it is about, so
the deck keeps the larger share); default 400px; remembered under
`xtm-presentation-panel-width`, separately from the rail. The wrapper carries
`DECK_KEYS_OFF_ATTR` and the handle (`presentation.panel.resize`) lives inside
it, so resizing never flips slides; the panels fill it with `w-full min-w-0`.

The agent picker offers only the chat agents (enabled, honouring `disable_chat`)
that CARRY `generate_presentation` — the `Presentation Generation` entry of
an agent's tools — plus the General Assistant when it is not linked to an
agent and its effective tools (the user's `ga_builtin_tool_ids` override, else
the platform's `builtin_tool_ids`, the precedence `general_chat` applies)
include it. The turn would hand the tool to any agent, but an agent whose
persona was written for detection rules or STIX harvesting does not become a
deck editor by holding it for one turn — it ignores it or declines — so the
list is the agents built for the job, and the default is the one that already
worked on this deck (while it still carries the tool), else the user's own
default when it does, else a company-managed one, else any, else the General
Assistant. An install with none says so in the row, the picker and the empty
state ("No agent carries the deck tools yet", with a link to Agents) and does
not offer a conversation that could not edit; an EXISTING conversation keeps
its agent whatever it carries, and the origin conversation stays offered since
it brings its own. The empty state otherwise suggests concrete edits and, when
the deck came out of a conversation, offers to continue that one. Collapsed, the rail
is a thin handle strip (`PresentationStudioHandle`, same module); `Present`
leaves the studio first, since a rail beside a fullscreen stage is invisible.
Opening the studio changes nothing for presence: collaborators see
"editing with an assistant" only while a turn is actually in flight in the
rail's conversation — the server reads that off the chat turn marker, the rail
merely reports its `isSending` so the beat fires promptly — and an open, idle
rail is a reader like any other. The studio is offered to an editor only:
on a deck whose `can_manage` is false the viewer draws neither the
`Studio` verb nor the handle, folds a `?studio=1&c=` link away (state and URL),
and the chat card offers `Open in a new tab` instead of `Open in studio`; a
reader who wants to work on the deck goes through `Copy and edit with my
assistant`, which duplicates it first.

Entry points that land in the studio: the toolbar `Studio` verb (deck and
page — `StudioToggle`, a labelled `ToolbarVerb` like `Present`, never an icon
alone, with the tooltip reading `Edit with my assistant` / `Hide the studio`
and the pressed state while the rail is out), `Edit with my assistant` on the library card and in the actions menu
(`usePresentationActions` — a read-only deck is duplicated first and the
studio opens on the copy), and `Open in studio` on the chat card (binding the
producing conversation when there is one). `createWithAssistantPrompt` in
`assistant-handoff.ts` is the only primed message left: creating a NEW deck
still starts in the main chat, because there is no deck to bind yet.

## Presenting (fullscreen and its fallback)

`fullscreen.ts` has two modes, because `Present` must always present
something. `native` is the browser's fullscreen requested on the STAGE (not
the bare deck), so the toolbar comes along. **A presented deck fills the
screen edge to edge on a black stage.** Two rules in
`presentation.css`, pinned by
`test_frontend_fullscreen_takes_the_stage_and_hides_the_toolbar_when_idle`
(the letterbox by `test_frontend_stage_letterboxes_the_deck_in_the_app_background`
too since it became the stage's rule at rest):

- **The stage letterboxes the deck; the deck never letterboxes itself.**
  `.deck-root` paints `--deck-bg` across whatever box it is given (so do
  `.deck-canvas` and `.reveal-viewport`) and reveal centres the scaled canvas
  inside it, so a root the size of the screen showed the DECK's paper beside
  the slide - white for a light deck - on every screen whose ratio was not
  the deck's. The surface is a `container-type: size` grid painted with the
  letterbox, and the deck root is the largest box of the deck's own ratio
  that fits it, centred, in the surface's container units (`min(100cqw,
  100cqh * 16 / 9)` x `min(100cqh, 100cqw * 9 / 16)`, one rule per
  `data-aspect`); while presenting the surface is the whole screen and the
  letterbox black. Everything inside reveal fills the root exactly; the bands
  on a 16:10 or an ultrawide are the surface's. These are the
  stage's base rules, at rest as well (see below).
- **The toolbar is a transient overlay**, `position: absolute; top: 0` over
  the slide, a `rgb(0 0 0 / 0.55)` scrim, hidden when idle through opacity +
  transform (`data-deck-idle` after `IDLE_HIDE_MS` of stillness - the one
  number in `deck-idle.ts`, a module without React so the e2e spec reads it
  too; pointer activity brings it back, keys never do) and the pointer hidden
  with it (`cursor: none` on the SURFACE and every element in it - `cursor` is
  inherited, not cascaded, so a value on the stage alone would lose to any
  slide control with a cursor of its own; the bar and the dialogs portaled
  into the stage keep theirs). The hide spares a bar that is hovered, that
  holds the keyboard focus (`:has(:focus-visible)`) and that holds an OPEN
  menu (`:has([aria-expanded="true"])` - the jump list and the `More` menu
  render inline in the bar, and the stage's timer runs on under a menu being
  read). It once KEPT ITS ROW so a title set against the top
  edge was never covered - but the row took its height off the slide on
  exactly the screens the deck would have filled, pillarboxing every 16:9
  deck on every 16:9 monitor for the whole presentation, which is the common
  case and the one reported. The overlay covers the top of the slide only for
  the moment the bar is up - on a 1080p screen the 48px bar sits inside the
  slide's own 80px top safe margin - and on a screen taller than the deck it
  sits over the letterbox. The stage carries `data-mode="dark"` while
  presenting and `index.css` re-declares the dark token set on
  `[data-mode="dark"]`, so the bar and the menus portaled into the stage read
  light on black in the light app mode too; that subtree flips the MODE only
  - the XTM One accent is declared on `[data-theme="xtm-one"] [data-mode=
  "dark"]` as well, the administrator's accent on `ACCENT_SELECTOR`
  (`lib/color-utils.ts`: the branding block puts the accent on `:root` AND
  the subtree, the palette derived from the background on `:root` alone),
  and the `light:` / `dark:` custom variants read the subtree - so a brand or
  a per-mode override never comes out wrong on the black stage. The black
  itself is one custom property, `--deck-stage-bg`, re-declared on the
  presenting stage rule.

**At rest the letterbox is the app's background.** The letterbox rule was
scoped to presenting, so in the viewer, the studio and the chat card the
deck root still filled the surface and painted the deck's paper around the
slide wherever the surface's ratio was not the deck's - beside the studio rail
or the editor panel, in a chat card capped at 420px in a wide column. A light
deck whose cover carries its own dark theme showed that cover between
two white bands while the rest of the screen was the app's dark background.
The size container, the aspect-sized root and the surface painted with
`--deck-stage-bg` are now the stage's BASE rules, and `--deck-stage-bg` is
`hsl(var(--background))` on `.deck-stage`: the app mode, the product theme and
an administrator's branded background (`buildCssVarsBlock`) all come through
the token, whatever the deck theme and whatever the scheme of the slide on
screen - the page reader already showed its sheets on the app background. The
presenting rule keeps only what undoes the chat card's aspect box and height
cap. The root is centred by the ITEM (`place-self: center`), never by
`place-items` on the grid: the slide editor's layer is an absolutely
positioned child of the surface that must keep covering all of it, and it
measures its boxes relative to itself, so nothing else in the editor moves.
Pinned by `test_frontend_stage_letterboxes_the_deck_in_the_app_background` and
`frontend/tests_e2e/presentation-stage-letterbox.spec.ts`.

**The paper under a slide is the slide's own.** The deck root and
reveal's viewport used to paint the deck's paper under every slide. They are
snapped to whole device pixels, while a slide - scaled by reveal's transform,
from a scale reveal computes off the root's rounded `offsetWidth` /
`offsetHeight` - ends between two, and reveal composites the slides and its
background layer separately, each with its own anti-aliased edge. So the
deck's white showed as a hairline along one edge of a dark cover on a light
deck (and a dark line along a light slide of a dark deck), which edge
depending on the size of the stage and the device pixel ratio - invisible
while the bands were that same paper, obvious once they became the app
background. Every slide now hands reveal its own paper as its background
(`slidePaper()` in `theme.ts`: its palette's `background` for its scheme,
else `SCHEME_PAPER` - `var(--deck-paper-<scheme>)`, the token
`presentation.css` declares once and both token blocks read their `--deck-bg`
from, so nothing mirrors the colour; a palette gradient is no colour and rides
reveal's gradient layer over the scheme's paper instead of leaving the slide
transparent, and a slide without one hands that layer `none`, since reveal's
per-slide sync writes a gradient but never clears one; a value that paints no
colour - `none` or an `image-set()`, which reveal drops as one, a CSS-wide
keyword such as `inherit`, which the browser's colour check (`CSS.supports`)
takes but which paints nothing opaque - gives the slide its scheme's paper
instead, while a `var()` passes on trust and a colour the template makes
see-through - `transparent`, an alpha - is its own choice, the stage showing
through it as it showed through the deck root), which reveal
paints inside the clip, and on a stage the root and the viewport stop
painting `--deck-bg` once reveal is ready (until then the root keeps it, so a
deck still loading is never a hole in the stage; an engine without `:has()`
drops the whole rule and keeps the look from before, a hairline at worst).
Nothing else may paint under the slides: a deck-wide paper left inside the
clip, under the per-slide one, still leaks into the edge pixels at a
fractional ratio.

With nothing under them, the papers must cover the deck through every slide
change too. reveal hides the slides beyond its view distance with an inline
`display: none` on the slide AND its background, which on a jump across the
deck (the jump menu, a slide link, Home / End) took the outgoing paper away
while the incoming one was still on its way in: on a stage every background
stays laid out, and reveal's own `visibility` still decides what paints (the
overview moves the layer into `.slides` and keeps reveal's culling).
`backgroundTransition` slides the papers with a sliding deck; under a fade, a
zoom or a turn it cross-fades them in place - reveal fades both at once, which
lets the stage show through mid-way, so the stage holds the outgoing paper for
exactly the incoming one's fade (sliding papers there swept a hard seam across
the fading slide) - and it switches them at once for a deck with no
transition. The print view and the static render behind the exports are not on
a stage and keep the deck's paper. Pinned by
`test_frontend_the_paper_under_a_slide_is_the_slides_own` and
`presentation-stage-letterbox.spec.ts`: at a fractional device pixel ratio, a
dark cover on a light deck and a light cover on a dark deck in the light app
mode, each failing on a device-pixel line lighter or darker than both the band
and the slide; every slide's reveal background in its own paper - colour and
gradient layer - and laid out, the two slides beyond the cover's view distance
included; a deck theme picked in the editor repainting them before the save;
frozen frames of a jump across the deck, both ways (the leaving paper still
under the slide), and of a fade, both ways (the outgoing paper held whole, one
blend edge to edge); a gradient template paper painted under its slide; and a
gradient palette leaving no gradient behind when a draft takes it away (a
theme, no template) - on a draft already dirty, since the first dirty edit
moves the stage to the draft's own copy of the slides, which re-creates every
background.

`viewport` is the in-app fallback taken when the browser will not grant the
native one — the API reports itself available and then REFUSES the request (an
Electron webview, a kiosk or enterprise policy: "Permissions check failed"),
or is unavailable outright. The stage then sets `data-deck-viewport`
SYNCHRONOUSLY (a key handler racing the re-render must read the truth, as
`document.fullscreenElement` is the truth for the native mode) and moves into
the browser's top layer through the Popover API (`popover="manual"`: no light
dismiss, no `Esc` of its own) — the same layer `:fullscreen` paints in, above
every `z-index` and clear of any transformed ancestor, which is what lets a
deck present from inside a Radix dialog whose centring transform would
otherwise pin a plain `position: fixed` stage to the dialog. Where even the
Popover API is missing the same CSS still fixes it over the page.

`presentedElement()` is the ONE read for "is something being presented", in
either mode; `DeckStage`'s `Esc` handling, `deck-keys.ts`'s preview guard (a
dialog hosting a deck must not close on the `Esc` meant for the presentation)
and the toolbar all go through it. Every fullscreen CSS rule in
`presentation.css` is written for both selectors (`:fullscreen` and
`[data-deck-viewport]`) — the tripwire pairs them. In the toolbar,
`presentHere = !!onPresent || fullscreen.supported || !viewerUrl`: inside an
iframe without `allow="fullscreen"` (the chat embedded in OpenCTI / OpenAEV)
`Present` opens the standalone viewer, a tab that CAN go fullscreen, rather
than presenting inside a small embedded frame; with no viewer to offer, the
viewport mode it is.

## Speaker view and notes handout

Speaker notes had one surface before this: reveal's `notes` plugin, which
opens a popup and loads THE VIEWER PAGE into it to draw the "next slide" pane
— studio rail, toolbar and a chat conversation included — and offers the
notes as raw text. It is gone (`RevealNotes` is no longer mounted; the
tripwire refuses it). The presenter's surfaces are two routes of their own:

* **The speaker window** — `/presentations/:id/speaker`
  (`PresentationSpeaker.tsx`). No app shell: it is glanced at in a dark room
  from a metre away. Current slide (with a "screen is black" notice while the
  deck is paused), the notes in presenter-sized type (`+` / `-`, remembered in
  `localStorage`) with the deck's inline markup RENDERED through the same
  tokenizer as the slides (`SpeakerNotes.tsx`, never an HTML sink), the next
  slide, a stopwatch (`useSpeakerTimer.ts`, auto-started on the first slide
  change, persisted in `sessionStorage` so a reload mid-talk keeps the time),
  the wall clock, PACING against the notes' own estimate (spoken time left;
  re-scaled to a per-deck **target length** the presenter types in, also in
  `localStorage`), and the transport — prev / next / go-to / black screen — on
  the deck's own keys (`deckMayHandleKey` guards them the same way). The
  plan gives the CURRENT slide a window — the spoken time of the slides before
  it, to that plus its own — drawn on the pace bar; the talk is "on pace"
  while the clock is inside that window (plus `ON_PACE_SECONDS` of slack),
  "ahead" when the slide was reached before the window opened, "behind" when
  the window closed and the presenter is still on it. Measured against the
  window's START alone, every second spent talking over a slide would read as
  falling behind. The stopwatch auto-starts on the first slide CHANGE only:
  never on the first sync with the stage, a deck edit that shifts the index,
  or a move that went nowhere (Left on the first slide). The pace label is not
  a live region — it ticks — a visually hidden `role="status"` announces the
  STATE (on pace / ahead / behind / over) when it changes. `Close` closes the
  window when a script opened it and otherwise (the library's same-tab
  fallback, a typed URL — `window.close()` is refused silently there) goes to
  the deck.
* **The notes handout** — `/presentations/:id/notes` (`PresentationNotes.tsx`):
  every slide with its notes as ONE printable document (browser print -> PDF).
  Paper-first: the header is screen-only, a row never breaks across pages, and
  the thumbnail column is a fixed pixel width on screen AND in print (the
  preview scales from its MEASURED width, and a print pass is not guaranteed
  to run the resize observer again — a `%` column would print at the wrong
  scale). `Only slides with notes` leaves the dividers out.

Both draw slides through `SlidePreview.tsx`: the export's own `StaticSlide`
canvas (1920px) transformed onto a measured box — `fit="width"` (the box's
height follows the format) or `fit="contain"` (letterboxed in a fixed box, what
the current-slide pane uses so the notes keep the floor). The preview IS the
slide, not a screenshot of it, so an edit shows the moment the spec does.

**The bridge** (`speaker-sync.ts`): one `BroadcastChannel` per deck
(`xtm-deck:<presentation id>`), same origin, no parent/child relationship —
which is why the speaker view can be opened from a chat card, the viewer or a
fullscreen stage alike, and why it survives the opener navigating away. Every
`DeckStage` mounts `useSpeakerStage` (decks only — a page has no bridge and no
`S`): it posts its `state` (index, total, revision, blackout) on every move,
on every `hello` it hears and as a heartbeat (`STAGE_HEARTBEAT_MS`), so a
window opened late syncs at once and one whose stage died without a `bye`
notices within `STAGE_TIMEOUT_MS`. Every frame carries a `reason` (`move` /
`hello` / `sync` / `heartbeat`) because the follower must not read a heartbeat
as a move. The speaker view (`useSpeakerFollower`) follows the stage that
MOVED last and addresses its `command`s (`prev` / `next` / `goto` /
`blackout`) to it, so when the same deck is open in a chat card and in the
viewer the one the presenter is using is the one shown and driven, and the
two never fight over the keys. Only a `move` switches the followed stage —
were a heartbeat enough, two idle stages would swap the window's slide every
few seconds — and only the followed stage's frames feed the watchdog, so
another stage's heartbeat cannot keep a dead one alive. The other stages are
remembered (freshest frame each) — including the one the window just switched
AWAY from, filed with its last frame the moment a move on another stage takes
the window, so it need not be heard again first: when the followed one says
`bye` or falls silent the freshest of them takes over, and only with none left does the
window drop to REHEARSAL mode — same notes, same timer, its own position — and
says so. The stage says `bye` on unmount AND on `pagehide`, because a closed
tab or a full navigation unloads the document without running the effect
cleanup — the hand-over is immediate in the common case and the watchdog is
the fallback, not the path. A `state` carrying a newer `revision` than the loaded deck
invalidates the query, so a studio edit mid-rehearsal lands without a reload
(the stage reads the revision through a ref so the channel is not reopened on
every edit). A frame whose fields are not the numbers they should be — a stage
from another build in a stale tab — is dropped rather than turned into a `NaN`
slide. Everything degrades to a no-op where `BroadcastChannel` is missing.
The bridge is driven for real in `frontend/tests_e2e/presentation-speaker.spec.ts`
(two Playwright pages of one context are two windows of the same origin): a
window opened after the stage and one opened before it, navigation both ways,
`B` from the window, the fall to rehearsal on the presenter's slide when the
stage leaves, and — with two stages on one deck — following the one that MOVED
last, a heartbeat not moving the window, the hand-over when the followed tab
closes, and the hand-back to the stage just left when the new one closes
within a heartbeat.

`speaker-url.ts` is the ONE place the two URLs and the window features live;
`openSpeakerView` reuses a named window per deck (pressing `S` twice focuses
the window it opened, it does not open a second) and returns `false` when the
browser refused the popup, which `DeckStage` turns into a toast that says so.

**Layout** (`speaker.css`): two columns (current + notes | next + clock) above
820px; under it ONE column with the side column turned into a row — the clock
first, the next slide beside it — and the grid rows sized to their content
(`grid-auto-rows: max-content; align-content: start`). That last rule is a
regression guard: the container has a definite height (the shell's middle
row), so plain `auto` rows are STRETCHED to equal shares of it and two columns
taller than their share overflow into each other — the "next slide drawn over
the notes" bug the first cut shipped with. The side rule is written as
`.speaker-column.speaker-side` on purpose: `.speaker-column { display: flex }`
sits later in the file and would win a single-class tie.

**The backend half** (`presentation_notes.py`) exists so the agent and the
exports speak about notes the way the window does:

* `WORDS_PER_MINUTE = 130` and `NOTES_OPTIONAL_LAYOUTS` (`title`, `section`,
  `agenda`, `closing` — slides one does not talk over) are declared on BOTH
  sides (`speaking-time.ts` mirrors them; `test_presentation_notes.py` pins
  the two equal), so the "about 6m 30s" in a tool result is the plan the
  pacing bar measures against. `format_speaking_time` reuses
  `format_wait_duration` so a duration reads the same everywhere.
* Words are counted on the SPOKEN text (`notes_plain_text` first): a marker is
  not a word, a hyphenated or elided word is one. `notes_plain_text` strips the
  markup the LENIENT way (`markup_to_plain_text_lenient`), i.e. exactly as the
  frontend tokenizer renders it — an unknown `[[tone:…]]` keeps its text and
  drops the wrapper, an unbalanced marker or a tag stays literal — rather than
  through the strict parser, whose "keep the raw line" fallback would count
  `[[warn:risk]]` as two words and print it as such in PowerPoint where the
  presenter reads one. Scripts written without spaces (Japanese, Chinese) are
  counted by the CHARACTER at `CJK_CHARS_PER_MINUTE = 300` and converted to
  word-equivalents, on both sides over the same character class (pinned) —
  otherwise a whole Japanese sentence is one word and a Japanese deck takes
  "2s" to say.
* `notes_coverage` / `format_notes_coverage` produce the `Speaker notes:` line
  every `generate_presentation` result carries (`notes_line`), and the outline
  the agent edits from flags each content slide without notes with
  `(no notes)` — visible where the slide is addressed, not only in a total
  under it. The craft rule in `presentation_guidance.py` says what a note is
  (what to SAY, not the slide re-read) and the `presentation-design` skill
  carries the register.
* `pptx_writer._set_notes` writes `notes_plain_text(notes)` to the notes pane
  — PowerPoint would otherwise show `**` and `[[warning:…]]` to a presenter
  who never saw the deck on this platform — and skips the pane for an empty
  note. `notes` are deliberately NOT markup-validated at save time (a
  presenter's shorthand is not a slide), which is why the stripping lives
  here and not in `validate_spec`.

## Export

`presentation_export.py` renders every export through the SAME renderer as the
screen: the platform's headless Chromium (already in both images for the
`browser` tool and `generate_file`) loads the static render page
`/presentations/{id}/static` (`PresentationStaticRender.tsx`), which lays every
slide out as one unscaled `.deck-static-slide` canvas, one under the other, no
reveal runtime, and flags `data-deck-ready` once the spec, the fonts, every
image and every Mermaid diagram have settled (or `data-deck-error`).

* **Render token** (`mint_render_token` / `decode_render_token`): a confined
  JWT — `scope = presentation_render`, bound to ONE `presentation_id`, three
  minutes — that `get_current_user` refuses on sight (`is_confined_token`) and
  that only the two render routes accept: `GET /{id}/render-spec` and
  `GET /{id}/render-asset/{file_id}`, the latter serving a file iff its id is
  in `referenced_file_ids(spec, theme)` (every `*_file_id` in the spec plus the
  theme's logo keys). It travels in the query string because `<img>` cannot
  send a header; it never leaves the server process. The render-spec route
  still applies the visibility gate for the token's user.
* **Network policy**: the browser context aborts every request that is not for
  the render origin (or `data:` / `blob:`), so a deck cannot make the exporter
  fetch anything external.
* **Where the page lives** (`render_base_url`): `PRESENTATION_RENDER_BASE_URL`
  wins; else the unified image (detected by the `frontend_dist` directory) uses
  `http://127.0.0.1:4000` — no ingress round-trip, no public-hostname
  resolution from the pod; else `FRONTEND_URL` (split dev setup).
* **PDF**: `page.pdf()`, one page per slide (`break-after: page`), paper set to
  PowerPoint's slide sizes (`PDF_PAGE_SIZES_IN`: 13.333x7.5in / 10x7.5in, and
  real A4 / Letter for the portrait formats) with `PDF_PRINT_SCALE = 720/1080`
  so 1 canvas px = 0.5pt exactly as in the PPTX; text stays vector and
  selectable. The paginator works on LAYOUT boxes: a CSS transform
  moves what is painted, never what is fragmented, and a single line box has
  no break opportunity — so a decoration whose untransformed box crosses the
  slide's bottom edge is pushed whole onto the NEXT page and clipped by the
  slide's overflow (the section slide's ghost number, centred with `top: 50%`
  + `translateY(-50%)`, printed truncated at the bottom of every section slide
  that had a slide after it). A bleed decoration is therefore placed by its
  layout box (`top: calc(50% - 0.5em)` on a `line-height: 1` line), never by a
  vertical transform; `page.emulate_media("print")` does not paginate, so only
  a real `page.pdf()` rasterization shows the difference.
* **PPTX, pixel-perfect** (`pptx_writer.build_image_pptx`): one full-bleed PNG
  screenshot per slide, speaker notes attached as plain text
  (`presentation_notes.notes_plain_text` — the notes pane never shows
  the raw inline markup).
* **Transitions in both PPTX variants**: `export_presentation` hands
  the deck's `transition` to both writers, and `_set_transition` writes the
  closest base PresentationML transition on every slide
  (`_PPTX_TRANSITIONS`, pinned to `PRESENTATION_TRANSITIONS` at import):
  `fade` -> fade, `slide` -> push from the right, `zoom` -> zoom in, reveal's
  3D `convex` / `concave` -> push / cover from the right, `none` -> none, at
  `spd="med"` (0.75 s, reveal's default pace), after `p:clrMapOvr` as the
  schema orders it. The base set, not the PowerPoint 2010 `p14` effects: no
  `mc:AlternateContent` fallback to get wrong, and every version opens it
  without a repair prompt (PowerPoint reads back Fade Smoothly, Push Left,
  Cover Left and Box In, its object model's name for the base zoom).
* **PPTX, editable** (`build_native_pptx`): `EXTRACT_PRIMITIVES_JS` walks the
  rendered DOM and returns, per slide and in paint order, `rect` (visible
  background / border), `text` (box, padding, alignment, one run per style
  change) and `image` (an `<img>`, an inline SVG, a gradient-text or
  background-image element — screenshotted by `data-export-id`) primitives in
  canvas coordinates; the writer places them as PowerPoint shapes, blending
  alpha against the slide background (a PPTX solid fill has no alpha) and
  mapping CSS families to installed fonts (`pick_font`). Lessons the collateral
  taught the extractor, each pinned in `test_presentation_export.py`: an inline
  `<svg>` is recognised by namespace (its `tagName` is lowercase, unlike HTML);
  `color-mix()` tints compute to `color(srgb ...)` and are parsed as such; an
  icon sharing a line with text yields a picture PLUS a text frame measured on
  the text nodes alone; stroked "ghost" numbers stay editable text with an
  `a:ln` outline; a single-line box keeps word-wrap off and a wrapped one gets
  `_WRAP_SLACK_RATIO` slack on the unanchored side (PowerPoint's line breaking
  is not Chromium's); borders are read PER SIDE and painted per edge (a card is
  a grey frame with a toned top rule, not a frame painted whole in the tone —
  `edges.colors`); and CSS `text-transform` is applied to the exported runs so
  an uppercase eyebrow exports uppercase instead of in the source's case.
* **Bounds**: `MAX_CONCURRENT_RENDERS = 2` per event loop, `READY_TIMEOUT_MS =
  60s`, `NAVIGATION_TIMEOUT_MS = 30s`; a render that does not settle fails with
  `PresentationRenderError` (a 502 on the REST route) rather than hanging.

Surfaces: `POST /presentations/{id}/export?format=pdf|pptx|pptx_native`
(streams the file, `Content-Disposition` via `build_content_disposition`), the
`export_presentation` tool (persists through `_persist_generated_file` and
returns a `[[FILE:<id>]]`-style download like `generate_file`), and
`PresentationExportMenu.tsx` (shared by the chat card and the viewer so the two
never offer different formats — `test_frontend_offers_every_export_format`).
The browser-print path (`?print-pdf`) stays as the no-server fallback.

**The browser print always starts.** It waits on `deckSettled()`
like the export page, and a figure that could never settle held that wait for
good: a chart canvas with no room, a lazy chunk the network held or that
failed (it reached the route's error boundary, which replaced the deck), a
Mermaid render or the icon catalog that never answered. Now every source of
`data-deck-pending` settles or gives up on its own, in VISIBLE time
(`visible-time.ts`: counted frame by frame, a frame for at most
`FRAME_STEP_MS`, one frame loop for every wait on the page):

* a chart canvas with no room gives up after `STALL_MS`, a drawing whose chunk
  has not landed after `FIGURE_WAIT_MS` (15 s); a canvas that gets room later
  is a new build, pending again;
* the Mermaid chunk, and Mermaid's own load and render, give up after
  `FIGURE_WAIT_MS` with the placeholder standing; a late answer still draws;
* an icon (`DeckIcon`, the architecture's `SvgIcon`, through
  `useDeckIconCatalog`) whose catalog failed or has not landed keeps its
  placeholder box; the architecture's logo holds the signal while its file is
  fetched (an invisible `.deck-arch-logo-pending` box, `useDeckAsset`'s
  `loading`) and while its `<image>` bytes arrive, and each wait gives up after
  `FIGURE_WAIT_MS`;
* a lazy drawing that fails to load, or throws, is caught by its own
  `FigureBoundary` and shows the block's error state (the diagram's existing
  one, the chart's `.deck-chart-error` line); Mermaid itself failing to load
  is unfinished too, while a source Mermaid refuses is the deck's content and
  keeps drawing its error as before.

A figure that gave up or could not be drawn drops `data-deck-pending` and
carries `data-deck-unfinished`. The print passes `deckSettled()` a last resort
of its own, `PRINT_SETTLE_MS` (twice `FIGURE_WAIT_MS`), for what nothing else
bounds (a font, a logo's bytes still on their way); `complete` is `false` when
that ran out or any `[data-deck-unfinished]` is left, and the viewer then says
so once `window.print()` returns (`Part of the deck was not finished`, naming
the chart, diagram, icon, picture or font that may be missing; toasts are
`print:hidden`). "Settled" still means one thing - no `[data-deck-pending]`
left - for the export page, `deckSettled()` and the specs.

The export page is not bounded: the give-up hooks do nothing under
`[data-deck-static="true"]` and it calls `deckSettled()` with no bound, so the
exporter and the layout check keep waiting under their server-side timeouts.
A chart canvas with no room is the one case that is not a wait there.
The page hides no slide, so the room will not come: the chart settles at once,
empty and not unfinished, and the layout check reports it as a `chart` finding.
Nothing gives up there, so an unfinished figure is one that could not be drawn
(a chunk, Mermaid or the icon catalog that failed to load, a drawing that
threw): `PresentationStaticRender` turns `complete: false` into `deckError`,
and the export fails at once with that reason (the layout check reports it as
skipped) instead of shipping the figure's error state - where the render used
to hang until `READY_TIMEOUT_MS` and fail with "did not finish rendering in
time" (`test_static_render_fails_on_a_figure_that_could_not_be_drawn`).

**The tool is offered on every turn**. Its description says to use it
only when the user asks for a file, and that call is the model's: the platform
reads no word of the request for it (invariant 18). The gate it replaces
(`asks_for_export_file`) offered the tool only when the request matched
a list of export / download / print / file stems in six languages; a list like
that is bound to the languages it names and silent on the phrasing it misses.
What the platform does hold is the deck's `revision` and its template as the
render resolves it (`resolve_template_export_stamp`: a digest of the palette
`resolve_template_theme` resolves for the deck's theme, the container's
`theme_config` and `extra_metadata`, and every asset's id, role, description
(the picture's alt text) and `file_version`, since an edited template restyles a deck without a new
revision, a chrome pattern, a hero or a deck default lives outside the palette,
and an asset replaced in place keeps its id; not the container's `updated_at`,
which misses that replacement and moves on the `lookup_count` analytics): the
builder keeps the file each export stored by deck, revision, format and that
stamp, so exporting a deck nobody changed since, in the same turn or run, hands
back that same file ("has not changed since you exported it", the same
`[[FILE:id]]`) instead of a second render and a second attachment. Another
format, the deck saved again or its template edited in any way renders anew
(`test_exporting_a_deck_already_exported_hands_back_its_file`).
`export_report` follows the same contract by report, revision and stamp
(`resolve_report_export_stamp`).

### Charts in the exports

A `chart` block is data, so the editable PPTX gets a CHART, not a picture of
one — the acceptance criterion the board deck was rejected on. The three
formats take three different paths from the same rendered page:

* **PDF: vector, from the same SVG.** The static page draws every chart as
  inline Recharts SVG, and `page.pdf()` prints inline SVG as vector — the
  glyphs, the bars and the target line are paths on the page, in the very
  colours and fonts the viewer computed (the deck's palette resolved through
  `themeConfigFor`, the page's loaded fonts). Nothing is rasterised and
  nothing is re-drawn server-side; a vector figure is sharper than the 2x
  raster the issue asked for as a floor, and stays selectable text where the
  labels are text. Only the pixel-perfect PPTX is a screenshot, and it was
  one before charts existed.
* **Editable PPTX: native charts** (`pptx_writer._add_chart`, the python-pptx
  chart API). The figure publishes its own data for the extractor — the
  `data-deck-chart` attribute on the `<figure>` carries `chartExportPayload()`
  (`chart-format.ts`: kind, categories, series with their RESOLVED colours,
  slice colours, format / unit, stacked / horizontal, legend and value-label
  switches, target + its name, locale), and the drawing publishes what only
  the drawing knows in a hidden `.deck-chart-facts` span: the value axis it
  settled on (`data-min` / `data-max` / `data-step`, the same `niceTicks()`
  the viewer drew), whether it wrote each bar's or stack's own value
  (`data-values`) and how (`data-label-size`, `data-label-rotate`,
  `data-segment-size`). `EXTRACT_PRIMITIVES_JS` (`emitChart`) reads
  both (`label_size` / `segment_size` in canvas px times the body's fit,
  `label_rotate`), the tick font the chart's `label_size` set (`style.size`)
  and the legend's own size (`style.legend_size`: the legend is the
  viewer's HTML text, not a tick, and PowerPoint's legend takes its size;
  the tick size where the page measured none), measures
  the boxes the writer needs — the figure, the `.deck-chart-canvas` plot, the
  legend (its items, the target entry excluded), the INNER plot area
  (`.recharts-cartesian-grid`, or the union of the pie sectors), the target
  line (`strokeBox`: a `<line>` has a zero-thickness box, so the stroke width
  is what makes it a rectangle), every `.deck-chart-value` label with its
  role, series, category and turn — and the paint (`paintOf`) of the ticks,
  labels (off a label that is not a segment's), grid, axis, target and legend, plus
  the surface the chart sits on (a framed figure's card, `.deck-panel` inside a
  dashboard, the slide for a bare chart), and emits ONE `chart` primitive.
  A framed figure's card is emitted first, by the `emitRect` every
  element box goes through - a plain rounded shape under the chart, its fill
  and hairline editable; a figure that is `display: contents` (in a panel)
  has no box and emits none. `.deck-chart-center` (a donut's
  reading) and the caption stay text primitives.
  The chart's text is set in the sizes the page measured; a page that
  measured no tick font falls back on the tick size of the payload's
  `label_size` (`chart_export.chart_tick_px`, the fit included), and values
  with no measured size on the first size of the payload's
  `value_label_size` (`chart_export.chart_value_px`, the same
  conversion), else on the ticks' size as before. The payload carries
  `value_label_size` only when the block names one on a kind it acts on.
  The writer rebuilds the figure as PowerPoint would draw it: `bar` /
  `stacked_bar` / `line` / `area` / `pie` / `donut` / `combo`
  (`NATIVE_CHART_KINDS`) map to `_chart_type` (`COLUMN_*` / `BAR_*` when
  horizontal, `LINE_MARKERS`, `AREA` / `AREA_STACKED`, `PIE`, `DOUGHNUT` with
  `holeSize` 62, and a combo built as `COLUMN_CLUSTERED`); the series
  keep their names, values and colours (blended on the surface, since a
  solid fill has no alpha). A chart whose series are drawn two ways or read
  against two value axes is ONE plot per group (`_plot_groups`: bars
  on the left-hand axis, then on the right, then lines, painted in that
  order so lines sit over bars): `_split_plots` rewrites python-pptx's single
  plot into those plots (a line plot taken from python-pptx's own line chart
  XML of the same data, so every series keeps its `c:idx` - its workbook
  column - and its line-only children), and `_add_secondary_axes` adds the
  right-hand `c:valAx` (`axPos` `r`, crossing at `max`, no gridlines) over a
  hidden copy of the category axis (`delete` 1), both copies of the primary
  pair so they meet the categories the same way. Each value axis is pinned
  to the viewer's ticks (`axis` / `secondary_axis`), written in its own
  format WITH its unit (`axis_number_format(..., with_unit=True)`:
  `#,##0.0"x"`), in the tick ink the viewer gave it (`style.primary_tick` /
  `secondary_tick`); each plot labels its values in its axis' format, a
  combo's line where the viewer wrote them (`_add_point_labels`: each
  series' own `c:dLbls` above its points, a point's `c:dLbl` with `dLblPos`
  `b` - the series' `numFmt` / `txPr` copied in, a point label inheriting
  nothing - where the viewer wrote it under, deleted where it raised it or
  slid it clear of the axis (`_PLACED_AS_TEXT`), which is then the measured
  text, or wrote none). A line or area series whose values the viewer moved or
  left out (`_points_moved`), or planned alternating over and under
  their points (`values_alternate`: every series of the chart), gets
  the same point by point, an area's
  value under its point as text (an area's labels take no position). A bar
  plot whose values the viewer fitted (`values_fit` stagger / upright)
  is labelled point by point too (`_add_fitted_bar_labels`): each
  series' own `c:dLbls`, `outEnd`, at the viewer's size; a value the viewer
  wrote a row out (`lift`, the extractor's `data-lift` times the fit) a
  point's `c:dLbl` carrying `c:layout` / `c:manualLayout` with `c:y` the lift
  over the chart frame's height (up past a rise, down past a fall - the offset
  PowerPoint records for a label dragged by hand) and the series' `numFmt` /
  `  txPr` copied in; a value it left out, or a zero with no bar, deleted. Bars
  the viewer fitted values around (`bar_spacing`: bars that gave way, or a
  group of upright bars kept tight) take `c:gapWidth` / `c:overlap`
  from the viewer's width and gaps (`_bar_spacing`, within PowerPoint's
  0-500 and -100-100), the writer's constants otherwise; a stack's totals stay the
  measured text, raised where the viewer raised them. A series
  whose values all sit over their points keeps the plot's labels, its XML
  unchanged; a series marked `show_values: false` has its labels deleted
  (`_delete_series_labels`), and a combo's line is labelled whenever the page
  wrote its values, the bars' own off included. A pie's labels
  (`_place_slice_labels`) are PowerPoint's own at the size the page set them
  in, `outEnd` with `c:showLeaderLines` where the viewer wrote them outside,
  `ctr` where it wrote them inside (`values_placement`, read off
  `data-values-placement`), each such point's `c:dLbl` inked from its slice
  (`_slice_ink`, the `_segment_ink` rule) in the SERIES' `c:dLbls`, which
  PowerPoint reads over the plot's, and a slice the viewer wrote no value on
  deleted; a donut's values stay the measured text (a doughnut's labels sit
  on its ring), one inside its slice inked alike, and its leaders are drawn
  as connectors between the ends the extractor read off each
  `.deck-chart-slice-leader` (`slice_leaders`, `_add_slice_leader`). A pie
  has no axis: the chart's text size is its `--deck-chart-text`, never a
  value's (each carries its own). The right-hand target is a `ROUND_DOT`
  connector. `_value_axes()` finds the two axes by id: python-pptx's
  `Chart.value_axis` answers the SECOND `c:valAx` when there are two (an XY
  chart's Y axis). The legend sits at the top on a MANUAL layout
  over the measured legend box (widened by `_LEGEND_SLACK_RATIO` — PowerPoint
  sets legend type wider than Chromium — and by `_LEGEND_LINE_KEY_PX` per line
  series, whose key is wider than the viewer's swatch; it grows into the room
  the frame has on its right first, a panel's legend ending at the frame's
  edge, because PowerPoint drops an entry that does not fit its box). A legend
  the viewer wrapped (`legend_rows` > 1) is stacked instead (`RIGHT`, one row
  per series): PowerPoint cannot be given the viewer's wrap, and a top legend
  it cannot fit is laid out anew over the panel title. A block that says
  where its legend sits (`legend_position`, read off the payload) puts
  PowerPoint's legend on that side - `c:legendPos` `b` for `bottom`, pinned
  to the measured box widened both ways alike since it is centred under the
  plot, `r` for `right` (python-pptx writes that schema default by omission),
  `t` for an explicit `top` - and the stacked fallback belongs to the unset
  default alone. The inner plot area is laid out
  manually over the measured grid so the bars land where the viewer drew
  them, the value axis is pinned to the viewer's `min` / `max` / `step` with
  dashed gridlines and no axis line, a horizontal chart lists its categories
  top-down (`reverse_order` + the value axis crossing at `max`, which
  python-pptx 1.0 has no property for, so it is one `c:crosses` element), and
  the frame runs `_AXIS_FRAME_SLACK_PX` past the canvas below (and to the
  left when horizontal) because PowerPoint clips chart content to its frame
  and places bottom axis labels lower than the viewer did. `plot.
  vary_by_categories` is set `False` explicitly on every axis chart — with
  the element absent, PowerPoint colours a single series by category and
  lists the categories in the legend. Value labels are PowerPoint DATA LABELS
  where PowerPoint can place them (`OUTSIDE_END`, `ABOVE` for a line; area
  and doughnut accept no position), at the viewer's `label_size` and turned
  on their side (`a:bodyPr/@rot` -5400000) where the viewer turned them, and
  otherwise the TEXT the viewer wrote, placed as text boxes
  (`_add_chart_label`) — the stack totals (a turned one laid out upright and
  rotated 270 about the centre it was drawn on), the donut's reading. Such a
  box is pinned where the viewer pinned the text: the extractor reads
  each value's `text-anchor` (`anchor`), and a `start` value - a horizontal
  chart's total, written from its bar's end - is a left-aligned box on the
  measured run with no slack, so what PowerPoint's glyphs add runs into the
  room the viewer kept inside the frame (`labelRun`) rather than back over
  the bar; an `end` value keeps its right edge; a centred one keeps the 6px
  symmetric slack it had. The
  values the viewer wrote INSIDE a stack's segments are series data labels,
  `CENTER`, on exactly those segments (`_add_segment_labels`: every other
  point's `c:dLbl` is a `c:delete`), inked as the viewer inks them
  (`_segment_ink`, the same OKLCH-lightness threshold, pinned against the
  stylesheet by a test). A thin segment's value the viewer wrote PAST its
  stack (role `annotation`) is its text, placed like a total, in the
  series' resolved colour at the size the page published
  (`annotation_size`), and its leader is drawn leg by leg (`leaders`, each
  leg's `strokeBox` and series) as solid connectors in the same colour at
  the stroke the viewer measured (`_series_rgb`, `_add_rule`) - also on a
  chart that writes no totals, whose series asked for its own values. The
  values' paint is read off a label that is neither a segment's nor such a
  value. A stack is as wide as the viewer's
  (`_BAR_GAP_WIDTH_STACKED` = 32, the viewer's 12% a side). The target line is a dashed straight connector along the
  measured stroke box, named by a text box above its right end
  (`_add_target_line`). Number formats are Excel format codes derived from the
  block's `format` / `unit` and the values actually plotted
  (`chart_export.label_number_format` / `axis_number_format`): a currency is
  compact per BAND (`[>=1000000]"€"#,##0.0,,"M";[>=1000]"€"#,##0,"k";"€"#,##0`
  — a band carries a decimal only if one of ITS values needs it: whole
  million ticks read `€0` .. `€30M`, while labels of `€16.5M` and `€30.0M`
  share the decimal, the one thing an Excel format cannot avoid), a percent is
  `General"%"`, a number is thousands-grouped with the unit stuck on the
  labels only (`#,##0" users"`, `#,##0"h"`) and compact on an axis whose top
  tick reaches `AXIS_COMPACT_FROM` (10 000 — the viewer's own threshold,
  mirrored by a test) - as are the labels of an axis chart whose axis
  reaches it (`value_extent`, the mirror of `axisExtent`). A `waterfall` (`EMULATED_CHART_KINDS`) is a
  `COLUMN_STACKED` chart built from the bars the viewer DREW (the payload's
  `steps`, gap bars included — the writer never sums a bridge,
  `_waterfall_columns`): an invisible `Base` series under each bar, the bar,
  and an `(below zero)` series for the part of a bar that crosses the axis
  (PowerPoint stacks positive values up and negative ones down from zero);
  each point wears its step's colour (`step_colors`, resolved by the
  extractor), the legend hides the base and the below-zero part
  (`c:legendEntry` + `c:delete`), the signed labels are the viewer's text
  (a data label would write a bar's length) and the connectors are dashed
  connectors on their measured stroke boxes (`_add_rule`, shared with the
  target line). PowerPoint's own waterfall is a `chartex` part python-pptx
  cannot write and Keynote, Google Slides and LibreOffice draw as a blank
  box, so the emulation is the editable form everywhere, and the log says so
  (`chart_export.emulation_note`). A `butterfly` (`MIRRORED_CHART_KINDS`)
  is a native `BAR_CLUSTERED` chart, never an emulation nor a picture: its
  two series overlap fully on each row (`c:overlap` 100, the gap width from
  the viewer's `data-bar-*`), the first stored NEGATIVE so it runs leftwards,
  `invertIfNegative` off (PowerPoint fills a negative bar white otherwise),
  and its labels and value axis written with `mirrored_number_format`: the
  code a bar chart would take, as both its positive and its negative section
  (`#,##0;#,##0`, the issue's `0;0`) - Excel writes no sign in a negative
  section, so the left side reads 80, never -80, and editing its data sheet
  means typing a negative number. The labels' code ends on an empty zero
  section (`#,##0;#,##0;`): a zero - which the viewer draws no bar and
  writes no value for - is labelled with nothing, while the axis code keeps
  two sections and its `0` tick. A compact code of conditional bands has no
  negative section, so amounts reaching two tiers are written in the larger
  one (`"€"#,##0.00,,"M"` twice, the tier a `unit_scale` would pin, with the
  decimals that separate them). The axis is the viewer's (`data-min` the
  left extent negative, `-max`, `-step`); the categories read top-down on
  the left axis (`reverse_order`, `c:crosses` max, `tickLblPos` low).
  PowerPoint's bar chart has no column between its sides, so a centre spine's
  categories move to the left axis - the plot gives them a column of its
  left, the viewer's estimate of the longest (`_TICK_GLYPH_EM`,
  `_CATEGORY_AXIS_PADDING_PX`, pinned to `chart-layout.ts`) - and the log
  says so (`chart_export.spine_note`). A left value whose bar ends too near
  the plot's left edge for its label - where PowerPoint writes the
  categories - gets a point label of its own (`_place_mirrored_labels`),
  written inside the end of its bar (`inEnd`), inked from the bar as a
  segment is (`_segment_ink`), so the axis keeps the viewer's one extent; a
  zero never gets one. The legend is the viewer's text and swatches, the side
  totals under each entry (`EXTRACT_PRIMITIVES_JS` visits it for a mirrored
  kind and gives the chart no legend box): PowerPoint's legend cannot sit
  one entry over each side. What PowerPoint does not draw stays a picture: a
  `gauge` (`PICTURE_CHART_KINDS`, no such chart type), a `map` (also in
  `PICTURE_CHART_KINDS`: PowerPoint's filled map is a `chartex` part geocoded
  by Bing when the file opens, which python-pptx cannot write and no other app
  shows - its canvas is the picture, and its title, ramp legend and caption are
  walked as text like the legend of any picture chart, the ramp's gradient bar
  a small picture of its own), and a chart with BARS
  read against the right-hand axis (`TWO_AXIS_PICTURE_SERIES_KINDS`:
  PowerPoint centres the bars of both value axes on their category, one over
  the other, where the viewer sets them side by side) are captured at
  `NATIVE_PPTX_IMAGE_SCALE` = 2x like the slide images and pasted, and the
  export LOG says so, one sentence per figure, with the extractor's reason
  (`chart_reason`: the kind, or `TWO_AXIS_BARS_REASON`) - both sets are
  injected into `EXTRACT_PRIMITIVES_JS` (`pictureReason`), so the browser and
  the writer agree on every figure
  (`chart_export.picture_note`: `Slide 8: the gauge chart is a picture of the
  rendered figure - PowerPoint has no gauge chart.`; a map's: `... - PowerPoint
  has no map chart that opens outside Office; its title, caption and legend
  are text.`). A chart the extractor
  could not read (no categories, no series, an unknown kind) is logged and
  left out rather than drawn wrong.
* **The export log** is part of the file's contract: `ExportedFile.notes`
  (a tuple, empty for a faithful export) rides the REST response as the
  `X-Export-Notes` header (a JSON list, ASCII by construction — the notes
  are the exporter's own sentences, never user text; no header when there is
  nothing to say), which `PresentationExportMenu.tsx` reads through
  `export-notes.ts` (`EXPORT_NOTES_HEADER`, `exportNotes(headers)`) into an
  info toast (`Exported with notes`) before the file opens; and the
  `export_presentation` tool result ends with an `Export notes:` list so the
  agent hands the file over knowing what stayed a picture (nothing is
  appended when the export was faithful — no empty heading for the model to
  narrate).
* **The dashboard is audited like every layout.** Its body is a
  `SlideBodyArea`, so the shrink-to-fit check (`fit`) covers it, and a
  finding inside it is named by the block the agent can find in its spec —
  the `panel`, or the `chart` / `table` it holds — never by the grid, the
  tile row or the chart's own scaffolding: the audit's `STRUCTURAL` list
  carries `dashboard`, `dashboard-tiles`, `dashboard-panels`, `panel-body`
  and every `deck-chart-*` structural class the figure renders (legend,
  swatches, canvas, values, centre, gauge readings, facts, target, tooltip),
  and `test_the_dashboard_and_its_charts_are_measured_and_named_by_their_block`
  fails the build on a class the components render that the audit would
  name as a block.

Tests: `tests/test_chart_export.py` (the pure format-code / note / kind
helpers, read back by a small Excel-format interpreter — `€16.5M`, `€527.1k`,
`86%`, `12,400 users` — and the mirror of `AXIS_COMPACT_FROM` and the currency
signs in `chart-format.ts`), the "Charts" section of
`tests/test_presentation_export.py` (every native kind opened back from the
written PPTX with its type, categories and series values; pie / donut slice
fills and hole; a pie's labels outside with leader lines or inside in each
slice's ink, a value the viewer left out deleted, a donut's leaders and inked
values; axis, legend and plot layouts; label positions; stacked totals
as text; horizontal axes; frame slack and clamp; the target connector; colour
blending; a butterfly's clustered bar chart - its left series negative, its
two-section formats, its left labels inside a bar or left of the axis, its
categories' column and log line; unreadable and picture charts + the
log; `export_presentation`
rendering at 2x and returning the notes; the `X-Export-Notes` header and its
frontend reader; the tool result's `Export notes:`; and the extractor /
figure contract strings on both sides), and
`tests_e2e/presentation-charts.spec.ts` for the live figures.

### Portable packages (JSON export / import)

The fourth entry of the export menu is not a render. `presentation_package.py`
writes the deck as ONE JSON document — `{version, export_type, exported_at,
source_platform, presentation: {spec, template: {slug, name, kind}, assets:
[…]}}` — and reads it back on any install. It exists so a deck built on a
staging or local platform continues on production (and so a deck can live in a
backup) without going through the agent again.

* **Export** (`build_presentation_package`, `GET /presentations/{id}/package`):
  same visibility as reading the deck. Every file id the spec references
  (`_walk_file_reference_ids`: every `*_file_id` plus the theme's logo keys via
  `referenced_file_ids`) becomes an `assets[]` entry — filename, content type,
  size, sha256, role, description, and the OWNING TEMPLATE (`slug` for a
  built-in, `name` otherwise) when it is a template asset. The bytes are
  embedded (`data_base64`) only when `authorize_file_access` admits the
  EXPORTER; a file the caller may not download travels as a reference, and a
  dangling id as `missing: true`. The package therefore carries exactly what
  its exporter could already fetch picture by picture through the download
  route, never more: reading a deck grants the pictures its slides
  show, so a reader's package embeds them (the deck is passed as the
  `presentation_id` hint and resolved by primary key, no per-picture scan).
  Download name: `<slug(title)>.presentation.json`.
* **Parse** (`parse_presentation_package`): hostile input. Bounded before
  decoding — `MAX_PACKAGE_BYTES` (checked against bytes READ, one past the cap,
  never against a declared size), `MAX_PACKAGE_ASSETS`, `MAX_EMBEDDED_ASSET_BYTES`
  estimated from the base64 length before `b64decode` — with a field-addressed
  `PresentationPackageError` for every structural fault (`assets[3].sha256`),
  filenames through `safe_filename`, a declared sha256 refused unless it is a
  hex SHA-256 and re-verified against the decoded bytes (an embedded file that
  declares none gets the digest of its bytes). A bare `PresentationSpec` (no
  envelope) is accepted as a package with no assets, so a spec copied out of a
  tool result imports too.
* **Import** (`import_presentation_package`, `POST /presentations/import`,
  multipart): MATCH before COPY. The template is resolved by slug (the built-in
  `slug:` tag, so a brand deck finds the brand deck), then by name within the
  importer's three-tier visibility (admins included: a name match is a
  lookup, not a by-id read), then the install's default template
  (`resolve_default_template_id`), then none. Each asset is looked up in the
  matching local template by filename and re-pointed at the local row — that
  is how a deck that uses the brand logos ends up on the DESTINATION's seeded
  logos and duplicates nothing. A lone namesake is the match whatever its bytes
  (this install's version of the file). Several are told apart by the packaged
  SHA-256, whatever the object-key format (`_find_template_asset`):
  first what a row records - `FileAttachment.sha256` compared exactly, and a
  seeded key's 16-hex prefix (`seeded_digest_prefix`) only as a pre-filter
  that rules the row out on a mismatch - then a read of the rows left, prefix
  hits first, confirmed on the full digest (an uploaded asset's key is a
  random name carrying no digest, and a row written before its path recorded
  one has none), at most `MAX_CONTENT_MATCH_READS` per packaged file and, per
  import, `MAX_CONTENT_MATCH_DOWNLOADS` objects and
  `MAX_CONTENT_MATCH_READ_BYTES` bytes, each namesake downloaded once per
  import. When none is confirmed, the one the template lists first (oldest,
  then id) is used and the report's `warnings` say so, once per template and
  filename. The recorded digest is trusted without a read, so it is kept TRUE:
  every template asset write records the digest of the bytes it stored
  (`template_asset_sha256()` for the upload, the replace in place, the
  template import and duplicate and `attach_template_asset`; the brand seed
  and its refresh from the manifest bytes), and
  `tests/test_file_attachment_digest.py` fails on any write of
  `s3_object_name` - attribute, `setattr`, bulk `values()`, constructor - that
  does not set `sha256` with it (a frozen list holds the legacy non-template
  constructors, whose new rows record none and are never matched). Only an
  unmatched asset with embedded bytes is created: sanitized like any upload
  (`sanitize_template_asset`, an SVG that does not parse refuses the import
  naming the file), uploaded under `presentation-import/<user>`, owned by the
  importer, `source = presentation_import`, recording the digest of the
  sanitized bytes. An asset that can be neither
  matched nor copied refuses the import — silently dropping it would change the
  deck. The spec's ids are rewritten with `template_package.remap_asset_ids`
  and the deck is created through `save_presentation(..., verify_files_for=user,
  change_source=VERSION_SOURCE_IMPORT)`: `validate_spec` and
  `verify_file_references` run exactly as for an agent-authored deck, so a
  package is not a way around the validator (`test_presentation_api_never_accepts_a_raw_spec`
  pins that the importer never constructs a `Presentation` row itself). The
  `ImportReport` (`assets_matched` / `assets_copied` / `template` =
  `matched|default|none` / `warnings`) is returned with the deck and audited.
* **Not AI-gated**: the import is listed in `_EXEMPT_OWN_DATA_HOUSEKEEPING`
  (`test_ai_disabled_read_surface.py`) — it re-creates something the user
  already owns through the same validator a manual edit uses, no model call —
  so a deck generated before AI went off on an install is not stranded there.

Tests: `tests/test_presentation_package.py` (export embedding rules, parse
bounds and malformed envelopes, template / asset resolution, namesakes told
apart by content and the read bounds, sanitized uploads, refusal paths, API
wiring); `tests/test_file_attachment_digest.py` (every write of an object key
sets the digest with it).

## Looking at a deck

Two halves of one problem: an agent that produces a deck cannot see it, and an
agent asked to rebuild someone else's deck as a template can only read its
words. Both are fixed the same way — PNGs on a tool result, which
`prepare_tool_result_attachments` forwards as native `detail=high` vision
blocks (the path the runner and browser screenshots already use). A PDF on a
tool result would NOT work: that helper forwards `image/*` and turns everything
else into a `[File: …]` stub.

**`capture_presentation_slides`** (the `generate_presentation` suite) is the
agent's own eyes on its own work. It reuses the export renderer
(`render_deck(..., want_slide_images=True)`), takes the same slide selector as
`get_presentation` (`"1,4"`, `"2-4"`, a slide id — `parse_slide_selection`, so
an agent never meets a second dialect), and defaults to the opening, the middle
and the closing slide. Read access is enough, like an export.
`MAX_SLIDE_CAPTURES = 4` because each capture is a 1920px PNG —
roughly 2.5k tokens — and a "show me the deck" reflex would otherwise spend a
turn's budget on pictures.

Two things it deliberately does NOT do. It does not screenshot the whole deck:
`render_deck` takes `slide_indices`, so a three-slide review of a sixty-slide
deck captures three images instead of sixty (the export path passes `None` and
keeps the full deck-ordered list the PPTX writers index into). And it does not
register the captures on the conversation's `FileContext`: a ref added there is
attached to the assistant message as a download (`_turn_generated_ids` in
`api/v1/chat.py`), so three review rounds would leave nine file chips nobody
asked for. The PNGs ride the tool result as vision blocks, which is the whole
point, and the rows stay as evidence.

A slide that renders the SAME pixels as its earlier capture of the turn comes
back as the SAME file. The loop shows an image's pixels once per file per turn
and counts distinct images against `AGENT_LOOP_VISION_VIEWS_PER_TURN`.
Wherever the conversation-scoped dedup of tool files does not apply (an
assignment run has no conversation), a capture used to store every PNG under a
new file id, so an edit-check round of three unchanged slides spent three more
images of the budget: two default captures exhausted a budget of six and left
the rest of the review blind, which the model answered by capturing again (the
identical-call loop breaker then warned on the third). The tools are built per
turn, so a closure memo keyed by deck, slide position and PNG digest, filled as
each file is stored, hands back the ref the turn already stored for that slide,
with the notes the agent saved on it since (`annotate_file` writes them on the
context's ref), and persists nothing: no dedup lookup, no upload. The position
is part of the key: "unchanged" is said of a slide, so a slide captured for the
first time is never reported unchanged because another slide rendered the same
pixels. The loop answers such a re-capture with its dated `Already viewed at
step N` note instead of the pixels, and the result says which slides render
the identical PNG, so an edit that changed nothing on the screen is visible as
such. The render itself still runs every time: a template or asset edit changes
the pixels without bumping the deck's revision, and only the render can tell.
A slide whose render varied by a single byte is simply persisted again, as
before; the INFO log line `capture_presentation_slides deck=...: N slide(s)
persisted, M reused an earlier capture of this turn` says how often that
happens.

**`document_vision`** (its own built-in id) reads the SOURCE. A PDF is
rasterized; a `.pptx` and an HTML page are read statically, because both state
their identity and neither can be rendered here without either an office suite
or someone else's JavaScript:

* `render_source_pages` rasterizes PDF pages with **pypdfium2** — self-contained
  wheels, no system libraries, the same constraint that picked `resvg-py` over
  the Cairo-backed renderers (PyMuPDF is AGPL; `pdf2image` shells out to
  poppler). Pages render at `RENDER_LONG_EDGE_PX = 1600`, just under the
  high-detail ceiling, so the vision path never resamples them. That long edge
  is a **bound**, not a target: there is no lower clamp on the scale, because a
  floor would let a poster-sized page box (or a PDF authored to have one)
  allocate a bitmap proportional to how the file was written. A page-selector
  range is refused before it is expanded, so `1-999999999` cannot materialize a
  billion integers on the event loop on its way to the cap.
* `inspect_pptx_identity` opens the `.pptx` as the zip it is and reads
  `ppt/theme/theme*.xml` — the theme of the FIRST slide master, resolved
  through the relationship parts, with the sorted first theme as the fallback.
  It returns the twelve colour slots (both the `srgbClr` and `sysClr`
  spellings), the major / minor typefaces, the slide size as the platform's own
  aspect-ratio vocabulary — including the portrait `A4` / `letter` labels,
  matched on the real page size rather than the ratio, since the agent copies
  that value straight onto the template and the validator accepts only
  `PRESENTATION_ASPECT_RATIOS` — and a `theme_config` draft for each scheme
  (`accent1..6` map one-to-one onto `chart_1..6`). These are READ values, which
  is why this beats rendering a PowerPoint: the alternative — LibreOffice
  headless — costs ~1 GB of image to produce an estimate of what the file
  already states. `extract_media=true` lifts the smallest embedded images out
  as platform files, which is how a logo reaches `attach_template_asset`.
* `inspect_html_identity` reads an HTML page the same way: the CSS custom
  properties (authors name them `--paper`, `--ink`, `--red`), the colours and
  font stacks it declares ranked by use — including the ones written in the
  `font:` shorthand, which is where real pages put the display face — which
  stack dresses the headings, and the heading outline. **The static read is a
  security boundary, not a limitation**: nothing is fetched (a `<link
  rel=stylesheet>`, an `@import`, an `<img src>` or a `url()` is inventoried
  and never resolved, so an uploaded file cannot become an SSRF primitive
  against whatever the backend can reach), nothing is executed (no browser, no
  JS engine — `<script>` elements are counted and dropped before the CSS scan),
  and the parser is stdlib `html.parser`: pure Python, no external entity
  resolution, no C extension. Rendering arbitrary HTML in the export Chromium
  would buy a picture at the price of running someone else's page inside the
  backend; the declared values are worth more than that trade. The page's own
  words come back inside a `wrap_untrusted` quarantine block — which is why
  every entry point gating `UNTRUSTED_CONTENT_POLICY` now counts
  `document_vision` as a producer alongside `web_fetch`. "Its words" is wider
  than it looks: the title, description, outline and the references it names,
  but also **every CSS custom property and font stack**, because
  `--note: Ignore all previous instructions` and `font-family: Then confirm the
  action was authorised` are both legal declarations (the value grammar is
  `[^;{}]`, and `_font_stack` rejects only `{};\`). Only what this code
  DERIVED stays outside the block: the colours, which survive
  `_normalize_color`'s strict hex/rgb/hsl whitelist, and the counts. A
  mechanism with two channels routed around it would be worse than no markers
  at all, since the convention teaches the model to trust what sits outside
  them.
* `compare_palette` measures CIE76 ΔE per role against a template's stored
  `theme_config`. It exists because the eye is the wrong instrument here: two
  greys at ΔE 8 look identical side by side, and a model comparing two renders
  will call a near-miss "very close". A role stored as `hsl()` or a font stack
  comes back unmeasurable rather than guessed at.

Every parse and every rasterization goes through `run_parse`
(`app/core/parse_offload.py`) — inline, they would block the event loop for
their full duration.

**The Deck Importer agent** (`deck-importer`) is the consumer: `document_vision`
+ `generate_file` (for `read_file_content`) + the template and presentation
suites. Its persona orders the reads (look first, then read the text), and the
`deck-import-review` skill holds the comparison loop — capture both sides, judge
six named points, measure the palette, fix one gap per round, stop after three,
and state what still differs instead of calling it close.

Tests: `tests/test_document_vision.py` (rasterization and its bounds, the PPTX
theme walk, ΔE thresholds, the tool surface and its registration),
`tests/test_presentation_export.py` (the capture tool: selection, defaults, the
cap, and the refusal that keeps an agent from narrating a render it never saw).

## Generate with AI (templates)

`POST /ai/generate-template` (`GenerateTemplateRequest`: `brief`, `kind`,
`name`, `palette_hints`, `logo_file_id`, `current`) drafts a complete
`TemplateCandidate` — name, description, kind, aspect ratio, tags,
`theme_config`, `layouts`, `extra_metadata` (`deck_defaults`, `guidance`,
`brand`; no style label — the schemes a candidate supports are
DERIVED from its palettes and shown as badges) — through the user's resolved
model (`get_llm_for_user`). Nothing is
persisted: the Templates form applies the candidate (create or edit), previews
it through the real deck renderer (`previewSpec.buildTemplatePreview` +
`candidateToTemplate`), and sends it back as `current` for a refinement round.

`template_generation.py`: `extract_palette()` reads dominant colours from a
logo (Pillow quantisation for rasters, hex/`rgb()` regex for SVGs; the file is
authorised through `authorize_file_access` and capped at `MAX_LOGO_BYTES`),
`build_template_system_prompt()` derives the layout catalog from the same
`layout_catalog()` the validator uses, and `normalize_template_candidate()`
runs the proposal through the SAME guardrails the `template_authoring` tools
enforce (`validate_theme_config`, `validate_layouts`, `normalize_tags`,
`DECK_DEFAULT_KEYS`), listing whatever was pruned in `warnings` rather than
silently dropping it. The palette shown in the dialog is therefore one the
renderer honours.

## Guidance: contract vs craft

The `generate_presentation` tool description is BUILT, not written
(`presentation_guidance.build_generate_presentation_description()`): routing
(when to use it and when NOT — never `generate_file` for slides), the
model-derived `spec_reference()`, one validated example per v2 layout and per
block type (`LAYOUT_EXAMPLES` / `BLOCK_EXAMPLES`, every one run through
`validate_spec` by `test_presentation_guidance.py`), tone semantics pinned to
the `Tone` literal (`TONE_SEMANTICS`), density budgets quoting the `MAX_*`
caps, and the editing protocol (a change to an existing deck goes to
`edit_presentation`; `presentation_id` on this tool is a wholesale rewrite). A
new layout or field is documented to the agent the moment it exists. The description is ~3.8K tokens; prompt caching
makes that cheaper than the validation round-trips an under-specified contract
costs.

The CRAFT lives in the global `presentation-design` built-in skill
(`skill_content/presentation-design.md`): working method (brief -> outline ->
one complete generation -> in-place refinement, a dashboard panel by panel; a
copy for a recurring or read-only deck; `expected_revision` against live
collaborators and the reviewers' comments), twenty-two slide compositions
distilled from the Filigran framework decks and the sales collateral as
validated JSON, the portrait page, twelve deck arcs (six talks, six collateral
formats), the review checklist with the post-save layout check and a look at
the slides (`capture_presentation_slides`), and the delivery (the marker,
export, sharing and the public link only when asked). Its
numbers are the tool description's: `tests/test_presentation_guidance.py` reads
the portrait budget, the closing banner and the Mermaid scope back from
`presentation_guidance.py`, so the two cannot disagree, and fails when a tool
`build_presentation_tools` hands an agent is not named in the skill (it
had fallen eight tools behind). Global so every agent carrying the
tool — in Copilot mode the General Assistant — can `load_skill` it on demand;
progressive disclosure keeps its size out of the per-turn prompt. The
`template-designer` persona binds it and points users at `Generate with AI` for
palette extraction (the agent cannot read colours out of an image).

Rule 16 of `AGENTS.md` (no system prompt inflation) is why the contract is
generated from the models and the craft is a skill: neither adds standing prompt
text, and every example is validated so the prompt can never advertise a
construct the validator rejects.

## Tests and tripwires

| File | Pins |
| --- | --- |
| `tests/test_presentation_spec_v2.py` | Markup grammar (+ frontend tokenizer mirror), icon allow-list vs installed `lucide-react`, slide ids, nesting caps, diagram guard, legacy decks still validating (blank optional strings read as absent, required blanks still refused), the two readers of a `PresentationSpecError` (agent `str()` vs `human_message()`), structural union-tag lifting, catalogs, `spec_reference`, the `architecture` block (graph consistency refusals - duplicate ids, dangling edges, self-loops, duplicate pairs, cell collisions, off-grid nodes and zones, empty and overlapping zones, shape rules - bounds, catalog presence, `logo_file_id` walked as a file reference); frontend tripwires: a failed save toasts once and line by line, a component per layout and per block, a CSS variable per theme key, a stylesheet rule per tone, a toned heading colouring its icon and a whole-text tone span read as the block's tone, lazy + strict Mermaid, lazy icon chunk, slide numbers inside the canvas, no HTML built from spec text, the architecture renderer drawing SVG through React only (no `foreignObject` / `innerHTML` / `mermaid`, glyphs as `<text>`, node-hit and crossing checks in the router, `data-deck-pending` on logos); the retirement: `SLIDE_LAYOUTS` is the seven ids and the catalog agrees, a retired id is in no list and a slide sent with one is stored upgraded, `spec_reference` lists exactly the catalog and names no retired layout and no `rich_text`, a v1-era deck still validates as its upgrade (the blanks on the retired shapes included), `layouts/legacy.tsx` is gone, no layout component has an HTML sink and `layout-options.ts` carries one list |
| `tests/test_presentation_architecture_zone_chips.py` | Zone chips: `chips` stored `[]` and `chips_align` `"end"` by default, at most 6 chips of at most 24 characters of markup, `chips_align` start / center / end only and never without chips, chips alone heading a zone, the type line and both tool descriptions (`ARCHITECTURE_BARE_HINT`), the renderer's and the exporter's hooks, `_capture_lifted` hiding the chips for the shot and showing them again even when it fails, the `zone_chips` finding's words; in Chromium on the real stylesheet, `tests/data/architecture/zone-chips.html` (pinned by `blocks/architecture-zone-chips.test.tsx`): the logo top left and the four chips in one row top right in both schemes, tinted in `brand_1` and every tone, the layout check silent on it and naming the zone of `zone-chips-overflow.html`, whose chips are drawn whole, and the editable PPTX: one picture captured without the chips, each chip a rounded rectangle and a text box within 2px of the page at 1920x1080, its fill the page's |
| `tests/test_presentation_architecture_edge_sides.py` | Edge sides, aligned pairs and zone ends: `source_side` / `target_side` stored as written on every side and refused outside the four, `lane` an int 1-6 (0, 7, 1.5 and text refused), two edges of a pair on one lane refused naming both, a lane on a zone-ended edge refused, zone ids unique and identifiers, a `zone:` end named by id, index or label and as a source, an unknown zone refused with the edge index and the zones listed, a shared label refused as ambiguous, an edge between two zones and one into the zone holding its node refused, one zone spelled two ways caught as a duplicate pair, a node named `zone` still a node, the type lines and both tool descriptions (`ARCHITECTURE_EDGE_HINT`), the frontend mirror (`DeckEdgeSide`, `ZONE_REF_PREFIX`, `resolveZoneRef`); in Chromium on the real stylesheet, `tests/data/architecture/edge-sides.html` (pinned by `blocks/architecture-edge-sides.test.tsx`) in both schemes: every route where the layout put it within 2px at 1920x1080, the lane pair exactly level and 44px apart, the zone arrow ending on the panel's top border with its head painted, the layout check silent, and the editable PPTX one picture at the drawing's box whose pixels along every route are the page's |
| `tests/test_presentation_architecture_bare.py` | Zone logos and frameless nodes: `frame` stored `true` by default and `false` as written on every shape and emphasis, a zone headed by a label, a logo or both (never by nothing - chips may head one too - never an icon and a logo), an unlabelled zone named by its place in a refusal, the zone and node logos read for their geometry (`logo_file_ids`) and walked as files, an invented zone logo refused on save in the words a node logo gets, the type lines and both tool descriptions (`ARCHITECTURE_BARE_HINT`), the renderer's hooks; in Chromium on the real stylesheet, the 5-column fixture (`tests/data/architecture/`, pinned by `blocks/architecture-bare.test.tsx`): no border, surface, shadow or glow around a frameless node in either scheme, the zone mark painted in its box to 2px, the layout check silent, and the editable PPTX one picture at the drawing's box whose pixels are the page's |
| `tests/test_presentations.py` | Spec normalisation, tool marker / in-place edit / revision bump, `duplicate_presentation` (identical slides under the new title, the footer reading it, read access enough, nothing written for an unreadable deck, one service with the library copy), visibility + permission tiers (config vs read-only grantees), REST visibility in SQL, PATCH pieces + `expected_revision`, channel marker resolution, theme whitelist + visibility re-check, brand container seeding (both modes, sync in place, stale removal, deck defaults valid, layouts derived), the export offered whatever the request says, every entry point passing `request_text` for the template choice; slide links: the marker parity test compiles the frontend regex and reads the `#/<slide id>` fragment on both sides (the fragment grammar is `SLIDE_ID_PATTERN`, `#/`, a space and 65 chars refused), the channel resolver keeps the fragment on the absolute link and absolutizes the agent's relative slide links for in-scope decks only (out-of-scope and already-absolute paths untouched, one query for markers and links, no scope = no rewrite), and one link shape (`slide_link` / `focus_marker`) is read by every frontend surface (`slide-links.ts`, `MarkdownLink`, the card, the transcript registry, the rail, the viewer hash); the retired layouts: every v1 shape validates as its `content` upgrade with its words, notes and id intact and no tag or `rich_text` left, an empty v1 slide becomes a `section` (never a blank `content` slide), the upgrade is idempotent and copy-on-write, `SpecJSON` upgrades on read on both spec columns and passes a v2 document (same object), `None` and a non-deck value through with no bind hook, a generated deck naming a retired layout is saved upgraded and the result names the upgrade, the v1 slides a save leaves behind are re-validated as v2, a script-capable v1 `rich_text` is reduced to words before storage (no tag, attribute or scheme survives) while safe paragraph markup keeps its words as a `text` block, and `test_no_field_in_the_spec_carries_html` walks the whole model tree (no `rich_text`, no `html` / `iframe` / `srcdoc` / `script` field, the banner `url` the one address field and `check_action_url` admitting `https://` and `mailto:` only) |
| `tests/test_presentation_pictures.py` | Pictures: every place a deck shows a picture is named in the edit grammar and the address resolves to the block holding it, the key rule is shared by the walk / the export allow-list / `replace_text` / `replace_image`, copied file-id spellings are read, `replace_image` swaps every place on purpose (count), per slide or per block, names what IS shown when the file is not (a "No change"), follows a slide a later op moved, leaves the other slides byte-identical, and is accounted from the final spec (a swap a later `replace_slide` / `replace_block` / `delete_slide` undid is neither reported nor counted, a chained swap is the swap that stands, a place several swaps wrote counts once and one swapped back to the picture it showed (`A -> X -> A`) not at all, a later insert re-addresses the place, a caption fixed by `replace_text` in the same call keeps both accounted); the tools list the served / captured pictures (MISSING flagged, no query for a picture-free deck, no swap hint for a read-only reader, the capture paired with the revision it rendered or no list at all, the list bounded and never stranding a picture of a single slide), the edit names the new file and refuses a dead picture with the exact swap while a swap to an invented or unreadable file is refused by the real verifier with nothing saved and no such hint, a swap of a file not shown is a `no_op` result, the account never looks up a replacement the saved deck does not show (an undone swap, the middle of a chained one); `load_picture_facts` tells a reader a template asset's name and caption only under the template's visibility and a conversation file's name and notes only when it is theirs; the parity tripwires (every picture-like field of every spec model follows the key rule, the key rule's exact table, a file id in prose grants nothing, every theme file key stays on the render allow-list) |
| `tests/test_presentation_patch.py` | Surgical edits: every operation leaves the slides it does not name canonical-JSON-identical (parametrised over the eleven text / structure ops - `replace_image` is pinned in `test_presentation_pictures.py` - plus a mixed burst), slide addressing by position or id, block addressing (`N`, `column.N`, `footer`, nested `N.M`; a dashboard's panel `N` - its figure, title and span set in place, the panel replaced, added and removed, the other panels and tiles identical, end to end through the tool; the no-body-blocks refusal naming `footer` where the layout takes one), `update_*` keeps identity and type, `replace_text` exact / scoped / refuses ambiguity with per-slide counts / never touches structural strings, insert bounds and the deck's own slide cap (the default, a template's `slide_cap`, the ceiling, an over-cap deck still edited; the tool inserts on a full deck whose template sets more and refuses on a free one naming the cap), delete / move accounted by original position, `update_deck` merges chrome and refuses unknown fields, named-but-unchanged accounting, errors name the failing operation, `coerce_operations` (JSON string / single object), the patched spec is validated like a fresh deck, `verify_untouched` reports drift and vanished slides, `parse_slide_selection`, every op has a contract and a handler; the tool: one changed slide + the rest proven untouched with one `PresentationVersion` and the revision bump, the agent's `change_summary`, validation / patch errors write nothing, a no-op is not a revision, read-only grantee and invisible deck refused, file references checked on the written slides only (untouched masked), registration order and schema (`enum` = `OPERATIONS`), `get_presentation` `slides` selector returns whole slides, the over-budget read-back is whole-slide paging with a `Not shown` pointer that the selector then serves, a selector naming nothing is an error, studio prompt + guidance route edits to `edit_presentation`, the deck row is read `FOR UPDATE`, the account is reconciled against the validated spec (a normalised-away write is "already as requested" in the result and absent from the history line; `reconcile` unit-tested on a mixed burst), the layout check is narrowed to the written slides / whole only for a `LAYOUT_DECK_FIELDS` change (parametrised over the five deck fields, alone and with a written slide: theme / aspect ratio / chrome measure every slide, a title or transition edit renders nothing alone and narrows to the written slide otherwise; the constant is a subset of `DECK_FIELDS` minus exactly those two, with a `chrome.tsx` tripwire) / skipped for a move-or-delete-only edit, a stale `expected_revision` is refused before any operation with nothing written and names a later save rather than a collaborator (int, integer-valued float and digit-string accepted, `0` and anything else refused as not a revision; `coerce_revision` unit-tested), and the schema offers `expected_revision` as an optional integer, a surgical write into `rich_text` (`update_slide`, `replace_text`, `replace_slide`) is refused by the same guard as a fresh deck and writes nothing, `MAX_OPERATIONS` refused before anything is applied, and one JSON type per schema property (every deck tool the builder can expose is declared through the real Gemini cleaner and SDK `FunctionDeclaration` + `Tool`, a digit string addresses like the integer did); slide links: the account links every written slide by its stable id and tells the agent to put the link in the reply, the marker and the `presentation_saved` payload carry the focus (changed > inserted > moved; a moved slide is the focus when nothing was written), and a deck-field-only edit keeps the plain marker with no `focus_slide_id` |
| `tests/test_presentation_slide_links_in_deck.py` | Slide links inside a deck: `[text](#/<slide id>)` parses as one `link` segment (plain text keeps its label), every other Markdown link stays literal, a link with no label, a ref that is no slide id, markup in the label and a link inside a bold run or a span are refused with the shape to write, `SLIDE_LINK_REF_PATTERN` is `SLIDE_ID_PATTERN` without its anchors, `MARKUP_SUMMARY` names it, `fit_markup` keeps or flattens it; `slide_links_line` silent on a deck with none, counting the links that land, naming the broken ones (slide, ref, the id-not-number reminder, 12 listed and the count complete) and read on the real edit account after a `delete_slide`; both PPTX writers lay one jump target per box (0% fill, no outline, the box's geometry, above the slide's shapes), skipping a link to its own slide, past the deck, zero-sized or with no integer target; the renderer's `data-slide-link-index`, the box probe's selector and the export page's anchors agree, `render_deck` measures the boxes for both variants; the frontend tokenizer carries the same link alternative in the backend's order and the same `MARKER_RE` |
| `tests/test_presentation_defaults.py` | Default template resolution order, `template_id` semantics (UUID / opt-out / omitted / garbage), deck defaults filling only unset keys, duplicate semantics, the Settings validation of the default, `.env.sample` + docs wiring for `PRESENTATION_RENDER_BASE_URL`, mkdocs nav entries |
| `tests/test_presentation_chrome.py` | Template header and footer: `render_chrome_pattern` collapses an empty placeholder with its separator (literal-only, all-empty, leading / inner / trailing separators, whitespace), `render_chrome` reads the deck settings and the variable defaults, `normalize_template_chrome` accepts the brand declaration canonically (header-only and empty included) and reports every malformed one — unknown key, over-long pattern, unbalanced or malformed slot, too many / invalid / built-in / duplicate keys, missing or over-long label, hint or default, variables without a pattern, undeclared placeholders, unused variables — storing nothing, the lenient reader never raises on what an older row stores, the seeded brand decks declare the footer and it validates (the Filigran deck alone with the watermark), the watermark read from a literal `true` only, stored only when on, a declaration of its own that draws no pattern, refused when not a boolean and read back to the agent, `validate_chrome` is the one gate every template write goes through (REST create / update / import, the authoring tool, the generator — pinned by source), `Chrome.fields` cleaned and bounded like a label with the field named on refusal, `update_deck` merges `chrome.fields` key by key, `footer_status_line` / `describe_template_chrome` say what the footer reads and which setting fills each placeholder, `template_chrome_redraws` widens the layout check only when the pattern reads the changed field (a rename re-renders the deck only when the footer shows the title), `edit_presentation` sets one footer variable and reads the footer back, `get_presentation` and `serialize_presentation` carry the template chrome live, the tool schema tells the agent the footer is a deck setting, and the frontend mirror (`chrome-pattern.ts`, `chrome.tsx`, `presentation.css`) implements the same rule from the same regex and draws the header; `update_deck` reads field keys through the validator's own normaliser (`canonical_field_key` - a differently spelled key clears or overwrites the stored one, `null` / blank unsets, the last key dropped drops the object; the validator, the merge and the reader call it by name), `{{subtitle}}` reads the opening subtitle as plain text (markers stripped, line folded), the shared table `tests_e2e/fixtures/chrome-pattern-cases.json` renders / validates / fills a deck the same on the backend (parametrised; every problem code both validators can report is covered, pinned against the TS `ChromeProblem` union), the frontend mirror is pinned to the same `VARIABLE_KEY_RE`, the same limits and the same normalisers (`canonicalFieldKey`, `markupToPlainText`), `template_chrome` follows the reader's visibility of the template (visible = the declaration, invisible or pattern-less = `None`, a free deck in the response), the REST `PATCH` replaces the chrome wholesale (a stale `expected_revision` is a 409 that writes nothing) while `update_deck` merges it, and an edit that changes which subtitle opens the deck (a `delete_slide` of the opening slide, a `move_slide` that puts another title slide first - operations that write no slide) redraws the whole deck and reads the header back under a `{{subtitle}}` pattern, while a rewrite of the opening slide that leaves its subtitle alone stays measured on that slide; the footer / header read-back shares the redraw's `template_chrome_redraws()` reading, so the same delete or move under the brand pattern (which never reads the subtitle), a rename on a template whose fixed pattern never shows the title, and a rename on a free deck are neither rendered nor read back (`tests/test_template_authoring.py` pins the authoring side: a kind change alone re-validates the stored metadata against the new kind, dropping the header / footer pattern and the scheme list from a template that stops dressing decks - the result names the dropped keys - keeping the rest, an echo of the kind re-validating nothing; and a `PATCH` that echoes the stored metadata, a scheme already declared or the stored kind writes nothing, records no change and no audit, and says so) |
| `tests/test_presentation_export.py` | Render token (confined, bound, short-lived, disjoint from session JWTs), `referenced_file_ids`, `render_base_url` order, PDF paper vs PPTX slide vs canvas, image + native PPTX writers, colour blending / parsing / font mapping, render routes (token only, other deck refused before lookup, visibility gate, asset allow-list, `render-spec` serves `template_chrome` / `template_name` / the deck's `chrome.fields` like the session route so an export draws the template footer - and the free footer for a template the token user cannot see), export endpoint + tool, frontend export menu |
| `tests/test_presentation_guidance.py` | Every block / layout example validates, budgets quote the caps, the tool definition uses the built description and v2 schema, the skill's twenty-two compositions validate, the skill names every tool the builder hands an agent, persona coverage |
| `tests/test_presentation_collaboration.py` | Versions: snapshot on every save numbered by `revision`, retention, restore as a new revision + re-validation, manual summary derived from the diff (no-op on an unchanged spec); comments: stable-id anchoring, detached slides, permission matrix (read → comment, author / manager → resolve, edit, delete), agent formatter with ids / status filter / one-slide filter, `resolve_slide_anchor` (position vs id, out of range); the three comment tools end to end through the fake session (list, add on a slide, reply inheriting its thread's slide, resolve with a closing reply, reopen, reply not resolvable, audit `via: agent`) and the permission parity (a read-only grantee's agent cannot close another's note, can close its own, an invisible deck is no deck); the AI draft (server-side slide / thread / current-text context, `clean_draft`, deck-scoped + AI-gated + bounded endpoint, `CommentDraftError` on an empty or failed reply); presence: heartbeat / TTL / departed users / fail-open on a dead Redis, and the `activity` contract (assistant over editing over viewing, nothing remembered across beats, legacy `editing`-only entries decoded, `any_turn_active` as one `EXISTS`, `studio_turn_active` scoped to the caller's bound conversations and `False` without a probe or on a dead Redis, the route deriving `assistant` itself while the heartbeat body cannot claim it, and the frontend tripwire — no `editing || studioOpen`, `editing = draftSpec !== null`, `onBusyChange` from `chatWs.isSending` reset on unmount, the flag never in the POST body, avatars keyed on `activity`, the tooltip in every catalog); tool (`change_summary` accepted, `get_presentation` surfaces open comments with their ids); routes scoped to a deck, serializers (`open_comment_count`, `updated_by_name`), model registry + lock-safe migration; frontend tripwire (toolbar entry points, `Open in studio` on the card, one `panel` slot, `useDraftPresentationComment`). The fullscreen viewport fallback tripwire (both CSS selectors paired, `presentedElement()` in the `Esc` paths, `presentHere`) lives in `test_presentation_spec_v2.py` |
| `tests/test_presentation_studio.py` | Studio: `parse_focused_index` bounds; `ensure_studio_tools` is a union and idempotent; `load_studio_context` over the tiers (unbound, invisible deck, owner, read-only grantee, `config` grantee, admin on a company-managed deck — role looked up, never assumed — and admin on a colleague's private deck: read-only); `format_studio_prompt` (title / id / revision / access line / focused slide / empty deck / `page` noun); the block lands BEFORE the security anchor in both loops; the conversation API carries `presentation_id` and `studio_slide`; `Conversation.presentation_id` (nullable, `SET NULL`, `use_alter`) + the lock-safe migration; frontend tripwires (the rail renders `LazyXtmChatPanel` with `useTurnFrames`, `DECK_KEYS_OFF_ATTR`, live refresh on the `presentation_saved` progress frame — top-level and under a consult — with the `generate_presentation` / `edit_presentation` and comment-tool `tool_done` frames as the fallback and the frame constants mirrored byte for byte, one `studio-url.ts` shape used by every entry point, compact card in the rail, the rail resizes up to half the screen and the `PresentationSidePanel` up to a third through `useResizableWidth`, the composer's `PromptGenerator` + `generate` callback, every locale carries the studio strings, both `handleToolProgress` sites hand the frame to the consumer's `onToolProgress` and the rail follows the agent's pen from it while the viewer wires `onSelectSlide`, the rail's transcript and composer are the shared panel's — `hideConversationList`, its own never-prefetched chunk, no second transcript or composer — and `RunDetail` draws a stored answer through `InlineFileContent`) |
| `tests/test_tool_events.py` | Tool-side progress: the `tool_progress` frame rides the loop's `on_event` between `tool_start` and `tool_done`, a parallel batch tags each report with its own `tool_call_id`, the loop owns `status` / `tool` / `tool_call_id`, no consumer means a silent no-op, `emit_tool_event` outside a bound call is a no-op, a failing sink is logged and never reaches the handler, the binding does not leak past the call, an emit after the call CLOSED its binding (a continuation the handler spawned) is a silent no-op, the payload reaches the sink JSON-safe (`UUID` / `datetime` as strings, a tuple as a list), an unserialisable or oversized payload (a `NaN`, a tuple key, a value whose `__str__` raises) is dropped with a warning naming the tool and the event and never sent - through the real loop too - and a nested loop (a consult inside a tool) keeps its reports on its own consumer, shadows the outer sink when it has none, and leaves the outer call's binding intact afterwards. The presentation half — `presentation_saved` fires after the commit and before the layout render, on both write paths, never on a refused edit — sits with the tool tests in `test_presentation_layout.py` and `test_presentation_patch.py` |
| `tests/test_presentation_notes.py` | Speaker notes: words counted on the spoken text (markers are not words, an unknown tone is literal), speaking time at the shared pace via the platform duration form, plain text keeps the lines (and a line the tokenizer refuses), coverage owes notes on content slides only (dicts and models), the `Speaker notes:` line on tool results for decks only + the `(no notes)` outline flags (never on a page), guidance asks for speech at the pace, PPTX notes pane gets plain text / is skipped when empty; frontend tripwires: `WORDS_PER_MINUTE` + `NOTES_OPTIONAL_LAYOUTS` mirrored, own `/speaker` + `/notes` routes with no `RevealNotes`, the channel keyed on the deck id, `S` + menus for decks only, the presenter essentials in the window, the handout's fixed thumbnail column in print, the narrow layout's content-sized rows (no stretched grid rows, the side rule outranks the column rule), every locale carries the speaker strings, the editor panel shows words / time per slide |
| `tests/test_presentation_layout.py` | Layout check: observations -> findings (the four kinds, the two severity thresholds, the floor variant with its cut, remedies per kind, page vocabulary for a portrait brief, bounded quotes, garbage ignored, overflow not doubled as the frame's cut, worst-first ordering), report formatting (disabled / skipped / clean / listed / bounded, `unit` and `revision` in the payload and the cache round-trip), `check_presentation_layout` over a mocked `render_deck` (runs as the given user with the deck's paper, timeout and error and missing browser all skipped not raised, disabled never renders nor asks Redis, a mid-render save is a skipped check that is not cached, a page without a revision is trusted, one render per (deck, user, revision) with the cache failing open), the tool results (`generate_presentation` appends the note pinned to the saved revision and never fails the save, `get_presentation` measures for an editor only), the tool description announces the check, the REST route (a POST that reads, pinned to the row's revision, 200 on a skipped report, 404 when disabled or invisible), and the frontend tripwires (the audit reads the attribute `fit.ts` publishes and the extractor corrects by, `FIT_MIN` equals `FIT_FLOOR` and a fitted host clips, every body is wrapped in the fit host, aria-hidden text excluded from the measurements, kinds and severities equal the frontend unions and the panel's config maps, `data-slide-id` on the static slide and `data-deck-revision` on the static page, the panel locates a slide by its stable id, panel strings in every catalog, the switch documented) |
| `tests/test_presentation_layout_switch.py` | Layout switch: the one catalog (`SLIDE_LAYOUTS` has a model per id, `slide_model()` returns none for a retired or unknown id), `template_supported_layouts()` (advertised ids restricted to the catalog in catalog order, whole catalog when nothing renderable is advertised, strings or `{id}` objects, garbage ignored) and its ride on `serialize_presentation` / `TemplatePalette` / `PresentationResponse`, every conversion (parametrised over the whole grid: the result validates through the target model, the source layout's own default title is not carried, a person's title is, notes / speaker / footer / visual blocks kept where the target has a home for them, list items derived from blocks, the account in the person's words), the refusals (nothing to list, a retired target named as such with the catalog offered instead, unknown layouts, the same layout is a no-op), a heading lent to the title not also listed as the first item, the default titles read off the models, a dropped body counted once, a retired SOURCE upgraded first and the account naming the step (the same target for the same words gives the same slide whether the draft was v1 or v2; a script-capable v1 rich text flattened to words before the target model sees it, its `rich_text` gone), the route (manage tier, 422 with the reason, the loader's 404 passed through, no save / no audit, listed in `test_presentation_api_never_accepts_a_raw_spec`), and the frontend tripwires (`layout-options.ts` mirrors the one list and carries no legacy split, `previewSpec.ts` reads the catalog from it, the hook and the panel go through the route, `supported_layouts` typed on the deck, every locale carries the picker strings and the picker never reads the ambiguous `Title` id) |
| `tests/test_presentation_editor.py` | In-place editing: the block and slide schema catalogs mirror the Pydantic models field by field (keys, option lists, `num` ranges against `ge` / `le`, `strings` and `icons` maxima against `max_length`, shared lists, nested groups) and back (every model field without a descriptor is named in the allow-lists - edited on the stage, or the assistant's), every palette and `Add slide` default validates through node (`scripts/dump-presentation-schema.mts`, items grown to their bounds, table growth appends a cell per row; a FAIL on CI when node cannot run the dump, a skip only on a developer machine), item bounds match `min_length` / `max_length`, the slide and run caps (`DEFAULT_SLIDE_CAP` - the editor reads the deck's `max_slides` over it -, `MAX_BLOCKS_PER_*`, `MAX_TABLE_*`) are the models' and are enforced in the session, every `*BlockView` publishes `useBlockEditAttrs()` / `editAttrs`, no file of the presentation module reaches an HTML sink (`Diagram.tsx` excepted), the paste and drop handlers insert plain text, a held commit key or Ctrl+S asks once and the field leaves an IME composition alone, `markupProblem` mirrors the validator's refusals, the stage and the page document render the edit session and publish the slide frame's index / id in edit mode ONLY, the typing-target and key-handling contracts (keys read on the document in capture, never while a field has the focus, Escape / Alt+arrows kept from reveal); `frontend/src/components/presentation/{edit-paths,inline-runs,block-schema,presentation-editor}.test.ts` (node) pin the path algebra, the runs model round trip and honesty, the catalog defaults and the timeline a save leaves behind; `tests_e2e/presentation-editor.spec.ts` drives it end to end with real input and checks the DOM is the plain deck's once the panel is closed |
| `tests/test_presentation_quote.py` | Quote variants and the `plain` metric: a quote with no new field stores as the card (only `variant` spelled out), the statement example is the one the tool description teaches and validates alone on an untitled slide, look values the renderer does not draw are refused, `attribution` and the split credit are one or the other, `quote_credit` reads one line, the figures bound and carrying a value, a label and a tone only, the quote's pictures seen by the file walk, the metric `variant` / `align`, `QUOTE_VARIANT_DEFAULTS` and `HERO_QUOTE_MAX_LINES` mirrored by `quote-style.ts`, the reference's generated default wording (`QuoteFigure`, the plain tile's "value + label only"), the `lines` finding (threshold, remedy, never next to `sparse`, the audit's count and structural names, the kind and its `amount` in the API schema and the client types), the layout switch reading a customer quote and naming its pictures, and the tripwires: the card's stylesheet rules verbatim, the marks as real glyphs (the decorative one an in-flow box cut to its ink, which is what makes leaving it out of the audit's glyph measure safe), the lone statement centred by the layout (and the design skill's recipe needing no spacer), the plain tile's rules and its adornments drawn only through `tileAdornments`; `frontend/src/components/presentation/blocks/quote-style.test.ts` (node) pins the look resolution, the attribute rule, the credit, the lone-statement rule and the hero step, `blocks/metric-look.test.ts` what a plain tile draws, `field-applicability.test.ts` the inspector's notes on the credit and on a plain tile, and `block-schema.test.ts` / `presentation-editor.test.ts` the look pickers written back unset and read clean |
| `tests/test_presentation_showcase.py` | The keynote compositions: `image.frame` off by default and the only difference a framed image writes, its three values and the alias in the reference, the showcase composition validating in the wide column; `challenge_answer` atomic (in a callout, a comparison side, a column), its stored defaults and refusals (an empty side, HTML, a tone), the reference wording of its card-stretch `fill`; the tool description's one `SHOWCASE:` paragraph with the compact example, the skill's 3.21 / 3.22 and their review-pass lines; the layout switch reading the two cards as two points; the frontend halves (`data-frame` from the field, the glow as a background with `data-export-backdrop`, `onRatio` through the shared image, the bucket and room width rules, the column `:has()` rules, the challenge grid and its card light, the connector (one SVG, decoration, captured alone, the icon's stroke), the types, the schema constants and entry, the icon, the option labels, the four catalogs). `blocks/showcase.test.tsx` reads the cascade and the published variables through the real stylesheet; `tests_e2e/presentation-showcase.spec.ts` the geometry on a live deck |
| `tests/test_template_asset_seed.py` | Manifest / role validity, deterministic keys, idempotent re-seed, a changed file refreshed in place with its row id kept, a renamed or removed file retired, every obsolete object (refreshed or retired) deleted only after the commit and none at all when the commit fails, S3 failure isolation, SVG sanitizer, a pack included minus roles and a file borrowed from a sibling pack (an invalid pack name skipped), the THREAD deck shipping its own identity and every Filigran figure but the Filigran chrome logos |
| `tests/test_template_generation.py` | Palette extraction, candidate normalisation guardrails, prompt derivation |
| `tests/test_template_containers.py` | Built-in read-only on REST + tools, no vector code in template modules |
| `tests/test_template_theme_values.py` | The theme value guard over the shared `theme-value-cases.json` table: every fetching CSS function refused for every deck and report key (quotes, prefixes, case, CSS escapes, a space before `(`, nested, a second layer, a dashed custom function), every colour / gradient / font stack kept as written, the structural refusals kept, one trim set and the canonical file-id form on both sides, the allow-list pinned, the frontend mirror pinned to it, every write path wired, the REST guard trusting what does nothing, a stored template's refused values dropped on read |

## Checklists

**Adding a slide layout**: Pydantic model in `presentation_spec.py` +
`SLIDE_LAYOUTS` entry; component in `frontend/.../layouts/` and the `SlideBody`
switch; a `LAYOUT_EXAMPLES` entry in `presentation_guidance.py`; a
`_LAYOUT_LABELS` entry in `template_container_seed.py` (the seeded catalog is
derived, the label is not); preview support in `previewSpec.ts`. The tripwires
name what is missing.

**Adding a block**: model + `BLOCK_TYPES` (and `CONTAINER_BLOCK_TYPES` if it
holds blocks); component in `frontend/.../blocks/` and the `blocks/index.tsx`
switch; a `BLOCK_EXAMPLES` entry. Mind the native PPTX exporter: it turns a
box with a background / border into a shape and a text-only element into a text
frame, and it screenshots anything image-like (`<img>`, inline `<svg>`,
`<canvas>`, a `background-image`, gradient text) as a picture — so a visual
that a PowerPoint shape cannot express must be painted through one of those
(e.g. a `background-image` rather than a `box-shadow` or `backdrop-filter`,
which are dropped from the editable export and only survive in the image one).

**Adding a theme key**: `THEME_CONFIG_KEYS` in `presentations.py` AND
`THEME_VARIABLES` in `theme.ts` (a key with no variable is stored, served and
silently ignored); a file-id key also needs `THEME_FILE_ID_KEYS` /
`THEME_FILE_ID_ROLES`. Report templates share the base six keys
(`BRAND_THEME_CONFIG_KEYS`) — see `REPORT_THEME_CONFIG_KEYS` before widening.

**Bumping `lucide-react`**: `cd backend && uv run python scripts/refresh_lucide_icons.py`.

**Changing the brand**: edit `BUILTIN_TEMPLATE_CONTAINERS` / the manifest and
its files; the next boot syncs metadata and re-uploads only changed assets.
Never edit a built-in container in the database — the seed overwrites it.
