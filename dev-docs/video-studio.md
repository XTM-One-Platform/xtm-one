# Video Studio

Agents record product demos in the backend's own browser and produce narrated,
branded videos from them, plus fully synthetic clips from the AI providers'
video models. Three layers, each its own entity and job, so a recording is
footage reused by any number of videos and a video is an edit rendered as often
as needed:

| Layer | Entity | Job | Module |
|---|---|---|---|
| Capture | `VideoTake` (a recording) | `video_take` | `services/video_studio/capture.py`; its holds' page watch `page_settle.py`; the page state it opened and missed a step in `take_state.py` |
| Production | `VideoProject` (the edit) | - | `spec.py`, `templates.py`, `projects.py`, `timeline.py` |
| Render | `VideoRender` (one MP4) | `video_render`, nightly render retention sweep | `render.py`, `compositor*.py`, `encoder.py`, `render_retention.py`, `render_cleanup.py`; its wait in the queue `render_queue.py`; its checks `stitch.py`, `voice_check.py`, `audio_qc.py`, `frame_freeze.py`, `render_checks.py`, `insert_checks.py`, `music_level.py`; the screen inserts `screen_insert.py`, `surface_refine.py` (the bezel lock), `insert_tracking.py`, `insert_builds.py` (the preview's); the pronunciation lexicon `lexicon.py`, `lexicon_store.py`, `phonetics.py`, `pronunciation_check.py` |
| Synthesis | `VideoClip` (an AI shot) | `video_clip` | `clips.py` |
| Music | `VideoMusic` (an AI track); built-in scores | `video_music` | `music.py`, `score.py` |
| Editing | - | live preview, retention sweep | `preview.py`, `retention.py`, `studio.py` |
| Sharing | `VideoPublicLink` | - | `presentation_sharing.py` (the grants), `public_links.py`, `media.py` |

For the person a video is a video: recordings and AI shots are its media (the
library's `Media` tab), scenes of the timeline like cards, slides and images.
`VideoClip` stays its own row because generating one is a job and its file has
a retention, exactly like a recording.

Everything runs in the backend (worker): headless Chromium (Playwright) and
ffmpeg (the Docker image ships it). XTM One Desktop is not involved; a demo
environment on a private network is reached under the browser profiles'
security option (`browser_private_targets`, `dev-docs/browser-profiles.md`).

The whole Studio ships behind the `VIDEO_STUDIO` feature flag, off by default:
what it gates, what it keeps and what turning it off removes are in
`video-studio-feature-flag.md`.

## Capture: a recording

`record_take` drives a `TakeScript` (`spec.py`): a start URL, up to 200 steps
(`goto`, `click`, `double_click`, `right_click`, `type`, `press`, `hover`,
`drag`, `wheel`, `scroll`, `select`, `wait`, `wait_for`, `highlight`, `pause`,
`marker`), a viewport (1600x900) and
a pixel ratio (1, 1.5, 2 default: a recording is sharp on a screen
insert of a 4K render - 1.5 is the balance for a 1080p video, 1 the smoothest
capture; a recording too small for its quad fails the render as a `soft
insert`, below, naming the ratio it needs - and, is refused at
save at the quality the video is made for, below), and a browser zoom
(`browser_zoom`, 100-500%, unset 100).

- **A browser zoom on the same frame.** `spec.zoomed_window` lays the page
  out as a browser zoomed to it does: a CSS viewport that many times
  smaller at that many times the pixel ratio, so a 1600x900 window at 2x
  zoomed to 280% is a 571x321 page at 5.6x, painted onto the same 3200 px
  frame - its text larger for the frame's width, its frame as large and as
  sharp. The take records the viewport and the ratio the page was laid out
  at, which every reader of its events and cursor (CSS pixels) maps onto
  its frames; the profile browser bounds the frame it paints
  (`browser.MAX_FRAME_SIDE`, 2560 at 2x), never the ratio alone; and
  `render_checks.insert_geometry` gives each recording its window's pixel
  ratio and its zoom (`InsertGeometry.browser_zooms`). It is what the
  legibility advisory asks of a recording on a narrow screen insert. A
  take's answer, a soft insert and the legibility advice all quote its
  painted size (`spec.painted_size`, `capture.size_words`: "3200x1801
  px painted (a 1600x900 window at pixel ratio 2, its page laid out at
  842x474 by a browser zoom of 190%)"), never the CSS viewport alone, and
  read the pixel ratio it was recorded at off its script
  (`spec.recorded_ratio`).
- **Native high-resolution frames.** The browser launches with
  `--force-device-scale-factor=<ratio>`, and the CDP screencast
  (`Page.startScreencast`, JPEG q95) then delivers device pixels: 2400x1350 at
  1.5x, 3200x1800 at 2x. Without the flag the screencast is the CSS size
  whatever the context's scale (measured, Chromium 153); a screenshot loop
  reaches 7.5 fps where the screencast reaches 18+.
- **Variable to constant rate.** Screencast frames arrive when the page
  paints; `encoder.frames_to_mezzanine` turns them into a constant-rate
  mezzanine (60 fps, CRF 16) with the concat demuxer and per-frame durations,
  so a static page costs nothing.
- **Scrolls paced at the take's rate.** The screencast hands over a
  frame only when the one before was taken: at 3200x1800 a take carried 2.1
  frames a second (38f88e75: 128 in 61.4 s; 7 at pixel ratio 1), so a scroll
  reached the insert as 2 positions a second. While a step scrolls (a
  `scroll` step, or an element scrolled into view), `_Recorder.scroll_log`
  reads at 60 Hz where the window and the scroll containers under the
  cursor or holding the element stand (`_SCROLL_WATCH_JS`, stamped on the
  screencast's clock). Once recorded, `scroll_pacing.pace` draws each
  scroll painted under 90% of `TakeScript.fps` (30, or 60 for a 60 fps
  scene; `record_browser_demo` with `target_render` takes the video's)
  again at that rate. A scroll is a vertical translation of a page already
  painted:
  - a window's keyframes say the offset they painted (the screencast's
    `scrollOffsetY`, kept by `_Screencast` as `CaptureResult.painted`): its scroll is paced on them (`offsets: painted`); a scroll
    container's keyframes, or a keyframe that did not say, have each
    offset read off its pixels against the keyframe before (`register`, on
    4 px blocks then to the pixel; `offsets: registered`);
  - the lag between the log's clock and the paint's is fitted on those
    offsets (`clock_lag`);
  - each frame is the page at the log's offset for its instant, its rows
    cut from the keyframe before, then the one after;
  - what stands still on every keyframe (a fixed header, a sidebar, what
    lies outside the container) is kept as painted (`static_bands`);
  - a wheel's stalls between increments are played through (`moves`), as
    a browser that does not animate the wheel lands on each one.

  - between two keyframes the frame's offset stays between the offsets they
    painted: a slow page's main thread answers the 60 Hz log once a
    paint, so the log runs a whole paint behind, past `MAX_LAG`, and the
    keyframes' own offsets bound it;
  - a page that repaints on its main thread at every offset hands over a
    frame whose `scrollOffsetY` already says the next offset over the paint
    before ("reported 76 px, pixels 0 px", then "reported 0 px,
    pixels 76 px"; paced on what they said, 30 of the scroll's 78 frames
    repeated the one before). Each offset a window's keyframe says is
    checked against its pixels (`_checked`): a keyframe showing the one
    before as it was takes its offset, one matching another shift within
    twice `STILL_LEVELS` takes that shift (`read`: how many), and two
    keyframes sharing no rows keep what they said. A keyframe painting the
    offset of the one before is the paint lagging, never a stop
    (`_anchors`);
  - a `scroll` step keeps the wheel's turns as it sent them (`wheel: {t0,
    pixels, turns}`, a busy host's late turns included; `[[t, share], ...]`
    once paced). A scroll that one scroller made plays at the pace the
    wheel turned, from where the page rested to where it came to rest, as
    late as the page painted it (`_wheel_late`, kept as `late`): what a
    compositor shows, never the page's paint crawling ahead of it.

  A scroll that moved sideways, moved two scrollers at once, or was painted
  at its rate keeps its frames and says why. The scroller is the one that
  moved: the window - the document's own scroller, so a body laid
  out as a scroll container is a container - or one the watch marked (the
  deepest under the cursor and its ancestors, those holding an element
  brought into view, one that fired a scroll), named as the page names it
  (`_NAME_JS`: tag, id, two classes, role; `element`). A container is
  "found once it had begun to scroll" only when the log's first sample,
  on the page's own clock, does not list it: the recorder's clock opens
  the span a round trip earlier, which on a slow page is more than the
  two samples it allowed, and every inner panel's scroll stayed at its
  paint rate (2 to 3 fps). Scrollers that moved one after the other - a
  panel under the wheel to its end, then the page - are a scroll each
  (`read_scrolls`), cut halfway between; only two moving at once are not
  paced. Each scroll of the take's events keeps `{t, scroller, element?,
  box?, wheel?, log, painted, fps, paced, frames?, offsets?, read?, late?,
  still?, held?, outran?, seconds?, why?}`. A scroll that held frames (past what any
  keyframe painted) plays at the share that moved; one whose two paints
  moved further than its scroller shows outran them (`outran: {moved,
  between, shows}`), and the take's notes say the `seconds` its step takes
  for every two paints to share 40% of its rows (`OVERLAPPING`,
  `outran_note`: "Step 2's scroll moved 1333 px between two paints 0.50 s
  apart, further than the 840 px its scroller shows: ... Record it again
  with seconds: 4 on step 2"). The OpenAEV exposure overview repaints its
  whole page on every scroll offset (a `background-attachment: fixed`
  gradient): 3.2 to 11.6 fps painted, now drawn again at 30
  (`test_video_studio_recorder_motion_chromium`: a page painting 300 ms
  an offset, 600 px over 2.5 s at 1600x900).

  Its limits (`test_video_studio_scroll_pacing_edges_6413`):
  - an upward scroll (`to: top`) is paced as a downward one; what stands
    still on every keyframe - above, below or beside the moving rows - is
    kept as painted;
  - a fixed or sticky element outside those bands, a CSS animation or a
    playing video inside the moving rows is translated with them: it
    slides or freezes between two paints;
  - a flat page, or two keyframes sharing under `MIN_OVERLAP` of their
    rows, gives `register` nothing to match: the offset the log or the
    screencast gave stands there, and a take whose keyframes are all alike
    keeps them ("nothing of it moved on its keyframes");
  - content repeating within `SEARCH` rows (identical list rows, a striped
    background) can register a period off, and one such pair skews the
    lag `clock_lag` fits on them all; a window's own painted offsets
    are checked against their pixels first;
  - a scroll faster than a screen between two paints holds the keyframe
    before it and says the `seconds` that keep two paints overlapping;
  - `clock_lag` finds a log running up to `MAX_LAG` ahead of the paint or
    behind it, past the two frames it once assumed.
- **A canvas zoomed by the wheel.** A wheel over a graph drawn on a
  canvas or by WebGL (a knowledge graph, an attack path drawn on a canvas)
  zooms it and moves no scroller. Such a scroll used to be kept as "it did
  not move", and the take was then judged on the frames it painted over its
  length (1.4 to 5 fps at pixel ratio 1.5 or 2) and refused at save.
  - Before a `scroll` step turns the wheel, `_Recorder._canvas_at` reads the
    topmost canvas under the cursor (`_CANVAS_AT_JS`). It keeps the canvas's
    context, client box and the point. It also keeps the boxes of what the
    page draws over it (`over`, read on a 24x14 grid: shown, under half the
    canvas). Every canvas remembers the kind of context it was given
    (`_CANVAS_CONTEXT_JS`, an init script), so a context is never created
    to find out.
  - Over a canvas the wheel turns on the take's own clock
    (`TakeScript.fps`), not at 60 Hz. Each turn is painted before the next
    (`_CANVAS_TICK_JS`: a `requestAnimationFrame`, then a task, so the
    frame's rendering is done). A turn is logged as `[t, delta sent, 2D
    transform | null]`, the transform read off a 2D context
    (`getTransform()`). A canvas painting slower than the clock gets as many
    turns as it paints, the delta caught up on each, so the burst of wheel
    events that held its repaint rate down is never queued. Once the wheel
    stops it is read on for 0.3 s, then until the transform holds (1 s at
    most).
  - The entry keeps all of it as `surface`. When no scroller moved,
    `scroll_pacing.read_scrolls` reads it as a `canvas` scroll. Its log is
    the ln of the transform's scale (`progress: transform`) where every turn
    read one that changed, else the wheel's delta sent (`progress: wheel`: a
    WebGL canvas).
  - `scroll_pacing.register_zoom` reads how the canvas moved between two
    keyframes off their pixels: a zoom `p -> s * p + t`, no rotation,
    searched about the cursor and the canvas's centre on a 200 px wide copy
    (ln s within 1.4), then refined at 400 and 800 px. It answers the
    identity when nothing moved, and `None` when no zoom explains a quarter
    of what tells the keyframes apart.
  - What stands still is left out of the comparison and kept as painted
    (`standing`): a box drawn over the canvas whose inside every keyframe
    paints the same, and any drawn pixel (text, an icon) every keyframe
    paints the same. The canvas's plain ground is never counted, since its
    content zooms across it.
  - Each frame between two keyframes is the keyframe before zoomed on `u`
    of the zoom, blended into the keyframe after zoomed back the rest
    (`_draw_zoom_frame`, `Zoom.share`; each fades over 24 px where what it
    painted ends). `u` is how far the log went between them, else how far
    in time (a canvas easing on after the wheel).
  - The scroll keeps `{scroller: canvas, box, log, progress, painted, fps,
    paced, frames?, offsets: registered, zoom, still_share?, held?, why?}`.
    One whose keyframes could not be registered (more than a quarter of its
    frames held) keeps its frames at the rate they were painted.
  - A scroll that moved neither a scroller nor the canvas says "nothing
    moved under the wheel" and scrolls nowhere in `take_rate`. One logged
    "it did not move" may have zoomed a canvas, so its take
    is still judged on what it painted.
  - The take's answer says each step that scrolled, what it moved, the rate
    it was painted at and whether it was drawn again
    (`scroll_pacing.scroll_steps_words`: "Scroll steps: step 2 (scroll)
    zoomed a canvas: painted at 2.4 fps, drawn again from its keyframes at
    60 fps."). A stepping scroll is seen before the take is placed.
- **Transitions paced at the take's rate.** A take of hovers and
  clicks painted only when the page changed (1.0 to 1.4 fps): a tooltip
  fading in reached it as one frame before and one after.
  `capture._MOTION_WATCH_JS`, an init script of every document the take
  opens, notes each element a `transitionrun` or an `animationstart` runs
  on (its computed duration and delay for that property or animation,
  iterations with it) and each one a mutation changes (its subtree's
  longest computed `transition-duration` / `animation-duration`, read when
  the step ends, at most `READ` elements), at most `MOST_SECONDS` (2) a
  motion. A step that changes the page (`capture.MOTION_ACTIONS`: hover,
  click, double click, type, select, wait for, press) keeps those windows
  since it began (`motion: [[start, end, element], ...]`). The screencast
  hands over a frame only when the page paints, and a finished transition
  paints nothing more: until `PAINT_AFTER` past a noted motion's end, the
  watch repaints a dot of one pixel at 1% (`#__xtm_paint`) every 200 ms,
  so the page as the change left it is painted soon after.
  `transition_pacing.pace_transitions` (run by `scroll_pacing.pace`, after
  the scrolls) merges each step's windows and draws each one painted under
  90% of the take's rate again at that rate: each frame the keyframes about
  its instant blended - the one at or before the window's start standing
  for the page from the start, the first after its end (within `MAX_LATE`,
  before anything else starts) for the page from the end, those inside at
  their own instants. A window a scroll moved over keeps the scroll's
  frames; one whose keyframes differ on fewer than `SAME_PIXELS` pixels
  changed nothing (`why: nothing changed on its keyframes`, counted in no
  rate). Each window keeps `{t, element?, painted, fps, paced, frames?,
  why?}`.
- **The rate of each step and of the take.** Each step says the lowest
  rate its keyframes were painted at where something moved, the lowest it
  plays at, and the element it scrolled most (`painted_fps`, `fps`,
  `scrolled_element`, `scroll_pacing.summarise`), and the take's answer
  says each scroll step and each transition step with the element the
  page names (`scroll_pacing.scroll_steps_words`: "Scroll steps:
  step 1 (scroll) scrolled main#panel: painted at 2.1 fps, drawn again
  from its keyframes at 30 fps. Transition steps: step 2 (hover) moved
  div#tip: ..."). The take's real frame rate (`scroll_pacing.take_rate`)
  is the lowest a step plays at, and that step. What moved nothing on
  screen counts in no rate - a wheel that moved nothing on the page,
  an element brought into view that was already there, a
  transition nothing painted - but a `scroll` step logged "it did not
  move" before a canvas was looked for plays at the rate the take
  painted, as a take recorded before its scrolls were logged does over its
  length. Its answer
  says it ("scrolls at 30 fps (27 frames a second synthesised between
  keyframes)"), and a take under `MIN_TAKE_FPS` (24) on a recording scene
  or a screen insert is the save finding `take_frame_rate` naming the step
  and what to do ("take 38f88e75 carries 2.1 fps on step 0 (scroll):
  scrolls will step on ...; record it again at 30 fps"; the `seconds` for
  one that outran its paints, why for one not drawn again).
- **Human pacing and the action log.** The cursor glides on an eased arc to
  each element, typing has a natural rhythm, and the wheel turns on a 60 Hz
  clock over a scroll step's `seconds`, else the pace's speed
  (`capture.scroll_seconds`: 2400, 1500 or 900 CSS px a second, 0.35 to 4 s). The turns it falls behind on are caught up on the next tick, so the
  page scrolls at a steady pace whatever each turn costs (fixed 22 ms sleeps
  made a scroll last as long as the turns' round trips, landing unevenly:
  33 turns over about 1.5 s on a Windows host). Over a canvas it turns on
  the take's clock instead, each turn painted before the next
  (above). Every step
  logs `t0` (start), `ta` (the act), `t1` (end), its element's box, the point
  acted on, the URL and, when the capture scrolled the element into view
  first, that scroll's `[start, end]` (`scrolled`), and the URL the
  step started on (`url_before`: a step that opened another page):
  the planner draws the camera, the cursor and the callouts from it.
  Redaction (blur) and hiding are CSS injected before any page script.
- **Optional steps.** A step waits for its element `timeout` seconds,
  else `TakeStep.wait_seconds`: 15 s, 3 s for an `optional` one. A skipped
  optional step is logged with the whole span it waited (`t0` when it
  started, `t1` when it gave up, `ok: false`), and the planner cuts that span
  (below), so a banner that never showed costs no footage.
- **Holds that watch their page (`page_settle.py`).** A `wait` used to
  sleep its seconds whatever the page showed: after a click on an OpenCTI
  entity's `Knowledge` tab, whose graph and Diamond model fetch for 7 to 9 s
  in production, a 6 s wait ended on a progress bar or an empty graph, seen
  only on the contact sheet. Now every hold step (`page_settle.HOLD_ACTIONS`:
  `wait`, `pause`, `highlight`) reads its page while it holds it
  (`capture.watch_page`, every `SAMPLE_SECONDS`, 0.2 s):
  - `capture._SETTLE_WATCH_JS`, an init script of every document the take
    opens, notes when the DOM last changed (a `MutationObserver`; the
    recorder's own `__xtm*` nodes aside). Its `read()` returns the progress
    indicators shown: `role=progressbar` (MUI's LinearProgress and
    CircularProgress render it), a `progress`, `aria-busy=true`, an indicator
    with a value only while that value moves. It also returns the boxes of the
    largest canvases shown (`MOST_CANVASES`, `MIN_CANVAS_SIDE` CSS px a side),
    each named as the page names it (`_NAME_JS`);
  - a canvas's pixels are read off the frame the screencast painted last
    (`_Screencast.latest`, decoded about `READ_WIDTH` wide,
    `page_settle.canvas_crops`), so a WebGL or a tainted canvas reads like any
    other;
  - `page_settle.PageWatch` judges the reads. The page is **settled** once no
    indicator shows, the DOM stood still `STILL_SECONDS` (0.5) and each
    canvas's pixels as long - twice the interval between two of its paints
    when it paints slower (a page painted at 2.1 fps stands still 0.96 s
    before it is settled), at most `MOST_STILL_SECONDS`. A canvas repainting
    the same picture on every frame is still. It settled at the instant the
    last of them moved. The page is **loading** while an indicator shows or a
    canvas paints one colour only (`is_empty`: under `EMPTY_SHARE` of its
    pixels off its main colour);
  - an element running an animation that never ends (a CSS spinner, a
    skeleton's shimmer - no DOM change, so the page read as settled with one
    on screen) is an indicator too while its pixels move: `read()` returns
    the shown elements of `document.getAnimations()` running with infinite
    iterations (`looping`, `MOST_LOOPING`), their boxes are cropped off the
    frame with the canvases' (`PageRead.boxes`, keyed `loop:`), and a crop
    changed since the last read at `LOOP_NOISE` (6 levels: a JPEG block whose
    pixels did not change encodes the same) marks the read busy, named
    `div.spinner (animating)`. A wait holds until it stops, or names it when
    it gives up.
  - **ambient** (#10040): a looping element with no loading signature
    (`page_settle.loading_signature`, `LOADING_SIGNATURE`: spin, loader,
    loading, skeleton, shimmer, progress, busy, placeholder, pulse, a
    `[status]` role in its name) shown from the hold's first read while the
    DOM stands still (particles, `span.animate-ping`, an orbit) is
    decoration: kept out of `loading`, logged `ambient: [names]` on the
    step's event, never keeping a wait from settling. The take's answer says
    it as a fact (`page_settle.ambient_words`: "Ambient animation (not
    loading): ..."), never a finding. A loop that appears mid-hold, or one
    named like a spinner or a skeleton, stays loading.
    `page_settle.loading_hold` re-judges the logged spans the same way
    (`ambient_names`: a signature-less `(animating)` name in the span the hold
    began on, within `AMBIENT_LEAD` 1 s, and the one it ended on, within
    `AMBIENT_TAIL` 0.5 s), so a take recorded before the rule loses its
    `take_loading_hold` advisory at its next save, with no new recording.
    Take-wide (#10064): an element proven ambient on one hold of a take
    (`page_settle.take_ambient`) is struck from every hold of that take by
    `loading_holds`, so a decorative pulse the page starts once loaded, first
    seen mid-wait on the wait that loads it, is not a loading hold there
    either. A name with a loading signature is never struck this way.

  A `wait` holds **until settled** (`TakeStep.until_settled`; unset,
  `page_settle.until_settled`: on after a `click` or `double_click` that
  opened another page, its `url` not its `url_before`, or clicked an element
  inside a `role=tab`, logged `tab: true`). It lasts its `seconds` at least
  and, past them, until the page settled, at most its `timeout`
  (`TIMEOUT_SECONDS`, 10) more; its event keeps `until_settled: true` and
  `settled: {at, after, seconds}` (`after`: seconds into the wait), or
  `{at: null, waited, seconds}` when it gave up. The action log says it ("step
  2 wait at 1.7 s (until settled: settled at 9.1 s, 7.4 s in, past its 6
  s)"). Every hold keeps the spans it showed loading (`loading: [[start, end,
  [what showed]], ...]`, a canvas painting nothing named `canvas#graph
  (empty)`), which `page_settle.loading_hold` reads:
  - a `pause` or a `highlight` is flagged for every second it showed loading,
    since the video plays them at their speed;
  - a `wait` is flagged for the span it ends on: it handed a page still
    loading to the next step;
  - a stretch under one read's interval is not flagged, except a wait that
    gave up.

  The take's answer says each flagged hold (`page_settle.holds_words`:
  "Holds on a loading page: step 4 (pause) holds on a loading page for 2.1 s
  (10.2-12.3 s: span.MuiLinearProgress-root...[progressbar]). Before placing
  it in a video, record it again so its page has loaded before the hold:
  ..."), and a video showing one has the save advisory `take_loading_hold`
  (below). `take_preflight.estimate` counts a wait's `seconds`, not what it
  may hold past them: a take a slow page held longer still stops at the cap,
  `ready (truncated at step N)`.
- **Contact sheet.** A grid of frames at the steps, returned to the agent to
  check the recording before producing (`capture.sheet_frames`): half a second
  after each main action and, the frame painted when each skipped
  step gave up (`SHEET_SKIPPED`, 3, `step N skipped: ...`). A take a step
  failed has one too (`capture.failure_sheet_frames`, stored by
  `runners._failure_sheet` on the failed row, purged with it): the frames after
  its steps up to there, the last one painted when the step failed (`step N
  failed: ...`), attached to the failure's answer. `save_video_frame` keeps any one
  frame as a picture (see "The first frame"); a statement's `backdrop` and an
  image scene may name the frame instead (`spec.RecordingFrame`: `take_id`
  with `at_step` or `at_seconds`), which the save keeps as a picture
  once (`take_frames.resolve`, before `verify_references`): its `file_id`
  written beside the recording with `frame_of` (the moment it holds), the
  frame taken again only when the take or the moment changes. A recording's
  id given as a `file_id` is refused with the form a frame takes
  (`projects.unknown_file_words`).
- **Looking before scripting.** `snapshot_browser_profile_page`
  (`capture.snapshot_page`) opens a URL of the profile's application (its
  `start_url` by default) in the saved session - signed in again first when
  the profile can do it alone, the take's pre-flight, the renewed session
  saved - waits for it to settle (watched as a hold is,
  `capture.watch_page`, its canvases read on CDP screenshots, at most
  `page_settle.SNAPSHOT_SECONDS`, 20 s) and says how long it took once it
  opened (`PageSnapshot.settled_after`, `page_settle.snapshot_words`: "It
  settled 7.4 s after it opened ...", what a wait after the step that opens
  it holds), and returns its screenshot (a file of the
  conversation) and its interactive elements: Playwright's
  `aria_snapshot` read by `capture.interactive_names` (role, accessible
  name, `role=<role>[name="..."]`), the names a `role` + `name` step
  matches. Nothing is typed, no step runs. A step that does not find its
  element - an optional one skipped, or one that fails the take - is
  reported with the `NEAREST_NAMES` (3) interactive names the page offered
  at that moment (`capture.closest_names`: every word of the asked text in
  the name first, a word matching its plural or stem, then how alike they
  read; the skipped event keeps them as `near`), and what the page was
  filtered by then (`capture.page_filters`: the text its fields
  held and its address's parameters, `filters_words`; the skipped event
  keeps it as `filters`): an OpenCTI list page keeps its search term
  across navigations, so a later `text=` step misses the row the author
  expects - 'The page then held its field "Search" "alpha"; its address
  searchTerm=alpha: a search term or a filter the page kept narrows what it
  lists.' A `role`, `text` or `selector` step also says the page
  state it missed its element in (`take_state.page_state`, `missed_words`; the
  skipped event keeps `page` and `found_before`): the page's URL with its
  query, its title and how many elements of the step's role it showed - or
  matched its text or selector, and how many of them were visible -, and,
  when a ready take of the same person and profile found the same step within
  `EARLIER_HOURS` (24 h, `load_earlier_finds`, read by `runners.run_take` and
  passed as `record_take(earlier=...)`), "It was found on take X at URL Y; this
  take is at URL Z: the page is in another state". A step is the same when it
  names the same accessible name (whatever its role), text or selector
  (`take_state.step_key`); the URL is the one its page was at when it started.
- **A clean page state per take (`capture.reset_page`,
  `browser.open_cleared`).** A profile's saved session carries what its last
  take left in the page - a graph's layout in its origin's localStorage, read
  back into its address (`?layoutMode=radial`) - so a take that opened in it
  found another page than its script was written on. `TakeScript.reset_state`
  (default `true`; `spec.PageReset`: `url_query`, `local_storage`,
  `session_storage` on, `indexed_db` off) runs once the session is checked,
  when the browser was loaded with a saved session: `start_url` is opened
  exactly as given (`url_query`; off, it is opened and reloaded at the address
  it landed on) with a one-shot init script that, at the new document's start -
  after what the page left on unload, after the saved `sessionStorage` was put
  back, before any of its scripts reads it -, removes every key of the
  origin's localStorage and sessionStorage but the profile's
  `keep_storage_keys` (`ProfileSnapshot.keep_storage_keys`, below) and deletes
  its IndexedDB databases when asked; cookies are never touched. The init
  script that puts the saved `sessionStorage` back is then disposed
  (`ProfileBrowser.session_restore`), so a later page does not bring a cleared
  key back. `capture.opened` keeps `{asked, carried, reset, cleared, url,
  added_query}`, the first line of the action log
  (`take_state.opened_words`): "opened on URL - page state reset before step 0:
  cleared 2 localStorage keys (...); kept auth (the profile's
  keep_storage_keys); cookies untouched; start_url opened as given", and the
  parameters the page wrote into its address itself (state the reset does not
  reach: the application's own data). A page the reset signed out - its session
  passed the pre-flight - fails the take with `ResetSignedOutError`, naming
  what was cleared and `keep_storage_keys`; the profile's saved session is not
  marked expired. A `goto` step takes `reset_state` too (its event logs
  `reset`), and `snapshot_browser_profile_page` opens its page the same way
  (`snapshot_page(reset_state=...)`), so the names it lists are those a take
  finds. `check_browser_profile_session` probes as before.
- **An application's own preference, seeded (`TakeScript.local_storage`,
  `browser.seed_storage_script`, `capture.seeded_keep`).** `color_scheme`
  only emulates `prefers-color-scheme`; an application keeping its theme in
  its own storage (the Copilot web app reads `copilot-theme`, never the media
  query) ignores it, and the page state reset clears that key. A take's
  `local_storage` (`{key: value}`, at most `MAX_SEEDED_KEYS` = 20) is written
  into the start URL's origin by an init script registered before step 0 and
  before any reset's, at the start of every top document of that origin before
  its scripts run; its keys join the profile's `keep_storage_keys` in every
  reset of the take, so the reset clears what earlier takes left and never what
  this one seeds. Nothing reaches the signed-in account and no request is made.
  `{"copilot-theme": "light"}` records the Copilot web app in its light theme.
  `from_recording_id` copies it (`take_script.RECORDING_PARAMS`).
- **Choosing in a combobox.** `select` on a native `<select>` picks
  its `value` (a value or a label). On anything else - an ARIA combobox, a
  button opening a list, an OpenCRQ tenant picker - `_Recorder._pick_option`
  clicks it open and finds the option by its accessible name
  (`capture.find_choice`: `CHOICE_ROLES`, an exact name then a name
  containing it in any case, in the list the control names by
  `aria-controls` / `aria-owns` first - its id quoted as JSON, a valid CSS
  string whatever it holds -, then anywhere on the page, a portalled list
  included: there only an `option` is taken anywhere, a `menuitem*`,
  `treeitem` or `gridcell` only inside a popup (`_POPUP_CONTAINER`), so
  a navigation entry or a cell named like the option is never clicked); an
  editable combobox whose list does not show it has the value typed first,
  one still showing nothing is opened with ArrowDown. The three tries share
  one deadline, `OPTION_WAIT_SECONDS` or the step's own wait when shorter
  (an optional step gives up as fast as its wait says). The cursor glides to
  the option and clicks it; the event logs `option` (its name and box),
  `control_box`, and as `box` the two together, so the camera frames the
  control and its choice. No such option: the list is closed (Escape) and the
  step fails naming the options it offered; a list that cannot even be
  searched fails the step too (`TakeError`), never the whole take.
- **Role families.** A `role` + `name` step is looked for under its
  own role first, then under its family's (`capture.ROLE_FAMILIES`:
  `menuitem | menuitemcheckbox | menuitemradio`, `checkbox | switch`,
  `option | treeitem`), and a `button` under `menuitem` only by its exact
  name held by one element (`UNIQUE_ROLE_FAMILIES`); `_Recorder.locate`
  polls them within the step's own wait, and the event logs `matched_role`
  when a family member matched.
- **The time limit, before and during the take.**
  `take_preflight.estimate` plays the script in the estimate (waits and
  pauses, an optional `wait_for`'s whole timeout, `ACTION_SECONDS` plus the
  settle per action, keystrokes at the pace's `spec.TYPING_DELAY_MS`); past
  `CAP_SHARE` (90%) of `VIDEO_STUDIO_MAX_TAKE_SECONDS`, `record_browser_demo`
  refuses it before anything is stored, naming the step that crosses the cap
  and the last `marker` before it as the split point. At run time each step
  is bounded by the time left (`asyncio.timeout`): the cap stops the step it
  falls in, the take is encoded up to there and stored `ready`, its notes
  saying where it stopped (`capture.truncation_note`); `jobs.truncated_at_step`
  reads it back (a take logs one event per step played) and the answer says
  `ready (truncated at step N)`.
- **No credential in a step.** `take_preflight.credential_refusal`
  refuses a `type` step whose selector names a password field
  (`type=password`) or whose text holds the profile's stored password or
  TOTP secret; at run time a `type` step into a password field - its
  element's, or the focused element's, type - raises `CredentialRefusedError`
  before a key is typed, and the runner blanks that step's text in the
  stored script. `check_browser_profile_session` says when a profile that
  needs a sign-in stores no password and cannot renew by itself.
- **Encode progress.** `encoder.frames_to_mezzanine(on_progress=...)`
  passes ffmpeg `-progress` and reads its last `frame=` every
  `PROGRESS_REPORT_SECONDS` (5 s, `encoder.run(wait_every=...)`): the take's
  stage reads `Encoding the recording (62 %, 5,580 of 9,000 frames)` and its
  `progress_at` moves with it.
- **A canvas acted on (`pointer.py`).** A graph drawn on one
  `<canvas>` has no element for its nodes, so a pointer step
  (`spec.POINTER_ACTIONS`: `click`, `double_click`, `right_click`, `hover`,
  `drag`, `wheel`) may name, instead of a locator, a point (`at: {x, y}` in CSS
  px of the window, or `{x, y, in: "<selector>"}` on that element - a value up
  to 1 a share of its box, larger CSS px from its corner, `pointer.placed_in`)
  or a node (`node: {label}`). A node is found by its label with nothing
  asked of the application filmed (`node_locate.locate`, called by
  `pointer.locate_node`), read again before every step - never kept across a
  pan, a zoom or a layout change - in three stages, the one that found it
  logged on the event (`located`: `by` `ocr` / `highlight`, the `point` it
  resolved, a `said` line, what each stage read; a drag's release
  `to_located`), the titles the node goes by kept beside it
  (`Target.names`: the label, the entry's titles and head,
  `node_locate.names_of`):
  1. **Identity** (`node_locate._MIRROR_JS`, `match_label`): the visually
     hidden (`sr-only`, or off-screen) `option` / `treeitem` entries of every
     `role=listbox` / `role=tree`, and the `aria-label`s of canvas children,
     matched on the text before the first comma (`Entry.head`, "type title" of
     "type title, N relationships"): the label exactly or as its end after a
     space (the title after the type), then case-insensitively, then as the
     beginning of the head or of one of its words for one entry only (several:
     ambiguous, named). The titles the canvas may paint follow: the matched
     end, or a whole head and its word suffixes (read only as whole labels).
     A list without the label fails the step at the step's wait naming the
     `NEAREST_LABELS` (3) closest entries (`pointer.closest_labels`); a page
     with no such list goes on with the label as its title.
  2. **Position** (`canvas_ocr`): the shown canvases (the largest first, at
     most `MOST_CANVASES`, stacked layers shot together) are screenshot at the
     page's pixel ratio (`page.screenshot(clip, scale="device")`) and read by
     Tesseract (`--psm 11`, sparse text; grey, inverted on a dark ground,
     doubled under pixel ratio 2; one thread), words under an element drawn
     over the canvas left out (`over`, `node_locate._OVER_FN`, read with the
     page's mirror and again as each shot is taken: the elements a hit
     test finds on a grid over it, and those it cannot - shown with
     `pointer-events: none` and meeting the canvas, a hover card repeating
     the node's title in larger type - each under half the canvas).
     Words side by side on a line make a label (`canvas_ocr.labels`: a gap
     over `LABEL_GAP` line heights starts another). A label holding the
     title that is drawn as an edge's is left out
     (`canvas_ocr.edge_label`, said in `located.ocr.left_out`): `rotated` (the
     lowest ink of each group of `_BASELINE_GROUP` label heights of columns
     fitted by Theil-Sen slopes over `ROTATED_SLOPE`, 0.05), `italic` (the
     strength-weighted median lean of the edges within `_UPRIGHT_EDGE` of
     vertical is `ITALIC_LEAN`, 0.12, or more) or `along a line` (a thin
     straight line at its height runs on from both its ends for `_RUN_ON`
     label heights past `_HALO` of clear ground, as a graph clears a halo
     around an edge's label). `find_title` reads the
     title as a whole label exactly, then normalised (whitespace, case), then
     inside a longer label. The node is `LABEL_TO_NODE` (1.2) label heights
     above the label's centre and the camera frames a square of `NODE_SIDE`
     (2.4) label heights with its label (`node_point`): the label's height is
     the zoom. A box that moved over 1 px between two reads is read again (a
     layout settling) until the step's wait ends.
  3. **Highlight** (`highlight_of`): when OCR finds no box (or no OCR engine
     is installed) on a canvas holding still between two shots, the entry's
     list is focused (`_FOCUS_JS`, without scrolling; else its roving entry)
     and its active entry (`aria-activedescendant`, else the entry focused,
     else the one selected; its place `aria-posinset`, else its index: a list
     may mount a window of its entries) moved with ArrowDown / ArrowUp (two
     tries, `MOST_KEYS` at most), the canvas shot two frames after each move
     and the focus given back. A graph's hovered element is one, shared by the
     pointer and the list (OpenCTI's), so the shot before the list had focus
     also holds the node the pointer hovered before: the canvas is shot with
     an entry beside the node active, the node's, then the other entry beside
     it (`_beside`: one each side, else two on its one side), and what
     changed both onto the node and off it is its highlight. The node's shot
     is taken only once the list's active entry is the node's, compared by
     text; one that stops elsewhere fails the step "the list's active entry
     stopped on '<entry>', not on the node's ('<entry>')". A list under three
     entries, or one whose entry beside will not take, reads against the shot
     before. What the page draws over the canvas at any of the shots read
     (`over`: its hover card following the active entry) never counts. The
     node is where what changed is thickest - the point farthest inside it,
     its holes filled, so a ring drawn around a node reads as the node -,
     never the middle of the links and the label it redraws with it; the box
     the camera frames is all that changed around it (parts within
     `_JOIN_PX` joined). Nothing changed, or more than `MOST_CHANGED` (10%) of
     the canvas, fails the step "node not located on the canvas (label read
     in the list, no box by OCR, no highlight on focus: why)".
  A node not found carries the canvas shot (`TargetNotFoundError.picture`):
  `record_take` writes it beside the frames (`StepFailedError.attached`) and
  the failed take's contact sheet ends on it (`failure_sheet_frames`). A label
  neither mirrored in a list nor painted cannot be located, by design; the
  failure says which is missing. A page that still exposes the old demo hook
  `window.__xtmDemoLocate(label)` (`pointer.LOCATE_HOOK`) is read through it
  for one more release (`located: {by: hook, said: "located through the
  application's hook"}`), then the hook is dropped. The images ship
  `tesseract-ocr` beside ffmpeg (both Dockerfiles, the Chromium CI job).
  `_Recorder._pointer_step` glides the cursor to
  the target and rests on it `hover_ms` (`pointer.HOVER_MS`, 250, on a point or
  a node; none on an element), moving by under a pixel every 40 ms - each a
  `pointermove` a canvas reads its hover from, without which a right-click on a
  node opens no menu - then presses: `right_click` reads the `role=menu` it
  opened (`pointer.read_menu`: its entries, box and section titles - each
  `role=group`'s name and the text drawn outside every entry, separator and
  hidden part, in order -, `menu` on the event, `null` when none showed
  within `MENU_WAIT_SECONDS` or the step's `timeout`). On a node that menu is
  checked (`pointer.menu_check`, `menu_check: {title, node}` on the
  event): an application heads it with what it opened on - OpenCTI the
  node's title, "Graph" for the empty canvas, a relationship's type -, so
  when none of its titles names the node (the same words whatever the case,
  one with a type before it, or cut by an ellipsis) the step fails there,
  "the menu it opened is headed 'Graph', 6 entries, not the node
  'Storm-1567': the point it resolved by ocr, (812, 433), is not on the
  node", the canvas the node was read on attached with that point marked
  (`node_locate.marked`); a menu without a title is not checked. A step that
  acted before it failed carries its log entry (`TargetNotFoundError.logged`),
  which `record_take` logs for it; a failed take keeps its action log
  (`runners.run_take`, `events`), and `get_video_recording` says how each of
  its node steps was located (`jobs.located_words`: the stage, the point, the
  menu's first title and whether it was the node's), as the action log of a
  ready take does on every node step; `drag`
  reads where it releases (a point or `{node: {label}}`) once the cursor rests
  on where it presses and before its keys are held, presses, moves there over its `seconds`
  (`capture.DRAG_SECONDS`, 0.8) in `MIN_DRAG_MOVES` (24) eased moves at least,
  and releases (`moves`, `to_point`, `to_box`; its `box` the press and the
  release together); `wheel` turns the wheel on the target through `_scroll`,
  the clock a `scroll` turns on. `modifiers` (`Control`, `Shift`, `Alt`,
  `Meta`) are held through the action (`_Recorder._held`). The event logs the
  target (`target: {node}` / `{at}`) and the box the camera frames: the node's
  size, else a `POINT_BOX` (56 px) square; the planner zooms on a
  `right_click` and a `drag` as on a click (`timeline._ZOOM_ACTIONS`), ripples
  where they press, and stays wide over a `wheel` (`_WIDE_ACTIONS`: the page
  zooms itself). A plain `click`, `double_click` or `hover` on an element, with
  no keys and no rest asked, plays as before (`capture.drives_pointer`).
  `take_preflight.step_seconds` counts the rest, the drag, the wheel and a
  menu's `MENU_SECONDS`. A step whose element is drawn for a screen reader
  only (`capture._SR_ONLY_JS`, read in `_Recorder._box`: itself or one
  of 12 ancestors clipped to nothing - 1 px with overflow hidden,
  `clip: rect(0 0 0 0)`, `clip-path: inset(50%)`) and whose point a click
  does not reach (`elementFromPoint` lands outside it, its labels and the
  control it sits in, `_SR_ONLY_CONTROLS`) fails that very step
  (`SrOnlyTargetError`, through the step-failed path with its page state; an
  optional one is skipped). A graph's accessible list is the case: a
  `role=listbox` mirroring the canvas with one `role=option` per node, whose
  1 px option a click lands off the canvas, so no menu opens and the take
  used to fail steps later. `sr_only_words` names the role, the list, what
  the click lands on and, on a page drawing a canvas, the node step reading
  that entry (`node {label: '<its text before the first comma>'}`, cut at
  `SR_ONLY_NAME_CHARS`, which a node step reads as a prefix) or the
  point (`at {x, y, in: 'canvas'}`) to use instead. A toggle's input under
  its label or an icon button's hidden name is clicked as before.
- **The capture rate (`capture_rate.py`).** The screencast's rate
  falls exactly when a page animates (a graph laid out again) on a worker that
  cannot encode its frames as fast. Before step 1, a worker's take
  (`runners.run_take`, `record_take(warm_up=True)`) measures it:
  `capture_rate.warm_up` opens a bare Chromium painting its whole frame on
  every tick at the take's frame size and counts the screencast's frames over
  `PREFLIGHT_SECONDS` (2 s). Under `SUSTAIN_SHARE` (85%) of the take's `fps`,
  `capture_rate.decide` records it at pixel ratio `LOWERED_RATIO` (1.5) when a
  second warm-up there sustains it - the note says so, the take's `capture`
  keeps `pixel_ratio {asked, recorded}` and the runner writes the recorded
  ratio into its stored script, which every size reader quotes -, else
  (`capture_rate.choose`) looks for the largest window of the take's
  shape that sustains its full `fps`: in the rates measured on this worker
  shape (`worker_shape`: the CPUs and memory ceiling `resources` reads; an
  in-process table every warm-up adds to, `remember` / `measured_table`, each
  rate standing `TABLE_SECONDS`, 15 min), measured where the table leaves it
  open (`sustained_size`: first guess `sustained_window` - a frame's cost goes
  with its pixels -, then halved between the widest that held and the
  narrowest that fell short until within `SEARCH_TOLERANCE`, 5 %, at most
  `MAX_PROBES`, 5, warm-ups), at its pixel ratio, else at the first lower one
  of `RATIOS` where one does, never under `narrowest` (640 px wide, 480 high).
  Its `fps_fallback` (`TakeScript`, default `refuse`) then records at that
  window and ratio (`size`: `CapturePlan.window`, the recorder rebuilding its
  viewport, the runner writing the window into the stored script), at fps 30
  at its own size when the rate measured there holds all of it (`fps`), or
  refuses naming both: "the largest size measured sustaining 60 fps on this
  worker (4 CPUs, 7.5 GiB) is a 1460x821 window at pixel ratio 1.5 (2190x1232
  painted, 61 fps measured); or fps 30 at the requested size (50 fps measured
  there)". A fallback taken is its note and the end of the action log's first
  line (`capture.fallback`); the warm-ups it measured are kept
  (`preflight.probes`). A search whose warm-ups cannot run still refuses a take
  measured too slow, saying so. A warm-up that cannot run at all records as asked.
  **The recording lane (`recording_lane.py`).** The take adapter is the
  render queue's third lane (`jobs.LANES`: `recording`). The warm-up runs in the
  take's slot (`capture.preflight`), in the worker's process at the priority the
  capture records at - a scene preview's process runs `cpu_share.PREVIEW_NICE`
  below it and yields its CPU, never the reverse -, one take of a worker at a
  time (`recording_lane.measuring`: a take waiting for its warm-up is never
  another's load). It reads what runs on its worker before and after
  (`load_on`: the scene previews, renders and other recordings that worker holds
  the lease of) and keeps it (`preflight.beside`). Short of its fps beside other
  jobs it records nothing (`CapturePlan.waits`, `capture.WorkerBusyError`): no
  lower ratio, no smaller window, no refusal - those answer what the worker
  sustains, not its load -, and its rates stay out of the worker shape's table
  (`_Measured.keep`: only an idle worker's stand). Its row goes back to queued
  (stage "Waiting in the recording lane: busy: 2 scene previews and 1 render on
  its worker (21 fps measured ...)"), its execution to its place
  (`defer(place="keep")`, `WAITING_STATE`, the wait in `payload.recording_wait`:
  since, worker, the jobs beside, the rate). Claimed again every
  `POLL_SECONDS` (15 s), it reads its worker's load - a query, no browser - and
  waits again while that load weighs as much as the one it fell short beside
  (`held`, `Load.weight`: a render or a recording 1024, a preview 110); lighter,
  idle or on another worker, it measures again, and on an idle worker the
  warm-up decides as above. `jobs.queue_place` gives its place, the recordings
  ahead by name and what it waits behind (`recording_lane.line`: "waiting in
  the recording lane, place 1 (nothing queued ahead of it): busy: 2 scene
  previews on its worker; it measured 21 fps at 3200x1800 beside them ...");
  once recorded, its notes and the end of its action log's first line say the
  wait (`capture.waited`). A refusal on an idle worker ends "No other Video
  Studio job ran on this worker while it measured"; past `MAX_WAIT` (6 h) a
  busy take is decided as on an idle worker, and its refusal says the jobs
  beside it ("busy: 2 scene previews and 1 render on this worker") and offers
  the lane again. A take cancelled while it waits records nothing: its row
  never goes back from cancelled (`jobs.mark_if_open`).
  **A take asking for a worker class (`worker_class.py`, #10959).** Every
  recording worker says its class in its presence record (`recording_class`,
  with `recording_gpu`): the GPU model its WebGL renderer names, lower-cased
  (`l4`, `v100s`), `gpu` when the renderer names none, `cpu` on software GL
  (`capture_rate.worker_record`). `record_browser_demo(worker_class=...)` keeps
  the class on the take's execution payload; `VideoTakeAdapter.deal_claims`
  hands a worker only the takes asking for none or for its own class, so a take
  asking for `l4` waits in the lane while every L4 worker is busy and never runs
  on a V100S. A class no running recording worker serves is refused before the
  take exists (unpaid), naming the classes there are. The class is a recording
  parameter of the submitted script (`take_script.RECORDING_PARAMS`), kept only
  when given, so `from_recording_id` copies it only when its source asked.
  The worker that claims the take says its class before step 1 (stage
  "Recording on a worker of class l4 (GPU L4)") and on the action log's first
  line (`capture.worker`, `take_state.opened_words`); `serialize_take` returns
  both `worker_class` (asked) and `worker` (recorded on). Capture and pacing are
  unchanged. The
  recording Chromium and its warm-up draw WebGL and their canvases with
  `capture_rate.gl_backend`, decided once per worker process, through
  `profile_browser(extra_args=...)`: under `VIDEO_STUDIO_RECORDING_GL=auto`
  (the default) a Chromium launched under `GPU_GL_ARGS` (`--use-angle=vulkan
  --enable-features=Vulkan --disable-vulkan-surface --ignore-gpu-blocklist`)
  is asked what it draws WebGL on (`gl_status`: CDP `SystemInfo.getInfo`,
  `featureStatus.webgl`), and those flags are kept only when it answers
  `enabled...` (`ON_GPU_STATUS`, a GPU); otherwise, and always under
  `software`, `SOFTWARE_GL_ARGS` (`--use-gl=angle
  --use-angle=swiftshader-webgl --ignore-gpu-blocklist`): a WebGL or canvas
  graph renders on a worker without a GPU, on the CPU. Chromium's feature
  status decides, never the renderer's name: on SwiftShader it reads
  `unavailable_software` for `webgl` and `2d_canvas` (measured on a CPU-only
  worker image, where the GPU flags fall back to SwiftShader too, and on an
  NVIDIA host, where they draw on it). A probe that cannot run is not kept.
  The take keeps what its own browser drew with once recorded (`capture.gl`:
  `gpu`, `webgl`, `canvas_2d`, `why` when software) and the WebGL renderer it
  reports (`capture.backend`), both read once the screencast stopped (a WebGL
  context made before step 1 starts the GPU process the page's 2D canvases
  would then draw on); its answer's capture line says "canvases drawn with
  GPU GL" or "software GL (why)". A recording lane kept to GPU workers is
  `VIDEO_STUDIO_TAKE_CONCURRENCY=0` on the others
  (`VideoTakeAdapter.claims_here`). The screencast itself is the compositor's frame clock,
  never a timer; the mezzanine holds each frame until the next
  (`frames_to_mezzanine`), so the output cadence never drops. Once paced,
  `capture_rate.measure` keeps on the take (`video_takes.capture`): the frames
  kept each second (`rates`), each step's rate while its page painted
  (`capture_fps` on its event: frames over the runs painted without a gap past
  `RUN_GAP`), the stretches animated under `LOW_FPS` (20) for more than
  `LOW_SECONDS` (1 s) (`low`: a run whose rate - averaged over `RATE_WINDOW`
  intervals - stays under the floor while consecutive frames differ on
  `MOVING_SHARE` of the frame at least, so a keystroke's or a caret's repaint
  never counts, and outside the steps' scrolls, which `take_frame_rate`
  judges), and the stretches painted under 90% of the take's rate with their
  painted instants (`sparse`). The take's notes and answer say `low capture
  rate at step 4 (12 fps for 2.3 s)`, its answer the frames kept each second
  and a step under the take's rate `(captured at 12.4 fps)`; a video showing
  that stretch has the save finding `take_capture_rate` (once per take, a
  recording scene only within what it keeps). A recording scene played under
  speed 1 on a sparse stretch (`timeline.slowed_sparse`, `blend` on its
  payload) blends the painted frames on either side of each source instant
  (`compositor_page.blendPair`) instead of holding each one longer.
- **Each canvas measured on its own pixels (`capture_rate.canvas_motion`).**
  The OpenCTI graph's 2D layout moves (layered, hierarchical, radial) were
  captured at 5 to 16 fps under SwiftShader and played stepped, never said:
  the whole frame's rate was judged, over stretches longer than a second, and
  a page painting elsewhere while its canvas stepped read as smooth. Each step
  now logs the canvases it ended on (`canvases` on its event, read from the
  settle watch, `_Recorder.log_canvases`), and on the frames the browser
  painted (`measure(painted=...)`, never the paced ones: a transition window
  is drawn again as a blend) each frame's crop of a canvas moves it where
  `MOVING_SHARE` of its pixels changed from the frame before, read at an
  eighth of the frame (`_CANVAS_COMPARED_AT`). A transition is
  `CANVAS_FRAMES` (5) frames moving it at least over `CANVAS_SECONDS` (0.4 s),
  none further apart than `RUN_GAP`, its rate its frames less one over its
  span, kept on the event of the step it started in (`canvas_motion: [{canvas,
  t, fps, frames}]`, `MOST_CANVAS_RUNS` a step) and said at that step in the
  action log ("canvas#graph moved 1.4 s at 9.8 fps", `canvas_words`,
  `jobs.take_timeline`), whatever its rate. The steps' scrolls (a scroll moves
  the canvas, a wheel zooms it: drawn again at the take's rate) and the spans
  the cursor moved over to `POINTER_TAIL` (0.15 s) past its last move
  (`pointer_spans`: a hover repaints the canvas, it does not animate it) belong
  to no transition. One under `LOW_FPS` is a stretch under the floor (`low`,
  `canvas` on it) unless the whole frame's already is there: "low capture rate
  at step 1 (canvas#graph moved at 10 fps for 1.0 s, software GL)" in the
  take's notes, with `software_words` naming a GPU worker when it was drawn on
  SwiftShader (`gl: software` on every stretch), and the save finding
  `take_capture_rate` saying to record it on a worker whose Chromium draws on
  a GPU then (`QUALITY_REPORT_VERSION` 17).
  `test_video_studio_canvas_6710_chromium.py` records a WebGL canvas animating
  60 frames: above 24 fps where Chromium draws on a GPU (skipped elsewhere),
  flagged on the software-GL path. Measured on an NVIDIA laptop, 1600x900: 60
  fps at pixel ratio 1 on either backend, 40 fps on the GPU and 27.6 fps on
  SwiftShader at pixel ratio 2, where the 60 frames took 1.85 s instead of 1.
- **A canvas captured alone on a CPU worker (`canvas_alone`).** Which
  GL a worker shape draws with: a GPU worker (its Chromium finds a GPU,
  `GlBackend.gpu`) draws canvases and WebGL there and captures them with
  their page at the take's rate; a CPU worker draws them on SwiftShader and,
  where the screencast of the whole page falls short of a transition, also
  captures each canvas alone (`GlBackend.alone`, the one rule). Every
  document runs `canvas_alone.tap_script` first: the 2D and WebGL drawing
  calls mark their canvas, and once an animation frame's callbacks have all
  run (`requestAnimationFrame` counted, so a WebGL buffer is read before it is
  presented) each canvas drawn is copied - with the canvases stacked in its
  box, over the colour behind them - at pixel ratio `ALONE_RATIO` (1), once
  a frame of the take's rate at most. A Web Worker encodes the copy as JPEG at
  `ALONE_QUALITY` (0.8) and drops one alike to the copy before; the page's
  thread pays the copy alone (measured: about 3 ms a frame for a 2D canvas at
  2400x1350, 12 ms for a WebGL one read off SwiftShader; an encode on the
  page's thread cost 15 frames a second of the page's own, an async one ran
  at 2 to 4 fps, a clipped CDP screenshot at 10 to 15 while halving the
  screencast). A canvas copied alike `IDLE_AFTER` (3) times is copied every
  `IDLE_MS` (250) until it changes or the page is acted on. Each copy keeps
  its instant, its canvas's visible box and the boxes drawn over it (a grid
  of `elementsFromPoint`), and makes the page paint on a moment
  (`__xtmMotion.paint`), so the screencast hands over the state it left;
  `canvas_alone.Tap` drains them every 0.25 s (`CaptureResult.alone`,
  `capture.alone`: `ratio`, `quality`, `copies`, and `off` where the page
  refused the encoder - a `worker-src` policy). Once recorded and before it is
  paced, `canvas_alone.composite` makes runs of each canvas's copies
  (`RUN_GAP`, one box, `CANVAS_FRAMES` over `CANVAS_SECONDS`, none during a
  scroll); a run painted at `PAINTED_ENOUGH` of the take's rate, or copied no
  faster, keeps its painted frames. Any other is checked against what the
  browser painted at rest about it (`check`: its last copy against the first
  frame painted once it stood still, the copy before it against the last
  frame painted before it moved), both read at a quarter and averaged over 3
  px (`SOFTENED`: labels and outlines drawn at the copy's ratio differed on
  2.8% of a 300-node graph unaveraged, 0.3% averaged), outside what is drawn
  over it: past `MISMATCH_SHARE` (2%) of the canvas differing by
  `MATCH_LEVELS` (32) - a layer it did not draw, a buffer read once presented
  - the run keeps its painted frames, its `why` kept. A run that matches is
  composited: at each copy's instant the last frame painted, each canvas then
  moving drawn into its box from its latest copy but where the page draws
  over it (`a000001.jpg`, `capture_rate.COMPOSITED_PREFIX`); the frames
  painted over its span stand behind them. `capture.alone.runs` keeps each
  run (`canvas`, `t`, `step`, `copies`, `fps`, `painted`, `composited`,
  `why`); the notes say each one composited, the answer's capture line
  "canvas transitions captured alone: 1 composited", and its transition in
  the action log "canvas#graph moved 1.5 s at 29.6 fps, captured alone"
  (`alone` on it).
- **Stepping motion (`capture_rate.stepping`).** The motion no pacer
  draws again plays each frame captured to the next: a frame of the take's
  rate with none captured in it is synthesised from the keyframe before. A
  canvas transition (its frames those that moved it) and a stretch of the
  whole frame moving outside the steps' scrolls, transitions and canvas
  transitions, painted under half the take's rate for `CANVAS_SECONDS`, are
  stepping where more than `STEPPING_SHARE` (0.5) of their frames
  (`frames_at`) were synthesised: `capture.stepping` (`t`, `frames`,
  `synthesised`, `share`, `fps`, `step`, `canvas`, `gl`, and `alone`: why its
  canvas was not composited there), `synthesised` on the step's event, "stepping
  motion at step 1 (canvas#graph moved 1.5 s: 33 of its 46 frames at 30 fps
  synthesised from keyframes)" in the notes (`motion_notes`: a low stretch it
  covers is not said again, `besides`) and the save finding
  `stepping_motion` (`QUALITY_REPORT_VERSION` 20). A scroll or a canvas zoom
  drawn again at the take's rate is motion, never synthesised here:
  `take_frame_rate` judges those. `test_video_studio_canvas_alone_6761_chromium.py`
  records a 300-node graph laid out again over 1.5 s at 1600x900 at pixel
  ratio 1.5, fps 30: 24 fps or more on a GPU worker (skipped elsewhere), 20
  fps or more composited from its copies on software GL with the screencast
  held to 8 fps, and the same throttled take without the canvas-only path
  saving with `stepping_motion`.
- **Stepping graded as each scene draws it (`capture_rate.stepping_grade`).**
  A stretch and a canvas transition keep their longest held keyframe
  (`hold`, seconds of the take; a stretch kept before it is held for its
  mean painted interval). Each scene showing a stretch grades it at the rate
  its `time_map` plays the take there: `advisory` when the transition lasts
  `STEPPING_BRIEF_SECONDS` (1.25 s) of the take at most, is painted at
  `STEPPING_ADVISORY_FPS` (10) a second of output at least and holds no
  keyframe past `STEPPING_HOLD_SECONDS` (0.25 s) of output; `dense`
  otherwise. A recording scene playing it slowed over the painted instants
  its take kept (`sparse`, `BLENDED_SHARE` 0.9 of it: what the compositor
  blends, `timeline.slowed_sparse`) does not say it. The dense finding
  offers slowing the scene only where that blends it, says otherwise that it
  would not, and offers fps 30 (`fps_fallback: fps`) to a 60 fps take drawn
  on software GL. Every scene showing a stretch says it (one check lists them
  all), not the first only (`QUALITY_REPORT_VERSION` 26). A 60 fps take
  drawn on software GL says in its answer that its canvas transitions will
  step and that fps 30 is the fallback (`software_gl_notice`).

## Browser profiles

A recording signs in with a browser profile, a platform entity of its own
(`dev-docs/browser-profiles.md`): credentials, TOTP, interactive
MFA, saved sessions, the keep-alive and the URL policy live in
`services/browser_profiles/`, and the recording browser is its
`browser.profile_browser`. A take uses any profile its person may use (no
agent binding is needed: the take is a scripted job, not an agent browsing).

- **Recordings fail fast.** `capture.record_take` opens the profile with
  `signin.ensure_signed_in`: a profile that cannot open the application is
  signed in again when it is `renewable` (credentials, or a saved IdP session
  and a `login_url` to replay it on), else the take fails with
  `ProfileNotAuthenticatedError` ("profile not authenticated: landed on
  <url>") before the screencast starts. The session a take ends with replaces
  the saved one only when its probe passes.
- **The first frame is signed in.** The take's start page, opened
  again in that session, may paint the application's sign-in form while it
  reads the session (a single-page application waiting on its session
  call). `signin.wait_signed_in` waits, without navigating, for the
  signed-in application: the profile's `signed_in_selector` on the page,
  else its URL matching `signed_in_url_pattern` with no sign-in form (read
  as soon as they show, `signin._marked`, the same marks `_judge` reads),
  else the pre-flight's own judgement of the rested page (`_follow` +
  `_judge`). The screencast starts only then, so nothing painted before is
  in the take and the action log counts from that moment. A page that does
  not show signed in within the profile's `sign_in_timeout` fails the take
  with `ProfileNotAuthenticatedError` like the pre-flight, before any frame.
- **How far down the page keeps its layout.** Once the take is
  recorded - the screencast stopped, so no frame shows it - and its session
  saved, `capture.probe_layout` narrows the page the take ends on: its
  layout blocks (at least `_BLOCK_SHARE` of the viewport's width and
  `_BLOCK_HEIGHT` tall, within two screens, at most `_MAX_BLOCKS`) are marked
  at the recorded width, the viewport is narrowed by `LAYOUT_STEPS` (90 % to
  40 %, never under `LAYOUT_PROBE_FLOOR`, the recorder's own
  `spec.MIN_VIEWPORT_WIDTH`) and halved `LAYOUT_BISECTIONS` times between the
  last width kept and the first changed, each width compared to the recorded
  one (`capture.layout_change`: a block no longer shown, blocks side by side
  that stack, the page scrolling sideways), then the viewport is put back.
  `VideoTake.layout` keeps `{width, holds_down_to, changes_at, why}`; a probe
  that fails or outlasts `LAYOUT_PROBE_SECONDS` leaves it empty and never
  fails the take. The legibility advisory offers a narrower viewport only
  down to `holds_down_to`. Each read measures how far the document and its
  main container (`main`, `[role="main"]`) overflow their width (`spill`,
  `main_spill`): a page that overflows below a minimum width - the
  document scrolling sideways, or an app shell clipping a column its main
  container cuts - says that width exactly (`capture.min_width`: the width
  read plus what it cut off past `SPILL_TOLERANCE`, 2 px, read once more to
  see it holds), so the take's answer reads "Its page keeps this layout
  down to 1000 px".
- **A browser zoom the layout holds.** `browser_zoom: 190` on a
  1600x900 window lays the page out at 842 CSS px; a three-column dashboard
  whose minimum width is above that overflowed, its third column cut by the
  page's edge, and only the 4K frames showed it. Before the screencast
  starts, `capture.zoom_fit` reads the start page at the zoomed CSS
  viewport; one that overflows there more than it does unzoomed (read at
  the window, then put back: a page wider than any window is not the zoom's
  doing) refuses the take in one line - "Refused before recording: at zoom
  190 the page lays out at 842 px and overflows by 225 px; the largest zoom
  that keeps its layout is 150" (`ZoomFit.largest_zoom`: the largest zoom
  whose window over it holds the minimum width, within the tolerance). A
  zoom that keeps the layout reads the page once, never resizing it.

## Production: the spec and the template

A video project stores a `VideoSpec` (`spec.py`): a title, a format (aspect
ratio, fps), a theme (scheme, backdrop, watermark, optional colour and logo
overrides), scenes (`title`, `statement`, `recording`, `slide`, `figure`,
`clip`, `image`, `outro`) with narration, overlays (`lower_third`, `callout`, `badge`,
`caption`, `spotlight`, `image`, anchored to a recording step with `at_step`),
transitions, voice-over (with pronunciations), music (see "Music and the
mix") and captions. Every model is
`extra = "forbid"`: an invented field is a field-addressed error the agent
repairs. An edit (`projects.apply_operations`) that sets one field of a
one-of group (`spec.ONE_OF_FIELDS`: a clip scene's or a backdrop's
`clip_id` / `file_id`, the music's `track` / `music_id` / `file_id`, a
slide's `slide` / `slide_id`, an image overlay's `file_id` / `asset`) drops
its siblings in the same object, so switching a backdrop from a file
to a shot is one field. A model whose fields depend on its type drops the
ones its type does not take, on every write path: a `Transition` of
type `cut` keeps no `duration` or `pace`, a named move keeps its `duration`
only (`spec.transition_fields`), so setting `{type: cut}` over
`{type: auto, duration: 0.7}` leaves a plain cut; an `Overlay` drops the
picture's fields on a text overlay and `subtext` / `style` on a picture.

**Image overlays.** `{type: image, file_id | asset, position, width,
opacity, start, duration, at_step?, text?}` draws a picture - a logo, an
emblem, a product mark - on any scene type: a file (`file_id`, a reference
checked like any file) or a brand mark of the template by its role
(`asset`: `logo`, `logo_light`, `logo_mark`, `spec.BRAND_ASSETS`), never a
logo drawn by a video model. `width` is a share of the frame's width (up
to 1; `IMAGE_OVERLAY_WIDTH`, 0.12, when unset), else design pixels; `text`
is its label (what the quality check names it). The planner sizes it in
design pixels from its picture (`PlanMedia.overlays`, keyed by
`timeline.overlay_image_key`: `overlay/<file id>` or `overlay/<role>`) so
the page places it before it has loaded; `timeline.overlay_file_ids`
resolves a role through the palette as the cards and the watermark read it
(the mark falls back to the logo). The render downloads each picture, the
preview reads its size from the file's `image_meta` (read once and stored
when missing, `preview.overlay_picture_size`); one that cannot be read is
left out of the scene with a note. A step gives a picture its time, never
its element: it is placed like a badge.

**Screen inserts.** `{type: screen_insert, source: {file_id | take_id |
clip_id | asset}, quad: [[x, y] x 4], trim_start?, fit cover|contain, glow?, light?,
track?, key?, start, duration, at_step?}` maps a picture or a video onto a surface of
the scene's own footage - the monitor, billboard or poster a video model
drew - so the real product appears there, legible at the render's
resolution. Only a scene that plays footage takes one
(`spec.SCREEN_INSERT_SCENES`: a clip, a recording, a statement over a
backdrop, an image scene - a still of a room with its monitors is
footage as a statement's picture backdrop is, with the same quad, fit, light
and track rules: `timeline.insert_footage` is the one reading of what an
insert lands on, `timeline.footage_still` whether that is a picture - no
frame to follow, its matte read on the picture, no preview build -, the page
draws it on the still through a host that moves with the scene's motion,
warped at `timeline.STILL_HOST_ZOOM` as a backdrop's is, `render.host_size`
frames the still as `buildStill` does, the preview sizes its picture, and
the save measures its bezels on the still, or on its animation once made).
`quad` is the surface's corners in the footage, 0-1 across and
down it, top left, top right, bottom right, bottom left;
`spec.quad_problems` refuses a quad that is not convex and clockwise on
screen (a bow tie, another order) or encloses under `MIN_QUAD_AREA`, and a
corner may stand up to `QUAD_MARGIN` outside the footage (the quality check
says when it leaves the frame). The source is a reference of the video
(`referenced_ids`: checked on save, kept from the retention); `{file_id,
take_id, clip_id, asset}` is a one-of group. `asset` names the
template's own file of a brand role (`logo`: the logo the cards draw, the
video's own `theme.logo_file_id` when it names one; `logo_light`;
`logo_mark`: the bare mark), resolved by `timeline.brand_asset_files` -
`spec.brand_marks` with `stand_in=False`, so no role falls back on another,
where the cards and image overlays let the logo stand in: an insert naming
a mark the template lacks is never drawn with the logo, and fails its scene
(`insert_missing`: left out, the audience would see the mark the
video model drew). An SVG source (a template's logo) is drawn to a PNG
first (`screen_insert.svg_to_png`, the bounded rasteriser's worker): one it
draws nothing of raises `SvgNotDrawnError` with the rasteriser's reason
(`svg_raster.render_svg_async`), logged and carried by the scene's
`insert_missing` finding. A PNG keeps its alpha. `key`
(`spec.InsertKey`) is `{color: green | blue | magenta | #rrggbb, tolerance
(0.3), feather (1.5 px of a 1080p frame)}`. A named colour keys on the shade
of it the surface shows (`screen_insert.keyed_on`,
`screens.sample_key`): the median of the quad's pixels within
`FIND_HUE_DEGREES` (40) of the name's hue and saturated past
`FIND_MIN_SATURATION` (0.45) on the insert's first footage frame - a video
model's "chroma green" is about (32, 255, 30), never the (0, 177, 64) the
name stands for, and keyed on the name such a surface mattes about 0.6, the
footage blending under the insert. It is read once, by `_matte_inserts`
(and the preview build on its first frame), logged, and travels with the
insert (`KeySpec.rgb`, a scene range's bundle included); a surface showing
less than `SAMPLE_MIN_SHARE` of the quad of that colour keeps the name's
own, said. A `#rrggbb` is keyed as written.

- **A wall of panes is one insert.** `panes: N` (side by side) or
  `[rows, cols]` (`Overlay.grid`, at most `MAX_PANE_SIDE` a side and
  `MAX_PANES` in all) with `sources` - one per pane, row by row - instead of
  `source` (`{source, sources}` is a one-of group), and `gutter` (a share of
  the quad's width between columns and of its height between rows, default
  `DEFAULT_PANE_GUTTER` 0.015). `spec.pane_quads` splits the quad on the
  surface: each pane's corners are taken through the projective map of the
  unit square onto the quad (the one `quadMatrix` builds), so a wall seen at
  an angle keeps its panes in perspective. `Overlay.insert_panes` is the one
  reading of what an insert draws - `(pane, source, quad)`, or
  `(None, source, quad)` without panes -, read by the planner, the render,
  the preview, the gate and the quality check (`Overlay.shown_sources`).
- **Planned as part of the picture.** `timeline.insert_sources` keys each
  insert `insert/<scene id>.<overlay index>` (`insert_key`), a wall's pane
  `insert/<scene id>.<overlay index>.<pane>` (an `InsertPart`: its source,
  its overlay, its own quad), so each pane is drawn as an insert of its own
  and the page needs nothing new; the planner puts
  it in the scene's `inserts` (never its `overlays`, which the layout places
  and the quality audit measures), with a video's time map in scene seconds
  (from `trim_start`, holding its last frame). Only a caller that gathered
  the sources plans them (`PlanMedia.inserts_gathered`: a render, a
  preview). A render reads an insert's source as it reads a scene's own
  footage: a recording through `render._ready_take` and the file the
  footage uses (`render._take_file`), a shot through `_ready_clip`, a file
  that must still exist - in the scene's own preview and when it is drawn as
  a neighbour in another scene's preview alike - and fails, naming the
  scene and the source, on one it cannot read, where it used to leave the
  insert out with a note (the brand film's preview `3a9b2466` of `s03-stat`
  drew `s02-noise` without its insert). A neighbour's every source is read so:
  its insert's file, recording or shot, its backdrop, its clip - a
  deleted shot says it was deleted, never "still being generated" -, and the
  preview of the next scene fails with the reason the neighbour's own would
  give. A brand mark the template has no file for is missing the same
  in both, where it crashed both (`KeyError: 'insert_notes'`): an
  `insert_missing` finding of its scene, never a note. The editor's
  live preview still plans a source not made yet without it, said where it
  plays.
- **Warped frame by frame, in Python** (`screen_insert.py`). The page asks the
  frame server for `insert/<key>/<source frame>.png?w=&h=` at its host's
  on-screen size in device pixels (the camera's closest zoom on a
  recording) - the render's resolution, the AI footage upscaled by the page
  on its own, never the composited frame; the server decodes the source
  frame like a scene's, and `screen_insert.warp` fits the source to the
  quad's proportions (`pictures.fitted`: `cover` cuts, `contain` keeps it
  whole on transparency - the surface shows around it, never a black card;
  on a keyed surface `compose` fills those bands under the source,
  below), brings it to the size it is drawn at (LANCZOS, reduced or
  enlarged, within `RESIZE_TOLERANCE`), and samples it premultiplied
  (`RGBa`) inside a transparent `EDGE_PAD` through the homography of
  `pictures.perspective_coefficients`: antialiased edges, no dark fringe.
  Pillow's perspective transform reads bilinear whatever filter it is
  asked, which blurred every one-pixel stroke by half a pixel (the
  insert came out soft); the warp is now drawn at `SUPERSAMPLE` (2) times
  the box's size and brought to it once with LANCZOS while the box fits
  `SUPERSAMPLE_BUDGET` pixels (a 4K box of 2000 x 2000 does, a 4K frame
  whole does not) - measured as Laplacian variance against the source at the
  drawn size, 0.73-1.16 of it where the bilinear read gave 0.11-0.57. The
  answer covers the quad's bounding box (`screen_insert.bbox`), which the
  page places with the same `floor` / `ceil` arithmetic (`insertBox`); a
  matted insert's covers the quad grown by `BOX_MARGIN` of the frame's width
  (`screen_insert.grown_box`) - room for the bezel lock and the key
  margin - which `render._matte_inserts` puts on the payload (`box`) and the
  source (`InsertSource.box`, carried to a range worker's bundle), so page
  and driver place every frame on one box. `glow` adds a bloom: what is drawn of the insert - its
  alpha silhouette, matted, so a logo halos its emblem and its wordmark and
  never the quad's rectangle - at 1/`GLOW_SCALE` over the whole host,
  blurred, drawn under the sharp frame with a `screen` blend at the
  overlay's `glow`. The bloom is one colour: the insert's mean
  colour (`screen_insert.mean_colour`, over the pixels it draws) at
  `GLOW_ALPHA` at its brightest, never the blurred source itself, which read
  as a yellow-green halo on dark footage; and it is clipped to the bezel -
  the quad grown by `GLOW_REACH` (2 %) of its width, feathered - so it never
  falls on the wall around the screen. A keyed insert glows only when asked
  (`glow` defaults to 0, and the skill pairs no glow with a key). A keyed
  layer covers its quad whole (the despilled
  surface where the source is transparent), which hid that glow on the
  surface and left it only past the quad's edges: `compose` lays its light
  on the despilled surface in the layer itself
  (`screen_insert._halo`, the page's own blend), so a logo on a keyed
  billboard halos its mark. Each page keeps its last frame (the frame and its glow
  are asked one after the other), counts a video insert among its decoders
  (`decoder_budget`) and releases it with its scene.
- **The footage's light over it.** `light` (0-1, unset
  `spec.INSERT_DEFAULT_LIGHT`, 0.15; `timeline.insert_light`, on the payload,
  the source - carried to a range worker's bundle - and the editor's build,
  `insert_builds.BUILD_VERSION` 3) is how much of the footage's ambient light
  and reflections play over the insert, the same control as the screen
  replacement's `screen.light`. `screen_insert.ambient_light` reads the
  footage's luma over the surface the insert covers (a keyed surface's key
  pixels clear of the foreground, else what the occlusion leaves showing),
  blurred at `LIGHT_SIGMA` (8 %) of the quad's short side by a normalised
  blur on a grid at most `LIGHT_GRID` long - the bezel and the foreground
  lend it nothing - and takes it above its `LIGHT_FLOOR_PERCENTILE` (5th)
  over the surface: the light that varies over it, a reflection, a lamp's
  falloff, a window's glare. `lay_light` screen-blends it over the source at
  `light`: light added, the same on every channel and never past white. Luma
  alone, so a surface's colour - the key's green, a room's blue, a screen the
  model drew - never casts on the insert's blacks, and a surface lit evenly
  lays nothing whatever its colour or brightness; the glow keeps the
  insert's own (unlit) colour. The colour gate below holds what it lifts.
- **A camera push on it.** `zoom: {start, end, scale, focus}`
  (`spec.InsertZoom`: seconds after the insert comes in, `end` within its
  `duration`; `scale` over 1 and at most `MAX_INSERT_ZOOM`, 4; `focus` 0-1
  of the source, the centre by default) draws the source larger within its
  quad, panned to a region - what the legibility advisory offers instead of
  a narrower viewport, the page's own layout drawn larger. One reading,
  `screen_insert.Zoom`: `scale_at(seconds)` eases from 1 to `scale`
  (smoothstep) between `start` and `end` and holds; `window(size, scale)` is
  the part of the source shown, centred on `focus` and kept inside the
  source. `timeline.insert_zoom` puts it on the payload (so it is in the
  segment digests' `placed`, and `compositor._insert_inputs` carries it),
  `render._gather_inserts` on the `InsertSource` (a range worker's bundle
  too, `render_ranges._manifest`). The page asks a pushed insert with `&o=`
  - milliseconds after it came in, held at the push's ends, so a still
  before and after the push is asked once - and the frame server cuts the
  source to that window (`screen_insert.zoomed`: an `EXTENT` transform at a
  sub-pixel window, at the window's own pixels) before `compose` fits it,
  its last frame keyed by the scale too: the quad, the matte and every gate
  read the picture drawn, as without a push. The editor's build draws it the
  same way per frame (`InsertBuild.zoom`, in its digest only when set), the
  stand-in mapping with the page's `zoomTransform` (the same easing and
  window). The soft gate (`insert_checks.soft_before`) and the render's own
  measure (`render._insert_density`) judge the source at the push's peak,
  drawn as on a quad `scale` times larger ("pushed in 3x by its zoom"); the
  save and a scene preview read the same gate at every render quality
  (`insert_resolution`), and the legibility advice offers only a push
  the source carries at the quality the video's renders are made at
  (`render_checks.target_quality`, below). The render's own soft record
  advises a pixel ratio from the ratio the recording was made at
  (`spec.recorded_ratio`, `_Media.insert_ratios`), never the ratio a browser
  zoom laid its page out at.
- **Matted on the footage under it (`screen_insert.compose`).** What
  passes in front of the surface stays in front. `render._matte_inserts`
  tells each insert the footage it lies on (`InsertSource.footage`: the
  scene's frames, or a still backdrop) and the footage frame it comes in on
  (`first_frame`); the payload gets `matte: {fps, frames}` (`{still: true}`
  over a picture), and the page names the footage frame under the insert
  (`&f=`, the frame its own footage shows) and the output frame it draws
  (`&n=`). The server decodes that footage frame with the scene's own
  decoder (`FrameServer._footage_frame`) - once a frame, every matted insert
  of the page reading the same pixels - and draws through `compose`:
  - **the quad snapped to its bezel first (`surface_refine`).** The
    given quad - the spec's, or the tracker's for the frame - is a seed: on
    every footage frame each of its edges is looked for in a band `BAND`
    (3 %) of the frame's width either side of it, the field read along the
    edge's normal at `SAMPLES_PER_EDGE` points (a keyed surface's matte, else
    the footage's luma, an unkeyed surface's occluder giving no edge); of
    the gradient peaks within `PEAK_SHARE` of the strongest, the one with
    the surface on its inward side wins (the inner edge of a bezel over its
    outer one, whichever the seed is nearer to); each edge's points are
    fitted to the line most of them agree on (every pair proposes one, a
    hand or a person across the edge agrees with none) and the lines
    intersected. An edge not found keeps the seed's line, so a surface partly
    out of frame keeps its true corners outside it. `surface_refine.Refiner`
    (one per insert and page, in `screen_insert.Matting`) carries the
    correction to the next frame as its seed - a static quad on a moving
    camera follows its bezel - and tries the given quad again every
    `RESEED_EVERY` frames; the alignment error it reports is the GIVEN quad's
    against the bezel (mean over the corners whose two edges were found, a
    share of the frame's width), the finding's number. The insert is drawn
    `EDGE_GROW_PX` past the edge found (mid-ramp), so no rim of the key stays
    along the bezel. A keyed insert that is not tracked is locked ONCE, on
    its first footage frame (below): the same quad on every frame.
  - **`quad: auto`, the bezel as the input.** Copying the refined
    quad of a failed preview back by hand is gone: a static insert may leave
    its quad to the render (`spec.AUTO_QUAD`, `Overlay.auto_quad`; refused
    with `track: true` and on two inserts of one scene). Until found it
    draws nothing (`insert_panes` is empty: the quality check, the gate's
    soft check, the editor's preview and its builds skip it, the plan says
    so), while its sources still count as what the scene shows
    (`shown_sources`: the gate waits for them). The render gathers and plans
    it on a stand-in, the whole footage (`timeline.with_quads(stand_in=True)`;
    the pre-flight's `InsertLoad.of_spec` counts it so too), then
    `render._find_quads` reads the footage frame it comes in on (as
    `_matte_inserts` reads a surface's reference) and takes
    `surface_refine.found_quad` there: the flat surface of the key's colour
    the frame shows (`screens.find_screen(candidates=...)`; green, blue or
    magenta without a key) snapped to its edges by `refine`, on the key's
    matte or on the luma. The quad found is written into the spec drawn
    (`timeline.with_quads`), the plan made again (no frame of the timeline
    moves), and the render's note and the insert's report
    (`checks.inserts[].auto_quad`, its `Insert` line) say it; the alignment
    measured frame by frame is then against it. No such surface: the insert
    is not drawn, an `insert_missing` finding (`render.NO_BEZEL`) - the one
    way it fails.
  - **The bezel measured at save (`insert_bezels`).** A static
    insert's footage frame is known as soon as its footage is made, so the
    save measures it rather than a preview: a static insert with no quad IS
    `quad: auto` (`Overlay.auto_quad`; a tracked one still needs the quad it
    is followed from). `insert_bezels.measure` plans the video with every
    insert placed on a stand-in (`timeline.estimate_plan(place_inserts=True,
    backdrops=...)`: a recording's, a shot's numbers from its row, an
    upload's probed once and kept by file), reads the footage frame the
    insert comes in on (`map_forward` of its `t0`) and takes
    `surface_refine.measure_bezel` there: the edges near a quad given, else
    the flat key-coloured surface the frame shows. `resolve` writes what that
    settles into the spec stored - a quad left out takes the bezel (said in a
    note; `get_video` shows it), a quad within `SNAP_MAX_SHARE` (2% of the
    frame's width) is snapped to it - and refuses the save
    (`BezelRefusedError`, a `VideoSpecError` naming `scenes.N.overlays.M.quad`)
    on a frame with no bezel, its frame attached by the tool, or a quad
    further off that does not stand inside the surface (an insert drawn on
    part of a screen on purpose). A measure is kept per footage file, second,
    quad given and key (`render_cache`, `CACHE_KIND`, the frame beside it):
    a save that changes nothing under it reads it back, downloading nothing.
    Footage not made yet is not measured; the pre-render lint measures it
    (`insert_bezels.settle`, below). Never a failed save for a measure that
    cannot be made.
  - **Flat.** Each edge's points also give how
    far it bows (`surface_refine._bow`: a parabola across them,
    `screens.edge_parabola`, its sagitta over the edge), how many of them lie
    on the line most agree on (within `surface_refine.inlier_px`: 1.25 px up
    to a 1920 px frame, in proportion past it, so a 2160p frame's wider blur
    agrees as a 1080p one's does) and how far they stand off one straight
    line (`surface_refine.EdgeFit`: `inliers`, `residual`). An edge
    is read only on the readings that cross nothing standing in front of the
    surface (`screen_insert.edge_front`: the foreground as every gate reads
    it, `_foreground` on the key's matte, within the surface's own outline -
    `screen_insert.surface_outline`, the convex hull of its key regions, so
    the bezel inside a seed off it, the wall between a straight seed and a
    curved bezel, a key colour past the bezel are never taken for a subject -
    and the lock's search band, grown by `FOREGROUND_GROW_PX` and the key's
    feather); the share of its readings left is its visibility
    (`Refined.visible`, `EdgeFit.visible`, the audit's `edge_visible`), and
    under `MIN_EDGE_VISIBLE` (0.4) the edge is mostly covered: its line is the
    seed's and its bow is not judged on that frame, said as such
    (`Flatness.covered_words`, "right edge mostly covered (34% visible)", the
    save's `insert_bezels.covered_note`, the report's `flatness.covered`). A
    bow is confirmed only by all three together, on the visible part - past
    `FLAT_MAX_SHARE` (5% of the edge's length), under
    `BOW_MAX_INLIERS` (0.7) of its points on one line AND a median residual
    past `ALIGNMENT_MAX_SHARE` (0.5 % of the frame's width) -, never by one
    alone: a bow most points deny is a line of what the screen shows caught on
    a few of them, a low inlier share alone a silhouette crossing a straight
    edge (`surface_refine.Flatness.confirmed`; a subject at the bezel read as
    "right edge bows 10%, 251 frames" on a quad seated 0.28 % off that edge
    whose save had passed). A quad that is not convex is
    no planar surface either (`Flatness.convex`, `Flatness.fault`). Opposite
    sides of unequal length are a flat surface seen at an angle, which the
    eight-coefficient perspective warp maps onto exactly: a fact, said in
    degrees (`surface_refine.viewing_angle`: the angle between the surface's
    normal and the line of sight to its middle, the focal length from the
    quad's two vanishing points, else `ASSUMED_FOCAL`; `Flatness.seen_at`,
    "seen at 35 degrees" past `ANGLE_FACT_DEGREES`; a record without degrees
    by its sides past `ANGLE_FACT_SHARE`, `Flatness.angle`), in the save's
    notes (`insert_bezels.angle_note`) and as `by design` in a render's insert
    lines, never a fault (inserts 14-27% off square seated at
    0.00-0.04% of the frame's width over 240-275 frames). Measured only where
    a screen insert lands. On a shot's start picture when it is
    planned (`clips.lint_surface`), for a screen replacement, a call asking
    `insert: true`, or a shot generated again that a video's scene draws a
    `screen_insert` on (`clips.drawn_under_insert`, read off
    `retention.uses_media` and `spec.insert_footage` only when the picture
    shows a surface): a curved one refused before anything is paid;
    `validate_only` answers its bezel and flatness,
    `ClipPlan.summary["surface"]`; any other shot validates, its notes saying
    nothing was checked. On the take as made, before a screenshot is drawn
    into it, of a shot that carries an insert (`clips.carries_insert`,
    `clips.take_surface`, `frame_qc.SURFACE`: a take that bends it fails, the
    next fallback model may keep it flat). Both read the luma, nothing left
    out: on F9-F11 they give the save's and the render's verdict (the
    fixtures fail CI otherwise), but a subject placed so that its silhouette
    bows the luma edge past all three readings (a ball over half of one edge
    of a flat panel: 9%, 0.625 of the points on one line, 0.59 % residual)
    is refused there while the render, keyed, passes it. Keying them through
    `edge_front` would trade that for a worse fault: `edge_front` takes the
    surface's outline as the convex hull of its key, so the wall between
    the chord and an edge that bows inward - a curved monitor seen from the
    front - reads as standing in front, the edge "mostly covered" and never
    judged (a 7.5% inward bow keyed passes as flat, `covered: [top,
    bottom]`). Telling an inward edge from what stands in front is
    `edge_front`'s to solve, for the save and the render first. On the bezel a static insert lands
    on (`insert_bezels.findings`, `insert_flatness`: "Regenerate the shot ...
    with a flat screen" first). `insert: false` (`clips.INSERT`, kept by the
    shot's regenerations) says the shot carries none: no screen is checked on
    its start picture or its take, `stray_screens` is dropped, and a `screen`
    asked with it is refused. A record without inliers reads
    back through the same rule: its side ratio is no fault, and its bow, which
    no other reading confirms, is not judged (never one reading alone)
    (`insert_bezels.MEASURE_VERSION` 4 measures again: keyed and
    masked as the render's lock reads a frame - the key's shade sampled, its
    tolerance widened to the surface's spread, `edge_front` left out of the
    edges -, so the save and the render read one thing; its verdicts,
    `insert_bezels.verdicts`, are what the render holds its own findings to,
    below). `MEASURE_VERSION` 5 (the insert's `despill` in the
    measure's digest): keyed, the same measure reads the key's spill on the
    edge of what stands in front on that frame (`Bezel.spill`,
    `screen_insert.edge_spill`, an `EdgeSpill`: the share of the subject's
    side of the halo's band holding the key's hue or its complement, the
    footage's own hue left out, and what the layer's clamp and edge
    treatment at the insert's despill leave of it), so the shot's own lint
    keeps it with its surface record and the save says it past
    `insert_bezels.SPILL_NOTE_SHARE` (5 %, the halo gate's own), before any
    preview (`insert_bezels.spill_note`: "what stands in front of the surface
    under its screen insert 0 carries the key's hue on 34% of its edge (812
    px) on frame 0 (0.00 s) of its footage: expected edge spill; the edge
    despill (width 2 px) leaves 0.0% of it" - past the gate, raise
    `despill.width_px` before a preview; off, the halo gate reads it as it
    is). `MEASURE_VERSION` 6 (the span the insert plays in the
    measure's digest): keyed, the save reads its footage once more, from
    the frame it comes in on to the frame it goes out on
    (`compositor.read_subject`: `screen_insert.SUBJECT_FRAMES` frames at
    most, `SUBJECT_WIDTH` wide), for what stands in front of its surface over
    the clip (`screen_insert.subject_of`, a `Subject`: its hue histogram,
    `OWN_HUE_BIN_DEGREES` bins, the despill's reach in from its edge left
    out) - on a still shot what of it goes on past the quad too, joined to
    it and off the clip's own median background, so a muzzle whose tip
    alone crosses the quad is read whole -, kept with the measure
    (`Measured.subject`) and said when it holds the key's hue or its
    complement on `OWN_HUE_MIN_SHARE` (2 %) of its pixels
    (`insert_bezels.subject_note`: "holds red - the key's complement - on
    3.1% of its pixels over its clip ... its own colour, which the edge
    despill keeps and the halo gate never counts"). The render takes it for
    the insert and every pane of it (`insert_bezels.subjects`,
    `render._subject_of`), reads it itself where the save did not (a
    tracked insert: the quad's foreground alone), and so does an editor
    build; it travels in a range's bundle and in the insert's segment
    digest part. On the frames a render draws, every audit carries its edges'
    bows, inliers, residuals and visibility (`FrameAudit.edge_bows`,
    `edge_inliers`, `edge_residuals`, `edge_visible`): a confirmed bow on
    `surface_refine.PERSIST_FRAMES` (5) frames in a row (`persistent`, or
    every frame of an insert drawn on fewer) is the render's own
    `insert_flatness` (a tracked insert's too, which no save measures), its
    quad off the bezel and the key left past its curved edge folded into it
    (`data.follows`), said with the three readings that confirm it and the
    edges not judged (`data.edges`, `data.covered`); `insert_alignment` on a
    curved bezel says the same first (`insert_checks._alignment_remedy`). On
    a static keyed insert, what already stands in front on the frame its
    fill is read on stays in front (`Fill.front`: the occlusion holds
    every later frame to that one and never saw it, so a ball standing at
    the bezel from the first frame was drawn under the insert).
  - **Measured once, covered or not.** A 1080p preview found a surface
    flat and the 4K render of the same footage found it curved (0.99 % of the
    frame's width off a line), asking for a reshoot of an insert drawn 0.03 %
    off its bezel with no residual key: the render read the bow again on its
    own frames, the footage brought up to 2160p first. Two rules now:
    - **One measure.** A static insert's flatness is the save's, on the
      footage frame at the footage's own resolution
      (`insert_bezels.measured_surfaces`, the render's *measured*): its
      previews and its renders take it (`insert_checks.saved_measure`, a wall's
      pane its overlay's) and never judge their frames' bows
      (`gate_frames(saved=)`, `verdicts`, `_alignment_remedy`). A surface the
      save measured curved is the render's `insert_flatness` as the save
      measured it (`data.measured: "save"`), whatever its frames read; one it
      measured flat is no finding, not even a disagreement. The report says the
      save's measure (`flatness.measured: "at save, on frame 12 (0.50 s) of
      its footage"`). A tracked insert, which no save measures, keeps its
      frames' reading.
    - **A bow the insert covers is a fact.** On its bezel within
      `ALIGNMENT_MAX_SHARE` (the mean or a side, `insert_checks.off_bezel`) and
      no key left past `RESIDUAL_FAIL_SHARE`, both read
      (`insert_checks.covers_surface`), the insert hides the bow: the compositor
      keeps the wall past a curved corner in front and fills the key within
      the residual margin, so nothing of the surface shows. A frame so drawn
      never fails the flatness gate (`_curved`); the report counts it
      (`frames_covered`, `covered_by_insert`) and the answer says it as a fact.
      The save reads the same on its own frame: on a curved convex keyed
      surface it draws the insert once (`screen_insert.drawn_on`:
      `static_fill` + `compose`, a flat stand-in source, on the quad the save
      stores, `insert_bezels.drawn_quad`), keeps the audit's alignment and
      residual key with the measure (`Measured.drawn`, `MEASURE_VERSION` 7),
      and a bow it covers is a note (`insert_bezels.bow_note`), never a finding
      (`bow_covered`; `flat_problem` and `verdicts` pass it). An unkeyed
      insert reads no residual key, so nothing says it covers a bow; a folded
      quad never does. Measured on a 640 px edge of a 1280x720 frame: a 40 px
      outward bow (6 %) is covered, a 48 px one (8 %) leaves 0.9 % of the quad
      showing the key and stays a finding, as F2, F10 and F11 do.
  - **One reading per size, on a join.** A statement previewed at
    draft (1280x720) read the insert of the clip after it on their join
    1.24 % off its bezel (its top side 2.25 %) and filed `insert_alignment`
    on every draft preview, asking for `quad auto`, while the save, on its
    footage frame, and the clip's own 1080p preview read it 0.02 % off: the
    bezel finder read the footage downscaled and blended with the statement.
    `insert_checks.bezel_readings` settles the readings of the join frames
    before the findings and the reports read them
    (`BEZEL_READING_FIELDS` set aside: the frame then reads as one where no
    bezel was found):
    - **A neighbour's insert is read at its own size.** In a scene preview,
      an insert of another scene is read on its joins only at the size it
      was last measured at: its own scene's drawing as it is now
      (`render_checks.insert_measures` keeps `alignment`, the report's
      `alignment_max`, and `measured_at`; `insert_verdicts` hands them on),
      else its save (`insert_bezels.verdicts`: `alignment`, the bezel's
      `error` for a quad given, `measured_at`, the footage frame's size kept
      with the measure as `Measured.size`, `MEASURE_VERSION` 8, and
      `where`) - `insert_checks.stored_bezel`, the first of its
      `passed_records` that says both. Drawn at another size, its readings
      there are set aside and that measure is one note, a fact of the
      answer ("Scene s02: its screen insert 0, drawn on this preview's join
      with scene s02 at 1280x720: 0.02% of the frame's width off its bezel
      (mean over its corners), as save revision 9 measured it on frame 0
      (0.00 s) of its footage at 1920x1080 - not measured again at another
      size: a fact, not a finding."), its report `bezel_stored`, never a
      finding. At that same size it is read again, as before; an insert
      nothing measured before (tracked, never previewed) is read too.
    - **The save's measure over a smaller frame's.** A join frame drawn
      smaller than the footage frame the save read (fewer pixels than
      `Measured.size`), whose reading (`off_bezel`: the mean or a side) is
      more than `ALIGNMENT_MAX_SHARE` off the save's `error`, is the
      lower-resolution reading: set aside for the save's, its report
      `bezel_discarded` (`{frames, worst}`, said as a fact), and the
      disagreement a line of `checks.infra` - logged, said under the
      answer's `infra_once`, never filed against the video. A frame at the
      save's size or more reads the footage whole: it stands, and a finding
      of it that the save passed disagrees with it as before. Only the
      insert's own save measure is held so, never a pane's overlay's.
  - **The bezel read on every frame against the lock.** A static
    insert's quad is locked once, on its first footage frame
    (`screen_insert.static_lock`, a keyed one's `static_fill`), and drawn on
    that quad on every frame - so the alignment the audit kept was the first
    frame's on all of them. A locked-off Veo 3.1 take whose camera drifted
    about 11 % over its 6 s (stabilised to 82 % of the frame) placed with a
    fixed quad passed every gate at 0.00 % while the preview's last crop
    showed the monitor some 150 px left of the insert; only with `track:
    true` did the gate see it (4.12 % off, 113 frames), two previews later.
    `compose` now reads the bezel the frame shows against the lock on every
    matted frame of a locked or filled insert (`screen_insert._drift_of`: a
    `surface_refine.Refiner` of its own on `Matting.drift`, stepped with no
    prior from the lock as the seed, the bezel found carried to the next
    frame's seed, the luma or the key's matte as the lock read it, what
    stands in front left out; `DRAWING_VERSION` 9, about 7 % of a frame's
    cost) and keeps it on the audit, never drawn: `alignment_drift` (the
    lock to that bezel, mean over the corners placed, a share of the frame's
    width), `drift_sides`, `bezel_found`. The gate reads it with the lock's
    own error (`insert_checks._alignment_of`: the worst of the three), so a
    bezel that moves from under the quad past `ALIGNMENT_MAX_SHARE` on
    `PERSIST_FRAMES` frames is `insert_alignment` on the frame it moved
    farthest, its sides the drift's, its one action "track it (track:
    true), or generate the shot again locked off" (`_alignment_remedy`, the
    frame's `_drifted`); the report keeps the worst drift, its frame and
    the bezel found there (`drift_max`, `drift_at`, `drift_sides`,
    `bezel_found`) and the answer says it as a fact or a finding ("bezel
    read on every frame against the lock: it moves at most 3.44% of the
    frame's width from under the quad at 0:00.46"). A still camera reads
    the lock itself on every frame (0.00 %); a tracked insert, whose bezel
    is found again frame by frame, carries no drift field.
  - **A screen, not a mug.** Without a quad, the candidate is the
    largest region of a key's hue (`screens.find_screen`) that is also a
    screen (`screens._not_a_screen`): four straight edges
    (`edge_straightness`, the edge's chord over the length of the curve its
    outline follows, at least `MIN_EDGE_STRAIGHTNESS` 0.97 - a screen bowing
    up to about 10% stays one, the flatness lint says it bows), a fill of the
    key's hue or dark (`DARK_FILL_LUMA`), and never the picture's brightest
    region (a fill past `BRIGHT_FILL_LUMA`, above any saturated key, and the
    rest of the picture's `BRIGHT_PERCENTILE`): a mug, a bottle, a lamp of a
    key's hue is refused, the refusal saying why.
  - **The fix, never the number.** No finding asks for corners to
    type back: `insert_alignment` says to leave the quad to the save (off from
    the insert's first frame), `track: true` (the surface moving under it
    later) or a regeneration (a surface not flat); a residual key on the
    surface running past the quad says to leave the quad to the save.
  - with a `key` and `track: true`, the key colour AND the surface's spread
    are read off the insert's first footage frame (`screens.key_spread`: the
    97th percentile of the surface's `key_distance`, widening the matte's
    tolerance to `SPREAD_FACTOR` times it, at most `MAX_SPREAD_TOLERANCE` -
    never lowering the tolerance the author gave, which used to be clamped
    to it; a `#rrggbb` keeps its colour and takes the spread too), so a surface under
    uneven light is one surface; the matte (`screens.key_matte`) is read per
    frame and smoothed as the lower median of the `MATTE_WINDOW` (5) footage
    frames centred on the one shown - the two before, read when they were
    drawn, the two after read ahead (`compositor.MATTE_LOOKAHEAD`: the frame
    server decodes them first and keeps them for the page, `_SceneDecoder`'s
    `RECENT_FRAMES`), so the encoder's noise at the tolerance boundary never
    flickers and a person walking in never lags (a centred window; anything
    thinner than three frames of motion is smoothed away); the foreground is
    the non-key regions that reach the surface's rim or hold
    `FOREGROUND_MIN_SHARE` of it, reaching deeper than the edge's blur
    (`_foreground`); a region closed inside the surface is what the model
    drew on it. The surface is **painted flat**: inside the refined quad and
    clear of the foreground a pixel is all surface (the insert covers it
    whole, no lit or half-keyed pixel of the footage shows through, where a
    half-way matte used to blend the footage under the insert), at
    the foreground's feathered edge the key's own share. What is not the key
    in a pixel is unmixed by the linear colour-difference share
    (`screens.key_share`, never the matte, which saturates long before a
    pixel is all key and read as a share takes too much out - past black,
    back as the key's complement, the magenta an inverting despill leaves),
    brought back to its own colour and despilled by the clamp
    (`screens.despill_clamp`: the key channel clamped to the max of the other
    two, a secondary key's weak channel lifted to the min of the two), within
    the quad and `KEY_MARGIN` (2 %) around it - where the key's bleed on the
    bezel is taken out, and a pixel keyed past `MARGIN_BLEED_MATTE` is one of
    two things (`screen_insert.band_of`, a `Band`): joined to the
    surface inside the quad (a connected region of the matte past
    `BAND_KEY_MATTE` reaching the ring just inside the quad's edge) it is the
    surface running past a quad too small, left for the residual-key gate;
    parted from it by the bezel it is the footage's own key colour past the
    bezel - a keyed panel the model drew larger than the bezel the quad is
    snapped to, which no quad can cover without leaving the bezel - keyed at
    the insert's tolerance, feathered outward by its feather and filled with
    the bezel's colour on that side (`screen_insert.bezel_sides`: the
    median of the pixels within `RESIDUAL_MARGIN` of the quad on that side
    that are not the key - a bezel the surface's light tints included -, its
    spill clamped out; `screen_insert._sides`, the quad's nearest edge). Left
    as it was, such a panel failed the take on pixels no setting could change
    (20 % of the quad on synthetic footage, every side). The layer carries
    the whole surface and that margin. Within `RESIDUAL_MARGIN` (0.5 % of the
    frame's width) of the quad, the key's hue the layer still shows takes the
    same bezel colour (`screen_insert.fill_margin`: the bezel the quad
    is snapped to, never a grey), but for the surface running past a quad too
    small - a region of `Band.edge` reaching past that margin
    (`screen_insert.running_past`) -, left for the residual-key gate. The
    bezel lock takes the nearest of the edges that read as the surface's own
    within `surface_refine.PEAK_EQUAL` of the best, so a keyed
    panel's green running past a thin bezel never moves the lock off the
    screen's edge. Past `RESIDUAL_MARGIN` from the quad, whatever of the key's
    hue the layer still shows there - the footage's own screen glow on the
    rim, a spill on the desk - is despilled on every frame
    (`screen_insert._despill_margin`): a
    pixel of the key's hue saturated past `DESPILL_MIN_SATURATION` brought
    toward the luminance of the margin's pixels of another hue around it
    (fully from `RESIDUAL_MIN_SATURATION`), never clamped to black; what the
    layer took out of the margin this way, by the clamp or by the band's fill
    - the margin pixels of the key's hue in the footage that no longer are in
    the layer - is audited (`despilled`, `despilled_where` by side) and
    reported, never a finding. How much of the
    matte as drawn holds from the frame before is audited: one less
    the share of its pixels (past a half) that changed into the frame before
    and changed back into this one (`screens.edge_hold`, `Matting.hold`,
    `FrameAudit.edge_hold`) - what a median of five cannot settle, an edge
    read in and out on every other frame, alternates with it, while a person
    crossing the surface changes a new band of pixels on every frame and
    never the same ones back, so they hold however fast they walk. Under
    `screens.MIN_EDGE_HOLD` (0.98) the key flickers (`insert_flicker`,
    below, beside the coverage and luma reversals);
  - with a `key` and no `track`, nothing is keyed frame by frame: a
    key read against compressed footage drifts, and a matte - or a bezel -
    recomputed on every frame blinked at its edge. The quad is filled
    instead (`screen_insert.static_fill`, a `screen_insert.Fill` read once
    per host size off the first footage frame by the frame server and the
    preview build, `FrameServer._fills`): the bezel locked on that frame
    (`screen_insert._locked`, the same step as above, once), the surface as
    that frame shows it with the key's spill clamped out
    (`screens.despill_clamp`, the layer's `flat`), the source drawn over it
    on every frame - the same pixels every time, so nothing can blink. What
    passes in front of it still stays in front through the occlusion every
    unkeyed insert has (next point: what differs from the first frame and
    enters through the rim), which reads no key colour, pooled over the same
    window, its edge feathered by the key's `feather` (the fill read
    neither the feather nor the tolerance past the bezel lock, so a key-only
    revision drew byte-identical frames); where it stands, the layer carries
    the frame's own pixels with the key's spill clamped out. The margin is
    read once on the same frame too (`Fill.band`, the `Band` above): the
    footage's key colour past the bezel filled with the bezel's colour where
    nothing stands in front, the surface past the quad left for the gate. The
    fill is read on the box `compose` draws on - the insert's own box
    (`InsertSource.box`), else the quad grown -, so the two cannot disagree by
    a pixel and drop the insert back to the per-frame key;
  - **the contain bands and the edge of what stands in front, keyed.
    ** The clamp despill turns a key the model painted off the
    primary - a green leaning to cyan, hue 150 - into a colour still of the
    key's hue to the gates (cyan, 180, within `HALO_HUE_DEGREES`), and no
    tolerance or feather reaches it: a contain fit's bands, which showed that
    clamped surface, failed `insert_residual_key` "inside the quad along its
    bottom" at the same share at tolerance 0.4, 0.6 and 0.88, and the soft
    edge of hair crossing the screen failed `insert_halo` at 27-29 % at
    feather 3 and at feather 10 on synthetic footage. Both keyed paths now fill the bands under the
    source (`screen_insert.contained_quad`: the picture's rectangle through
    the quad's perspective, as `pictures.fitted` centres it;
    `band_backdrop`): with the source's own colour along the edges they lie
    against when those are opaque and one flat colour (`EDGE_FLAT_SPREAD`) -
    the source then runs to the bezel -, else with the bezel's colour
    (`bezel_colour`, the margin's non-key pixels nearest the quad, clamped;
    read once on the first frame for a static insert, `Fill.bezel`, per frame
    for a tracked one) when its luma is under `MAX_BACKDROP_LUMA`, else
    black; never a colour of the key's hue; the footage's light laid over the
    fill as over the source. A source whose edges are transparent - a mark on
    transparency - keeps the keyed surface around it, its board, never a
    card. On the edge of what stands in front, within
    `EDGE_DESPILL_PX` (plus twice the feather) of the foreground's boundary,
    the edge despill (`edge_despill`) gives a footage pixel the clamp left on
    the key's hue or its complement the colour of its clean neighbours in
    front (a normalised blur of the foreground's pixels of neither hue, or of
    the subject's own), or a grey at its luminance - unless that colour is
    the subject's own (`edge_of`, `Edge.owned`): a hue holding
    `OWN_HUE_MIN_SHARE` (2 %) of the subject's pixels over its clip (its
    `Subject`, above) or one the author declares (`despill.own_hues`: hue
    names of `spec.HUE_NAMES` - a span of the wheel - or `#rrggbb`, its hue
    `OWN_HUE_DECLARED_DEGREES` either side); the key's hue the raw footage
    shows that runs on into the subject - `RUN_ON_SHARE` of its interior
    around it, past twice the reach (a thinner part's own ridge), holding
    that hue no less saturated by `SPILL_FALLOFF`: spill is an edge
    gradient, falling off into the subject, a colour of its own runs on (a
    green collar, a green-lit person); and the complement wherever the
    compositor's clamp did not touch the pixel (moved by `CLAMP_MOVED`
    grey levels on the clamped colours), or where it did and the hue runs
    on in the colours read - the inverted despill's magenta is the clamp's,
    never the footage's, and the subject holding that hue over its clip
    never excuses it (a declared hue does). Previously a hue was the
    footage's own only where it held `OWN_HUE_SHARE` (0.5) of the subject's
    interior around the pixel on that frame: a muzzle's tip crossing the
    quad, motion-blurred, never did: its pink was read as spill and failed
    the halo at 7-26 % on synthetic footage (F18). The halo gate leaves those
    pixels out of the edge it measures and says how much it left out; it is
    not judged against the compositing matte, so a bad matte cannot excuse
    its own fringe;
  - **the key's spill on the edge of what stands in front.** A
    subject in front of the surface carries its colour on its own edge in the
    footage - fur, hair, a sleeve - which the matte keeps in front. The clamp
    limits a key by its channels (`screens._key_channels`): a key a model
    painted between a primary and a secondary - the sampled `(39, 199, 129)`,
    hue 154 - reads as two-channelled like magenta, so its weak channel (red)
    is lifted and the edge stays green (`(66, 169, 108)` comes out
    `(108, 169, 108)`), just past the edge despill's hue test until the insert
    blends with it: 20-40 % of a furry subject's edge on synthetic footage,
    no tolerance or feather moving it. Before that pass, `edge_limit` takes
    the key's colour out of that edge - within the insert's
    `despill.width_px` (`spec.InsertDespill`, 2 px of a 1080p frame by
    default) and the key's feather of the boundary against the surface
    inside the quad (`screen_insert._against_surface`, never the quad's own
    edge), every footage pixel whose hue, read before the clamp, lies within
    `HALO_HUE_DEGREES` of the key's (fading out at twice that), or whose key
    channel leads the other two (`screens.key_share` past `EDGE_SPILL_LEAD`,
    2 %, in full from `EDGE_SPILL_LEAD_FULL`, 8 %: a skin or fur tone mixed
    with the key reads yellow-green, 40-60 degrees off the key's hue, where the
    hue's weight alone corrected 45-65 % of a hand's edge, #10573), has its chroma
    along the key's own removed with its luminance kept (`screens.despill` with
    nothing unmixed: green limiting for a primary green, a key between a
    primary and a secondary alike - a literal channel clamp with the lost
    luma added back left 0.1-0.8 % on a key of hue 165 where this leaves
    none), at `despill.strength`, full on the surface's side and at the
    boundary and fading to nothing as far into the subject, so its interior
    is never touched (F15: delta E under 1); the footage's own hue there
    (`edge_of`) is left. Then the edge despill, then a light wrap
    (`light_wrap`): a subject pixel on that boundary takes
    `LIGHT_WRAP_SHARE` (25 %) of the insert's colour around it (what the
    surface shows there, read over the surface clear of the foreground),
    less a pixel deeper, nothing past `LIGHT_WRAP_PX` (2 px of a 1080p
    frame) - what a real screen does to a real edge -, and never on the core
    of what stands in front (`front_core`, where the insert is never drawn):
    on a finger or a muzzle thinner than twice the wrap, the insert's blurred
    strokes wrapped in from both sides read as the source drawn through it
    and failed `insert_through_foreground` (#10573). Before the wrap
    (`key_residue`, in both keyed paths), the key's residue: the key's
    share a pixel keeps at that edge - an unmix capped by `matte x 1.5` where
    the matte window holds the key in front past what the frame shows, or no
    unmix at all where a static insert's occlusion holds it in front, a few
    pixels past a dark subject - comes out of the clamp as a grey the
    footage never had (a green key's `(32, 255, 30)` clamps to
    `(32, 32, 30)`), and `edge_limit` lifts it toward the key's luminance:
    a neutral rim 4-12 px wide that the wrap's 2 px never cover. Within
    `RIM_PX` of the part of what stands in front the frame's own key reads
    clear (`clear_part`), inside the quad, a pixel that key reads part key
    (past `KEY_CLEAR_MATTE`) whose colour has no chroma left (under
    `MIN_HUE_CHROMA`, fully from half of it) takes the insert's own pixel
    (what the surface shows there), blended by that matte - all of it where
    it reads the key whole -, so the band reads as the insert behind a soft
    edge; the static path reads the frame's matte for this alone (its
    occlusion still says what stands in front) and hands it to the drift
    read. F57 (`tests/test_video_studio_insert_rim.py`, a dark-brown-rimmed
    head crossing a bright blue panel at 1080p): the band 2-8 px past the
    head from a mean dE of 33 against the insert to under 2, `rim` from
    0.47 to under 0.02. `DRAWING_VERSION` 10. `despill.edge: false`
    draws that edge as the footage shows it, the clamp alone taken out (none
    of the three, the residue still drawn as the insert). The share of the edge corrected is audited per frame
    (`FrameAudit.edge_spill`, the report's `edge_spill: {edge, width_px,
    strength, max, mean, frames, frames_read}`, said `by design` on the
    `Insert` line). The despill is in each insert's segment digest part,
    its preview build's digest when set, a range's bundle, and kept on the
    scene's drawing with the insert's measures (`drawings[...].inserts[...]
    .despill`), apart from `insert_checks.rest_digest`. `screen_insert.DRAWING_VERSION`
    (5) is in each insert's segment digest part, so
    a render resumed across a release never joins frames drawn the other
    way, and `insert_builds.BUILD_VERSION` is 7;
  - without one, the surface's look on its first footage frame is the
    reference (`screen_insert.Surface`, read once per page): each frame is
    read on the same plane - its quad and a rim `RIM` past it, on a grid at
    most `SURFACE_SIDE` long - its exposure matched to the reference, and a
    grid pixel `CHANGE_MIN` grey levels and `CHANGE_SPREAD` median
    differences off the closest of its 3x3 reference neighbours (a track a
    pixel off moves nothing) is changed; a connected change that reaches
    the rim entered the surface from outside it and is in front (bigger
    than `MIN_OCCLUDER_SHARE`); one that stays inside is what the surface
    itself shows (the model's drawing on a screen) and is covered. A frame
    whose surface moved more than `MAX_DRIFT` grid pixels off the reference
    (the shift within `DRIFT_SEARCH` that leaves the fewest rim pixels
    changed, `screen_insert._drift`: a cat over part of the rim leaves it at
    zero, a camera the quad does not follow moves it; a shift wins over none
    only by `DRIFT_MIN_GAIN` of the rim - an occluder's edge pixels
    crossing the threshold decide nothing) holds no occluder -
    the quad no longer sits on the surface, and `track: true` is the fix.
    The insert is drawn where the surface is still visible, feathered by
    `OCCLUSION_FEATHER`, the occluder smoothed over the same window of
    frames and its overlap with the frame before's audited like a
    key's.
  - **What stands in front is never drawn over.** A render
    reported "what stands in front covers up to N% of the quad" and passed
    while its frames showed the insert over that foreground - a black cat's
    head cut at the screen's edge, an orange one seen through the insert -
    and filed nothing. Three causes, three rules:
    - **The weaker change joined to what crossed the rim is in front.**
      Dark fur over a dark screen differs from the reference by less than
      `CHANGE_MIN` (about 20 grey levels against a dark screen; less still
      over a dark bezel), so the mask stopped where the subject crossed the
      bright wall. `Surface.occluded` now grows what came in through the rim
      (the strong change, as before: the seeds) into the weaker change joined
      to it - `WEAK_CHANGE_MIN` (8) grey levels and `WEAK_CHANGE_SPREAD`
      (2.5) median differences off -, only where that weaker change is at
      least `WEAK_MIN_THICKNESS` (2) grid pixels thick once opened: the thin
      ring of weaker change round a strong edge is that edge's blur, never
      more of the subject (grown into, a magenta jacket's mask stood 4 px
      wide of it and the halo gate read the panel). The surface's own noise
      joins nothing.
    - **The clean surface is read over the insert's frames.** The reference
      was the frame the insert comes in on: a subject already in front there
      was part of the surface and drawn over wherever it stayed. The render
      gives each matted insert its plate (`InsertSource.plate`,
      `screen_insert.plate_frames`: the frame it comes in on and
      `PLATE_FRAMES` (7) in all, spread to the frame it goes out on, within
      its footage; in its drawn-by digest when set, a range's bundle, a
      build's), the frame server reads those frames at the host's size
      (`compositor.plate_footage`) and the reference is their median, pixel
      by pixel, on the first frame's grid, each brought to its exposure and
      a frame whose surface drifted past `MAX_DRIFT` left out
      (`Surface.of_plate`): a subject gone from most of them is no part of
      it. One standing still the whole time stayed in it - a cat sat under
      the screen for the whole shot was drawn over on every frame, the gate
      green (the cats film, scenes demo-opencti-graph, demo-openaev-scores
      and demo-openaev-remediation). The plate's median now gives up such a
      subject (`screen_insert.still_subject`, `Surface.still`): inside the
      quad, a connected region `STILL_CHANGE_MIN` grey levels and
      `STILL_CHANGE_SPREAD` median differences off the surface's own flat
      colour (the quad's median), touching the quad's edge and holding
      `MIN_OCCLUDER_SHARE` of it at least - one holding more than
      `STILL_MAX_SHARE` (40 %) is what the screen shows, a UI the model drew,
      never a subject -, filled in the reference from the surface around it
      (`_filled_reference`, a normalised convolution) and made an entry zone
      of the occluder beside the rim: a change standing where the subject
      stood is in front, as one crossing the rim is. `DRAWING_VERSION` 12.
      The depth cue stays the rim's: a subject that never touches the quad's
      edge and never moves is still the screen's.
    - **No ghost where the still subject moved off.** The filled
      reference was a blur of the surface around the still zone: once the
      subject moved its head off its plate position, the uncovered screen
      differed from that blur by more than `CHANGE_MIN`, read as a change
      standing in the entry zone, and the insert was left undrawn there - a
      hard-edged black patch of the footage's dark screen beside the cat
      (the cats film, demo-openaev-remediation at 4 s). `_filled_reference`
      now takes, for each still pixel, the plate frame that shows it clear
      of the subject (within `STILL_CHANGE_MIN` of the surface's flat
      colour, read inside the quad as `still_subject` reads it, never on
      the wall past its rim) - the surface as it is behind the subject -,
      and falls back to the convolution only where no frame ever shows it.
      On the film's frames the ghost mask inside the still zone fell from
      275 to 78 grid pixels and the confidence on the moved frames rose
      from 0.33-0.61 to 0.86-0.94. `DRAWING_VERSION` 13.
    - **One body, one pixel of edge.** The occluder's mask is closed
      over `FRONT_CLOSE_PX` grid pixels and its holes filled - fur in motion
      blur over a dark UI read as a sieve, the insert drawn through it (a
      cat seen through the OpenAEV gauges, the cats film at 80.8 s and
      89.1 s) -, the surface grid is `SURFACE_SIDE` 512 (320 read a 1080p
      quad's edge at 3-4 px: the light halo seen at 57-61 s), the
      occlusion's feather `OCCLUSION_FEATHER` is 1.0 px of a 1080p frame and
      the core's ring (`front_core`) on an occlusion's mask is the feather
      deep, no longer twice it: the subject's edge is drawn at its own
      pixel, the insert never half over it. A key's matte keeps its ring
      twice the key's feather deep: there the ring holds the key's share of
      each pixel, not a feathered mask.
    - **The mask at the frame's own resolution.** On an unkeyed
      insert (`Surface.front(..., full=True)`; a keyed one's fringe is its
      key's share of each pixel, what its edge despill and its halo read, so
      the mask never grows over it), the grid's mask,
      brought to the box (`Surface.on_box`), is read again pixel by pixel
      (`Surface._full`) in the window around it: within `FULL_BAND_GRID`
      (2.5) grid pixels of its edge - where the grid's own pixels and its
      openings leave it unsure: a tuft thinner than two grid pixels is opened
      away - a pixel of the frame (through a Gaussian of `FULL_SIGMA_PX`, 1 px
      of a 1080p frame) that differs from the clean reference brought to the
      box (at the frame's exposure, at its closest within a pixel) by the weak
      change (`WEAK_CHANGE_MIN`, `WEAK_CHANGE_SPREAD` times the grid's own
      spread, `Occlusion.spread`) and is joined to the grid's mask is in
      front. The mask is then closed with a square kernel `close_width` wide -
      `FRONT_FULL_CLOSE_PX` (3) at 1080p, scaled with the frame, odd, never
      under 3 - and its holes filled before the insert is drawn. Its edge is
      the subject's faintest fringe, so the whole mask shows no surface
      (`Front.visible` is 0 on it): the feather falls past it, never on a
      strand a pixel wide, and the frame server's window of readings,
      thresholded at a half, keeps every pixel of it. Before, a bright
      soft-edged subject standing still over a dark UI read 94-97 % sure, yet
      1-2 px strokes of the source (row separators, a panel's corner) were
      drawn across its tufts as dashes. The preview build and the render
      draw through the same `compose`: one matte. `DRAWING_VERSION` 14.
    - **The mask mattes, frame by frame.** The window steadies the mask from
      frame to frame; the frame's own reading now stays in front whatever its
      window says (`visible` is the lower of the two): a head narrower than
      three frames of its motion was smoothed away from under the insert. On
      what the mask marks as in front less its edge ring - twice the key's
      feather deep on a key's matte, the feather deep on an occlusion's
      (`screen_insert.front_core`) - the insert is drawn at
      nothing (`in_front`), keyed or not; a keyed insert keeps its key's
      share of each pixel in that ring, and a subject the tolerance reads
      part key (a green-lit sweater, about 0.27) is never seen through the
      insert where it read 0.27 of it. `DRAWING_VERSION` 8,
      `insert_builds.BUILD_VERSION` 9.
    How sure the mask is, per frame (`FrameAudit.front_confidence`): the
    share of what stands in front inside the quad its cue alone tells from
    the surface - the difference past the strong threshold, the key under
    `KEY_CLEAR_MATTE` (0.1) -, the rest in front by its link to what crossed
    the rim; per insert, so per scene, `foreground.confidence: {lowest,
    lowest_frame, lowest_at, mean, frames, low, ghost}` (`low`: frames under
    `LOW_FRONT_CONFIDENCE`, 0.5; `ghost`: those where it covers more than
    `GHOST_MIN_FOREGROUND`, 2 %, of the quad - `FrameAudit.ghosted`),
    said on the insert's line ("its mask 3% sure at its lowest (t=0:00.25)
    ... (dark on dark)"): a fact at 0.5 or more, the `insert_ghost` finding
    below on more than 5 % of the insert's frames. There is no depth
    estimate in the pipeline: the order the rim gives - what crosses the
    surface's edge stands in front of its plane - is the depth cue the weak
    change is joined to.
  Every frame drawn is audited (`screen_insert.FrameAudit`): the share of the
  quad the insert is drawn on - past `DRAWN_MIN_ALPHA` (0.05) of it, seen
  through included (alpha 0.5 or more before) - where the matte, its
  edge ring left out (`front_core`), says the foreground is (`covered`),
  how sure that matte is (`front_confidence`), for a brand mark the share of its own
  opaque pixels still showing (`logo_visible`), and what the
  gate below reads: the given quad's error against the bezel (`alignment`),
  the quad found (`refined`) and whether the bezel lock's temporal prior held
  it on the last frames' bezel (`held`), the matte's coverage of the quad
  (`coverage`), the mean luma of the quad as shown and of the source frame
  (`luma`, `source_luma`), the key's hue left in connected regions inside
  the quad or within `RESIDUAL_MARGIN` (0.5 % of the frame's width) of it -
  no farther: past it the footage is its own - (`residual_key`), what
  the layer despilled past it (`despilled`, above) and where it sits
  (`residual_where`: `{inside | contain | edge | band: {top | right |
  bottom | left: share of the quad}}` - inside the quad where the source is
  drawn, in the bands a contain fit leaves inside it, the surface
  running past it, or the footage's key colour past the bezel the key did
  not take; the side the quad's nearest edge), the Laplacian variance of the
  insert region over the source's at the drawn size (`sharpness`), the
  hue outliers along the foreground's edge (`halo`) with the share of that
  edge left out as the subject's own colour (`halo_own`), of
  `halo` the share counted where the compositor's clamp touched the edge
  and the mean hue counted (`halo_clamped`, `halo_hue`: with
  `halo_edge`, an author tells spill, own colour and the clamp's apart
  without a crop), the share of it the edge despill recoloured (`edge_despill`) and the share of
  the edge the key's colour was limited out of (`edge_spill`), and the bands a
  contain fit leaves - their share of the quad (`band_share`), what filled
  them (`band_fill`: `source edge`, `bezel`, `black`, `surface` around a
  mark, `footage` unkeyed) and its colour (`band_colour`). Every
  gate of a frame reads the same masks, computed once
  (`screen_insert.gate_masks`, a `GateMasks`): the source's own pixels of
  the key's hue and of its complement in the warped source, grown by
  `SOURCE_HUE_GROW_PX` (2 px of a 1080p frame); the contain bands filled
  under the source (`FILLED_BANDS`; a mark's board keeps being read); the
  foreground grown by `FOREGROUND_GROW_PX` and the key's feather; its share
  of the quad; the residual margin. The audit adds the given quad's offset
  side by side (`alignment_sides`), each edge's bow, inliers, residual and
  visible share (`edge_bows`, `edge_inliers`, `edge_residuals`,
  `edge_visible`), the source's own key hue (`source_key_hue`), the
  foreground's share (`foreground_share`), the share of the quad the colours
  and the sharpness are judged on (`judged_share`, `GateMasks.clean`), whether the colours were compared (`colour_skipped`) and the
  halo's edge length (`halo_edge`). The bezel lock and the flatness
  read the same foreground before anything is drawn (`edge_front`, above),
  on the save's frame as on a preview's or a render's. The server keeps them
  by output frame and the worst failing frame per insert and kind as a JPEG
  under `segments/evidence` (an alignment finding's frame outlines the given
  quad in red and the bezel found in green; a residual key's worst frame
  also keeps, as kind `residual_crop`, a lossless PNG of the side holding
  most of it - that edge's margin and as deep inside it, unmarked,
  `Layer.residual_crop`); each closed segment writes its
  audits beside it (`<segment>.audits.json`), so a render resumed in place
  keeps them, and `render_frames(audits=...)` hands them back merged
  (`compositor.segment_audits`). They become findings (below).
- **A keyed surface is never shown raw.** Render `dad61b44` (ultra,
  revision 106, all gates green) showed the booth TV solid key green at 96.7 s
  (`fade_black` from one insert scene to the next) and key green along the
  bezel at 113.0 s (a cut). The page faded each insert on its own (a 0.2 s
  ramp after a `t0` past its scene's start and before its `t1`, nothing
  outside them) over footage drawn at full strength: the key showed through
  every fade, every frame before `start` and after `start + duration`, and a
  scene's `t1` lands in its transition out. The frame server audited the
  layer it drew, whole, and a frame the page did not ask for had no audit.
  Now:
  - **Held on its surface.** The planner marks a keyed insert (`keyed`) and,
    once its scene has its last length, holds it
    (`timeline.hold_surfaces`: `held`, the scene seconds its layer is drawn,
    and `surface`): the keyed inserts whose quads overlap share one surface
    and hand it over - the first to come in holds it from the scene's start,
    every other from its own `t0`, each until the next comes in or it goes,
    whichever is later, the last to the scene's end (`HELD_END_SLACK` holds
    it on the last frame whatever the rounding). A wall's panes never
    overlap: each holds its own.
  - **Drawn whole, composed over the bezel.** The page (`buildInserts`,
    `heldAt`, `insertShown`; `timeline.insert_layers` /
    `insert_visibility` / `insert_held` are the same rule in Python) draws a
    held insert with nothing held beneath it on its surface at opacity 1 on
    every frame, naming how much of it shows (`&v=`, permille, the ramp of
    `spec.INSERT_FADE_SECONDS`); `screen_insert.compose(visibility=)` draws
    that share of the insert over the colour the surface shows with nothing
    on it (`screen_insert.screen_off`: the bezel's colour when dark and not
    of the key's hue, else black - the contain bands' rule - the footage's
    light laid over it), what stands in front as drawn. Over another layer
    held beneath it an insert fades as any insert: that one covers the key.
    A transition then blends each scene's finished frame, never an insert
    on its own (the layer's CSS opacity is its scene layer's).
  - **Audited for its surface alone when partly shown.** `FrameAudit.visibility`;
    under 1, the colours, the luma, the sharpness and a brand mark are not
    read (what the fade does to the insert), the key, the bezel, the edge
    and what stands in front are.
  - **Followed over those frames.** A tracked keyed insert is followed from
    its first held frame (`Asked.since`): `insert_tracking.track_all` keeps
    the frames before the one its quad is given on, at the tracking size, in
    a temporary file (`_Behind`: hundreds of frames of about 1.5 MB
    each, never in memory, a slot freed once no insert waits for its frame),
    and follows them in reverse with a second tracker; a surface lost there
    holds the quad of the frame after it back to `since`
    (`InsertTrack.held_before`, kept with the track), said in the render's
    notes, never a failure: nothing is drawn on that surface yet, and the
    residual-key and presence gates read what those frames show (a
    behaviour change: such a render failed before). `compositor.matted_spans`
    (the pieces' warm-up), `render_preflight.FrameKinds` and
    `frame_freeze.frame_parts` read the drawn span (`timeline.insert_drawn`).
  - **Its layer keyed by all it is drawn by.** The frame server's last layer
    is reused for the same scene and insert (its key), the same frame time in
    that scene's time base (source and footage frames), size, push and
    visibility, and the same digest of every field it is drawn by
    (`_insert_inputs`, `FrameServer._drawn_as`); `screen_insert.DRAWING_VERSION`
    is 6.
- **An insert that runs to its scene's end is on its last frame.**
  Preview `7b2a216d` (revision 125: an 8.0 s scene, its keyed insert from 0
  for 14 s, `extend_mode: hold`, a cut out, the preview 8.2 s) showed the
  source whole at 7.6 s and the surface flat in the bezel's colour at 8.15 s,
  the footage moving; preview `15e53e0e` showed it fading at 7.95 s. The
  join gate said "drawn on every one" on both. The insert's `t1` was the
  scene's length when the inserts were planned, before the music's beat
  lengthened the scene (`beat_extra`); `held` ran to the final length; and
  the ramp faded the insert out over its last 0.2 s. Past `t1` the layer was
  drawn at visibility 0, in the bezel's colour, and its audits passed. Now:
  - **Never faded out.** An insert whose `start + duration` reaches its
    scene's length is marked `to_end` (`timeline._insert_payload`). Its
    visibility (`timeline.insert_visibility`, the page's `insertShown`) is 1
    from its fade in to the scene's last painted frame. Only an insert that
    ends inside its scene fades out.
  - **One clock.** `timeline.hold_surfaces` first moves such an insert's `t1`
    (and a video's last held frame) to the scene's final length
    (`run_to_end`), so `t1` and `held[1]` are one second:
    `timeline.painted_end`.
  - **Tripwires.** The page notes every frame that paints a scene at or past
    that end (`noteOverrun`, `window.__xtm.overruns()`, read by the driver
    as each segment closes). The frame server notes a layer asked on a frame
    its scene does not own, or asked partly shown (`&v=` under 1000) inside
    the span the insert shows whole on (`timeline.whole_frames`: its fade in
    done to its scene's last encoded frame, `frame_owners`;
    `FrameServer.overran`), on a frame's centre sub-frame alone. Both reach
    the audits as `overrun`. `insert_checks.overruns` words it as
    `insert_missing`, naming the scene, the second, the length and the
    painted end - a defect of the render harness (`action: nothing`, below).
  - **Its source read off the encoded frames.**
    `insert_checks.presence_frames` names every frame each insert's scene
    paints it on, or where it shows whole (`Placed.whole`,
    `insert_checks.shown_whole`). The driver keeps where the page places
    it on those frames (`FrameServer.place`). After encoding,
    `insert_checks.presence` decodes the render's or the preview's own MP4
    (at most `PRESENCE_WIDTH` wide) and reads each frame inside the quad,
    as `PresenceGate` does:
    - the key's colour (`screen_insert.key_left`) where its scene shows
      alone;
    - where it shows whole and at least `PRESENCE_MIN_SHOWN` of it is on
      screen, a flat fill (`detail_variance` under `FLAT_VAR_MIN`, judged
      only where the source expected there is not flat itself);
    - the source's presence (`structural_similarity` at least
      `PRESENCE_SSIM_MIN` against the source frame expected there, or the
      one either side). The structural similarity is taken on lumas brought
      to one mean and spread, so a fade, a transition's dimming or the
      footage's light leave it.

    A frame shown whole that the page placed no layer on, beside one it
    did, is missing too. Any of them is `insert_missing`, with the encoded
    frame as evidence. A frame covered over `PRESENCE_MAX_FOREGROUND` by
    what stands in front, or a tracked insert with no refined quad, is
    counted unjudged. The report carries
    `presence: {frames, missing, unjudged, joins, joins_missing,
    first_missing?, blended?, least_alpha?, faint?, blended_unread?,
    covered?, undrawn?, dimmed?}`. It reads only the frames that are its scene's
    (`Placed.owns_frame`, those its flicker is judged on): a transition's
    first frame, the scene before whole there, is faint. What it read on
    the joins is its presence line's; the joins line says the frames the
    layer was measured on there and the residual key alone.
  - **A transition's frames read unblended.** On a crossfade the
    encoded frame is the insert blended with the scene coming in, and the
    gate compared it with the raw source: at 8.13 s of the brand film's
    scene s03-stat it read SSIM 0.30 under 0.3 and blocked every preview
    (11 of 11 join frames from the s04-filigran side), with "render again"
    as the fix. A presence frame that shows less than `BLEND_WHOLE` (0.995)
    of an insert is now captured twice more while it is drawn
    (`FrameServer.blend_scenes`, `keep_blend`): the frame with the insert's
    scene painted black, then white (the page's `tone()`: `brightness(0)`,
    then `invert(1)`, on its layer, its opacity, transform and mask kept),
    at most `PRESENCE_WIDTH` wide, handed on as the audits' `blend`. The
    black frame is everything else the frame shows; the white one less the
    black one is how much of the scene it shows at each pixel. The gate
    unblends the encoded frame with them (`PresenceGate._unblended`) and
    judges it like a frame shown alone, where that share is at least
    `PRESENCE_MIN_SHOWN`, so every transition kind is read the same way.
    A finding there names the insert's alpha on that frame. The report
    adds `blended`, `least_alpha`, `faint` (frames shown under
    `PRESENCE_MIN_SHOWN`, not judged, said as a fact) and `blended_unread`
    (a segment drawn without the paintings: read raw only
    where it shows `BLEND_RAW_SHOWN` or more). The fix line of both
    presence findings no longer asks to render again: the measure is
    deterministic.
  - **One floor for a blended frame.** `PRESENCE_MIN_SHOWN` is
    0.65, for the share the page placed and for the alpha read unblended:
    the s03-stat insert's crossfade into s04 passed on 13 of 19 blended
    frames and warned on the last ones (alpha 0.53 to 0.60), where the
    incoming statement's bright text over the quad left the codec's
    ringing in the unblended quad, divided by the alpha. Under the floor a
    frame is faint - a fact, never a finding - so a transition frame can
    never fail presence (`test_video_studio_insert_join_blend_6408`: the
    joins fixture with that ringing fails from 0.5 and passes from 0.65,
    its black-screen variant still fails).
  - **Two drawings of one join agree.** The s01 preview of a sweep
    warned on the s01 to s02 join where the s02 preview, minutes later,
    read the same frames present: the measure sat on its threshold and
    flipped with the encode. The presence summary keeps the plan's spans of
    the join frames read present and missing (`join_spans`). `record_seen`
    keeps them on the scene's drawing (`joins`,
    `render_checks.join_measures`): its own inserts, and those of the
    neighbours a scene preview reads through its transitions, each with its
    scene's digest (a preview served from a kept one keeps them too). The
    next drawing reads them back (`join_readings`: the drawings of the
    scenes as they are now, the insert's scene unchanged) and
    `insert_checks.agree_on_joins` holds its presence to them: where one
    read the insert present and the other missing, a note says so and the
    summary carries `disagrees` (its report line says it), and a presence
    finding whose every missing frame is on a join an earlier drawing read
    present is taken out, an `avoid_transition` one included
    (`test_video_studio_presence_faint_6434`).
  - **A transition's blur or dimming judged against the frame around it.**
    Unblending takes the scene beside the insert away, never the blur or the
    dimming its own layer is drawn with. Inside a transition or a fade (`Placed.blends`,
    `insert_checks.blend_windows`) the whole scene is dimmed or blurred, its
    insert with it, and the absolute floor no longer says "flat": the
    frames of each transition into or out of the scene that lasts (blur,
    fade_black, crossfade, zoom, flash, ...) and of the video's fades from
    and to black. There, a quad under `FLAT_VAR_MIN` is held until the
    scene's un-transitioned frame beside the window is read
    (`PRESENCE_REFERENCE_FRAMES` each side), then judged against the frame
    around it:
    - the frame around the quad (the quad grown by
      `PRESENCE_AROUND_MARGIN` left out) is read as a `PRESENCE_BLOCKS`
      grid of detail (`frame_blocks`), so a caption or a mark over a few
      blocks moves nothing - its own scene's frame, unblended where the
      frame blends it (`own_blocks`: a block showing under
      `PRESENCE_MIN_SHOWN` of the scene left out), its quad read
      unblended too: a statement's bright text coming in over a black
      quad is no detail of the insert's scene;
    - its attenuation is the median block ratio against the
      un-transitioned frame, over the blocks holding
      `PRESENCE_TEXTURE_MIN` there (`frame_attenuation`, at least
      `PRESENCE_MIN_BLOCKS`);
    - the quad is present when its detail kept `PRESENCE_TRACK_SHARE` of
      the un-transitioned frame's quad's, attenuated as much: a source
      dims and blurs with its scene, a flat surface has no detail to keep;
    - with no un-transitioned frame or no texture to compare, the quad is
      present when the whole frame around it is under the floor too
      (`around_detail`): the screen alone flat is the insert missing.

    A frame judged present so is a fact of the report (`dimmed:
    [{window, frames, whole_frame}]`): "2 encoded frame(s) under the
    presence floor (6) with the whole frame, inside the blur in: the
    transition's, its source judged present". One judged flat says it was
    flat against the frame around it, inside its window.

    `test_video_studio_presence_blend_windows_6426` runs every window
    through the gate on the shared synthetic booth
    (`tests/video_studio_insert_fixture.py`, `PresenceRender`: the layers
    as `applyTransition` sets them, the flash and the video's fades over
    them): an insert entering and one leaving on a crossfade, a zoom, a
    flash and a wipe, the video's fade in and out, a scene its two blurs
    cover whole (no frame beside its windows: `around_detail`), a
    high-contrast source under a blur (over the floor, compared by its
    structure: another picture there is its source absent) and a zoom
    blurring a noisy wall 8 CSS px on the frames it is judged on. Each
    passes drawn and fails as missing with its source removed there.

    `insert_missing` and `insert_dropped` read off the encoded frames, an
    overrun and a frame not drawn (`insert_flicker`) carry `action:
    nothing`: nothing in the spec causes them. On one frame alone
    (`render_checks.HARNESS_MAX_FRAMES`: one frame the page drew and
    the encoder dropped) such a finding is the render harness's defect; on
    more it is the video's and blocks - `insert_missing` (a layer not drawn
    on several frames the page draws it on is `insert_missing`, never a
    flicker), or `insert_measurement_lost` when the gate's own measurement
    lost its frame (below): "source missing on 480 of 480 frames" and "not
    drawn on 462 frames" were said once and left, the verdict green. A
    render is deterministic,
    so rendering again draws the same frames, and no edit of the video
    moves them: each one-frame defect is the render harness's by its own words
    (`render_checks.harness_defects`). It leaves the findings - it
    never blocks nor warns, on any drawing - and is kept on the render as
    `checks.harness`, with its frames' span in the plan (`data.span`) and
    the frame that shows it (`data.frame_file_id`, stored, deleted and
    attached like a finding's). The answer says it once, for the
    administrator, under `infra_once` (`render_checks.harness_line`); on a
    later drawing of the same digest that left it on the same frames it is
    `said: false` with `repeated_from`, a fact. `record_seen` keeps them on
    the scene's drawing (`unexplained`), `unexplained_before` reads them
    back for the next one; a render's `open_findings`, `scene_passed` and
    `picture_findings` never count one, one stored among the findings
    included.
    A presence or a dropped finding whose frames all lie in one transition
    of its scene names the edit that keeps the insert off them instead
    (`insert_checks.join_action`, `action: avoid_transition`: "make
    scene b's transition a cut, or end the insert before it (a duration
    under 1.50 s)"; "start the insert after it" for a transition in): a
    finding of the video, which fails like any the spec causes - a black
    screen on a crossfade is never left to the harness.
  - **A render reads its inserts as its scene's preview does.**
    Render ac413cea (high, 1920x1080 at 60 fps, motion blur on) reported
    the OpenAEV insert "no bezel found under the quad" and "its source
    missing from 480 of 480 encoded frames", XTM One and OpenCTI "not
    drawn on 462 / 385 frames the page draws it on", while its crops
    showed every insert drawn and its scenes' previews - drawn at standard,
    where no motion blur runs (`motion_design.BLUR_QUALITIES`) - read the
    bezel 0.05% off and every frame present. Under motion blur the page
    draws each frame as `BLUR_SAMPLES` sub-frames, names the output frame
    on each (`S.frame = Math.round(index)`) and asks a matted insert's
    frame again only for another address (`setImg`): the first sub-frame,
    never audited (`FrameServer.shutter`), asked it, and the centre one -
    the one a frame is judged by - asked nothing, so it was audited on no
    frame whose footage and source frame did not change between the two
    (every frame of a still on a still or filled surface). Now the frame
    server keeps what each matted insert was last asked for
    (`FrameServer._asked`: the output frame it named, its audit, its
    evidence), and on the centre sub-frame the driver reads
    `insertRects()` whenever one waits for it (`FrameServer.unaudited`)
    and audits each insert the centre shows with the drawing it shows
    (`FrameServer.centre_shown`): a high or ultra render audits every
    frame a standard preview audits, with the same readings. One
    coordinate system everywhere: every box is 0-1 of the frame,
    multiplied by the decoded frame's own size, never a device scale nor a
    preset - the frame server cuts each crop with the box it places
    (`FrameServer.crop` with the stage, `_box_of`, `_save_crop`), where an
    ultra render's crops were the 1080p box doubled and clamped; a
    segment restored or taken over brings its blend paintings back with it
    (`render_segments._evidence_files`), and a range's are merged
    (`render_ranges.merge_audits`).
  - **The measurement checks itself (`insert_measurement_lost`, a
    block).** An insert a render drew whole whose frames found none of the
    bezel its save or a drawing of the same shot and spec found
    (`insert_checks.unmeasured`, `earlier_bezels`: the save's `Measured`
    and the drawings' records, each with the size it read at) is not read
    by the presence gate, and its frames not drawn are no finding of their
    own: its first frame shown whole is named, with its box, the size the
    encoded frame was decoded at and the size of the frames it audited, and
    each earlier measure ("found as preview 5fc492aa measured it at
    1920x1080 ... where this render read no frame of it audited"). And on
    an insert's first `MEASURE_FRAMES` frames read whole the gate looks for
    its source around the quad it reads (`PresenceGate._looked_around`:
    the quad moved up to `MEASURE_SEARCH` of its size in `MEASURE_STEP`
    steps, a read already within `MEASURE_GAIN` of an SSIM of 1 never
    searched): its source found there at `MEASURE_FOUND_SSIM` or more,
    `MEASURE_GAIN` more like it than where it is read, at one place on
    each of them, the gate reads another region than the page drew it on -
    named with the box, the frame, the size and where the source is ("8%
    of the quad's width left of it"). Either way none of its presence
    readings is said (`classify`): never "source missing from N of N", nor
    present on a region it is not drawn on. A quad painted flat is no
    match (an edge of what surrounds it reads well under
    `MEASURE_FOUND_SSIM`): an insert missing from its first frames on is
    `insert_missing`.  - **Each encoded frame judged for the scene that owns it.** A
    clip scene with an insert running to its end (6.127 s) cut into the
    next at 60 fps: the cut lands at frame 8078.94, so frame 8079 is the
    next scene's. Under motion blur its first sub-frame (8078.83) still
    paints the scene the cut leaves, stamped 8079 (`S.frame`), and the
    frame server judged that request by its output frame against a span
    worked out from seconds: "frame 8079 has its scene's footage painted at
    t=6.128 s ... at or past its painted end". Now one table says which
    scenes each encoded frame paints (`frame_owners.FrameOwners`, built
    once per render from the planned document): the page draws frame `n`
    at `n / fps` and paints every scene with `start <= t < end`, the last
    to the video's end, so a scene's frames are `[first, end)`, each the
    first frame whose time reaches its second (`frame_owners.first_at`:
    about `ceil(seconds * fps)`, read as the page compares, never a float
    landing one frame over - `0.28 * 25` is `7.000000000000001`, and the
    page paints frame 7). A cut gives the frame it falls inside to the
    scene coming in; a transition's frames are the scene coming in's but
    its first, the scene before's (`FrameOwners.owner`). The frame
    server's spans (`timeline.whole_frames`),
    the presence gate's frames (`insert_checks._painted`, `Placed.whole`
    and `Placed.expected`), the joins (`FrameOwners.joins`: a cut's
    `CUT_FRAMES` each side, a transition's frames both scenes are painted
    on - the page blends nothing once the scene it leaves is gone), the
    blend windows (`insert_checks.blend_windows`) and the scene each frame
    of the frozen-picture watch shows (`frame_freeze.Shown`) read it. A
    sub-frame of the shutter away from its frame's centre is drawn, never
    audited nor judged (`FrameServer.shutter`, set by the driver before
    each one). `tests/test_video_studio_frame_owners_6433.py` holds the
    table to the page's rule and to every check's frames on 20 random
    documents: a check that works frames out from seconds on its own is a
    test failure, not a finding.
  - **A layer judged drawn only where its scene owns the frame and shows it.
    ** A 1080p60 render said "insert flickers at t=...: ... is not
    drawn on frame N, where it is drawn on the frames around it" on two
    insert scenes, with "not drawn on 61 frame(s) the page draws it on" and
    "not drawn on 55" on the joins line; the frame there is the scene before
    whole, the blur into the insert scene not begun. `missing_frames` read
    every frame the page draws the layer on (`Placed.expected`), and that
    run starts at the scene's first encoded frame - where a scene coming in
    on a frame is at the page's progress 0 (`applyTransition`: `(T -
    start) / duration`), its layer at opacity 0, slid out or masked out - and
    holds its transition out, whose frames the table gives to the scene
    coming in. It now reads `Placed.judged_frames`: the frames its scene
    owns (`FrameOwners.owned`, `Placed.owns`: those no later scene is
    painted on) and, through its scene's transition in, only those past its
    progress 0 (`Placed.arriving`) where the page placed its quad at
    `PRESENCE_MIN_SHOWN` or more (`insert_checks.placed_share`, the
    presence gate's own floor). A frame the scene before still paints whole
    is never counted; what the layer shows on the frames left out is read
    off the encoded frames (the guard, the presence gate). The report's
    `joins` no longer counts the audits' missing frames: the joins line
    says what the presence gate read there by its own `join_spans`, in the
    MP4's seconds (`joins.read: {present, missing}`) - one account of the
    joins. `tests/test_video_studio_insert_flicker_owned_6542.py` joins a
    full-screen recording to an insert scene by each transition and a cut,
    at 30 and 60 fps, coming in on a frame and half a frame past one: no
    `insert_flicker`, and one withheld frame its scene owns flickers on
    exactly that frame.
  - **One table of an insert's frames for its flicker, its flicker fact and
    its presence.** A 1080p60 render with two static
    inserts (fit contain, no key, untracked) still said under `infra_once`
    "insert flickers at t=1:17.57: ... not drawn on frame 4654 ... (376
    frames without it, 18 on its scene's join)" while its facts said
    "flicker 0 frame(s)" and "its source present on all 381 encoded
    frame(s) it shows whole on": the flicker check (`Placed.arriving`, a
    transition's first frame the scene coming in's whenever the plan's
    second lands before it), the presence gate (every frame it shows whole
    on placed at `PRESENCE_MIN_SHOWN`, a slide's or a wipe's first frame
    included, placed whole there) and the flicker fact (`flicker_frames`
    over every audited frame, never a frame not drawn) each worked out the
    insert's frames on their own. Now `FrameOwners.owner` gives the first
    frame of a transition in that lasts to the scene before (the page's
    progress 0 or a hair past it: the scene before whole), `FrameOwners.alone`
    the frames a scene owns past its transition in, and
    `Placed.owns_frame` is the one rule the three read: a frame its scene
    owns alone, or a frame of its transition in or out where the page
    placed its quad at `PRESENCE_MIN_SHOWN` or more - never the
    transition's first. `insert_checks.owned_flicker` is the flicker
    check's one count on it, its matte reversing and its layer not drawn:
    the findings read it and the report's `flicker_frames` counts both, so
    the harness's note of a frame not drawn is never said beside a flicker
    of 0; `insert_checks.contradictions` names a report that does, logged
    by the render as the checks' own defect. A frame that is not its
    scene's is faint to the presence gate, a fact. The "not drawn on N
    frame(s) the page draws it on" fact is gone (`missing_frames` stays in
    the report for debugging), and the joins line says the frames measured
    there and the residual key alone - what the encoded frames showed is
    the presence line's (`joins.read` kept for debugging).
    `tests/test_video_studio_insert_flicker_join_owner_6597.py` joins a
    full-screen recording to a static insert scene by a zoom, a whip, a
    slide and a flash at 60 fps: flicker 0 and no `infra_once` note, one
    withheld frame of its scene's failing `insert_flicker` on exactly that
    frame, and a report holding a note beside a flicker of 0 failing its
    self-consistency check.
  - **A layer not drawn said off the encoded frame.** A
    high render that passed its gates said "insert flickers at t=0:30.33
    ... not drawn on frame 1820 (357 frames without it)" for the insert of a
    statement over a clip, while the presence gate read its source present
    on all 467 encoded frames and the MP4 at 30.35 s shows it drawn, blended
    with the scene before (a crossfade frame): the page log of that scene
    (the frame server's audits) lacked the layer on frames it owns, and the
    finding was raised from the page log alone. `missing_frames` is now the
    page log's account and never a finding: `insert_checks.findings` raises
    none from it. The presence gate keeps it (`PresenceGate.undrawn`) and
    raises `insert_flicker` on those frames only where it reads the insert
    missing off the encoded frame (`PresenceGate.not_drawn`): a frame shown
    whole (`BLEND_WHOLE` or more, never one it read unblended), outside its
    scene's transitions and the video's fades (`Placed.blends`), the page
    having placed it there - whatever the scene's type; on more than
    `render_checks.HARNESS_MAX_FRAMES` of them it is `insert_missing`, a
    block, and an insert whose measurement is lost
    (`unmeasured`) is not read, nor said not drawn. The frames it names
    leave the `insert_missing` finding (one note for one loss), its data
    names them (`missed`, the message each of them up to
    `MAX_NAMED_FRAMES`), the summary counts them (`undrawn`) and the report's
    `missing_frames` is that count. The one table holds:
    `owned_flicker` is the matte's reversals on the frames that are its
    scene's, and the report's `flicker_frames` counts them and these
    confirmed frames, so `contradictions` still finds no note beside a
    flicker of 0 - and the withheld frame is a note only where the
    encoded frame misses the insert. A page-log miss the encoded frame
    contradicts is dropped silently; a crossfade frame is never a flicker
    frame.
  - **What the scene draws over the quad left out of the presence read.
    ** A statement over a picture, its insert on the wall display,
    said "insert missing at t=0:50.80 ... SSIM 0.29 under 0.3 against the
    source frame" on 47 of 806 frames, once per preview and once per render:
    its three-line headline and its chips sit over the quad and lowered the
    similarity to the bare source. The page knows those boxes: on a presence
    frame `insertRects()` adds, per insert, the `[x, y, w, h]` ink of what
    its own scene draws over it - the page's own `onScreen` (the quality
    check's reading: a block's glyphs, a chip or an overlay whole, the
    subtitles burned in, the watermark), any of it shown -, kept by the
    frame server 0-1 of the frame (`FrameServer.covers`, the audits'
    `over`, merged by `segment_audits` and `render_ranges.merge_audits`).
    The gate paints them on the frame, reads them on the quad's plane as it
    reads the frame (`PresenceGate._covered`), and compares on the rest of
    the quad, the boxes grown by `PRESENCE_COVER_GROW` grid pixels - when
    the boxes as given leave at least `PRESENCE_MIN_UNCOVERED` (40 %) of the
    quad; under it the frame is not judged (`covered`, its report line says
    how many). The threshold stays `PRESENCE_SSIM_MIN`. A segment without
    `over` is read whole.
    `tests/test_video_studio_insert_infra_notes_6596.py`: a statement over
    a clip whose page log misses 300 frames that every encoded frame shows
    the insert on, crossfade frames among them, says nothing; the insert
    really gone from 3 of them is one finding naming them (`insert_missing`,
    a block: more than one frame); a headline over 55 % of the quad leaves a
    drawn insert present on every frame (and reads it missing without the
    page's boxes), the insert removed under it is missing on every frame
    shown whole, a headline over 70 % is not judged; and in a real Chromium,
    `insertRects` names a statement's lines and chips over its insert.
- **The insert region cut from the frame as drawn.** The render
  names, per insert, the output frames whose region is kept as a 100 % crop
  (`insert_checks.crop_frames`: its first frame once faded in, its middle
  and its last before the fade out, inside a preview's window; a range
  worker takes its own); after each such frame is captured, the driver asks
  the page where it placed each insert (`window.__xtm.insertRects()`, the
  `img.ins[data-insert]` nodes' rects in CSS pixels of the stage, how much of
  each shows and, the boxes of what its scene draws over it) and cuts
  that rect out of the captured frame at the render's resolution
  (`FrameServer.crop`, a PNG under `segments/evidence`), handed back with
  the audits (`crops`, each with its box 0-1 of the frame, `crop_boxes`, and
  the captured frame's size, `crop_frame`), stored as files of the render
  (`render._store_crops`) with their `file_id` on the insert's report, and
  attached to the preview and render answers: the real pixels of the
  compositing, where the scene preview's three frames are stored at
  `max_side=1280`. An answer names every file of its checks - each crop, each
  finding's frame - by the conversation's copy it shows
  (`_render_pictures`, `render_checks.with_files` over `_file_refs`'s
  `copies`): the render's own ids are not files of the conversation, and
  `read_file_content` refused the ids a preview's `inserts` block gave. The
  files the checks name - each crop, each residual side, each finding's frame
  - are attached, a failed render's too (a finding's frame was only
  linked, and `read_file_content` answered "no file ... is attached" for it),
  up to `render_checks.ATTACHED_EVIDENCE` (12) pictures an answer
  (`render_checks.evidence_files`): the render's frames or poster
  first, then each insert's worst finding's frame (a failing one before a
  warning, then the earliest) and its first, middle and last crops, then the
  rest; past it they are linked - files of the conversation, named, which
  `read_file_content` opens - and the answer says how many. The stored
  checks keep the render's ids, which the retention deletes.
- **Hosted where the footage is.** Inside a clip's picture box, a recording's
  viewport (under the camera) or a backdrop's cover rect (`coverRect`, under
  an image's motion), so it moves with the scene and is darkened with the
  backdrop.
- **The editor's preview plays it as the render draws it
  (`insert_builds`).** The stage cannot ask the driver for each frame, so
  `insert_builds.attach` (called by `preview.build_preview`) has every insert
  over video footage drawn ahead, in the background (`prepare`, through the
  figures' claim and drawing slots, `figures.start_background_draw`), by
  `screen_insert.compose` itself: the source at the scene's time, its key or
  its occlusion read on the footage frame under it, its quad tracked when
  `track` is set, its glow under it - every frame the whole footage frame
  `BUILD_WIDTH` wide at `BUILD_FPS`, transparent but for the insert,
  encoded as a VP9 WebM with its alpha (`yuva420p`). It is kept under the
  digest of what it is drawn from (`InsertBuild.digest`: the footage and
  source files, the quad, the times and maps, fit, glow, track, key) in
  `video-studio/insert-builds/`, swept with the figure stills' retention
  (`insert_builds.sweep`), and served by an insert token
  (`media.mint_insert_token`, `GET /video-studio/insert-build/{token}`, by
  ranges). The payload gets `build: {fps, duration, t0}` and the media
  `build/<insert key>`; the page plays it over the footage in step with the
  scene (`previewBuild`). Until it is drawn the scene is `preview_building`
  (the editor polls) and the page maps the source onto the quad with a CSS
  `matrix3d` (`quadMatrix`), a video source playing at the scene's time
  (`previewInsert`), on transparency (`.insprev` has no background). The
  timeline's thumbnail draws it the same way over the scene's poster.
  That stand-in is brought to the render's picture by
  `insert_builds.attach_stand_ins`: a keyed insert gets `preview_key` (its
  key's colour and matte tolerance), which the page tests on the footage frame
  shown with `screens.key_matte`'s own arithmetic (`keyMatte`, its constants
  from `screens.key_matte_constants`) and paints what is not the surface back
  over the insert (`keyMask`, a canvas at most 480 px across): a subject in
  front of the screen stays in front, no key colour shows. A tracked insert
  over video gets `preview_track` (`{from, fps, quads}`), the quads the last
  render of the same scene followed: `render._track_inserts` keeps them in
  `render_cache` under `preview-tracks/<insert_tracking.preview_track_digest>`
  (the footage file, the scene, the insert's key, quad and timing, the
  scene's `time_map`; none kept for a stabilised footage, whose quads are
  not the played file's), and the page draws the stand-in on the quad of the
  footage frame shown (`previewQuad`). A tracked insert with no kept quads
  (never rendered, or its quad or timing changed since) stays on its planned
  quad: it is marked `approximate` and its scene `preview_inserts_approximate`
  (`insert_builds.mark_approximate`), and the editor's stage shows "Preview
  approximate: insert compositing" while the playhead is on that scene,
  instead of passing the mapped picture as the render's. Nothing else is
  marked.
- **Followed through the footage.** Without `track` the quad is fixed
  for the insert's duration. With `track: true` the quad is the surface on the
  footage frame the insert comes in on, and `render._track_inserts` follows
  it once the timeline is planned (the scene's `time_map` says which footage
  frames the insert shows, `timeline.map_forward` of its `t0` / `t1` times the
  footage's fps - a keyed insert's whole held span, followed back from its
  `t0`, above): `insert_tracking.track_all` decodes each footage once for
  every tracked insert it shows (grouped by file, across scenes), at most
  `TRACK_SIDE` long, and runs one `tracking.PlanarTracker` per insert, from
  its own first frame to its last, on the surface and its bezel -
  the quad and a ring `BEZEL_OUTSIDE` past its edges; what moves on its own
  on the surface (the model's drawing on a screen) agrees with no
  homography and is dropped as an outlier - one homography per frame
  against the first, the four corners moved by it (the surface's
  perspective, never an axis-aligned rectangle) and averaged over
  `SMOOTH_WINDOW` frames (`screens.smooth`). The payload gets `track: {from,
  to, fps}` and `box` (the box every tracked quad lies in, as four corners);
  the page places each frame on that box (`insertBox(ins.box || ins.quad)`)
  and names the footage frame it shows (`&f=`, from the footage second its
  scene's frame source shows, `inserts(t, at)`); the driver's
  `InsertSource.track` draws the frame into that frame's quad
  (`screen_insert.draw(..., footage_frame)`, `warp(within=box)`), cached by
  source frame, footage frame and size. A frame the tracker is not sure of
  keeps the bezel read instead (below); a surface the bezel read loses
  too raises `InsertTrackLostError`: `Scene <id>: its screen insert <n>:
  surface lost at t=<scene seconds> s of its scene (footage frame <f>, <s>
  s): <why the tracker let it go>, and <why the bezel read did>`, carrying
  that footage frame with the last quad found outlined
  (`frame_png`). The render (and the scene preview, the same pipeline) ends
  failed with a `surface_lost` finding, the frame stored as a file of the
  requester (`render._surface_lost`). An insert over a picture backdrop has
  no footage to follow: it stays where it is, with a note. The tracking
  reports its progress every `PROGRESS_EVERY` frames, the render's
  heartbeat on a long recording. The editor's preview build is tracked the
  same way. A wall is followed once, as its overlay's quad, and each pane's
  quads are the wall's split (`insert_tracking.pane_track`).
- **A frame the tracker is not sure of keeps the bezel read.** On a
  wall shot whose bezel drifts 0.5 % over 8 s (the static alignment gate,
  blocking its locked quad past 6.6 s), `track: true` failed on
  footage frame 1: "the track's confidence fell to 20%". Two causes, both
  fixed:
  - **The confidence is the share of the points followed into the frame.**
    `PlanarTracker.adopt` reports `points` after it replaced the points it
    lost (`_refill`); the insert tracker divided its inliers by that, so 18
    of 29 points agreeing read 15 % against the 122 kept. `TrackStep.followed`
    carries the points followed, which the confidence is now read against,
    as `MIN_CONFIDENCE` says and the logo trackers already read it.
  - **The bezel read carries the frames the tracker doubts.** On every frame
    each way an insert is followed (`_Follower`) reads the bezel near where
    it read it on the frame before (`surface_refine.read_near`: `refine` on
    `edge_field` - the luma, or the key's matte with what stands in front of
    its edges left out, the read the save and the static gate make - on the
    part of the frame a refined quad can reach), seeded by its own reading,
    never by a tracker's quad but on a frame it found nothing (then a sure
    tracker carries it: a surface moving faster than its band). A reading
    more than `BEZEL_READ_MAX_SHARE` (the alignment gate's 0.5 %) from its
    seed is none. The frame's quad is the tracker's when it is sure - enough
    points, at least `MIN_CONFIDENCE` of those followed agreeing, a quad that
    neither folds nor changes size, and its farthest corner within
    `BEZEL_READ_MAX_SHARE` of the bezel read there (a quad the bezel denies
    is one the alignment gate would fail); else the bezel read's, the
    tracker opened again on it (a surface with too little detail to follow
    at all is carried by the bezel read from its first frame). Neither:
    `InsertTrackLostError`, saying why each let it go. `InsertTrack.bezel_read`
    lists the frames the read placed (kept with the track, `render._keep_tracks`,
    and through `pane_track` and `track_stabilised`); `said()` is "N frames by
    the tracker, M by the bezel read". The track's digest carries
    `TRACK_VERSION` and the insert's key, a tracked build's digest
    (`insert_builds`) `track_version`: tracks kept before are followed again.
    The read costs about a third of a tracker step a frame.
  - **The save seeds and follows it.** `insert_bezels.measure(..., tracks=True)`
    (the save alone; the render's and the quality check's measures read the
    static inserts) measures a tracked insert as a static one, on the frame
    it comes in on: its quad is snapped to the bezel measured near it
    (`seed_quad`: within `SNAP_MAX_SHARE`, past `SNAP_NOISE_SHARE`, written
    and said like a static one's), the seed its track starts from; then it
    is followed as a preview follows it (`insert_tracking.asked_of`, the
    render's own reading of the frames to follow; the raw take of a
    stabilised shot) and the answer says "Scene <id>: its screen insert <n>,
    track: N frames by the tracker, M by the bezel read." or the loss, with
    "(followed at save: a preview of scene <id> fails there)"
    (`track_note`). Kept with the measure (its digest carries the seconds
    followed and `TRACK_VERSION`); never refused nor judged by the static
    gates; the tracks of one save bounded by `TRACK_SECONDS`, one past it
    left to the preview.
  Tests: `test_video_studio_track_bezel_read_6666.py` (an 8 s wall shot whose
  screen draws on its own, followed from frame 1 where the old reading gave
  up; a flat green screen on a shimmering wall, most frames by the bezel read;
  both drawn by the compositor with alignment under 0.1 % on every frame and
  no insert gate failing; the monitor leaving the frame at 3 s lost at
  t=3.00 s; the save's seed, its answer and the loss it reads).
- **On a stabilised AI shot, through its raw take.** A stabilised take
  is warped to its first frame and cropped: followed there, the quad jitters
  with the warp's residue and the crop cuts the screen at the frame's edge.
  The render reads the playing take's stabilisation (`stabilise.raw_take`:
  the un-stabilised file and each frame's map, `MAPS`), downloads the raw
  take of every shot a tracked insert sits on (`render._gather_raw_take`,
  `_Media.steadied`), and `insert_tracking.track_stabilised` takes each quad
  into the raw frame through the map of the frame it comes in on, follows it
  there with the same tracker, and brings each quad followed back through the
  map of its frame - where the stabilised take draws the screen, so the
  insert moves with the picture, not with a tracker reading warped frames. A
  quad standing past the stabilised frame by more than `CROP_TOLERANCE`
  raises `ScreenCroppedError` (an `InsertTrackLostError`, so a
  `surface_lost` finding with its scene time): "screen cropped" at that
  frame, saying whether the crop cut it (generate the shot again with
  `stabilise: false`) or the camera carries it out (cut the insert short). A
  stabilised shot without maps has its inserts follow the stabilised take.
- **Its findings (`insert_checks`).** A whole render and a scene
  preview turn the audits into findings, each a failure with its time in
  the MP4, its scene and, in `data.frame_file_id`, the frame that shows it
  (stored by `render._store_proofs`, deleted with the render:
  `render_retention.render_files` reads them from its checks):
  `insert_foreground` (`insert over foreground at t=...`: any one
  frame where more than `MAX_COVERED_SHARE`, 0.5 %, of the quad is drawn -
  past `DRAWN_MIN_ALPHA`, seen through included - over what the matte marks
  as in front, its edge ring left out; blocking, with that frame marked in
  red and its worst stretch at 100 %, unmarked: `Layer.foreground_crop`,
  evidence kind `foreground_crop`, the report's `foreground_crop`, stored
  with `render_checks.INSERT_STRIPS`; `data.worst_frame`, `threshold` and
  `front_confidence`. The matte draws nothing there, so the finding is a
  compositor's tripwire), `insert_padded` (`insert padded`: a picture whose own opaque
  pixels hold flat bands on two opposite sides, each at least
  `PAD_MIN_SHARE` of it, `screen_insert.padded_sides` measured once at the
  shape its quad gives it - a card or a letterbox baked in; a one-sided
  band, a header bar, is none. The letterbox a contain fit adds is
  alpha zero, so a contain picture is measured as it is, without it - a
  card baked in is found whatever quad it pads - and a band line is at
  least nine tenths `OPAQUE_ALPHA` opaque with its colour flat on those
  opaque pixels alone: a picture's own transparent margin, a glow fading
  into the surface, is never one. Only a `contain` fit is judged - a
  `cover` one fills the quad -, the picture's alpha-zero rows and columns are
  cut away first, and a band is padding only when its colour is more than
  `BAND_SAME_LEVELS` off the picture's own border on its other two sides, so
  a brushed plate whose top band is its sides' colour is one surface with a
  mark on it, never a card), `logo_duplicated` (an AI shot
  whose composited logo shows twice: see "Logos drawn back"), `logo_lost` (a brand mark showing less
  than `LOGO_MIN_VISIBLE` of its pixels in any frame), `insert_colour_shift`
  (`screen_insert.colour_fidelity`, keyed
  or not: on every frame the quad as the page shows it - the layer over the
  footage, its light laid - is held to the source drawn into the same pixels,
  over the source's opaque pixels that are not foreground: the mean luma's
  move, the black level's - the source's darkest `BLACK_SHARE` (10 %) of
  pixels, as composited less as the source has them -, the contrast's scale
  (5th to 95th percentile), and the colour - a source with a hue of its own
  (`screens.MIN_HUE_CHROMA`) by the angle its mean CbCr turned and its
  chroma's scale, one with none (a dark UI, a grey) by the cast the
  compositing added, its mean CbCr move, and that cast's hue (`hue_name`).
  It fails past `MAX_BLACK_LIFT` (5 % of the full scale), `MAX_HUE_ROTATION_DEGREES`
  (5) or `MAX_CAST` (6 grey levels; `colour_badness` over 1):
  `insert colour shift at t=...: ... is drawn tinted blue (a cast of 9 grey
  levels ...), with its blacks lifted 18% against its source (...; luminance
  +12.0%, blacks +18.0%, ..., contrast 0.71x) - lower its light ...` - one
  action: its light for blacks the footage's light lifts (the one
  setting the fixtures show moving the measure: 0.6 fails, 0.15 passes, 0
  draws the source as it is), else what shows through it (the footage where
  the matte leaves it: the shot again; the source's translucent pixels: an
  opaque source); on `insert_checks.MIN_FAILING_FRAMES` (5) frames at least;
  the worst frame attached; read on `GateMasks.clean`, clear of
  the foreground grown by `FOREGROUND_GROW_PX` (3 px of a 1080p frame) and
  the key's feather - its edge ring blends it with the insert, which read
  blacks +5.5% and contrast 1.47x on the frames a visitor's hair crossed
  the insert, at light 0.15 as at 0.08 - and of the contain bands, and not
  compared at all on a frame whose foreground covers more than
  `COLOUR_MAX_FOREGROUND` (30 %) of the quad, `FrameAudit.colour_skipped`,
  said in the insert's line with its coverage frame by frame,
  `report["foreground"]`) and
  `insert_missing` (`insert_checks.missing`: an insert the render
  cannot draw at all - a mark the template has no file for, an SVG the
  rasteriser drew nothing of - timed at the overlay's start in its scene,
  outside a preview's window none), with `surface_lost` above; and the gate,
  each a finding the audience would see: `insert_alignment`
  (`insert off its bezel at t=...`: the given quad more than
  `surface_refine.ALIGNMENT_MAX_SHARE`, 0.5 % of the frame's width, off the
  bezel found under it - its mean or any one side,
  `insert_checks._off_of` - the insert is drawn snapped to the bezel, its one
  action the quad's corners given: left out so the save measures and stores
  the bezel's, or frame by frame (`track: true`) when the bezel moves under
  it, the frame outlining both quads, on `ALIGNMENT_MIN_RUN`,
  5, consecutive frames, `surface_refine.alignment_failing` - frames
  scattered never fail - and the bezel lock itself holds an untracked insert, whose
  bezel cannot move (`step(prior=insert.track is None)`: a tracker's own
  correction is a real one): a frame's correction moving a corner more than
  `PRIOR_MAX_SHARE`, 0.5 % of the frame's width, from the median of the last
  `PRIOR_FRAMES`, 15, kept is an inner edge the refiner caught - a text line
  painted on the surface, which moved the bezel 1.3 % on one or two frames of
  260 - and that median is drawn instead (`Refiner._prior`, the audit's
  `held`), unless `PRIOR_SETTLE_FRAMES`, 5, frames in a row agree: then the
  bezel did move, and the lock follows. The error of the bezel a held frame
  rejected is kept (`alignment_found`) and the gate reads the worse of the
  two (`insert_checks._alignment_of`), so a bezel that really stepped is
  never hidden while the prior settles), `insert_jitter` (`insert
  jitters: 0.4% between 26.000 s and 26.017 s`: a quad that jumps on one
  frame against the frame before it, which a mean alignment of 0.11 %
  averaged away - `insert_checks.quad_jitter` holds each position of the
  quad drawn (`FrameAudit.refined`) to the ones around it: its displacement
  against the position before less the steady motion through it, half the
  way from the position before to the one after, mean over its corners, a
  share of the frame's width; the frames showing one footage frame draw one
  quad and are one position, so a quad stepping with a footage slower than
  the render never jitters, and only frames drawn one after another are
  compared; past `JITTER_MAX_SHARE`, 0.1 %, on any frame it fails, its two
  frames read back off the encoded video and its region on both cut side by
  side at 100 % as its evidence (`insert_checks.jitter_proofs`), its worst
  jump in the report (`jitter_max`, `jitter_frames`, `jitter_at`) and its
  verdicts (`jitter`); its one action the shot again when its error against
  the bezel held across the jump - the footage itself shook -, else a quad
  on calmer footage or a static one), `insert_residual_key` (`residual key at t=...`: a
  connected region of the key's hue, saturated past `RESIDUAL_MIN_SATURATION`,
  at least `RESIDUAL_MIN_SHARE` of the quad, inside the quad or within
  `RESIDUAL_MARGIN`, 0.5 % of the frame's width, of it after compositing - a
  quad stopping short of its surface -, past `RESIDUAL_FAIL_SHARE`, 0.3 %,
  of the quad on `MIN_FAILING_FRAMES` (5) frames, or on one frame of
  its scene's joins with another (`Placed.joins`, a transition's frames
  and a cut's `timeline.CUT_FRAMES`, 2, each side, `timeline.transition_windows`;
  `data.transition`, named by that frame, never folded into a flatness
  finding nor made a warning by a drawing that passed before): under it a
  few pixels nobody sees, the insert's
  report says it (`residual_advisory`) and nothing fails, and past the margin
  the footage's own key colour is despilled, never a finding (the gate failed
  scenes on 0.05 to 0.6 % of the quad for the footage's screen glow on the
  bezel and its spill on the desk); said by where it sits on the worst frame, the most first,
  `insert_checks.residual_where_text` - "1.18% on the surface running past
  the quad on its right" -, `data.where`; one action, where most of
  it sits (`insert_checks._RESIDUAL_ACTIONS`): the quad's corners for the
  surface running past it, the shot again for the footage's own key colour
  past the bezel, another source for the surface showing through the
  source's transparent pixels or the contain bands (a source of the quad's
  proportions, or one opaque to its edges) - never a wider tolerance, which
  no fixture shows moving a residual the shared masks leave; read on `GateMasks.residual_zone` - the
  source's own pixels of the key's hue (a product UI's green chart, ring or
  bar, which read 0.19-1.03 % "inside the quad" at every tolerance, feather
  and light) grown by `SOURCE_HUE_GROW_PX`, the contain bands filled under
  the source and the grown foreground left out, the first said as
  `source_key_hue`, a fact - but a seam (`screen_insert.key_seams`):
  a straight run of the key's hue down a column or across a row of the
  quad, on `SEAM_RUN_SHARE` (80 %) of its extent there and at most
  `SEAM_MAX_PX` (3 px of a 1080p frame) wide, where the source drawn at
  that very place is not of the key's hue (`GateMasks.source_own`, ungrown),
  is counted whatever mask it falls in (`FrameAudit.residual_seam`) and
  fails the frame whatever its share (`insert_checks.residual_past`), its
  finding naming it the compositing's, its action `nothing` - a 2 px
  full-height green column inside a keyed contain insert's quad had read as
  "the source's own key hue" and passed; `test_video_studio_insert_seam_6667.py`
  draws the case at three source sizes with no such run),
  `insert_flatness` (above: a curved surface on the frames drawn, the
  findings that follow from it folded into it),
  `insert_flicker` (`insert flickers at t=...`: the matte's
  coverage of the quad, or the insert's luma less its source frame's,
  reversing from one frame to the next by more than `FLICKER_COVERAGE` /
  `FLICKER_LUMA` with another reversal within `insert_checks.FLICKER_SPAN`
  frames - `flicker_frames`, read on every frame drawn; a person walking in
  and out turns the coverage once, never flicker; or, more than one
  less `screens.MIN_EDGE_HOLD` of the matte as drawn toggling back over the
  last two frames, the audit's `edge_hold`; or, the insert not drawn on
  a frame the page draws its layer on - `insert_checks.Placed.expected`,
  `drawn_frames`: every frame of its scene for a keyed one, as the page
  activates them - while drawn on the frames around it,
  `insert_checks.missing_frames`, raised by the presence gate only on
  those the encoded frame shows whole and reads it missing on, `data.absent`,
  its frames named, never a warning; both judged
  (`insert_checks.owned_flicker`) on the frames that are its scene's, the
  presence gate's too (`Placed.owns_frame`: those it owns alone, and those of
  its transitions where the page shows it at `PRESENCE_MIN_SHOWN` or more,
  never a transition's first), the report's `flicker_frames` counting both),
  `insert_soft` (`insert soft at t=...`: the insert
  region's Laplacian variance at the output size under `MIN_SHARPNESS` of
  the source's at that size; or, `insert soft: ...`,
  `insert_checks.soft`: fewer than `screen_insert.MIN_DENSITY`, 0.8, of the
  source's own pixels per pixel of its quad at the render's size -
  `screen_insert.density`: the quad in the render's pixels,
  `render.host_size` - the footage's cover rect in the frame, a recording's
  at its camera's closest zoom -, the source scaled by its fit; measured
  once per insert by `render._matte_inserts` onto the payload's `soft`, the
  finding naming the source (`take` / `clip` / `file` / `asset` and its id),
  the smallest size the check passes (`Density.least`), the size at
  one pixel per pixel and, for a recording, the `pixel_ratio` to record it
  at again (`insert_checks.ratio_for`: the smallest that passes on the same
  window); a replaced screen's screenshot the
  same way, `insert_checks.soft_screens`, from the take's
  `params.screen.picture_size` the replacement keeps - a 1280 px recording on
  an 1800 px quad of a 2160p frame was soft and nothing said so; the gate
  asks the same measure before a whole render starts,
  `render_checks._soft_reasons` / `insert_checks.soft_before`: a recording's
  window and pixel ratio (its browser zoom taken out), a shot's size, a picture's stored geometry, a
  replaced screen's record, at the quality asked, without the camera's zoom,
  so a 4K render is refused before it is paid for; `force` renders anyway;
  The same reading at every quality a whole video may be rendered at,
  `spec.RENDER_QUALITIES` - standard, high, ultra -, before any render,
  `insert_checks.soft_by_quality` / `render_checks.soft_qualities`: the
  save's `insert_resolution` advisory and a scene preview's, below - every
  scene previewed green at 1080p was refused by a 4K render, its sources
  recorded again and every scene previewed twice),
  `insert_halo` (`insert halo at t=...`: hue outliers - the key's hue or its
  complement, `HALO_HUE_DEGREES` / `HALO_MIN_SATURATION` - along more than
  `MAX_HALO_SHARE` of the foreground's edge; none when the foreground is of
  that hue itself; the edge pixels whose hue the footage itself has
  there, read against the raw footage frame, left out and said - "12.0% of
  the edge is the footage's own colour there and is not counted"; the
  edge over the source's own key hue or its complement left out too, the
  edge's length said in pixels (`halo_edge`), and a failure on
  `insert_checks.HALO_MIN_FRAMES`, 5, frames at least - a strand crossing
  the edge for a frame or two is none; never a feather as its
  action; read after the edge despill and said by what it leaves -
  `edge spill 7.5% after despill width 2 px on the foreground's edge (350
  px long)`, or `with the edge despill off (despill.edge: false)` -, its
  worst stretch kept as a lossless 100 % crop (`screen_insert._halo_crop`,
  evidence kind `halo_crop`, the report's `halo_crop: {frame, at,
  file_id}`, named on its `Insert` line and attached with the region
  crops), its one action `widen_despill`: `despill.width_px` raised (its
  strength to 1 when lowered), or the despill turned on - the fixtures show
  the halo moving with the width - and, at `spec.MAX_INSERT_DESPILL_WIDTH`,
  the shot again with what stands in front lit apart from the surface;
  The subject's own colour left out of it (`Edge.owned`, above), what
  was counted where the compositor's clamp touched the edge said apart -
  "9.6% of the edge is counted where the compositor's clamp touched it" -
  and the worst frame's edge told apart on the finding and the report
  (`data.parts`, the report's `halo_parts: {frame, at, edge_px, spill, own,
  clamped, hue}`, `insert_checks._halo_parts`, said on its `Insert` line);
  an earlier drawing at another key, light or despill that left the same
  halo says `not a setting: the foreground's own colour (<hue name>) -
  declare it with insert.despill.own_hues or accept`, its action
  `declare_own_hues` - the shot again when most of it was counted where
  the clamp touched, a colour the shot never had),
  `insert_through_foreground` (`insert seen through the foreground at
  t=...`: the source's own strokes drawn across what stands in front, read in
  the frame's pixels - `screen_insert.through_share` on the core of the mask
  the insert is drawn by, the part the key reads clear on a keyed insert,
  never the key's residue the layer draws as the insert there: the high
  frequencies the drawing added (the composited frame less the footage, its
  luma less a Gaussian of `THROUGH_SIGMA_PX`) held to the source's own in
  tiles `THROUGH_TILE_PX` (8 px of a 1080p frame) a side, half of each in the
  core at least, the source holding an edge there (`THROUGH_MIN_SOURCE`), the
  added detail one a viewer sees (`THROUGH_MIN_ADDED`) and the two correlated
  past `THROUGH_MIN_CORRELATION` (0.3); `FrameAudit.through` is the share of
  the core in those tiles, failing past `MAX_THROUGH_SHARE` (0.1 %) on any
  frame, with that frame - the tiles marked - and the worst stretch at 100 %
  (`through_crop`, a render file like the other crops,
  `render_checks.INSERT_STRIPS`); its action `regenerate_shot`: a keyed
  surface for an unkeyed insert, a subject far from the key's colours for a
  keyed one. A keyed frame failing it on the edge of what stands in front
  alone (`screen_insert.on_edge`: every hit within `REFINED_MATTE_PX` (3 px)
  and a tile of its boundary; `FrameAudit.through_edge`) is drawn again by
  `screen_insert.compose` with the matte refined there before it is reported
  (#10573): `refined_front` refines the subject's opacity with a guided filter
  (`guided_matte`, radius `REFINED_MATTE_PX`, `REFINED_MATTE_EPS`) on the
  footage's luma over that edge band, never lowering it, and its pixels past
  `REFINED_OPAQUE` (0.9) join the core the insert is never drawn on; the
  state's edge hold and bezel priors are put back first, so the next frame
  reads them as if the frame was drawn once, and a frame that passes is drawn
  once as before. `FrameAudit.matte_refined` says the retry ran; the finding
  then names the cause: refined and still on the edge, `it is the footage`
  (no key setting clears it); inside the subject, `lower key.tolerance`),
  `insert_ghost` (`insert ghost at t=...`: the mask under
  `LOW_FRONT_CONFIDENCE` (0.5) while what stands in front covers more than
  `GHOST_MIN_FOREGROUND` (2 %) of the quad - dark fur on a dark UI, told from
  the surface by its link to what crossed its edge alone, the insert seen
  through it wherever that link falls short - on more than
  `insert_checks.GHOST_FRAME_SHARE` (5 %) of the insert's frames, or on
  `insert_checks.GHOST_RUN_FRAMES` (6) frames in a row whatever their share
  (dev:video-render-wide-insert-gate: a third of a second of a ghost in a
  long insert, its run named in the message and `data.run`); the frame of its
  lowest kept, its one action as `insert_through_foreground`'s; at 0.5 or
  more, or on fewer frames, the confidence stays the fact of the insert's
  line),
  `insert_matte_halo` (`insert matte halo at t=...`: an unkeyed
  insert's mask of what stands in front larger than its subject or off it,
  read by `screen_insert.matte_read` on every frame drawn whole: inside the
  mask, a band `MATTE_HALO_BAND_PX` (12) deep at 1080p (scaled, at least 2)
  where the insert is not drawn and the footage is the flat surface - its
  median colour clear of the mask, within `MATTE_HALO_TOLERANCE` (12 grey
  levels); a textured surface is never read - opened by the mask's own
  closing kernel so a gap between strokes is no halo; a frame fails past
  `MAX_MATTE_HALO_SHARE` (3 %) of the mask's edge or one region past
  `MAX_MATTE_HALO_REGION` (0.05 %) of the frame, `FrameAudit.matte_halo`,
  `matte_halo_region`; filed on `MIN_FAILING_FRAMES` frames, its worst frame
  kept with the surface left showing marked red, one action: key the surface,
  or the subject clear of the screen's edge), `insert_matte_lag` (the mask's
  centroid against the subject's - what is no surface near the mask - past
  `MAX_MATTE_LAG_PX` (4 px at 1080p) on more than
  `insert_checks.MATTE_LAG_FRAME_SHARE` (5 %) of the frames,
  `FrameAudit.matte_lag`; read only where the footage tells the subject from
  the surface over `MATTE_LAG_TOLD_SHARE` (half) of the mask - a subject dark
  on a dark screen has no lag to read - and 0 when the subject left past the
  mask, the insert drawn over it, is under `MATTE_LAG_UNCOVERED_SHARE` (5 %)
  of it: a mask covering its subject whole lags nothing), and `insert_occluded` (a legible insert -
  `legibility` unset or `legible`, never `ambient` - whose quad what stands
  in front covers, `FrameAudit.foreground_share`, past
  `screen_insert.OCCLUDED_SHARE` (25 %) on more than
  `insert_checks.OCCLUDED_FRAME_SHARE` (10 %) of its frames or past
  `OCCLUDED_PEAK` (40 %) on any: the fix named is to move the subject off
  the screen, `legibility: ambient`, or a smaller subject). The three fail the
  render like the gates above, in preview and render alike; the sweep's
  `failing_frames` names `matte_halo` and `matte_lag`. `DRAWING_VERSION` 17,
  `QUALITY_REPORT_VERSION` 36.
- **One logo on the frame.** `duplicate_logo`
  (`render_checks.DUPLICATE_LOGO`, a `PICTURE_KINDS` failure: the next render
  is refused) reads every scene playing footage (`spec.SCREEN_INSERT_SCENES`,
  a statement over a backdrop) on three frames of the span it owns
  (`render.DUPLICATE_LOGO_SAMPLES`: 15 %, 50 %, 85 %), cut from the encoded
  MP4 of the render and of a scene preview alike (`render._duplicate_logos`).
  The template's logo and mark (the `logo` and `mark` images the render
  gathers) are template-matched over the whole frame at 16 sizes, 3 % to 60 %
  of its width (`logos.brand_marks_on`, the colour or flat-shape correlation
  `locate` uses, from `MIN_MATCH` / `MIN_SHAPE_MATCH`); places sharing more than
  `MARK_SAME_PLACE` (30 %) of the smaller are one. Two places or more file the
  finding (`logos.duplicate_logo_finding`) with the frame, both boxes outlined
  (`logos.boxed_frame_jpeg`), the roles and scores, and one fix: when the
  watermark stands over the scene (`logos.scene_watermarked`: the video has one,
  the scene is not a card carrying the brand), turn it off on that scene;
  otherwise generate the shot again with one logo. A scene's
  `watermark: false` (`spec._SceneBase.watermark`, optional, unset is the
  video's `theme.watermark` - the template's) is planned as `"watermark": false`
  on the scene (only when false: every other document is unchanged) and the
  page's `watermarkAlpha` leaves the corner on it as it does on a card
  (`noWatermark`); the window digest reads it on the scene before
  (`compositor.WATERMARK_FIELD`). `get_video_insert_sweep` frames in red, and
  names, the tiles failing `insert_matte_halo`, `insert_matte_lag` and
  `insert_occluded` (`insert_sweep.GATE_LABELS`; `failing_frames(legible=True)`
  adds `occluded` past `OCCLUDED_SHARE`) and the tile nearest a scene's
  `duplicate_logo` (`insert_sweep.SCENE_FINDINGS`). `QUALITY_REPORT_VERSION` 37.
  - **Every frame read, said (dev:video-render-wide-insert-gate).** The frame
    server audits every frame of a matted insert it draws (never only the
    first, middle and last, which are the 100 % crops kept): the gates above
    run on each. Each insert report carries `coverage` (`insert_checks.coverage`):
    `mode` `every_frame` when every frame that is its scene's its layer is
    drawn on was audited, `sampled` otherwise (a frame read back without its
    audit, a scene range), `checked` of `of` frames, `over` the frames failing
    a gate on their own (`insert_checks.failing_frames`: the frame-by-frame
    gates but the flicker, and `through`, `ghost`, `covered`, `key_spill`),
    the worst of them (`worst_frame`, `worst_at`, `worst_gates`), the longest
    run of failing frames (`run`, `run_at`) and up to
    `COVERAGE_MAX_FRAMES` (240) failing frames with their gates
    (`over_frames`) - integers and gate names only, no picture: the worst
    frame's evidence and the crops stay the only pictures kept. The insert's
    answer line says it as a fact (`its gates read 240 of 240 frames (every
    frame); 5 over a gate, the worst at 0:05.00 (through), 5 in a row at its
    longest`). `insert_through_foreground` carries `of` and `run` too.
    `QUALITY_REPORT_VERSION` 25.
  - **The sweep (`get_video_insert_sweep`, `insert_sweep.py`).** One call
    lays out every insert of a render (its latest finished one, or a
    `render_id`: a scene preview too) as one contact sheet per scene: the
    insert's region box (its crops' `box`) cut at 100 % of the MP4's pixels
    every `step_seconds` (default 1 s, at least 0.1 s, widened to fit
    `insert_sweep.MAX_TILES` (60) tiles an insert, said) through its
    `cropped_within` span, its worst frame always among the tiles, each tile
    labelled with its time and frame, a frame its coverage names failing
    framed in red with its gates. The frames are read off the MP4 in one
    `encoder.extract_frames` pass; the answer carries the table of every tile
    (`at`, `frame`, `gates`) and each insert's coverage, and the sheets as
    files of the conversation. An insert with no region box is said, never a
    blank sheet.
  `key_spill_outside_quad` (`key spill outside the quad at t=...`: a
  keyed insert's key read over its whole footage frame,
  `screen_insert.key_spill` - a connected region of the key's hue
  (`_key_hued`) past the quad and the layer's margin (`KEY_MARGIN`), clear of
  every other surface its scene draws on the footage (`InsertSource.others`:
  a wall's other panes, a second screen) and of the subject's own hues,
  larger than `SPILL_MIN_SHARE` (0.2 %) of the frame, read every few pixels
  (`SPILL_ROWS` rows): a glossy floor reflecting the green panel, a wall it
  lights, which key, despill and residual key - inside the quad or within
  `despill.width_px` of it - never read. It fails on any frame, the whole
  frame kept with its regions marked (`spill_frame`), its largest region's
  box said (`key_spill_box`); its action `regenerate_shot` with nothing past
  the screen in its key colour. Inside the drawn source the key's hue is its
  own: `by design (its source's own colours)` on the insert's line),
  `insert_rim` (`insert rim at t=...`: the key's residue a dark edge
  leaves, a grey band no hue gate reads - render 0f47dcc0 drew Marguerite's
  head over the OpenAEV dashboard with a neutral grey rim 4-12 px wide, the
  halo at 0.0 %. `FrameAudit.rim` is the share of the band `RIM_FROM_PX` to
  `RIM_PX` (2-12 px of a 1080p frame) on the surface side of the part of
  what stands in front the frame's own key reads clear (`KEY_CLEAR_MATTE`,
  `screen_insert.clear_part`), where the source is opaque and not of the
  key's hue, whose chroma is under `screens.MIN_HUE_CHROMA` while the
  source's there is not and whose luma sits between the subject's - its
  nearest clear pixel - and the source's, or the key's own once clamped
  (the residue's floor: a green key's weak channels are darker than most
  dark edges), within `RIM_LUMA_MARGIN`; `rim_edge` that part's edge and
  `rim_light` the share of the rim where the footage's light laid over the
  insert took the source's chroma. It fails on `MIN_FAILING_FRAMES` frames
  past `MAX_RIM_SHARE` (15 %), its worst stretch kept as a lossless 100 %
  crop (evidence kind `rim_crop`, the report's `rim_crop: {frame, at,
  file_id}` beside `rim` and `rim_parts: {frame, at, edge_px, light}`, said
  on its `Insert` line), its one action `lower_light` when that light made
  most of the rim grey and no earlier drawing at another light left the
  same, else `regenerate_shot` - `not a setting: foreground` when one did)
  and
  `insert_cropped` (`insert cropped at t=...`,
  `insert_checks._PIXEL_CHECKS`: a `cover` fit cuts more than
  `screen_insert.MAX_CONTENT_CUT`, 1 %, of the source's content -
  `pictures.content_box`, the picture less the flat margins of its own
  background, `pictures.cover_cuts` - to fill the quad's shape: a sidebar or
  a right column goes; `fit: contain` or a quad of the source's proportions
  is the fix). They are `render_checks.PICTURE_KINDS`, each labelled in the
  editor's render history like every kind `render_checks` stores
  (`video-format.ts` `FINDING_KIND_LABELS`, kept equal to them by
  `test_video_studio_render_checks.py`): a whole render
  with one fails its picture check, its MP4 kept; the next render of the
  revision is refused (`open_findings`) until a newer drawing of the scene
  passed; and a scene preview records them on the scene's drawing
  (`scene_previews[...].drawings[...].picture`), so `render_video` is refused
  (`scene_findings`) while the scene is drawn as it was - a drawing kept for
  the digest it has now; each drawing also keeps the revision it drew
  and what its inserts measured at their key and light
  (`drawings[...].inserts`, `render_checks.insert_measures`:
  the key and the light, the digest of every other field the insert is drawn
  by, `insert_checks.rest_digest`, and each finding's value,
  `report["measures"]`). A finding - the residual key, the colours, the
  halo, the flicker - that an earlier drawing differing in its key, feather,
  light or edge despill (a record kept before it was drawn at the
  default despill) alone measured the same to three decimals says
  `not a setting: <cause> - <what leaves it>` and the two drawings' settings
  (`insert_checks.unchanged`, over `render_checks.earlier_measures`,
  `data.unchanged`), the cause one of `insert_checks.NOT_A_SETTING_CAUSES`:
  `source content`, `foreground`, `footage past bezel`,
  `perspective`, `curvature`, `the foreground's own colour` (its hue
  named after it). Two drawings differing in their declared
  `despill.own_hues` alone differ in their despill.
  The finding's shape: every frame-by-frame finding carries its gate,
  its value against its threshold (`data.gate`, `data.value`,
  `data.threshold`), the mask shares left out on its worst frame
  (`data.excluded`: `source_keyhue`, `coverage`, `contain_band`,
  `insert_checks.excluded_shares`), its worst frame's time and file, and
  exactly one action (`data.action`, `{kind, text}`, its `fix`; the kinds
  `insert_checks.ACTIONS`: `regenerate_flat_screen`, `regenerate_shot`,
  `rerecord_viewport`, `give_quad_corners`, `replace_source`,
  `lower_light`, `widen_despill`, `declare_own_hues`, `avoid_transition`, `nothing`). The alignment and the flatness fail on five
  consecutive frames, the residual key, the halo and the colours on five
  frames, the sharpness under `MIN_SHARPNESS` (0.5) of the source
  (`insert_checks.gate_fails`). What each gate concluded is the report's
  `verdicts` (`insert_checks.verdicts`), kept on the scene's drawing with
  its `stage` (`preview` or `render`, `record_seen(stage=...)`). A render
  finding a gate that the save's measure (`insert_bezels.verdicts`), the
  shot's own lint of the take that plays (`insert_bezels.shot_verdicts`,
  The `frame_qc.SURFACE` record whose `take_file_id` is the file the
  render draws - a take a later step drew over, or an earlier take, is no
  evidence) or the drawing of its scene as it is now
  (`render_checks.insert_verdicts`: the same digest - the same shot and
  spec) passed is a warning that `disagrees with save revision 7: ...` /
  `disagrees with save shot <id>: ...` / `disagrees with preview <id>:
  ...` (`insert_checks.disagreeing`, `data.disagrees`), never what fails the
  render: the stages measure one thing, and the fixtures fail CI on any
  disagreement (F9-F11, the shot's lint included). `finding_line` names the frame file. Beside the
  findings, `insert_checks.reports` reads the same audits as what each
  insert measured - `checks.inserts`, one entry per insert drawn: its
  refined quad (the first frame's), the mean seed error and the worst, the
  frames past the alignment gate and those the prior held
  (`alignment_frames`, `held_frames`), what the layer despilled past the
  residual margin (`despilled: {share, frames, sides}`, the worst frame's),
  the worst residual key - `residual_advisory` when under the gate's share -
  with where it sits on that frame (`residual_where`) and
  the crop of its worst side (`residual_crop: {frame, at, side, file_id}`,
  Attached to the answer with the region crops, so one preview round
  tells the footage's key colour from key leakage), the bands a contain fit
  left and what filled them (`contain_band: {share, filled_with,
  colour}`, said on the `Insert` line: "contain bands 24.8% of the quad,
  filled with the source's own edge colour #181C24") and, keyed, its `fit`,
  its key as set (`key_settings`) and the residual inside its quad on the
  worst frame (`residual_inside`), the flicker frames, the
  lowest sharpness, the worst
  halo, its colours at its light (`colour`: the mean luminance move,
  the worst black lift, hue turn and cast, the saturation farthest from 1,
  the lowest contrast, the frames past the gate), the size its frames were
  measured at (`measured_at`, the audits' `size`: the host's size in device
  pixels the page asked - an ultra render's 3840x2160, a preview's draft) and
  the frame its crops were cut from (`cropped_at`), the frames audited and
  its crops (`{frame, at, word, box, file_id}`) - cut within
  `insert_checks.crop_window` (`cropped_within`, MP4 seconds): its own fade
  in and out, and the frames its scene owns alone (`Placed.owned`,
  `timeline.owned_span`: after its transition in, before its transition out,
  out of the video's fades - the rule `render.window_times` settles a
  preview's frames by), a frame inside each bound; the "last" crop was the
  scene's last frame, inside the transition out, a blend with the next scene
  that measured the transition, not the compositing - which
  `render_checks.insert_lines` says in every answer that formats the checks
  (`Insert Scene x: its screen insert 0 (refined quad [...]): fact: bezel
  found, the quad 0.21% of the frame's width off it ...; by design (light
  0.15): colour at light 0.15: luminance +0.8%, blacks +1.2%, ...; fact:
  measured on 3840x2160 frames; fact: 100% crops of the 3840x2160 frame:
  first frame file ... (cut from 0:06.00 to 0:11.40: the first and last
  frames its scene owns alone, after its transition in and before its
  transition out); fact: its region box [...]`, each measurement with its
  class);
  a residual key's line names where it sits and its side's crop, `its right
  side at 100%: file ...`), and whose crops `render_checks.frame_file_ids`
  counts among the render's files (`render_checks.with_files` naming the
  side's crop by the conversation's copy too). A preview's answer says its
  numbers are its draft's and that a render measures and crops them again on
  its own frames; a whole render's says they were measured on its own.
- **The joins read back, once encoded (`insert_checks.guard_frames`,
  `dropped`).** A render names `GUARD_FRAMES` (4) frames spread over each
  join of a keyed insert's scene with another, among those its layer is
  drawn on - picked over the whole join, so a scene range or a preview
  drawing part of it picks the same ones (`render_ranges` computes them off
  its bundle). On each, the driver keeps where the page placed each insert
  and how much of it the frame shows (`insertRects()`: its opacity less what
  the scenes coming in over it cover of it, and on a wipe the share of it the
  edge has passed - a mask is no opacity, so an insert wiping in is not
  read whole where the scene it leaves still showed; `FrameServer.place`,
  `placed`),
  then has the page draw each of their scenes alone (`window.__xtm.alone`:
  the other scenes hidden, its transition undone, the watermark, subtitles,
  flash and fade off) and captures it as a PNG, never encoded
  (`FrameServer.keep_alone`, `alone`, a begun page's repeated frame kept the
  encoded one; stored with the segment's evidence). After the mux,
  `dropped` reads those frames off the encoded video
  (`encoder.extract_frames`: one seek a run of frames, each picked by its
  number) and `judge_dropped` measures the key inside each quad
  (`screen_insert.key_left`: the residual key's measure, inside the quad and
  `RESIDUAL_MARGIN` around it, less the source's own key hue on that frame):
  on the scene drawn alone on every frame read - on a blended frame a
  key-hued picture of the scene beside it reads as the key: the fixture's
  test pattern did - and on the encoded frame where its scene shows alone
  (`GUARD_ALONE`, 0.98 of it shown: a cut's frames, a transition's ends).
  Past `RESIDUAL_FAIL_SHARE`, or a layer the page placed nowhere (its scene
  drawn alone with no box of it), it fails as `insert_dropped` (`insert
  dropped at t=...: ... on frame <n>, on its scene's join with the scene
  beside it`, `data.transition`, the frame attached), whatever the audits
  said; nothing in the video causes it, so its one action is to render
  again. A guard frame it could not read at all - neither its scene
  drawn alone nor where the page placed it kept for it, a segment an
  attempt before drew, or no picture left - is no measure of an absence: a
  warning, `insert guard could not read frame <n> ... requested N guard
  frames, read M` (`data.unread`, `requested`, `read`), never a failure.
  `extract_frames` clears the PNGs of a pass before it runs, so a frame an
  earlier attempt left in `guard/` is never read for one this pass did not
  yield.
- **Check frames at the render's own size.** Beside the 100 % crops
  of each insert's region, a whole render stores the first, middle
  and last settled frames of every scene with a screen insert or a replaced
  screen (`render.composited_scenes`, `render._check_frames`: `window_times`
  of the whole document, `extract_still` at `max(size)`) as files of the
  render (`frame_file_ids`, deleted with it) - a replaced screen has no
  insert region to crop, and the whole frame shows the screen in its room;
  `render_video` links them as files of the conversation and asks the agent
  to look at them before calling the screens right. A scene preview's three
  frames are cut at the preview's own size
  (`_scene_frames(max_side=max(out_w, out_h))`), never downscaled.

References (recordings, clips, files, decks, a statement's backdrop)
are checked against what the writer may read (`projects.verify_references`);
a recording or an AI shot that failed is still the writer's to use - the
video saves and previews with it, only a render refuses what is not ready.
A medium the STORED spec already names (`held`: the project's spec
on a save and a draft preview, the source's on a duplicate) whose row was
deleted since is no problem either: the preview shows the scene
`failed` with `missing: true` (a recording, an AI shot) and plays the rest,
and the render refuses it naming the scene. A held row that still exists is
judged as before, and a new reference to an id that names nothing is refused.

**Statements.** `StatementScene` is the message in big type of brand
films, event loops and launch films: 1-4 `lines` (90 characters each)
revealed `lines` / `words` / `together`, a `kicker`, a `figure` counted up
from zero when it holds a whole number, `secondary` lines (a proof point, a
translation), `chips`, `align`, `show_logo`, and an optional `backdrop`
(`clip_id` - an AI shot - or `file_id` - an uploaded video or image, the
content type decides) darkened by `dim` under the text, an image moving with
`motion`. `*words*` between asterisks are drawn in the accent colour, in a
statement and in a title or outro title (`compositor_page.textWords`): an
accent opens on an asterisk before a non-space and closes on one after a
non-space (`ACCENT`, injected from `spec.ACCENT_PATTERN`: one pattern for the
page and the save), so `5* rated, 4* loved` stays text. An accent opened and
never closed is drawn as typed, never a refused spec - a stored one
keeps validating -, and `save_spec` adds a note per such statement line
(`spec.unclosed_accents`) naming the word to close. A `\n` in a title
breaks the line (a `<br>`); titles wrap `balance`, subtitles `pretty`.

### Video templates

A video template is a `TemplateContainer` of kind `video`
(`template_containers.VIDEO_KIND`): the Templates library lists, shares,
duplicates, imports and publishes it like a deck template.

- `theme_config` is the deck palette (`presentations.THEME_CONFIG_KEYS`), read
  per scheme by the deck resolver (`resolve_template_theme`, `theme_variants`),
  with the `logo` / `logo_light` / `logo_mark` assets resolved by role. The
  built-in video templates take their deck's palette by reference, so a brand
  colour is declared once.
- `extra_metadata.video` is the rules, `templates.VideoTemplateStyle`:
  `aspect_ratios`, `purposes` (what it is made for, `TEMPLATE_PURPOSES`:
  product_demo, brand_film, event, launch, explainer, social - the "New
  video" dialog suggests a template per kind, the capabilities tool lists
  them), `defaults` (the top of a spec: theme mode / background /
  watermark, transition, captions, voice-over - the watermark's
  corner is a default like the others, filling only an unset
  `theme.watermark`; `templates.template_watermark` reads it, the picker's
  and the capabilities' `template_summary` name it (`watermark`), the
  editor's `Watermark` field shows it as its unset value and offers `None`
  apart, `create_video`'s spec help says `watermark?` is the template's
  corner when left out, and a save of a video drawing none while its
  template draws one says so, `templates.watermark_note`), `scene_defaults` and
  `overlay_defaults` (how a scene type or an overlay type looks, never what it
  shows - an image overlay's `width` and `opacity` included; a scene type's
  defaults may also add the brand's marks as image overlays by their role,
  `{type: image, asset: logo_mark, ...}`, never a file: drawn before
  the scene's own overlays unless the scene draws that mark itself,
  `templates._with_brand_overlays`; the built-in templates add none, their
  watermark already carries the mark), `intro` / `outro` (a title and a closing card added when the video
  has none, `{title}` substituted; the video's own first `title` / last
  `outro` replaces them and takes their look, `_bookend_defaults` - the
  editor's `Customize the opening / closing card` inserts that copy with the
  template's words, `customizeBookend`; no built-in template locks a card
  style; the spec's `template_scenes: {intro?: {duration, kicker}, outro?:
  {duration, kicker}}` (`spec.TemplateScenes`, 1-30 s) sets the length of the
  card the template adds - and only of that one, `templates._card_settings`
  -, so a quality finding on it has a fix: `quality._Scenes.length_field`
  names `template_scenes.<card>.duration` for a card not in the spec as
  written, `duration` for the author's own. Its minimum is said before a
  save refuses a shorter one (an outro passed at 7 s, then 4 s was
  refused "make the scene 5.5 s long"): the layout report's
  `quality.card_minimums` gives every card the template adds the shortest
  length the `reading` finding passes - its reveal, a glance, its words at
  `WORDS_READ_PER_SECOND`, the end it is not shown at, less
  `READING_TOLERANCE_SECONDS`, rounded up to the tenth
  (`_readable_length`, the arithmetic the `rushed` and `reading` findings
  ask their length with) - as a note of every save's quality check: "The
  template's cards, the least each lasts with its words read:
  template_scenes.outro.duration at least 5.2 s for its 8 words (it lasts
  7 s)." (`quality.card_leasts` is the measure, `CardLeast` each card.) The
  least is applied by default where the music needs it (a default
  outro of 5.5 s and intro of 3.7 s, above their 4.9 s and 3.1 s, made a
  video a second too long for its play-once track): when `music.loop` is
  false and the track (`music.playable_seconds`, not while composing) ends
  more than `card_fit.TOLERANCE_SECONDS` (0.5, the `music_short` one)
  before the measured plan, `card_fit.shortened` measures the planned
  document (`quality.measure_document`) and sets each card the template
  adds whose `template_scenes.<card>.duration` is unset to its least - the
  outro first, then the intro, only as many as the gap needs. `save_spec`
  applies the template again, plans again and keeps it when the video came
  out shorter (a narrated card its narration holds does not), writing the
  duration into the authored spec - the render applies the same - and the
  save says it: "The music plays once (loop: false) and ends at 170.2 s of
  the 171.2 s video: the template's outro shortened from 5.5 s to its
  least, 4.9 s, for its 9 words (template_scenes.outro.duration: 4.9) - the
  video now lasts 170.6 s. Set another duration there to keep a card
  longer." No headless browser here: nothing shortened, the `music_short`
  finding says the gap. Its `kicker` (up to 80
  characters; a closing card takes one too) replaces the template's, and
  `null` - kept through `authored_spec` by `TemplateCard`'s serializer -
  means none at all; the opening card's `align: left | center`
  replaces the template's, a closing card's refused - it is always centred,
  `TemplateScenes._outro_centred`), `brand_name` (the name the template's logo
  spells when it is not the template's own name, see "The brand on a card"
  below), `locked` (paths whose template value
  always wins, plus `theme.palette` which drops the spec's colour and logo
  overrides; a locked object leaves to each scene the fields
  `templates.LOCK_OPEN_FIELDS` declares open and its value does not name -
  `scenes.recording.frame` leaves `fill`, which a template turns off
  by locking `fill: false`), `shots` (the art direction of its AI shots, `ShotDirection`,
  `style` - the look, up to 600 characters - and `avoid` - what no shot
  shows, up to `MAX_AVOIDED_TERMS` terms; see "Art direction" under AI clips).
  `validate_video_style` (beside `validate_chrome`) runs on every
  write path - REST create / update / import, the agent's template tools - and
  probes every preset into a valid spec, so a template can never make a video
  unrenderable.

**A dynamic pace (`pace.py`).** `VideoSpec.pace: dynamic` - a short
social cut's, no built-in template's - fills, where the spec leaves them unset
and before the template's own defaults (`pace.with_defaults`, first thing in
`apply_template`, so a template's calm preset never wins over the pace it
asks for): each recording scene's `idle.mode cut`, `camera.pace dynamic`,
`camera.move_seconds 0.5` and `end_hold_ms 300` (`timeline.kept_end`: the
take's hold after its last step cut, read by `build_time_map`'s two callers),
the `auto` transitions' `pace dynamic`, a statement's `reveal words`; `fit:
auto` then also speeds a recording longer than its narration (below). A
scene's own `narration_speed` (0.7-1.3) is said over `voiceover.speed`
(`voice.Line.speed`, `voice.line_speed`: the request digest keys on it, the
voice check's slower retry on the speed it said), and `voiceover.style:
energetic | calm` sends its preset's words (`voice.STYLE_PRESETS`,
`voice.delivery`, in the digest too). A recording scene's `frame.fill`
draws its window edge to edge (`timeline.drawn_frame`: no margin, bar,
corner or halo): the compositor scales the page to cover the whole frame
on every frame and draws the captions over it, never fitting it above the
captions' band, which left the film's backdrop under the page (issue
#10867). A paced transition a scene shows (a canvas zoom, a layout change)
whose frames at the take's rate were more than 30% drawn from keyframes, or
held then cut (`held_not_blended`), is a blocking `synthesised_transition`
finding naming its step and seconds (`scroll_pacing.synthesised_transitions`),
whatever `stepping_motion` grades it. On GPU GL under `fps_fallback: refuse`,
`capture_rate.refusal` refuses a take whose canvas transition is mostly
synthesised even when brief enough to grade advisory: the lane did not
capture it at the take's rate, so the take is recorded again smaller or at
`fps_fallback: fps`, never kept. The same refusal reads the take's paced
events once its pacing pass ran: a transition `synthesised_transitions`
finds (over 30% of its frames drawn from keyframes, or held then cut) refuses
the take on GPU GL under `fps_fallback: refuse`, so the recording lane never
keeps ready a take the save check would flag `synthesised_transition`
(#10869 retest). A `frame.fill` recording raises no coverage finding: the
compositor covers the frame by construction since #10867, so a render made
before it is judged again only by rendering it again.

**A demo told as a product manager tells it (`demo_grammar.py`).**
Demos built on the dynamic preset read as screen readings with a restless
camera: a narration saying what was drawn, a zoom on every click, a
recording slowed to half speed under its narration. The Filigran Product
Video template now presets `pace: calm`, `default_transition {type:
crossfade, pace: calm}`, a static recording camera (`camera {mode: static,
pace: calm, max_zoom: 1.3, move_seconds: 2.0}`: a scene setting `mode: auto`
moves calmly and shallow), 3 s callouts, a centred title card and a product
manager's voice style, and the skill teaches the narration (section 7 of
`video-production.md`). The quality check holds every recording scene to
it, as advisories (`demo_grammar.advise`, from `quality.content_findings`):
`demo_narration` - a UI action said as an instruction (`instructions`: a
verb of `SPOKEN_ACTIONS`, keyed by the `TakeStep.action` it says, opening a
clause and followed by what it acts on, so "open source" or "one click away"
are not), a count of what a graph view draws (`screen_counts`), a name of two
words or more its frames show while it is said (`labels_on_frame`; the
brand's names and the video's title taken out of it, `known_names`);
`demo_camera` - an automatic camera making more than `MOVES_PER_MINUTE` (4)
moves a minute of its scene, one always allowed (`allowed_moves`), or moving
in on a step whose element covers more than `LARGE_ELEMENT_SHARE` (40%) of
the page, from the moves the render draws (`timeline.recording_camera`,
whose `_AutoCamera` records each zoom in and re-frame as a `CameraMove`,
never the way back out); `demo_speed` - a recording played under
`timeline.SLOWEST_FILL` (0.7) to last as long as its narration, by its
`fit: auto` (the speed solved without the fit's 0.5 floor) or its own
`speed`, never one whose scene lasts for the recording, naming the
narration length - whole seconds, the words at the reading pace - that keeps
it at `ADVISED_SPEED` (0.8). The frames a label is read off are read only
for a narration naming something (`frame_moments`: 15%, 50% and 85% of the
narration, on the take), each take's video fetched once and every frame
read in one call of the person's chat model (`text_drift.transcribe`),
kept in `render_cache` (`frame-text`, by the take's file and the second),
at most `MAX_FRAMES_READ` in `READ_TIMEOUT_SECONDS` a check; the render
gate reads none (`check_content(read_frames=False)`), only those read before.

**Authored vs effective.** The project stores the spec AS WRITTEN
(`spec.authored_spec`, `exclude_unset`) and the template it wears
(`template_container_id`: the spec's `template_id`, else the project's, else
the platform default `DEFAULT_VIDEO_TEMPLATE_SLUGS`; none with
`freestyle: true`). `templates.apply_template` computes the effective spec on
every save (validation, the estimate, the notes the tool returns) and every
render: a default fills only what the author left unset, a lock overwrites
and says so, the cards are added with stable unique ids. A brand update
therefore reaches the next render of every video wearing the template; edits
(`apply_operations`) address the authored scenes, whose indexes are the ones
the agent reads. A template the reader can no longer see contributes nothing
(the deck rule), and one whose rules a stored spec now breaks is skipped with
a note rather than failing the read.

**The brand on a card.** A card shows its brand once: its logo. The
template is read with its marks (`templates.load_video_template`, the one
loader of `save_spec`, `effective_spec` and the preview): for each role, the
file the palette names (`logo_file_id` / `logo_light_file_id` /
`mark_file_id`), else the container's oldest image asset of that role, with
its caption (the asset's description). Which mark a card draws is
`spec.brand_marks` - the rule the render, the preview and
`timeline.overlay_file_ids` read too: `logo` (its light variant in a light
video, the video's own logo when it names one), `logo_light`, `logo_mark`
(else the logo). A mark spells the template's brand names
(`templates.brand_names`: `brand_name`, then the template's name) that its
caption or its alt text writes as whole words, case-insensitive
(`templates.names_in`; an address such as `filigran.io/thread` is one word);
the symbol alone - an asset of role `logo_mark` - and a logo with no caption
known spell none. `templates.brand_repeats` lists where a title, closing or
statement card writes a name one of its marks spells (`show_logo`, or an
image overlay of a brand mark or of the template's mark file). Two readers:
`apply_template` leaves the template's kicker off a card it adds when that
kicker repeats the brand (`_drop_repeated_kickers`, after validation; a
kicker the spec sets in `template_scenes` stays), and the quality check
reports every repeat as `brand_repeated`, `dense` - a finding the audience
would see, so the render waits for it - on the template's cards and on the
author's own, naming the fix (`template_scenes.<card>.kicker: null`, retitle
the video, `show_logo: false`, drop the logo overlay). The built-ins declare
their `brand_name` and carry no kicker naming it; THREAD's intro keeps the
edition (`2026`) under the lockup and its closing card says `See you there`.

**The brand on a frame.** The mark is drawn once on a frame. A
statement does not draw its logo while the watermark is on
(`spec.card_draws_logo`: the planner's `show_logo`, and the `show_logo`
`brand_repeats` reads, so a hidden logo spells nothing); a title or closing
card keeps its own, the watermark stepping aside until the card has left
(`watermarkAlpha`). The quality check's audit counts the marks every frame it
plays draws and reports two as "brand mark drawn twice" (see "The check").

Built-ins (`template_container_seed`): `filigran-video`, `thread-video`,
`filigran-brand-film` and `filigran-event-loop` (Copilot only, like their
decks), `xtm-one-video`, `xtm-one-brand-film`, `threat-briefing-video` and
`social-teaser-video` (every mode) - every purpose has one on every platform.
Each carries exactly the assets of its deck (`brand.deck`):
its manifest (`brand_assets/<slug>/manifest.json`) includes the packs the
deck's does - the whole Filigran library for the four Filigran ones, the
THREAD deck's packs for the THREAD one, the Filigran pack's product families
(`"only_roles": ["product_logo", "icon", "diagram", "screenshot"]`) for the
XTM One ones, as the XTM One deck takes them - and lists the marks its cards
and watermark draw (`"pack": "<folder>"`) under video captions of its own.
`get_template` lists them to the assistant (the video studio turns carry
`template_search` for it), for image scenes, statement backdrops,
`compose_picture` layers and the file ids of a `figure` block. Pinned by
`test_a_brand_video_ships_every_asset_of_its_deck`.

### The timeline planner

`timeline.plan` turns the effective spec and its media into the compositor's
frame-exact document (invariant 16: the production decisions are computed,
not prompted):

- **Time maps.** Each recording's kept range is a piecewise-linear map of
  output to recording time: idle stretches (no step active) are compressed
  (`idle.mode` compress / cut / off), steps keep real time, a hold freezes the
  last frame. A skipped step is never active, and the span it waited plays at
  `CUT_SPEED` whatever the idle mode (`_skipped_windows`); a step's
  scroll into view (`scrolled`) is active, so it plays at real speed.
- **Footage fills its scene.** A scene lasts as long as its
  narration; footage shorter than that never spends the difference on a
  frozen frame first. A recording slows the pauses `compress` sped up
  (`build_time_map(fill_to=)`, `_relax_pauses`: each compressed pause gets the
  same share of what slowing it to `RELAXED_PAUSE_SPEED` (2x) would give, never
  below it - a spinner the author compressed still reads compressed, and
  `idle.speed` keeps its meaning; a `cut` pause and a skipped step's wait stay
  jump cuts, the segments carry which is which). An
  AI shot or an upload (`clip`), an animated image and a statement's backdrop
  video play slower (`filled_length`), down to `SLOWEST_FILL` (0.7) of their
  speed - never a clip whose own sound is heard, which would be slowed too.
  Only what is still missing holds the last frame (plus the author's
  `hold_end`, kept whole). The scene lengths are unchanged
  (`max(played + hold_end, narration_need)` either way), so starts,
  transitions, captions and the music do not move; only the time map's shape
  does, which the compositor, the cursor, the camera and the editor's cut
  (`splitScene`, through the planned `time_map`) read. The compositor keeps a
  held frame moving, in the render and the preview alike: from the flat end of
  the time map (`heldSpan`) to the scene's end, the footage drifts in
  (`heldDrift`: `HELD_DRIFT` 2% a second of hold, `HELD_DRIFT_MAX` 8% in all,
  spread over the whole hold and eased in from rest, the pace of an image's
  slow push in) - a clip's and a backdrop's `.shot` wrapper (the footage and
  its screen inserts), a recording's camera zoom (its cursor and callouts
  follow). The author's `hold_end` is a still beat: the planned clip or
  recording carries it, and the drift stops that long before the scene's end,
  keeping the scale it reached. The transform is set only while a frame
  holds: no extra compositing layer otherwise. All of this is a clip's,
  an animated image's and a recording's `extend_mode: zoom`, the default
  (see "Frozen pictures" below); `loop` and `hold` neither slow the
  footage nor drift (`driftSpan`).
- **Auto camera.** It follows the action (`_AutoCamera`):
  the scene opens wide (`establish`, after the transition in), the camera
  zooms in on a step (the element filling 0.42 of the view, a highlighted
  region 0.75 - `HIGHLIGHT_FILL` -, max zoom 1.8), re-frames on every next
  step unless it is already well in view (`FOLLOW_MARGIN`, `MIN_PAN`),
  changes its zoom only past `REZOOM` or when the element would overflow
  (`MAX_FILL`), and pulls back half-way through a pan longer than `ARC_FROM`
  view widths (the keyframe's fifth value, the dip in log of zoom, drawn by
  the compositor's `camera()`). It backs out when the next step is far enough
  for a real wide moment (`min_wide`), right after a step that opened another
  page (`url_before` / `url` differ, `PAGE_HOLD`; no zoom in before that page
  is there), and before a page load or a scroll (explicit, `scrolled`, or a
  step the camera does not frame that opens a page), wide while the page
  moves. A step trimmed away never moves it. `camera.pace` picks a
  `CameraPace` (`calm`, the default, or `dynamic`; `camera.move_seconds`
  rescales its moves): the length of a zoom and of a pan (scaled by the zoom
  depth and the pan distance, never shrunk), the lead and hold around a step,
  the rest between two moves (`settle`), `min_wide`, the shortest worthwhile
  time in (`min_in`: with no room, the camera arrives a little late, and only
  a step it could not reach while on screen plays wide) and `establish`.
  Moves are eased on a smootherstep (`glide`, peak speed 1.875x the mean).
  `static` and `manual` (keyframes) modes; a manual move squeezed by an idle
  compression keeps the length it was written with, up to the pace's zoom in.
- **Cursor and effects.** The recorded cursor path, smoothed; click ripples
  or pulses; highlights and spotlights on step elements; callouts anchored to
  their step's time and box.
- **Pacing.** A scene lasts at least as long as its narration (the measured
  voice, else 156 words per minute); narrations never overlap; captions are
  cut into phrases spread over the voice (and a WebVTT file).

## Render

`render.run_render` (the `video_render` adapter) reads every reference on one
short session, then works with none open:

**The read-back's stage line (issue #9280).** After the draw, the render's
stage at 94 % is written by `render.reading_back` from `ReadbackWhere`:
every `READBACK_SAY_SECONDS` and at the first beat after the read-back moves
to another step, so a short step never leaves the line of the one before. On
the presence frames it says frame X of Y and the time left at the attempt's
own pace (stalled once a frame stands longer than that); every other step
says how long it has run (`running for N min`), never an invented ETA. The
duplicate logo pass runs under its own title (`DUPLICATE_LOGO_STAGE`) and
counts its samples (`sample X of Y`), so a long pass over footage frames is
never shown as the presence frames' last "about 1 min left".

1. **Media.** Recording mezzanines and clips downloaded; a slide scene is
   rendered by the deck exporter at 2x as the requester (a deck they cannot
   open renders nothing) - without the deck's chrome when the scene says
   `chrome: false` (`render_deck(hide_chrome=True)`: the
   `data-export-no-chrome` attribute on every slide, answered by
   `presentation.css`); images (an SVG sized from what it declares),
   the logo, the mark and the music bed. A figure scene's block is resolved
   here (`figures.source_from_deck` / `source_from_spec`) into the figure
   page's document (`figures.figure_document`), drawn after the first plan
   (see "Figure scenes" below).
2. **Voice-over** (`voice.py`): each narration spoken through the speech
   capability (see "Speech generation" below), as the requester
   (`bind_llm_caller`, see "Jobs act for their person" below), with the
   narration before and
   after it and the video's pronunciation lexicon said through the strongest
   mechanism its model offers (`lexicon.render`, see "Pronunciation lexicon"
   below), through
   the voice chain (`encoder.condition_voice`, see "Music and the mix" below)
   and cached in object storage by the digest of the provider, model, voice,
   style, speed, spoken text, neighbours and chain version - then heard back
   (`voice_check.speak_checked`, see "Audio and picture checks" below).
3. **Frames** (`compositor.py`, `compositor_page.py`): a self-contained page
   in headless Chromium draws every frame as a pure function of time (cards,
   the browser window, the camera, the cursor, overlays, transitions, the
   `mosaic` backdrop in the template's six series colours, captions, the
   watermark), its frames and images served by a route on
   `https://studio.render`, each frame captured over CDP and piped to ffmpeg.
   **Pages.** The frames are cut into segments over the whole range
   (`compositor.plan_segments`: an attempt on another number of pages keeps
   every segment an earlier one closed whole), each page encoding its own;
   the segments are concatenated without re-encoding.
   **The pages share the frames by what they cost
   (`page_schedule.py`).** The 22 Oct 2026 ultra render of a 136 s video
   (8,462 frames, 8 pages) drew its 7,100 plain frames in about 15 min, then
   the 780 frames of its two screen-insert scenes for over 40 min on the 2
   pages whose runs held them, 6 pages idle and the render "running" at
   81-94% with no end time: the segments left were cut into one contiguous
   run a page by frame count (`page_runs`), and an insert frame costs a page
   many times a plain one. Each frame is now priced by its kind
   (`FrameCosts`: what this attempt drew of the kind once it drew
   `MIN_DRAWN_FRAMES` of it, else the prior `render_frames(frame_costs=...)`
   is handed - the previous render of the same video's insert frames, the
   platform's median for the rest, `render_preflight.page_costs` -, else the
   kind's default share), `FramePrices` counting each kind over any range at
   once. The segments left are shared out in contiguous runs of the same
   cost, a segment a run ends inside cut on that frame (`cost_runs`), and
   each page draws its run, then takes what the others have left
   (`PageWork.take`): the last piece of the run with the most work left that
   no page started, else the second half of the piece with the most work
   left, cut on a frame so both halves end together, the taker's encoder and
   pre-roll counted on its side - in the tail the unit of work is a frame
   range, never a whole scene. A piece's end comes down under the page
   drawing it (`Piece.end`, read before each frame; the segment is named by
   the frames it ends with once closed), and a cut leaves each side
   `MIN_SPLIT_FRAMES` (2) frames and the taker `MIN_SPLIT_SECONDS` (5) of
   frames: a page idles only once what is left is not worth an encoder. A
   page that starts a piece anywhere but where it left off drops what a
   matted insert carried from frame to frame (`FrameServer.jumped`) and,
   inside a matted insert (`compositor.matted_spans`: every frame its layer is
   drawn on, a keyed insert's whole scene), first draws the
   `PREROLL_FRAMES` frames before it uncaptured - as many as the bezel lock's
   temporal prior takes to agree with itself (`surface_refine.PRIOR_SETTLE_FRAMES`),
   more than the matte's median reads behind - their audits dropped, their
   seconds the page's, never a kind's (a static insert's bezel is
   locked once and a frame drawn ahead is kept on the worker's disk, so the
   page that owns it reads it back). The render logs the pieces, the
   whole ones taken, the cuts, the frames drawn ahead and each page's
   seconds without a frame left while another drew (`DrawProgress.idle`).
   The status line says the frames no page started, the frames a second
   over the last minute (`page_schedule.Rate`) and the time the pages take
   for the rest, the schedule replayed at the costs measured by then
   (`DrawProgress.status`: "Drawing frames (87%): 1,104 left, 7.4 frames/s
   over the last minute, ETA 17:42 UTC (in about 2 min 30 s)" - a time, the
   line being read later than it is written). Replayed at those 22 Oct
   costs (`tests/test_video_studio_page_schedule.py`), the render took 57
   min, a page idle up to 47 min; under the schedule 26.6 min, none idle
   more than 13 s. `test_video_studio_render_tail_chromium` draws the
   render fixture's two insert scenes at ultra on four pages, for real, and
   holds every page to 60 s without a frame while frames remain. An x264 encoder at
   2160p `slow` holds its 50-frame lookahead (2.06 GiB on 1 thread, 2.61 GiB
   on 8, with ultra's keyframe every 120 frames, which caps the lookahead -
   1.35 to 1.91 GiB at 20), and each page's encoder runs on its
   page's share of the CPUs (`render_preflight.encoder_threads`: the CPUs
   divided by the pages, one at least), never on every CPU of the worker.
   `render_preflight.layout` draws on the pages `VIDEO_STUDIO_RENDER_PAGES`
   asks (0, the default: the CPU quota less the one the worker's own jobs
   keep, `auto_pages`), never more than the worker's CPU quota nor
   than the memory ceiling holds - each page at its measured peak and its
   margin (`memory_budget`, "Pages from a measured peak" below) where one was
   measured, within 90% of the ceiling for the per-pixel model elsewhere:
   on an 8-CPU worker an ultra render draws on 4 pages within 16 GiB for a
   video without screen inserts and 3 for one with them, a 1080p one on 7,
   and a scene preview on `PREVIEW_PAGES` (2) at most, the pages its own
   ceiling is sized for (`render_process.preview_memory_ceiling`). Several pages feeding one
   encoder in turn was measured and left out: a page is a pure function of
   time to within its raster history only (frames drawn after others on
   another page differ from one page's at PSNR 42 to 50 dB), which pages
   drawing in turn would spread to nearly every frame. Presets: draft
   720p30, standard 1080p30, high 1080p60 (default), ultra 2160p60. Before a
   whole render is queued,
   `render_preflight.preflight` estimates it from its plan: its
   frames at the rate one page draws them (weighted pixel-frames a second of
   a page with its CPU, `PAGE_CPUS`, scaled to the CPUs each page has on the
   worker, times its pages), measured on this platform's last whole renders
   at that quality (`measured_rates`, from `VideoRender.measured` - the
   pages and CPUs a render drew on and its frames in how long,
   recorded by every render, migration `v6155rate01` - the median of the
   last 20; the other qualities' when none was measured at it), else on the
   slowest CI ultra run (`DEFAULT_PAGE_RATE`: 2.15 frames a second of 2160p
   on two pages of a 2 vCPU runner), its figure frames on one page
   (`FIGURE_SPEED`) - less the figures the reuse index serves (below) -, its
   other stages at their measured seconds per second of
   video; and its memory (`memory_needed`: the processes' base, per page its
   Chromium, screenshots and decoded frames and its encoder's lookahead,
   `ENCODER_BYTES_PER_PIXEL` by x264 preset) against the ceiling the worker
   publishes with its pages and CPU quota (`publish_worker`, from the render
   adapter's `periodic`: Redis, `xtm:video-studio:render-worker`, for an
   hour; `VIDEO_STUDIO_RENDER_MEMORY_MB` when none did). `render_video` and
   the route's `preflight` say it with every input it is made of
   (`Preflight.inputs`): "Expected to take about 46 min: 7,695 frames at
   3840x2160, 0.93 s a frame on each of 4 pages (8 CPUs) - the CI
   measurement: no render of this platform measured yet, plus about 17 min
   for the figures, the media, the sound and the checks; ...". The source of
   the rate is `Rates.source` (`measured` at that quality, `scaled` from the
   others, `default`). The render keeps it (`VideoRender.preflight`: the
   frames, `frame_seconds`, `rate_source` and `basis`, the pages, the CPUs,
   the duration on one worker and with its ranges, `cap_seconds`,
   `cpus_needed`), and `get_video_jobs` says it beside what the render
   measured (`said_of`). `video_studio_capabilities` gives each quality's
   pages, memory ceiling, hours, `max_ranges` and `max_duration_s` before any
   render is asked (`render_preflight.limits`). Past
   `RENDER_DEADLINE_SECONDS` (4 h) on one worker, the render is drawn in
   scene ranges (`ranges_for`: as few as keep each within the 4 h, at most
   `VIDEO_STUDIO_RENDER_MAX_RANGES`, one a scene at most; see "Scene ranges
   across workers" below), the cap applying per range. Unsplit
   (`VIDEO_STUDIO_RENDER_MAX_RANGES=1`), an ultra render may run 8 h
   (`ULTRA_WALL_SECONDS`, its execution's deadline): it closes a segment every
   10% of its frames at most, kept beside it, and a new attempt resumes from
   the last one; past 4 h the estimate says so and refuses nothing. Past the
   ceiling even on one page, a page frame slower than half the stall window,
   or a range (or the unsplit render) past the hours its quality may run (4
   h, 8 h for ultra), is a `preflight` reason of the quality gate - refused,
   `force` overriding and audited - unless this platform already rendered
   that quality whole, whose evidence beats the estimate: then it is said
   only. An estimate that cannot be made never stops a render. What a render
   is short of is given to the worker first: a problem says the
   memory to give (`render_process.memory_to_give`: the
   `VIDEO_STUDIO_RENDER_MEMORY_MB` budget, or the container limit whose share
   holds it) or the CPUs that bring it under the cap (`needs`: the fewest `C`
   whose `auto_pages(C)` pages on `VIDEO_STUDIO_RENDER_PAGES=0` draw it in
   time, with the memory ceiling when the worker's holds too few), as does a
   render stopped at its memory ceiling. Then, in the same message, the
   refusal offers the highest quality that fits this worker now
   (`highest_fitting`) as the person's choice: an agent asks, never
   renders at another quality on its own.
   **A screen insert's frames, drawn once per worker.** A 10 s scene
   with one screen insert took about 12 min to preview at 1080p (4.26 s a
   frame on 2 CPUs; a recording scene of the same length, under a minute):
   every page redid the insert's whole pipeline on every frame, and a page
   starting elsewhere, an attempt run again or the next preview of the same
   revision redid it all. A frame of an insert is a pure function of what its
   pixels are drawn from (`compositor.insert_drawn_by`: its source and
   footage files, quad, fit, key, light, glow, track, box, push, despill,
   subject and `screen_insert.DRAWING_VERSION` - never when it shows nor
   what the findings call it) and of the frame (`insert_frames.frame_name`:
   the source frame, the footage frame under it, the host's size, a push's
   scale). So the frame server keeps every frame it draws on the worker's
   disk (`insert_frames`, `xtm-insert-frames/<key>/` beside the working
   directories, the key with the digest of the modules a frame is drawn by,
   `code_digest`): its PNG and its glow's, its audit (`FrameAudit.as_dict`)
   and the pictures a failing audit shows. Any page of any render process of
   the worker reads it back instead of decoding and composing - the pages of
   a render, an attempt that runs again, the next preview, a whole render at
   the same size -, its audit recorded for the output frame as if drawn; a
   frame drawn uncaptured before a piece (no evidence written) is drawn again
   for a page that writes evidence when its audit fails. The folders are held
   within `VIDEO_STUDIO_INSERT_FRAMES_MB` (1024 by default, `0` keeps none;
   never more than a quarter of what their disk has free; the least recently
   used first, `insert_frames.bound`, every 256 MiB written and with the
   working directories' sweep) and removed after `KEPT_ON_DISK_SECONDS` (6 h)
   unused (`sweep_disk`): they share the worker's `/tmp` with the renders'
   working directories, so a Kubernetes `emptyDir` size limit holds both. A worker reads back
   only what it drew: a preview routed to another worker draws again. The
   render logs the share read back ("N screen-insert frames read back from
   this worker's disk, M drawn: X% reused") and records it
   (`measured.insert_frames`, `DrawProgress.insert_frames`;
   `render_preflight.said_of`: "Its screen inserts: N frames read back ...").
   A frame whose inserts were all read back prices the attempt's own schedule
   at what it costs (`DrawProgress.costs`) but never what the next pre-flights
   read (`DrawProgress.timed(reused=)`: `kinds()` and `scenes()` leave it out),
   and a render that read any back is no measure of the platform's whole rate,
   its stages or its estimate's error (`render_preflight._read_back`). What a
   frame costs when it is drawn was cut too: a static insert's bezel is
   locked once, on its first footage frame (`screen_insert.Lock` /
   `static_lock`, the keyed one's `Fill` already; only `track: true`
   finds it again frame by frame), each footage frame's raw matte is read
   once whatever output frames show it (`Matting.reading`: the frame read
   ahead for another frame's window is that frame's), a still source on a
   locked quad is warped once (`Matting.warp_of`), and a page reads the
   footage frames behind the one it draws that its state has not read
   (`FrameServer._footage_behind`, never before the insert comes in), so a
   frame comes out the same whichever page draws it after whatever it read
   back. `test_video_studio_insert_preview_chromium` (the `Video Studio ultra
   render` workflow) previews a 10 s clip with one static insert, keyed and
   not, at 1080p on two pages held to two CPUs: under 2 s a frame, and the
   next preview reads at least 90% of its insert frames back. On a 24-thread
   desktop held to two CPUs the first preview drew 0.81 s a frame for the
   keyed insert (0.87 before) and 0.40 s for the other (0.44); the next drew
   0.13 and 0.12 s, every insert frame read back, the whole preview in 27 s
   instead of 135.
   **Pages from a measured peak.** A 4K render of 21 Oct 2026 drew
   on 3 of 8 pages, the pre-flight holding each page to a per-pixel model
   (3.7 GiB with one video insert) and advising a 37 GiB container. One page
   drawing 2160p frames is now measured: the 60 s mixed video of
   `tests/video_studio_render_fixture.py` (a title, a recording with a video
   insert, a green screen with a keyed insert, a statement over a clip, a
   picture, a clip with a video and a picture insert, a closing card), every
   insert matted as a render mattes it, drawn on one page while the process
   tree is sampled four times a second by its proportional share, as the
   supervisor counts it (`tests/video_studio_memory.TreeSampler`: the page's
   renderer, its encoder and decoders, and what the render process and the
   Playwright driver grew by for it). It peaks at 4.6 GiB with an insert on
   screen and 3.7 GiB with none (its encoder on 8 threads: encoder 2.4 GiB,
   frame server 1.3 GiB, renderer 844 MiB, driver 466 MiB), kept in
   `data/render_page_peak.json` (`render_preflight.page_peak`). The model was
   low for a page composing inserts, not high. A layout holds each page to
   that peak and `PAGE_MARGIN` (20%, `page_budget`), scaled by the page's
   pixels (one short side: 2160), its encoder's threads
   (`ENCODER_THREAD_BYTES_PER_PIXEL`, 80 MiB a thread at 2160p) and the
   inserts it shows past the fixture's (`InsertLoad`, the model's per-insert
   bytes); a page with no insert on screen is held to the plain peak. The
   other qualities keep the per-pixel model until measured. The
   `Video Studio ultra render` workflow measures it again
   (`test_video_studio_page_peak_chromium`: 10 frames a second of the 60 s,
   600 frames, encoded with ultra's 120-frame keyframe interval, under
   `MALLOC_ARENA_MAX=2` as a render process runs) and fails when the
   committed peak is outside its margin either way, logging the record to
   commit. What a page holds was cut on the way: each frame goes to
   its encoder a chunk at a time (`FramePipeEncoder.write`, 1 MiB: the
   pipe's transport never buffers a copy of a 20 MiB PNG) and its base64
   text is dropped once decoded; when a scene ends its page's renderer is
   told to collect its garbage and purge its caches
   (`compositor._release_page_caches`: `HeapProfiler.collectGarbage`,
   `Memory.simulatePressureNotification` critical - the renderer went from
   835 to 327 MiB at its peak on a 20 s run) and the render process gives
   its freed heap back (`resources.release_freed_memory`, glibc
   `malloc_trim(0)`), and a render process runs on two glibc heaps
   (`render_process.RENDER_ENVIRONMENT`, `MALLOC_ARENA_MAX=2` unless the
   worker sets its own: each thread composing an insert kept a heap of its
   own). Together 5.3 GiB became 4.6 at the insert peak and 4.3 became 3.7
   with none, at the same speed. The answer says, beside the pages
   (`Preflight.peak_words`, the memory note): each page's peak and where it
   comes from, its budget with the margin, the pages opened of the pages
   wanted, and the time on them against the time on every page
   (`full_pages_seconds`), with the memory to give for every page. A render
   that would take longer than `VIDEO_STUDIO_RENDER_SPLIT_MINUTES` (30) on
   the pages its memory opens is split into scene ranges across the workers
   there (`worker_presence.count`, at most one range a worker; `ranges_for`
   with `within`), not only past the 4 h a render may run; with one worker
   it is drawn whole (`split_reason`: `cap` or `split`).
   **An estimate by what its frames show.** Estimated 30 min, it took
   47: the model was a per-frame average. A frame is now of a kind
   (`FrameKinds`, `FRAME_KINDS`: plain, in a transition - the page draws both
   scenes -, with a screen insert, both), read off the plan's transitions
   and inserts (a pre-flight's plan places no insert: `insert_spans` reads
   them off the spec). Each render records its frames by kind with the
   page-seconds they took from drawing to the encoder taking them
   (`DrawProgress.kinds`), each page's run - its segments, its frames'
   seconds, its own (`DrawProgress.runs`) -, each stage's seconds (`_stage`
   timings: `media`, `figures`, `inserts` - quads found, followed, matted,
   a stage of its own now -, `frames`, `audio`, `mux`, `checks`, `store`),
   its whole time and its inserts' time on screen (`measurement`). The
   pre-flight then prices each kind at what a page measured for it
   (`Rates.kind_rates`; `DEFAULT_KIND_COST` against `DEFAULT_PAGE_RATE` until
   measured: at 2160p a plain frame took a page 0.08 s, one in a transition
   0.13 s, one with a matted insert 1.8 s) - a screen insert's frames at what
   the previous render of the same video measured, whose cost depends on the
   insert far more than on the platform (`video_kind_rates`, its newest
   whole render that drew enough of the kind, at any quality, a rate being
   per weighted pixel; `rates_of`, `Rates.video_kinds`, the answer saying
   "those with a screen insert at what the previous render of this video
   measured") -, replays the compositor's schedule (`page_run_seconds`:
   `plan_segments`, then `page_schedule.simulate` of `PageWork` - its runs of
   the same cost, the frames left taken by the pages that are idle, its
   screen inserts warmed as if matted) - each segment at what a page spends
   on one beside its frames (`segment_per_megapixel`: an encoder opened and
   flushed, 4 s at 2160p) - and takes the slowest page; adds the inserts' preparation per second of
   insert on screen and the check stage per frame of a million pixels
   (0.00064 s: 30 s of 2160p60 watched in 9.5 s); plans each narration at
   its spoken length where it was spoken (`preview.spoken_lengths`, the
   editor's own reading of the voice cache), so the frames counted are the
   render's where its voice exists (a frame is drawn once: pages' runs and
   scene ranges never overlap; the 4K render drew 7,714 for the 7,317 its
   pre-flight planned at the reading pace); and counts pages fewer than
   half the CPUs (`contended`)
   at the rate contended renders measured, `DEFAULT_CONTENTION` (0.64, that
   render's own ratio) of the others' until one did. A render that lumped its
   checks and inserts into its other stages is counted once.
   Every estimate keeps its model (`ESTIMATE_MODEL`) and its raw seconds;
   the next reads the last five of the same model at the quality whose
   renders drew every frame in one attempt (`Rates.errors`: what each said
   against what its render took, `total_seconds`) and is multiplied by the
   median of their ratios, within 0.5 and 3 (`Rates.correction`), the answer
   saying them ("Its last 3 ultra estimates said 30 min, 20 min and 15 min
   for renders that took 47 min, 25 min and 17 min: this one is corrected by
   x1.25, their median."). `test_video_studio_estimate_chromium` renders the
   fixture for real, reads back what it measured as the next pre-flight
   reads a render's row, estimates another mix of its scenes and holds the
   estimate within 20% of the time that render took.
   **An estimate by scene type, quoted from the slowest.** An ultra
   pre-flight said 13 min for 8,462 frames; at 60 min the render stood at
   94%: two clip scenes with a screen insert drew at about 1% per 8 min, and
   a frame kind's cost is a median over every render of the platform. Each
   frame is now also of a scene type (`render_preflight.scene_class`, read
   off the spec by `scene_classes` for the pre-flight and the render alike):
   the scene's type, `+clip` / `+file` for a statement over a shot or a
   file, `+insert` with a screen insert - `clip+insert`, `statement+clip`,
   `figure`. Every render and every scene preview records what a frame of
   each type took a page (`DrawProgress.scenes`, `measured.scenes`), and the
   figures stage its frames and seconds. `measured_rates` reads both - the
   last whole renders and the last ready previews, at any quality, per
   weighted pixel, each turned to the regime of the rate returned
   (`_in_regime`) -, so the preview that drew a clip scene with an insert
   prices it for the whole render (`Rates.scene_rates`, `figure_rate`). The
   estimate weighs each frame by its scene type's rate
   (`Rates.frame_rate`), its kind's only for a type never measured here
   (`UNMEASURED_SCENE`), times the figures stage at its measured rate (else
   `FIGURE_SPEED`), counts the CPUs a refusal names on the frames the video
   has, holds its slowest frame - never its average one - against the stall
   window, and quotes its slowest scene type: "8,462 frames at 3840x2160, its
   slowest scenes (clip with a screen insert, 1,240 frames) at 5.60 s a
   frame on each of 4 pages (8 CPUs) - measured on 2 renders or previews of
   this platform", then every type by its cost (`Preflight.scenes_line`).
   `ESTIMATE_MODEL` is 3: the errors of the per-kind model correct none of
   these. `get_video_jobs` says, once a render is drawn, what a frame of
   each type took (`render_preflight.scene_timings`).
   **An estimate from the video's own render, its ETA from its frames.
   ** An ultra pre-flight said about 32 min (1.63 s a frame,
   corrected by x1.43 from one render) for 8,462 frames on 8 pages; its
   status line measured an ETA 1 h 19 min after the start, and the previous
   render of the same video, same revision, had taken 1 h 6 min for its
   frames alone: each scene type was priced at the platform's median across
   videos, and the video's own insert kinds lost to it in `Rates.frame_rate`.
   `rates_of` now reads the newest whole render of the video at the same
   quality that timed its scene types (`own_render`, `OwnRender`): every
   scene type it drew is priced at what it measured, its stages and its
   figures stage too (`with_own_render`, before the platform's median), and
   its pages' parallel efficiency (`parallel_efficiency`: its frames'
   page-seconds over its pages times its frames stage's seconds - its frames
   a second over the whole run divided by its pages, against what a page
   took for a frame) shares the new frames' page-seconds out (`spent /
   (pages x efficiency)`, never under the schedule replayed), before any
   correction. A scene type the video's renders never drew is priced at what
   the whole renders at the quality on the same worker shape measured
   (`measured_rates(shape=...)`, `Rates.scene_here`), before the median
   across qualities and previews. The correction is read per worker shape
   (`Shape`: pages, CPUs, memory ceiling to 256 MiB) and per kind of history
   the estimate is priced from (`priced_from`: `video`, `shape` or
   `platform` - an error the platform's medians made never corrects an
   estimate made from the video's own render), over the last 5 such renders
   among the last 60 at the quality (`estimate_errors`, `Missed`), and the
   answer names them ("Its last 3 ultra estimates on this worker shape (8
   pages of 9 CPUs, a 64.0 GiB memory ceiling), priced from its video's own
   last render like this one - renders `1a2b3c4d`, ... - said ..."), or says
   it is not corrected. `ESTIMATE_MODEL` is 4. Once `PACE_SHARE` (10%) of
   the frames an attempt draws are drawn, the status line's ETA
   (`DrawProgress.pace`: the frames left at what each kind costs by then)
   and the pre-flight's checks after it are kept on the render's `preflight`
   as `pace` (`render.PaceNote`, every 30 s at most; a new attempt drops the
   last one's, `unpaced`), and replace the pre-flight's estimate in the
   render card (`serialize_render`'s `eta`, `render_preflight.eta_of`:
   `preflight` from its start, then `measured`) and in `get_video_jobs`
   (`said_of`: "Measured on its first 846 of 8,462 frames ...: this replaces
   the pre-flight's estimate of about 32 min"). Each estimate keeps what a
   page takes for a frame of each scene type before its correction
   (`scene_raw_seconds`), and a finished render's answer sets its own
   measure against it ("clip with a screen insert 5.41 s (3,062 frames,
   against 1.20 s estimated: x4.51)"): what the next pre-flights price that
   scene type at. `test_video_studio_preflight_pace_6372.py` holds the
   second ultra pre-flight of a video within 20% of its first render's time
   and switches a render's ETA once its first tenth measures twice the
   estimated pace; `test_video_studio_estimate_chromium` estimates its
   fixture again from its own render, drawn for real.
   **Where they live.** The ETA its pace gives (`Pace`, `paced`,
   `unpaced`, `eta_of`, `pace_line`, `PACE_SHARE`) is `render_pace.py`'s;
   the history a pre-flight reads (`Shape`, `Missed`, `OwnRender`,
   `parallel_efficiency`, `own_render`, `estimate_error`, `estimate_errors`,
   `read_back`) is `render_history.py`'s, which reads the pre-flight's own
   measures (`render_preflight.measured_scene_rates`, `stage_rates`) where
   it reads them - the pre-flight imports it, never the other way at import
   time. `estimate_errors` filters in SQL - the quality, the estimate model,
   the shape the estimate was made for and drawn on, the history it was
   priced from - and selects only what an error is made of, every row
   checked again as it is read; a malformed row's pages or CPUs never drop
   the history (`measured_rates`' `on_shape` and `regime_of` read them as
   `estimate_errors` does). `test_video_studio_render_history_postgres.py`
   reads `rates_of`, `own_render` and `estimate_errors` on real rows.
   **A pace under the pre-flight's, explained (`render_pace.py`).**
   An ultra render drew at 1.3 to 3.0 frames/s where its video's previous
   render, same scenes and inserts on the same 8 pages, drew at 7.23: 2 h
   42 min projected for the 28 min its pre-flight said, and nothing said why.
   `PaceWatch` reads each whole render's progress (`render.frame_progress`):
   the frames drawn over the last minute against what the pre-flight priced
   those very frames at (`Priced`: each scene type's `scene_raw_seconds`
   corrected, times the overhead its average frame adds, on the pre-flight's
   pages), so a slow scene type it priced slow never reads slow. Under
   `SLOW_SHARE` (half) for `SLOW_SECONDS` (2 min), the status line names
   the cause within the stage's 200 characters (`short_words`) and the
   breakdown is kept on the render's `preflight` as `slow` (`PaceNote`), said
   whole by `get_video_jobs` (`slow_words`): each scene type's cost a frame
   so far against its price, named when it is `OVER_COST` (2x) over it and
   2x what the others took against theirs; the screen-insert frames read
   from the worker's cache; the jobs the same worker holds a lease on
   (`neighbours_of`: scene previews, other renders at their stage - their
   checks, their music -, AI tracks and shots, any other adapter); the CPUs
   its process tree got (`resources.tree_cpu_seconds`) and their share of
   what the container used (`resources.cpu_stat`). Under `CHECK_SHARE` (a
   third) for `CHECK_SECONDS` (5 min), the worker's CPU throttling over that
   span and the render lanes (`lanes_of`: running here against the slots,
   on all workers, waiting) are checked once and said in the stage from then
   on. The breakdown is the render's `measured["pace"]`: its time corrects
   no estimate (`render_pace.explained`, `_error`), and drawn on a shared
   worker (`shared`: jobs beside it, its pages under 75% of the CPU used, or
   throttled 10% of the periods) it prices its video's next pre-flights with
   the scene types it found over their cost only (`learned_scenes`,
   `OwnRender.of`, `OwnRender.shared`), its stages not at all. A scene type
   the video's own last render does not price - a scene added since, or one
   drawn on a shared worker - is priced at the platform's measure of its
   type and said (`Preflight.unlearned`, `unlearned_words`: "Not learned
   from this video's own history, priced from the platform's instead:
   figure (1,200 frames, which its last ultra render `1a2b3c4d` did not
   draw) - measured on 4 renders or previews of this platform").
   `test_video_studio_render_pace_6405.py` names a scene type 5x over its
   cost, counts two previews on a shared worker and checks it once, and
   prices a figure the video never drew from the platform's class.
   **A render's CPUs beside the preview lane (`cpu_share.py`).** The
   next ultra render (8 pages, 8 CPUs) drew at 0.30x its pre-flight, its
   pages getting 3.7 of the worker's 8 CPUs (46% of the CPU used) beside 2
   scene previews and a recording, 12 more previews queued. Every job ran at
   the worker's priority, and the kernel shares a busy container's CPUs by
   runnable thread. Now a scene preview's process starts `PREVIEW_NICE` (10)
   steps below the worker's priority (`render_process.nice_for`,
   `resources.niced`: `nice -n`, inherited by its Chromium and encoders), and
   so does a recording's encode (`runners.run_take`); a recording's capture
   keeps the worker's priority, as it follows a live page in real time. A
   whole render - or scene range - reserves its pages' CPUs on its worker
   (`render_process.cpus_reserved`, from the pages its layout event names),
   and the worker's preview slots shrink to what the rest holds
   (`preview_slots`, `PREVIEW_SLOTS_BESIDE_A_RENDER` (1) at least, so a
   preview still never waits for a whole render): the preview adapter's
   `slots_held_back`, a generic `OrchestrationAdapter` hook the reconciler
   subtracts from a dedicated budget (`_available_slots_for`). The preview
   room each worker publishes counts its free slots so, and the queue line
   says the slots held back (`preview_routing.LaneWorkers`). `cpu_share.shares`
   models the kernel's sharing (each job as many threads as the CPUs it
   uses, weighed 1024 / 1.25^nice): beside two previews (their pages and
   encoders, 4 CPUs each) the render's pages keep 90% of the CPUs, beside
   two previews and a recording 74%, where one priority left them half. `PaceWatch` reads the jobs beside the
   render all along: beside any, the status line says the CPUs its pages get
   (`share_words`), and its measure keeps the whole run's (`contention`:
   CPUs got, share of the CPU used, the most jobs of each kind beside it,
   `shared`). Every render and preview records the CPU its process tree used
   (`measured["cpu_seconds"]`). The pre-flight prices a whole render against
   its worker's committed load (`render_preflight.with_load`,
   `cpu_share.committed_load`): the previews and recordings running on the
   render worker running fewest, and its share of those queued, each at the
   time and CPU the last 20 finished of its kind took (a recording at
   `RECORDING_CPUS`, its CPU never told apart from the worker's), less what a
   running one already ran; `shared_seconds` steps through them - each at its
   priority, a queued preview starting only in the slots the render leaves -
   and the answer says "about 31 min on a worker shared with 5 scene
   previews and 1 recording (about 28 min on an idle worker)" and what the
   load is made of (`Shared`). The render keeps both (`preflight
   ["expected_seconds"]` beside the load, which its card's ETA reads,
   `idle_seconds`, `load`); once drawn, `_error` sets its time back to an
   idle worker's at the share of the CPU it got, over the part of its run a
   job was seen beside it (`cpu_share.idle_equivalent`, 25% at least,
   `contention["overlap"]`: a preview beside it for a minute of an
   hour sets back that minute), and `get_video_jobs` says it
   (`contention_words`). The committed load counts whole renders when it
   picks the least busy render worker, and where `worker_presence` is not
   read the workers running jobs stand for them - one worker's load, never
   every worker's summed; a refusal's offer of a lower quality is
   priced against it too (`highest_fitting`). `get_video_jobs` on a running
   render lists the jobs on its worker (`cpu_share.competitors`, one query
   per kind, one for the owners, one for the videos): for a job the caller
   may read (`jobs.may_read_job`), who started it, its video's title and
   whether the caller may cancel it (`jobs.may_cancel`, the rule
   `cancel_video_job` applies), with the call that would; any other by its
   kind alone, "a scene preview of another user" - never its owner's name
   or e-mail, its id, scene or video.
   `test_video_studio_cpu_share_6409.py` keeps an ultra render's pages at
   75% of the CPUs beside two previews, prices a pre-flight against queued
   previews, lists the competing jobs with owner and video - anonymous where
   unreadable -, corrects the next estimate by the share a render got over
   the time it shared, and runs the POSIX `nice` path on Linux.
   **The render lane, against every job class (`cpu_lanes.py`).** A
   24-scene 1080p60 final's 8 pages got 4.0 of 8 CPUs (92% of the CPU used)
   beside 6 assignment jobs and a scene preview, 28 min against 12 idle:
   the reconciler's shared pool (assignment runs, sub-agent tasks, flows,
   evaluations) runs in the worker's own process at the render's priority,
   and the pre-flight priced none of it. Now:
   - a whole render's process (and a scene range's) goes to the worker's
     render lane right after it is spawned, before it opens its pages
     (`render_process.supervise(lane=True)`, `cpu_lanes.place`); what it
     starts inherits it. The lane is the first the worker can give
     (`cpu_lanes.expected`, set up once by `lane()`): a cgroup v2 leaf
     where the container's cgroup is delegated (writable, `cpu` offered -
     the container's processes move to `xtm-worker`, the root enables
     `+cpu`, `xtm-render` gets `cpu.weight` `VIDEO_STUDIO_RENDER_CPU_WEIGHT`,
     1000 against 100; `docker exec` / `kubectl exec` land in the init's
     leaf, runc's cgroup v2 fallback on `EBUSY`); else a raised priority
     (`raised_nice`: the same weight ratio, -10 for 1000) where the worker
     holds `CAP_SYS_NICE` or an `RLIMIT_NICE` allowing it; else none, its
     `reason` and `REMEDY` said by the pre-flight. The render worker
     publishes it with its capacity (`render_preflight.publish_worker`,
     `render_lane`) and `with_load` prices with it: `cpu_share.priced(lane=)`
     shares the CPUs between the two cgroups by weight
     (`grouped_shares`: a cgroup under its share leaves the rest to the
     other, within the worker's by thread and priority) or at the render's
     raised nice (`shares`). Six assignment jobs at a CPU each leave a flat
     render 4.6 of 8 CPUs, the cgroup lane 7.3, the raised priority 7.4;
   - while a worker's work reserves CPUs (`OrchestrationAdapter.cpus_reserved`,
     a generic hook: the render adapter's `render_process.cpus_reserved`),
     a due execution of the shared pool waits `LEAVE_TO_OTHERS_CYCLES` (3)
     reconciler intervals for a running worker that claims its adapter
     and reserves none (`worker_presence.reserved_cpus`, published with
     the worker's presence, `_presence_extra`) before this one claims it
     (`orchestration_reconciler._leave_seconds`,
     `engine.claim_due(ripe_seconds=)`); with no such worker it runs here,
     on what the lane leaves. Adapters with a budget of their own are not
     held;
   - the pre-flight prices the jobs of the target worker's own process
     (`committed_load`: its running executions of every adapter but the
     render queue's lanes, an AI shot or track - provider-bound - and a
     figure still, counted by adapter in one query, named by
     `cpu_share.job_kind`: "assignment job") at what that worker measured
     beside its renders and previews over the last minute
     (`WorkerCpu`, sampled when its presence is written, the render
     adapter's `presence`: its container's CPU less the process trees of
     its attempts, `render_process.attempt_pids`; `_beside_cpus` takes its
     recordings out at `RECORDING_CPUS` and divides the rest), each
     running the whole render (`LASTS_THE_RENDER`); unmeasured, at
     `IN_PROCESS_CPUS` (0.25). "6 assignment jobs (6 running, about 0.06
     CPUs each: what its worker's own process used beside its renders and
     previews over the last minute)", then where new ones go, then the
     lane;
   - the render's measure says what the rest of its container used
     (`contention["others_cpus"]`), the CPUs nobody in it used
     (`unused_cpus`) and, from its cgroup's PSI (`resources.cpu_pressure_seconds`,
     `cpu_lanes.own_pressure` - the render lane's own where it is in one),
     the share of its run a task waited for a CPU (`waited`,
     `waited_own`). Pages that left `UNUSED_CPUS` (1) or more unused while
     waiting under `WAITED_SHARE` (10%) of the time were not held back by
     their neighbours, and `contention_words` says so: the observed 92% was
     that case, its missing CPUs idle rather than taken.
   `test_video_studio_render_lane_6764.py` sets up a fake delegated cgroup,
   falls back to the priority and to none, prices six assignment jobs per
   lane, names them from a worker's measure, leaves the shared pool to an
   idle worker and reads the observed render's record.
   **The reuse index (`reuse_index.py`).** Only what the render
   will draw is counted. A render serves two things without drawing them:
   a figure whose drawing is kept beside the video (`figure_cache`) - by its
   very key, or drawn sharper by a render at a higher quality and rescaled
   (`find_sharper`) - and the segments a former attempt of the same
   render closed (`compositor.kept_segments`, `render_segments`), the
   render's own, so a render about to be queued has none. A figure's key is
   `<content>-<scale>x<fps>`, its content hashing its narration's word
   times, known once the voice is spoken; so every whole render records in
   `VideoRender.measured["figures"]`, for each figure it drew or took
   (`figures.Drawing`), its key beside the inputs of its content known
   before a render (`reuse_index.figure_inputs`): the scene's digest
   (`render_checks.scene_digests`), the voice digests of its narration and
   of its neighbours' (`voice.digest_of`: the take that times its build, the
   takes whose length its transitions follow), `figures.RENDER_VERSION` and
   the release - never the size nor the frame rate. The pre-flight computes
   the same inputs as the person who renders and reads the last
   `INDEXED_RENDERS` (5) whole renders of the video at any quality
   (`reuse_index.served`, under a savepoint: an index that cannot be read
   counts every figure): a figure one recorded is left out of the figure
   frames when a drawing of its content is listed beside the video at the
   variant this quality draws, or sharper (`figure_cache.serves`, the rule
   `find_sharper` applies), and the answer says it ("2 of its 5 figures
   (1,840 frames) are kept from an earlier render and not drawn again").
   Each quality `highest_fitting` tries is estimated with its own figures
   served. `VideoRender.measured` records what was reused beside what was
   drawn: `frames` (the composed frames this attempt drew, the rate's),
   `frames_reused` (kept from an earlier attempt's segments - its own
   ranges' only), `frames_joined` (drawn by the workers of its other scene
   ranges), `figure_frames` and `figure_frames_reused`; `get_video_jobs`
   says them ("It drew 3,298 frames ...; 7,802 more were kept from the
   segments an earlier attempt finished"), so an attempt that resumed - the
   XTM Blast render of 20 Oct 2026, whose last attempt drew 3,298 of its
   11,100 frames in 16 min - is never read as the whole render.
   `test_video_studio_ultra_render_chromium.py` renders a 30 s mixed video at
   ultra under a 60 s lease (`VIDEO_STUDIO_ULTRA_RENDER=1`, the `Video Studio
   ultra render` workflow: nightly, on demand, and on the pull requests that
   change the render pipeline), and holds the pre-flight to it: its memory
   peak within 1.5 times what it said, either way, and the time the
   render took at most 1.5 times what it said and at least a third of it
   (`time_band`: the runner's 1.5 over the 0.5 floor of
   `CORRECTION_BOUNDS`). A runner faster than the default rate passes - the
   runs on record took 997 to 1058 s against 19 min said on 20 Oct 2026, 591
   to 614 s on 31 Oct 2026 -, a pre-flight that under-promises fails
   (`test_video_studio_ultra_time_band` checks the band on every CI run).
   Transitions: `spec.Transition` defaults to `auto` with a `pace`
   (`calm` / `dynamic`); `timeline.auto_transition` picks each cut from the
   role of the scenes on either side (`_SCENE_ROLE`: card, words, picture,
   screen) through `_AUTO_GRAMMAR`, a run of pictures alternating push and
   whip when dynamic, a cut out of a scene under `_MIN_DISSOLVE_SCENE`. The
   `classic` pace is the calm grammar with every move replaced by a
   crossfade of its length (`CLASSIC_TRANSITIONS`: cut, crossfade,
   fade_black), what the brand-film templates use. A
   named type plays for its `duration` or `TRANSITION_SECONDS`. The page's
   `applyTransition` draws each type (the flash raises the white `#flash`
   layer at the swap); every built-in template sets `auto` at its pace.
   `projects.transitions_line` plans the effective spec without media
   (`timeline.estimate_plan`, `planned_transitions`) and is the
   `Transitions into each scene` line of `create_video`, `edit_video` and
   `get_video`: the agent audits the cuts before it renders.
4. **Sound**: the narrations laid on one voice stem (`render.build_stem`,
   `stitch.py`), the music's bed prepared for the plan (`music.make_bed`),
   then `encoder.mix_audio`: the stem as one input, clip audio trimmed and
   retimed, the bed ducked by an envelope computed from the narration, the
   master normalised to -16 LUFS in two passes, AAC 48 kHz (see "Music and
   the mix"); then the mux and the checks (`render.check_render`, below).
5. **Outputs.** MP4, poster, WebVTT stored as the requester's files. The
   poster is a settled frame (`timeline.poster_time`): the first scene
   that is not a title card, `POSTER_SETTLE_SECONDS` (0.5) after its
   transition in ended and before the next scene's transition starts, out of
   the fades from and to black - never a dissolve mid-way; with no such
   scene, the first frame the fade in leaves visible. The
   WebVTT cues carry `line:0` when the captions sit at the top; the render
   records `captions_burned_in` (migration `vcapburn5905`, NULL - older
   renders - reads as burned in) and the player attaches the track only when
   they are not, so captions never show twice. A whole render records its
   **chapters** (`timeline.chapters`, migration `v6055chapt01`): one
   per scene of the effective spec, starting where the scene takes over
   (halfway through its transition in, the first at 0) and named as the
   editor's timeline names it - a card's title, a statement's first line
   without its `*accent*` marks, the recording's or the AI shot's name
   (`_read_references`), else `null` with the authored `type`, which the
   player words. A scene preview and older renders have none. The
   player marks them on its scrubber and names the one playing. The render
   becomes the project's current one (`latest_render_id`) and the count rule
   of the render retention runs on its video (below).

A render is an edit: it changes what the video plays and moves its renders
through the retention, so `POST /projects/{id}/renders` and `render_video`
need `can_manage`; a viewer duplicates the video to render their own.

### Scene previews

`render_video_scene` and `POST /projects/{id}/scene-renders` (`{scene}`: its
id, or its 0-based index in the spec as written - `projects.resolve_scene`;
a card the template adds by its id) start a preview of one scene
through `projects.start_scene_render`: a `VideoRender` with `scene_id` set
(migration `v6004scene01`, with `frame_file_ids`) and its quality.

- **The pre-render lint first (`projects._lint`).** A preview, and a
  whole render, never rediscovers a fault the spec shows without a frame
  drawn. Before either is queued: the static screen inserts are settled on
  their footage as it is now (`insert_bezels.settle`: a quad left out found,
  a quad within 2% snapped - through `save_spec`, a new revision, said
  "Quad 0.9% off: snapped to the measured bezel, saved as revision N -
  re-preview scene X" -, a bezel missing or a quad far off a reason), then
  every static fault it would find (`render_checks.static_reasons`, of the
  scene alone for a preview): media not ready or gone, the blocking findings
  of the quality check (its layout measured when none is kept for the
  revision, `check_video_quality(measure_layout=True)`, the music's level
  never: text fit and overlap are never skipped; a preview leaves out a
  shot's own findings, which its passing clears), sources too soft for their
  quad. All of them refuse the start in one `RenderGateError` (the tool's
  answer lists every reason; the REST route's 409 `render_gate`), `force`
  overriding it, audited; a quad the lint saves is committed and audited at
  once, refused start or not. A lint that cannot run never stops a preview.
  A source the preview draws sharp that a whole render at a larger quality
  would refuse as soft (`render_checks.soft_ahead`: soft from a
  quality larger than the preview's alone) is said, never refused:
  `StartedRender.advisories`, the route's `advisories`, the tool's
  "Advisory for a whole render" line.

- **Drawn at the size it is judged at.** A preview's quality is one
  of `spec.PREVIEW_QUALITIES`: the `quality` the start is given
  (`render_video_scene(quality)`, the body's `quality`), else
  `projects.preview_quality`'s - `standard` (1080p30) for a scene showing a
  figure, a recording or a screen insert, whose sharpness is what a
  reviewer judges, `draft` (720p30) otherwise. A player scales the MP4 to
  its display, so a 720p preview of an insert read as pixelated though the
  render would be sharp. The render draws it at that preset (never past
  1080p30, a whole render's quality ignored), its note and the tool's answer
  say the size (`{quality, size}` in the answer's head; a draft's answer
  says how to ask the 1080p one), and the editor's `Renders` tab lists the
  video's scene previews (`?scene_previews=true`) with that size under each
  one (`RendersPanel` `ScenePreviewRow`, `previewSize`), the theater's
  caption too. Its memory ceiling is its size's
  (`render_process.preview_memory_ceiling(quality=...)`, below).
- **The same pipeline on a window.** `render.scene_window` takes the
  effective spec's scene with the one before and the one after it, planned
  as in the whole video; the video's fades stay only where the window
  reaches them, and a music file or an AI track starts where it plays at that
  point of the whole video (`music.start` moved by the window's start in the
  whole video's estimate). `window_times` gives the span drawn - from the
  start of the scene's transition in to the end of its transition out (to
  the end for the last scene) - and its settled part, out of the video's
  fade from and to black where the window reaches it; `compositor.
  render_frames(frame_range=...)` draws only those frames, in parallel pages;
  the sound is mixed for the window, then cut to the same span
  (`encoder.cut_audio`). It stores the MP4 and three frames of the scene
  (settled: after its transition in, mid-way, before its transition out),
  files of the requester, and no poster or captions track. The frames are
  frames of the composed MP4 (`render.settled_frames`: indices bounded by
  its probed frame count), each cut from a quarter of a frame before its
  own time: ffmpeg keeps the first frame at or after the `-ss` it is
  given, and when nothing follows the scene in its window (the outro, a card
  cut into the next scene) the exact time of the MP4's last frame, rounded
  up to the millisecond, was past it - no frame decoded, exit 234, the
  preview failed. A card the template adds is previewed as the whole render
  plans it: the window is cut from the same effective spec
  (`test_video_studio_template_card_preview` previews the intro and the
  outro of every built-in template in Chromium and ffmpeg).
- **Every frame cached: served at once, never the lane.** An
  unchanged figure scene's preview waited 12 minutes behind two insert
  scenes' previews of its video for 744 frames its last preview had drawn.
  A preview now records what its picture and its sound are made from
  (`kept_previews.inputs`, `VideoRender.measured.kept_as`): the window the
  render plans it in (`render.scene_window`: the scene and the ones around
  it, the look, the music where the window starts in the whole video), the
  scene's drawing (`render_checks.digest_scenes`'s `whole`: the takes,
  shots and decks it plays, its neighbours' edges, the transitions), the
  narrations of its window (`reuse_index.voice_digests`), the template it
  wears and when it last changed, its quality, its person and the release.
  `start_scene_render` computes the same after the lint; a ready preview of
  the scene at that quality made from the same, its MP4 still stored
  (`kept_previews.kept`), has every frame of the new one: the new preview
  is never dealt to the lane - its job is queued behind
  `SERVE_GRACE_SECONDS` (180) - and once the start is committed
  (`projects.run_started`, from `render_video_scene` and the REST route)
  `kept_previews.serve` makes it on the worker that asked: the kept MP4
  encoded into a file of its own (`encoder.remux`, a stream copy), its three
  frames and every file its checks name (`file_id`, `frame_file_id`) copied
  as its person's, its checks carried (`Checks.from_dict`), finished like
  any preview (`render.finish_made`: the scene's drawing recorded, the
  retention applied). Its first note, a fact of its answer, says "744 of
  744 frames cached, encoded in 1.4 s: scene preview `...` drew them from
  the same inputs - ..." and names what the key compared
  (`kept_previews.COMPARED`: the window's scenes as specified, every
  overlay's text, subtext, position, start and duration included, pinned by
  `test_video_studio_harness_6420.py`). Its held job, once claimed, finds the row
  finished and ends. A serve that cannot finish (the kept MP4 gone, a file
  unread) gives the preview back to the lane, its job due at once
  (`jobs.due_now`); a process that died mid-serve leaves it to the grace.
  It measured no drawing, so no rate the pre-flights read.
- **Never what the video plays.** `latest_render_id` is untouched,
  `PATCH /renders/{id}` refuses to make one current, the render lists and
  `latest_renders` leave previews out (`?scene_previews=true` lists them),
  and a whole render never replaces one (nor the reverse). A preview replaces
  the preview of the same scene still in progress. Every preview asked is
  queued: a video has one in flight per scene at most, so a sweep of
  every scene is one batch of `render_video_scene` calls, and
  `jobs.max_scene_previews_per_video` - the renders' own
  `VIDEO_STUDIO_MAX_RENDERS_PER_USER`, counted per video - bounds how many of
  them RUN at once, never how many are asked. The bound is read when the
  worker claims a preview (`jobs.preview_waits`, from the executions running,
  not the rows): past it the preview is handed back to wait in its place
  (`AdapterResult.defer(place="keep")`, `adapter_state` `preview_waiting`,
  no attempt spent, its stage saying why): its `run_at` - its place in the
  queue - is kept, and it is claimed again once `PREVIEW_WAIT_SECONDS` (15)
  passed (`payload.not_before`, which `engine.claim_due` honours) before the
  jobs queued after it, while those are claimed meanwhile; its hold is closed
  once it starts (`engine.end_hold`).
- **One preview per video in turn, dealt at the claim.**
  Served in the order asked, one worker running two previews at a
  time with 28 queued across three videos made a 20-scene sweep wait behind
  every preview of the other films. Dealing the lane when a preview is
  queued, by permuting the queued previews' `run_at` values once, does not
  hold: that order does not survive the first wait. A preview that waits -
  its video's cap, its worker's memory, handed to another worker
  (`defer(place="keep")`, a `not_before` window) - is skipped by `engine.claim_due`, which claimed
  whatever was due next by `run_at`: on 22 Oct 2026 six previews of one
  video sat at places 3 to 15 behind another video's batch for 30 minutes,
  the queue line saying "0 ahead" while two previews of other videos ran.
  The lane is now dealt as its slots claim it, by one module
  (`preview_lane.py`): `engine.claim_due(deal=...)` hands an adapter's due
  executions (up to `DEAL_WINDOW`, 500, oldest `run_at` first) to its
  `deal_claims` hook - the reconciler passes the adapter's own, the
  borrower's for a render slot lent to the preview lane - and locks
  them in the order it answers, `SKIP LOCKED`, so a preview another worker
  took meanwhile is passed over for the next one in the deal.
  `VideoPreviewAdapter.deal_claims` (`preview_lane.deal_claims`) puts a
  render a stopped worker left (`jobs.RESUMING_STATES`) and a preview handed
  to this worker (`claim_for`) first, then deals one preview per
  video in turn (`preview_lane.dealt`): each turn to the video with the
  fewest previews being made now (and dealt before in this deal), then to
  the one served longest ago (its latest preview's `started_at` within
  `SERVED_WINDOW`, 12 h; never served first), then to the one whose first
  queued preview was asked first - each video's own previews in their
  `run_at` order. A video running `jobs.max_scene_previews_per_video` is not
  claimed for until one of its previews ends (`jobs.preview_waits` still
  refuses one two workers claim at once). Two videos queuing six previews
  each alternate, one slot or several. No `run_at` is rewritten any more
  (`jobs.deal_previews` and its advisory lock are gone). A job's queue place
  (`jobs.queue_place`, read from the executions) counts the pending
  executions of its lane due before its own, the renders a stopped worker
  left counted with the ones being made (`jobs.RESUMING_STATES`, they were
  being made and go first): on the render lane nothing queued after a job
  ever goes before it. A queued preview's place is its place in the deal a
  free slot would make now (`preview_lane.place_of`), and `QueuePlace.videos`
  names the videos whose previews it waits behind - being made, and dealt
  before it - with their titles (`preview_lane.VideoAhead`): the queue line
  reads "waiting for a worker: 1 preview of video Launch film ahead (1
  worker draws previews, 2 at a time)" (`jobs.describe`,
  `preview_lane.ahead_words`), never a bare "0 ahead". How many workers draw
  previews is read from what each publishes for preview routing
  (`preview_routing.lane_workers`: the rooms alive within
  `ROOM_TTL_SECONDS`, each with its `capacity`; one scan of them
  kept `LANE_WORKERS_SECONDS`, 2 s, shared by every queue line and every
  preview start's lane advice - a route decision reads `rooms`
  fresh), so a deployment that runs one preview worker reads as one,
  whatever a compose file declares; with no room readable the line says "a
  worker makes 2 at a time" as before. The legibility advice that has a
  recording recorded again - at a higher pixel ratio for a push, at a
  browser zoom - names the call that sizes it from the saved quad,
  `record_browser_demo(target_render: "<quality>", video_id, scene,
  insert)` (`Legibility._sized_call`), as `insert_resolution` does. The preview's start answers it on its own session
  before it commits (`StartedRender.queue`, the route's `queue: {position,
  ahead, running, per_worker, lane, videos_ahead, preview_workers,
  preview_slots}`, `render_video_scene`'s "position N in the preview lane
  of the render queue"), and `get_video_jobs` serves it with `position`,
  `lane` and `videos_ahead`. The default deployment runs two preview
  workers (`docker-compose.yml`, `worker-preview` with `deploy.replicas:
  2`); a Kubernetes deployment needs a second worker Deployment with
  `VIDEO_STUDIO_RENDER_CONCURRENCY=0` for the same.
- **A fair share of the slots, and the wait said.** 13 previews of
  one video waited about 20 min behind another video's batch on one worker
  (two slots, one held back beside a render). The deal took turns only among
  the previews due at the instant a slot freed: one waiting for memory or
  handed to another worker was not, so the other video's batch took the slot
  and kept it for its draw time; and the start answered a place, never a wait.
  A video asks the lane while a preview of it is being made or due within
  `SHARE_HORIZON` (60 s, `Lane.asking`: a preview waiting for memory, a route
  or its video's cap is claimed again well within it). Its fair share is
  `fair_share`: the lane's open slots (`open_slots`: the workers' slots less
  those held back, `preview_routing.LaneWorkers`) over the videos asking,
  rounded up, one at least, never past `max_scene_previews_per_video`. While
  one video asking holds fewer than its share, every one is held to it
  (`Lane.caps`, `dealt(caps=...)`, in `deal_claims` and `place_of` alike); once
  each holds its share, none is, so a slot left - a render slot lent to the
  lane - goes to whoever is due. With no worker said, the fixed cap
  alone. The queue place of a preview says its wait at the lane's pace
  (`jobs.queue_place`, `QueuePlace.expected_wait_s`, `wait_words`): its place
  and the previews being made over the open slots (`expected_wait`: none with
  a slot free for it, else half a draw for those being made and a draw for
  each round of the slots ahead past the free ones), each at the median draw
  time of the last `DRAW_SAMPLE` (20) previews drawn, whenever (`draw_seconds`,
  a kept one never drew) - "about 20 min before it starts at the lane's pace (a
  scene preview drawn in about 90 s, the median of the last 20)", appended to
  the queue line every answer reads (`jobs.describe`, so `render_video_scene`
  says it as it queues the preview, `get_video_jobs` as it waits), and
  `expected_wait_seconds` / `median_draw_seconds` on the route's `queue`. A
  second preview worker or a preview pool remains the deployment's to add.
  `test_video_studio_preview_share_6710.py`.
- **The lane's load and the start instant beside the wait.** A sweep
  of 24 scenes on one worker's two slots told each queued preview its own
  wait and nothing of the lane: the p95 wait grew with the queue depth and only
  `measure_video_lane` said it. `queue_place` adds the p95 wait in the queue of
  the previews started in the last `MEASURED_WINDOW` (`recent_p95_wait`, on the
  `recent_timings` the measure reads, a kept preview never counted) to the
  `QueuePlace` (`p95_wait_s`): `wait_words` appends "; the previews started in
  the last hour waited 25 min at the 95th percentile" inside the parenthesis,
  `as_dict` carries `p95_wait_seconds` and `expected_start_at`
  (`QueuePlace.expected_start_at`: now plus the expected wait, the instant a
  poller compares with). The deal is unchanged: 24 queued previews of one video
  and 2 of another on two slots claim one of each first (pinned). The slots do
  not scale with the queue depth: the adapter's `concurrency_limit` is read once
  per process when its semaphore is built and `slots_held_back` only lowers it;
  `VIDEO_STUDIO_PREVIEW_CONCURRENCY` and a preview-only worker remain the levers.
- **A queued whole render's wait.** A render queued at position 1
  behind another video's render read "1 render being made now, 0 queued
  ahead of it" for 25 min, the pre-flight's estimate covering its own
  drawing only. `render_queue.read` (from `jobs.queue_place`, on the render
  lane while a render is being made) reads the renders being made and those
  queued before it; each being made has its end (`render_pace.eta_of`: its
  measured pace, else its start plus its pre-flight's estimate; past it, its
  end is unknown and said so), each queued before it its pre-flight's
  estimate. `starts_in` frees the slots the renders being made hold as each
  ends, each taken in turn by the next render queued before it: the queue
  line (`QueuePlace.render_wait`, `wait_words`) says "the render being made
  ends in about 18 min (by its measured pace) - this one starts in about
  18 min", `renders_ahead` / `starts_in_seconds` on the route's `queue`. No
  other video is named. `test_video_studio_music_fit_6719.py`.
- **A lane of its own.** A scene preview is queued on the
  `video_preview` adapter (`jobs.adapter_of`: a render with a `scene_id`),
  whole renders on `video_render`: each lane has its own slots
  (`VIDEO_STUDIO_PREVIEW_CONCURRENCY`, 2, and
  `VIDEO_STUDIO_RENDER_CONCURRENCY`, 1), so a sweep of one video's scenes
  never delays a 4K render of another, nor a 20-minute 4K render the
  sweep. It replaces the head start previews had on the renders' one slot
  (`enqueue(priority=True)`, `PRIORITY_LEAD`). The
  `VideoPreviewAdapter` runs a preview exactly as `VideoRenderAdapter`
  runs a render (its own process, lease, resumes, handover), and a preview
  queued on `video_render` before the lane existed runs there as before. A
  preview has a render's deadline: the former 30 min, the queue
  included, dead-lettered a preview still waiting behind the others the
  renders' limit lets queue; a preview that stalls once running is failed
  by the render's supervisor (`VIDEO_STUDIO_RENDER_STALL_SECONDS`), like a
  render.
- **An idle render slot takes previews.** 18 previews of other
  videos, two at a time on one worker, held a two-scene check for 40 min.
  The render lane's adapter lends its idle slots to the preview lane
  (`VideoRenderAdapter.lends_idle_slots_to`, the reconciler's
  `_lend_idle_slots`, generic: any adapter may name another lane): on a
  worker whose render lane claimed nothing this cycle and runs nothing,
  while the preview lane's own slots are all taken, the render slot claims
  the preview lane's next due preview - in that lane's dealt order, by
  `engine.claim_due` on the preview adapter - and runs it as the preview
  adapter does (its admission by memory included), holding the render
  slot's semaphore and in-flight count (`_spawn_execution_task(slot=)`). A
  lane with room claims for itself; a whole render asked meanwhile waits for
  that one preview, never longer. Every worker does it, so a second worker
  whose render lane is idle takes previews in its render slot too.
- **Memory beside a whole render.** Renders used to run one at a
  time so their ceiling held. A preview's process now has a ceiling of its
  own (`render_process.preview_memory_ceiling`:
  `VIDEO_STUDIO_PREVIEW_MEMORY_MB`, else `PREVIEW_HEADROOM` (2) times
  `render_preflight.memory_needed` of the preview at its quality
  (720p30 or 1080p30, the row's `quality`, `ceiling_of` / `preview_size_of`)
  of the largest frame on the pages `render_preflight.layout` wants - about
  2.7 GiB at 720p and 4.5 GiB at 1080p on 2 pages for a scene without screen
  inserts - with the inserts its window shows at
  once on each page (below), kept within the preview reserve unless
  the budget its pages are laid out under alone is more, and never above the
  worker's memory; `run_render` lays a preview's pages out under it),
  and it is admitted by its worker (`render_process.admit_preview` with
  `preview_size_of`, from the adapter, checked and held with nothing awaited
  in between).
- **The preview reserve, a hard floor.** Admitted against
  `WORKER_RESERVE` (4 GiB) and the renders' ceilings, every preview that
  showed screen inserts (4.5 to 7.5 GiB) waited for a whole 16 GiB render on
  a 24 GiB worker: 45 to 50 min, twice on 21 Oct 2026. Then a 24 GiB worker
  drawing one 2160p render on 3 pages with 19.5 GiB held left its previews
  one page, a 7 s insert scene previewed at a frame a second. Now
  `render_process.preview_reserve` is `PREVIEW_RESERVE_FLOOR` (8 GiB, the
  largest preview's worst case) at least: `VIDEO_STUDIO_PREVIEW_RESERVE_MB`
  raises it, never lowers it, and no clamp shrinks it. A worker that draws no
  preview (`VIDEO_STUDIO_PREVIEW_CONCURRENCY=0`, `draws_previews`) keeps
  none. A whole render's ceiling is never more than the container's limit
  (the host's memory without one) less the reserve, an ultra one's never
  more than that limit less `kept_from_renders` (the larger of
  `WORKER_RESERVE` and the reserve), so the pre-flight admits a render only
  where what is left covers it and `memory_to_give` names the container that
  holds it beside the reserve; a worker no bigger than its reserve holds no
  render (`no_room`: failed with the memory to give, never started
  unbounded). An ultra render's ceiling is
  `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB` at least and, where the container has
  more, what the pages its CPUs want need at their measured 2160p peak
  (`render_process.ultra_pages_need`: `render_preflight.memory_budget`
  of `layout(...).wanted` pages with the peak record's inserts): a fixed 16
  GiB held two pages of 5.9 GiB whatever the container had.
- **A render's room, at launch.** The supervisor reads
  `launch_room(quality)` as it starts a whole render or a scene range: its
  quality's ceiling, never more than the worker's memory less the reserve,
  its other renders (`renders_held`) and what its previews hold past the
  reserve. It passes it to the process (`render_preflight.ROOM_ENV`, read by
  `launch_ceiling`), which lays its pages out in it and reports them
  (`layout_event`: pages, pages wanted, their `memory_budget`); from then on
  the render holds that budget (`_Attempt.holds`, `_holding`) and the rest
  of its room is the previews'. Its kill ceiling stays its room.
- **A preview runs on its pages, here, elsewhere, or after waiting.**
  A preview starts in what the runs of this worker hold (`memory_held`:
  each render its pages' budget once laid out, its ceiling until then; each
  preview its ceiling from its admission) leave, as a `PreviewAdmission`
  (`admission_of`, which its supervisor reads), on its pages only:
  `admit_preview` returns why when they do not fit, never a one-page
  admission (one page drew a 1080p insert scene at a frame a second beside a
  4K render, two pages five). The preview lane (`VideoPreviewAdapter`) then
  asks `preview_routing`: every worker that draws previews publishes what it
  has free (`publish`: `room_for_previews`, its free preview slots, at most
  every 2 s and as a preview starts or ends, a record living 15 s), and the
  preview is handed to the worker with the most memory free and a free slot
  (`best`, never one `tried`, `MAX_ROUTES` workers at most): deferred with
  `claim_for=(worker, ROUTE_HOLD_SECONDS)`, `adapter_state`
  `preview_routed`, the route in the payload (`preview_route`). The engine's
  `claim_due` lets only that worker claim it for those seconds
  (`CLAIM_FOR_PAYLOAD_KEY`), any worker after. No worker has room: it waits
  for its pages in its place (`place="keep"`, `adapter_state`
  `preview_memory_waiting`, no attempt spent, its stage saying why), claimed
  again every `WAIT_POLL_SECONDS`, its wait counted from the first of its
  memory waits in a row (`preview_memory_wait_since`,
  `adapters.memory_waited`, and why it began, `preview_memory_wait_why`).
  Once admitted, `admission_note` says where it was drawn - routed from
  which worker and why, after how long a wait - and its supervisor hands it
  to the process (`ADMISSION_NOTE_ENV`), which puts it first in its `notes`.
  A preview alone always starts on its pages; a whole render never waits for
  a preview. Previews and the worker's own jobs share the reserve, so every
  render process raises its own
  `oom_score_adj` before it starts anything (`OOM_SCORE_ENV`,
  `stopped_before_the_worker`; figure stills too): 1000 for a preview, 500
  for a whole render or a range, the worker 0 - should the container's
  memory run out, the kernel stops a preview's processes first, a render's
  next, and the supervisor says it was the out-of-memory killer.
- **Screen inserts are counted, per insert and per page.** Each
  page's frame server opens a decoder per video insert and composes every
  insert on the footage frame decoded at the host's size, so
  `render_preflight.memory_needed` / `page_memory` / `layout` take an
  `InsertLoad` - the inserts a page draws at once at most, a scene's or two
  scenes' over the transition between them - and add `insert_memory` per
  page: `INSERT_VIDEO_BASE` (90 MiB: a 1920x1080 source's ffmpeg decoder, 55
  to 60 MiB a page, and its frame in the frame server) for a video, and
  `INSERT_BYTES_PER_PIXEL` of the output for each (60
  for the matte's five raw mattes and the warp read at twice the
  box's size, estimated from the arrays held), plus
  `INSERT_FOOTAGE_BYTES_PER_PIXEL` (12) once per page for the footage frames
  the frame server keeps for the matte's window. Measured on a 6 s
  clip scene of a chroma-green screen with three keyed 1920x1080 video
  inserts and a picture, in Chromium (draft): 300 to 400 MiB more than
  without them on one 1280x720 page, 480 to 530 MiB on two, 310 MiB on one
  1920x1080 page; `tests/test_video_studio_insert_memory.py` holds every
  run under the term, and a production run (8 pages, 8.5 to 8.7
  GiB against an 8.3 GiB ceiling) under the ceiling it sizes. A whole render
  reads the load off its plan (`InsertLoad.of`), its pre-flight and a
  preview's supervisor and admission off the spec before anything is planned
  (`InsertLoad.of_spec`, a preview's window `timeline.preview_window`; a
  file counted as a video), so the three size a preview alike
  (`render_process.preview_inserts`).
- **A preview stopped at its ceiling runs again on one page.** The
  supervisor starts it again at once, in the same working directory (its
  finished segments kept: they do not depend on the pages), with
  `render_preflight.ONE_PAGE_ENV`; `preview_drawn_pages` then draws it on
  one page, its insert layers composed one after the other under the same
  ceiling, and its answer says why (`ONE_PAGE_NOTE_ENV`). Only a
  preview stopped on one page too fails, saying to raise
  `VIDEO_STUDIO_PREVIEW_MEMORY_MB`; a whole render is never drawn again so.
  This is the one way a preview draws on one page. A whole render
  its memory ceiling stopped runs again once on half the pages its
  first run laid out, one when they are not known
  (`render_retry.lighter_pages`: the pages go with `run_again`'s record;
  `render_preflight.RETRY_PAGES_ENV`, `retry_pages`, laid out on the
  worker's own CPUs), its answer saying so (`retry_pages_note`) - never
  the layout that failed, run again unchanged for the same minutes.
  `test_video_studio_render_retry_6332.py` runs `render_retry.requeue`
  against PostgreSQL: the dead letter back to pending, its attempts back,
  its payload merged, the row reopened, and a render cancelled while its
  execution is requeued keeping its cancel.
- **A worker's lanes.** An adapter declares whether this worker
  claims its executions (`OrchestrationAdapter.claims_here`): the render
  lane's is `VIDEO_STUDIO_RENDER_CONCURRENCY > 0`, the preview lane's
  `VIDEO_STUDIO_PREVIEW_CONCURRENCY > 0`. The reconciler claims none for a
  lane its worker does not run and lends it no idle slot; its presence lists
  the lanes it claims (`claimed_adapters`, `worker_presence.publish(claims=)`)
  and `worker_presence.count(claiming=...)` counts the workers of one lane,
  which `render_preflight.workers_present` splits a render across. The
  default `docker-compose.yml` runs `worker` (renders and previews) beside
  two replicas of `worker-preview` (`VIDEO_STUDIO_RENDER_CONCURRENCY=0`).
- **Its verdict is its own scene's.** Its window draws the scenes
  before and after it through the transitions, so its checks may see a
  neighbour's screen insert (the crossfade out of `s03` showed `s03`'s
  insert for 24 frames, and the preview of `s04` failed on it). Every
  insert finding names the scene that owns the insert
  (`insert_checks.placed_inserts`, `missing`, `soft`), and before the
  preview's outcome `render_checks.settle_neighbours` reads its failing
  findings on another scene: one that scene's own drawing as it is now
  already reported (`reported_pictures`, read off `scene_previews` when the
  preview starts) is left out, said in a note, its frame never stored;
  another is kept as a warning naming that scene ("on scene s03, drawn
  beside this one (its own preview judges it)", `data.neighbour`). What
  blocks on the frames of a join (`data.transition`: a residual key
  there, an insert not drawn there, `insert_dropped`) is this preview's own
  frames: kept a failure ("on scene s03, measured on this preview's join
  with it: ...") and said as a finding of the answer. Its insert report is
  marked `neighbour` and its measurements said as measurements ("a
  neighbour's, measured on this preview's frames of the join: residual key
  0.00% ...", with its joins' own line: frames measured, the most residual
  key, every frame drawn) - never deferred to another preview, whose window
  holds the same join drawn the same way. Its bezel is the exception
  (`insert_checks.bezel_readings`): read at another size than it was
  last measured at, its alignment is that measure, said as a fact, never a
  finding ("One reading per size, on a join", with the screen inserts). The
  preview's status and the
  drawing it records are then its own scene's alone; a whole render judges
  every scene as before.
- **Its own retention.** A preview that ends ready replaces the
  previous previews of its scene (`render_retention.replaced_previews`,
  from `render.finish_made`), their frames deleted with them; the figure and
  insert caches keep their frames under their own eviction. The nightly
  sweep deletes a preview `previews_days` (7) after it ended; a preview is
  never the newest ready render of its quality.
- **The tool's answer** opens with `{render_id, scene_id, state, status,
  findings, by_design, facts, infra_once, quality, size, file_id,
  frame_file_ids, checks: {pronunciation, inserts}}` (below): the
  MP4 as a file of the conversation (linked, never attached to the model),
  the three frames attached, and the scene's findings timed in the
  preview's MP4.

### What a render's answer is made of

Every wave of the 21 Oct 2026 sweep ended with residual items reported by
hand: the answers mixed what blocks, what warns, what a setting the author
chose shows and the platform's sizing in one paragraph. `render_answer`
builds the answer of a finished render or scene preview
(`RenderAnswer`, `of_render` / `of_checks`), its first line JSON and its
text after:

- `status`: `green` when `findings` is empty, `blocked` when one of them
  blocks, else `warnings` (a warning alone never reads as blocked -
  "Verdict: not blocked - 1 warning, nothing blocking")
  (`RenderAnswer.verdict`, read off `findings` alone: "all gates green" is
  `findings` empty).
- `findings`: the render's own `render_checks.Finding`s -
  `{kind, severity: blocks | warns, at, end, scene, what, fix}` and the
  frame each shows. `fix` is the finding's own (`Finding.fix`, set by its
  maker and the end of its message), else its kind's (`KIND_FIXES`); `what`
  is its message without it. A neighbour's finding a preview saw in a
  transition is a fact of the preview, never its finding. A render
  that ended with no finding to say why (its memory, a crash, a cancel)
  blocks with `render_failed`; one the platform failed
  (`VideoRender.failure`) says so, that it ran again once, and that
  nothing in the video needs to change.
- `by_design`: what a setting the author chose makes the video show,
  `{what, because, insert, scene}`. Every insert measurement is classed by
  `insert_checks.classify` (`render_checks.insert_measured` adds the
  evidence): a finding when a finding of its kind holds it on that insert
  (`_MEASURED_BY`), by design when its fit, light or key makes it - a
  contain fit's bands (`fit contain, no key`), the colours at its light,
  what its key took out of the margin -, else a fact. The `Insert` line
  (`render_checks.insert_lines`) says each with its class first.
- `facts`: what this render measured that blocks nothing: its inserts'
  numbers, its checks that passed (`render_checks.check_lines`), its notes,
  its stalls, its estimate and its drawing (`render_preflight.said_of`).
- `infra_once`: the platform's sizing (`render_preflight.Infra`), on the
  answer of the `render_video` call that started the render only; and
  each bezel reading of a join frame set aside for the save's
  measure (`checks.infra`, `insert_checks.bezel_readings`), on the answer
  of the render that read it.

**Infra advice, once.** `render_preflight.estimate` keeps the render's own
facts in `notes` ("It draws on 3 pages at 3840x2160, as many as its memory
ceiling holds") and the sizing apart in `infra`: `pages` (the ceiling holds
fewer pages than the CPUs: the time on them against the time on all, and
`memory_to_give`) and `cap` (past 4 h: what a worker needs). Each has a
`key` of its numbers - never the video's times. `preflight` reads the keys
the latest whole render at that quality recorded
(`infra_said_before`, off `VideoRender.preflight["infra"]`) and says only
an `Infra` whose key is new (`with_infra_said`, `Preflight.infra_once`):
two ultra renders in a row on one worker say it once, a worker given more
memory says it again.

**A preview that waited.** The reason a preview cannot start
(`render_process.admit_preview`) names what holds its worker's memory by
kind (`holders`: "1 render (11.6 GiB) and 1 scene preview (7.0 GiB)"):
the reserve keeps the renders off the previews' memory, never one preview
off another's. The reserve's floor is one preview's worst case and a
worker runs `VIDEO_STUDIO_PREVIEW_CONCURRENCY` previews, so beside a render
the second waits for the first. Once a preview that waited for its pages
starts, `VideoPreviewAdapter` records `reserve_advice` on its row
(`render_preflight.record_preview_infra`: `preflight["infra"]`, a
`preview_reserve` `Infra` keyed on the reserve, the slots and the reserve
that holds them all at their worst case) and its answer lists it under
`infra_once` (`infra_of`), said only when the latest earlier preview that
gave advice of that kind gave other numbers.

**A preview that waited behind other videos on one worker.** Four
previews of one video took 25 min in production with two other videos in
the lane: the compose file runs two preview workers, the helm chart
one worker for everything, so the lane had one worker's two slots. When a
preview starts after `preview_lane.LANE_WAIT_SECONDS` (120 s) or more in
the queue, while other videos have previews in the lane and one worker
draws previews (`preview_routing.lane_workers`, below), `VideoPreviewAdapter`
records `preview_lane.lane_advice` beside any reserve advice: a
`preview_workers` `Infra` keyed on the workers and their slots, naming a
second preview-only worker (`VIDEO_STUDIO_RENDER_CONCURRENCY=0`). It is
the deployment's to add, nothing in the video. `record_preview_infra` keeps
advice of another kind on the row and decides "said before" kind by kind.

**The lane measured.** `preview_lane.read_measure` reads the scene
previews that started in the last `MEASURED_WINDOW` (1 h) off their rows -
the queue wait (`started_at - created_at`) and the draw time
(`finished_at - started_at`), a preview served from a kept one left out (it
never queued) - and `video_studio_capabilities` answers it as
`preview_lane: {workers, slots, queued, measured_previews, median_wait_s,
p95_wait_s, median_draw_s, sizing}`. `LaneMeasure.undersized` is the sizing
rule: the p95 wait past `SIZING_WAIT_FACTOR` (2) times the median draw time,
`sizing` then naming a preview-only worker or more preview slots on idle
CPUs - the deployment's to change. A sweep is not pinned to one worker: the
figure cache is shared through the object store, and pinning would halve a
sweep's parallelism on two workers.

### Audio and picture checks

What the viewer hears and sees is checked in code after it is made, every
finding a `render_checks.Finding` with its kind, its severity, its time in
the render's MP4 (`at`, `end`) and its scene, stored on the render
(`VideoRender.checks`, migration `v6065avqa01`) and said by `render_video`,
`render_video_scene` and `get_video_jobs` (`render_checks.format_checks`,
`m:ss.xx`), and listed under the render in the editor's `Renders`.

- **Speech-to-text** is `app/llm/transcription.py`: OpenAI's transcription
  API (`whisper-1`, word times with `words=True`) on the key of the first
  OpenAI provider the person may use - the chat's dictation (`POST
  /chat/stt`) goes through it too. None available: the narrations are placed
  `unchecked`, the render says so, the other checks run. A data flow to
  know: the narrations' audio goes to that OpenAI provider whatever provider
  speaks them (ElevenLabs, Gemini...), under the provider routing's
  reservations and exclusions like any call (`usable_provider_configs`).
  Each narration is transcribed once, with its word times (its record keeps
  both, `words`); the rendered-sound check reads those words where the
  stem places the take while the encoded voice still plays it, and
  transcribes only a narration whose words are not known (a record kept
  before they were, a take not heard back) or whose take changed on its way
  to the viewer: a clean render of N narrations makes N calls, not 2N. The checks name
  the model and the provider (`Transcriber.provider`,
  `transcription.provider_label`: `OpenAI (<its display name>)`), with how
  many narrations were read from their own transcript (`reused_transcripts`)
  and how many parts of the stem were transcribed (`transcribed_parts`).
- **Each narration heard back** (`voice_check.py`). After it is spoken, the
  WAV (16 kHz mono) is transcribed with its word times and aligned to the
  narration, each pronunciation lexicon term one token on both sides
  whichever reading the lexicon accepts was heard (see "Pronunciation
  lexicon"); the words are normalised alike (case, punctuation, numbers
  either way, an acronym spelled out, a compound heard as two words). A
  proper noun no lexicon entry covers is matched as the lexicon matches a
  term's reading (`pronunciation_check.name_flags` / `says_name`): a
  word of two letters or more with a capital first where no sentence starts
  with it (a sentence's first word when the next one is a name too, `Moscone
  Center`, or when it is written like a coined name, `SecOps`) is heard when
  up to three words aligned there are its written form, case and spaces
  aside, or its sound as English reads both - the same consonants and
  word-initial vowels (`phonetics.key`), starting alike, at most one off (none
  under three of them), its inner vowels free. A spelling the speech-to-text
  chose for a name (`Mosconi`, `Moscow knee`) is no word error, and the
  record says the names heard so (`names`); a name never gets a brand check,
  and every other word is held to its spelling. A WER
  over `MAX_WER` (10 %), a token sequence repeated in a row that the text does
  not repeat, or a lexicon term heard wrong where the narration has it (the
  brand check) fails it. Then the take of the same voice that passed its
  check on the same words before is placed (`voice.text_digest_of`,
  the narration's digest without its neighbours, under which each pass is
  also kept, `voice-cache/passed/<digest>/`, with its scene and the render it
  passed in; the scene's own first, newest first; placed as it was heard,
  never transcribed again) - the scene's preview, or a render made before a
  neighbour was reworded. Without one, it is spoken again with the same voice and the
  next-strongest mechanism (a new take, `voice.speak_take`, kept under its
  own bytes' name `<digest>.take-<sha>.wav`: an object key names one sound,
  so the cached take is never written over). A lexicon term both takes heard
  wrong is never sent the same way a third time
  (`voice_check.heard_wrong_twice`): the next take says it in another context
  (`lexicon.REFRAMED`, see "The retry ladder" below), then once more slower
  with that text - the text rewrite when no term failed twice
  (`voice.slower_speed`: `SLOWER` (0.85) of the video's speed,
  never under `speech.MIN_SPEED`, skipped at the slowest), then by the voice
  of the same speech model nearest the video's (`voice.closest_voice`:
  the most traits shared in what the provider says of its voices -
  `speech.Voice.description`, accent, gender, tone - the provider's own order
  breaking a tie; never a list of voice names, never another provider's
  voice, never the platform's default). Each check adds a record under
  `voice-cache/checks/<digest>/` (`file_storage.upload`, never rewritten; the
  newest decides): duration, transcript, WER, the terms as heard, each
  attempt's mechanism, speed and string sent, and the take that passed (`placed`). A narration heard once is placed from that take and never
  transcribed again, the editor's preview plays it too (`voice.placed`), and
  one that failed is spoken as a new take first next time.
  A narration only that closest voice said right failed in the video's voice:
  its record is `failed` with `fallback` and its `voice_change` (the
  video's voice, the voice used, why the video's failed, why that voice is the
  closest: `voice_check.VoiceChange`; that voice's own digest keeps its pass),
  so the gate refuses the video, the next render tries the video's voice
  again, and the editor never plays the other voice's take. In a render it is
  a `voice_fallback` failure at its time and scene, naming the voice,
  why and what to change (`VoiceChange.sentence`: reword, a pronunciation
  entry, or that voice made the video's) - two narrators in one film, which
  the audience hears: a whole render stops on it before its frames
  are drawn, as on a narration that failed (`render._voice_findings`, every
  other scene untouched), a forced one plays that voice with the finding, and
  a scene preview plays it with the failure so it can be heard. The quality
  check reads it from the record (`voice_check.voice_changes`) as a
  `voice_fallback` finding, `dense`, which the gate holds the next render on
  until the narration is reworded, a pronunciation added or that voice made
  the video's (`voiceover.voice`).
  A failed narration is never placed in another voice: a forced render
  places the closest take of the video's own. A record of version 4 that passed still stands
  (`voice_check.PASSED_SINCE`: version 5 only accepts more), one that failed
  is heard again, and a version-4 pass the default voice said is a failure.
  A narration that failed every attempt stops a whole render before its
  frames are drawn (`voice_segment` finding); a forced render places its
  closest take with the finding; a scene preview goes on and says it. A
  preview speaks and hears the scenes before and after it too (their
  narrations play in its transitions): a neighbour's failure is a `warn`
  finding on the neighbour, never a failure of the scene previewed
  (`render._voice_findings(scene_id=...)`); the gate still refuses a
  whole render on the neighbour's failed record. A preview speaks each
  narration with its neighbours in the whole video, never the window's
  (`voice.lines_of(within=...)`): cut at a scene that says nothing, the
  window would give it other neighbours, so another digest - a check the
  whole render never reads.
- **The voice stem** (`stitch.py`). Every narration on one mono 32-bit float
  WAV at the mix rate, streamed: each segment resampled once, whole, by a
  Kaiser-windowed sinc (`scipy.signal.resample_poly`) when its rate is not the
  mix's (the voice chain writes 48 kHz: not touched), placed on the sample of
  its narration start, a 10 ms raised-cosine fade at both edges, an overlap
  refused (`StitchError`: the planner extends a scene to its narration, so an
  overlap is a planning defect, never cut or crossfaded). The mix takes it
  as one input (`encoder.VoiceTrack`, `pan` to both channels, no `adelay`, no
  `aresample`); the per-narration `adelay` / `amix` graph is gone.
- **The rendered sound** (`audio_qc.py`). The final AAC is demuxed
  (`encoder.demux_audio`) and decoded with the stem at 16 kHz mono float -
  about 38 MB per track per 10 minutes, so some 80 MB for the two, plus
  15 MB of band energies each, in the render's process. The stem is heard
  through the final mix's encode (`encoder.encode_voice_like_mix`),
  and each narration's words are aligned on its own text alone, lexicon-aware,
  with the very normalisation of its own check
  (`voice_check.hearing_of`, `audio_qc.hear_narrations`): over 10 % for the
  whole voice-over the render fails, a `transcript` finding per scene heard
  wrong, and a `pronunciation` finding per lexicon term heard wrong, at its
  time (the `pronunciation` section). A narration `voice_check` heard already
  (`SpokenSegment.heard_words`, its take in `audio_qc.HeardTake`) is read from
  its own words, moved to where the stem places the take (whole, from its
  first sample), as long as the take placed is still the sound those words
  were heard in (its SHA-256, `voice_check.take_digest`, kept in the check
  record as `words_digest`) and the encoded voice still plays that very take
  (`audio_qc.plays_the_take`: the take's band energies against the
  voice heard over its span, the levels aligned, within `TAKE_LAG_SECONDS` for
  an encoder's delay, every 20 ms of its speech within `SAME_TAKE_DB` - a word
  the stitching, the resampling or the encoding cut, repeated or garbled jumps
  far past it); the rest - a narration not heard before, or changed since
  (`changed_since_heard`) - is transcribed from the encoded voice on its own
  placed span (`stitch.Stem.spans`, the clip the stem placed, never its
  scene's bounds on screen), where the stem's voice has energy
  (`audio_qc.narration_windows`: voiced runs inside the span, split on
  1.5 s silences, at most 90 s each; a span with none is never sent, and the
  narration is `unvoiced` - missing from the render, a failure its own check
  never turns into an advisory). Each window hears its narration alone -
  every other sound of the stem silenced (`audio_qc._alone`) - and
  its words are that narration's by the window that heard them, never by the
  speech-to-text's times, which drift past a chunk's speech; a word a window
  times outside its narration's span (padded 0.2 s) is scored against
  nothing (`outside_narration_words`). A scene
  preview grades its own narration alone (`audio_qc.graded_narrations`): its
  MP4 keeps its transitions, where its neighbours' narrations play, and a
  part padded around its narration reached the last syllable before it and
  the first after it - takes trimmed tight, placed back to back - and graded
  them against its script ("8% of the narration's words heard wrong" where
  the whole render heard 1%). `test_video_studio_preview_narration_window`
  previews the middle of three narrated, crossfaded scenes at the whole
  render's word error rate for that scene. What the speech-to-text writes
  where nobody speaks is dropped before anything is graded and counted in the
  audio summary (`hallucinated: {segments, words, dropped: [{scene_id, at,
  end, text, why}]}`, `audio_qc.kept_words`): a window heard in
  another language than its narration's own check heard the take in
  (`voice_check.Attempt.language`, kept in the check record), else than the
  voice-over's (`audio_qc.voice_over_language`: the language most of its
  words were heard in), and, in each segment of the transcript
  (`Transcript.segments`), from the first word saying what its model is
  known to write over silence or music (`transcription.known_hallucinations`,
  declared with the model: whisper-1's "Diolch am wylio'r fideo", "Thanks for
  watching", ...) to the segment's end - unless the narration writes those
  words itself. A scene's word error rate is at most 100 %, the voice-over's
  its scenes' errors over their words. A scene heard wrong whose own check
  passed (`audio_qc.OwnCheck`, from `check_render`) is a `warn` advisory
  naming both transcripts (`data.advisory`, `heard`, `own_transcript`): the
  narration's own check decides its words, so one source of truth blocks; a
  narration never checked, or failed, is held to the render's hearing. A
  lexicon term its narration's words do not say is looked for in the whole
  transcript within `TERM_REACH` (3 s) of its planned time (its word paced
  over the placed span, `speech_timing.paced_words`), among the words no
  narration heard as its own (`audio_qc.heard_terms`,
  `pronunciation_check.found_reading`): heard there, it passes at that time
  (`terms_found_nearby`). A 181 s render whose 24 narrations had each passed
  was refused on whisper-1's Welsh closing line over music-only stretches,
  charged to the neighbouring scenes by one alignment of every word against
  every narration ("Available now" 500 % wrong while heard exactly), and on a
  brand term heard 2 s late reported "not heard";
  `test_video_studio_render_transcript_gate` pins each. A sentence of four words or more heard more
  times than written is a `repeated_sentence`. One that two narrations write
  is refused at save, before any preview (`audio_qc.shared_sentences`,
  `projects.shared_sentence_problems`, at the later scene's `narration`,
  naming every scene that says it): the same words once normalised
  (`audio_qc.written_sentences`), or close enough that one is a hearing of the
  other (`audio_qc._heard_as`, the gate's own matcher). Each scene's preview
  hears its narration alone and passes; the whole render heard it more times
  than either writes it (a 94-minute 4K render failed on it). Without a
  voice-over nothing is spoken and nothing is refused. A `stutter` is the same
  80-200 ms of sound three or more times in a row inside a voiced region: log
  energies in 40 bands every 5 ms compared at each lag, the chunk's spread
  over `CHANGE_DB` (a held tone or vowel repeats without stuttering), at
  least half of it loud, and the waveform repeating at the exact lag
  (correlation 0.8). Looked for on the stem and on the mix; one of the mix
  counts when its second chunk is nearer the voice's first than its own
  (`repeats_the_voice`), so a music loop is never one.
- **How loud the music plays** (`music_level.py`). The bed the mix
  took is measured (`encoder.momentary_loudness`) as it played: its
  integrated loudness where no one speaks, its level under the voice once
  ducked and its longest stretch under -35 LUFS, on the master's scale. The
  audio check's `music` section holds them (`MusicLevel.summary`), the tool
  answers' `Music:` line says them, and a silent or too quiet track, a long
  quiet stretch or music lost under the voice is a `music_level` warning:
  the render stays ready. A quiet stretch that starts in a fading bed's
  closing window is its decay under the closing card, a fact. See
  "How loud it plays" under "Music".
- **Frozen pictures** (`frame_freeze.py`). The MP4 streamed as 192-pixel-wide
  grey frames at up to 30 fps (`encoder.iter_gray_frames`), at a whole
  fraction of the video's rate (`frame_freeze.analysis_rate`: 25 for
  50 fps, 30 for 60) so each frame read is one encoded frame, its scene that
  frame's owner (`frame_freeze.Shown`, `frame_owners`); a run of frames
  under 0.3 grey levels of mean difference from the previous one and from the
  run's first - and with at most `MOVED_PIXELS` pixels more than
  `MOVED_LEVELS` off the run's first (a locked-off shot whose only
  motion is a small subject, a cat far down a corridor, changes a few pixels
  of the copy by tens of levels, which the mean reads as still) - and with
  its take's subject still too (`FreezeWatch._subject`: each frame
  names its scene, and the pixels whose grey has spanned more than
  `SUBJECT_LEVELS` over that scene's frames - each span closing by
  `SUBJECT_DECAY` a frame once the pixel stops changing, so a subject crossing
  a long take is where it moves now -, at least `SUBJECT_MIN_SHARE` of the
  copy, held to the same 0.3 levels of mean difference from the run's first:
  a low-contrast subject covering 5 % of a locked-off frame never moves a
  pixel past `MOVED_LEVELS`, and the whole frame's mean diluted its motion
  into 0.5 s frozen windows) -, longer
  than 0.5 s, is a `frozen` warning in its scene - outside
  what is still by design (`frame_freeze.designed_stills`): a title, a closing
  card, a statement, an image or a slide with `motion: none`, a figure (it
  rests between its steps; the planner marks it `figure: true`), a
  recording up to its end (a product at rest is what it shows; only its held
  end is footage that ran out), a scene's `hold_end` (the still beat its
  author asked for), and a blend (`frame_freeze.blended_windows`: a
  scene's `transition_in` of `BLENDED_TRANSITIONS` - crossfade, fade through
  black - over its duration, and the video's own `fade_in` / `fade_out`):
  the picture is meant to hold while it fades. A clip's screen inserts are
  watched apart from its footage (`frame_freeze.frame_parts`): each
  insert whose layer is drawn (`timeline.insert_drawn`: `[t0, t1)`, a keyed
  one's whole scene) is a mask of the grey copy where the page draws
  it - its `quad` on the footage box `footage_box` reproduces from
  `buildStill` (cover, else held clear of the frame's edges; the caption
  band the page may also keep it clear of is not known there), the `box` of
  a tracked one -, the footage the rest. Each part (`FreezeWatch._Part`) is
  held to the thresholds on its own pixels, its subject's share of its own
  area, and keeps its own run; the picture holds only while every part does
  and the same parts are shown. A static page over a candle flickering in a
  locked-off room, or a still subject under a page that scrolls, is not
  frozen (the whole frame's measure diluted the flame under the page), and
  the finding says which part stopped moving and which were still
  (`StillParts`, `data.still` / `data.stopped`: `its footage stops moving
  while its screen insert 0 is still`), its remedy the footage's or the
  insert's source. The cause is fixed in the
  planner: footage shorter than its scene plays its `extend_mode`
  (`spec.EXTEND_MODES`, `timeline.extension`) - `zoom` (the default: the
  footage fills its scene and drifts in on what still holds, as described in
  "Footage fills its scene" above; the planner leaves it unmarked),
  `loop` (at its own speed, then its last second again and again, each
  crossfaded 0.3 s into the next until the scene's `hold_end`: two layers in
  the page, the frame server keeping those frames, `timeline.loop_frames`, so
  it never decodes backward) or `hold` (at its own speed, its last frame still:
  `extend: {mode: hold}` turns the page's drift off). A loop or a hold never
  slows the footage or relaxes a recording's pauses. Clips and animated images
  take all three, a recording `zoom` or `hold` (its cursor and address bar
  would jump back in a loop). A clip too short to loop (under 0.3 s) drifts
  like `zoom`.
- **A watch that never holds the render.** The watch judges the MP4
  one `WINDOW_SECONDS` (10 s) window at a time, each within a wall-clock
  budget (`frame_freeze.window_budget`: `WINDOW_BUDGET_SECONDS`, 60 s, never
  past half `VIDEO_STUDIO_RENDER_STALL_SECONDS`). A window whose decoder
  stops delivering frames, or whose frames take longer than that, is left
  unjudged: its time range, its scenes and why are logged and kept in the
  check's summary (`frames.unjudged: [{at, end, scenes, why}]`, said in the
  `Picture:` line of the answer), the run under way ends at the last frame
  judged, and the watch goes on from the next window on a decoder started
  there (`encoder.iter_gray_frames(start=...)`, a decoded seek). A scene
  still by design from end to end (`frame_freeze.still_by_design`: a title or
  a closing card - a template's intro and outro are such cards -, a
  statement, a motion scene, an image or a slide with `motion: none`, a
  figure) is never compared: its frames end the run under way
  (`FreezeWatch.rest`), and how long its picture holds is measured on the
  frame alone (`DesignedHolds`: the mean difference from the hold's first
  frame), reported as `frames.by_design: [{scene, type, at, end, seconds,
  because}]` and in the answer's `by_design`, never as a frozen picture.
  After each window what was judged is kept in the render's working directory
  (`frame-watch.json`, `frame_freeze.Checkpoint`), keyed by the drawing's
  digest, the frame rate, the size and the window's offset: a resume goes on
  from the start of the run or hold under way (`frames.resumed_at`), never
  from the first frame.
- **Progress through the checks.** The check stage is the render's
  progress like any other: `check_render(progress=...)` is told each
  narration heard - before the first part is transcribed and after each
  (`audio_qc.check_audio(heard=...)`: "Hearing the render: 7 of 19
  narrations heard", at 93 %), a narration read from its own transcript
  heard at once - and each `frame_freeze.WINDOW_SECONDS` (10 s) window of
  picture as it starts, named with its scenes (`check_frames(watched=...)`,
  `frame_freeze.Window`: "Watching the render for frozen pictures:
  190 of 194 s, now 3:10.00 to 3:14.00, scene `s12` (outro)", at 94 %), and
  once more when the watch ends. The read-back of the screen inserts off the
  encoded MP4 that follows is a stage of its own ("Reading the screen inserts
  back off the render"), its decoded frames heartbeats
  (`insert_checks.presence(on_frame=...)`): it was said as the watch's last
  window. Each writes the stage and `progress_at`,
  so `get_video_jobs` tells a check that stalls (its stage and its last
  progress standing still) from a long one, and a stall's message names the
  window and the scenes it stood on; before, the stage sat at
  "Hearing and watching the render" with only the lease's heartbeat moving.
  The speech-to-text failing stays a note; what the progress raises - the
  render cancelled - stops the check, never a check that could not run.
- **The outcome.** An audio finding (`fail`) fails a whole render
  (`failure_summary`, its MP4 and poster kept to look at, never made
  current); a frozen picture (`warn`) leaves it ready. A check that cannot run
  is a `skipped` part with its reason, never a failed render.
- **The gate** (`render_checks.gate`, from `projects.start_render`, so the
  editor and `render_video` alike). A whole render is refused
  (`RenderGateError`; 409 `render_gate` with the reasons for the editor,
  which lists them and offers to render anyway) when an AI shot or a
  recording a scene plays is not ready (`media_not_ready`: its
  footage, a statement's backdrop, a screen insert's source, an image's
  animation, through `clips.playable`, each with what a render does with it -
  refuses it, plays the still); when the quality check
  saved for the revision has a finding the audience would see
  (`quality`: `BLOCKING_SEVERITIES`, `overloaded` or `dense`; read with
  `check_video_quality(measure=False)`, the layout kept for the revision
  and the content findings fresh, so the gate never plays the video, and a
  `shot_state` finding is left to `media_not_ready`); when a narration of the
  video failed its last check; when the latest whole render of the same
  revision left a failing (audio) finding - whatever the quality, the
  narration and the plan decide it - or the latest at the same quality left a
  frozen picture (the same video again: a draft's warning never stops the
  final render); or when a scene changed since a ready preview or whole
  render last drew it (a scene never drawn never holds a render). What was
  drawn is kept on the video, not on the
  preview rows the retention deletes: `VideoProject.scene_previews` (`{scene
  id: {digest, render_id, at, passed, version, drawings}}`,
  `render_checks.record_seen`, written with the project row locked when a
  render ends ready; `passed`: its checks left no failing finding and no
  frozen picture in that scene, `scene_passed`). A scene keeps its last
  `DRAWINGS_KEPT` (8) drawings by digest, newest first (the top of the
  entry is the newest): a digest drawn again replaces its older drawing, and
  the gate reads the drawing of the digest the scene has now
  (`drawing_of`), so an edit previewed then reverted finds the preview of
  what the scene shows again - the content decides, never the revision nor
  the last drawing. The digest (`render_checks.scene_digests`) is what
  a preview of the scene draws: what it shows - its spec as the template
  dresses it and as it reads the video (its transition in, the
  video's `default_transition` when it names none; a clip's own sound as
  `clip_sound` decides it, `spec.clip_sound_choice`; the video's fade in on
  its first scene, its fade out on its last), the caption text a lexicon entry applied to the captions writes
  for its narration (the platform's entries included,
  `lexicon_store.for_voiceover`) and the take it plays (a recording's or a
  shot's file while it plays, a deck's revision - never a job's status, so a
  shot generated again whose take still plays, or whose attempt failed beside
  it, changes nothing) -, what its preview draws of the scenes before and
  after it: each neighbour's picture at the edge - its spec but its
  narration, the pause before it and its own transition in
  (`render_checks._UNDRAWN_IN_A_NEIGHBOUR`), and the take it plays - and the
  transitions between them as the preview's window plans them
  (`projects.preview_transitions`: type and duration, from the estimate's
  lengths, `timeline.preview_window` the window `render.scene_window`
  plans) -, the video's look (format, theme, captions) and its music (an AI
  track's take). A neighbour's narration - its words, its captions - is its
  own preview's and the narration check's: rewording a scene's narration
  leaves its neighbours' previews current, unless it changes a transition
  between them (a scene too short to dissolve from, lengthened by its
  narration). The voice-over is not
  part of it: a voice, a style or a lexicon reading changed makes no
  scene stale, since a whole render speaks and hears every narration before it
  draws a frame (`voice_check.speak_checked`, its record per narration digest)
  and stops on one that fails - the narration check without the frames.
  Editing one scene's picture marks three scenes at most - itself and its
  neighbours -, its narration one, as changed since their preview,
  and a shared input the scenes that read it; `save_spec` says which
  (`SavedSpec.changed_since_preview`, a save note,
  `scenes_changed_since_preview` of `PUT /spec` and `POST /operations`,
  hashed with the gate's lexicon), and which are back to an earlier drawing
  (`restored_preview`, `scenes_restored_preview`: "that preview counts
  again"); every other scene keeps its preview. A drawing also records its
  content digest (`render_checks.digest_scenes` -> `SceneDigests`,
  `record_seen(content=...)`): the same hash less the video's theme
  (`_SHARED_LOOK_KEYS`) and its music. A scene no drawing shows as it is now
  whose content matches a drawing that PASSED keeps that drawing
  (`drawing_of(content=...)`): the gate counts it previewed and passed,
  `changed_scenes` leaves it out and the save says so (`kept_scenes`: "only
  the video's theme or music changed"). A drawing that failed is never kept
  so - the theme may be its fix. A watermark moved, a backdrop or a track
  changed then leaves every passing preview standing (a watermark change used to
  invalidate all 20 scenes of a film); the whole render draws and
  checks the new look. A drawing recorded before the content digest matches
  by its whole digest only; the whole digest is unchanged. A drawing recorded under
  another `DIGEST_VERSION` (now 4) is no drawing (neither changed nor current).
- **The scene as it renders, never the dump of its defaults.** The
  `whole` and `content` digests hash each scene settled: its fields set to
  anything but their default (`model_dump(exclude_defaults=True)` of the
  parsed scene, its own transition kept even when all defaults), the look and
  the music likewise, and without what draws nothing
  (`spec.UNDRAWN_SCENE_FIELDS`: `consistency`, how an AI shot is generated and
  checked). Before, the digest dumped the effective spec with every default
  filled: a release adding a defaulted field (`consistency: []`) moved
  the digest of every clip scene and its neighbours, and a `references` /
  `consistency` edit undone still left every scene changed (an 18-scene film
  refused whole). Drawings recorded before are matched through
  `SceneDigests.aliases` (`{old digest: digest now}`: the defaults-filled hash, with
  and without the undrawn fields), applied on read by
  `render_checks.adopted` in every reader of `scene_previews` (the gate, the
  save, the render's earlier readings, the quality check's cleared shots); a
  new drawing is recorded under the digests of now. Each drawing also keeps,
  on the newest one only, `parts` (`SceneDigests.parts`: a short fingerprint
  of each of the scene's fields, each overlay field `overlay N <field>`, its
  media, its planned transitions, its neighbours' edges, the look, the theme
  and the music), so the refusal and the save name what changed
  (`render_checks.changed_parts`: `narration`, `overlay 0 quad`,
  `transition in`, `scene before`; at most `PARTS_NAMED`). The save note
  "Previews that still hold" and `get_video` list the scenes a render needs no
  new preview of (`render_checks.holding_scenes`, `projects.preview_holds`);
  the REST saves answer `scenes_preview_holds` and `scenes_changed_parts`.
  The `drawn`, `heard` and `picture` digests a whole render is carried and
  covered by are unchanged (still the full dump).
- **A save that changes nothing.** A save whose authored spec is byte-identical
  to the stored one (canonical JSON, `projects.same_spec`) on the same
  template - once its shots and AI track are named - writes nothing
  (`SavedSpec.unchanged`): no revision, no audit entry, no preview out
  of date; `edit_video` answers "No change" and the REST saves answer
  `unchanged: true`. The gate trusts a passing preview: a finding
  about an AI shot (`QualityFinding.clip_id`, the camera moved, its state) is
  cleared once every scene showing that shot passed a preview drawn as it is
  now - at its current trim (`cleared_shots`) -; the quality check says so
  itself, an advisory naming the passing preview and the revision it drew
  (`render_checks.cleared_by` over the drawings' `revision`,
  `quality.ContentFacts.cleared`) -, and a finding a previous
  render of the revision left on a scene is cleared once a newer drawing of
  that scene passed; a scene finding of the layout (a reading time, an
  overlap) is never cleared by a preview. So a video whose every scene preview
  passed renders without `force`. `force: true` starts it anyway; only a refusal
  overridden is recorded - `VideoRender.gate_override` (who, when, the
  reasons), a warning logged, `forced` and `gate_reasons` on the audit row -
  and the forced render places its failed narrations and keeps its findings
  without failing on them.

A video has one render in progress at most. Both entry points start a
render through `projects.start_render`. A start asking what is already being
made - the current revision at the same quality, queued or running - answers
that render (`projects.render_in_flight`, `StartedRender.reused`, the
route's `reused`): nothing is gated, cancelled, queued or audited again, and
the asking chat is owed it when nobody is. A render the engine runs again
after a lost lease or a crash is that same row, so a relaunch follows it and
the render history shows it once; the twin read again under the start's lock
covers two starts racing. Any other start cancels every render of the
video still queued or running (`jobs.cancel`: a running one stops between
frames, a queued one never starts) and records on each the render that
replaced it (`VideoRender.superseded_by_id`, migration `v5991rsupr01`; its
error says so too, and `mark_cancelled` keeps that reason when the worker
ends the job). The starts of one video are serialized by a transaction-level
advisory lock and the replaced renders are read `FOR UPDATE`; the worker locks
its row when it starts and when it saves the result (`render.finish_ready`),
so a render replaced at that moment ends either replaced or ready, never both;
every failure it writes goes through `jobs.mark_if_open` (a conditional
`UPDATE ... WHERE status IN (pending, running)`), so one replaced
meanwhile keeps its `cancelled` and its reason, and a crashed render replaced
before its retry is not queued again. The person's limit of
renders in flight (`VIDEO_STUDIO_MAX_RENDERS_PER_USER`,
`jobs.max_renders_per_user`) counts the replaced renders out:
`RenderLimitError` carries the renders of other videos it found, which the
route and `render_video` list. The starts of one person are serialized too
(the video's advisory lock, then the person's, always in that order),
so two starts on two videos never both pass the limit. The start answers
`superseded_render_ids`, and `render_video` / `get_video_jobs` point an agent
from a replaced render to the one that replaced it - or say that one no longer
exists when the retention or a manager deleted it. A replaced render
owed to a conversation (a `render_video` whose turn ended) hands it to the new
render when the same person starts it (`projects.inherited_conversation`,
never across people: the wake runs as the new render's `user_id`), so that
conversation hears how the replacement ended.

A revision that changes nothing drawn never cancels a render. A whole
render records, as its attempt reads its spec, what it draws
(`VideoRender.drawing`, migration `v6339draw01`, `render.drawing_of`,
written under the row's lock as the attempt starts): each
scene's drawn digest (`render_checks.SceneDigests.drawn` - the scene's
`whole` digest less a clip's own sound and level, `spec.HEARD_SCENE_FIELDS`,
and the music unless the picture follows its beats, with the voice-over but
its level, `spec.HEARD_VOICEOVER_FIELDS`, which times the narrations and
their captions, and a deck by its slides as an audience sees them, without
their speaker notes) and the sources of its music
(`render_checks.sound_sources`: what it gathers before it draws). A save
locks the whole renders of the revision it replaces that are still in
progress (`projects.renders_to_carry`) and carries to its new revision those
whose recorded digests are its own (`projects.carry_renders`; a queued one,
which reads the spec when it starts, is compared on the spec it was asked
for; one that started before recording is never carried): its `revision`
moves, `drawing` keeps the revision it was first asked for
(`carried_from`), the new effective spec and its scene digests, and the save
says so ("Kept: the render in progress ... draws exactly what revision 13
draws"). The next `start_render` of that revision and quality answers it
(`StartedRender.carried_from`, `render_video`: "started for revision 12,
draws exactly what this revision draws"), and the render, finding at its
audio stage that it was carried while it drew, mixes the sound of the
revision it finishes for over the frames it drew (`render._sound_of`: the
clips' own sound replanned, the voice-over's level, the music's settings),
records that revision's scene digests and is its render - a legibility
declaration, a deck's speaker notes or a clip's audio flag no longer cost a
cancel and a redraw.

A render of an older revision is superseded by the newer one, never left to
draw on. A save that changes what it draws leaves it running, and the
person is waiting on it: once the gate passed, the editor's start
(`ask_before_replacing`, the route's default until `replace: true`) raises
`projects.RenderReplaceAskedError` - 409 `render_replace`, its renders with
their revision, quality and progress - and the Render panel asks once
("The render of revision 99 is 35% done. Cancel it and render revision
100?"); `render_video`, an automation, cancels it and says so
(`projects.replaced_line`). Either way the new render keeps the frames of
the scenes its revision left alone:

- **Window digests** (`compositor.WindowDigests`). Each segment a render
  closes records, beside its revision and attempt, the render that drew it
  and a digest of what its frames are drawn from (`compositor.DRAWN_SUFFIX`,
  `window`): the scenes on screen whole - their start, end and index
  included, so a scene moved draws other frames -, the branding of the scene
  before (`BRANDING_FIELDS`, what `brandsItself` reads for the watermark),
  the cues over those scenes, every cue when the watermark stands on the
  captions' edge (it is lifted above the tallest), the look of the whole
  video, the fades and the last frame only where the window reaches them,
  each file those scenes name and each file no scene names by what it holds,
  the preset, the output size and how the build draws a frame
  (`compositor.drawing_release`: a digest of the compositor page's CSS,
  script and Lottie player, `screen_insert.DRAWING_VERSION`,
  `insert_builds.BUILD_VERSION` and the Playwright release), never the
  build's version: a rollout that draws alike keeps every window, so a
  render handed to a worker of the next build takes back its stored
  segments (`render_segments.take_back`) and resumes where it stood, never
  from frame 0 (#8819). A tripwire pins the page's two cross-scene reads.
- **Hand-over** (`render_segments.handed_over`). A render replaced by a
  render of its video at its quality still queued or running keeps its
  stored segments when it ends (the render process skips `forget`; the
  hourly `sweep` keeps them too); the new render deletes them once it ended
  (`forget_handed_to`).
- **Take-over** (`render_segments.take_over`, `compositor.render_frames`'s
  `predecessors`: the renders it replaced at its quality, read as it
  starts). After its own kept segments, a whole render drawn in one range
  takes every predecessor segment, clear of what it kept, whose recorded
  window digest is the digest of its own same frames - with its audits and
  evidence frames - and stores it as its own, so a resume elsewhere has it.
  Its notes say what it kept by scene (`render.inherited_line`: "Kept from
  render `...` (revision 99), which this render replaced: 1,800 frames of the
  scenes this revision leaves unchanged (s01 600, s03 1,200)"), and its
  measured record keeps `frames_inherited` and `inherited`, out of its
  `frames_reused`, its rate and the estimate's error. A segment over the
  changed scene, or recorded without a window, is drawn. A
  render drawn in scene ranges takes nothing.

A ready render already covering the revision is answered, never drawn again.
Once ready, a render was kept and valid across such a revision, yet
`render_video` on it queued a new full render of 20 to 60 minutes. A whole
render also records what it plays beside its frames (`drawing.heard`,
`render_checks.SceneDigests.heard`: the voice-over's level, the lexicon its
narrations are said with, each scene's own sound and level, the music and
the take it plays - a carried render the revision it is carried to, whose
sound it mixes). Before anything else, a start that is not forced compares
the video's latest ready whole render (`projects.covering_render`) with the
current revision: the quality asked, its checks without a finding
(`render_answer`: all gates green), every scene's drawn digest and the heard
digest. All matching, it answers that render - nothing linted, gated,
queued, cancelled or audited (`StartedRender.covered_from`: the revision it
was drawn for, the route's `covered_from`) - and that render becomes the
render of the current revision (`revision` moved, `drawing.drawn_for`
keeping the one it was drawn for), as a carried one does. `render_video`
says "Render `<id>` already covers revision N, nothing to draw ...", and
`force: true` renders again anyway; the editor's Render panel says "Already
rendered" with a "Render anyway" action. A legibility declaration or a deck's
speaker notes is covered; a scene's trim (drawn) or a clip's own sound
(heard: the ready render plays the sound it was mixed with) is not, nor
another quality, a render with a finding, or one made before it recorded
what it plays (`tests/test_video_studio_covering_render.py`). Nor is a
render this release would draw otherwise: every whole render records
the release that drew it (`drawing.release`,
`render_checks.release_drawing`: `screen_insert.DRAWING_VERSION` and
`insert_builds.BUILD_VERSION`), and one whose versions are not the current
ones - or that recorded none - is drawn again without `force`, the start
saying why (`render_checks.stale_release`, `StartedRender.redrawn`, the
route's `redrawn`, said first by `render_video`): "Render `1d30f3fe` draws
every scene of revision 136 at high as it is now, but was drawn at
screen-insert drawing version 7, now 8: rendering again." - where a matte
fixed changes every insert frame and no scene's digest.

A ready render playing a fallback take is re-checked, never kept for good
(`voice_recheck.py`). A narration the video's voice fails at its speed
is placed slower (0.85), with a term set apart, or in another voice; a later
change of the voice pipeline or of that term's lexicon entry may let it pass
at full speed, yet the render kept being answered "already covers".

- **The pipeline version.** Every check record keeps the voice pipeline it
  was made under (`voice_check.pipeline_version`: `RECORD_VERSION`,
  `lexicon.RULES_VERSION`, `voice_check.LADDER_VERSION`). A record placing a
  fallback take under another pipeline no longer places it unheard
  (`voice_check.stale_fallback`): the ladder hears the narration again from
  its first take, in a render, a preview or a re-check alike, and
  `passed_take` skips such a take.
- **What a render records.** Beside `drawing.scenes` and `heard`, a whole
  render records `said` (each narration's lexicon entries), `beside` (`heard`
  less the lexicon) and `picture` (each drawn digest less the voice-over's
  pronunciations, which draw nothing but the narrations' lengths), the take
  each narration plays (`takes`), each scene playing a fallback take
  (`fallbacks`: the take, why, its fix and its reuse digest,
  `render_checks.fallback_digest` = its `said` and the pipeline) and what its
  frames were drawn from (`encoded_as`, `compositor.drawing_digest`). Its notes
  say each fallback with the fix that clears it.
- **The offer.** `covering_render` (`voice_recheck.offered`) offers a re-check
  of each scene whose fallback digest moved - the pipeline, or the lexicon
  its narration writes - when nothing else did (`picture`, `beside`, every
  other narration's `said`). `start_render` answers that render with
  `StartedRender.recheck`, nothing queued (the route's `recheck`), and
  `render_video` says "Render `X` covers the picture of revision N; scene
  `s2` plays a 0.85 take, the voice check changed since: re-check it?". A
  render playing no fallback, or whose fallbacks did not move, is answered
  "already covers" as before; a render that recorded no fallback is offered
  from its voice summaries (a slower take, under an unrecorded pipeline) - a summary
  counts its takes (`SpokenSegment.summary`, `attempts` an int), so
  `FallbackTake.of_record` reads the takes only from a check record, and
  `covering_render` reads each scene's record from the voice cache
  (`voice_recheck.records_of`) to say what the takes heard wrong. An offer
  that cannot be computed never stops the start: the render covers the
  revision as before, the start says why (`StartedRender.unrechecked`,
  said by `render_video` after "already covers") and logs it.
- **The re-check.** `render_video(recheck: true)` - at once in an
  automation (`build_video_studio_tools(automation=True)`, the assignment
  engine) - speaks and hears each offered narration again, on no session
  (`voice_recheck.recheck`). A scene still needing its fallback is said with
  its fix and settled on the render (`drawing.rechecked`: never offered again
  at that digest). One that now passes at the video's speed is spliced
  (`voice_recheck.Splice`, `drawing.splice`): a new whole render places every
  other narration with the take the ready render played
  (`voice_check.place_kept`), and when its `encoded_as` is the ready
  render's, muxes its new sound onto that render's MP4 - no frame drawn
  (`measured.frames` 0, `frames_spliced`, `spliced_from`), its picture
  checks that render's (`checks.frames`, `checks.inserts` without their
  crops), the frozen-picture watch skipped. A new take that changes what is
  drawn (a scene's length or its burned-in captions follow its narration)
  is drawn again, its first note saying so
  (`tests/test_video_studio_fallback_recheck_6759.py`).
- **The save.** The quality check lists each narration placed slower or with
  a term set apart as a fact, with the fix that would clear it - a lexicon
  entry the voice says right, or a reworded clause
  (`voice_check.fallback_takes`, `quality.fallback_note`); another voice stays
  the `voice_fallback` finding.

#### Re-judging a ready render (issue #10885)

A render keeps the report of the quality report version that judged it
(`render_checks.report_provenance`). `get_video_jobs(render_ids=[...],
rejudge=true)` judges each ready render again under the running
`quality.QUALITY_REPORT_VERSION` (`render_rejudge.rejudge`), read-only on its
stored MP4: nothing is drawn and no render is queued.

- **Pictures**: `frame_freeze.check_frames` and `cut_blend.read_cuts` on the
  render's own encoded frames, the plan read from the spec of the render's
  revision (`revisions.spec_at`, `projects.effective_spec(authored=)`), bounded
  by `REJUDGE_BUDGET_SECONDS`; a stretch not judged in time is listed under
  `sampled`, never passed in silence. A scene preview is not watched again.
- **Content**: `quality.check_content(authored=)` on that revision, each
  finding (a recording scene's `synthesised_transition`, `stepping_motion`,
  `take_capture_rate`...) placed at its scene's start in the render; `dense`
  and `overloaded` are failures, as the render gate counts them. Bounded too:
  at most `CONTENT_SHARE` of the budget; a content check not finished in it is
  listed under `sampled`.
- **Several renders** (`rejudge_many`): the call's wait budget is shared, each
  render given its share of the time left (at most `REJUDGE_BUDGET_SECONDS`);
  once a share falls under `REJUDGE_MIN_SECONDS` the renders not reached are
  answered as `not_rejudged_yet` with the call that judges them.
- **Stored** beside the old report as `checks.rejudged`, one entry per
  version (the newest `REJUDGED_KEPT`), only when the caller may manage the
  video; the report the render was judged with is never changed.
  `report_provenance` names the newest re-judge (`rejudged`), and
  `provenance_line` says it instead of "a new render is judged anew".

Every finding carries its MP4 time and `data.frame`
(`tests/test_video_studio_render_rejudge_10885.py`).

- **Revision no longer held** (#10899): `revisions.record_revision` never
  prunes the revision of a ready whole render. A render whose revision is gone
  anyway (saved before the history, or pruned before this rule) is judged on a
  held revision that draws and plays exactly what it recorded
  (`render_rejudge.same_drawing_spec`: `VideoRender.drawing` `scenes` and
  `heard` against `digest_scenes`, the current revision first, then the oldest
  held, at most `SAME_DRAWING_TRIES`), and the answer names that revision; none
  matching answers why, with "render the video again"
  (`tests/test_video_studio_render_rejudge_pruned_10899.py`).

### A render's process and memory

A render never runs in the worker's own process: the `video_render` adapter
(and `video_preview`, the scene previews' lane) calls
`render_process.run_isolated`, which starts `python -m
app.services.video_studio.render_process <render id>` (it runs
`render.run_render` unchanged) and supervises it from the worker's event loop.

- **Memory ceiling.** Every second the supervisor measures the render process
  and everything it started - Chromium, the ffmpeg encoders and decoders - by
  their proportional share (`resources.tree_memory`, PSS from `/proc`). Past
  `memory_ceiling()` (`VIDEO_STUDIO_RENDER_MEMORY_MB`, by default 60% of the
  container's memory limit, 4096 MiB without one) it kills the whole tree,
  Chromium's own process group included, and fails the render with its peak
  memory and the scenes it was drawing. The kernel never OOM-kills the worker,
  so the assignments, embeddings and AI shots beside it keep running. A scene
  preview is held to its own, smaller ceiling, and starts beside a whole
  render only in the memory that render leaves (scene previews, "Memory
  beside a whole render").
- **Death seen at once.** The process writes its stages, progress and outcome
  as JSON lines on its standard output (its logs go to standard error, so to
  the pod log). One that ends without an outcome crashed: the supervisor names
  the signal or exit code, the progress, the scenes being drawn and the peak
  memory in the same second, never through a lease expiring. The worker makes
  the render's working directory (its media, chunks and outputs): a killed
  process cannot clean up, and its files would fill the worker's `/tmp`, a
  size-limited volume in the Helm chart. It is one directory per render
  (`render_process.workdir_of`), removed once an attempt ended the
  render, kept when the attempt runs it again (see "Retry").
- **Stall, resumed in place.** A render whose progress stands still
  for `VIDEO_STUDIO_RENDER_STALL_SECONDS` (120), at any stage, is killed and
  started again by its supervisor in the same working directory, under the
  same execution: its lease is renewed first (`lease.renew`; a lease the
  engine took back meanwhile stops it, `lease_lost`), and the frames it had
  finished are kept. The compositor encodes each page's run as segments of
  `compositor.SEGMENT_SECONDS` (10 s), each closed, then renamed whole
  (`seg-<first>-<end>.mp4`), under `segments/` with the digest of what they
  were drawn from (`drawing_digest` over `drawing_inputs`' named parts: the
  document, the preset, the size, the range and, every source,
  picture and screen insert the frame server reads - each file by what it
  holds, `compositor.file_identity`: `file:<id>` from the file it was
  downloaded from, `sha256:<digest>` for one the render made, a range
  worker naming its fetched files the same way from the bundle's refs - and
  each insert by every field it is drawn by: file, quad, fit, key with its
  colour, tolerance, feather, sampled colour and spread, glow, track, matte
  footage, first frame, box, and its payloads in the plan, timing and trim.
  The document alone never carried the key, the track's quads, the matte
  footage nor which file a source plays, and each attempt plans again from
  the project's current spec: an attempt resumed after a key-only edit kept
  the segments drawn with the old key): a resumed process plans again, finds the same digest and draws
  only the segments missing (`kept_segments`), the progress counting the kept
  ones; a digest that changed (the video saved meanwhile) draws everything.
  Once every frame is encoded, the silent video is recorded beside them with
  that digest and the screen inserts' audits (`encoded.json`,
  `compositor.keep_encoded`): a resume past the encode - a stall in
  the checks - keeps it whole (`compositor.encoded_video`), draws and joins
  nothing, mixes and muxes again and checks the MP4, its frozen-picture watch
  going on where it stood (see "A watch that never holds the render"). The
  frames a stall, a retry or a handover says were kept
  (`compositor.frames_kept`) are all of them once encoded, else the finished
  segments of the render's own directory and of its scene ranges'
  (`range-<n>/segments/`): a render drawn in ranges, or one stopped in
  its checks, no longer says "0 frames kept".
  Each segment closed records who drew it (`<segment>.drawn.json`,
  `compositor.DRAWN_SUFFIX`: the revision and the attempt, stored with it by
  `render_segments.keep`); the attempt that keeps segments reads them back
  (`compositor.kept_from`, `DrawProgress.reused` and `.matched`), the render
  records them (`measured.segments_reused: {segments: [{frames, segments,
  revision, attempt}], matched: {part: digest}}`,
  `render_preflight.segments_reused`) and every answer that says what it
  measured says which frames were kept, for which revision, by which
  attempt and the digest parts they matched on (`render_preflight.reused_line`),
  or "no frame was kept from an earlier attempt" - a scene preview's answer
  included. A preview is a render of its own, with its own working
  directory: it never takes another render's segments.
  Each stall is recorded on the render (`VideoRender.stalls`, migration
  `v6101stall01`: when, its progress and stage, the scenes drawn, how long it
  stood still, the frames kept, whether it was resumed;
  `render_process.record_stall`), its stage saying it resumes; `get_video_jobs`
  and the render's answers say them (`render_process.stall_line`) and the
  render serializes them. Past `MAX_STALL_RESUMES` (2) it is failed as
  "stalled at X%", saying how many times it was resumed. Every progress
  report counts: frames drawn (every 15, at most every 2 s), each scene's
  media gathered, each narration spoken, each stage - and every
  frame loop between them (a `heartbeat` event at most every
  `render.HEARTBEAT_SECONDS`, 10, from the frames drawn, followed or watched,
  the frozen-picture check's included) and an encode while it runs
  (`encoder.run(on_wait=...)`, a figure's video: "Encoding figure 1/1"), so a
  2160p stage that draws or encodes for minutes is never taken for a stall;
  a hung ffmpeg is still stopped by its own timeout. The render's
  `progress_at` (migration `v5951rprog01`) is served with its status, and a
  render resumed (or run again by the engine) keeps its `started_at`.
- **Retry, resumed.** A render execution has three attempts
  (`jobs.enqueue`): a crash runs it again at the same quality (the row goes
  back to `pending`, its stage saying why), and so does a lease the engine
  took back; a crash on the last attempt fails it ("This was its last attempt
  (3 of 3)."), unless the platform failed it (below). An attempt ending in a way that runs the render again
  (`lease_lost`, `retry`, `handover`, `again`: `render_process._RUNS_AGAIN`) keeps its working
  directory, so the next attempt on this worker resumes there, its stage
  saying what it kept ("Resuming (attempt 2 of 3): 1500 frames and 285 figure
  frames kept"): the compositor's finished segments, and each figure scene's
  frames and encoded video (`figures.draw`), all kept by the digest of what
  they draw (`compositor.same_drawing`, the one rule) and drawn again only
  when that changed. The render process leaves a working directory the
  supervisor gave it to the supervisor (`run_render` removes only one it made
  itself: it used to remove it on a lost lease or a retry, so the next
  attempt found nothing). Each segment closed is also stored beside the
  render in the object store (`render_segments.keep`, under
  `video-studio/render-segments/<render>/<digest>/`) - after its matted
  screen inserts' audits (`<segment>.audits.json`) and the evidence
  frames they name, so a segment stored never lacks them - and an attempt
  whose directory lacks a segment of the same digest downloads it with them
  (`render_segments.restore`, from `compositor.render_frames(render_id=...)`):
  a render resumes from its finished segments on whichever worker takes it -
  after a deploy, the new pod's empty `/tmp` included. They are deleted once
  an attempt ended the render (`render_segments.forget`), and hourly for a
  render that ended or is gone (`render_segments.sweep`, the scheduler's
  Video Studio retention). A figure's key is what its frames are drawn from
  (`figures.render_key`), never the page's document, which gives the deck
  and its slide new ids on every read, so no attempt could keep a figure
  keyed on it. A figure frame is screenshot beside its name and renamed
  whole, and its video counts as finished only once it is encoded (the
  `encoded` marker), so a frame or a video cut short is never kept. Each
  attempt registers in `render_process._ATTEMPTS`: a newer attempt of a render
  on the same worker - the engine gave it the execution - stops the one before
  (`superseded`, killed at once, `lease_lost`) and waits for it before it
  starts, so two processes never share a directory. Before each attempt
  `sweep_workdirs` removes the directories no attempt will resume: their render
  ended or is gone, or nothing touched them for 6 hours (the next attempt ran
  on another worker, which starts from zero: the directory is the worker's
  own disk, and its segments come from the store).
- **A failure of the platform runs the render again once
  (`render_retry`).** Some failures name what to change in the video; others
  come from the platform and no edit fixes them: the render's headless
  Chromium refusing or timing out a protocol call (`compositor.render_frames`
  raises `ChromiumError`, never a `RuntimeError` - those are the video's
  media failures -, its outcome's `cause` carried through the process's
  `outcome` event to `RenderExit.cause`), the memory ceiling or the kernel's
  out-of-memory killer, the process dying, a stall past its resumes, the
  worker's lease lapsing with no attempt left. Such a failure where the
  render would otherwise end - its Chromium on the last attempt (an earlier
  one is the engine's retry, as before), the ceiling and a stall at once, a
  crash on the last attempt, a lease dead-lettered that was not moving -
  runs it again once (`render_retry.run_again`, from `render_process.settle`
  and `VideoRenderAdapter.on_dead_letter`): its row back to `pending`
  ("Running again once on revision 4: it ran out of memory, not the video
  (...)"), its execution deferred first in the queue after
  `RETRY_DELAY_SECONDS`, no attempt spent (`again` ->
  `AdapterResult.defer`, `INFRA_RETRY_STATE`; a dead letter is queued again
  with `requeue_execution`), `INFRA_RETRY_KEY` on its payload so it never
  runs again a second time, its working directory and stored segments kept
  (`again` is in `_RUNS_AGAIN`). Only while its video is still at the
  revision it drew (`render_retry.same_revision`): one changed meanwhile is
  the author's to render again. A render that fails anyway records why on
  its row (`VideoRender.failure`, migration `v6329fail01`:
  `render_retry.failure_record`), and its answer's `render_failed` finding
  says the failure was the platform's, that it ran again once on its
  revision, and that nothing in the video needs to change
  (`render_retry.finding_words`) - never "fix what its reason names" on a
  protocol error.
- **A take handed back gets its whole time again; only a cancel ends it
  cancelled (#9780).** A 27-step 60 fps take stopped by a rollout at its
  pacing was handed back from its keyframes, but `defer` kept the runnable
  time it had left: the next worker's run hit that deadline and
  `runners.run_take` read the cut as the person's cancel ("Cancelled.", 80%).
  `_hand_back_take` now gives the execution a whole take's deadline again
  (`fresh_take_deadline`, `jobs.take_deadline`, never moved in); a run cut
  with no cancel asked (its `cancel_event` unset, the row not `cancelled` by
  `jobs.cancel`) and no worker stop fails with `TAKE_CUT_ERROR`, naming the
  rollout or drain and how to record it again (`_ended_on_cancel`); a cancel
  keeps who asked (`jobs.cancelled_by`: "Cancelled by a@b.c with
  cancel_video_job."). `get_video_recording` answers a failed or cancelled
  take as a status, never a tool error.
- **A worker that stops hands its render over.** Render `41a8ae71`
  failed "Lease expired at 38%, no attempt left": a deploy stopped its worker
  and its lease expired. The reconciler hands every adapter the worker's stop
  (`AdapterContext.stopping`, set by `stop_reconciler`); a render's supervisor
  then stops the render process (`handed_over`, at once, and a worker
  already stopping starts none), and the attempt ends `handover`
  (`render_process.hand_over`: its working directory and stored segments
  kept, its stage "Handed to the next worker at 38%: its worker was asked to
  stop (SIGTERM) ..."). The adapter hands the execution back to the queue
  (`AdapterResult.defer(place="head")`, `adapter_state` `worker_stopped`, the
  stopping worker and its version in `payload.handed_over`): first in the
  renders' queue (`engine.head_of_queue`: before every pending one, due now),
  no attempt spent - a deploy never fails a render, even on its last attempt
  -, and every worker is woken for it (`notify_orchestration_wake`), never
  waiting for the next job queued. An execution task torn down while the
  worker stops, before its supervisor saw it, is handed back the same way
  (`_hand_back`, run to completion); a task cancelled for any other reason - a
  timeout - is not.
- **A deploy drains the worker first.** SAQ sets its stop event on
  SIGTERM / SIGINT and runs the worker's shutdown hook only after its grace
  (`WORKER_SHUTDOWN_GRACE_PERIOD`); the reconciler used to claim new work all
  through it, and kill it at the hook. From the stop event on, the reconciler
  drains (`reconciler_loop(draining=...)`): it claims no execution and drives
  no outbox event, its sweeps and the executions it runs go on, so a render
  that ends within the grace ends there; the shutdown hook hands back what
  still runs. A larger grace (and the orchestrator's kill deadline, Compose
  `stop_grace_period`, Kubernetes `terminationGracePeriodSeconds`) lets longer
  renders finish on a deploy. The signal is recorded (`worker._note_stop_signals`).
- **A deploy waits for the renders past half their frames.** The 22
  Oct 2026 render was handed over by two deploys, at 72% and 81%, each costing
  it 10-20 min. Before it stops its reconciler, the shutdown hook waits for
  the whole renders and scene ranges it draws past `DRAIN_PAST_PROGRESS` (50%,
  the progress each process reports to its supervisor, `_Attempt.progress`)
  to end (`render_process.finish_before_stop`), up to `drain_seconds()` from
  the stop (`orchestration_reconciler.stop_asked_at`):
  `VIDEO_STUDIO_RENDER_DRAIN_SECONDS` (20 min), never past the deployment's
  termination grace (`WORKER_TERMINATION_GRACE_SECONDS`, Kubernetes' 30 s
  when not given) less `DRAIN_MARGIN_SECONDS` (30) for the hand-over - so a
  deployment that does not declare a longer grace waits for nothing, and the
  orchestrator never kills a worker mid-wait. `docker-compose.yml` gives its
  workers `stop_grace_period: 1260s` and the same
  `WORKER_TERMINATION_GRACE_SECONDS`; the dev override 25 s (watchfiles'
  `--sigint-timeout`). A scene preview is never waited for. Meanwhile a render
  under 50% is handed over at once when a running worker claims renders
  (`hand_over_early`, from the render adapter's `periodic` while
  `AdapterContext.draining`; `_Attempt.handing`, its stage "Handed to the
  next worker at 30%: under 50% as its worker was asked to stop ..."), and
  keeps drawing when none does (a Compose recreate: the new container starts
  after the old one stopped). What still draws at the end is handed over as
  before.
- **A render keeps its slot through a hand-over.** The render handed
  over at 81% then waited "first in the queue" while two renders the
  replacement pod had started meanwhile held the lane. An adapter declares the
  `adapter_state` values its executions wait in with the slot they held
  (`OrchestrationAdapter.keeps_slot_through`: the render's `RESUMING_STATES`,
  handed back by a stopping worker or taken back from a worker gone; the
  preview lane none): its claims record when an execution first took a slot
  (`claim_due(keep_slot=True)`, `payload.slot_since`), kept through its
  hand-overs. A running worker keeps a free slot for each execution in flight
  on a worker whose presence says `draining`, one a running worker claiming
  the lane in turn, oldest slot first (`_slots_kept_elsewhere`: the lease
  holders' records read in one `MGET`, the workers present listed only while
  one of them drains; `engine.running_on`) - never fewer free than the executions already
  waiting for theirs, which come first in the queue (`_claimable_slots`) -,
  and lends none of it to the preview lane. A worker whose slots are all
  taken while an execution has waited `GIVE_BACK_AFTER_SECONDS` (15) in one of
  those states (`engine.waiting_for_slot`, a cycle or two for a worker with a
  free slot to take it first) takes it in hand for
  `GIVE_BACK_HOLD_SECONDS` (60, `engine.hand_to`: `claim_for`, so no other
  worker gives a slot back for it) and asks the execution it runs that took
  its slot last - after the waiting one's - to give it back
  (`AdapterContext.give_back`, the reconciler's `_slot_holders`). The render
  process stops at once (`gave_back`, `render_process.give_slot_back`: its
  working directory and stored segments kept, its stage "Gave its slot back
  at 12% to a render that held one before it ..."), and the adapter puts its
  execution back in its place, its attempt unspent
  (`AdapterResult.defer(place="keep", not_for=<this worker>)`, `adapter_state`
  `slot_given_back`: any other worker takes it at once, this one once the
  render it gave its slot to is out of its hand). That state keeps no slot:
  the render that gave its slot back waits for the next one to free and takes
  none back. `tests/test_video_studio_render_slot_postgres.py` stops a worker
  at 80% while the replacement worker's two slots hold renders queued after
  it, and resumes it there within 60 s of its hand-back, its slot's age kept,
  before the render queued behind it.
- **A worker that went is seen, and its render resumed at once.**
  Every reconciler says it is there (`services/worker_presence.py`, Redis,
  every 5 s, a record living `PRESENCE_TTL_SECONDS`, 20): its version, host
  and process, `running` / `draining` / `stopped` with its signal, and the
  container's memory and out-of-memory kills; its last word stays an hour.
  Each cycle reads the holders of the live render leases
  (`engine.leased_owners`) and takes back at once
  (`engine.reclaim_from_gone_owners`, `adapter_state` `worker_gone`, first in
  the queue, the attempt counted) the renders of a holder that is gone: one
  that said `stopped` (its hand-back did not land), one on this host whose
  process is no longer the one that wrote its word (a container restarted in
  place: found at its first cycle), one whose word ran out while its last word
  is kept. A worker of a release that never published is never taken for
  gone, and an unreachable Redis takes nobody. A worker killed mid-render is
  so resumed within `PRESENCE_TTL_SECONDS` + a cycle of its kill, never after
  its three-minute lease. Only adapters that declare it are taken back this
  way (`OrchestrationAdapter.resume_when_worker_gone`: the render, harmless to
  run twice); an expired lease of theirs goes first in the queue too.
- **Why it stopped, said.** The worker that resumes a render says
  what it resumed after (`orchestration_adapters.video_studio.resumed_after`,
  its stage "Resuming after a deploy (1.4.1 -> 1.4.2) (attempt 2 of 3): ..."):
  a deploy when the stopping worker's version was another, a restart when it
  was the same, or how a worker gone went (`worker_presence.stop_reason`: the
  stop it was asked for and its signal, the orchestrator's kill deadline cut
  short a drain, or - no stop asked - the signal no process can catch
  (SIGKILL, the out-of-memory killer's) or a crash, with the memory of its
  last word and the container's out-of-memory count where this host reads
  it); the same words end its execution's `last_error`, and its failure if it
  has no attempt left. A render process killed while its worker lives is said
  by its supervisor: its signal, "killed by the kernel's out-of-memory killer"
  when the container's `memory.events` `oom_kill` count rose meanwhile, and
  the container's memory and limit at the last sample
  (`render_process.RenderExit`). A worker that dies under a render (the
  process dies with it, `PR_SET_PDEATHSIG`) whose presence is unknown is still
  recovered by its lease, and the dead letter says how far the render had gone
  and when its worker was last heard from - `Lease expired at 63% (Drawing
  frames (82%)), last heartbeat 74 s ago, with no attempt left.`
  (`engine.lease_expiry`).
- **Never left `running`.** A render a stopped worker
  left waits first in the queue; `get_video_jobs` says so (`jobs.Resume`,
  `lease_clause`: "handed back by its stopping worker 40 s ago (...), first in
  the queue for the next worker - it fails if no worker resumes it in 560 s"),
  and says the lease of a running job - its holder, renewed N s ago, expiring
  in M s - with its last progress. It waits as long as the deployment's
  rolling restart can take (`jobs.resume_wait`): the worker pod's termination
  grace plus a new pod's start (`WORKER_TERMINATION_GRACE_SECONDS` +
  `WORKER_STARTUP_SECONDS`, 30 s + 570 s when not given), never less than
  `jobs.MIN_RESUME_WAIT` (3 min), counted from when it last moved - its
  hand-over, or the progress its render recorded since. The stopping worker
  stamps its own figure in the hand-back (`handed_over.resume_wait_seconds`,
  `jobs.waits_for`): the platform's tick that fails it reads the worker's
  deployment, not its own. It also waits for as long as a job holds a slot of
  its lane anywhere (`jobs.holds_slot_of`): the render lane is held by
  its renders and by the scene previews in a slot it lent them - the
  engine's claim records whose slot an execution runs in,
  `orchestration_engine.LENT_SLOT_PAYLOAD_KEY` -, never by a preview on the
  preview lane's own slots, which `get_video_jobs` counts apart
  (`previews_running`: "it waits while 1 job holds a slot of its lane (1
  scene preview on its own lane holds none of it)"; `fails_in_seconds: null`,
  `lane_running`). Each check that finds its lane held records it on the
  execution (`jobs.LANE_HELD_KEY`, its `updated_at` kept), and its wait counts
  from the last of those or its last move: the wait it states once its lane
  frees ("once none does, it fails if no worker resumes it within 10 min") is
  the wait it gets, never one already spent while it waited. Past it, it is
  failed (`jobs.fail_unresumed_renders`, the platform's scheduler tick, which
  runs with no worker at all) saying where it stood, why it went back and the
  workers alive then with why none took it - asked to stop, drawing what, or
  running idle (`jobs.workers_seen`, from `worker_presence.present` and the
  leases) -, its execution cancelled and marked `jobs.UNRESUMED_KEY`.
- **A resumed render takes back the slot its lane lent.** A render a
  stopped worker left that has waited due for
  `orchestration_reconciler._TAKE_BACK_AFTER_SECONDS` (15 s: a worker with a
  free render slot claims it first) takes back a render slot its lane lent to
  a scene preview on a worker with no other free slot
  (`OrchestrationAdapter.takes_back_lent_slots_for`, `_take_back_lent_slots`,
  the newest lent first): the preview's `AdapterContext.give_back` is set, its
  process stops between frames as at a stop (`render_process.hand_over`,
  `given_back`), keeps its finished frames and goes back first in the preview
  lane (`jobs.SLOT_GIVEN_BACK_STATE`, `preview_lane.first_in_line`), its
  attempt unspent; the freed slot claims the render at the next cycle.
- **A relaunch brings it back as the same job.** A start of the same
  revision and quality within `jobs.RELAUNCH_KEEP` (a day) of that failure
  passes the gate, then revives the row (`projects.render_left_unresumed`,
  `jobs.enqueue_again`): its execution engaged afresh (`reset_execution`, same
  id and dedup key) at the head of its lane, so it resumes from the segments
  kept under its id (`render_segments`). The hourly sweeps keep those segments
  and its scene ranges meanwhile (`jobs.relaunchable`). `render_video` and the
  start endpoint say so (`StartedRender.relaunched`).
- **A lost lease on a render still moving resumes it.** The engine
  dead-letters an execution whose lease expired with no attempt left; the
  render adapter's `on_dead_letter` first asks
  `render_process.resume_after_lost_lease`. A render whose `progress_at` came
  within the stall window before its lease was taken back was drawing: its
  execution is queued again (`engine.requeue_execution`: its attempts back,
  its deadline kept, `payload.lease_resumes` counting, at most
  `MAX_LEASE_RESUMES`, 3), its row `pending` with "Resuming after a lost lease
  at 63% ...", and the next attempt resumes from its finished segments. "No
  attempt left" is said only for a render that had stopped moving. A render
  cancelled meanwhile keeps its cancel and its execution is cancelled again.
  Each segment holds `compositor.SEGMENT_SECONDS` of frames and never more than
  a tenth of the render's (`compositor.segment_frames`, `CHECKPOINT_SHARE`; a
  second at least, a segment opens its own encoder): a resumed attempt draws
  again one segment per page at most.
- **Lease.** The worker keeps the execution's lease from its own
  event loop, which draws nothing and reads `/proc` in a thread (a busy
  Chromium's `smaps_rollup` waits on its memory map). The render process
  renews it too, from every frame it draws, follows or watches - the
  compositor's frames, a figure scene's frames (`figures.draw`), the footage
  frames under a tracked screen insert (`insert_tracking.track_all`) and the
  frozen-picture check's (`frame_freeze.check_frames`), all handed
  `run_render`'s `on_frame` (render `14d6bcbd` lost its lease drawing
  a 588-frame figure with `motion: drift`, the one loop that renewed nothing,
  while the worker's own heartbeat was late). An encoder these stages wait on
  renews it as well, every `encoder.WAIT_REPORT_SECONDS`, through the same
  callback (`render.frame_reporter`): the figure's encode once its frames are
  drawn (`frames_to_mezzanine`) and a segment's encoder flushing its
  lookahead (`FramePipeEncoder.finish`) draw no frame, and at 2160p on a
  4-vCPU worker the figure's encode alone went 84 s without renewing it.
  The worker hands it the
  execution, the lease owner and the lease's length (`XTM_RENDER_EXECUTION_ID` /
  `XTM_RENDER_LEASE_OWNER` / `XTM_RENDER_LEASE_SECONDS`), and
  `lease.FrameLease` renews it from a **thread of its own**, on its own event
  loop and its own database connection (`NullPool`), every third of the lease
  (`lease.renewal_interval`), a renewal that hangs given up after one interval
  so the next can start. The frames and the reports only say the render moves
  (`frame_drawn`, `moved`, called from the render process's `report`): the
  thread renews while the render moved within the stall window, and leaves a
  render that stood still to the stall detector. Two High renders of one
  19-scene video lost their lease at about 60 % while drawing frames ("last
  heartbeat 62 s ago, with no attempt left"): the renewals the frames triggered
  ran on the render's event loop and the worker's heartbeat on the worker's,
  and both fell silent for over a minute. A thread is neither: a slow frame, a
  loop busy with a 4K screenshot or a worker short of CPU no longer silence
  it. Renewing every 600 frames was a time only at one frame
  cost: both attempts of a 23-scene render lost their lease at 28 %.
  A render's execution holds a lease of its own, `jobs.RENDER_LEASE_SECONDS`
  (180 s, three times the engine's): the adapter declares it
  (`OrchestrationAdapter.lease_seconds`), the reconciler claims and
  heartbeats at that length (`_heartbeat`, every third of it, a renewal that
  hangs given up after an interval) and so does the render process; the job
  state reads a renewal as late against the lease its holder took
  (`LeaseHeartbeat.lease_seconds`, the gap between the expiry and the last
  write). The pre-flight no longer bounds a frame by the lease: the lease
  outlives any frame, the stall window still bounds one. Each renewal is
  logged at info level with the time since the previous one, the frames drawn
  and the frame rate; a lease lost at frame N is logged with the frame rate of
  its last
  window. So a slow render that still moves is never taken for a dead one, and the stall
  detector decides when it stopped moving. A lease the engine took back
  (reclaimed, cancelled, dead-lettered) stops the render at the next frame
  (`lease_lost`): it leaves its row to the next attempt or the dead letter
  instead of drawing beside a second render of the same row. The lost lease
  and the render's cancel, raised from its callbacks, pass through the loops
  that wrap their own errors - a figure's `FigureError`, the frame check's
  "could not run" - and end the render as what they are (a render
  cancelled while it drew a figure used to fail instead).
- **Flat memory.** Each frame goes from the screenshot to its page's encoder
  before the next is drawn, a chunk at a time ("Pages from a measured
  peak"). A source's decoder opens when its scene first asks
  for a frame and closes from the first frame after the scene ended
  (`compositor.release_schedule`), its stills dropped with it. A page keeps at
  most `compositor.decoder_budget` decoders open (the most video sources on
  screen together, two for a crossfade), evicting the least recently used. The
  render page removes a scene's layer once it ended (the editor's preview, which
  seeks back, keeps them), and its renderer purges its caches then.
  ffmpeg and x264 run on `resources.cpu_budget()` threads, the container's
  CPU quota rather than the node's core count - a render page's encoder on
  its page's share of them (`render_preflight.encoder_threads`) - and
  a decoder on one thread. Measured on a synthetic 116.7 s timeline (10 clips, 6
  statements over video backdrops, crossfades, captions) at 1080p30 with two
  pages in a 2 CPU / 8 GiB container: the container's memory went from 4.2 GiB
  at 10% of the frames to 5.75 GiB at 90% before, and stays between 2.1 and
  2.4 GiB now. `tests/test_video_studio_render_memory.py` holds a 180 s
  timeline under a fixed ceiling in the Chromium CI job.
- **Statement text, one layer.** A statement's card is one composited
  layer (`.stmt { will-change: transform }`); its lines and words are not
  promoted one by one, since a render composites in software and pays for
  every layer on every frame over a moving backdrop. Measured at 1080p60
  `high` in a 2 CPU container: a statement frame over a video backdrop went
  from 62.4 to 58.8 ms (draw, capture, encode), over the brand backdrop from
  60.1 to 58.2 ms; the rest of a frame is mostly Chromium's capture itself
  (34 ms on an unchanged page). Capturing the settled text once and drawing
  it as a picture was measured too and saved less (about 2 ms): the push-in
  still resamples it every frame.
- **Logs.** Each stage (`media`, `frames` - drawn and encoded together -,
  `audio`, `mux`, `store`) logs its start and end at info level with its
  duration and the memory of the render's processes, then the frames drawn and
  the most decoders a page held, and how many times the frames renewed the
  lease; the supervisor logs the ceiling it applies and how the process ended.
  A reconciler heartbeat that fails is logged at warning level with how many
  failed in a row, and every lease the engine takes back with when its holder
  was last heard from.

### Scene ranges across workers

A whole render that one worker cannot draw within `RENDER_DEADLINE_SECONDS`
(4 h) is drawn in scene ranges, each on a worker of its own
(`render_ranges.py`). The cap applies per range.

- **When.** `render.ranges_here` makes the pre-flight's estimate again on the
  worker that runs the render, with its frames now known and that worker's CPUs
  and memory ceiling (`render_preflight.estimate(...).ranges`). A scene preview
  is never split, and neither is a render whose drawing inputs another worker
  cannot read (`BundleError`): it is drawn whole, as before.
- **Where the cuts fall.** `plan_ranges` makes contiguous `[first, end)` frame
  ranges of about the same frames, each cut on a scene's first frame. A range
  opens pages of its own, and a page draws a frame as a pure function of time
  only to within its raster history, so a cut never falls inside a
  scene.
- **What a range draws from: the bundle.** The render publishes once
  (`render_ranges.publish`) its planned document, quality, size and release,
  and every file the compositor reads, with each screen insert as the render
  prepared it: its track, key, footage and first frame. A file downloaded from
  a file id is named by it: `render._ORIGINS`, filled by every download of
  `run_render`. What the render made itself (a slide, a figure's video, an SVG
  drawn to a picture) is stored under
  `video-studio/render-ranges/<render>/files/` by its content's digest. The
  bundle's digest covers all of it, so a render's next attempt publishes the
  same bundle and finds the same ranges. A range reads the bundle (`read`;
  another release's bundle is refused, since its frames could differ at the
  seams), downloads those files once (`fetch`) and draws. It never gathers the
  media, speaks a narration, draws a figure or follows a track again.
- **Who draws a range.** `VideoRenderRange` (migration `v6163range01`, no
  foreign key) holds one row per range: its frames, its scenes, its status,
  `drawn_by`, `frames_done`, `progress_at` and what it measured. Whoever moves
  it from `pending` to `running` draws it (`claim`, one conditional `UPDATE`).
  That is a range execution on another worker, or the render itself; the
  execution that had it takes it again on a retry. Its frames drawn and its
  `progress_at` are its progress and heartbeat (`progress`), and a write by a
  drawer the range no longer has (`RangeTakenError`) stops that drawer.
- **On another worker.** Ranges 1 and up get an execution of the render
  adapter (payload `render_id`, `range`, `digest`), so a worker draws ranges
  within its render budget (`VIDEO_STUDIO_RENDER_CONCURRENCY`). The adapter
  runs it in a process of its own (`render_process.run_range_isolated`,
  `--range <part> <digest>`), under the same memory ceiling, stall resume,
  lease renewal, handover and three attempts as a render, with a deadline of
  its own. Its segments are kept beside the render in the store
  (`render_segments`, under the range's own digest). Whatever ends it writes
  the range's row only, never the render's: a range that cannot be drawn there
  (its memory, a stall past its resumes, a crash on its last attempt, a dead
  letter) is `failed`.
- **The render joins them.** `draw_all` draws range 0 itself, then every range
  that is still `pending`, `failed`, or `running` while its execution ended or
  went back to the queue (handed back by its stopping worker - first in
  the queue, as the render - or taken back from a worker gone; waiting for a
  worker to claim it hung a render holding its lane's only slot).
  It waits for the others, reporting where each stands (`describe_ranges`, in
  the render's stage and `get_video_jobs`). While it waits it sends a
  heartbeat for the stall window, and it pushes its own deadline out by its
  quality's hours (`orchestration_engine.extend_deadline`: owner-checked,
  never in; the reconciler's timeout of the running task follows the row at
  its next heartbeat, `orchestration_reconciler._follow_deadline`). Once a
  range is drawn, `compositor.render_frames` on the same range restores its
  segments from the store and draws only what is missing, so a range whose
  worker died is finished here. A single worker therefore renders a split
  video one range after another, each within its cap. `encoder.concat` joins
  the ranges by stream copy, and the sound is mixed and muxed once, as for a
  whole render. The render records what this worker drew in how long
  (`measured`, `ranges`), never the wait.
- **Cancel and clean up.** A range process reads its render's cancel every
  30 frames, as a render does, and stops. A range execution that starts once
  its render ended draws nothing. The attempt that ends a render cancels its
  ranges' executions and deletes the ranges and their bundle
  (`render_ranges.forget`, best effort). The hourly Video Studio sweep deletes
  those of renders that ended or are gone (`render_ranges.sweep`).

Pinned by `test_video_studio_render_ranges.py`, on a 130 s, 30-scene fixture
(statements, recordings, AI clips, figures). Its ultra render fits under the
cap on the documented worker. A worker too small to draw it whole draws it in
ranges that each fit. A refusal carries every number and the quality that
fits. The ranges are drawn, waited for, taken over and restored without
being drawn twice. The bundle round-trips. A second render at another quality
speaks no narration and opens no figure page. In Chromium (the `chromium`
marker), two workers draw two scene ranges: the second is drawn from the
bundle, the render restores it from the store without drawing a frame, and
the join plays every frame of a whole render, at over 40 dB PSNR against it.
Each range cuts its own segments, so its key frames fall elsewhere: the
frames match by picture, not byte for byte.

### Render retention

`render_retention.py` keeps a video's renders bounded under a platform
setting an administrator edits (`PlatformSettings.video_render_retention`,
migration `v6539rret01`, `GET` / `PUT /video-studio/render-retention`, the
Settings section `RenderRetentionSection`, `video_studio_capabilities`'
`render_retention`): `Policy`, in days after a render ended, 0 keeping a kind
for good - `failed_cancelled_days` (1) for a failed or cancelled render,
preview or whole; `previews_days` (7) for a ready scene preview;
`superseded_ready_days` (30) for a ready whole render that is no longer the
newest ready render of its quality. The `VIDEO_STUDIO_RENDER_KEEP_LAST` /
`VIDEO_STUDIO_RENDER_RETENTION_DAYS` variables and their count rule are gone:
a superseded 4K final and the 1080p cut are judged per quality, not ranked
together.

- **What keeps a render, one rule (`kept_by`).** For a video's `Standing`
  (its current render, whether it has a public link, its newest ready whole
  render per quality, the policy, now): `public_link` for the current render
  of a video with a public link (the row, whatever the admin switch: turned
  back on, the link plays it), else `latest_ready` or, picked by hand,
  `author`; `latest_ready` for the newest ready render of its quality;
  `author` for a render kept by hand (`VideoRender.keep`); `retention` for one
  newer than its days (`expires_at`); `None` for a render in progress (never
  judged) or past its days. The sweep, `get_video`, the render list
  (`serialize_render(project=, standing=)`: `is_current`, `kept_by`,
  `expires_at`) and the cleanup read it, so they never disagree.
- **The nightly sweep.** `sweep_if_due`, asked by the hourly cleanup loop
  (`autonomous._cleanup_video_renders`), runs during `NIGHT_HOURS` (00:00 to
  06:00 UTC) until a sweep looks at every video due, then marks the night
  swept in Redis (20 h) so the other hours and replicas skip it (Redis down:
  it runs, finding little the second time). `sweep` picks the videos with a
  render past its days in SQL (`_due_videos`: the newest ready render per
  quality ranked out with `row_number() over (partition by project_id,
  quality, ...)`, the current and kept renders left out), at most
  `SWEEP_VIDEOS` per call, then per video: the video's row locked `FOR UPDATE
  SKIP LOCKED` (a video a cleanup or another replica holds is skipped, never
  waited on), its renders judged by `kept_by`, the due ones locked `SKIP
  LOCKED` and judged again as read under the lock, deleted, committed. It
  logs what it freed per video (renders by kind, bytes) and commits even
  when it wrote nothing: a rollback would expire the cleanup loop's session.
- **The one deletion (`delete` + `purge`).** In the caller's transaction:
  the rows and their files' rows (`render_files`: MP4, poster, captions,
  frames, findings' frames and inserts' crops, through `assets.forget_files`,
  which keeps a file another video needs); the bytes freed are those files'
  (`measure` for a dry run, the same predicate); a video that lost a ready
  render is indexed again (`retention.mark_for_reindex`: what that render drew
  is no longer needed). Once committed, `purge` removes the objects
  (`file_cleanup.purge_storage_objects`) and the segments kept beside each
  render for a resume (`render_segments.forget`). The sweep, a replaced
  preview, the cleanup and `DELETE /renders/{id}` all go through it.
- **Lock order.** The render job, `PATCH` (current) and `DELETE
  /renders/{id}` lock the render, then the video (`lock_render`); the sweep
  and the cleanup lock the video, then its renders with `SKIP LOCKED`, so
  nothing waits on a lock the other side holds.

`PATCH /renders/{id}` keeps a render (`keep`) or makes it current
(`current: true`), its managers; `DELETE /renders/{id}` deletes one (its
managers or its requester), the newest other ready render becoming current
(`next_current`); the project payload carries the policy
(`render_retention`).

### Cleaning up renders

`render_cleanup.py` is `delete_video_renders` and the Studio's "Clean up
renders" (`POST /projects/{id}/renders/cleanup`, `RenderCleanupDialog`), one
`Ask` (`render_ids` or `select`: `all_but_latest_ready` - the default, the
newest ready render of each quality kept, of `keep_quality` alone when given
- `failed_and_cancelled`, `previews`, `all` only with `confirm`; `before`;
`dry_run`; `confirm`) and one answer:

- **`plan`, pure.** Per render in scope, newest first: `refuse` a render
  queued or running (`in_progress`, cancel it first), someone else's render
  when the caller may not manage the video (`not_yours`, the REST delete's
  rule), the current render of a video with a public link without `confirm`
  (`public_link`); `keep` a cancelled render whose replacement at its quality
  is still drawing (`handing_over`: it takes its segments,
  `render_segments.handed_over`), the protected newest ready renders
  (`latest_ready`), a render kept by hand under a selector (`kept_by_hand`;
  named, or `all` with `confirm`, deletes it); `delete` the rest, recording
  what `confirm` let go. A `keep_quality` the video has no ready render at is
  refused before anything goes. `settle` says what the video plays after: its
  current render, or the newest remaining ready render (`newest_playable`)
  when the current one goes.
- **`run`.** Under the video's row lock (none for a dry run): every render
  read, the plan made, the planned rows locked `SKIP LOCKED` (one someone is
  changing stays, `busy`), `render_retention.delete`, `latest_render_id`
  moved. The caller audits (`audit_details`: the render ids, bytes freed, the
  selector, `confirmed` / `confirmed_for` as `render_video force` records
  `forced`, the new current render), commits, then `purge`s.
- **The answer** (`Outcome.as_dict`, the tool's JSON block and the REST
  body): per render `{render_id, quality, revision, status, scene_id?,
  size_bytes, action: delete|keep|refuse, reason?, why?}`, `freed` (or
  `would_free` for a dry run: the same list), the unknown ids, and `serves`
  (the current render, whether it changed, the public link, the newest ready
  render per quality). The tool's text says each refusal's remedy
  (`cancel_video_job`, `confirm: true` and what the link plays then) and what
  the video plays now.

`get_video` lists the newest 8 renders with the bytes their files hold
(`size_bytes`, `render_retention.measure`) and `kept_by`, and a storage line
totalling every render by kind.

## Sharing, public link and downloads

- **Grants.** A video is shared exactly like a deck: `PATCH /projects/{id}`
  takes the `SharingChange` fields and writes them through
  `presentation_sharing.apply_sharing` (the one sharing write of decks and
  videos: per-user grant rules, admin-only company publication), after
  `lock_for_sharing`. Tiers: `read_only` watches, downloads and duplicates;
  `config` also edits, renders and shares. The payload carries the grants with
  their tiers, `access_restricted`, the reader's `permission`
  (`projects.video_permission`), `owner_name` and `has_public_link`. Its audit
  row names who gained, lost or changed access
  (`share_principals.grants_audit` over a `grant_snapshot` taken before the
  write), the keys `share_video` logs.
- **Public link.** `public_links.py` is the deck's public link for a video,
  sharing its token idiom (`mint_token`, `well_formed_token`): one
  `VideoPublicLink` per project (token hash, Fernet-encrypted token,
  `allow_download`), managed by `GET` / `POST` / `PATCH` / `DELETE
  /projects/{id}/public-link` and the `set_video_public_link` tool, behind the
  `video_public_links_enabled` security option (`access.video_public_links`),
  read on every public request. `api/v1/video_public.py` serves, without an
  account, `GET /public/videos/{token}` (`PublicVideoResponse`: the title,
  the current render's size, length, captions state and chapters, whether
  download is offered - never the spec, the owner or the grants) and `GET
  /public/videos/{token}/media/{video|poster|captions}` (the CURRENT render's
  files only, by byte ranges; `?download=true` an attachment, only when the
  link allows it). One 404 for every refusal, `no-store`, `no-referrer`,
  per-address budgets - of requests, and of BYTES for the media
  (`PUBLIC_MEDIA_BYTES_PER_WINDOW`, 2 GiB a minute: each answer is charged what
  it carries, `media.answer_bytes`, through `enforce_rate_limit(cost=)`, and
  the one that would cross it is a 429). A multi-range request is answered
  whole (200), a suffix range (`bytes=-N`) as its last N bytes. The download
  option is a convenience, not a protection: a plain `GET` of the MP4 plays
  it, and whoever plays it can save it - the Share dialog, the tool and the
  user docs say so.
- **Downloads.** A download token (`POST /media-token` with `download: true`)
  carries a signed file name (`media.download_filename`: "<title> -
  1080p60.mp4"), and `GET /media/{token}` answers it as an attachment.
  `media.serve_file` is the one byte-range answer of the signed-in and the
  public media routes.

## Layout and the quality check

Two deterministic answers, the deck's (`presentation_layout`), no prompt text:

- **The layout engine** (`compositor_page.py`, the render and the editor's
  preview alike). `layCaptions` measures every cue once (shrunk from 34 to
  26 px until it holds on two lines, kept clear of a watermark at its edge)
  and `reserveOf(scene)` is the band the tallest cue shown during a scene
  takes at the captions' edge - burned in or not, since a player draws the
  WebVTT track in that band. `frameText` sets a card's or a statement's box
  inside what the band and its bottom overlays (`placeBands`: lower thirds
  and big captions above the subtitles, stacked when shown together) leave,
  and scales the card's WHOLE text by `fit` to its height and width.
  `placeSpots` puts a badge, a picture (an image overlay) or an unanchored callout at the spot asked, else
  the first free of the card's glyphs (`inkRects`, text nodes only), the band
  and the watermark, else the one covering least; anchored callouts shown
  together take the element's other side or stack. A recording's window and a
  framed picture sit above the band. A slide is a page whose text runs to its
  edges (`framed` in its payload): of the frame's own shape it plays
  full bleed until subtitles are said over it, then `buildStill` scales it to
  stand above the band instead of playing them over its footer (a figure is
  drawn clear of the band in its own frame, `figures.caption_safe_area`). The watermark leaves title / outro cards
  and statements showing the logo (`watermarkAlpha`) and fades back in with
  the content once its transition in is over (never while the card
  it gave way to is still in view, however long the transition). A statement
  shows no logo while the watermark is on (`spec.card_draws_logo`, applied
  by the planner).
- **The check** (`quality.py`). After `create_video` / `edit_video` save,
  `check_video_quality` plans the saved revision as the editor previews it
  (`build_preview`, no media: placeholders), plays it in the compositor's
  headless Chromium (`measure_document`, `RENDER_CHROMIUM_ARGS`) and runs
  `window.__xtm.audit()`: each scene settled (all text in), each overlay and
  each cue just in and late. Observations: `overlap` (glyph or pill rects of
  two different groups - the scene's flow, each overlay (a picture's box too,
  counted at full strength whatever its opacity), the subtitles, the
  watermark), `outside` (past or within 1.5% of the frame's edge),
  `rushed` (a block still coming in when the scene ends), `brand_twice`
  (two brand marks drawn on one frame - every element carrying
  `data-brand`: the watermark, which stands with no picture where none is
  served, a card's logo, an image overlay of a template mark, the planner's
  `brand` - each counted from half strength, less what a scene coming in
  covers of it, `brandMarks` / `coverage`, on every frame the audit plays and
  over the scene's opening and the end of its transition in;
  `window.__xtm.marks(frame)` reads one frame), the scene's `fit`
  and `reveal_end`, each cue's lines. `findings_from_audit` grades them with
  what the document alone tells (`reading` time, `caption_speed`, a dim
  `backdrop`, an `overlay_short`) into findings naming the scene (its index
  in the authored spec and its id) and the change - a `brand_twice` is
  "brand mark drawn twice", `dense` in the content group, with its frame's
  time and the switch that drops each mark (`theme.watermark: none`,
  `show_logo: false`, the image overlay); `format_quality_report`
  is the `Quality check` section of the tool result (and `get_video`), and
  `GET /projects/{id}/quality` serves the editor's strip, whose `Fix with
  my assistant` primes the rail's composer with the findings in
  `format_quality_report`'s words and bound (`QualityStrip`, pinned by
  `test_video_studio_quality.py`), like the deck's layout panel. Advisory and
  bounded (40 s): a missing browser or an error is a one-line note, never a
  failed save; a report is cached per (video, user, revision) for 15 min.
  Switch: `VIDEO_QUALITY_CHECK_ENABLED` (`video_quality_check_enabled`).
- **The pictures and the sound.** `quality.check_content` reads,
  fresh on every check (a shot finishing does not change the revision), the
  effective spec, its estimated plan (`projects.estimate`) and what the
  database knows of its media (`content_facts`: each AI shot's state, size,
  kept frames and the camera its prompt asks for; each raster image scene's
  size; the music's playable length, `music.playable_seconds`; whether a
  speech model can speak), and `content_findings` decides in code, scene by
  scene, with the fix: `shot_state` (a shot failed - a render refuses it, an
  insert's is left out - or still being made; an image whose animation failed
  or is being made, advisory: it plays its still), `camera_moved`
  (`quality.camera_moved`: a shot whose last frame changed more than its own
  `max_camera_motion`, else one whose prompt asks for a locked camera - read
  by the content check's prompt reading, `content_drift.CAMERAS`, never a
  phrase list - and whose `motion_ssim` is under `LOCKED_SHOT_MIN_SSIM`:
  "camera moved on a locked-off shot"; a screen replacement without a
  tolerance never, its insert follows the camera; never on a clip
  scene whose author set `motion: intended` - a dolly, a pan asked for -,
  and advisory once every scene showing the shot passed a preview as it is
  now, naming that preview and its revision), `image_resolution` (an image drawn past `UPSCALE_SOFT`
  its size on the frame of a `DEFAULT_QUALITY` render, with the size it
  needs), `insert_outside` (a screen insert's corner outside what the frame
  shows of the footage, `footage_window`: advisory - the surface
  keeps its true corners outside the frame and the insert shows through the
  visible part, never squeezed to the frame's edge, which keystones it - and
  dense when under `INSERT_MIN_VISIBLE_SHARE` of the quad is in the frame,
  `quad_visible_share`: a surface the frame does not show), `music_short` (a track ending
  before the video with `loop` off, placed on the scene where it ends),
  `narration_voice` (a narration no voice speaks: voice-over off - advisory
  with captions, dense without - or no speech model), `held_frame`
  (advisory: an AI shot or a recording whose planned time map still holds its
  last frame `HELD_FRAME_SECONDS` (1.5 s) or more beyond its `hold_end` once
  filled, `quality.held_seconds`; said only when its length is real - a ready
  shot, a recorded take in `ready_takes`, never an upload the estimate does not
  probe - with the fix: a longer shot, one continued from its last frame
  (`reference_clip_id`), more of the flow recorded, or a shorter narration),
  `brand_repeated` (dense: a title, closing or statement card writing
  the brand name its logo or logo overlay already spells,
  `templates.brand_repeats`, read with the template `effective_spec` loaded -
  `ContentFacts.template` - see "The brand on a card"), `voice_fallback`
  (dense: a narration whose check record placed the closest voice of
  its speech model, `ContentFacts.voice_changes`, its words
  `voice_check.VoiceChange.sentence`), `frozen_hold` (dense - the
  render gate refuses on it: a clip with a playing AI shot or a recorded take
  whose `extend_mode` is `hold` and whose planned last frame stays on screen
  past `frame_freeze.FROZEN_SECONDS` (0.5 s, the render's own threshold),
  less its `hold_end`, within the span its scene owns alone - a frame of its
  transition out is a blend, `quality.frozen_hold_seconds` -; the plan reads
  each narration at its spoken length where it was spoken
  (`preview.spoken_lengths`, `ContentFacts.spoken`), else the reading pace,
  said; it names the footage's length, its trim and speed, the narration's,
  and suggests `extend_mode` zoom or loop - loop not for a recording - a
  longer clip or a shorter narration: a 4K render of 21 Oct 2026 warned
  "frozen 0.7 s" where its save had said nothing). `frozen_hold`
  and `held_frame` also say the `speed` that closes the shortfall, found
  with the planner's own arithmetic (`timeline.speed_to_fill` over
  `filled_length` for a clip, `recording_seconds` - `build_time_map` - for a
  recording, `ContentFacts.takes`). A recording scene with
  `fit: auto` (its default) is never handed that number: the save applies it
  (`projects._fit_recordings`, `recording_fit.fits` on the plan at the
  spoken lengths, `recording_fit.apply`), never below `SLOWEST_FIT_SPEED`
  (0.5), and notes "speed set to 0.69 automatically"; its `held_frame` then
  fires only for a recording still short at 0.5 and names the seconds
  missing, no speed; `fit: off` keeps the author's speed and the advice. In a
  dynamic video the fit also works the other way: a recording running
  past its narration's end and tail by more than `recording_fit.LONGER_SLACK`
  (0.3 s) - the narration spans of the plan, `fits(narration=...)` - is sped up
  to end with it (`recording_fit._faster`), never above
  `pace.FASTEST_FIT_SPEED` (1.25), said the same way ("the fastest a fit
  plays in a dynamic video" when it still runs past). The `pace` advisory
  (`pace.report` on the plan at the spoken lengths, narration spans in
  `ContentFacts.narration`) flags a scene over `LONGEST_SCENE_SECONDS` (20 s)
  or a recording whose screen stays still - the cursor still
  (`pace.cursor_moves` on its planned samples), the page unchanged
  (`pace.page_changes`: around each step that acts, never a wait, a pause or
  a marker) - for more than `STILL_SECONDS` (3 s) at once while its narration
  speaks; the report's `notes` add its summary (words a minute, the average
  and longest scene, each recording's still seconds), never a finding. And
  `take_capture_rate` (dense) is a recording that animated under 20
  fps on a stretch a scene shows (`capture_rate`, above). `stepping_motion`
  is a recording whose motion no pacer draws again had more
  than half of its frames synthesised from keyframes on a stretch a scene
  shows, naming the step (`capture_rate.stepping`, above), on every scene
  showing it and graded as that scene draws it (dense, or advisory for
  a brief transition the audience reads as motion; not said where a slowed
  scene blends it); the stretches it covers are not said by
  `take_capture_rate` too. `take_loading_hold`
  (advisory) is a recording whose hold step showed its page still
  loading (`page_settle.loading_holds`, above), once per take, on the first
  scene showing it - a recording scene only within what it keeps of the take
  (`trim_start` to `timeline.kept_end`), a screen insert wherever it plays it.
  `demo_narration`, `demo_camera` and `demo_speed` (advisory) hold a
  recording scene to the grammar of a product manager demoing live
  (`demo_grammar.advise`, see "A demo told as a product manager tells it"),
  the labels its frames show in `ContentFacts.frame_text`.
  `doubled_ui` (dense, `QUALITY_REPORT_VERSION` 28) is a recording
  scene crossfading into the next scene of the same recording while the two
  framings of its page differ - a camera still moving under the dissolve, or
  the two scenes framed differently - so the title bar, tabs and toolbar show
  twice, offset: `doubled_ui.doubled_under_transition` draws a synthetic page
  with one sharp title bar through both planned cameras (one geometric
  transform per frame, as `compositor_page` applies it), blends them as the
  crossfade does at 30 fps and reads each frame for an edge split in two
  (`doubled_ui.ghost_edges`: two edges of one sign, the rows between them a
  blend of both sides); a clean geometric zoom keeps the edge whole. The
  planner keeps an automatic camera still and wide before the transition out
  of its scene (`timeline.camera_settled_before`, never earlier than half the
  scene; `screen_insert.DRAWING_VERSION` 15), so the finding names hand-written
  keyframes or framings that differ.
  And `insert_loop_seam` (dense) is
  a screen insert of a looping clip (`extend_mode: loop`) running past the
  loop's seam (`extend.from`: where the clip ends and its last second starts
  playing again under the insert), with the duration that ends it there or
  zoom. `insert_bezel` and `insert_flatness` (dense) read what the
  save measured under each static insert (`ContentFacts.bezels`,
  `insert_bezels.findings`; `insert_flatness` for a confirmed bow
  only, a surface seen at an angle never; a report kept by another release
  is never read back, `quality.QUALITY_REPORT_VERSION` - served as the
  report's `version` and the project's `quality_version`, which the studio's
  `useVideoQuality` keys a revision's report by, so an open editor reads it
  again when a release moves it). `insert_legibility`
  is a screen insert surface - a wall's pane included -
  showing a recording or an uploaded file (`insert_legibility.READ_KINDS`)
  whose smallest text is drawn under `MIN_TEXT_PX` (10, advisory) or
  `ILLEGIBLE_TEXT_PX` (6, dense: the render waits for it) of the 1080p frame
  of the video's shape (`ContentFacts.legibility`): `insert_legibility.measure` reads one frame of
  each source a second past its trim (`text_size`: ink told from paper tile
  by tile, glyph components run into text lines, each line's cap height over
  `CAP_EM`, the smallest tenth of the glyphs; kept in `render_cache` by file
  and second), the source's size off `render_checks.insert_geometry` (the
  soft-source gate's reading), the quad on that frame through
  `insert_checks.quad_on_frame` and the fit through `screen_insert.density`,
  the text at the peak of the insert's own push (`zoom.scale`). Both
  offer only what draws the text `TARGET_TEXT_PX` (12) and keeps the page's
  layout (`Legibility.fix`): the quad width that does it and its
  share of the frame (fewer panes on a wall), when the frame has it; a push
  on the insert (`zoom: {start, end, scale}`, the scale rounded up to 0.1,
  within `spec.MAX_INSERT_ZOOM`); and a recording
  laid out narrower (`Legibility._relayout`) - on a quad under
  `insert_legibility.NARROW_SHARE` (40%) of the frame's width the browser
  zoom to record it at (`TakeScript.browser_zoom`, to the next 10%
  from the zoom it has; past `spec.MAX_BROWSER_ZOOM`, a larger surface too:
  its window and frame keep their size), on a wider one the window width
  (its pixel ratio and zoom taken out, never under
  `spec.MIN_VIEWPORT_WIDTH`) - only when the CSS width the page would then be
  laid out at (the window over the zoom) is no narrower than the width the
  recorder saw it keep its layout down to (`VideoTake.layout.holds_down_to`,
  `capture.probe_layout`), else, when the probe saw it change there, why
  not. A browser zoom lays the page out exactly as narrow as a smaller
  window. A take recorded before the probe, or a file, is told neither: a
  1600 px dashboard told "a viewport about 548 px wide" came back as its
  mobile layout, stacked and cropped on a wall screen. Each of the three draws the source larger by the same factor, so
  each is offered only at a scale the source carries at the quality the
  video's renders are made at (`Legibility.quality`,
  `render_checks.target_quality`; `Legibility.carried`: at least
  `screen_insert.MIN_DENSITY` source pixels per output pixel, what the soft
  gate holds that render to): an author told "push in with zoom {scale:
  1.7}" had `render_video` refuse it at ultra (0.65 per pixel). A scale it
  does not carry is offered with what makes a source carry it - the next
  pixel ratio a recording is made at (`RECORD_RATIOS`, the same layout,
  more pixels), the picture width a file needs -, else, for a recording,
  the largest push it carries and a higher browser zoom
  (`Legibility._rezoomed`: its text larger in the same pixels, the push
  left after it one the source carries) only where the probe saw the page
  keep its layout there; nothing larger when it carries no push at all. A
  scale sharp at the judged quality that a larger render refuses names the
  first such render and the density it would draw it at
  (`Legibility._ceiling`), as `insert_resolution` would once applied.
  A source drawn under `insert_legibility.MIN_PUSH_SCALE` (0.3x) shows
  as a whole page - a dashboard on a wall screen - which a push crops:
  it is offered no push (`Legibility.pushable`), nor a browser
  zoom whose push left after it would crop it, and the advice says why
  ("no push: drawn at 0.18x, under 0.3x, it shows as a whole page and a
  push would crop it"). Every advice ends offering `legibility:
  "ambient"` for an insert that is scenery (`AMBIENT_OFFER`), declared
  once on the insert, a wall's panes with it.
  The surface is offered only where the scene has it: `judge`
  places every insert surface of the scene on the frame
  (`insert_checks.box_on_frame`) and tells each one (`_beside`) the width
  of the other inserts side by side with it at the same time
  (`_side_by_side`: no more than half the narrower overlapping across,
  half the shorter's height shared; `_together`: their spans overlap), each
  as wide as its own advice asks (`Legibility.asked_width`: its
  `needed_width` when it is judged too small, else its width), its own
  wall's other panes apart (`panes_beside`: fewer panes frees theirs, so a
  pane they crowd is offered "fewer panes on the wall" alone). Where its
  `needed_width` and theirs sum past the frame's width
  (`Legibility.crowded`) - two monitors on one wall each told 68% of it -
  the advice says so first (`_crowding`: "the scene has no such surface
  for it: about 1372 px ... 143% together") and offers what needs no more
  of the frame, whatever the quad's share: a push, a recording made again
  at the browser zoom that draws its text 12 px, up to the
  `Legibility.largest_zoom` its page keeps its layout at - the window over
  the probe's `holds_down_to`, to the 10 below, within
  `spec.MAX_BROWSER_ZOOM` ("largest safe zoom 150 on this page"), with the
  text size it then reaches and why a higher one is not offered
  (`_safe_zoom`; a take never probed is said so) -, or a tighter page - a
  single panel recorded on its own, a file cropped to the part to read
  (`_tighter`).
  A save's answer says the advisory once per insert (`said_once`:
  the surface and its source, `QualityFinding.insert`, remembered by video
  in `render_cache` - kind `insert-legibility-said` - for the findings the
  answer lists, and with them every advisory of the same kind on the same
  insert whose advice is the one listed (`insert_of`, `advice_of`: the
  message past its measure): a wall's panes told the same are remembered
  together, never one more pane on each save, while a pane with
  another problem is said by a later save; one of an insert it lists
  nothing of is said by a later save; the finding under
  6 px every time; `get_video` and the editor's strip still show both). The
  downscale ratio is said, never judged: a pixel-ratio-2 recording is
  drawn under 0.6x by design.
  A screen insert declared `legibility: ambient` (`spec.INSERT_LEGIBILITY`;
  unset, `legible`) is scenery - three monitors sharing one frame, each
  recording at 0.2x -: its sources are read like any (`Legibility.ambient`) and never judged - no finding, no advice -, and the report says the
  size of its smallest text per insert as a fact, the smallest of a wall's
  panes ("ambient, not judged - its smallest text, about 24 px in the
  source, reads about 5 px on the 1080p frame (drawn at 0.21x)";
  `(its text not read)` when it was not; `insert_legibility.unchecked` ->
  `QualityReport.unchecked`, under its own "Not judged, as the video
  declares (no finding)" heading in `format_quality_report`). The field is in
  `spec.UNDRAWN_INSERT_FIELDS`: `render_checks.digest_scenes` leaves it out,
  so declaring it keeps the scene's passing preview. A shot's failed
  regeneration is an advisory said once whatever the number of scenes
  showing it, naming the file of the take that plays (`ShotFacts.playing`). `insert_resolution` (advisory, `ContentFacts.soft`) is a
  screen insert surface or a replaced screen a whole render would refuse as
  soft at one of `spec.RENDER_QUALITIES`, read as the render gate reads it
  (`render_checks.soft_qualities`): "soft at ultra, which render_video
  refuses: <the gate's words>; standard and high draw it sharp", the source
  size named at the largest - one that size is sharp at every quality -, a
  recording told the `record_browser_demo` call that sizes it. Said once
  per surface, source and qualities (`said_once`, `SAID_ONCE_KINDS`), never
  a block: the gate refuses at the quality asked. `record_browser_demo` says when a take is made
  what it plays at each of `timeline.CANDIDATE_SPEEDS` and the narration that
  carries (`timeline.recording_lengths`), and how far down its page keeps its
  layout (`capture.layout_words`). With `target_render` (one of
  `spec.RENDER_QUALITIES`) and the insert's `video_id`, `scene` and
  `insert` (its overlay index, needed when the scene has several), it reads
  the insert's quads off the saved spec at that quality
  (`render_checks.size_recording` -> `insert_checks.surfaces_at`, a wall
  pane by pane, the insert's push counted) and records what
  `insert_checks.recording_for` sizes, on a window of the width asked at the
  largest quad's own proportions (`insert_checks.quad_window`: a 1.9:1
  TV is a 1600x842 window, never a 16:9 one its `fit: contain` letterboxes -
  13.2% of the quad on 1d30f3fe): the smallest pixel ratio from the one
  asked that passes on that window (the same layout, more pixels), else
  at 2x a wider window at its proportions zoomed in as much
  (`browser_zoom`), so the page is laid out at least as wide as asked -
  judged by `screen_insert.density` on the frames it paints, so the gate
  passes it; none passes within `spec.MAX_VIEWPORT_WIDTH` / `MAX_BROWSER_ZOOM`
  is refused, the quad and the quality named.
  A recording is no longer only advised. The save refuses each screen
  insert surface whose recording is soft at the quality the video is made for
  (`projects.soft_recording_problems` -> `render_checks.soft_recordings` ->
  `insert_checks.soft_takes`, read off the rows as the gate reads them): its
  newest whole render's quality (`render_checks.target_quality`), else
  `spec.FINAL_QUALITY` (ultra) - a preview draws the source at 1080p, where a
  recording sharp there (0.80 sharpness) fails a 4K render (0.44) once every
  scene is green. The refusal (at `scenes.<n>.overlays.<i>.source`, or a wall
  pane's `sources.<p>`) names the source, the quad, the density and the
  recording that passes on every surface the take is drawn on there
  (`insert_checks.recording_for`, from the take's own window - its painted
  size over its pixel ratio - and ratio up): "record it again as a 1280x720
  window at pixel ratio 1.5, its frames 1920x1080 px (0.80 per pixel there):
  record_browser_demo(target_render: "ultra", ...) records it at that size",
  or, with no window a recording takes passing, the quad or the push to
  shrink. It runs once the save's bezels have written the quads left to the
  footage. An AI shot, a picture and a replaced screen stay advised
  (`insert_resolution`), and a source soft only above the target quality too:
  the render's gate refuses them at the quality asked. A reading that cannot be
  made refuses nothing.
  They are merged with
  the layout report worst first and kept when the layout could not be
  measured (`format_quality_report`, the editor's strip with its own
  severity words: each content finding carries `group: "content"`,
  `QualityFinding.group`, which the strip reads instead of a list of kinds of
  its own). Those findings change with the media, not the revision: the
  preview carries `media_key` (`projects.media_state`: each recording, AI shot
  and track - its status, the take it plays, when its last attempt ended) and
  the editor keys its quality query by it, so a shot generated again in place
  refreshes the strip without a save; `regenerating` lists the shots being made
  again beside their kept take, which the editor polls.

### Perceptual gates (issue #10036)

What a viewer sees as poor at once while every other gate is green, measured
in `video_studio/perceptual.py` (pure, numpy and Pillow) and judged by the
content findings of `quality.py` (`QUALITY_REPORT_VERSION` 53):

- **`synthetic_voice`**: the narration check (`voice_check._record`) keeps the
  placed take's timbre (`perceptual.timbre`: mean spectral flatness over the
  voiced frames, share of energy above 8 kHz) and, when a take of the same
  voice was heard at speed 1, that take's timbre as `timbre_reference`. A take
  flatter than its reference by 0.08, or carrying 2.5x its high-frequency
  share, sounds synthetic; with no reference the fixed ceilings (flatness 0.32,
  6 % above 8 kHz) apply. A narration said under speed 0.95 on a model that
  slows by time-stretching (`TIME_STRETCHING_MODELS`: `tts-1`, `tts-1-hd`) gets
  an advisory `synthetic_voice` whatever the template. Records kept before the
  release carry no timbre and say nothing until the narration is checked again.
- **`texture_noise`** (issue #10083, report v56): each ready AI shot's three
  kept frames are read on an 8x8 grid (`perceptual.noise_map`). Noise is the
  spatially uncorrelated residual, never fine detail: per pixel the smallest
  absolute second difference over four directions (a stone joint, a straw
  strand or an edge is smooth along one of them), averaged over 8 px patches,
  each tile reading the 10th percentile of its patches (noise is in every
  patch, texture in some). Judged:
  - against the shot's start picture when it has one
    (`clips.start_frame_file_id`, resized to the frame): tiles where the take
    carries 2.5x the start picture's noise and at least 4 grey levels, on 15 %
    of the frame or more; what the start picture already shows never counts;
  - else the tiles at 14 grey levels or more, covering at least 15 % of the
    frame: against the rest of the frame (2.2x) when they cover up to 60 % of
    it, against the absolute floor of 14 alone when they cover more (a
    whole-frame texture has no rest to compare with).
  Calibration: a clean stone barn over straw reads 11 at most, a crunchy
  over-sharpened carpet 18 and over. The finding names the region (a box of
  the frame), what it was judged against and the frame's file. A take is read
  once (`render_cache` kind `perceptual-texture`, keyed by `TEXTURE_VERSION`,
  its frame files and its start picture).
- **`insert_capture_floor`**: a recording a screen insert plays that painted
  under 15 frames a second is refused as an insert source. The rate held is the
  one painted over the seconds its page changed (#10087,
  `insert_floor.motion_rate`, kept as `capture.insert_rate` = `{whole, motion,
  seconds, windows}`): Chromium's screencast hands over a frame only when the
  page paints, so a still console gives 0 to 1 a second and a smooth take reads
  4 to 7 fps over its whole length. Motion windows are the stretches where
  consecutive painted frames differ on `capture_rate.MOVING_SHARE` of the frame,
  changed frames under `MOTION_GAP` (1 s) apart grouped, each starting at its
  first changed frame; a window under `MOTION_LEAST` (0.25 s) is a lone change
  captured exactly. A still page (`motion` null) is fully captured. Idle is told
  from starved (#10101, `insert_floor.classify`): the motion watch
  (`capture._MOTION_WATCH_JS`) records on each step's event the spans the page
  asked to move over (`painting`: the animation frames it requested, the
  transitions and animations that ran, merged under 100 ms apart, read by
  `TakeRecorder.painting`). With them, changed frames inside those spans and the
  scrolls form windows under `MOTION_GAP`; outside them only a dense run
  (`DENSE_GAP`, 0.1 s) is judged, so hover cards and tooltips a second apart
  are a resting page; a window needs `MIN_CHANGES` (3) changed frames. The
  capture keeps `{whole, motion, seconds, windows, starved: [[start, end,
  fps]], idle, painting: true}`; the failure reason and the take's capture line
  (`capture_rate.idle_words`) name the starved seconds and the idle ones, and a
  failure with no starved stretch, or measured before #10101, advises no smaller
  window or pixel ratio. A take recorded before the measure is held to
  `TakeMedia.captured_frames` over its duration (`perceptual.insert_rate`). The same floor is held earlier (#10079,
  `insert_floor`): a take recorded with `target_render` carries
  `capture.for_insert` (video, scene, insert, quality); `runners.run_take`
  measures its painted rate before it is saved and, under the floor on its first
  take, records it once more in the recording lane (no attempt spent, the next
  claim's worker, its first rate and worker kept in `capture.floor_retry`).
  Under the floor again it ends `failed` with "captured N fps, under the 15 an
  insert source needs", both rates and workers, and the window and pixel ratio
  estimated to sustain it (`insert_floor.sustained_size`: painted rate taken as
  proportional to painted pixels, 0.85 margin, the take's window at a lower
  pixel ratio first). Over it on the second take it is ready, its notes saying
  it was recorded twice. A save (`projects.save_spec`) adds one note per screen
  insert playing a take under the floor, with the
  `record_browser_demo(from_recording_id, target_render, video_id, scene,
  insert)` call that records it again, before any preview's lint. A take
  without `target_render` is unchanged.
- **`insert_legibility` on an ambient insert** (narrowed by issue #10093,
  `QUALITY_REPORT_VERSION` 55): `legibility: "ambient"` is honoured on
  scenery - the insert is said under "Not judged, as the video declares"
  only. `insert_legibility.ambient_overrides` keeps one case: a recording
  insert whose UI text is under the illegible floor (6 px of the 1080p
  frame), drawn at `MIN_PUSH_SCALE` (0.3x) or more, alone on its scene's
  frame (no other insert, no other pane of its wall) and not on a statement
  scene (dimmed under its text and chips). It is always an `advisory`, never
  counted by the render gate, whatever the template; its advice never offers
  the `ambient` declaration it already carries. Two wall monitors at 0.25x, a
  dashboard under a statement at 0.20x: nothing listed.

Severity of `synthetic_voice` and `texture_noise`: `dense` (a render waits for
it) when the video's template is made for a `brand_film` or a `product_demo`
(`perceptual.blocks` over the template's `purposes`), `advisory` on any other
video. The ambient `insert_legibility` override is advice on every video.

## Animated stills

An `image` scene's `animate` (`true` or `{prompt}`) asks for an AI shot made
FROM the image. `projects.start_animations`, inside `save_spec`, starts it
(`clips.start_clip` with `reference_file_id` and `sound=False`, the default
prompt moving the camera and keeping every detail) and writes its `clip_id`
into the scene; a save without it (an editor draft) inherits the stored
spec's for the same scene and image, and a changed image gets a new shot, so
a save never pays twice. `animate.keep` names regions of the image the
shot keeps exactly as the still has them (a banner, a sign: `keep_regions`,
see "Kept regions" below); a shot made with other regions is made again, the
save saying so. No live video model leaves the scene a still with a
note in the tool result; a picture no provider can read
(`clips.StartPictureError`, see "The first frame") fails the save with a
problem naming `scenes.<i>.file_id`, never the render. The planner plays a made shot as a `clip` payload holding its last
frame (`media.clips[scene_id]`, filled by the preview and the render only when
the shot is ready); otherwise the still with its motion - a render never
waits for an animation (a note says so).

**Without a model.** An `image` scene's `effects` (`spec.StillEffect`, at most
`MAX_STILL_EFFECTS`) move the real picture instead, its text never redrawn -
the way to give a product screenshot motion. `parallax` lifts regions of the
image (`layers`: a `box` `[x, y, width, height]` in shares of the image and a
`depth`) off it with a shadow, each a copy of the image cut to its box,
moving with the scene's camera motion faster than the image (outward on a
zoom, along a pan, a slow float on `motion: none`, `parallaxShift`);
`light_sweep` crosses it with a band of light (`at`, `duration`, `angle`,
`width`, `strength`, `repeat`); `pulse` glows on one region in the theme's
accent (`box`, `shape`, `period`, `count`). The planner hands them to the
compositor as the scene's `effects` (`timeline.plan`), which draws them in
one stage with the image, the stage carrying the camera transform
(`buildEffects`, a pure function of time like the rest), so the render and
the editor's preview agree; the preview sizes such an image from its stored
geometry (`overlay_picture_size`) so a box lands where the render puts it.
`animate` and `effects` are exclusive (`ImageScene._one_way_to_move`).

## Figure scenes

A `figure` scene (`spec.FigureScene`) draws ONE presentation block alone, on
the video template's palette, and builds it in: an architecture's zones and
nodes, a process flow's steps, a timeline's items, a roadmap slide's phases,
a chart's series, a Mermaid diagram's nodes come in one step after the
other, edges draw, series grow, and the camera can move between the steps;
built, the figure keeps moving to the end of its scene.

- **Source.** Either a deck address - `presentation_id` + `slide` (or
  `slide_id`) + `block`, the 0-based index among the slide's blocks
  (dashboard panels and columns flattened, `figures._blocks_of`; a roadmap
  slide is its phases) - or `block_spec`, a block written as in a
  presentation spec, validated by the deck's own validator as the block of a
  one-slide deck (`spec.figure_block_problems`, problems under
  `block_spec...`). Exactly one (`ONE_OF_FIELDS`). A save checks the address
  resolves (`projects._figure_address_problems`: no such slide, no such
  block); the files a `block_spec` names (`*file_id`,
  `spec.figure_file_ids`) are references checked like any file. Setting
  `presentation_id` drops a `block_spec` the scene had (and the other way
  round), so `{op: update_scene, scene, fields: {presentation_id, slide,
  block}}` repoints a scene that had copied its block.
- **Twins** (`figure_twins`). The figure is drawn in the video's
  scheme, its slide's pictures as the deck placed them for its own: a
  light deck's architecture places the dark-on-light product logos. Each
  template asset the slide draws (`FileAttachment.source` `template_asset`)
  is drawn as its twin when one suits the video's scheme better
  (`figure_twins.themed`, on every source the save, the preview, the stills
  and the render resolve: `figures.figure_previews`, `render._figure_source`).
  A twin is an asset of the same template the picture's description names
  by its file's name, or whose description names it (`named`: "...
  opencti-logo.svg is the light-on-dark twin"), drawing the same shape -
  its ink's box in the same proportions within `SAME_SHAPE` - in another
  tone: read off the pixels, never the words (`image_geometry.image_tone`:
  the alpha-weighted mean relative luminance of the ink, through the same
  bounded decoding and SVG rasterizer as the geometry; kept by stored
  object in `render_cache`, kind `figure-twin-tone`, within
  `TONE_SECONDS`). `pick` draws the one that suits the scheme best - the
  lighter ink on a dark figure, the darker on a light one, an opaque
  picture the other way round as its own surface - when it does by
  `MIN_TONE_GAP` more, and only a twin the person the figure is drawn for
  may read (`file_access.can_read_file`). The file ids are swapped in the
  source's slide (its `files` and its `twins` follow), so the still's
  digest, the render key and the pictures the page may fetch follow too;
  the save says which (`figures.twin_lines`: "Scene s05: its figure draws
  4 pictures as their twins for the dark video: ..."). A figure whose
  twins cannot be read is drawn as its slide places it.
- **A retired raster's vector successor** (`figure_twins.successor`).
  A template keeps a raster for the slides that already place it and names
  the vector that replaces it ("opencrq-logo-light.png ... place
  opencrq-logo-light.svg instead"). Of the assets a raster names or is named
  by, a vector (its stored content type, `Asset.vector`) drawing its shape
  in its tone (`Tone.same_tone`: closer than `MIN_TONE_GAP`) is its
  successor - the pixels and the form decide, never the words. A raster is
  drawn as its successor, and its twin is picked among the successor's
  (`_drawn_as`); a raster twin with a successor of its own is drawn as that
  vector (rows seeded before the vector logo name the two rasters as twins).
  So a light deck's `opencrq-logo-light.png` on a dark video draws
  `opencrq-logo.svg`, sharp at 4K, and on a light video
  `opencrq-logo-light.svg`. A successor the person may not read is not
  drawn. The deck is never written to; the save names the substitution
  apart (`Twin.sharpened`, `Twin.through`, `figures.twin_lines`: "Scene
  s05: its figure draws 1 raster as the vector it names for the dark video,
  sharp at any size, the deck left as it is: opencrq-logo-light.png as
  opencrq-logo.svg (through opencrq-logo-light.svg).").
- **Page.** `/videos/figures/static` (`VideoFigureStaticRender.tsx`), public
  and token-free: it reads `__figure__/spec.json` (the one-slide deck,
  `figures.figure_document`: chrome off, the template's palette in the
  video's scheme) and the block's pictures from `__figure__/asset/<id>`,
  both answered by the render process through `open_render_page(answer=...)`
  - the one locked-down export browser, everything else still same-origin
  only; a picture is served only if the person the render is for may read
  it (`file_access.load_readable_file`). The deck's `StaticSlide` draws the
  block; `presentation.css` hides everything but it (`data-figure-root`).
- **Build** (`figure-build.ts`, `window.__xtmFigure`). `describe()` reads
  the steps from the block's markup (`FIGURE_PARTS`: per kind, the groups,
  nodes, edges and extras) with, per step, the lines of text that name it
  (`labels`: a node's label, sublabel and chips, never its description or
  the legend) and the seconds it takes to come in (`settle`). A plane (an
  architecture, a Mermaid diagram) is built as it is read: its zones
  together first, then each node on its own, row by row (`rows`); an edge
  belongs to the step of its later end, a plane's edge label to its edge;
  a chart's axes and grid are there from the start. An org chart is built
  a LEVEL at a time (`levels: "data-level"`, `levelGroups`): the root, then
  each level - its cards first, then its bubbles growing from their centre
  (`grow`, `growIn`) - each bus with the level it reaches, the zones of
  `group_by_branch` with the first level, the dotted lines on a last step of
  their own (`late`) with the legend; every `data-count-to` number counts up
  from 0 to its value as its seat comes in (`countAt`, `COUNT_SECONDS`), set
  by `seek` like the animations.
  `plan({times, length, camera, hold, safe})` makes paused Web Animations -
  pieces fade in as they rise 8 px (`translate`, which composes with an SVG
  node's own `transform`), edges draw (`stroke-dashoffset`), bars grow from
  their base, lines draw - and the camera's keyframes: each step framed as
  it comes in (steps arriving within one move framed together, max zoom
  1.8, 0.6 s moves leaving 0.2 s early), on the whole figure from `hold`
  seconds before the scene ends; steps standing on one another (sharing
  most of the larger one's area), a chart (`whole`) and `camera: still`
  keep the whole. Every framing keeps clear of `safe` (the subtitles'
  band). `seek(t)` sets every animation's time, so a frame is a pure
  function of time.
- **Motion** (`FigureScene.motion`). `drift` (the default): once
  built the figure never holds a frozen frame. The whole figure drifts in,
  eased, from 1.00 to `1 + DRIFT_ZOOM` (1.06) by the scene's end, about the
  middle of the room the subtitles leave (`drifted`), from the moment the
  camera is back on the whole figure (`driftFrom`; a still camera: once the
  last step has settled). Every edge (`FIGURE_PARTS.*.edges`, never a label)
  carries a light pulse from its source to its target once drawn - a copy
  of its line laid over it, one dash, thicker and glowing, without its
  arrowheads, `FLOW_SECONDS` end to end every `FLOW_PERIOD` (2.4 s) - and
  the core node breathes: the architecture's `emphasis: core` node (its
  `.deck-arch-node-glow` halo grows and fades back), else the one node most
  edges meet when at least three do and no other as many (`hubNode`), else
  none. They are looping Web Animations, seeked like the rest; `plan()`
  answers the scene moving from the first loop to its end (every frame is
  drawn) and `whole`, when every step is in with the whole figure framed.
  `none` holds still once built, as before. The editor's still is drawn
  `motion: none`: the figure at rest.
- **Fitted to the frame, as it is drawn.** A figure at `motion:
  drift`, `camera: follow`, stood `tilt` built correctly, then its drift
  and its stand added up on the last seconds and its left-most node went
  past the frame's edge, while the save, measuring the figure flat and
  still, said every text fit. Two changes. The figure's extent
  (`figure-build.ts`: `extent`) is what it paints: each piece's box and
  every painted element under the block - a label or a logo drawn past its
  node counted -, each cut to the ancestors that clip it (`paintedBox`, an
  SVG's viewport, an `overflow: hidden`), so the framing frames what is
  drawn. And the drift is fitted (`figure_fit.fit`, from `figures.plan_fit`):
  every pose from the drift's start (`drift_start`: the camera back on the
  whole figure, `hold` before the end, when it follows its steps -
  `camera_follows`, the page's own rule -, else once built) to the last
  frame is the page's framing and drift (`framing`, `drifted`,
  `drift_zoom`: `figure-build.ts`'s, their constants pinned by a test) under
  the compositor's stand (`project`: `scale(s) rotateX rotateY` about the
  frame's middle in a perspective of `PERSPECTIVE`, 1.1, times its longer
  side, the turns eased by the brand's `move` token, `move_ease`). The
  stand's numbers live in `figure_fit.STANDS` alone, which the compositor
  reads (`FIGURE_STANDS`, injected into `compositor_page`); a test holds
  `project` to Chromium's own projection. The drift (`DRIFT_ZOOM`) is
  capped so every pose keeps `FIT_MARGIN` (3 %) of the frame clear on every
  side (bisected, rounded down to 0.1 %); a cap under `MIN_DRIFT` (1.5 %)
  is not seen, and the figure is drawn `motion: none` instead. The plan
  carries it (`drift`, `figureBuild`'s `driftAt(..., amount)`), so the
  render, a preview and the editor's build draw the same; the render key and
  the build digest take the perspective and the easing, and `RENDER_VERSION`
  (2), `PREVIEW_VERSION` (4) and `BUILD_VERSION` (2) moved. A save says, per
  figure whose still is drawn, the drift it plays and its closest edge
  (`figures.fit_lines`: "Scene arch: its figure drifts in 6.0%, stood tilt,
  inside the frame at every pose of its drift: its right edge 5.9% inside
  the frame at 24.0 s"; capped: "its figure's drift is capped at 4.8% (of
  6%) so it stays 3% inside the frame on every side"; held: "holds still
  once built (drawn with motion none)"), and the quality check
  (`ContentFacts.figures`, `QUALITY_REPORT_VERSION` 11) finds a figure whose
  drawn box still crosses the frame (`outside`, overloaded). Measured: framed
  on what it paints, a figure keeps 4 % or more of the frame clear at every
  pose of a full drift under either stand, wide or tall, with or without
  subtitles - the cap engages where the subtitles leave a figure little room
  (a deep band under a tall figure facing the camera); the node cut off was
  a figure framed on its pieces' boxes while it painted past them.
- **Timing** (`figures.reveal_times`). `even`: step k of N at
  `0.35 + k * slot`, the slot an equal share of the scene before its last
  `WHOLE_HOLD` (2 s, the whole figure on screen). `together` (`build`):
  every step 0.15 s apart, the camera still. `narration`: a step comes in
  0.15 s before the narration names it - or before the subtitle showing
  the words that name it comes on screen, when that is earlier (a
  caption cue shows its whole phrase from the phrase's start, so a label
  said mid-sentence used to come in up to a second after its subtitle;
  `figures.shown_from` aligns the cue's words on the narration's,
  `difflib`, so a word the captions write otherwise keeps its cue; the
  cues are `TimelinePlan.captions`, on the scene's clock through
  `figures.scene_captions`, and part of the figure's key and its editor
  build's digest; the reveal line says `with its subtitle`) - and the steps it does not name are
  spread evenly between, before and after those it does (after the last
  one named, up to the scene's whole-figure hold: never stacked at the
  end); a step named so late that the steps after it would not have room is
  not followed. A step is named by a cue (`_cues`): one of its labels
  whole, a part of one split at its separators ("OpenCTI" of "OpenCTI -
  Threat intelligence", "OpenCRQ" of "Risk quantification (OpenCRQ)"), or
  one of its words in no other step's labels - one written like a name
  (inner capitals, all capitals, a digit) from 3 letters, any other from 5.
  Cue and narration are compared on one key (`_key`): lower-cased, run
  together without punctuation, a possessive or a plural, so "XTM-One",
  "xtm one's" and "XTM One" match, "Open CTI" and "OpenCTI" too, across up
  to six words of the narration; a cue of 6 letters or more also matches a
  spelling close to it (`difflib` ratio 0.84, weighed less). The steps are
  then aligned on the narration as a whole (`_align`): as many steps named
  as can be, in order and on words that do not overlap, the best cues
  first, the earliest last - so one step matched on a word said late never
  hides the steps after it.
  The words are timed on the spoken voice (`speech_timing.word_times`: the
  conditioned WAV's loudness every 10 ms, its pauses, the text's clause
  breaks put on the pauses that match them, the words laid over the voiced
  time by their letters), else paced over the narration's planned span; no
  speech provider returns word timestamps. A step then stays its scene's
  `dwell` (0.8 s) on screen once in: a step that would cut the one before
  it short joins its beat, so a scene too short for its steps shows them a
  few at a time, never all at the end. A narrated figure lasts
  `figures.narrated_hold` past its narration (`timeline.plan`: a step's
  settle, its `dwell`, then `WHOLE_HOLD`), so a step the narration names last
  still has the room to be followed.
- **Reveal times said**. The steps the figure page describes are
  kept beside its still (`figures.steps_object`, written before the still
  so a kept still has them). A save (`create_video` / `edit_video`) times
  each figure scene on its own plan (`projects.figure_timings_of` over
  `projects.estimate`: the scene's length, its narration's span) and its
  result says when each step comes in and what the narration says there,
  or that it is spread (`figures.reveal_lines`, its words paced: the render
  times them again on the spoken voice and logs the same line). The
  quality check, run on every save and every `get_video`, judges the same
  stills (`projects.figure_stills_of`) and keeps each figure's fit in the
  process by what it is judged from (`figures._fit_key`: the still's
  digest, its timing, the brand's easing, the band kept clear, the scene as
  written - its perspective, motion and narration): an unchanged figure
  reads its steps from the object store once.
- **Draw** (`figures.draw`, render stage `figures`). After the first
  `timeline.plan`, each figure is drawn for its scene's planned length and
  narration's words, at the video's frame rate and output scale, centred in
  the band its subtitles leave (`figures.caption_safe_area`: when captions
  are on and the scene has a narration, from the safe margin,
  `figures.SAFE_MARGIN` 4% of the height, to the top of the tallest pill the
  page draws, `compositor_page.caption_extent` - two lines of the caption
  style at its edge offset; mirrored for subtitles at the top - the page
  itself is drawn full frame, so the compositor's own reserve cannot apply).
  The band used to run to the reserve's top, a gap and two lines above
  the pill, which left the brand film's loop figure at 71% of the frame
  with the band under it empty. A save's line says where the built figure
  spans (`FigureFit.extent`: "Its figure spans 14% to 72% of the frame's
  height, centred between 4% and 83%."). The page is
  screenshot only where something moves (`figures.frame_times`; a still
  stretch is one held frame) and the frames are encoded by
  `encoder.frames_to_mezzanine`. The planner plays it as a `clip` payload
  (`fit: contain`, identity time map) and the render plans again - the same
  timeline, a figure's length never depending on its drawing. Undrawn (the
  preview, an estimate), the planner plans a still. The whole path - the
  built page served on loopback, Chromium, ffmpeg - is pinned by
  `test_video_studio_figure_chromium.py` (the nodes drawn at 25 / 50 / 75 %
  of the scene), run by the `Browser tool (Chromium)` job, which builds the
  frontend.
- **Drawn once per key.** The brand film's previews of `s04` and `s06`
  each redrew the 589 frames of `s05`'s figure for the 0.6 s their
  transitions show of it. A figure's key (`figures.render_key`) is what its
  frames are drawn from: the block, the scheme, the palette and logos as they
  go into the page's document (never the document, whose deck and slide ids
  change on every read), the pictures the person may read
  (`figures.readable_pictures`), how the scene builds it, its length, when its
  narration says each word, the band it keeps clear, the size, the frame rate,
  the release and `RENDER_VERSION`. `figure_cache` keeps it: on the worker,
  one folder per key under the temporary directory shared by every render
  process (`figure_cache.folder`), each frame drawn whole linked there under
  its index (`share_frame`) and taken by the next render of the key
  (`take_frame`), so a preview replaced while it drew leaves its frames to
  the next; the video encoded whole is kept there, its frames removed, and
  stored beside the video in the object store
  (`video-studio/figure-renders/<video>/<key>.mp4`, `figure_cache.keep`), so
  another worker, or this one after a deploy, plays it as it is with no page
  opened (`figure_cache.find`). The key is `<content>-<scale>x<fps>`
  (`figures.variant_key`): a render at another quality of the same
  revision finds a sharper drawing of the same content
  (`figure_cache.find_sharper`: a higher or equal scale at a multiple of its
  frame rate, the least sharp that covers it, on the worker then beside the
  video) and derives its own from it with one ffmpeg pass
  (`encoder.rescale`), with no page opened. An ultra render's figure serves a
  high one, a high one's a draft, and only a sharper quality than any kept
  is drawn, since an upscale would blur it. A video drawn while a picture
  the person may read could not be read is never kept. The drawings go with the video
  (`figure_cache.evict`, from `DELETE /projects/{id}`; hourly for a video gone,
  `sweep_store`), past `KEPT_PER_VIDEO` (48) the oldest stored, and a worker
  folder unused for 6 hours is swept with the working directories
  (`sweep_disk`). A scene preview draws a neighbour's figure only for the
  frames its window shows (`render.figure_span`: from the window of
  `window_times`, two frames of margin): `figures.draw(span=...)` draws
  `frames_in` the span into the same folder and encodes a part holding the
  nearest frame elsewhere, never kept as the whole - the next render of the
  key draws the rest. Pinned by `test_video_studio_figure_cache.py`.
- **The editor's still.** Every save hands, once committed, the still of each
  figure not drawn yet to the worker (`projects.queue_figure_stills` after the
  REST writes and `create_video` / `edit_video`, before the studio is told to
  refresh; `figure_stills.enqueue`) and answers at once - the tools say
  the stills are being drawn. The still is the scene's last frame - every step in,
  `camera: still`, the same band, seeked to the end (`figures.draw_still`) - as a JPEG at the
  video's design size, kept in the object store under `PREVIEW_PREFIX` by the
  digest of what it is drawn from (`figures.preview_digest`: the block, the
  scheme, the palette, the logos' metadata, the size, the band,
  `PREVIEW_VERSION`; never the figure page's document, whose deck and slide
  ids change on every read). The preview computes the same digest for its reader
  (`figures.figure_previews`: the deck as they may read it, the palette of the
  template they see) and, when the still exists and they may read every
  picture it draws (`figures.pictures_readable`), plays it as the scene's
  image through `GET /figure-preview/{token}` (`media.mint_figure_token`,
  scope `video_studio_figure`, like the voice token); otherwise it words the
  `figure` state as before. A still a manager's draft finds missing (a video
  saved before stills, a deck block edited since, a draft never saved) is
  handed to the worker the same way, and the scene's state says `drawing`,
  which the editor polls. Nothing the spec says names a stored still. Two
  scenes showing one figure draw it once, and the timeline's scene thumbnail
  is the still (an image scene's picture), never a figure "in progress".
- **The stills job (`figure_stills.py`).** Never in a request, never in
  the API process: `enqueue` claims each digest not drawn yet (Redis,
  `figures.claim_drawing`, held while queued: a still asked twice - the next
  autosave, a preview - is queued once) and creates one `video_figure_stills`
  execution carrying what to draw (each still's figure page document, size,
  band and digest, and who reads its pictures), so a draft's stills are drawn
  like a save's. The worker runs one at a time (`concurrency_limit` 1) in a
  process of its own (`python -m app.services.video_studio.figure_stills
  <execution id>`, supervised by `render_process.supervise`: the render's
  `memory_ceiling()` over it and the Chromium it starts, stopped when no still
  is drawn for `STALL_SECONDS`); each still already drawn is skipped, one that
  fails is logged, the claims are released however it ends, and a failed job
  is never retried - the next preview that finds a still missing asks again.
  The still is stored with its steps beside it (`figures.store_still`).
  Retention: the hourly cleanup deletes the stills and their steps written
  more than `VIDEO_STUDIO_FIGURE_STILL_RETENTION_DAYS` ago
  (`figure_stills.sweep`, one listing with the dates,
  `file_storage.list_objects_modified`); the preview, a still's only reader,
  writes one it shows again, its steps with it, once halfway there
  (`figure_stills.keep`), so a still in use is kept and one nobody opened goes,
  to be drawn again by the next save or preview that finds it missing.
- **The editor's build.** Once its still shows, the preview plays
  the figure built in, as the render will: drawn for the scene as the
  preview's plan times it (`figures.figure_timing`, the narration paced),
  lighter than a render's (`BUILD_FPS` 15, `BUILD_SCALE` 0.5), kept under
  `BUILD_PREFIX` by the digest of its still and of that timing
  (`figures.build_digest`: build, reveal, camera, dwell, motion, length and,
  narrated, the narration and its span), its meta - size, frames, `whole` -
  written after the video. `preview._figure_builds` finds it and plans the
  scene again with it as a clip (`GET /figure-build/{token}`, the figure
  token, by ranges), the still its `poster/` - what the stage and the
  timeline show while it loads; one not drawn yet a manager's preview draws
  in the background (`figures.prepare_builds`, the stills' claim and
  semaphore), the scene marked `preview_building`, which the editor polls,
  showing the still meanwhile. The scene says `preview_figure_whole`:
  selecting it in the timeline lands there (`sceneLanding`, before the next
  scene comes in), and the pointer over it skims the build - the stage
  draws the time under the pointer while paused (`Skim`, `playhead.ts`),
  back on the playhead when it leaves.
- **In space**. `perspective` stands the figure in 3D while it
  builds: `tilt` leans it back and turns it towards the camera, `orbit`
  turns it slowly side to side (`flat`, the default, is the page as it
  was). The planner sends it in the payload; the page gives the layer a
  CSS perspective of 1.1 times the frame's long side and moves the box, so
  a still, a preview and a render stand the same. Its poses are
  `figure_fit.STANDS`, the ones its drift is fitted at (above).

## Motion design

Everything on screen moves through ONE set of numbers, the brand's motion
tokens, and a motion scene draws the choreographies a motion designer
writes - kinetic type, a logo reveal, a counted figure, a list built item
by item, masks, drawn strokes, morphing shapes, a 3D camera, particles,
Lottie animations - as a pure function of time, like every other scene of
the compositor page: frame N is the same picture whatever frame came
before it, so page runs, segments and scene ranges stay exact.

- **Tokens** (`motion_design.resolve_motion`). `theme.motion`
  (`spec.MotionStyle`) names a personality - `precise`, `calm`,
  `energetic`, `cinematic`, `playful` - with `speed` (0.5-2, every
  duration divided by it), `title` (how card titles split: lines, words,
  letters), `logo` (rise, fade, scale, mask), `accent` (`sweep`: a light
  crossing the gradient words once they are in), `beat_sync` and
  `motion_blur`. A personality resolves into a time scale, the four
  semantic easings (`enter`, `exit`, `move`, `emphasis`), a title style
  (split, rise/mask/pop/blur, duration, distance, blur, rotation, stagger),
  a block style (statement lines, steps, words, drift), the cards' logo,
  kicker, rule and call to action, and the overlay timing. `precise` is
  the numbers the page drew before tokens existed, so a video without
  `theme.motion` draws the same frames (`test_video_studio_quality.py`
  and the transitions' extraction tests pin it). The tokens travel in the
  document (`theme.motion`, `build_theme`); the page decides no duration of
  its own.
- **Easing** (`compositor_page.py`, before `el`). Named curves, CSS
  `cubic-bezier(...)` and `spring(stiffness, damping)` - a closed-form
  damped oscillator, so a spring at time t needs no previous frame - and
  the semantic aliases a personality maps. Every curve is checked at save
  (`spec._ease_text`).
- **Motion scenes** (`spec.MotionScene`, `type: motion`). A
  `composition` (`spec.MOTION_COMPOSITIONS`: `kinetic_title`,
  `logo_reveal`, `stat_counter`, `list_build`, `split_statement`, `quote`,
  `constellation`) from the scene's texts (`title`, `kicker`, `subtitle`,
  `figure` + `label`, `items`, `lines`), and/or its own `elements` drawn
  over it. `motion_design.compose` lays the composition out on the
  frame's design canvas (`Frame`: type scaled by the frame's short side to
  the power 0.35, so a 9:16 title stays readable) with the tokens, then
  validates every element it made through `spec.MotionElement` - a
  composition can never draw what an author could not write. The page
  draws elements and knows no composition.
- **Elements** (`spec.MotionElement`, `_KIND_FIELDS`: a field another kind
  owns is refused). `text` (a type `role`, `split` into lines, words or
  letters staggered from the start, the centre, the end or at random, a
  `count` of the number in its text from zero, a list `marker`, the
  accent's `*words*`), `shape` (rect, ellipse, line, ring, polygon, star,
  filled and stroked in the theme's roles), `path` (SVG path data `draw`n
  along its length, `morph`ed into `morph_to`), `image` (a file, served
  like an image overlay's picture, or the template's logo, `asset`),
  `lottie` and `particles` (`number` seeded points drifting and
  twinkling, joined under `connect` into a constellation). Each sits at
  `x` / `y` by its `anchor` - or `below` an element listed before it, a
  `gap` under its bottom as the page lays it out, so a title that wraps
  pushes what stands under it down - with a `z` for the camera and a
  `start` / `end`. `keys` are keyframes of position, scale, turns (also in
  3D), depth, opacity, blur, a mask's `reveal`, `draw`, `morph` and
  `progress` (a count's, a Lottie's playhead), each with its easing.
  `enter` / `exit` / `loop` are presets drawn with the tokens (rise, drop,
  slides, scale, pop, blur, track, masks from each side, iris, draw, flip,
  typewriter; float, pulse, breathe, spin, sway, shimmer), laid over the
  keyframes. A `mask` (a rectangle from one side or a circle from the
  centre, feathered) is what a reveal uncovers; a motion `path` is one the
  element follows, turned along it with `orient`. Sizes are bounded per
  kind (a text 8-400 design pixels, a particle 1-40), blur to 120.
- **Camera** (`spec.MotionCamera`). A scene's `camera` has a perspective
  distance and keys of the point it centres, its zoom and its turns; every
  composition pushes in slowly (`_camera`, deeper and turning for
  `cinematic`). Elements with a `z` or a 3D turn are seen through it in
  perspective.
- **Lottie** (`vendor/lottie_light.min.js`, lottie-web 5.13.0, MIT,
  `vendor/lottie-web-LICENSE.md`). The light SVG player is inlined in the
  page; each Lottie element is a player seeked to the frame of the scene's
  time (`seekLottie`, from `it.want` once loaded, so the first frame is
  never frame 0 by accident). `check_lottie` refuses a file over
  `MAX_LOTTIE_BYTES` (2 MB) or `MAX_LOTTIE_LAYERS` (400), one with no
  size, rate or frames, and one that loads anything from outside itself
  (an image not embedded, a font by path or URL): the page fetches
  nothing a file names. Expressions are never evaluated (`lottie_light`
  has no expression engine); a file that has some plays its keyframes and
  the render's notes say so. A save checks every Lottie file a scene names
  (`projects._lottie_problems`, problems under
  `scenes.{i}.elements.{j}.file_id`); the render and the preview read and
  check them again (`render._gather_lotties`, `PlanMedia.lotties`) and
  leave out the ones they could not, with a note.
- **Planner** (`timeline._motion_payload`). The scene's elements as the
  page draws them (`element_payload`: presets' timings and easings
  resolved), its pictures and Lottie files keyed, a missing one left out
  with a note; chapters and the editor name the scene from its texts
  (`drawn_texts`). `reveal_end` is when its last element is in: the
  quality check measures the time left to read it (`quality._TEXT_SCENES`),
  and `frame_freeze.STILL_SCENES` knows a motion scene moves.
- **Beat sync** (`beats.py`). `beat_sync: cuts` lands each cut on a beat
  of the music: a scene plays a little longer, never shorter, up to
  `MAX_SHIFT` (1 s) - its footage holds its last frame, a loop runs on
  (`timeline.beat_extra`, `_hold_longer`); a figure scene and the last
  scene are left as they are. `all` also lands a statement's lines and a
  motion scene's element entrances on the nearest beat within half a beat
  (`payload["beats"]`, `beat_tolerance`; `snap_to_beats` for the
  elements). A built-in score's beats are its
  style's tempo from the video's first frame (`score_grid`). An AI track or
  an uploaded sound is listened to once (`detect`: spectral-flux onsets,
  the tempo between 70 and 180 BPM at which they repeat best, refined to
  the period that lands most of them, the phase as their circular mean,
  corrected for the analysis window's latency); a track whose onsets
  repeat at no tempo clearly (`MIN_CLARITY`, `MIN_LANDING`) has no beats:
  nothing is synced, said by the save's quality check before the render
  does (`projects.MeasuredPlan.beat_note`) with what to change (a
  track with a steady beat - every built-in one - or `beat_sync: off`).
  That note and the one of a `beat_sync` video with no music are
  `beats.SyncsNothing` (a `str`, every reader of the note unchanged), which
  `check_content` raises as the advisory `beat_sync_unsynced` on the first
  scene, its two fixes named, rather than a note of the report; an
  AI track not made yet stays a note.
  The save plans the cuts on the beats as the render lands them
  (`timeline.estimate_plan(beats=)`). The analysis is cached beside the render cache
  by file id (`file_analysis`), so the preview and every render read the
  same grid (`video_grid`). Measured on generated click tracks: within
  0.1 BPM and 10 ms of the true grid.
- **Motion blur** (`motion_blur: on`, high and ultra qualities only,
  `motion_design.blur_samples`). Each frame is drawn `BLUR_SAMPLES` (3)
  times across a 180-degree shutter (`blur_offsets`, centred on the
  frame) and ffmpeg averages them (`encoder.motion_blur_filter`: `tmix`,
  then `select` keeps one frame in three, `setpts` restores the clock;
  `FramePipeEncoder(subframes=...)` feeds the pipe at three times the
  rate). The frame count and the duration do not change; the pre-flight
  counts the sub-frames (`FrameKinds.samples`), so the estimate and the
  page's memory budget see the extra work. A render at draft or standard
  quality draws one sample: previews stay fast.
- **Frame capture** (`compositor.frame_capture_mode`,
  `VIDEO_STUDIO_FRAME_CAPTURE`: `auto`, `screenshot`, `begin_frame`).
  `begin_frame` drives the page's compositor with
  `HeadlessExperimental.beginFrame` (Chromium started with
  `BEGIN_FRAME_ARGS` after `RENDER_CHROMIUM_ARGS`):
  each frame is produced on demand rather than taken from the live
  compositor. A page whose frames cannot be begun (`_frame_page`) takes
  screenshots on an ordinary page instead; a begun page is never asked for
  a screenshot - it never paints by itself, so one would wait forever - and
  while its renderer starts up it answers begins without a picture, so it is
  begun again until it gives one (`_capture`, `_PICTURE_BEGINS`). Each page
  draws in a browser of its own (`compositor._on_browsers`): begin-frame
  control drives a browser's one display, and two pages begun in one browser
  stalled together on the render fixture - pages screenshot share one. The
  layout's memory counts them (`render_preflight.browsers_of`,
  `BROWSER_BASE`: 48 MiB a browser past the first, measured at 43 MiB in the
  worker image over a page more in the same browser, a blank page and one
  drawing at 3840x2160 alike), so a ceiling that holds 4 pages screenshot
  holds 3 begun. The page peaks keep one browser, measured on one page;
  the rates (`ESTIMATE_MODEL`) are per page-second and keep. Those two hung every render on the CI headless shell,
  which is why the default is `screenshot` and `begin_frame` is
  opt-in. `auto` is `begin_frame` on Linux (the render image's headless
  shell) and `screenshot` elsewhere. What is asked is never what a render
  uses unchecked: each worker probes it once at start
  (`worker.startup` -> `compositor.start_capture_probe`,
  `compositor.worker_capture`) - one begun frame with a picture within
  `CAPTURE_PROBE_SECONDS` (2 s) on a page of the compositor's own Chromium
  (`RENDER_CHROMIUM_ARGS` and `BEGIN_FRAME_ARGS`), tried again within 5 s
  for a Chromium starting cold on a busy worker (`CAPTURE_PROBE_TRIES`) - and takes screenshots when it gets none (`FrameCapture`: what
  was asked, what the worker uses, why). Screenshots asked start no
  Chromium. The supervisor hands each render process its worker's capture
  (`FRAME_CAPTURE_ENV`, `compositor.render_capture`): a render never
  probes, nor asks for begun frames its worker's Chromium did not give. A
  render whose begun frames fail it - its Chromium, a crash, a stall -
  demotes its worker to screenshots (`compositor.demote_capture`), so the
  render's next run draws with them. A worker on screenshots where begun
  frames are asked - its probe without one, or demoted - is probed again
  in the background `CAPTURE_REPROBE_SECONDS` (30 min) after
  (`FrameCapture.stale`), the render asking meanwhile drawing with
  what is known; the worker's shutdown cancels a probe still running, its
  Chromium closed (`compositor.stop_capture_probe`). The worker publishes its capture
  with its capacity (`render_preflight.publish_worker`, at once when the
  probe ends or a demotion changes it) and the pre-flight line says it
  (`Preflight.capture`, `FrameCapture.words`): "Frames are begun one by
  one on the render worker (its probe at start: a begun frame in 0.21
  s)." or why it takes screenshots. A capture change can then never fail
  a paid render. Measured in the render image: 19-29% faster per frame on
  motion and picture scenes (0.077 against 0.095 s at 1080p, 0.084
  against 0.111 s at 2160p), 13% on the render fixture's mixed 30 s video
  at 720p on 2 pages (109 against 125 s). It does NOT remove
  Chromium's raster history: the page's state at frame N is exact, but a
  moving glyph edge or a 3D layer's raster scale can differ by a few
  levels between two pages that reached N by different paths (PSNR 42 dB
  in both modes) - invisible, and why the tests compare the page's state
  exactly and the pixels by PSNR.
- **Templates.** Every built-in video template presets its motion
  (`services/template_container_seed.py`): the Filigran video `precise`
  with the masked logo and the accent sweep, the XTM One video `precise`
  with the sweep; THREAD `energetic`, cuts on the beat, motion blur; the
  two brand films `cinematic` with the sweep, cuts on the beat and motion
  blur; the event loop `energetic` with cuts and builds on the beat; the
  threat briefing `calm`; the social teaser `energetic` at 1.15x, titles by
  words, everything on the beat. The Filigran video, THREAD, the Filigran
  brand film and the event loop lock `theme.motion.personality` (a video
  still sets its own speed, beat sync and blur); the brand films' and
  THREAD's figures stand in `tilt`; each template's guidance tells the
  assistant when a motion scene fits.
  `templates.template_summary` answers the template's motion, which the
  editor shows (`VideoTemplateRules`) and uses as the defaults of the
  video's Motion settings.

## AI clips and video generation

Video generation is an LLM-provider capability, like image generation
(`app/llm/video.py`): `BaseLLMProvider.list_video_models` /
`generate_video(VideoRequest)` with a provider-declared `default_video_model`.

- **One OpenAI Videos API client** (`OpenAIVideosMixin`: create, poll,
  download) on both OpenAI-shaped bases, so OpenAI (Sora 2 / Pro / Pro
  High-Res), Azure OpenAI (its v1 endpoint, `_videos_client`), LiteLLM, a custom
  OpenAI-compatible endpoint and the Filigran gateway (`_video_headers` pins
  the upstream) all speak it; durations snap to 4 / 8 / 12 s. A request the
  API refuses as invalid (400 / 422) about a field it can relax is asked
  again in the next shape (`_attempts`: both 1080p sizes, 720p, the model's
  own size, then its own length too - a gateway in front of another vendor's
  model may take neither field), with a note. Veo is
  `GeminiProvider.generate_video` (a long-running operation; 4 / 6 / 8 s),
  relaxed the same way on a 400 (`_video_attempts`: 720p, then 8 s at the
  model's resolution, then its defaults). `video.names_a_field` decides: the
  structured `error.param` when there is one, else the refusal's text naming
  a relaxed field; any other refusal (a blocked prompt) ends the attempts,
  and the last refusal is the one reported.
- **Providers with a job API of their own**: ElevenLabs (Image &
  Video API: `POST /v1/flows/video`, Veo 3.1 and Seedance 2.x), Kling AI
  (`/v1/videos/text2video` or `image2video`; `kling-v3`, `kling-v2-6`,
  `kling-v2-5-turbo`), Runway (`/v1/text_to_video` or `image_to_video` with
  `X-Runway-Version`; Gen-4.5, Gen-4 Turbo from a start image only, Veo 3.1
  through Runway) and Seedance on BytePlus ModelArk
  (`/contents/generations/tasks`, the start image a `first_frame` data URL).
  Each creates the job, hands its id to `on_job` (Kling's names its endpoint,
  `image2video:<id>`), then `video.await_job` polls it (telling `on_state`
  the job's state at every poll: each provider's waiting word mapped to
  `queued`, Runway's `THROTTLED` included) and
  `video.download_video` fetches the signed URL it answers with
  (`video.fetch_signed_media` over `web_fetch.fetch_url_bytes`: the outbound
  policy on every redirect, the connection pinned to the checked address,
  `MAX_VIDEO_BYTES`, no API key). Each model's limits
  are data on the provider (lengths, resolutions, negative prompt, sound,
  start-frame types; `video.snap_resolution` beside `snap_duration`), applied
  and said in the notes; Kling relaxes `sound` then `mode` on a 400 naming
  them, and its account refusals (codes 1100-1103, sent with a 429) are
  never retried. Kling AI, Runway and Seedance are `only_capability = "video"`
  (validated with a free authenticated read, offered while the Video Studio
  is on, ranked first by `_VIDEO_PROVIDERS`); ElevenLabs keeps its voices as
  its validated capability and declares no `default_video_model`.
- **Errors.** `video.status_error` turns an HTTP failure into a readable
  `VideoGenerationError` (401 / 403: refused; 404: no video generation for
  that model at that endpoint; the provider's own message included) or, for a
  429, a 5xx or a network failure while CREATING the job, a
  `VideoProviderBusyError` (its `status_code`, the wait the provider
  asked for as `retry_after`, and `quota`) that `clips.generate` lets
  through. Every failure is read through `app/llm/provider_errors.py`:
  the HTTP status (`error_status`), the provider's own words
  (`api_error_message`), the wait it asked for (`retry_delay`:
  `retry_after`, a `Retry-After` header, Google's
  `google.rpc.RetryInfo.retryDelay`) and whether a quota ran out
  (`quota_exceeded`: Google's `google.rpc.QuotaFailure`, an
  OpenAI-shaped `insufficient_quota` - the error's structure, never its
  words).
  - **One model per round.** A shot is made on the model its author
    chose - the one named, else the platform's -, and waits for it when it
    cannot take the shot now; it is never failed for that, never moved to
    another model or provider, never asked at a lower quality. A provider
    busy - an operation it ended for its state (`google.rpc.Status` code
    `RESOURCE_EXHAUSTED`, `UNAVAILABLE` or `DEADLINE_EXCEEDED`,
    `provider_errors.rpc_busy_status`, read off the code, never the
    message: Vertex's "the service is currently experiencing high load"
    ended three regenerations of one shot of b56997a2 as failures), a 503,
    no answer at all, a 429 no cooling holds - has `run_clip` hold the
    shot on its own schedule (`provider_waits`: 30 s, then 1, 2, 5 and
    10 min, then every 10 min, or the wait the provider named, plus the
    shot's own share of it read off its id), its row `pending` with
    `provider_state` `waiting`, `provider_eta_at` its next try and the tries
    counted in `params.provider_waits` (`jobs.describe`: "provider busy,
    next try at 17:45 UTC (in 37 s), 3 tries: Vertex AI
    (veo-3.1-generate-001) - ..."), the adapter deferring the exact wait
    (`JobOutcome.busy`) for up to `provider_cooling.MAX_HOLD` (a day). An
    operation the service ended is never resumed: the next try asks anew.
    The record stays on the shot once the provider made it (`taken_at`); a
    regeneration starts its own. The author may cancel the shot meanwhile.
  - **Quota, read before paying.** A provider says what an account
    has left for a request (`BaseLLMProvider.video_credits` ->
    `video.VideoCredits`): ElevenLabs reads its subscription
    (`GET /v1/user/subscription`, the limit less what it used); Vertex AI
    exposes no quota to the Vertex AI User role and says nothing.
    `clips.refuse_unpaid` refuses a shot costing more than is left at
    `validate_only` and at the call (`quota: N remaining, M needed`,
    `status: invalid_parameters`, nothing queued or paid) - the cost the
    provider names, else the one its last quota refusal of the model said;
    `run_clip` reads it again before the paid call, and a shortfall cools
    the model for its quota instead of sending it. ElevenLabs' 401
    `quota_exceeded` is a quota (`elevenlabs_provider._quota_refusal`,
    read off `detail.status`, its credits off its message): the model cools
    until its `Retry-After`, else `QUOTA_COOLING_SECONDS` (an hour), the
    credits kept on the cooling (`Cooling.remaining` / `needed`, said in its
    sentence and `validate_only`'s `provider_state`), where every call of
    b56997a2 failed for a day and `validate_only` said `provider_state:
    null`.
  - A 429 is a refusal: nothing started upstream, nothing paid. `run_clip`
    answers `JobOutcome("hold", wait=retry_after)` and the clip adapter
    defers the execution (`orchestration_engine.defer`: no attempt spent,
    the deadline's budget given back, `adapter_state` `rate_limited`) for
    `rate_limit_wait` - the provider's wait, else 10 s doubling up to 60 s
    (the limits that refuse a generation are per minute), each up to a
    quarter longer at random (a minute at most) so a batch refused in the
    same second spreads out. Past `RATE_LIMIT_HOLD_SECONDS` (30 min,
    measured from the hold's `since`) - or with a provider wait that would
    cross it - the shot fails through `dead_letter` with how long it was
    held; a hold for a quota (`JobOutcome.quota`) waits up to
    `provider_cooling.MAX_HOLD` (a day) instead. The bound reads the wait
    itself (`rate_limit_base`), never the random spread added to it, so a
    quota naming the whole day `MAX_RETRY_AFTER_SECONDS` honours is waited
    out, not failed. Meanwhile the row says
    `rate_limited` (`jobs.describe`: "rate limited by the provider: asked
    again after a back-off").
  - The refusal is the provider's state, not the shot's:
    `provider_cooling.note_refusal` puts that provider configuration in a
    cooling state, one Redis key
    (`video_studio:provider_cooling:<provider_id>`), until the wait it asked
    for (`MAX_RETRY_AFTER_SECONDS` at most) or a backoff doubling with each
    refusal in a row (`FIRST_BACKOFF_SECONDS` to `MAX_BACKOFF_SECONDS`, a
    streak remembered `STREAK_SECONDS`); a job the provider creates clears
    it. A refusal read as a quota (`VideoProviderBusyError.quota`) cools
    the model that refused alone (Veo counts a quota per model), on
    `video_studio:provider_cooling:<provider_id>:<model>`; a plain 429 the
    whole provider. Every reader (`cooling_of(provider_id, model=...)`,
    `coolings([(provider_id, model)])`) asks for the model it would use and
    meets either key; a job taken clears both. "In a row" counts refusal
    EVENTS (`next_cooling`): a refusal while the cooling lasts is the
    same event - a batch refused together -, keeps the count and only moves
    the end further, to a wait the provider names; only a refusal after the
    cooling ended, inside the streak, doubles the backoff. Every write is a
    compare-and-set on the key (`_CAS_SCRIPT`, a Lua `eval`), read and
    computed again when another worker wrote meanwhile, so two workers never
    shorten a cooling or count one refusal twice. The shot is held for the cooling's end (`JobOutcome("hold")` as
    above, `quota` when it is one), its row `pending` with `provider_state`
    `cooling`, the stage naming the provider (and the model, for a quota)
    and the reason and
    `provider_eta_at` the end of the cooling (`jobs.job_state`: `queued`
    with `held`; `describe`: "waiting in the queue: <provider> (<model>):
    quota exceeded, retry after ..."). Before asking, a run reads the cooling and
    holds the shot the same way, so the rest of a batch is never asked;
    `start_clip` / `restart_clip` queue a shot on a cooling provider for
    when it cools (`jobs.enqueue(not_before=...)`, the deadline counted from
    then), and `generate_video_clip` and `get_video_jobs` do not wait for a
    shot held for its provider. `video_studio_capabilities` lists each
    model's `cooling`, `validate_only` its `provider_state`. Redis
    unreachable: no cooling is known and the 429 is held as above.
  - A 5xx or no answer may have started a paid job the answer was lost for:
    the `video_clip` job retries after its back-off (three attempts; the
    dead letter writes the last error on the shot). It is an outage, never
    a cooling.

  Once the job exists upstream, a
  retry would pay for a second video: a transient failure while polling (a
  429, a 5xx, a network error - `httpx.TransportError`, a timeout - on either
  provider) is asked again in place (`video.TransientFailures`: a time budget,
  `TRANSIENT_BUDGET_SECONDS` and at least `MIN_TRANSIENT_ATTEMPTS`, the wait
  doubling up to `MAX_BACKOFF_SECONDS` or the provider's own wait, reset
  by a good answer), the download - read inside the guarded block, an empty
  body refused - up to `DOWNLOAD_ATTEMPTS` times with the same back-off, and
  running out is a plain `VideoGenerationError`, never the busy one. No
  provider here exposes how much of its quota is left (no rate-limit
  headers on these APIs, no quota endpoint on Gemini's), so
  `video_studio_capabilities` reports none; a waiting shot's state is what
  says a limit was met.
- **Never paid twice.** `generate_video(..., on_job, resume_job)`:
  the provider hands the upstream job id (an OpenAI video id, a Veo operation
  name) to `on_job` the moment the job exists, and `runners.run_clip` stores
  it (`provider_job_id`) at once. A run that dies after that - a worker
  restart, an unexpected error `clips.generate` lets propagate to the job's
  retry - resumes the SAME job (`resume_job`) instead of creating one; a job
  the provider no longer knows says so. The video is stored (`file_id`)
  before the probe and the poster, so a failure there only redoes what
  followed, never the generation. `on_state` hears the provider's own state
  of that job at every poll (`VideoJobState`), which the runner keeps
  on the row (`provider_state`, `provider_eta_at`); a busy refusal carries its
  HTTP status (`VideoProviderBusyError.status_code`), so the row says
  `rate_limited` rather than `unavailable` while the job waits for its retry.
  A shot's `error` is shown
  everywhere it appears: the preview (`preview_state` `failed` with the
  reason; a deleted or expired shot says so), the timeline, the inspector,
  the media list, the tools' results.
- **Generated again in place.** `clips.restart_clip` (`POST
  /clips/{id}/retry`, `generate_video_clip(clip_id=...)`) re-queues a shot on
  the SAME row - failed, cancelled, expired, or ready but wrong; only
  one still being made is refused - every scene using it keeps it, the
  stored job reset (and the media, unless a ready take is kept: below) -
  with an optional new prompt, model, length,
  format (`aspect_ratio`), resolution, `sound`, `text_check`,
  `content_check`, `accept_minor_drift`, reference images, start frame or
  `screen` (`clips.RETRY_FIELDS`; what the call leaves out stays as the
  shot was asked; a stored `screen` is checked again whenever the shape or
  the start frame changes, its quad being in the start picture's pixels). `plan_restart`
  decides the shot as it will be asked without changing the row, and
  `restart_clip` applies it under the lock. The start frame
  (`_retry_start`): the `reference_*` arguments (`clips.START_FIELDS`) name a
  new start, resolved as `start_clip` resolves one; a moment, a step or
  `crop` alone moves the frame of the video the shot started from (its kind
  and id in `params.reference_video`), cut anew; the very start the shot
  has, named again as it was asked (`_same_start`), keeps its kept frame
  and is no new start - nor a silence, so it is never dropped for reference
  images; `start_frame: false` drops
  the kept one (`reference_file_id` / `reference_video` cleared, the kept
  frame file left in the conversation, never among `replaced_media`);
  `start_frame: true` keeps it beside reference images. A call that gives
  `reference_image_file_ids` for a model that takes them but no start frame
  beside them (`reference_images_with_start_frame` false), says nothing of
  `start_frame` and names no start, drops the start frame by itself and
  says so (`ClipPlan.notes`: "Start frame dropped: <model> takes reference
  images alone", in the tool's answer). The way back:
  `start_frame: true` with a `reference_*` and no
  `reference_image_file_ids` asks the start frame alone and drops the kept
  reference images (`clips._drops_references`, "Reference images dropped"),
  as `reference_image_file_ids: []` does. Every plan names its mode
  (`clips.request_mode`: `start_frame`, `reference_images`,
  `start_frame_and_reference_images`, `prompt_only`), said in the answer
  (`clips.mode_note`). A drop sends the reference images it
  keeps, the same files: nothing is asked or paid for again. A READY shot
  keeps playing its take (`clips.KEPT_TAKE`): its video, poster, size
  and length stay on the row's columns, its frames and notes wait in
  `params.kept_take` (with the `prompt`, `model`, `made_at` that made it),
  and `clips.playable` - the one rule the render (`render._ready_clip`), the
  preview (`preview._clip`) and the quality check read - plays it while the
  row is `pending` / `running`. `run_clip` keeps the attempt's media beside
  it (`params.attempt_media`: the video once downloaded, so a later run takes
  it and never asks again; its poster and size) and its frames in
  `params.frame_qc`; once the new take is ready and passed its checks it
  goes on the columns and `clips.retire_kept_take` deletes the old take,
  its frames and the attempts that failed beside it. An attempt that ends
  without a ready take - refused, drifted, screen lost, cancelled
  (`jobs.cancel`, the worker's cancel), given up on (`runners.dead_letter`:
  the engine's, or a hold past its bound) - goes through `clips.end_attempt`: the row is READY
  again on the kept take, `error` empty, the attempt appended to
  `params.attempts` (`status`, `reason`, `at`, the video, poster and frames
  it made, to look at), the take's frames and notes put back; nothing is
  generated again. `jobs.job_state` then answers `failed` - that job did -
  with `previous_take_plays` and the reason "Last regeneration failed:
  <reason>, the previous take plays" (`clips.regeneration_reason`); the
  tool's answer adds `failed_attempt` (its reason, video and frames),
  `serialize_clip` a `regeneration` (`running` / `failed`), the quality
  check an advisory instead of a refused scene. The attempt keeps its size
  and length too, so it can be promoted: `generate_video_clip(clip_id,
  accept_attempt: <its file_id>)` (`clips.promote_attempt`, audited as
  `video_clip` update) makes a failed attempt, or the take of a failed
  shot, the shot's take after review, nothing generated - only a take that
  failed its text check, and only when every finding of it lies on the
  screen its screen insert covers (`clips.attempt_findings_outside`); the
  take that played moves to `params.replaced_media`, the invented words
  under the insert leave the record (`status: accepted`, `under_insert`)
  and `params.accepted_attempt` keeps the promotion. **The text check
  never reads the screen an insert covers** (#10056): for a shot carrying
  a screen insert other than a screen replacement,
  `insert_keep_out.covered_box` (the screen measured on the take, else on
  its start frame) is painted flat grey on the start picture and on every
  frame before the reading (`text_drift.blank_region`); the record carries
  `covered_by_insert`, the take a note "covered by the insert", and
  `insert_keep_out.key_advisory` advises `key: null` when the keyed screen
  shows something not of the key colour. `discard: true`
  (`clips.RETRY_FIELDS`, `ClipRetry.discard`) drops the take at once
  instead: the files of a take it replaces (a discarded ready shot's, or a
  drift-failed one's, with the attempts kept beside it) move to
  `params.replaced_media`: `run_clip` deletes them once the new generation
  ends, made or failed, and the retention counts them as the shot's
  (`clips.side_files`, `clips.owned_files`: the expiry, the purge, the
  clip delete) should a run die first. Each generation runs on its own execution
  (`jobs.enqueue(generation=n)` suffixes the dedup key), the previous error
  kept in `params.previous_error`. The row is locked (`SELECT ... FOR
  UPDATE`) before its status is read, so the editor and the assistant asking
  at once start one generation. The cap is a soft limit per prompt and start
  frame (`clips.count_regeneration`): a shot is generated again at
  most `clips.MAX_RETRIES` (5) times from the same prompt (its spacing
  aside) and the same start frame, counted in `params.regenerations`
  (`{key, count}`, `clips.regeneration_key`); a new prompt or a new start
  frame starts the count again, whatever `params.attempt` says (that one
  keeps numbering every paid generation, for the executions). A retry asking
  only another model or another `max_camera_motion`
  (`clips.UNCOUNTED_CHANGES`, `counts_toward_cap`) - the worker's fallback to
  the next model (`runners._fall_back`) included - is never counted and never
  refused; what a retry says of its context (the video it is for, `discard`)
  counts for nothing.
- **Which models are video models**: `router.is_video_model`, the catalog's
  `video_generation` mode first (Sora, Veo, Runway Gen-3a / Gen-4), else the
  family prefix; `is_chat_capable_model` refuses them. LiteLLM also reads its
  proxy's `/model/info` modes, the gateway its own `type`.
- **Which provider and model**: `router.get_video_provider` - the caller's
  choice (a model from `video_studio_capabilities`), else
  `PlatformSettings.video_provider_id` / `video_model` (Settings > AI models >
  Video generation, `/settings/available-video-models`, migration
  `v5774vgen03`), else the first provider with a built-in model (a chatless
  one - Kling AI, Runway, Seedance - first); never one that excludes the
  person. A model named without its provider is asked of the
  provider that lists it (`router._video_listings` / `_offering`: the
  listings `video_studio_capabilities` shows, read together), never of the
  one chosen in Settings - `kling-v3` named on a shot generated again used to
  reach the Gemini endpoint; a model several providers list is asked of the
  video's own provider for it - the one its shots of that model are made on
  (`clips.video_providers`), and for a shot generated again its own first
  (`clips.OWN_SHOT_CHOICE`) -, else of the platform's chosen provider
  (`router._shared_model_provider`; `veo-3.1-generate-001` on Vertex
  AI and ElevenLabs was refused on b56997a2, Vertex being the video's), the
  answer's notes naming which and why; refused until `provider_id` names one
  only when neither lists it. One no provider lists goes only to a provider
  that makes videos and lists nothing (or whose listing failed: a
  deployment it does not list), the Settings one first, else is refused.
  `declared_video_reference_limits` reads a named model's limits on the same
  provider. A deleted provider's choice is cleared
  with the other platform pairs.

`clips.start_clip` is the one start of a shot - the agent tool and the editor's
`POST /clips` alike: `clips.plan_clip` resolves the model through it, checks
the reference images and the first frame (`clips.resolve_reference`) and gives
the params - the art direction of the video it is made for among them
(below) -, then `clips.queue_clip` stores the row and enqueues the job (the
agent tool calls the two itself, so its answer says the plan's notes, as a
regeneration's does); the
`video_clip` job resolves again when it runs (the person may have lost the
provider meanwhile). A call refused on its arguments - any `ClipError` of the
plan, a prompt missing, `start_frame` or `discard` without `clip_id`, a
`clip_id` naming no shot of the person's - reached no provider: the tool
answers it as a validation note, `{status: invalid_parameters, clip_id,
reason, queued: false}` with `is_error` unset (and `no_op` set), so the agent
loop's repeated-failure brake never counts it. `generate_video_clip(validate_only: true)` answers
`plan_clip` / `plan_restart` (`ClipPlan.summary`: `{validated, clip_id, model,
provider, provider_id, prompt, duration_seconds, aspect_ratio, resolution,
negative_prompt, sound, mode, start_frame, reference_image_file_ids,
ingredients, ingredients_path, min_reference_similarity, max_camera_motion,
keep_regions, stray_screens, stabilise, fallback_models, art_direction,
sent_prompt, sent_negative_prompt, screen, previous_take, provider_state,
notes}` - the `sent_*` pair is what the worker will send, the art direction
applied and the ingredients named, `clips._sent`; `previous_take` is `kept`
or `discarded` for a ready shot generated again; `provider_state` is its
provider's cooling, read from `provider_cooling`) and stops there: no
row added, locked or queued, nothing paid.

**Art direction.** A brand's look reaches its AI shots through code,
never through the prompt the author writes (invariant 16). It steers and
informs, it never refuses a shot or limits how many a video has: a video may
be AI shots, demos and animated pictures alone.

- **Which direction.** `clips.resolve_direction`: `freestyle` none; a
  `template_id` the person may use (a shot asked before its video exists); a
  `video_id`'s template (none for a free-style video); neither - or a video
  whose template the person can no longer use - the platform's default video
  template, the one a video naming none wears. The editor's
  `POST /clips` passes the video the shot joins, an animated still the template
  its video wears (`projects.start_animations`). `templates.shot_direction`
  is the snapshot the shot keeps (`params.direction`: the template's id, name,
  `style`, `avoid`; a template without `shots` is recorded with neither, so a
  regeneration reads the same template) or `params.freestyle`. A shot
  generated again (`restart_clip`, `POST /clips/{id}/retry` with `video_id`,
  `template_id` or `freestyle` - `RETRY_FIELDS`) takes the video or template the call names, else reads its
  template again (a brand update reaches the new take), else keeps its
  snapshot when the person can no longer use that template.
- **What the model is sent.** `clips.shot_prompts` through
  `templates.direct_shot`, by the worker (`runners.run_clip`) and by the plans
  (`ClipPlan.sent`, what `validate_only` shows): the `style` is appended to the
  prompt of a shot drawn from text - a shot that starts from a picture or a
  frame keeps that picture's look -, and the `avoid` terms join the negative
  prompt of a model that takes one (`templates.merge_negative`: after the
  author's, each once). They are never written into a prompt: a text-to-video
  model renders what its prompt names, so a model without a negative prompt is
  steered by the style alone and gets the author's negative prompt as before.
  Each provider declares the field per model
  (`BaseLLMProvider.takes_negative_prompt`, read through
  `clips.takes_negative_prompt` only when there is a direction to apply):
  `False` by default - the OpenAI Videos API, Seedance -, `True` for Veo on
  Gemini and every Kling route, and for Runway and ElevenLabs what their own
  request builder does with the model (Runway sends `negativePrompt` only for
  a model its table says takes one, ElevenLabs sends `negative_prompt` for a
  model its table does not know). The shot's own `prompt` stays the author's.
- **Read on the frames, never a failure.** The content check takes the
  template's terms apart from the author's (`content_drift.check(avoided=...)`):
  they are read on the frames even when the prompt names no subject, never cut
  by the author's `MAX_EXCLUDED`, and a shot showing one is kept with a note
  (`ContentCheck.avoided_seen`, `avoided_note`) - a template's taste is no
  reason to throw away a paid shot. A term of the author's negative prompt
  shown still fails it as content drift (below).
- **Said at save, as advice.** Only for a template with a direction,
  `projects.shot_uses` lists the shots the video plays - clip scenes and
  statement backdrops - made from text under a template's direction, and
  `templates.shot_notes` names those made under another template's, with the
  call that makes one again in place should its look clash. A shot made
  before shots had a direction, in free style or from a picture is the
  author's choice and is never named; nothing is a render gate.
- **Built-ins.** Every built-in video template declares a direction: a
  photographic style in its brand's tones (`_shot_style`) and the stock
  imagery of a generated "cyber" picture as `avoid` (`_AI_CLICHES` - visual
  cliches only, never text or a logo, which a shot made from a real screenshot
  carries). Their guidance treats AI shots, demos and animated pictures as a
  complete film. `tests/test_video_studio_shot_direction.py` pins that every
  built-in has one and that no built-in description or guidance asks for what
  its shots avoid.

**Once made**, `runners.run_clip`:

- **Silent shots.** `sound: false` (`VideoRequest.sound`) asks a model that
  makes its own sound for none (Kling AI drops `sound`, Seedance and Runway
  send `generate_audio` / `audio` false; the Gemini API takes no audio
  switch), and `_silenced` drops any track the shot still came with
  (`encoder.without_sound`: the picture copied, `-an`) before it is stored.
  An animated still is always asked silent.
- **Reference images.** `reference_image_file_ids` (1 to
  `clips.MAX_REFERENCE_IMAGES`, 3) are pictures whose subjects the shot keeps
  - the same character, animal, garment, product or logo in every shot -
  beside the prompt (`VideoRequest.reference_images`), not its first frame.
  Each provider declares what a model takes (`max_reference_images`,
  `reference_images_with_start_frame`, served per model by
  `video_studio_capabilities` through `router.video_reference_limits`): Veo
  3.1 and Veo 3.1 Fast up to three asset images (`referenceImages`), at 8 s
  only and never with a start image; Kling 3.0 Omni (`kling-3.0-omni`, its
  own route, `POST /omni-video/kling-3.0-omni` read through `GET /tasks`) up
  to three `refer_image` beside a `first_frame`; none on the OpenAI Videos
  API, Runway's video routes, Seedance or ElevenLabs. `clips.resolve_reference_images`
  checks them before anything is paid - readable pictures a provider reads
  (`check_start_picture`), a model that takes that many (refused with its
  name otherwise), with a start frame only where the model takes both
  (`clips.check_reference_combination`, before any file is read; the
  refusal names the model and the two ways out, as the call's own
  arguments) - and `start_clip` keeps them on the row
  (`params.reference_images`). A start frame asked with reference images is
  checked first against what the model DECLARES, before a row is locked or
  the model resolved (`clips.check_restart`, `plan_clip`, through
  `router.declared_video_reference_limits` - the model the provider named or
  the Settings choice would be asked of, read with `_video_model_on`, the
  rule `get_video_provider` picks it by, and no lifecycle call), so a batch
  of retries the model cannot take fails at once; the resolved model is
  checked again.
- **The shape of a reference image.** A provider may refuse a
  reference picture on its shape before anything is made (Kling 3.0 Omni:
  "Image aspect ratio is invalid"). Each provider declares what a model takes
  (`BaseLLMProvider.reference_image_limits`, a `video.ReferenceImageLimits`:
  width / height range, smallest and largest side, formats, count; `None` by
  default). `video_studio_capabilities` lists it per model as
  `reference_constraints` (`router.video_reference_listing`), `unverified`
  where the provider documents none (nothing refused on our side then).
  `clips.check_reference_shape` measures each reference image, and each
  ingredient sent on the native path, as it is sent (an SVG rasterised first,
  `check_start_picture` returning the picture), in `validate_only` and before
  the row is queued: outside the limits the call answers `invalid_parameters`
  naming the file id, its size and ratio, the accepted range and the fix.
  `reference_fit: "pad"` (kept on the row as `params.reference_fit`) pads it
  on transparent to the nearest accepted ratio, every pixel kept, scaled to
  the smallest side (`clips.fit_reference`), and the answer says so; the
  worker fits the pictures again before the provider is asked
  (`clips.fit_reference_pictures`). A provider refusing the request on its
  arguments (HTTP 4xx before a job existed: `video.VideoRequestRefusedError`,
  `ClipError.unpaid`) and a picture refused at the worker give back the try
  the round counted (`clips.uncount_regeneration`, once per counted round),
  so the per-prompt allowance the next `validate_only` shows is unchanged.
  Every generation reads the same files (`clips.reference_pictures`, through
  `start_picture`), so a shot generated again keeps its subjects and pays for
  no new picture; `restart_clip` keeps them, checks them against the model
  it is generated with now, and replaces them when the call names others
  (`[]` drops them). One deleted since fails the shot.
- **Ingredients.** `ingredients: [{file_id, role: logo | product |
  character, label?}]` (1 to 3, `clips.parse_ingredients`) are reference
  images with what they are, beside the start frame. Their path is decided
  from the provider's own declarations (`clips.ingredient_path`): the
  model's own mechanism (`native`: Veo 3.1 `referenceImages`, Kling 3.0 Omni
  `refer_image`, Runway `seedance2` `references`) when it takes them all with
  the shot's start frame, else the prompt (`prompt`: `run_clip` appends
  "Keep <label> exactly as in the reference image." per ingredient,
  `ingredient_prompt`, to the request only -   the shot keeps its prompt as
  asked). The prompt path points at the start frame, so a shot without one
  is refused by the model's name. With a start frame and a `logo` among them
  the path is `composited` (`clips.PATH_COMPOSITED`): named in the
  prompt, and each logo drawn back onto the made take with its own pixels
  (see "Logos drawn back" below); a logo ingredient may give its `quad` (0-1
  across and down the start frame), another role may not. `params.ingredients` and
  `params.ingredients_path` record them (a native one is also
  `params.reference_images`, what the adapters read); `restart_clip` keeps
  them and decides the path again for the model it is generated with now;
  the shot's notes and the tool's answer (`ingredients_path`) say which.
  `reference_image_file_ids` stays as the form without a role, native only.
  Runway's `seedance2` declares references without a literal first frame
  (its reference mode and a positioned `promptImage` exclude each other).
- **A logo on a subject the start frame does not show
  (`tracked_logos.py`).** A logo ingredient may name its subject instead of
  its place: `ingredients: [{file_id, role: logo, subject: {file_id, label?}}]`
  (`clips.Subject`, `Ingredient.tracked`; a picture of the subject wearing the
  logo - a conversation file, a frame saved from another shot - and what to
  call it, "the cow in the navy blanket"; `quad` and `subject` exclude each
  other, a `subject` on another role is refused, and a shot's logos all name
  a subject or none). The path is then `tracked` (`clips.PATH_TRACKED`,
  `ingredient_path(tracked=True)`) on a model that takes no reference images,
  start frame or not - the only ingredients a shot may have without a start
  frame (`clips.needs_start_frame`: an ingredient the prompt points at still
  needs one; the refusal names `ingredients[].subject` as a way out) - and
  `native` on a model that takes the pictures, the subject kept for the check
  below. The prompt names the other ingredients and never the tracked logo
  (`ingredient_prompt`); the subject's picture is one of the shot's
  `source_refs`. Once the take is stored, `runners._logos_tracked` runs
  `tracked_logos.composite` as the take step `tracked`: (1) the take is
  sampled every `DETECT_EVERY_SECONDS` (at most `MAX_KEYFRAMES`, the first and
  last among them, each named by its index, `keyframes`) and the person's chat
  model is shown the subject's picture - the logo's place on it outlined when
  `logos.locate` finds it (`reference_picture`) - beside the sampled frames in
  ONE call (`detect`, `detector(user_id)`), asked for every instance of the
  subject in each frame with the four corners of the surface the logo sits on;
  the code bounds the answer (`sightings_of`: a surface in a line, off the
  frame or under `MIN_SURFACE_SHARE` is none, corners put in drawn order, two
  sightings overlapping by `SAME_INSTANCE_IOU` one instance; an unusable
  answer is `DetectionUnavailableError`, the job's retry); (2) between sampled
  frames each instance's surface is followed by a `PlanarTracker` on the
  surface and `REGION_GROW` of what it is on, the frame's `TRACK_PLANES`
  candidates judged by the share of points on the surface itself agreeing
  (`_Instance.step`, at least `MIN_CONFIDENCE`: the card, never the table
  behind it), an instance first seen at a sampled frame followed backward
  through the frames since the one before (`_backfill`); at a sampled frame a
  sighting overlapping a followed surface by `MATCH_IOU` confirms it, under
  `KEEP_IOU` re-seats the track on the sighting, an instance the detector no
  longer sees ends, a sighting matching nothing starts one (`_follow`); each
  instance's quads are averaged over `SMOOTH_FRAMES` with a window that stays
  centred at a run's ends (`smoothed`: a moving subject is never drawn behind
  itself), a run under `MIN_RUN_FRAMES` dropped as a flicker; (3) in every
  frame each instance gets the logo once - two instances overlapping by
  `SAME_INSTANCE_IOU` in a frame are one subject (`_distinct`) - fitted inside
  its surface with its shape kept (`screen_insert.warp(..., "contain")`) under
  the light the model drew there (`screens.lit`, the ONE lighting match, on
  the surface's broad light only, `_broad_light`: the strokes of the model's
  own redraw never print through), then read back as drawn (`logos._verify`,
  a miss re-raised as `SurfaceLostError`). Failures carry their frame like a
  lost logo (`runners._lost_logo_reason(what=...)`, `frame_qc.LOST_FRAME`):
  `ingredient subject not found` (`SubjectNotFoundError`: in no sampled frame,
  or followed for fewer than `MIN_RUN_FRAMES` consecutive frames anywhere)
  and `ingredient surface lost at t=<s> s (frame <n>)` (`SurfaceLostError`:
  the track lost while the next sampled frame still shows the subject within
  `REACQUIRE_SHARE` of the frame's diagonal; a surface leaving the frame -
  its centre out, fewer than `MIN_CORNERS_INSIDE` corners in - ends its
  instance instead). The record (`Tracked.record`, `frame_qc.LOGO_TRACKED`:
  `{status: drawn, frames, drawn_frames, share, instances, keyframes_sampled,
  keyframes_seen, labels, subjects, fell_back_from?}`, carried with the
  stabilisation) is served as `logo_tracked` by `serialize_clip` and the
  tool's answer beside the first / mid / last frames. **The native check.** A
  model sent the logo as a reference image has its take checked first
  (`clips.native_logos_with_subject`, `tracked_logos.native_check`:
  `logos.locate` of the asset on `NATIVE_CHECK_FRAMES` sampled frames at
  `NATIVE_CHECK_SIDE`); a logo found as given in none of them fails the check,
  and the tracked path draws it onto the subject on that very take, nothing
  generated again - `params.ingredients_path` becomes `tracked`, the record
  says `fell_back_from: native`, a note says why. The duplicate gate
  (`logos.find_duplicates`) does not read a tracked take: several instances
  legitimately carry the logo, one each.
- **Frame QC.** Once stored, a shot's first, middle and last frames
  are cut (`frame_qc.cut_frames`, at most 1280 px) and stored as files of
  the shot (`params.frame_qc`: `first_frame_file_id`, `mid_frame_file_id`,
  `last_frame_file_id`), served by `serialize_clip`, the media token assets
  `first_frame` / `mid_frame` / `last_frame` (the inspector's `The shot's
  frames`) and `generate_video_clip`'s answer - there as files of the
  conversation (`_frames_named`, the copies `_file_refs(show=False)`
  makes, linked and never attached), so `read_file_content` opens them, on
  a made shot and on a failed one alike -, and measured in code:
  `reference_ssim` (the first frame against the start picture, the picture
  cut to the frame's shape around its centre as a provider fits it),
  `motion_ssim` (the last frame against the first) - SSIM of grey copies
  `SSIM_SIDE` long, Gaussian window, Wang et al.'s constants - and, with the
  text check, `reference_text_match` (the share of the start picture's
  words the first frame reads, from the text check's own transcription:
  `TextCheck.texts`). `min_reference_similarity` (0-1, needs a start frame,
  `clips.reference_similarity_threshold`) fails a take under it as
  "drifted from reference (ssim 0.41)" (`frame_qc.drift_reason`) before the
  paid checks, its video and frames kept on the row; `restart_clip` moves
  the frames to `replaced_media` with a failed take, or beside a ready take
  it keeps (`kept_take.frame_qc`). `max_camera_motion` (0-1, 1 drops
  it, `clips.camera_motion_tolerance`, stored as `params.max_camera_motion`)
  fails a take whose last frame changed more than that from its first
  (`frame_qc.camera_motion`: `1 - motion_ssim`) as "camera moved: ..."
  (`frame_qc.motion_reason`), its video kept: a camera move is
  measured, never read off a still frame. At or under
  `clips.LOCKED_CAMERA_MOTION` (0.5) it asks a locked-off camera: a
  model whose provider says it does not hold one, with or without a start
  frame (`BaseLLMProvider.holds_locked_camera`, `None` unknown by default:
  Kling AI's models hold it, Gemini's Veo only from a start frame), is
  replaced on a new shot whose model the platform picked by the first model
  `video_studio_capabilities` lists whose provider says it does
  (`clips.locked_camera`, a note saying so); a model the caller named, or a
  shot generated again on its own model, is kept and the answer says it will
  not hold the camera and what would (a start frame, the model that does),
  before the take is paid for - `validate_only`'s answer among them, in its
  `notes`. A take that passes records the
  thresholds it was checked under with its frames
  (`frame_qc.CHECKED_WITH`: those applied only); the record moves with the
  take (`kept_take.frame_qc` while an attempt runs beside it), so the quality
  check judges the take that plays by its own tolerance
  (`quality.content_facts`), never by one a later `generate_video_clip` call
  stored on the shot - a regeneration asked with `max_camera_motion` that
  failed leaves no camera finding on the take still playing. Advisory otherwise: frames
  that cannot be cut leave the shot without them (a note only when a
  threshold could not be applied). The frames outlive the media's expiry
  like the poster and go with the row (`retention`, the clip delete).
  Each frame, and the poster, says what it is in its file's `description`
  (`frame_qc.caption`: which frame, of which shot, at what second, its size
  and what it measures), carried onto the conversation's copy
  (`_file_refs`, `_shared.store_description`): `read_file_content`'s
  look-once rule answers a second read in the turn with it instead of
  refusing. The scene preview's frames, its MP4, the start frames
  (`clips.save_frame`) and the frames kept as pictures carry one too, and a
  file with no text to read answers `read_file_content` with its notes.
- **Cross-scene consistency of a recurring prop or character
  (`reference_drift.py`).** A branded jacket on the hero drifted across nine
  scenes (a collar, light-blue piping for teal) and every shot passed: the
  content check reads a shot against its own prompt and start picture, never
  against the video's reference for that prop. A video now keeps
  `references: [{id, file_id, label, kind prop|character, description?}]`
  (`spec.VideoReference`, at most `MAX_REFERENCES`; a crop of the prop from
  the scene that got it right, `save_video_frame` with a region) and a clip
  scene names those its AI shot must match (`ClipScene.consistency`, at most
  `MAX_CONSISTENCY`; only with a `clip_id`). `VideoSpec._references_named`
  refuses a reference id used twice and a `consistency` naming no reference
  of the video, naming it and the video's references (F63); `referenced_ids`
  adds the reference files, so `verify_references` checks them and the
  retention keeps them; `spec.references_of` / `consistency_of` are the
  stored spec's readers. The save-time check (`quality.content_facts`) reads
  each held shot's kept frames (`frame_qc` first, middle, last) against each
  reference (`reference_drift.drifts_of`), measured once per take and
  reference (`render_cache`, `CACHE_KIND`, `MEASURE_VERSION` 2): the
  reference is found in the frame by a patch-embedding sliding window
  (`candidates`: every window at `FIND_SCALES` widths and `FIND_ASPECTS`
  shapes described from integral tables by its colour histogram - hue bands
  at two chroma levels, neutral dark and light apart - its edge-orientation
  layout and its lightness layout, `Fields`; the window nearest the
  reference's description is the find, robust to the prop's angle and scale,
  which the first locator - `logos.locate`, a pixel-wise correlation of a
  flat logo - was not: on real footage it never found a textured rug on an
  animal seen from another angle and the fallback read the whole frame, so
  every shot drifted at structure 0.04-0.19, dev:reference-drift-real-footage).
  A best match under `MIN_LOCATE` (0.74) is no find: the reference is not
  seen in the frame as it shows it, said as an advisory, never a drift (F66;
  calibrated on the Normandy film's frames: 0.746-0.807 where the rug shows,
  0.641-0.722 where it does not). Found, two measures on the crop, in code:
  colour, the main signal, the farthest an accent of the reference (a peak
  of its hue histogram at one of `ACCENT_LEVELS` chroma levels, `accents`;
  the navy body and a grey floor are neutral, what is read is the piping, the
  emblem, the bandana) stands from the crop's nearest in CIELAB a*, b*, the
  crop's accent brought to the reference's chroma first (a paler shade under
  another light is the same colour, F61; L* left out) - over `MAX_DELTA_E`
  (14): a teal piping redrawn light blue, a collar the reference has none of,
  drift (F60, F65: the same rug stays under 10, recoloured it reaches 18-21);
  and structure, the cosine of the edge-orientation layouts (`structure`,
  `STRUCTURE_CELLS`) under `MIN_SIMILARITY` (0.55), a coarse guard since the
  prop is seen from another angle than the reference (0.74-0.86 on the same
  rug, about 0 on a flat crop). Among the windows within `LOCATE_TIE` of the
  best, the one nearest in colour is read: a prop seen twice is held to its
  best showing. A found frame under either is a `Drift`
  (`ContentFacts.reference_drifts`, `clip id -> [Drift]`); a reference seen
  in none of the frames is a `Drift` with `found` false. `content_findings`
  raises `reference_drift` on the scene: dense, "reference drift: the branded
  rug differs from the video's reference `rug` (first, last frames: colour dE
  19 over 14) - crop: the first frame (file `...`) at x 0.31-0.52, y
  0.40-0.71; generate it again in place ..."; advisory when not seen,
  "reference not seen: the branded rug is not seen in the shot as the video's
  reference `rug` shows it (first, middle, last frames: not seen as the
  reference shows it (match 0.66 under 0.74)) - crop: ... is the nearest;
  check the shot by eye, crop the reference on the prop as this shot frames
  it, or drop `rug` ..."; `QualityFinding.data` carrying the reference, the
  measures (each with its `match`) and the crop's box, `found`
  (`Drift.as_dict`). A shot naming no reference is never read and gets no
  finding (F62). `QUALITY_REPORT_VERSION` 24. In `generate_video_clip`,
  `consistency: [ids]` with `video_id` (`clips.resolve_consistency`, before
  anything is paid: unknown ids refused by name) is kept as
  `params.consistency {video_id, references, path}`: the references' pictures
  go as the model's reference images when it takes them all beside the start
  frame (`native`: `keep_consistency` adds them to `reference_images`), else
  `consistency_prompt` appends "Keep <label> exactly as in the start picture:
  <description>." per reference (`prompt`; without a start frame the
  description alone); the answer's `consistency {path, references}` and the
  notes (`consistency_note`) say which. `run_clip` cuts the take's frames
  again and reads them against the references before the take is accepted
  (`runners._reference_drifts`, `drifts_from_frames`): a take that drifted
  (`failures`: the found drifts) fails as "reference drift: ..."
  (`take_reason`), its video kept, the next fallback model tried; a reference
  seen in none of the take's frames is a note on the take, never a failure; a
  check that cannot read lets the take through with a note (the save reads it
  again). `plan_restart` keeps a shot's references
  and decides their path for the model it is generated with now
  (`consistency: []` drops them). `test_video_studio_reference_drift_6782.py`
  (F60-F63, synthetic pictures; F64-F66, real footage:
  `tests/fixtures/video_studio`, two 640-wide frames of the booth scenes with
  the rug seen from the front and from behind, the rug's crop from another
  shot as the reference, the same frame with the piping recoloured light
  blue, and a product-screen insert without the prop).
- **Stabilised before it is measured (`stabilise.py`).** A take whose
  camera only drifted is locked off in code before its frames are cut: when
  the shot says `stabilise: true`, or says nothing and has a
  `max_camera_motion` or a prompt asking for a locked camera
  (`clips.wants_stabilising`; the camera read by
  `content_drift.prompt_camera`, the content check's own prompt reading made
  first and kept with the shot, never a phrase list - a shot skipping the
  content check is read for none). `stabilise.estimate` decodes the take at
  most `ESTIMATE_SIDE` long and follows features of the whole frame with a
  `tracking.PlanarTracker` from frame 0 (one homography per frame against
  frame 0, RANSAC leaving out what moves in the scene); `crop_window` finds the
  largest window of the frame's shape inside every frame warped back onto
  frame 0 (its centre on a grid around the frame's, its size by bisection),
  one that holds the shot's own screen whole when it has one (its
  `screen`'s quad on the first frame, `screens.start_quad`, passed as `keep`;
  no such window leaves the take un-stabilised, "locking it would crop into
  its screen"); each frame is drawn through its homography and the window in one Pillow
  `PERSPECTIVE` transform and re-encoded (`encoder.transform_frames`). The
  stabilised take replaces the raw one only when it keeps
  `MIN_KEPT_SHARE` of the frame and moves at least `MIN_GAIN` less (its
  `1 - SSIM` of first and last frames, measured on the small frames);
  otherwise - fewer than `MIN_INLIERS` points agreeing on a frame (the frame
  is named), no drift past `MIN_DRIFT`, a crop too large, no gain - the raw
  take stays the take. The record (`applied`, `reason`, `raw_motion`,
  `residual_motion`, `max_drift`, `kept_share`, `crop`, `lost_at`, and
  `maps`: each frame's map from a 0-1 point of the stabilised take to the
  same point of the raw one, eight coefficients to seven significant digits,
  `stabilise.normalised_maps`) is kept in
  the take's frames (`frame_qc.STABILISATION`) with the un-stabilised take as
  a file of the take (`frame_qc.UNSTABILISED`): it moves, is kept and is
  deleted with the take's frames (`frame_qc.frame_file_ids`), and expires
  with the videos (`clips.side_files(videos_only=True)`). `serialize_clip`
  and the tool serve it as `stabilisation` (the file said kept, never named,
  the maps left to the render),
  the shot's notes say it (`stabilise.note`), the first frame is compared
  with the part of its start picture the crop kept (`stabilise.cropped`), and
  a take still past its tolerance fails as `camera moved` saying what
  stabilisation did (`frame_qc.stabilised_clause`).
- **Logos drawn back (`logos.py`).** On the `composited` path each logo
  ingredient is found in the start frame - at its `quad`, else by normalised
  cross-correlation at `FIND_SCALES` sizes, refined (`logos.locate`): of its
  colours on its own opaque pixels (what it is printed on left out; a size
  with fewer than `FIND_MIN_PIXELS` of them skipped), at least `MIN_MATCH`
  (0.85: a textured frame without it reaches about 0.75, the logo itself
  about 0.97 on synthetic footage), or for a logo of one flat colour, which
  has none to correlate, by its shape drawn over a neutral grey, at least
  `MIN_SHAPE_MATCH`; a place under that is no logo, since a logo drawn in
  the wrong place is worse than one only named -, followed from the start frame
  through every frame by a `tracking.PlanarTracker` on the logo and
  `REGION_GROW` of what it is printed on (a first frame the model reframed
  has the logo looked for again there; a point may stand
  `LOCAL_INLIER_PIXELS` off the plane), and drawn in every frame over the
  model's redraw: onto its quad (`screen_insert.warp`, premultiplied edges)
  on a flat surface, or - on a curved one, a mug - on its local
  warp, what the homography leaves of the points followed interpolated by a
  smoothed thin-plate spline over a `MESH_CELLS` mesh of the logo
  (`logos._local_warp`, none when every residual is under `FLAT_RESIDUAL`)
  and the logo drawn cell by cell on it (`logos._mesh_layer`). **One place a
  frame.** The region followed can hold two planes - a mug and the
  table behind it - so each frame proposes up to `TRACK_PLANES` candidates
  (`PlanarTracker.propose`: each homography fitted on the points the ones
  before left out; `step` is `propose` + `adopt` of the first, unchanged for
  the screen inserts and the stabilisation). A candidate's confidence is the
  share of the points followed on the logo (its quad grown by `LOGO_REACH`,
  read where they were in the frame the tracker started from) that agree
  with it (`logos._confidence`; under `MIN_LOGO_POINTS` of them, the share of
  every point): a static table behind a mug that moves holds none of them.
  Of the candidates at or above `MIN_CONFIDENCE`, the most confident is
  drawn, and of those within `CONFIDENCE_TIE` of it the one whose corners
  moved least from the quad drawn the frame before (`logos._choose`); only
  that one is adopted, so the thin-plate warp is fitted on its points alone,
  and a mesh whose cells fold over each other (`logos._folds`: one turned
  the other way or under `MIN_CELL_SHARE` of a cell) is dropped for the flat
  quad - a folded mesh draws pieces of the logo several times. On the take's
  first frame (the model's drawing against the start picture) the candidate
  most points agree on is taken. Two copies of one ingredient at the same
  place are drawn once (`logos._drawn_already`). **Turning away and resting.
  ** How far the surface turned from the view axis is read off the
  quad's foreshortening against the logo's shape in the start frame
  (`logos._turned`). Past `AWAY_DEGREES` (80) the logo fades out over
  `FADE_FRAMES` (3: 2/3, 1/3, then nothing, `logos._toward` in whole steps);
  so does a surface turned past `TURNING_DEGREES` (55) that keeps under
  `LOGO_POINTS_LEFT` of the points the tracker found on the logo itself in
  its reference frame (`PlanarTracker.original`: a point refilled later sits
  where the homography put the region, which on a surface that rolls is no
  longer where it was - the rest of the surface rolled into view, the edge
  of the logo past 80 degrees) or that the track loses; a fade with no
  candidate goes on along the last move. Faded out, the logo is drawn
  nowhere, its tracker dropped, and looked for again every `SEARCH_EVERY`
  frames over the whole frame from its pixels in the start frame
  (`logos._search`: at `SEARCH_SHRINK` of the frame, `SEARCH_SCALES` of its
  start size, `SEARCH_TURNS_WHOLE`, from `MIN_MATCH`), fading back in where
  it is found; the same search, around its last place, recovers a facing
  logo the track lost before the take fails. An object at rest keeps the
  quad (and mesh) of its first stable frame - `REST_FRAMES` quads within
  `REST_PIXELS` of it - until a quad moves `UNLOCK_PIXELS` from it
  (`logos._settled`), each frame at its own opacity. A logo not
  found stays named in the prompt, with a note. A logo facing the camera is
  located in every frame, never bridged: a frame where the track does not find its
  surface (a hand in front, the model cutting away, a blur) fails the take as
  `logo lost: <label> at t=<s> s (frame <n>) of the shot: <why>`; and the
  composited take, encoded, is read back (`logos._verify`): in every frame
  the pixels at the logo's place keep at least `MIN_SSIM` (0.7) of it as
  drawn there (grey, at most `VERIFY_SIDE` long, weighted by its alpha,
  `logos.masked_ssim`), else the same failure. Either way the frame goes with
  it (`LogoLostError.frame_jpeg`), stored as a file of the take
  (`frame_qc.LOST_FRAME`, one of `frame_qc.frame_file_ids`) and named by the
  failure (`(frame: file <id>)`, best effort: a frame that cannot be stored
  leaves the failure without it), its raw take kept on the row like a lost
  screen; any other error goes to the job's retry,
  which resumes from the take stored (nothing generated again), so a
  garbled logo is never kept because drawing it back failed once. **The
  duplicate gate.** The composited take read back once more: every
  `DUPLICATE_SAMPLE_EVERY` frames each logo is template-matched over the
  whole frame at the size its track drew it at in that frame, or the
  nearest frame it drew it (`logos.matches`: the size brought to about
  `DUPLICATE_LOGO_SIDE` pixels, its colours from `MIN_MATCH` or a flat
  logo's shape from `MIN_SHAPE_MATCH`, as `locate` finds it, places kept
  apart by non-maximum suppression); a frame with more than one place
  matching is `logo duplicated at t=<s> s (frame <n>) of the shot: ...`
  (`logos.find_duplicates`, `Duplicate`). The take is not failed: its check
  is recorded with its frames (`frame_qc.LOGO_CHECK`: `{status: passed |
  duplicated, frames_sampled, label, frame, seconds, count, boxes,
  frame_file_id}`, carried with the stabilisation when the frames are cut
  again, `frame_qc.CARRIED`), the frame stored as a file of the take
  (`runners._record_logo_check`, one of `frame_qc.frame_file_ids`), and
  served as the shot's `logo_check`; every render playing the take - a
  scene preview or the whole video - reads it (`render._read_references`,
  through `clips.take_params`: the take that plays) and fails the scene with
  a `logo_duplicated` finding (`logos.duplicate_findings`): the take's
  second placed in the scene's time through its time map (a frame the scene
  does not play, or outside a preview's window, is not that render's), the
  frame stored again as a file of the render. A failure like the audio's
  (`render_checks.PICTURE_KINDS`).
- **A logo drawn into its surface, and measured so
  (`logo_integration.py`).** A logo drawn back on the `composited` path, or
  onto its subject on the `tracked` path, is the brand's own pixels; drawn
  flat over a surface that is lit, textured and moving (a quilted rug on a
  walking animal, a jacket) it reads as a sticker pasted on the frame. Both
  paths therefore draw it, on its warp (the tracked homography, its mesh on
  a curved surface), into the surface: it takes the surface's broad light
  (`LIGHT` of it, read over `LIGHT_CELLS` cells, `screens.lit`) and its fine
  relief - the luma above a blur of `RELIEF_SIGMA`, relative to the light,
  `TEXTURE` of it printed through the lettering and a relief past
  `RELIEF_CLIP` (a stroke of the model's own redraw) faded out
  (`logo_integration.integrated`, `blend`). Then it is measured
  (`logo_integration.Probe`), one frame in `SAMPLE_EVERY`: *shading*, how
  much the light varies across the logo as drawn against across the surface
  around it (a surface lit flatter than `FLAT_SURFACE` says nothing; a score
  of 1 from `SHADING_TARGET` of it); *follow*, the surface's own points under
  and around the logo followed over up to `WINDOW` sampled frames against
  where the logo's drawing sent them (a logo missing more than `MAX_MISSED`
  of the surface's motion moves rigidly while the surface bends; fewer than
  `MIN_POINTS` points or a motion under `MIN_MOTION` says nothing); *size*,
  the change of those points' extent against the logo's (past `SIZE_CHANGE`).
  Each measure is read at its `ROBUST_SHARE` quantile over the frames; the
  integration score is the worst of the three, and under `MIN_SCORE` (0.5)
  the logo is not integrated at the worst frame of the measure that failed.
  The logo check records what it measured, never a status alone
  (`frame_qc.LOGO_CHECK`: `presence`, `similarity`, `integration` with its
  score, measures, frame and `frame_file_id`; status `not_integrated`), the
  tracked path's record carries the same `integration`, and
  `frame_qc.logo_gate` reads the least integrated of the two. Every render
  playing the take - a scene preview or the whole video - fails the scene
  with a `logo_not_integrated` finding (`logos.integration_findings`, a
  picture kind like `logo_duplicated`) naming the shot, the frame in the
  scene's time and one fix: regenerate with the model's native reference
  mechanism, or accept it with `ingredients_quality: "flat"`. The video's
  save quality check files it as `dense` (a render refused without
  `force`), `advisory` when the shot was asked flat
  (`quality._logo_integration_words`, `QUALITY_REPORT_VERSION` 30).
  `generate_video_clip` takes `ingredients_quality` (`integrated`, the
  default, or `flat`; anything else refused, `clips` validation) and its
  answer gives the `ingredients_path` and the take's `integration_score`.
- **What was drawn into the take is logged.** The screen, the kept
  regions, the logos, the stabilisation and the cleaned stray screens,
  in that order, each store a new take (`run_clip`'s `swap_take`)
  and log the step with the file it made (`params.take_steps`, in the same
  write, beside the step's own marker such as `screen.replaced_file_id`): a
  run that dies after a step resumes past it (`runners._steps_done`), never
  drawing a screen, a region or a logo twice, even after a later step
  replaced the take its marker names. A kept region the camera loses
  (`kept region lost`) and stray screens that could not be cleaned fail the
  take through `take_failed`, so a fallback model is tried as for any frame
  check.
- **Another model when a take fails its checks - only when the call
  names it.** No fallback is ever set by default: the seeded skill
  and the tool's description no longer ask the agent for one, and a call
  naming none has none - a regeneration (`clip_id`) never inherits the chain
  an earlier call named (`plan_restart` drops `params.fallback_models`
  unless the call passes them; only the worker's own continuation of a
  chain, `tried`, keeps it). On b56997a2 a chain named once fired
  `veo-3.1-fast-generate-001` on ElevenLabs twice, after a Vertex drift
  failure, on a provider out of credits. Nor does the platform swap a model
  by itself: a locked-off camera the model does not hold is said, with the
  model that would hold it (`clips.locked_camera`), never asked of it; a
  shot whose model is gone is refused, never handed to the platform's
  (`plan_restart`); the platform's chosen video model unusable - retired,
  not served, its provider removed - is refused with its reason
  (`router.get_video_provider`), never replaced by the next live one.
  `fallback_models`
  (1-3 models, `clips.resolve_fallbacks`: each resolved for the person and
  checked against the shot as asked - its reference images, its
  ingredients - before anything is paid; the shot's own model named among
  them is no fallback and is dropped, the others kept and the plan's notes
  saying so - `fallback X`, or `no fallback` when nothing is left -, while a
  new shot asking `[]` is refused; kept as
  `params.fallback_models` `{model, provider_id, provider}`, replaced or
  dropped by a retry, whose `[]` drops them) are tried in order when a made take fails a check of
  its frames: drifted from reference, camera moved, text drift, content
  drift (stray screens not cleaned included), screen lost, kept region lost,
  logo lost (`run_clip`'s `take_failed`). In one
  transaction on the locked row, `runners._fall_back` ends the attempt as any
  failed one (`clips.end_attempt`: its media on the row, or beside a kept
  take) and generates the shot again in place with the next model through
  `clips.restart_clip(tried=...)` - the shot as it was asked, its path and
  checks decided again for that model, the chain of models whose take
  failed kept in `params.fallback_tried` (a regeneration asked by hand
  starts a new one) - so no reader sees the shot failed in between and no
  wake fires. A model left to try that cannot take the shot any more ends
  the chain with a note; past the last, the take fails as before with every
  model in the chain. `clips.fallback_state` (`serialize_clip`'s
  `fallback`: `models`, `tried`, `made_by`, `fell_back`) and
  `clips.fallback_note` say which model made the take kept; the tool's
  answer carries `model` and `fallback`.
- **A model its provider no longer serves (`app/llm/video_served.py`).**
  A provider's listing is not always what it serves: Kling AI, Runway,
  Seedance and ElevenLabs list a catalog of their own once the key reads the
  account, Gemini its built-in video model when its listing fails. What the
  provider answers about a model is kept: a probe that makes no video
  (`BaseLLMProvider.probe_video_model`; Gemini's reads its listing, else
  `models.get`, since a model it shut down keeps a record but leaves the
  listing; the default raises `NotImplementedError`) or a generation
  answering HTTP 404 for it
  (`video.status_error` -> `VideoModelNotServedError` on the request that
  creates the video, never on a job polled, `about_job`; `clips.generate`
  -> `ClipError.not_served`, recorded by `run_clip`) records it as not
  served on that provider configuration - one Redis key per configuration
  and model for `RECORD_SECONDS` (24 h), a probe's answer kept in the process
  for `PROBE_SECONDS`, cleared once the provider creates a job for the model.
  `router._video_model_unusable` (not served, then retired) is the one check
  of every resolution in `get_video_provider` (`VideoModelUnusableError`,
  `not_served`), `_offering` leaves out a provider that does not serve a model
  it lists, and `router.video_model_listing` is what `video_studio_capabilities`
  shows: only served models, the others under `ai_shots.not_served` with
  `reason`. Settings > Video generation and the editor's media panel list the
  same served models (`/settings/available-video-models`). A shot naming such a model, or a fallback naming one, is refused
  at `validate_only` and on the real call before anything is paid
  (`fallback_models: fallback B is not served by PROVIDER (HTTP 404: ...)`);
  a fallback found unserved at its turn ends the chain with its note, the
  primary's take kept on the row.
- **Content drift.** A shot with a prompt is read against it by
  `content_drift.check`, after the text check, as the person
  (`get_llm_for_user`): the subjects the prompt names (living beings,
  people, characters, the objects the shot is about, each with its kind)
  are read from the prompt alone, kept with the shot for the same prompt
  (`params.content_subjects`, keyed by the prompt's digest), with which terms
  of the negative prompt are about the camera moving (`camera_terms`:
  only terms the negative prompt has, `parse_camera_terms`) - never sent to
  the frames reading, three still frames cannot show a camera move; the
  measured motion answers them (`max_camera_motion`); a prompt naming
  none - a landscape, an abstract motion - with nothing excluded and nothing
  its template avoids reads no frame. Otherwise its first, middle and last
  frames go to the chat model in ONE call with the subjects, the terms of the
  negative prompt (split in code, `excluded_terms`, at most `MAX_EXCLUDED`)
  and what the template's art direction avoids, asking what each
  frame shows: each subject there or not, of the kind named or not, what took
  its place, its deformities, and which of those terms show.
  `content_drift.drifted` decides in code: a subject never shown, shown as
  another kind, replaced or deformed, or a term of the negative prompt shown,
  is a finding; a subject out of one frame and in another left the frame; a
  term only the template avoids is a note (`terms_seen`, `avoided_note`),
  never a finding. A plural subject described by variants ("five
  cats, each a different colour: ginger, grey, white, black, tabby") is one
  subject of several (`Subject.several`: its `count` when the prompt says
  it, its `variants` as listed) - read so by the prompt reading, and grouped
  so in code when that reading lists its variants one by one, subjects of
  one kind told apart only by their names (`content_drift.grouped`, a
  reading kept before included) -, told to the frames reading with its count
  and variants and asked how many of it each frame shows and whether two
  look the same: it is several distinct subjects of its kind, none
  identical - two identical, or one alone on every frame counted, is
  `major`; a variant not shown is never a finding, and the negative prompt
  or a subject of another kind fail as before. Each finding has a severity, decided in code too:
  a subject missing, of another kind or replaced is `major`; a deformity or
  an excluded term is `minor` only when the model observed it is not
  noticeable at a glance on a 1080p screen (`noticeable: false`), `major`
  otherwise (a bare string without that judgement included). A shot fails
  on its failing findings (`ContentCheck.failing`: the major ones, every one
  when the shot was asked `accept_minor_drift: false`, stored as
  `params.accept_minor_drift`) with `ContentCheck.reason` ("content drift:
  ...", each finding with its severity), its video and poster kept on the
  row; minor findings accepted are a note ("Minor content drift accepted
  ..."). A check that cannot run leaves the shot ready with a note;
  `content_check: false` (`start_clip`, `restart_clip`, the tool,
  `POST /clips`) skips it - an animated still with the default prompt is
  never read.
- **What the start picture already shows (issue #10053).** A shot made
  from a picture opens on it: a negative term that picture already shows
  (a raised hand under "waving") used to fail every take on its first
  frame. `clips.lint_start_shown` (in `plan_clip` and `plan_restart`, so
  `validate_only` answers it, nothing paid) has the person's chat model
  read the stored start picture against the negative prompt
  (`content_drift.read_start_picture`), keeps the terms it shows in
  `params.start_shows_excluded` (`{digest, terms}`, the digest of the start
  file and the terms: a new picture or negative prompt is read again) and
  says them once ("The start picture already shows: waving - judged from
  the middle frame on ..."; `validate_only`'s `start_shows_excluded`). A
  frame cut from a video, stored only at generation, is read by the worker
  on the picture in hand (`content_drift.start_shown`). The take's frames
  reading is told the first image is that picture and to list such a term
  on a later image only where it is new or grows; `content_drift.drifted`
  drops it from the first frame (`start_shown`). A start reading that
  fails keeps nothing: the take is judged on every frame as before.
- **A term the takes keep drawing.** Each take's content check
  records the negative prompt's terms it failed on (`Finding.excluded`,
  `content_drift.excluded_shown`; `[]` for a take that passed), under the
  take's attempt, the last `clips.INSIST_AFTER`, 2, kept in
  `params.excluded_shown` (`{attempt, terms}`). A shot generated again - by
  hand or by its fallback chain - whose two takes right before it both
  showed a term still in its negative prompt (`clips.insistent_terms`; a take
  never checked - refused, failed an earlier check - breaks the run; at most
  `MAX_INSISTED_TERMS`, 3, terms of at most `MAX_INSISTED_TERM_CHARS`, so the
  prompt's own words always stay whole) records it in `params.insisted` with
  the model it is asked of (`plan_restart`): the take is asked with "No X
  anywhere in the shot." at the end of its prompt and X out of its negative
  prompt (`clips.insisted_prompts`, in `runners.run_clip` and in what
  `validate_only` shows as sent), and, where the model has a guidance setting
  (the provider's `video_guidance(model)`, `(lowest, default)`; no video model
  the platform serves has one today - Kling documents `cfg_scale` for its 1.x
  models only), with its guidance lowered halfway to its lowest, from the
  last lowering of that same model only and within its range
  (`clips.lowered_guidance`, `VideoRequest.guidance`), so the term's mention
  is not drawn harder. The answer and the made take's notes say it
  (`clips.insisted_note_of`, among the notes a new take is made with). The
  content check still holds the take to the shot's own negative prompt, the
  term included.
- **A screen insert's screen kept clear.** Video b56997a2: four of
  six takes of a locked-off shot drew a head, hands or a cow in front of the
  TV against the negative prompt, each paid and failed after the fact.
  `insert_keep_out` owns the rule. A shot carrying a screen insert
  (`clips.carries_insert`) whose screen is known on its start frame - a
  screen replacement's quad (`screens.start_quad`), else the start
  picture's measured screen (`params.surface.quad`, 0-1;
  `insert_keep_out.quad_of`) - has its prompt sent end with a keep-out
  sentence built from the rectangle the quad spans, widened to whole
  hundredths (`rectangle`, `line`: "nothing enters the rectangle x0-x1,
  y0-y1 of the frame"), within `MAX_PROMPT_CHARS` (`with_line`, applied by
  `clips.provider_prompts` - the one composition `runners.run_clip` and
  `validate_only`'s `sent_prompt` share: ingredients, flat screen, keep-out,
  insisted terms). On a model whose provider declares reference images
  beside a start frame with room for one more
  (`router.video_reference_limits`; Gemini Omni, never Veo, which refuses
  `referenceImages` with `image`), the start picture is sent a second time as
  a reference image (`start_as_reference`); the plan's notes say which
  (`insert_keep_out.note`). The take as made, before a screenshot is drawn
  into it and only when its content check runs, is read frame by frame at
  most `READ_SIDE` a side (`measure_take`): the key its screen shows on the
  first frame (a screen replacement's key, the start screen's hue, then
  every candidate; `screen_insert.KeySpec.sampled`), its matte
  (`screens.key_matte`), what stands in front (`screen_insert._foreground`)
  and its share of the quad, and the mask's confidence
  (`screen_insert.key_confidence`), lowest over the frames it covers
  `MIN_READ_SHARE` of the quad on - kept as `frame_qc.FOREGROUND_OVERLAP`
  (`{frames, key, max_share, frame, at, confidence, frames_in_front,
  take_file_id}`, carried with the frames, served as `foreground_overlap`). A
  screen showing no key leaves the matte off (`key: null`). The frames
  reading is told the rectangle (`content_drift.check(keep_out=...)`) and
  says of each excluded term seen whether it shows only where it enters it
  (`Finding.keep_out`, false unless every entry of the frame says so).
  `insert_keep_out.judge` holds those terms to the measure: matted, under
  `MAX_SHARE` (10%) of the quad and the mask surer than `MIN_CONFIDENCE`
  (0.7), the take is kept with a note ("Kept: ... the insert is drawn under
  it") and the term is not recorded as shown; otherwise the take
  fails as content drift, the measure and why it fell short in its reason.
  Any other finding fails as before, the measure said with it; a take that
  passes says the measure in its notes. Every content-drift failure ends
  with `clips.attempt_clause`: the take's attempt (`params.attempt` + 1) and
  how many of the `MAX_RETRIES` regenerations of its prompt and start frame
  remain (`params.regenerations`).
- **A screen insert's screen occluded, said early
  (dev:screen-occlusion-check).** Video b56997a2: a subject crossing the keyed
  screen was caught only by the render's `insert_through_foreground` gate, 10
  to 20 minutes into the render. The take's measure (`Overlap`) also keeps
  the first frame something covers `MIN_READ_SHARE` of the quad on
  (`first_frame`, `first_at`), the box it covers there (`first_box`, 0-1 of
  the frame, `x0, y0, x1, y1`), the frame rate read (`fps`), the share of
  occluded frames (`occluded_share`) and `trim_end`: one frame before the
  first occluded frame, `null` when it is occluded from frame 0 (nothing to
  keep: regenerate). The shot's notes say it (`occlusion_note`, "Occlusion:
  ... set trim_end to .. s"). At save, `quality.content_findings` files an
  advisory `insert_occluded` on each clip scene carrying a screen insert
  over the shot (`ShotFacts.occlusion`, read from the playing take's
  `foreground_overlap`), with the frame, the box and the trim in its `data`,
  unless the scene's own `trim_end` already ends it before the first
  occluded frame (`QUALITY_REPORT_VERSION` 27). A measure kept by an earlier
  release reads back without these fields and says nothing.
  `test_video_studio_screen_occlusion.py` (F71-F74).
- **Stray screens.** Veo 3.1 draws monitors showing an interface in
  an office "without screens or readable text" about half the time, and each
  such take failed as content drift. `stray_screens` (`generate_video_clip`,
  `POST /clips`, the retry; `clips.resolve_stray_screens`: `fail` by
  default, `flatten`, or `{file_id, fit, light}` - a screenshot the person
  can read; it needs a negative prompt and the content check) is stored as
  `params.stray_screens`. Its frames reading is asked to locate
  (`content_drift.check(locate_screens=True)`, `observe(locate=True)`): for
  each excluded term seen, the screens it shows on - each display's four
  corners in shares of the frame - and whether it shows `elsewhere`; a term
  shown only on screens that are convex quads of the frame
  (`stray_screens.parse_corners`) carries them (`Finding.screens`), a term
  listed once per screen in a frame carrying all of them - one entry not
  saying `elsewhere: false` leaves it uncleanable. When every
  failing finding does (`ContentCheck.cleanable`), `runners._screens_cleaned`
  runs `stray_screens.clean`: the camera measured from the first frame
  (`motion.align`, the screens left out), each sighting taken to every frame
  by that motion from the read frame nearest in time (sightings of one screen
  grouped where their quads overlap, `SAME_SCREEN_IOU`), its sides refined on
  each frame (`stray_screens.refine`: the strongest edges across each side,
  near where it is expected) and smoothed; then every frame redrawn
  (`stray_screens.paint`): a screen switched off keeping the room's soft
  light (`flatten`), or the screenshot warped in and lit by the light the
  model drew. A camera not followed, or a screen whose content changes
  between frames - something passing in front of it (`occluded`) - raises
  `CleanupError`: the take fails as before, saying why, never drawn over.
  A frame is judged on what it shows of the screen: a pan carrying
  the monitor out of the frame is compared on what is left of it, never on
  the black around it, a frame showing less than `MIN_SHOWN_SHARE` of it is
  not judged, and a screen judged in fewer than `MIN_JUDGED_FRAMES` frames
  is refused saying it leaves the frame.
  The cleaned take replaces the raw one (`params.stray_screens.cleaned_file_id`,
  each read frame's cleaned screens in `cleaned`, so a resumed run never
  cleans twice), its frames are cut again and it is read again: a term still
  read on a cleaned screen (a blank monitor is still a screen) is accepted
  and said (`ContentCheck.failing_after`), anything else fails as
  "content drift ... (after its stray screens were cleaned)". A
  `screen_insert` overlay could not do this: its quad is static on the
  footage, the shot's own camera moves.
- **Screen replacement.** A shot with `screen` (`screens.py`: `file_id` of
  the real screenshot, `quad` - the screen's four corners in the start
  picture's pixels -, `fit`, `light`) shows a real product screen whose text
  is the screenshot's own. `clips.resolve_screen` checks it before anything
  is paid (`start_clip`, `restart_clip` with a new `screen` or another
  `aspect_ratio`): a start image (`reference_file_id`, never a video frame)
  of the shot's shape, a convex quad inside it, a screenshot the person can
  read; it picks the flat colour (`screens.pick_key`: of green, blue and
  magenta, the one the picture outside the screen holds least) and stores
  `params.screen` with the start picture's size. A `screen` without `quad`
  has it found in the start picture (`screens.find_screen`): for each
  key, the largest connected regions of its hue band (`_surfaces`, three per
  key, so a green plant larger than a green screen hides nothing;
  `screens.hue_band` - a key's own hue within `FIND_HUE_DEGREES`, joined for
  green to 90-150 degrees -, saturation past 0.35 and value past 0.3, never
  a distance to a pure key colour: plate 00de87ae's flat green TV, shaded
  off the key, was "no flat green surface"), each read
  again around its own median colour with the tracker's `key_distance`; the
  outline's four diagonal extremes are the coarse corners `fit_corners`
  refines, and only a convex quad of at least `FIND_MIN_SHARE` (2%) of the
  picture whose
  intersection over union with its surface reaches `FIND_MIN_FILL` (0.9) is a
  screen - the largest wins, a pixel past its edge, read on a copy at most
  `FIND_SIDE` (1280) long; none is a `ClipError` naming both ways out (give
  the quad, or a picture whose screen is one solid key colour). The quad is
  stored with `found: true` (served by `ClipPlan.summary`'s `screen`, so
  `validate_only` shows it before anything is paid), and `clips.screen_again`
  drops it when the shot is generated again from another start picture, so
  it is found there anew; a quad the caller gave is kept. The worker sends the start
  picture with the quad filled with that colour (`screens.placeholder`, in
  `clips.start_frame`) and asks the provider for that surface kept flat
  (`clips.with_screen`, on top of the art direction and the ingredients, so
  the `sent_prompt` a `validate_only` answer shows is the one sent; the
  prompt stored, read against the frames and shown is the author's). Once the raw take is
  stored, `runners._screen_replaced` reads it twice (`encoder.iter_rgb_frames`):
  `screens.Tracker` follows the flat colour near where it was (a
  chromaticity distance shading leaves unchanged, the largest component, its
  outline's four edges fitted to lines and intersected, so a hand across an
  edge or a corner out of frame moves nothing), `screens.judge` refuses a
  surface missing from the first frame, from more than
  `1 - MIN_FOUND_SHARE` of the frames or for longer than `MAX_LOST_SECONDS`
  ("screen lost: ...", the raw take kept on the row like a drifted shot),
  `screens.smooth` fills and averages the corners, and
  `encoder.transform_frames` re-encodes every frame through
  `screens.composite`: the screenshot warped into the corners
  (`pictures.perspective_coefficients`), under what stands in front of the
  screen (the key's soft matte), the key's colour taken out of what shows
  around it, and the shading the model drew on the flat surface (its luma
  relative to the surface's median, smoothed) laid over it by `light`. The
  composite replaces the raw take on the row (its id kept as
  `params.screen.replaced_file_id`, so a resumed run never draws twice)
  before the frames are kept; their `reference_ssim` is measured against the
  start picture with the screenshot drawn in (`screens.expected_start`), and
  the text check is skipped: the screen is the screenshot.
  **Carried to the bezel, measured on every frame.** The quad the
  tracker fits is the inner flat area, and the darker key the model shades
  along the bezel stayed as a line around the screenshot. Each frame's
  corners are pushed outward over the pixels still the key by chroma
  distance (`screens.bezel_corners`, `screens._bezel_of`: each edge sampled
  along its length, the run of keyed pixels just outside it counted outward
  up to `BEZEL_REACH`, 3 % of the quad's width, the edge moved by the median
  run - a hand across it moves nothing - and the corners where the moved
  edges meet), smoothed over time like the corners (`smooth`), and the
  screenshot is drawn into them; inside, every pixel the chroma calls the
  key (`key_distance` under `KEY_OUT`) is the key whole, feathered outward
  by `BEZEL_FEATHER` (1.5 px of 1080p) so the key's share melts into the
  bezel instead of leaving a rim; the matte is pooled over `MATTE_WINDOW`
  (5) frames on the surface's own plane (`screens.MatteSmoother`, the
  median of the last frames, read on the quad's plane so a camera move
  does not smear it). What was drawn is measured on every frame
  (`screens.Measure`): the key colour left in the band `SPILL_BAND` (3 % of
  the quad's width) just outside the drawn quad - its own edge pixels left
  out, they blend both sides however the quad is rasterised
  (`screens.residual_key_share`, `band_mask`) -, over `MAX_SPILL_SHARE`
  (0.2 %) of the band a `KeySpillError` ("key spill: ..."); how much of the
  matte as drawn holds from the frame before (`screens.edge_hold`: one less
  the share of its pixels that toggled back over the last two frames - the
  same measure as the insert's), under `MIN_EDGE_HOLD` (0.98) a
  `KeyFlickerError` ("key flicker: ..."); and how far the screenshot's
  content stands past the bezel (`screens.content_overflow`: the content
  box's corners through the drawing homography), past `CONTENT_OVERFLOW_PX`
  a `ScreenContentCroppedError` ("screen content cropped: ..."). All three
  are `ScreenLostError`s: the take fails like a lost screen, its raw take
  kept, the next fallback model tried. **The screenshot's own fit.** `fit`
  defaults to `contain` (`DEFAULT_FIT`; `cover` cut product UIs - a
  sidebar, a right column): the screenshot is cut to its content box
  (`pictures.content_box`: its flat margins of its own background trimmed)
  and letterboxed in its own edge colours (`pictures.letterboxed`,
  `pictures.edge_colours`: each band the colour of the edge it continues,
  never a black card; `screens.content_fitted`); `cover` is refused by
  `clips.resolve_screen` before anything is paid when it would cut more
  than `MAX_CONTENT_CUT` of the content (`screens.check_content_fit`,
  `pictures.cover_cuts`). What the replacement drew travels with the take
  (`Replaced.record` into `params.screen`: `picture_size` as fitted,
  `content`, `checks`), and every render playing it judges the screenshot's
  pixels per pixel of the screen at its own size (`insert_soft`, above).
- **Text drift.** A shot made from a picture (`reference_file_id` or
  `reference_video`, unless `text_check: false` or the shot has a `screen`) is read against that
  picture by `text_drift.check`: its first and last frames
  (`encoder.extract_still`) and the start picture go to the person's chat
  model (`get_llm_for_user`, as the person `bind_llm_caller` bound) in ONE call asking
  for a verbatim transcription as JSON - word by word, each with how surely
  its letters read (`confidence`, 0-1), the height of its letters as a share
  of the picture and its box (`text_drift.Word`; an older answer shape,
  one text per picture, still reads at confidence 1 with no size) - and the
  verdict is decided in code (`text_drift.verdict`). **The expected words are
  what the start picture itself reads clearly:** its words read at or
  above `MIN_CONFIDENCE` and at least `MIN_HEIGHT_PX` tall in its pixels
  (`readable_words`, each picture's height read off its bytes,
  `picture_height`); the others - a flag on a pole, a distant banner, signage
  at an angle, read as garbage on the picture and as other garbage on the take
  - are recorded as unreadable in the start picture (`Excluded`, with their
  confidence) and never compared. `drifts_of` decides against that set
  alone: a frame word that is not one of the expected words yet is within
  `MAX_EDIT_RATIO` edits of one (Levenshtein, words of `MIN_WORD_LENGTH`
  letters or more) is that word misdrawn - a word cut by the frame's edge or
  run into another (a substring either way), missing, or new is not, and a
  frame word under `MIN_HEIGHT_PX` in the frame is not compared either
  (lettering that small, garbled or not, is the unreadable count's). The
  model also counts, per picture, the places drawn as text whose letters
  cannot be read (`unreadable`); with the words read under the thresholds
  added to each picture's count (`illegible_counts`: the start picture's own
  soft lettering is the baseline), a frame with more of them than the start
  picture, when the picture reads words, garbled its text
  (`unreadable_frames`: small text an animation melts - a screen in a
  photo, a badge - reads as no word at all, never as a near miss). **New text
  is a check of its own:** a frame word readable by the same
  thresholds that is no word of the start picture at any confidence, no near
  miss and no part of one, is hallucinated lettering (`new_words`,
  `NewText`), reported in the notes (`TextCheck.new_text_note`) and the
  record, never a failure - that is what drift usually looks like. The
  record (`TextCheck.record`, `frame_qc.TEXT_CHECK`: `{status: passed |
  failed | skipped, words_checked, excluded: [{word, confidence}],
  hallucinated: [{word, frame}], failure: {word, seen, frame, box,
  crop_file_id}}`) is written by `runners._record_text_check` with the final
  verdict and served as `text_check`; a failure's word is cropped out of the
  frame it was read in at its box, grown by half its height
  (`runners._crop_word`), stored as a file of the take (one of
  `frame_qc.frame_file_ids`) and named by the failure (`(crop: file <id>)`),
  best effort. The frame QC's `reference_text_match` reads the expected words
  alone (`TextCheck.expected_text`). A drift
  on a start picture that reads words is rescued once before it fails:
  the lettering is located on the start picture
  (`text_drift.locate_lettering`, the same chat model asked for each block's
  box, as the stray-screen cleanup has screens located), the boxes bounded
  in code (`lettering_boxes`: grown by `LETTERING_MARGIN` of their height,
  inside the picture, merged to at most `spec.MAX_KEPT_REGIONS`, one as
  large as the picture left out), drawn back from the start picture into
  every frame with the kept regions' own mechanism (`kept_regions.keep`: the
  regions masked out of the camera's measure, then composited back where
  the camera takes them) as the take step `lettering`
  (`params.lettering_kept`, its `drawn_file_id`), its frames kept again and
  the take read again by the same check. Only a take that still reads wrong
  fails - with `TextCheck.reason` ("text drift: ... reads ..."), saying it
  was after the lettering was drawn back -, as one whose lettering could not
  be located or followed (`RegionLostError`) does, its video and poster kept
  on the row for the person to see. Nothing new is paid for: the take the
  provider made is the one drawn over. A ready shot generated again keeps
  playing its take when the attempt fails this way (`previous_take_plays`),
  the attempt's video and frames beside it; a run resumed on a rescued take
  never rescues it twice (`_TAKE_STEPS`). A check that cannot
  run (no chat model, an unusable answer, the start picture gone) leaves the
  shot ready with a note. An animated image (`animate`) is checked like any
  shot from a picture, a template asset's included; one that failed plays its
  still, which the quality check says.
- **Invented lettering inside a generated shot (dev:invented-text-check,
  `text_drift.Invented`).** The text check compared a take's words with the
  start picture's and reported the rest as new text, never failed: three
  feature pills and two card subtitles in gibberish passed a branded booth.
  The check now reads three frames (`FRAMES`: first, middle, last;
  `frame_times`, the middle placed by the take's duration) and asks the
  reading, per word, whether it `known`s it as a word of a language, a name,
  a brand, an acronym or a number (`Word.known`; an older answer leaves it
  unsaid, nothing invented). `invented_words` decides in code: a word the
  reading does not know, that is no brand term of the lexicon (`brand_terms`,
  `lexicon.brand_lexicon`: each entry and each word of it), no number, no
  word of the start picture at any confidence - nor part or near miss of one
  (that is the drift check's) - and whose letters stand `MIN_INVENTED_HEIGHT_PX`
  (10) or taller at 1080p (`Word.tall_at_1080p`) is `Invented` (word, frame,
  box), once per word and frame; the take is never failed for it
  (`TextCheck.invented`, `invented_note`, the record's `invented: [{word,
  frame, box, crop_file_id}]`), `runners._record_text_check` crops each one
  out of its frame as it does a failed word. The video's quality check reads
  them back off the playing take (`text_drift.invented_of`, `ShotFacts.invented`
  / `text_check`) and files `invented_text` (`QUALITY_REPORT_VERSION` 23) on
  every scene showing the shot: dense, the crop named, without a `clip_id`
  so no passing preview clears it (the lettering does not move with the
  camera) - a render is refused -, advisory when the shot was asked with
  `text_check: false` (F67, F70); the same pill reading a brand term of the
  lexicon, a number or a word of the plate is no finding (F68). Before
  anything is paid, `clips.lint_lettering`
  (`plan_clip`, `plan_restart`: a shot made from a picture with its text
  check on) has the start picture read alone (`text_drift.read_start_picture`,
  `start_lettering`): gibberish already in the plate is refused naming its
  box ("The start picture shows unreadable lettering at x .., y ..: ..."),
  unless a `keep_regions` box covers it (`_covered`: drawn back from the
  picture); what it reads is kept in `params.start_lettering`
  (`START_LETTERING_KEY`, `StartLettering`), and a picture or a reading that
  cannot be had leaves the shot asked as before (F69). The prompt sent
  (`provider_prompts` -> `with_lettering`) ends with "Every printed word
  stays exactly: ..." built from those words (`lettering_line`, at most
  `MAX_LETTERING_WORDS`, within `MAX_PROMPT_CHARS`) and the negative prompt
  names new lettering (`NEGATIVE_LETTERING`, `with_negative_lettering`), as
  the keep-out line does for a screen. The unit suite does not read the
  plate (`conftest._no_start_picture_reading`).
  `test_video_studio_invented_text.py` (F67-F70).
- **Kept regions.** A team photo holding a banner always failed the
  text check once animated: the model melts the banner's letters.
  `keep_regions` (`generate_video_clip`, `POST /clips`, the retry,
  `animate.keep` on an image scene; boxes `[x, y, width, height]` in shares of
  the start picture, at most `spec.MAX_KEPT_REGIONS`, checked by
  `spec.kept_boxes`; it needs a start frame, `clips.kept_regions_asked`) is
  stored as `params.keep_regions.boxes`. Once the take is stored (after a
  screen replacement, before any check reads it), `runners._regions_kept`
  runs `kept_regions.keep`: the still cut to the frame's shape as a provider
  fits it (`frame_qc.cut_box`, the boxes taken through the same cut,
  `frame_rects`), the camera measured from it to every frame (`motion.align`,
  the regions left out of the measure, each frame started from the last one
  found, then `motion.smooth`), and in every frame the still's pixels of each
  region drawn back where that motion takes them, with a soft edge and the
  frame's exposure (a gain and an offset per channel fitted on the band
  around the regions). A still not found in the first frame, in too few
  frames or for too long (`kept_regions.judge`) fails the take as "kept
  region lost", its video kept. The drawn take replaces the raw one
  (`params.keep_regions.drawn_file_id`), so the text check reads the still's
  own words there. A region is for what nothing in the shot moves across: a
  hand over it is drawn over by the still.
- **The camera's motion (`motion.py`).** What the platform draws over
  a made shot follows its camera by measure, never by assumption: a
  similarity (scale, rotation, translation) from a reference picture to each
  frame, fitted coarse to fine (`ANALYSIS_SIDE` grey copies, `LEVELS`) by
  Gauss-Newton on the grey levels with a gain and an offset fitted beside it
  (a masked reference and a model's exposure drift bias nothing), Huber
  weights against what moves in the scene, and the caller's mask for what it
  does not trust; `Fit.score` (their correlation once aligned) under
  `MIN_SCORE` is a frame where the reference was not found.
- **The previous take** of a shot generated again is deleted once a new
  take is ready and passed these checks, or at once with `discard` (see
  "Generated again in place"); a failed take is replaced whichever way.

**The first frame.** Every video model takes a start image, so a shot
may start from an image (`reference_file_id`, `params.reference_file_id`) or
from a frame of a video (`params.reference_video`: `kind` recording / shot /
file, its id, the `file_id` of its video, its name, `at_seconds`, `step`): a
recording (`reference_recording_id`), another AI shot (`reference_clip_id`) or
a `video/*` file (`reference_file_id`). The frame is the one after a
recording's step (`reference_at_step`, `capture.step_result_seconds`: the frame
the contact sheet shows for it), else the one at `reference_at_seconds`, else
the last, so a shot placed after that footage continues it. `resolve_reference`
checks everything before anything is paid for: one source, readable as a scene
is (`can_read_project_asset`, `can_read_file`), ready with its media, and a
step or a moment inside it. The worker takes the frame when the job runs
(`clips.start_frame`: `encoder.extract_still`, JPEG, no side over
`START_FRAME_MAX_SIDE`; a job resumed upstream needs none), and
`clips.start_note` puts which frame it was first in the shot's notes. A source
gone by then fails the shot with the reason rather than making another shot
from the prompt alone. No video provider here takes a video as input
(restyling the footage itself, Runway Aleph, Seedance reference videos): that
would be a capability the provider declares on `VideoRequest`.

**A start picture every provider reads.** `clips.start_picture` is
what an image becomes before any provider sees it: a PNG or a JPEG as it is
(no side over `START_FRAME_MAX_SIDE`), an SVG (by its type or its bytes - a
template's logo, badge or diagram) drawn by the bounded rasteriser
(`svg_raster.rasterize_svg_async`, fitted into the box), anything else
(WebP, GIF, TIFF...) decoded in full and re-encoded as PNG; bytes that are no
picture raise `StartPictureError`. `resolve_reference` runs it on the file
(`check_start_picture`, at most `MAX_START_PICTURE_BYTES`) before anything is
paid for, and `start_frame` again in the worker.

**A frame kept as a picture.** `save_video_frame` takes the same
sources and moments (`clips.resolve_frame`, which refuses a file that is
already a picture, then `frame_picture`: PNG, up to `FRAME_PICTURE_MAX_SIDE`,
3840) and stores the frame as a working file of the conversation
(`_persist_generated_file`, deduplicated per conversation): its `file_id` is
an image scene, a statement backdrop or a shot's `reference_file_id`. With
`region` (`{box: [x0, y0, x1, y1]}` or `{quad: [[x, y] x 4]}`, cut by
its bounding box, 0-1 of the frame, `clips.frame_region`) the frame is
extracted at its own size (`REGION_FRAME_MAX_SIDE`) and only that region kept,
at 100 % (`clips.cut_region`): any moment of a render judged on its real
pixels, an insert's region box given by the answers. The
frame is resolved on a session, then extracted with none open. A moment past
the video's end gives its last frame and says so (`clamp` in
`resolve_reference` and `_frame_of`, the source's `clamped_from` / `lasts`,
read by `frame_label`); a shot asked to START past the end is still
refused, what it would open on not being what was asked. A render's MP4 is
a video file like any other: `render_video` and `get_video_jobs`
name a whole render's `file_id` - ready, or failed with its MP4 kept - with
how to save the frame at any moment (`render_frames_line`), and `get_video`'s
render list names each render's MP4 (`render_list_line`, a scene preview
said to be one), so any moment is looked at before the video is called
final. Anyone who can open the video may frame it, whoever rendered it, as
the media route serves it (`projects.visible_render_file`, read by
`resolve_reference` when `can_read_file` refuses a video file).

**Many frames in one call.** `save_video_frames` takes the same
sources and `region`, and either a list of `at_seconds` or a `step_seconds`
sweep from `from_seconds` (0) up to `to_seconds` (the end, not included),
at most `clips.SHEET_FRAMES_MOST` (60) frames (`clips.sheet_moments`).
`clips.frame_sweep` downloads the video ONCE and cuts every moment from it
(`encoder.extract_still`, four at a time; a moment past the end is its last
frame, `asked` kept), each region at 100 % like one frame's; `clips.frame_grid`
lays the frames out as one contact sheet, each tile labelled with its moment
(`render_checks.clock`, `m:ss.xx`), scaled to one width and never cropped. The tool
attaches the sheet only and names every frame as a working file of the
conversation at full size (its `file_id` in the answer's JSON head), so one
look covers an insert and `read_file_content` opens any frame. Both frame
tools declare their moment and region arguments as positions
(`ToolDefinition.position_arguments`, budget 60 per video and turn): the
loop breaker counts thirty frames of one MP4 as thirty pages, never as a
loop (`dev-docs/reference/backend-tools-loop-guards.md`). `frame_sweep` and
`frame_grid` are generic - a series of moments of any video, a grid of any
pictures; `get_video_insert_sweep` (`insert_sweep`) is the render's
own per-insert sheet, drawn from the gate's coverage (failing tiles framed
red), and `save_video_frames` the free sweep of any stretch of any video.

**The frame is kept.** A frame cut from a video is saved as a file of
the shot's conversation (`assets.SOURCE_CLIP_START_FRAME`, `clip-start-frame`,
the shot's owner's) and named in `params.reference_video.frame_file_id`, which
`run_clip` stores before the provider is asked. A shot generated again
downloads that file instead of cutting the frame again, so it starts from the
very same picture even once its recording is gone; only a frame deleted since
is cut (and kept) anew, and a frame the object store refused is sent anyway.
`clips.start_frame_file_id` is what the shot opened on - that frame, else the
start image - served as `start_frame_file_id` by `serialize_clip`, as the media
token asset `start_frame` of a clip (the inspector's `First frame`, with where
it was taken), and in `generate_video_clip`'s answer (the cut frame attached
before the poster, with its `[[FILE:id]]`). Being a conversation file, it is
not deleted with the shot: the chat may show it, and two shots may share it.

**The frame has the shot's shape.** Each provider fits a start image
of another aspect ratio its own way (cropped, padded, refused), so
`resolve_reference` refuses a frame whose width / height is not the shot's
`aspect_ratio` (`clips.fits`, within `ASPECT_TOLERANCE`: an encoder's
1920x1088 is 16:9, a 16:10 page is not), naming its size, the middle a crop
would keep (`clips.cropped_size`) and the other ratio when the frame has it.
`crop: "center"` (`generate_video_clip`, `POST /clips`) is recorded in
`reference_video.crop`, and `encoder.extract_still(crop_to=...)` cuts the
frame's middle to the shot's ratio before scaling it. A recording's size is
its viewport times its pixel ratio, an AI shot's its `width` / `height`; a
video file's is read when its frame is cut, so the worker applies the same
rule before the provider is asked (`_frame_of`, given the shot's ratio; a
frame kept as a picture by `save_video_frame` has none, so any shape). A kept frame that no longer
fits - a shot generated again in the other ratio - is cut again. An image is
sent as it is (an animated still is framed by its scene): it is neither
checked nor cropped, and `crop` with one is refused.

**Preview before paying.** `preview_start_frame` (tool) and
`GET /clips/start-frame` take the `reference_*` arguments of
`generate_video_clip` (one schema block, `_START_FRAME_PROPERTIES`) with
`aspect_ratio` and `crop`, and answer the frame the worker would cut - same
checks, same moment, same crop - saved as a `clip-start-frame` file of the
conversation (the route: of the caller, with a media URL), its `file_id` and a
note; the agent looks at it, moves `reference_at_seconds`, then generates. An
image is its own frame. Two phases, so no session is held while the video
downloads and decodes: `clips.plan_start_frame_preview` (the checks, a
recording's action log) then `clips.preview_start_frame`. A recording's frame
caught while its page still loads is flagged: `encoder.gray_frames` reads the
frames after it (`SETTLE_FPS` a second, `SETTLE_SIZE` gray thumbnails) up to
the next step that acts on the page (`spec.PASSIVE_ACTIONS` - wait, wait_for,
highlight, pause, marker - do not; a change that step makes is the script's,
not a load), the horizon and the end; `clips.settle` calls a frame loading
when one within `SETTLE_WINDOW_SECONDS` (1 s) differs from it in more than
`SETTLE_CHANGE` (10 %) of its pixels (by more than `SETTLE_NOISE` gray
levels), and settled when none does over at least `SETTLE_MIN_SPAN_SECONDS`.
The note then says "page still loading at 6.6 s, settled at 7.5 s" and offers
`reference_at_seconds` at the settled moment (or that it had not settled when
the watch ended). A spinner alone changes too few pixels to count; a page that
fills in within the second does.

**A real picture inside a picture.** `compose_picture` (tool) and
`POST /video-studio/pictures/compose` draw 1 to `pictures.MAX_LAYERS` (4)
pictures on a base picture - the real product screenshot on the screen
drawn in a still, a real wordmark on a backwall - with Pillow alone, no
browser and no AI (`pictures.compose_files`): each layer flat (`x`, `y`,
`width`, `height?`, in the base's pixels) or in perspective (`quad`: the
place's four corners, top left, top right, bottom right, bottom left; the
eight coefficients of Pillow's `PERSPECTIVE` transform solved from them,
`pictures.perspective_coefficients`), with `opacity` and `fit` (`contain`
pads the layer to the place's proportions, `cover` cuts its edges,
`stretch` fills it). Every picture is a file the person can read (an SVG
drawn by the bounded rasteriser; a base of at most `MAX_SIDE`, 4096 px);
the result is a PNG, a working file of the conversation (the tool) or of the
caller (the route, `SOURCE_UPLOAD`). Fields are checked one by one, named
(`pictures.parse_layers`).

**Images drawn from reference pictures.** `generate_image` takes
`reference_file_ids` (1 to the model's limit) and `mask_file_id` where the
image model takes them, offered in its schema only then:
`BaseLLMProvider.image_reference_limit` / `image_takes_mask` /
`edit_image`. The GPT image models draw from up to three pictures with a
mask (`POST /images/edits`, the mask editing the first picture where it is
transparent); a Gemini image model draws from pictures given as parts
before the prompt, without a mask; Imagen and the other providers take
none, and a call naming pictures there is refused with the model's name
(`media._image_inputs`, which also checks each picture is the caller's to
read).

**An image the provider failed.** `images.draw_retrying` asks a
drawing its provider refused as rate limited (429) or failed with a server
error again in the call, at most `IMAGE_RETRIES` (2) times, after the wait
the provider named (`provider_errors.retry_delay`) or 4 s doubling, each up
to a quarter longer at random; never when the provider asks more than
`IMAGE_RETRY_MAX_WAIT_SECONDS` (30), nor once the call's deadline
(`current_tool_deadline`) leaves less than `IMAGE_DRAW_SECONDS` for another
drawing. A failure answers `images.failure_message` - the model, the HTTP
status, the provider's own words - flagged `ToolResult.outcome_failure`, so
the failure brake counts it per call, never in the tool's streak (a provider
refusing four prompts in a row withdraws nothing). An image made but not
stored says so; a stored one is named by its `file_id`, the picture a shot
starts from or a layer is drawn from.

## Speech generation

Speech is an LLM-provider capability too (`app/llm/speech.py`):
`BaseLLMProvider.list_speech_models` / `list_voices(model)` (the default
first) / `synthesize_speech(SpeechRequest)` with a provider-declared
`default_speech_model`. It serves the Video Studio voice-over and the chat's
read-aloud (`POST /chat/tts`). Speech-to-text is `app/llm/transcription.py`:
the chat's dictation and the voice checks of a render.

- **One OpenAI speech API client** (`OpenAISpeechMixin`, `POST /audio/speech`)
  on both OpenAI-shaped bases: OpenAI and Azure OpenAI (its v1 endpoint,
  `_speech_client`) speak OpenAI's voices (`speech_voices_are_fixed`), with
  `instructions` for the models after `tts-1` and a retry without them when
  refused; the gateways (LiteLLM, custom endpoints, the Filigran gateway with
  `_speech_headers`) pass the caller's voice through, since behind them the
  voices are the upstream's.
- **Gemini** speaks through the Interactions API (`POST /v1beta/interactions`,
  WAV; the style and the pace as `speech_metadata`) and lists its studio
  voices, then the project's designed / replicated voices and the extended
  library (`GET /v1beta/voices`); the preview TTS models keep
  `generateContent`. REST, because the SDK in use predates those routes.
- **ElevenLabs** (`elevenlabs_provider.py`) is a provider type validated for
  speech (`only_capability = "speech"`; it also composes music and makes
  videos and images): models from `GET /v1/models`
  (`can_do_text_to_speech`), voices from the account's library (`GET
  /v2/voices`, paged, its own cloned / professional / generated voices first,
  reused 5 minutes), speech from `POST /v1/text-to-speech/{voice_id}` (44.1
  kHz MP3, `speed`, `previous_text` / `next_text`); `key||base_url` for a
  regional endpoint.
- **Single-capability providers**: `only_capability` on the provider class;
  `router.CHATLESS_PROVIDERS` (TEI, ElevenLabs, Kling AI, Runway, Seedance) keeps them out of chat
  selection, failover and the provider-deletion chat routes, and Settings
  validates them for that capability whatever flow added them.
- **Which models speak**: `router.is_speech_model`, the catalog's
  `audio_speech` mode first (OpenAI, Azure, Gemini, ElevenLabs, Polly, Azure
  Speech, Vertex Chirp, MiniMax...), else a `tts` marker. LiteLLM reads its
  proxy's `/model/info` modes, the gateway its own `type`.
- **Which provider, model and voice**: `router.get_speech_provider` returns a
  `SpeechTarget` - the caller's choice, else `PlatformSettings.speech_*`
  (Settings > AI models > Speech generation, `/settings/available-speech-models`,
  `/settings/speech-voices`, migration `v5799speech01`), else the first
  provider with a built-in model, a speech-only one first. Only among the
  providers the person may use (`provider_routing`: reservations and
  exclusions); an admin choice they may not use falls back. The voice
  is the admin's only on the admin's model. Deleting the provider clears
  the setting and its voice.
- **The video's voice**: `Voiceover.provider_id` / `model` / `voice` (a
  template's `defaults.voiceover` carries a brand voice); a spec's
  `provider` type name resolves to the first config of that type the person
  may use. Unset, the video follows the platform's choice, including a later
  change of it.
- **The style.** A provider says which of its speech models follow a
  written delivery instruction (`BaseLLMProvider.speech_takes_style(model)`:
  OpenAI's after `tts-1`, every Gemini TTS model, never ElevenLabs; `None`
  when unknown, another vendor's model behind a gateway). A voice-over with a
  `style` that follows the platform's model, when that model takes none, is
  spoken by a model of the same provider that does (`voice.resolve`: its
  `default_speech_model` when it qualifies, else the first it lists), for
  the render, the preview and the save line alike; a video naming its own
  model keeps it, and its `Voice-over:` line says the style is NOT applied -
  at save, not in the render's notes. `voice.describe` is the `Voice-over:` line of `create_video`,
  `edit_video` and `get_video`: the model and voice the render will speak
  with, resolved as the render does (`voice.resolve`), whether it is the
  platform's or named by the video (`voice.names_its_own`, the platform's
  model then named), or why nothing can speak. `preview_voiceover` hands back
  only the fields the call named (a voice, a provider and model), never the
  platform's model, so a preview does not pin it into the video. The editor's
  `Speech model` names the platform's model in its unset choice (the
  `/settings/speech-voices` answer) and lists a model the video names even
  when no provider offers it, so it never reads as the platform's.

### Jobs act for their person

A render (in its own process), an AI shot and an AI track (in the worker) run
outside any request: no LLM caller is bound. The Filigran AI Gateway signs
each request with the caller's XTM JWT (`app.llm.identity`), so without one
every narration failed ("No LLM caller identity available") and the render
finished silent with a note per scene. `app.llm.identity.bind_llm_caller(db,
user_id)` binds the job's person (`set_llm_caller`) before any provider call:
`run_render`, `run_clip` and `run_music` call it once their row is loaded,
which also charges the gateway's quotas to that person. A call with nobody
bound signs as the service identity, which a process the platform's bootstrap
did not run looks up when it starts (`load_service_identity`; the worker keeps
it current). A deactivated person's job is bound to nobody: it still runs, and
signs as (and is charged to) the service identity.

## Pronunciation lexicon

Brand names and acronyms are said the way one lexicon says them, and every
narration and every render is heard back against it.

- **The entries** (`lexicon.py`, `spec.Pronunciation`): `{text, say, ipa?,
  match, apply_to}`. `match` is `word` by default (the whole word, case and
  all, however it is joined: `OpenCTI's`, `XTM-based`), `exact` (the whole
  token, never glued to a hyphen or an apostrophe) or `case_insensitive`;
  several words match across any whitespace. `apply_to` is `voice` by default,
  `captions` or `both`: the captions keep the written form unless an entry says
  otherwise (`timeline.caption_texts`, `PlanMedia.captions`). Letters read one
  by one are capitals joined by hyphens in `say` (`X-T-M`): `Entry.segments`
  cuts a `say` into letter runs (two capitals or more) and words.
- **Two sources, merged per video** (`lexicon.merge`): the video's own
  `voiceover.pronunciations` first, then each platform entry it does not
  override (the same written form: `lexicon.same_form`, case-folded when
  either entry is `case_insensitive`). The platform's lexicon
  (`lexicon_store.py`, model `VideoPronunciationLexicon`, migration
  `v6091lexi01`) is versioned: a save adds a version (one row, never updated),
  a restore adds one holding an older version's entries; the newest decides
  and is read on every use (`lexicon_store.for_voiceover`), so the editor's
  preview, the render and the gate ask the voice cache for the same
  narrations. When no version exists, startup writes the first from
  `services/video_studio/data/pronunciation_lexicon.json`, validated against
  its JSON schema (`pronunciation_lexicon.schema.json`, `lexicon.load_seed`);
  a later startup never writes over an administrator's edits. The seed holds
  the readings the brand owner confirmed and nothing else; a new term is an
  administrator's, never the pipeline's guess. One exception: a seeded
  term the newest version holds without the data file's IPA, read as the data
  file reads it or as its bare written form (`Filigran` said `Filigran`), is
  given the data file's entry back in a version startup adds
  (`lexicon_store.seed_readings_lost`, `created_by` `system`, a note naming the
  terms); a reading an administrator changed, an entry with an IPA of its own
  and a term removed are left as they are.
- **Said through the strongest mechanism the speech model offers**
  (`lexicon.render`). A provider declares it per model
  (`BaseLLMProvider.speech_pronunciation` -> `speech.PronunciationSupport`):
  the SSML it reads (`w3c`, a `<speak>` document with its text escaped, or
  `inline`, tags in plain text), whether it honours `<phoneme
  alphabet="ipa">` and `<sub alias>`, whether it takes a term's IPA in its
  delivery instruction (`instruction`, `SpeechRequest.pronunciations`),
  and whether it keeps a lexicon a request
  refers to (`register_pronunciation_lexicon`, `SpeechRequest.lexicons`).
  ElevenLabs reads inline phonemes on `eleven_flash_v2`, `eleven_turbo_v2`
  and `eleven_monolingual_v1`, and a pronunciation dictionary of alias rules
  on every model (`POST /v1/pronunciation-dictionaries/add-from-rules`,
  `pronunciation_dictionary_locators`); OpenAI's models after `tts-1`
  (`gpt-4o-mini-tts`) take the IPA in `instructions`, after the style
  (`speech.pronunciation_instruction`: `Pronounce these words exactly as their
  IPA reads...`; a model refusing the instruction is spoken without it, a
  note saying so); `tts-1`, `tts-1-hd`, Azure OpenAI deployments, Gemini and the
  gateways declare nothing. Per term: an SSML phoneme for an entry with an IPA
  (`ssml_phoneme`), else its IPA in the delivery instruction with the written
  form kept in the text (`ipa_instruction`), an SSML `<sub>` for one without (`ssml_sub`), the
  provider's lexicon (`provider_lexicon`: its rules, `lexicon.provider_rules`,
  registered once per provider config and rule set, the id it answers kept in
  the voice cache under `lexicons/<config>/<digest>.json` and in memory,
  dropped when a request referring to it fails), else the narration
  rewritten (`text_rewrite`, `lexicon.rewrite_form`): the `say` form as given
  for a syllabic reading (`see-tem`). Matching is tokenised, so a word
  that only contains a term is never touched, punctuation stays, and a
  compound wins over its part. The neighbours sent for continuity are
  rewritten, never SSML. An entry's IPA that no mechanism of the model sends
  is said by its respelling as sent, and the render says so
  (`Rendition.respelled` / `Rendition.notes`: `"Filigran" has an IPA
  (/.../) that tts-1-hd cannot take ...: said by its respelling
  "Fil-i-gran".`, in the segment's notes, the render's notes and
  `preview_voiceover`).
- **Letters are one token.** An entry read letter by letter is never
  sent as separated letters, on any model. The IPA every IPA mechanism sends
  is `Entry.speech_ipa`: the entry's own, each run of letters joined into one
  phonetic word with the letters' names (`lexicon.LETTER_IPA`) and their
  stress, secondary on all but the last (`lexicon.letters_ipa`: `XTM` ->
  `ˌɛksˌtiːˈɛm`, `XTM One` -> `ˌɛksˌtiːˈɛm ˈwʌn`) - an IPA an administrator
  wrote letter by letter (`ɛks tiː ɛm`) is aligned run by run to the `say`
  form and joined the same way (`lexicon._aligned_ipa`), one that does not
  align is replaced by the letters' names and the words' English reading,
  and an entry read letter by letter with no IPA gets one from them (so it
  always has one: an SSML phoneme or a delivery instruction, never a
  fallback). Every mechanism that sends text sends the letters glued into
  the words the written form has (`Open C-T-I` -> `OpenCTI`, `X-T-M One` ->
  `XTM One`: the text rewrite, an SSML `<sub>`, a provider lexicon's alias),
  never with hyphens, spaces or punctuation between them, which a voice
  reads with a pause after each. The seed carries the compact IPA of each
  letters term (`XTM One` and `XTM Hub` included).
- **The cache digest** (`voice._request`) adds the rendition's key
  (mechanisms, SSML or not, the rules' digest, the IPA a delivery instruction
  carries) only for a narration that
  writes a term: every other narration keeps the digest it had, and its take.
  Each attempt records its mechanism (`ssml_phoneme+provider_lexicon` when
  several) and the exact string sent (`voice.rendition_of`), on the
  segment's record and in the render's `voice` summary.
- **Heard back, lexicon-aware** (`pronunciation_check.hear`, used by
  `voice_check.compare` and `audio_qc`). Both sides are tokenised alike: each
  term the narration writes stands as one token (`\x00term:<written form>`),
  and in the transcript any reading the lexicon accepts for it stands as that
  token - its written form, case, punctuation and spaces aside (`Open CTI`,
  `open-CTI`, `Fili gran`; the capitals of an acronym still capitals, unless
  the entry is `case_insensitive`, and letters written one per word only where
  the entry reads them so: `X T M` is `XTM`, `C T E M` is not `CTEM`), its `say` form with any separators
  (`see tem`, `seetem`; only when it sounds like the entry's IPA,
  `Entry.say_agrees`, so a wrong `say` said faithfully is still wrong), its
  letters one per word or glued in capitals or spelled by their names (`X T
  M`, `ex tee em`, `see are queue`), or a span of words that sounds like its
  IPA (`phonetics.reading_cost`: the IPA parsed into a General American
  inventory, word by word, the words read by English letter-to-sound rules).
  Two bounds hold together. A weighted edit
  distance - a reduced vowel for another 0.3, a voiced pair 0.5, a stop lost
  after its own nasal 0.35, a tense vowel for its lax pair 0.7, else 1 -
  within a tenth of a change per phoneme and never a whole one: it weighs a
  vowel's quality. And a metaphone-style key (`phonetics.key`: the
  consonants, and a mark where a word starts with a vowel) at most
  `MAX_KEY_EDITS` (1) phoneme from the entry's: a near consonant counts whole
  there, and so does a word broken before a vowel. `Phil Igram` for
  `Filigran` (the letter name `I` inside it, the last `n` heard `m`) keyed `f
  l V g r m` against `f l g r n` is two off and fails, where the weighted
  distance alone saw 0.6 and passed it; `Filigrand`, `Phil a gran` and
  `Philigran` stay one off or none. Exact
  readings are tried first, longest entry first; a phonetic one takes the
  span closest per phoneme. A number the speech-to-text writes in digits is
  read as its English words first (`phonetics.number_words`: `XTM 1` and
  `XTM1` are `XTM One`), and so is a number in the entry, in its
  written form or in its `say` form: `pronunciation_check._said_words`
  holds a `say` word run to its words with each number spelled out (`Windows
  11` heard `Windows 11` or `windows eleven`; also the span a reading may
  take), and `phonetics.say_phonemes` reads the number as its words instead
  of dropping it. The WER is computed on those tokens.
- **The brand check** is the same alignment read at each term: the transcript
  must hold its token where the narration has it. Otherwise the segment fails
  whatever its WER, `brand term mispronounced: CTEM heard as 'stem' at
  0:03.2` (the words aligned there, bounded by the neighbours, and the time of
  the first).
- **Fluency**: a term is said as one word. An accepted reading still
  fails when the audio holds a silence of `MAX_TERM_PAUSE` (0.15 s) or more
  inside its span (`pronunciation_check.longest_pause`, never the silences at
  its edges): `Phil ... Egran`, `Open ... C-T-I`, letters spelled one at a
  time, `brand term mispronounced: OpenAEV spelled with pauses, a 0.39 s
  silence inside ('Open A E V') at 0:01.3`, whatever speech model said it.
  The silences are the audio's own, the gaps
  between its voiced runs (`pronunciation_check.pauses_in` on
  `speech_timing.voiced_runs`, which already bridges the closures under
  `MIN_PAUSE`), read from the take heard (`voice_check.pauses_of_wav`), the
  encoded stem of a render (`audio_qc.check_audio`) or the preview
  (`preview_voiceover`). The threshold was measured on spoken takes: every
  reading a listener heard as broken held 0.15 s or more, every fluent one
  0.13 s at most. The check record's `RECORD_VERSION` (2) moved with the rule,
  so a narration that passed before is heard once more; it moved again (3)
  when a number in a `say` form started to be heard as its words, so
  a narration that failed on one is heard again rather than refusing the
  render, and again (4) with the key of the phonetic reading, so a
  narration a looser reading passed is heard once more. Version 5
  hears a proper noun by its sound and fails a narration another voice said:
  it only accepts more, so a version-4 pass still stands
  (`voice_check.PASSED_SINCE`) and a version-4 failure is heard again.
  Version 6 refuses a term read letter by letter, below: every
  narration heard before is heard once more (`PASSED_SINCE` 6).
- **Syllables**: a speech-to-text writes `CTEM` for `see tem` and
  for `C-T-E-M` alike, so a term read as words whose written form, read at
  sight, says more syllables than its reading (its capitals read one by one,
  `pronunciation_check.spelled_syllables`: `CTEM` 4 where `see-tem` says 2;
  `phonetics.syllables` counts a reading's vowels) is held to the voice's own
  syllables: the nuclei of the audio heard (`pronunciation_check.nuclei_in`:
  the peaks of its loudness in `NUCLEUS_BAND`, 150-1000 Hz, where a vowel is
  loudest and a fricative or a burst is not, each `NUCLEUS_DIP_DB` over the
  dip before it and `NUCLEUS_GAP` after the peak before) inside the span of
  its reading. More than half-way from the syllables its reading says to
  those its letters say fails it, whatever its words: `brand term
  mispronounced: CTEM read letter by letter at 0:41.2 (4 syllables heard
  where its reading says 2)`, `syllables` in the term's record. Read from the
  take heard (`voice_check.nuclei_of_wav`), the encoded stem of a render and
  the preview, as the pauses are; a term failing it was heard in place and
  is never looked for elsewhere (`audio_qc.heard_terms`).
- **Proper nouns**: a name no entry covers is no brand term, but it
  is no raw word either - see "Each narration heard back" above.
- **The retry ladder** (`voice_check.speak_checked`): the video's voice
  with its strongest mechanism, the take of the same voice that passed on the
  same words before (`earlier`: no new take, no new transcription),
  the same voice anew with the text rewrite
  (`lexicon.FALLBACK`), the letters of each reading glued into the words of
  its written form as every text mechanism sends them - never a pause
  between letters, which no fluent reading has; a syllabic reading stays as given (`Fil-i-gran`, `see-tem`):
  its parts are what tell the voice how to say it, and glued it is read as an
  unknown word (`Filigran` spoken `Filogram` by both OpenAI voices) -, then,
  for each term those two takes both heard wrong
  (`voice_check.heard_wrong_twice`: video b56997a2 sent `OpenAEV`, its text
  rewrite, three times and heard `OpenA` three times), the same voice with
  that term in another context (`lexicon.REFRAMED`, the `text_reframe`
  mechanism, `Attempt.reframed`): its `say` form as the lexicon spells it run
  by run (`lexicon.reframe_form`: `Open A-E-V` -> `Open AEV`, the letters of a
  run still glued) and alone in its clause - a comma before and after it
  where no punctuation, nor the narration's start or end, parts it already
  (`Run the drills with, Open AEV, every week.`); the render's notes say a
  take placed so. Then the same again slower (`voice.slower_speed`; the text
  rewrite when no term failed twice), then the closest voice of the
  same speech model with its strongest (`voice.closest_voice`), whose
  pass is a `voice_fallback` failure of the video's voice (refused by
  the render like one) - never another provider's voice, never the
  platform's default; the closest take is the one of the video's own voice
  with the fewest terms heard wrong. A failure names each term, what was heard
  and every mechanism tried (`tried: ssml_phoneme (the video's voice),
  text_rewrite (the video's voice again), text_reframe (the video's voice,
  OpenAEV respelled alone in its clause), text_reframe (the video's voice at
  0.85, OpenAEV respelled alone in its clause), ssml_phoneme (the closest
  voice, Cedar)`): the `voice_segment` finding, `render_video` and
  `get_video_jobs`.
- **The final render** (`audio_qc.check_audio`): the voice stem is put
  through the final mix's encode (`encoder.encode_voice_like_mix`: AAC at 48
  kHz and the preset's bitrate, cut to a preview's span, in an MP4 container),
  demuxed and decoded, then transcribed: what was right in isolation and
  broken by the resampling, the stitching or the encoding is caught. A term
  heard wrong or broken by a pause is a `pronunciation` finding (a failure) at
  its time in the MP4 - once the whole transcript was searched within 3 s of
  its planned time for a reading no narration heard as its own
  (`audio_qc.heard_terms`): a term heard a little late, or among the next
  narration's words, passes where it was heard. A term accepted in other words than the entry writes
  (`TermHearing.heard_otherwise`: neither its written form nor its `say` form,
  case, punctuation and spaces aside, `pronunciation_check.plain` - a phonetic
  reading, its letters by their names; never `Open CTI` for `OpenCTI`, nor `see
  tem` for `CTEM`, which is the reading the entry asks for)
  is a `pronunciation` warning, one per form heard at its first time
  with how many times (`brand term Filigran heard as 'Philigran' at 0:03.2,
  not as written: accepted as a reading that sounds like its entry - listen to
  it`): the render passes, a reviewer reads what was heard without the audio,
  and the gate holds nothing back for it (`open_findings` reads failing audio
  findings only). `Checks.pronunciation` says per term its occurrences, how many passed
  and the forms heard in the takes the render plays, and every take of a
  narration it grades that it does not play and that heard the term wrong
  (`audio_qc.with_attempts`, `SpokenSegment.unplayed_failures`:
  `attempts_failed: [{scene_id, times, heard}]`, a term only such takes said
  added with no occurrence) - `OpenAEV 3 of 3 in the takes kept (heard
  'OpenAEV'), 1 attempt failed on scene st-demo (heard 'OpenA')`
  (`render_checks.pronunciation_lines`, and the editor's renders panel): a
  term the narration's own check failed on and replaced never reads 100 %
  beside that failure. With the encode
  unavailable the stem itself is heard, with a note.
- **Authoring.** `preview_voiceover` merges the platform's lexicon under the
  entries it is given and answers with the mechanism, the string sent, the IPA
  its model could not take and the speech-to-text's reading of each term (a
  `WARNING` on one accepted in other words than written). It also runs the
  render's own transcript check on the line, a line with no lexicon term too
  (`voice_check.compare`: the same speech-to-text, normalisation and lexicon
  as `speak_checked`): the share of words heard as written (`1 - wer`,
  passing from 90 %), the verdict and the line word by word, each word heard
  otherwise in brackets (`pronunciation_check.word_diff`:
  `['Their' heard as 'There'] platform ...`, a word `not heard`, a word
  `added`), so a rewording is tested before a render; `transcript_only: true`
  skips the audio attachment. The same diff is kept with each take of a
  record (`Attempt.as_dict` `otherwise`, the first 12 words), named in a failed
  check's reason (`render_video`'s gate, the render's finding) and in a
  fallback take's fact (`FallbackTake.words`: `14% of its words otherwise
  ['Their' heard as 'There']`, what `get_video` and the save's quality check
  say); a record written before it says the share alone. At save, the content check
  (`quality.content_findings`, kind `pronunciation`, advisory) warns about a
  term written in another case than its entry (`Opencti`, `Ctem`) - unless the
  entry only changes the case (`lexicon._only_cases`: its `say` is its written
  words, said as words, agreeing with its IPA: `eXtended` said `extended`) -, the letters of a term the lexicon reads as a word written out
  (`C-T-E-M`), a term the brand lexicon reads (`lexicon.brand_lexicon`:
  the readings of the platform's data file the brand owner confirmed) that the
  video's lexicon has no entry for - an administrator removed it, or the
  platform lexicon could not be read -, `CTEM has no pronunciation; the voice
  may spell it` where its capitals read at sight say more syllables than its
  reading, else `the voice guesses how to say it`, with the brand's entry to
  add to `voiceover.pronunciations` -, and a word written like a coined name or an acronym (capitals
  after its first letter; in capitals, three letters or more) that no entry
  covers (`lexicon.pronunciation_warnings`). It reads the narration alone, and
  each sentence says so (`its narration writes ...`): a title or a line on
  screen may write the term as its entry does. The editor lists the video's own entries
  (with IPA, match and target behind a disclosure) and the platform's it does
  not override, each with a play button (`POST /pronunciations/speak`: the
  draft's voice-over and lexicon, the voice cache, a voice token) and an
  `Override` action; Settings > AI models > Pronunciation lexicon edits the
  platform's (`GET` for every user, `PUT`, `GET /versions`, `GET
  /versions/{n}`, `POST /versions/{n}/restore` for administrators, audited
  as `video_pronunciation_lexicon`).

## Music and the mix

A video's music (`spec.Music`) names ONE source: `track` (a built-in track),
`music_id` (an AI track) or `file_id` (an upload); `prompt` asks for an AI
track fitted to the video on save. Levels: `volume` (its level where no one
speaks, against the voice), `ducking` / `duck_db`, `start`, `loop`, `ending`
(`fade` | `natural`), `fade_in` / `fade_out`; `enabled: false` plays none. A
spec with levels and no source is valid (the template's track plays, else
nothing, and the save says so).

### Built-in scores (`score.py`)

Royalty-free tracks the platform synthesises (numpy, scipy: no sample, no
third-party audio). A `Style` is a tempo, a key, progressions (`main`, `lift`,
`cadence`, `final`), instruments (pads: warm / strings / dark / glass; keys:
FM electric piano, felt piano with inharmonic partials, mallet pluck, stabs;
bass; bells), a drum kit and transitions. `score.render_score(track, duration,
cues)` composes it for ONE video (invariant 16: computed, not prompted):

- `_layout` keeps the style's tempo and counts the bars so the final chord
  lands 1.6 s to a bar before the end and rings out on the last frame;
- `plan_sections` starts a section on the bar nearest each scene change
  (`TimelinePlan.cues()`: start, scene type, narrated); a section shorter than
  two bars joins the previous one; the energy follows the scene (`title` 1,
  `statement` / `clip` 3, others 2, a narrated scene at most 2 with "room":
  fewer notes, softer keys, no bell); the first section is an intro; the last
  bars play the style's cadence;
- a riser (or a reversed-cymbal swell) leads into a lift, an impact and a
  crash mark the peak, a fill ends the bar before it; the pads' low-pass
  opens with the energy; the kick pumps the pads and bass.

The render is deterministic (seed: `score_digest` of the style, the length and
the cues) and runs in 16 s chunks: notes ring into the next chunk through a
12 s carry, the filters' state and the reverb's / delay's tails
(`_Convolver`) are carried, so memory does not grow with the length (10 min:
~20 s of CPU, ~200 MB). A render composes a bed once: `music.make_bed`
keeps the prepared bed in the render cache by the bed's digest
(`render_cache`, kind `beds`: on the worker's disk and in the object store
under `video-studio/render-cache/`), so the next render of the video at
another quality, on any worker, takes it. The cache also keeps each screen
insert's track (kind `tracks`, `render._track_digest`: the footage's file id,
the raw take and maps of a stabilised shot, what each insert asks, the
release), so the inserts are followed once per footage and quad. An object
stored more than 14 days ago goes with the hourly sweep (`render_cache.sweep`),
and a file on the worker's disk unused for a day goes with the working
directories (`sweep_disk`). The narrations and their transcript checks are
cached by what is said already, so a second render at another
quality speaks and hears nothing again. `STYLES` holds seven tracks; their words are
mirrored in the editor's `MUSIC_TRACK_WORDS` / `MUSIC_TRACK_DESCRIPTIONS`
(`@i18n-keys`), kept equal by a test.

### AI tracks (`music.py`)

`VideoMusic` rows (migration `v5915music01`, no foreign key so the migration
locks no existing table; outside the media retention: a few MB reused for
months) are composed by the `video_music` job (`runners.run_music`, the AI
shots' concurrency budget): `music.start_music` resolves the model through
`router.get_music_provider` (see "Music generation" below), the job resolves
again and makes one request, a progress estimate ticking while it runs; a
busy provider (`MusicProviderBusyError`) is asked again after the back-off,
any other refusal fails the row with its reason. `restart_music` composes a
failed track again in place (row locked, at most `MAX_RETRIES`). A save with a
`music.prompt` (`start_for_spec`) starts a track at the planned length plus
`FIT_MARGIN_SECONDS` (8 s: the track's own end lands after the outro,
the bed fading on the last frame), inherits the stored one for the same
prompt (a draft never pays twice) and writes its id as `music_id`; with no
music model the save succeeds with a note. `generate_music` with `video_id`
asks for the length planned when it is called (`projects.planned_seconds`)
plus `margin_seconds` (`music.fit_margin`: 0-60, `FIT_MARGIN_SECONDS` unset).

**The music follows the timeline.** Shots come out longer than
asked, a scene grows with its narration, a scene is added after: a track
composed early ends before the video. `projects.save_spec` compares the
planned length with the music source's (`music.length_note`: an AI track's
file once made, the length asked while it is composed, an uploaded file's
own, probed once per stored object) and says it in the save line and in
`get_video`: "Music ends at 142 s of 183 s (loop: false): it fades out at
142 s, leaving the video's last 41 s without music" (never looped) -
or "Music loops at 142 s of 183 s". `generate_music(video_id, refit: true)` composes the video's
AI track again in place at the video's length now plus the margin, never
shorter - a `duration_seconds` the video's own length included
(`music.refit_seconds`, the answer saying the length and why). The
video's length is its plan, or its current render's measured length when
that render is of the video's revision and longer (`music.video_seconds`,
`projects.rendered_seconds`): the plan estimates the voice the render
measured, so a track fitted to the plan alone still loops under a render that
came out more than the margin longer (a 150 s plan, a 167 s render). A new
track asked with `video_id` and the editor's `POST /music` are fitted the same
way
(`restart_music(refit=True)`, which also takes a READY track: its file moves
to `params.replaced_media`, deleted by `run_music` once the new composition
ends), the video keeping its `music_id`. The render's notes name the track's
length against the video's (`music.bed_length_note`).

**Outgrown, said at the edit.** The save's note compared the track
with the paced plan only, so a video whose spoken narration ran a few seconds
longer than planned outgrew its AI track unsaid, and the loop's seam surfaced
after an ultra render. The save's quality check (`edit_video`, `get_video`,
the editor) now reads an AI track's length whatever its `loop`
(`ContentFacts.music_seconds`, `music_composing`) and plans with each
narration at its spoken length: a looping AI track the video outgrew by more
than `music.OUTGROWN_SECONDS` (2 s, `music.outgrows`) is a `music_outgrown`
advisory on the scene where it loops - "track 144 s under a 146.5 s video:
refit or accept the loop" (`music.outgrown_words`) - never a refused render.
`length_note` leaves that case to it (said once), and keeps the loop of 2 s
or less, a track asked to play once and an upload.

**Measured as the render places it.** The save still fitted the
music to the paced plan: it said "about 165 s" for a video its render placed
at 175 s - its narrations, the template cards' among them, spoken longer than
the reading pace -, so a 174 s track that fit the plan looped under the
outro, said only by the render's note. `projects.measured_plan` plans an
effective spec as the render will place it from what is measured before it:
each narration at the length its voice take measured where one was spoken in
the video's voice (`preview.spoken_lengths`, the cache the render reads),
the others at the reading pace; a `beat_sync` video's cuts on the music's
beats (`beats.video_grid`, `timeline.estimate_plan(beats=)`). The save's
length (`SavedSpec.seconds`, `estimated_duration_ms`, "about 175 s (every
narration at its spoken length)" - `MeasuredPlan.basis` -), the AI track a
`music.prompt` starts, `length_note`, `projects.planned_seconds` (what a
refit composes for) and the quality check (`check_content`) read it; the
music findings take the current render of the revision when it came out
longer (`ContentFacts.rendered_seconds`, `music.video_seconds`), as
`get_video`'s length note does. A track shorter than that length is a
finding of the save (`music_short` played once, `music_outgrown` looping),
never first a render note.

**Extend a take.** A refit composes the whole track again: the take
the author approved is gone. `generate_music(video_id, refit: true, extend:
true)` keeps it. `restart_music(extend=True)` records what it keeps
(`music.extension`, `params.extended_from`: its file, length and the cut,
`EXTEND_TAIL_SECONDS` (24 s) before its end; a take shorter than
`EXTEND_MIN_KEPT_SECONDS` plus that, or already as long as asked, is refused)
instead of queuing its file for deletion, on a model that continues a clip
(`music.resolve_continuing`: the take's own model when its provider says it
does, `BaseLLMProvider.continues_music`; else, unless the person named one,
the first music model they may use that does, read off
`router.list_music_models_for`'s `continues`, which `video_studio_capabilities`
shows). `run_music` downloads the take and `music.extend_take` decodes it at
its own rate and channels (`encoder.decode_pcm`, 32-bit float), sends the
`EXTEND_REFERENCE_SECONDS` (30 s) before the cut as
`MusicRequest.continue_from` with the length from the cut to the length asked,
skips what the answer replays (`MusicResult.continues_at`), crossfades the
continuation in over the take's own audio past the cut
(`EXTEND_CROSSFADE_SECONDS`, equal-power) and writes a 32-bit float WAV
(`encoder.encode_pcm`): up to the cut the take sample for sample. The take's
file is deleted once the extended one is stored. An extension that is not
made - refused, cancelled, no playable audio, no model that continues, or one
the engine gives up on (`dead_letter`) - leaves the track ready on the take it
kept (`music.kept_take`), the reason in `params.extend_failed`, said by
`get_video_jobs`.

**Another provider's credits, read first.** A take made with
`lyria-3.5` (which continues no clip) was extended with ElevenLabs'
`music_v2_5`, unsaid, and failed there on its quota (HTTP 401, 0 credits)
after `generate_music` had answered "ready" on the kept take. The account a
track is composed on is read before it is queued
(`BaseLLMProvider.music_credits`, ElevenLabs' subscription read it shares
with `video_credits`; `music.credits_refusal`): `start_music` and
`restart_music` refuse an account with no credits left, naming the model and
the provider, the take as it was. The refit's answer names the model and
provider the extension runs on, and, when it is not the take's own, says so
with how to keep the take's own model (`cancel_video_job`, then refit without
extend). A call whose wait sees the track ready on its kept take
(`params.extend_failed`) answers that the extension was not made and why,
as an error, never "ready".

**The take's own provider first, and a pad with no model.** Lyria
composes no continuation (text and pictures in, one turn), so an approved
Lyria take had no in-tool extension once the only continuing models' account
was empty. `resolve_continuing` takes a continuing model of the take's own
provider first, another provider's only when it has none.
`generate_music(video_id, refit: true, pad: true)` extends a take with no
model call: `restart_music(pad=True)` records the take it keeps like an
extension (`music.padding`, `params.extended_from` marked `pad`, cut at the
take's end - no provider resolved, no credits read, no attempt of
`MAX_RETRIES` spent: `params.pads` is taken out of the count), at the
video's length plus `margin_seconds` (none by default, `pad_seconds`), at
most `PAD_MOST_SECONDS` (8) past the take. `run_music` holds it there with
no provider (`_padded`, `music.pad_take`): `padded_samples` keeps the take
sample for sample up to the end of its last `PAD_HOLD_SECONDS` (1) within
`PAD_ENDING_DB` (12) of its level - the start of its own ending, a
crossfade before its end when that ending is shorter than the crossfade -,
holds that second's sound (`_frozen`: its averaged magnitude spectrum played
with seeded, channel-shared random phases, overlapped, at its loudness),
crossfades it in over `PAD_CROSSFADE_SECONDS` (1) and fades it out over the
last `PAD_FADE_SECONDS` (3), a 32-bit float WAV. A pad not made leaves the
take playing (`kept_take`, the reason "the pad: ..."). It is offered
(`pad_offer`) by the save's `music_short` (play-once) and `music_outgrown`
(looping) findings and `length_note` when an AI track ends less than
`PAD_PROPOSED_SECONDS` (3) before the video, and by an extension refused for
want of a continuing model or of credits.

**Which music a render was made with.** A refit keeps the track's
id and the video's revision, so the current render looked current while its
notes ("loops at 167 s") described the previous take. A render records the
music it played (`VideoRender.music`, migration `v6072music01`,
`music.played_record`: the bed's source and, for an AI track, the file it
was read from; `{"kind": "none"}` for none; NULL for an older render), served by
`serialize_render`. `music.render_music_note` compares it with the music the
video plays now - the same source and file, an earlier take (being composed
again, or composed again since), another source, music no longer played, or
a render that predates the record - and `projects.render_music_line` says it
for the current render when it is ready: in `save_spec`'s notes, in
`get_video`, and in a refit's answer. A render that predates the record
says it cannot tell once, from the save that changes what music plays
(`projects._music_source` before and after, or another template),
not on every save and read until the video is rendered again. The Video Production
skill composes the music last, once every shot is made. The planned
length (`projects.estimate`, under `measured_plan`) plans each clip scene's AI shot at its
file's length once made and at the length it was asked for meanwhile
(`preview.planned_clip`, the preview's rule): planned at the planner's
4 s, eight shots of 5 to 8 s made a track some 20 s shorter than the render. A
render refuses an AI track still composing or failed (`music.ready_file`),
like an AI shot.

### The bed

`music.Bed` is what decides the bed: the source, the video's length, start,
loop, ending, fades and, for a built-in track, the cues. `music.make_bed`
prepares it as a 48 kHz float WAV at `encoder.REFERENCE_LUFS` (-18):

- a built-in score rendered for the length and the cues, then levelled;
- a file fitted by `encoder.bed_segments` (from `start`; a shorter track
  repeated with equal-power crossfades; `ending="natural"` edits the middle
  out under a crossfade so the track's last bars land on the video's end, or
  trims the first repeat when it loops) and `encoder.bed_filter`, its
  integrated loudness measured (`measure_loudness`, EBU R128) and brought to
  the reference (`gain_to_reference`, bounded). A file asked to play once
  (`loop: false`) that is shorter than the video from its `start` is never
  looped: `bed_filter` ends its fade out where the
  music ends (`joined_seconds`) and pads silence to the video's length; the
  render's note (`bed_length_note`) and the save's `music_short` say where
  it fades out and how much of the video plays without music. Each segment
  of a looping bed reads an input of its own (the track given once per
  segment, `prepare_bed`): on ffmpeg 7.1 (the platform image's Debian
  build), an `acrossfade` fed by two branches of one `asplit` gave silence
  after the first segment, so a looping bed went quiet after its first pass.
  `BED_VERSION` 2.

### The mix

- **Voice chain** (`encoder.condition_voice`, `VOICE_CHAIN`): the speech
  engine's leading and trailing silence trimmed, a 75 Hz high-pass, a
  de-esser, a 2.5:1 compressor, then the clip measured and brought to the
  reference loudness: every provider and every scene at one level, and a
  narration that starts on its scene.
- **Ducking** computed from where the narration is (`TimelinePlan.
  narration_spans`, the measured voices; the clips that keep their sound too:
  `duck_windows`), never heard from it: `merge_windows` bridges gaps under
  1.1 s, the bed goes down `DUCK_LEAD` before the first word and back up
  `DUCK_RELEASE` after the last, by `duck_db` (`duck_expression`, an ffmpeg
  `volume` expression evaluated every 5 ms). `duck_points` gives the same
  envelope as breakpoints for the editor (they are where the expression bends,
  read from it).
- **A clip's own sound** plays by one rule, `spec.clip_sound_choice(scene,
  spec)`, which the planner, the preview and the tool results read: the
  scene's `audio` (`true` / `false`) decides, else the video's `clip_sound`
  (`keep` / `off`), else (`auto`) an AI shot under a music bed
  (`Music.plays`: a source, or a `prompt` the save composes) is silent and
  everything else keeps its sound. A shot made for a video whose
  `clip_sound` is `off` (`video_id`, its template applied), with `sound`
  unset, is asked silent and its track dropped at ingest like `sound: false`
  (`clips.video_drops_clip_sound`, `params.silent_for_video`, its notes saying
  why). The tool results' `Sound:` line (`spec.sound_summary`) lists the
  clips whose shot has no sound track as having none
  (`projects.silent_clip_scenes`) and names the scene's `audio: true|false`
  when it decides. `TimelinePlan.clip_sound` records the
  outcome per clip scene, `silent` for a clip without sound;
  `TimelinePlan.clip_audio` holds the `ClipSound`s that play, their fades set
  to the transition into the scene and to the overlap with the next one (at
  least `CLIP_SOUND_FADE` / 0.3 s). The render measures the seconds played
  (`measure_loudness(start, seconds)`) and levels them with `clip_gain`:
  toward the reference, a cut of up to 18 dB and a lift of at most
  `CLIP_MAX_LIFT_DB` (6), so a quiet ambience stays one. `volume` is then the
  level against the voice. Under the narration (`voice_windows`), the clip goes
  down by `CLIP_DUCK_DB` (10) with the music's envelope, computed on the
  clip's own clock (`clip_duck_windows`) before its delay. Under a narration
  the voice is on top, the clip's sound under it, the bed under both: the bed
  also ducks under every clip sound that plays (`duck_windows`).
- **Master**: the mix to float, its loudness measured (`loudnorm_measure`),
  then one linear gain to -16 LUFS / -1.5 dBTP (`master_filter`): two passes,
  no pumping.

### How loud it plays

Levelling a bed to the reference says nothing of how the track sounds in the
mix: an AI track mostly silent, a long quiet intro, a `volume` and a
`duck_db` that lose the music under the voice were heard on playback only.
`music_level.py` measures every track - built-in, uploaded or AI - on the
bed `music.make_bed` prepares, from its momentary loudness: the BS.1770
gating blocks (400 ms, one every 100 ms), read from ffmpeg's `ebur128` frame
log (`encoder.momentary_loudness`). `music_level.measure` derives, on the
master's scale:

- **integrated**: the music at its `volume` where no one speaks, gated as
  BS.1770 integrates (`music_level.integrated`: -70 LUFS, then 10 LU under
  the gated mean);
- **under the voice**: the same with the ducking envelope the mix applies
  (`encoder.duck_points`, read over each block), over the blocks wholly
  inside a narration, and how far below the voice it is;
- **the longest quiet stretch**: consecutive blocks under `QUIET_LUFS` (-35).

The master's gain is the mix's rule applied to what is known: the voice
(each narration at `REFERENCE_LUFS` times `voiceover.volume`, where it is
said) plus the ducked music, brought to `MASTER_LUFS` - a clip's own sound
left out. A video with no narration has its music as its master, so only
its quiet passages can be quiet.

`MusicLevel.problems` names what the audience would miss, each with what to
change: `silent` (nothing above -70 LUFS), `quiet` (integrated under -35
LUFS: a track too quiet to level, a volume too low), `quiet_stretch`
(`QUIET_SECONDS`, 6 s or more under -35 LUFS, with its times: start the track
elsewhere or choose another; a quiet stretch inside a too quiet track is not
said twice) and `lost_under_voice` (more than `LOST_UNDER_VOICE_DB`, 28 dB,
under the voice once ducked: raise `volume` or lower `duck_db`).

**The closing window.** A bed whose `ending` is `fade` decays under
the video's close: the track's own end, the bed's fade out and the fade to
black land together under the last card and the outro. `music_level.
closing_seconds` gives the window - the outro card's length (the planned
document's last scene, when it is an `outro`) plus `music.fade_out`, at most
`CLOSING_MAX_SECONDS` (8 s); 0 for a `natural` ending - and `measure(closing=)`
sets apart a quiet stretch that STARTS inside it and lasts `QUIET_SECONDS` or
more: `MusicLevel.decay`, never `quiet`, so never a `quiet_stretch` (a shorter
one, the last instant of any fade, is counted as before and the line reads as
it did). The `music` section says it
(`decay_seconds`, `decay_from`, `decay_to`, `closing_seconds`), the `Music:`
line adds "The track decays under the closing card, by design" - a fact of the
render's answer - and the editor's Renders panel says it too. A stretch that
starts before the window, whatever it runs into, and one mid-track beside the
decay are still findings. The render computes the window from the plan it
mixed (a scene preview's window plan: its own bed fades at the window's end),
the save from its plan (`PlannedMusic.closing`). Measured on a 144 s AI track
with a soft intro and its own 8 s decay under a 147 s video with a 6 s outro
(window 8 s): the bed loops into the intro and fades over the outro, quiet from
140.0 s to the end (7.0 s) - a warning before, a fact now; the intro's own
4.8 s at the start stays its longest quiet stretch, under the 6 s threshold.

- **In the render check** (`render.check_render(music=...)`): the bed the mix
  took, the narrations actually voiced; a scene preview measures its span,
  timed from its MP4, the master staying the whole mix's. The audio check's
  `music` section and `music_level` warnings (`render_checks.MUSIC_LEVEL`),
  never a failed render; the editor's Renders panel shows the line.
- **At save** (the quality check that follows it, `quality.check_content`):
  `music_level.planned_music` reads the bed the save's plan gives the music
  (`music.Bed.of`, the paced narration's windows), and once the database
  session is closed `measure_planned` prepares it as the render will - a
  built-in score composed and kept in the render cache's `beds`, a file
  downloaded and fitted - and keeps its momentary loudness under the bed's
  digest (`render_cache`, kind `loudness`), so the next check of the same
  bed reads it back. Bounded by `SAVE_MEASURE_TIMEOUT_SECONDS` (25 s): a bed
  not measured in time says nothing and the render measures it. A track still
  being composed or gone is not measured (its own findings say why). Each
  problem is a `music_level` advisory on the scene where it starts: the
  render gate never refuses on it, and reads the quality check without
  measuring (`check_video_quality(measure=False)`).

### Preview

`preview._music` returns the bed's URL, its volume and its duck points, or a
`state` (`music` being composed with its progress, `failed` with the reason,
`none`); the editor polls while a track is composed. The URL is
`GET /video-studio/music-bed/{token}`: a JWT of scope `video_studio_music`
(`media.mint_music_token`) carrying the bed's claims and the reader; the route
checks again that the reader may use the source, then serves
`music.preview_bed` (AAC, prepared once per digest, kept in a bounded disk
cache of the API, `FileResponse` byte ranges). `GET /music/library` gives
each built-in track an audition URL (32 s with its default arc). The editor
(`MusicSettings.tsx`) applies the duck points to the `<audio>` element's
volume (`duckGainAt`). A clip scene's payload carries `preview_sound`
(`{plays, reason}`, the reason being `clip_sound_choice`'s, or `silent`; a shot
not made yet shows what will decide) and, when it plays, `preview_audio`
(`{volume, duck}`: the clip's envelope under the paced narration, which the
compositor page applies to the video element, `envelopeAt`). An upload is not
probed before the render: the preview plans it with sound and its video
element plays whatever it has. The inspector words the reason under the
scene's sound choice (`SCENE_SOUND_REASONS`), the timeline marks a clip
whose sound plays, and the tool results end with `spec.sound_summary`.

### Templates

`extra_metadata.video.defaults.music` presets a brand's sound: a built-in
`track` (required) and its levels. A video naming its own source keeps it
(the template's track is dropped before the merge), `enabled: false` plays
none; `voiceover.pronunciations` can be preset the same way.

## Music generation

Music is an LLM-provider capability (`app/llm/music.py`):
`BaseLLMProvider.list_music_models` / `generate_music(MusicRequest)` with a
provider-declared `default_music_model`. ElevenLabs (`music_v2_5`, `music_v2`,
`music_v1`; `POST /v1/music`: `music_length_ms`, `force_instrumental`) and
Gemini (Lyria over the Interactions API; `lyria-3.5` takes its length and "no
vocals" in its prompt, `lyria-3-clip-preview` always makes 30 s) implement it.
`router.is_music_model` reads the family prefixes (`lyria`, `music_v`,
`music-`, `stable-audio`), which `is_chat_capable_model` refuses (the catalog
lists Lyria as `chat`); the gateway types them `music`.
`router.get_music_provider` resolves the caller's choice, then
`PlatformSettings.music_provider_id` / `music_model` (Settings > AI models >
Music generation, `/settings/available-music-models`, migration
`v5915music01`), then the first provider with a built-in model (a chatless
one first), among the providers the person may use; deleting the provider
clears it with the other platform pairs.

**Continuing a clip.** `MusicRequest.continue_from` (a
`MusicReference`: audio, mime, seconds) asks for the music that follows a
clip, from where it ends, `duration_seconds` being the continuation's length;
`MusicResult.continues_at` says where in the answer the new music starts (a
provider that replays the clip first gives its length). Which models take one
is the provider's to say, per model (`BaseLLMProvider.continues_music`, `False`
by default), served as `continues` by `router.list_music_models_for`.
ElevenLabs says it for `music_v2` and `music_v2_5`: the clip is uploaded
(`POST /v1/music/upload`, `song_id`) and composed by a composition plan
(`_continuation_plan`): an audio reference chunk replaying the clip (its last
2 min at most) unchanged, then generation chunks of 2 min at most - the first
conditioned on the clip's last 30 s (`conditioning_ref`, `condition_strength`
`high`), the last an `[Outro]` - their styles the prompt's phrases, an
instrumental request as an `instrumental` style and a `vocals` negative one
(a plan takes no `force_instrumental`). ElevenLabs serves inpainting to its
enterprise plans: a refusal is the provider's reason, and the Video Studio
leaves the take playing. Lyria takes text and images only: none of its models
continues a clip.

## The editor

`/videos/:id` (`pages/VideoEditor.tsx`, outside AppLayout like the deck
viewer) is the Presentation Studio's counterpart: the studio rail on the left,
the preview and the timeline in the centre, the inspector on the right.

A reader (no `can_manage`) gets what a read-only deck gets:
the editing chrome is not drawn rather than drawn disabled. No rail, no
inspector, no quality check (`useVideoQuality` is not even asked), no
Render / undo / redo / Studio / Share / public-link globe, the title a plain
heading, `VideoDownloadMenu` only once a render is ready, and the timeline
without its editing tools (the tracks, playhead and zoom stay). The modified
editor keys return early so the browser keeps its shortcuts, and
`useVideoEditor`'s `undo` / `redo` refuse like `edit` does. Once the video
has a current render (`ready_render`) a reader watches it instead, as
the public link does (`RenderedVideo`: the studio's player across the stage on
the render, its captions when not burned in, its length, size and date under
it): no preview is asked
for (`useVideoEditor(video, { preview: false })`) and no editor key is bound,
so the player keeps its own. Until then the preview stays, under a line saying
it is the edit's. Pinned by `pages/VideoEditor.test.tsx`.

The bar's primary verb is `Play`: the current render in the theater
(below), for an editor and for a reader alike once a render is ready; an
editor sees it disabled until then, saying why, and a reader does not see it.
`Render`, the outlined verb beside it, opens the `Renders` tab. Opening the
theater pauses the preview's transport.

### The player and the theater

Every place a video plays uses one player, `CinemaPlayer`: our controls over a
native `<video>`, never the browser's `controls`. The browser keeps what it
does well (decoding, the byte-range seeking it asks the media routes for, the
timing of the WebVTT cues, on a track it keeps `hidden`); the player draws the
captions inside the picture, in the band the scenes keep clear, lifted over
its controls and title bar while those are up, and draws
play / pause, 10-second skips, a scrubber (the buffered range, the time under
the pointer), volume and mute (remembered across videos in `localStorage`),
captions on / off when the render has a file for them, the speed, loop,
picture-in-picture and fullscreen. It sits on a forced-dark subtree (`data-mode="dark"` +
`dark`, as the presenting deck), on the design system's darkest primitive,
with the scrim tokens behind its controls. While it plays, the controls and the
pointer leave after the deck's `IDLE_HIDE_MS` (`deck-idle.ts`) and come back on
movement; they stay while hovered, focused or while their speed menu is open
(`cinema-player.css`, the deck bar's rule). Its keys, answered while the focus
is in it (`player-model.ts`, `playerKeyAction`): Space / K, Left / Right (5 s),
J / L (10 s), Up / Down (volume), M, C, F, 0-9 (a tenth of the length each),
Home / End, < / >, `,` / `.` (a frame at the render's `fps`, paused) and `?`
(the key sheet, `PlayerShortcutsDialog`, the deck sheet's form). A modified key
is the browser's; Space or Enter on a focused control presses that control, and
the key sheet keeps its own keys. What a key did is shown on the picture for
`FLASH_MS` and said in a `role="status"` region. On a touch screen a tap waits
`DOUBLE_TAP_MS` to be sure it is alone; two on the left or right third skip
10 s (`tapSide`).

- **Chapters** (`player-chapters.ts`): a render's `chapters` (the render's
  `timeline.chapters`, see "Outputs") cut the scrubber into one segment per
  scene, the hovered one taller and named over the time, the playing one
  named in the slider's `aria-valuetext` and on the chapters menu's trigger,
  which lists them with their start. One chapter, or none, draws the plain
  track. A nameless chapter reads `Scene {n} - <type>` (`SCENE_TYPE_LABELS`).
- **System media controls** (`useMediaSession`): the title, the poster, the
  position, play / pause / skips / seeking, and the track keys moving between
  chapters. The player played last holds them (`claim` changes on each play)
  and a player gives up only what it still holds.
- **Where the viewer left it** (`player-resume.ts`, `resumeKey`): saved every
  5 s, on pause and on `pagehide`, picked up on `loadedmetadata` with
  `Resumed at` and `Start over` for `RESUMED_NOTICE_MS`; forgotten at the end
  of the video and near either end, `RESUME_MAX_ENTRIES` videos kept for
  `RESUME_TTL_MS`. `VideoPlayer` keys it `<kind>:<id>`; the public page, the
  link's first 16 characters and the render's date (never the whole token).
- **A URL that stops answering** (a confined token outlives its 45 minutes):
  `VideoPlayer` holds the URL it started with while it answers, so the token
  query refreshing never restarts the video, and `onSourceLost` drops it and
  mints new ones (`useRenewVideoMedia`); the player goes on from where it was
  once the new URL loads. A second failure within
  `SOURCE_RENEWAL_COOLDOWN_MS`, no URL within `SOURCE_RENEWAL_TIMEOUT_MS`, or a
  failure on the public page shows "The video could not be played." with
  `Try again`.
- **Nothing flashes**: the spinner comes in only once a wait lasts
  (`animate-appear-late`, 0.35 s: a seek, a skip or a quick URL mint shows
  nothing), and an autoplaying player (the theater) is `starting` until the
  `play()` it asks on `loadedmetadata` is answered - the spinner meanwhile,
  the big Play only once the browser refused, never one shown and taken away
  as the video starts.

- **Fullscreen** is the deck's `useElementFullscreen` on the player: the
  browser's, or the viewport fallback where it refuses (an embedding iframe
  without `allow="fullscreen"`). A player that already fills the window
  (`fillsWindow`: the theater, the public page) offers only the browser's.
  `Esc` in the viewport mode is the player's (read live, `presentedElement()`)
  and arrives handled at whatever holds it.
- **Portals**: its tooltips are given the player as `container` and its menus
  render `inline`, since a fullscreen element paints only its subtree and the
  theater sits above every body portal.
- **`VideoPlayer`** feeds it the confined media URLs (`useVideoMediaUrl`) of a
  render, a take or a clip; the public page passes the public link's own.
- **`VideoTheater`** is what every Play opens - the editor's bar, a render of
  the history, a rendered video's card in the library (its Play button and its
  menu's `Play`), a recording or an AI shot of the media library: the video over
  the whole window (a body portal, `role="dialog"`, `aria-modal`), playing at
  once, with the browser's fullscreen asked in the commit of the click (no
  viewport fallback there: the theater already fills the window). The page
  behind is `inert`, the focus comes back to the opener, the title, the
  subtitle (`renderCaption`: quality and revision) and a render's downloads sit
  on top, and `Esc` or Close leaves - after the browser's own fullscreen, and
  after a menu or a tooltip of the player, whose `Esc` arrives handled.
- **Where it plays inline**: a reader's stage in the editor (filling it,
  letterboxed) and the chat card (16:9 box). The public page
  (`/public/videos/:token`) is the player itself, full window, its title, the
  public mark and the download on top of the picture.

Pinned by `components/video-studio/player-model.test.ts`,
`player-chapters.test.ts`, `player-resume.test.ts`, `CinemaPlayer.test.tsx`,
`VideoPlayer.test.tsx`, `useMediaSession.test.tsx`, `VideoTheater.test.tsx`,
`pages/VideoEditor.test.tsx` and `backend/tests/test_video_studio_chapters.py`;
`test_frontend_both_studios_share_one_top_bar` holds `Play` as the bar's one
primary verb and `Render` as its outlined one.

### Live preview: the render's compositor in the browser

`preview.build_preview` plans the DRAFT with the render's own planner
(`timeline.plan`): the template applied (`resolve_for_spec`, `apply_template`),
the references checked as on a save, the theme resolved as the render does
(`resolve_template_theme`, `build_theme`). What only a render knows is stated,
never invented: an image's size is
the page's to fit (`preview_unsized`), a deck slide (rendered at render time),
a recording still recording or an AI shot still generating, and one that
failed (with its reason: never a 422 for the whole draft) carry a
`preview_state` the editor words in the reader's language (`preview_label`);
a statement whose backdrop shot is not ready shows the brand backdrop and
says so (`backdrop: true`). A recording or clip scene carries `preview_name`
(the take's name, the shot's name or prompt): the timeline names a scene by
its title, its first line, then that, and the type ("Recording") only last,
so a made recording never reads as one being recorded. The editor polls the preview every 5 s while
something is being made, every 20 s while a shot is failed (the assistant may
generate it again from the rail), every 10 min otherwise (fresh tokens), and
at once after `Generate again` or a lost media URL.
The document carries `preview_media`: confined media URLs
(`media.mint_media_token(windowed=True)`: issued at the start of the
15-minute token window, so every plan within it gives a file the same URL) for
the recordings, clips, images, logo and mark, and `poster/<scene>` for the
poster of the take a recording or an AI shot plays (none before it has one);
and `preview_media_ids`: the file (or the figure still's digest) behind each
key (`preview._PreviewMedia`). The music bed and each spoken line carry a
`key` too (the bed's digest, the narration's digest).

**The narration's voice.** The preview speaks from the render's own
voice cache. `preview._voice` resolves the speech target as the render does
(`voice.resolve`), takes each narration with its neighbours
(`voice.lines_of`, which the render reads too) and its cache digest
(`voice.digest_of`, the digest `voice.synthesize` uses), and reads its
length from the cached WAV's header (`voice.spoken_seconds`: one ranged read
of the object, `wav_seconds` over the `fmt ` byte rate and the `data` size,
remembered per process). A spoken line is planned at that length
(`PlanMedia.voices`, so the scenes, the captions and the music's ducking
follow the real voice) and carries `audio_url`: `GET /video-studio/voice/{token}`,
a JWT of scope `video_studio_voice` naming the digest and the reader
(`media.mint_voice_token`, windowed like a bed's so the players keep their
source), which serves an AAC copy prepared once per digest on the API's disk
(`voice.preview_audio`) and never speaks anything. A manager's draft
(`speak=True`: the editor's `POST /preview` with a draft, AI on)
has its missing lines spoken in the background (`voice.prepare`): a Redis
`SET NX` per digest (`xtm:video-voice:speaking:*`, 180 s) keeps it to one
replica, three at once per process, as the person (`set_llm_caller`), and a
failure is kept 10 min (`xtm:video-voice:failed:*`) and shown on its line
instead of being retried at every poll. The payload's `narration[]` says per
line `ready` / `speaking` / `failed` / `none`, `voice` the voice-over's state
(`on`, `off`, `unavailable` with the reason) and level; the editor polls while
a line is `speaking`, `PreviewStage` plays each ready line's `<audio>` while
the playhead is inside it (`spokenOffsets`), at the voice-over's volume, and
the timeline draws a line solid once spoken, dashed until then, pulsing while
spoken, warning when failed. A reader's preview speaks nothing; the render
then finds every line heard in the editor already spoken.

The frame is `GET /video-studio/preview-page`: `compositor_page.page_html`
with the bundled fonts, built once, public like a static asset (it carries no
data), framed by the platform only (`register_page_policy(...,
frame_ancestors="self")`, its inline script hash). It is served `no-cache`
with the ETag of its content (a 304 when unchanged): a page cached for an
hour drew new templates with the previous release's compositor. With `preview_media` set the
page's preview mode (`S.media`) plays recordings and clips from `<video>`
elements: paused, every frame seeks exactly and `render()` waits for it;
playing (`__xtm.setPlaying`), the videos run at the time map's local rate
(`mapRate`) and are corrected only past a visible drift; a scene off screen is
paused, a clip with sound is heard (`setMuted`). A render leaves `S.media`
null: the page draws the extracted frames exactly as before, and `load()`
drops the previous document's layers at once.

**A preview that never blinks.** The editor plans the preview again
every 5 s while anything is being made and after every save of the
assistant, and each plan is a new document. Rebuilding the stage from
nothing showed every shot's layer as its bare theme background until its
video downloaded again (its URL carried a new token), several seconds at a
time. So, in preview mode:

- `PreviewStage` loads only a document that changed (its JSON), and the
  playing loop reads the document, the music and the narrations through refs:
  a plan landing while playing changes no clock and restarts nothing.
- A key plays the URL first minted for its file while it answers
  (`heldUrl`, keyed by `preview_media_ids`, for 20 min at most), so a newer
  token of the same file loads nothing again.
- The videos outlive a document (`S.pool`, by key and file): the next
  document's layer takes the element as it is, loaded, at its time.
- `loadDoc` keeps the previous layers on screen (`S.retired`) and draws the
  frame under the playhead from the new document before the browser paints
  again; the media only the old document played are released after that swap.
- A new file under a key (a new take), a lost URL or one held too long gets a
  new element behind what is on screen: the element that showed the key (kept
  in step, silent), else the shot's poster, stands in front until the new one
  can draw (`cover`, `READY_EVENTS`). A picture shows the one its key showed
  until the new one is decoded (`setAsset`).
- The scenes starting within 6 s of the last one on screen are built hidden,
  their videos at their first frame (`prepareAhead`), and a layer is inserted
  in the scenes' order whatever order it is built in (a seek back), so a scene
  always comes in over the one before.
- Playing, `render()` waits on no load; paused, it waits for the seek of what
  it shows. `render()` waits for a `load()` in flight (`S.job`).
- A URL that stops answering (an element's `error`) is reported once
  (`__xtm.onSourceLost`); the editor plans again at once (`refreshPreview`).
  The music and the narrations keep the URL they play the same way, by their
  `key`, until it stops answering.
- Only an edit shows "Updating the preview" (`useVideoEditor` compares the
  draft it last planned), and only once its plan lasts (`animate-appear-late`); a quiet preview is planned again every 10 min so
  its tokens (45 min) never run out on the stage, and a hidden page skips its
  polls and plans once when it is seen again.
- A recording's viewport (`.vp`) paints no fill under its footage, and the
  preview's `<video>` fills it (`object-fit: fill`) as a render's frame
  `<img>` does. The camera's zoom puts the footage's edge between two
  pixels on most frames, and the browser blends both layers on that edge: the
  white fill it had leaked through it, a seam lit and gone frame after frame
  along the window's border, and a source a few pixels off the viewport's
  aspect was letterboxed on it, a white line on one edge.
- The timeline's thumbnails are the preview's `poster/<scene>` and
  `image/<scene>` URLs: no media-token request per scene, none for a shot
  without a poster yet (those answered 404). A refresh of the studio
  (`invalidateVideoStudio`: a save of the assistant, a share) never re-mints a
  media token; `useVideoMediaUrl` takes the file it reads (`file`), so a shot
  generated again reads its new poster.
- A poster is the footage as it was shot, so a thumbnail draws the scene's
  screen inserts on it: each one's source - a picture, a video at the
  frame it comes in on - laid on its quad with the stage's arithmetic
  (`video-editor-model.ts`: `coverRect`, `insertPlane`, `quadMatrix`, the
  compositor page's), on the rect the poster covers in the block. A
  thumbnail keeps the URL it shows while it plays the same file
  (`holdMedia`, by `preview_media_ids`) and takes the plan's newest once that
  one fails: an inserted `<video>` handed the next token window's URL
  would load again, blank meanwhile.

`test_video_studio_preview_continuity.py` drives the page in Chromium: the
same media across plans (the very same element, never a task leaving the
stage without its scene), a new take behind the old one, a poster, the next
scene loaded ahead, the scene order, a lost source reported once, a picture
swapped once decoded, a recording's window edges read frame by frame while
the camera zooms.

### Drafts, saves, history

`useVideoEditor` keeps the authored spec as an undo history (a run of
keystrokes in one field is one step), saves it whole through `PUT
/projects/{id}/spec` (the tools' validator) once it settles, and adopts a
revision someone else saved (the assistant in the rail) when nothing is unsaved
here - as an undo step. Edits are pure functions of the spec
(`video-editor-model.ts`, tested by `video-editor-model.test.ts`): a cut
(`splitScene`) lands on the frame under the playhead, a recording or a clip cut
in its SOURCE through the planned `time_map` (idle compression and speed
included). The playhead is a store (`playhead.ts`): playing re-renders the
stage, the clock and the timeline's playhead only. The timeline draws it, and
the skim line, in whole device pixels (`playheadMarks`, followed through
`useDevicePixelRatio`): a `1px` line is 1.25 or 1.5 device pixels on a
125 % or 150 % screen, which the browser snaps one pixel wide then two by turns
as it moves (a blinking playhead), and its head is the line's width plus a side
on each hand, so its middle column is the line's.

### The studio rail

`components/studio/StudioRail.tsx` is the Presentation Studio rail made
generic: a `StudioSubject` gives the binding (`presentation_id` /
`video_project_id`), who can do the job (`useToolAgents`), what each message
says about the view (`studio_slide` / `studio_scene`), the words and the
suggestions. `PresentationStudioRail` and `VideoStudioRail` are the two
subjects. Backend: `Conversation.video_project_id` (no foreign key, no index:
the studio lists the caller's own conversations, a deleted video leaves an
ordinary chat), `PUT /chat/conversations/{id}/video`, and
`video_studio.studio.load_video_studio_context` per turn. The chat loops read
any studio context through `tool_ids` and `prompt()`, so `ensure_studio_tools`
guarantees `video_studio` and `template_search` (a brand's pictures) on a
video studio turn. The video tools emit `VIDEO_SAVED_EVENT` after their
commit; the stage refreshes on it. A new video's first message is sent by the
rail itself (`autoSendMessage`): the "New video" dialog shows the
request before creating the video, and the editor clears the launch state so
a reload never sends it twice.

### Creating a video

`NewVideoDialog` asks for one brief. The kind of video is optional
(`VIDEO_KIND_GROUPS`): a creative kind (`fun`, `story`, `howto`,
`greeting`, which no template is made for) or one for work (the template
purposes). It opens the request (`VIDEO_KIND_REQUESTS`, translated) and offers
its example brief (`exampleBrief`). The look and the format are derived in
`useNewVideoChoices`, never written by an effect: what the person chose, else
the template a template page opened the dialog with, else the one made for
the kind (`templatesForKind`) and its format (`VIDEO_KIND_FORMATS`), else
free style - the platform default template is never pre-selected. A kind never
overrides a chosen look or format, and unpicking it takes its suggestion back.
The request (`newVideoRequest`: the kind's sentence, then the brief) is shown
under `{assistant} receives`; `Create with the assistant` creates an UNTITLED
video (the title is the assistant's to write, never the brief) with the look
and format and opens the editor with `{send, agentId}`; `Start from a blank
video` creates the same look and format with a title card.

A free-style video stays free style: `templates.keep_look` (called by
`projects.save_spec` and the draft preview) sets `freestyle` again on a whole
new spec that names neither `template_id` nor `freestyle`, as
`resolve_for_spec` keeps the template a templated video wears. The cards a
built-in template adds stay on screen long enough to read: a video just
created reads clean on every built-in template and in free style
(`test_a_video_just_created_reads_clean_on_every_built_in_template`, Chromium).
The craft lives in the `video-production` skill
(`seed_data/skill_content/video-production.md`: kinds and arcs, statements,
AI shot prompts, footage, recording the product, explainers from data, sound,
transitions, acting on each save's quality check, waiting for long jobs,
recovering a failed shot, the review, rendering and delivering), bound to the
General Assistant and every default assistant.

### The General Assistant's skills

`GeneralAssistantConfig.skill_ids` (migration `gaskill5901`) binds
company-managed skills to the Copilot General Assistant, rendered by
`general_chat` with the agent path's helpers (`resolve_agent_skills`,
`build_skills_catalog_prompt`, `build_skill_tools`). NULL means the platform's
choice: the built-in skills declared `bind_to_general_assistant`
(`general_assistant_skills.GENERAL_ASSISTANT_SKILL_SLUGS`: `video-production`,
`presentation-design`), resolved by slug tag, so a release's new one reaches
every assistant whose admin never chose. `PUT /settings/general-assistant`
takes `skill_ids` (only enabled company-managed skills; `null` back to the
defaults) and answers `skill_ids` with `default_skill_slugs` (a constant: the
Settings page finds the built-in skills by their `slug:` tag, no query per
read); the Settings page edits it (`GeneralAssistantSkills`). A General Assistant linked to an agent
answers with that agent's skills.

### Media retention

`retention.py`: a recording or a shot a video uses is never expired,
however long the video is left alone. Once no video uses it, its files are
deleted `VIDEO_STUDIO_MEDIA_RETENTION_DAYS` after the last video using it
stopped or `VIDEO_STUDIO_UNUSED_MEDIA_RETENTION_DAYS` after it was made when no
save ever named it; `keep` exempts one. `last_used_at` is when to look, not
the verdict: `save_spec` stamps the spec it stores and the one it replaces, a
successful render the spec it drew, `DELETE /projects/{id}` the video's last
spec. The hourly cleanup loop runs the sweep (bounded batches); before
deleting, it checks that no video needs the asset (`_referenced`, the media
reference index below, every video, the person's or not) and stamps one a
video uses instead (`in_use` in its counts). `GET /assets` answers `in_use` / `expires_at: null` for an asset
any video uses (`retention.media_in_use`), and lists in `used_in` only the
videos the caller can open; a single read or update checks every video
(`retention.in_use`). The row stays with
`expired_at` (name, poster, script, prompt); the render refuses an expired
source with a message naming it. `DELETE /takes|clips|music/{id}` refuses a
medium any video uses with 409 `media_in_use` (`library.deletion_refusal`:
the videos the caller can open, `others` counted, never named) unless
`?force=true`; a forced deletion is audited (`forced`, the video ids). The
Media tab asks for a typed code (`ConfirmDialog` `challenge`) before forcing. Migration `v5826vstudio01` adds `keep` / `last_used_at` /
`expired_at`, `video_clips.name` and `conversations.video_project_id`.

Which videos use a medium is one indexed lookup:
`video_projects.asset_refs` is the one media reference index
(`retention.media_index`), `"<kind>:<id>"` keys of every medium the video
needs:

- the recordings, AI shots, AI tracks and files its spec names, wherever it
  names them (`spec.asset_refs`, the places `spec.referenced_ids` reads): a
  scene's footage, a screen insert's source and a wall's panes, a
  statement's backdrop, an image scene and its animation, an image overlay,
  a figure's pictures, the logo, the music;
- what the AI shots among them are made from, and theirs in turn
  (`MAX_SOURCE_DEPTH`, 8): the start picture, the recording, shot or file a
  frame was cut from and the frame kept, the reference images, the
  ingredients (`clips.source_refs`, stored on the shot as
  `video_clips.source_refs` by its mapper event; a shot written before is read
  from its params);
- what its ready renders drew (`video_renders.asset_refs`, the spec a render
  drew, written as it ends ready): the revision a ready render plays stays
  needed while the render is, whatever the spec moved on to.

It is written on every flush of the video by a mapper event
(`models.video_studio._index_asset_refs`, reading on the flush's connection);
a shot whose sources change indexes the videos naming it again
(`retention.reindex_users_of`, from the shot's own mapper event); a render
that ends ready marks its video for indexing (`retention.mark_for_reindex`).
The GIN index `ix_video_projects_asset_refs_jsonb_gin` serves it, and
`retention.uses_media` is the one predicate - over every video for the sweep,
`in_use`, the bulk deletion and `library.deletion_refusal`, narrowed to what
the reader may open for `library.used_in` and `projects.can_read_project_asset`
(a shared video's media, however many videos the reader sees). Every file a
Video Studio deletion would remove goes through `assets.forget_files`, which
keeps the files the index lists (`retention.files_in_use`): a shot's poster a
screen insert shows stays when the shot goes. Migration `v5921arefs01` added
the column and its index; `v6164mref01` adds `video_clips.source_refs`,
`video_renders.asset_refs` and `video_projects.asset_refs_version`. A video
indexed by an older rule (`asset_refs_version` below `spec.ASSET_REFS_VERSION`)
keeps the index it had meanwhile - what its scenes play - and is indexed again
(`retention.index_stale_refs`, up to `STALE_BATCHES` batches a call, its
`updated_at` kept), as is a row an older replica wrote NULL: by the scheduler
tick (`autonomous` step 5c-0c, one query once none is left), since a single
deletion's refusal and a shot's file cleanup read the index as stored, and by
the sweep before it purges. The earlier rule did not read a screen
insert's source: a video saved under it and not since kept an index without
the recording its insert shows, which read as unused to every deletion. A
render made before `v6164mref01` has no `asset_refs` - no render stores the
spec it drew, only its revision - so what it drew counts only while the video
still names it.

Failed media: a recording or a shot that failed or was cancelled made
nothing. The sweep deletes it, row included, once no video's spec names it and
`FAILED_MEDIA_GRACE` (1 h after `finished_at`) has passed - the assistant may
still generate it again in its turn - whatever the retention days. The media
library (`GET /assets`) never lists one; the scene inspector reads a scene's
recording or shot by id (`GET /takes/{id}`, `/clips/{id}`), so a failed shot
any video uses - one shared with the reader, or one long left alone - shows
failed in its scene with `Generate again`.

### Paginated libraries

`library.py`: no library is read whole - AI generation fills them by
the dozen. Every list answers `{items, total}`, takes `limit` (at most
`MAX_PAGE_SIZE`, 100) and `offset`, and runs its search and filters in SQL, so
every row stays reachable and a search covers the whole library. A search
matches when every word is in one of the fields, any case (`icontains`,
escaped).

- `GET /projects`: the videos the caller can open (`project_filters`), the
  last changed first; `search` (title, owner's name, the name of a template
  the caller can see), `visibility` (`all` / `mine` / `shared` / `company`;
  `shared` is visible, neither the caller's nor the company's - a video whose
  owner's account was deleted included).
- `GET /assets`: the caller's recordings and AI shots together, newest first
  (`asset_page`: a `UNION ALL` of ids and dates ranked once, then the page's
  rows loaded by id); `kind` (`all` / `take` / `clip`), `search` (name, and a
  shot's prompt), `ids` (comma-separated: a video's own media, whatever their
  age), `exclude` (left out of a library search), `available` (no expired
  media). Each row carries `kind` and `used_in`, read for the page's media only
  over every video the caller can open (`library.used_in`, the ids and titles
  of the videos from the `asset_refs` index - cheap enough for the editor's
  3 s poll while a medium is being made).
- The per-kind lists `GET /takes` and `GET /clips` are gone: nothing
  called them since `GET /assets`, and they read a capped set of videos.
- `GET /assets` also filters by use and by date: `usage` `used` /
  `unused` reads whether any video names the medium, anyone's - the
  retention's rule, so a purge never breaks a colleague's video - row by row
  (`retention.named_by_a_video`, an `EXISTS` over `asset_refs ? '<kind>:<id>'`
  on the same GIN index; the key is grouped, since `?` and `||` share a
  precedence). On 4000 videos PostgreSQL plans it as a nested loop semi join
  (an anti join for `unused`) over the reader's media, each one a Bitmap
  Index Scan on `ix_video_projects_asset_refs_jsonb_gin` - never a scan of
  the videos (`test_video_studio_library_postgres`, which reads the
  `EXPLAIN` of both and of `used_in`'s `uses_media`). The videos written
  since the last vacuum wait in the index's pending list (`fastupdate`), which
  the planner prices into every lookup: right after a burst of writes it may
  scan the videos until autovacuum flushes it; `date_from` /
  `date_to` bound `created_at`, both included, an
  instant without a zone read as UTC. Every row carries `in_use`, so a card
  used only by a video the reader cannot open says so instead of "In no
  video". The filters are one `library.AssetFilters`, shared by the page and
  the bulk deletion.
- `POST /assets/bulk-delete`: the caller's own media named in `items`
  (at most 500, `{kind, id}`), or every one `all_matching_filters` matches with
  `GET /assets`' filters (`kind`, `search`, `usage`, `date_from`, `date_to`).
  `library.delete_assets` deletes exactly what the library lists - a failed
  shot, a colleague's or an unknown id is skipped alike - and never a medium
  any video uses: those are kept and counted (`kept_in_use`), whatever
  `usage` asked, since a single delete refuses one unless forced and
  a bulk deletion never forces. It runs in batches of `DELETE_BATCH` (100),
  each its own transaction with the file rows (`assets.forget_files`), the
  use read again as each batch is read: a video saved before a batch is read
  keeps its media. The read and the delete are no single atomic step:
  a video's save writes its `asset_refs` and locks no medium, so a
  save committed while its batch deletes is not seen, and that video then
  names a medium that is gone - as after a forced single delete. By
  filters nothing made after the deletion started is deleted.
  The storage objects are purged after the answer
  (`file_cleanup.purge_storage_objects`), one `video_media` audit entry
  records the counts. A single delete (`DELETE /takes/{id}`, `/clips/{id}`)
  removes the same files through `library.remove_media`, after
  `deletion_refusal`.

The frontend keeps a tab's search, filters and page in the URL
(`useLibraryParams` in `pages/VideoStudio.tsx`) with the shared `Pagination`
bar above and below the grid; the Media tab adds the use filter, the time
range (`DateRangeFilter`, `lib/date-range.ts`, the audit logs' presets), the
shared `ListControlBar` (select the page, "select all N", Delete) with a
`SelectionBox` on every card, and `Select all unused`, which applies the
`unused` filter and selects everything it matches once its page is in. The
editor's Media panel reads the draft's own media by id (`videoMedia` -> `ids`)
and searches the rest with `exclude` and `available`.

## Surfaces

- **Agent tools** (`builtin_tools/video_studio.py`, catalog `video_studio`,
  in `DEFAULT_GA_BUILTIN_TOOL_IDS` with migration `v5774gadflt02`, and on the
  CTEM Assistant, the XTM One entry point): browser profiles (list, save,
  sign in, submit MFA code, `check_browser_profile_session` - the recording's
  pre-flight, a `session_check` result; `snapshot_browser_profile_page`
  - a page looked at before scripting; over
  `services/browser_profiles`), recordings (record, get,
  list), `video_studio_capabilities`
  (templates first, then providers, voices, clip models with the reference
  images each takes, the built-in tracks
  `music_library`, `music_models` and `ai_music`, qualities),
  `generate_video_clip`, `save_video_frame`, `save_video_frames` (a
  sweep of one video as one contact sheet), `preview_start_frame` (a
  shot's first frame looked at before paying), `compose_picture`,
  `generate_music`
  (`video_id`: composed at its length and made its music; with `refit`, its
  AI track composed again at its length now, and with `extend` the
  take kept and only its tail composed again as its continuation;
  `music_id`: a failed track again in place), `preview_voiceover` (pronunciations over the
  platform's lexicon; the mechanism, the string sent and how each lexicon
  term was heard), videos (create, edit, get, list), `share_video` and
  `set_video_public_link` (`get_video` says who has access and whether
  the link is on), `delete_video_renders` (renders by selector or id with
  their files, `render_cleanup`; `get_video` says each render's size and
  what keeps it), `render_video`, `get_video_jobs` (`music_ids` too; without
  any id, the person's jobs in flight listed at once, `jobs.jobs_in_flight`)
  and `cancel_video_job` (the caller's own recordings, shots, tracks and
  renders, through `jobs.cancel`). Long jobs wait up to the tool
  deadline (`current_tool_deadline`), then the agent follows up; a turn that
  ends first is resumed when they end (the wake, below). Every picture a
  result shows (a contact sheet, a shot's or a render's poster, a sign-in
  screen) is a file of the conversation (`_file_refs`): one the
  conversation does not hold is shown through a working copy it owns,
  deduplicated per conversation, so `read_file_content` opens it after a
  restart and deleting the conversation never takes the media's own picture.
  And each is attached ONCE per conversation: a picture shown
  earlier in the turn (the tools' `shown_this_turn`), by a message of an
  earlier turn (a conversation file with a `message_id`), or through a copy
  the conversation already holds (same bytes, any message) is named
  `[[FILE:id]]` - "Already shown in this conversation, not attached
  again" (`_Pictures.note`) - never re-attached; `get_video_jobs` shows a
  ready shot's poster the first time it reports it made, and names it
  after. Polling a batch of 16 shots no longer costs the conversation a
  poster per shot per call. A run WITHOUT a conversation (an assignment run)
  has no scope `read_file_content` could find those files by: every
  file an answer names - attached or only linked, a render's poster, insert
  crops, findings' frames, check frames, captions, a frame sweep's frames -
  is collected in `_Pictures.every` and put in the run's file context as a
  file it fetched to read (`register_named_files`, through `_refs` /
  `_readable`), so `read_file_content` opens each one in that call and every
  later one of the run, and `get_video_jobs(render_ids)` names them again
  without adding them twice. A ready render's answer names its captions file
  (`captions_file_id`), and `get_video` the latest render's.
- **`get_video` by whole scenes**. The spec is shown whole when it
  fits `SPEC_PAGE_CHARS` (12,000); a longer one is shown as everything but
  the scenes (voiceover, music, format, ...) followed by as many WHOLE scenes
  as fit (one at least, never cut inside one - `spec_page`), then the scenes
  left as 1-based ranges with the call that shows them (`get_video(video_id,
  scenes='18-24')`). `scenes` takes scene ids, numbers and ranges
  (`scene_selection`, an unknown one refused naming the count); `fields`
  (`GET_VIDEO_FIELDS`: spec, scenes, voiceover, music, renders) picks the
  parts, all by default; a partial spec says that `edit_video` works on the
  whole one.
- **The render queue**. A render is queued like any job and says
  where it stands (`jobs.queue_place`). Its lanes (`jobs.LANES`) are the whole
  renders, the scene previews and the recordings
  (`recording_lane`, under "The capture rate" above). One person has at most
  `VIDEO_STUDIO_MAX_RENDERS_PER_USER` (10) renders queued or running across
  their videos (`jobs.renders_in_flight`, `max_renders_per_user`); past it
  `render_video` lists those renders with their state and the way to wait for
  or cancel one, and `POST /projects/{id}/renders` answers 429 naming their
  videos.
- **Job state**. What a tool says of a recording, a shot, an AI track
  or a render is `jobs.job_state`, read from the job's row and nothing else:
  `queued` / `running` / `ready` / `failed`, the percent, the stage, the
  worker's last progress write and, on failure, the reason. Every job table
  carries `progress_at` (migration `v5955jobst01`, after the render's
  `v5951rprog01`), stamped by `jobs.mark` on any progress or stage write; a
  job with one is never `queued` (a render starting another attempt is
  `pending` again and reads `running`, "Starting again: ..."). A queued job
  says where it stands (`jobs.queue_place`: being made, queued ahead, a
  worker's slots), read from the executions the worker claims: the
  executions of its adapter running, and the pending ones due before its
  own - the claim order, a waiting preview kept in its place and a job
  queued again after a lost lease or a retry included. Counting the job rows created earlier
  told a whole render "0 queued ahead" while the previews queued after it
  went first. A running job also says how its execution's lease stands
  (`jobs.job_states` reading `engine.lease_heartbeat`): `lease:
  {last_heartbeat_seconds_ago, expires_in_seconds, attempt, max_attempts}`,
  the last renewal being the holder's last write of the row
  (`engine.last_heard_at`, the reclaim sweep's own rule). The progress write
  and the lease are two clocks: a render can move while its lease is not
  renewed, which is how render `14d6bcbd` drew its figure to 285/588 frames
  and lost its lease. Once more than half the lease passed since the last
  renewal (`LeaseHeartbeat.late`: a renewal missed), the answer says it in
  words (`jobs.lease_clause`), with the attempt past the first, before the
  lease expires. A shot also carries `provider_state` - what the provider
  said at its last poll (`VideoJobState`: `queued`, `in_progress`, `running`,
  or `rate_limited` / `unavailable` while its trouble is ridden out) - and
  `provider_eta_at`, extrapolated from the percent the provider reports (Veo
  reports none). `get_video_jobs` waits once across every kind it is given,
  returns as soon as one job finishes (`jobs.wait_for_jobs`) and opens its
  answer with those fields per job. `generate_video_clip` waits
  `VIDEO_STUDIO_CLIP_INLINE_WAIT_SECONDS` (200) from the call's start, the same
  for every call of a batch and capped by the call's deadline, and opens its
  answer with `{clip_id, status, waited_seconds, completion, reason,
  start_frame_file_id}`:
  `completion: background` names the budget and where the shot is (waiting
  for one of the worker's `VIDEO_STUDIO_CLIP_CONCURRENCY` slots, queued at the
  provider, rate limited). The job tools declare `ToolDefinition.live_state`,
  so the agent loop never answers them from its identical-call cache nor
  counts their repeats in the loop breaker: a replay of its first answer is
  how `get_video_jobs` reported "pending, 0%, Queued" for a whole render.
- **The wake** (`video_studio/wake.py`). A job a chat turn starts is
  owed to its conversation: `jobs.enqueue(owed_to=...)` stamps
  `owed_conversation_id` on the take, clip, track or render (migration
  `v5977jwake01`; `restart_clip` / `restart_music` pass it for a job started
  again, a REST start or retry clears it). A wait that reports a job that
  ended to the conversation it is owed to clears it (`jobs.wait_for_jobs` /
  `wait_for` with `told_to`, `jobs.mark_told` for `get_video_recording` and
  for the agent loop's `wait`), and so does `jobs.cancel`. A job id is a valid
  `wait(until=...)` id: the wait's condition
  (`builtin_tools/wait.watched_job_statuses`) reads it through
  `jobs.watched_jobs`, under `jobs.may_read_job` - the one read rule, which
  `get_video_jobs` uses too: a render through its video's visibility, the
  rest by their owner -, shows it on the wait card (kind `job`, its name, its
  state from `jobs.job_state` in the wait's words) and ends the pause when it
  ends. The Video Studio adapters call `wake.schedule` when a
  run returns and on the dead letter: after `WAKE_GRACE_SECONDS` (15, longer
  than a waiting tool's 2 s poll, so a turn still waiting reports first), and
  once no other job owed to that conversation is queued or running
  (`jobs.owed_jobs_in_flight`: the last of a batch wakes it, once),
  `jobs.claim_owed_jobs` takes every owed job that ended ready or failed (one
  conditional `UPDATE ... RETURNING` per kind: two wakes never both claim)
  and `parent_wake.wake_conversation` resumes the conversation with their
  outcome as `get_video_jobs` words it for the requester (the render card's
  marker, a failed shot's recovery) and the files it shows: the task wake's
  hidden platform message and turn, folded into a turn already running, and
  dropped when a Stop was recorded after the first of them started. The tools
  that hand a job to the background say the conversation is resumed (only in
  a chat turn). The wait runs in the worker that ran the job: a worker stopped
  during the grace takes that wake along, never the job or its card, and
  `wake.sweep_lost_wakes` (`VideoStudioWakeSweepAdapter.periodic`, every
  `SWEEP_INTERVAL_SECONDS` per worker) resumes a conversation still owed jobs
  that ended more than `SWEEP_AFTER_SECONDS` ago and none in flight
  (`jobs.conversations_owed_ended_jobs`, within `SWEEP_WINDOW`), through the
  same claim. Until the conversation is resumed, the chat shows what it waits
  on: `jobs.owed_jobs_pending` behind `GET /chat/conversations/{id}/awaited-work`
  (`dev-docs/chat-turn-resilience.md` section 6).
- **REST** `/api/v1/video-studio` (`api/v1/video_studio.py`): projects
  (paginated list, create, duplicate, spec / operations, preview, renders,
  sharing, public link), renders (keep, make current, delete), `templates`,
  `GET /assets` (the paginated media library), takes
  and clips (rename, `keep`; `POST /clips` generates a shot, AI-gated, into a
  video or a new one; `GET /clips/start-frame` previews its first frame;
  `POST /clips/{id}/retry` generates one again in place,
  AI-gated, with the fields of `clips.RETRY_FIELDS`), music (`GET /music/library`, `GET|POST /music`,
  `POST /music/{id}/retry`, `PATCH|GET|DELETE /music/{id}`,
  `GET /music-bed/{token}`), `GET /voice/{token}` (a narration the preview
  spoke), `GET /figure-preview/{token}` (a figure's still, drawn at a
  save), `GET /figure-build/{token}` (a figure's build for the editor's stage,
  by ranges), `POST /files` (an image, a sound or a video file
  for a video), media tokens, the preview page. Browser profiles have their
  own API, `/api/v1/browser-profiles`, outside the feature flag
  (`dev-docs/browser-profiles.md`). `<video>` cannot send the
  session header and asks for byte ranges itself: `POST /media-token` mints a
  JWT scoped to one file (45 min, `scope: video_studio_media`), and
  `GET /media/{token}` (no auth dependency; the token is the credential)
  answers `Range` with 206, at most 4 MB per answer. A scene preview stores no
  poster: its `poster` asset is its first settled frame, so a player
  never asks for one that is not there. The editor's `useVideoMediaUrl` mints
  each token again `MEDIA_TOKEN_REFRESH_MARGIN_MS` (5 min) before it expires,
  in the background too (`mediaTokenRefreshIn`), and asks a 404 once more
  before it gives up (`retryMediaToken`): a player that loses its URL takes a
  fresh one at once (`VideoPlayer`'s held URL) instead of showing its
  backdrop while one is minted. The public link's routes
  are under `/api/v1/public` (`api/v1/video_public.py`).
- **Frontend**: `/videos` (videos - searchable, filtered mine / shared /
  company, each card's menu sharing, copying the link, downloading,
  duplicating, deleting - media with their retention; a `Browser profiles`
  button opens `/browser-profiles`, and `?tab=profiles` redirects there),
  `/videos/:id` (the editor, whose header is built from the Presentation
  Studio bar's pieces, `components/studio/StudioChrome.tsx`: the back
  arrow, the title, the `Studio` toggle, the loading and unavailable screens,
  `Play` as the labelled primary verb and `Render` outlined beside it,
  and `Share` - `VideoShareDialog.tsx`
  over `ui/public-link-card.tsx`, which the deck dialog uses too - and
  `Download` - `VideoDownloadMenu.tsx`, `video-downloads.ts` - as icon
  buttons at the top right; the render history's keep / make current /
  delete), `/public/videos/:token`
  (`VideoPublic.tsx`, outside `ProtectedRoute`; `lib/public-video.ts` keeps a
  dead session there off the sign-in redirect), the `New video` dialog (one brief,
  an optional kind - creative or for work -, free style unless a template is
  asked for, the request shown and sent as the rail's first message; or a blank
  video), the Admin Objects `video` arm, the
  `[[VIDEO:id]]` chat card (with its downloads), the Work hub card, the template page's video
  rules. Channels rewrite the marker to a link
  (`channels/base.resolve_channel_video_markers`). Every "with your
  assistant" hand-off opens the chat on `useToolAgents("video_studio")`'s
  first maker - the user's default agent when it carries the tool, else the
  General Assistant - the hook the Presentations module reads for
  `generate_presentation`.

## Configuration

`VIDEO_STUDIO_*` settings (`core/config.py`, documented in
`docs/docs/deployment/configuration.md`): ffmpeg / ffprobe paths, recording
and video length limits, job concurrency, the renders one person may have in
flight (`VIDEO_STUDIO_MAX_RENDERS_PER_USER`), compositor pages per render,
the minutes past which a render is split across the workers there
(`VIDEO_STUDIO_RENDER_SPLIT_MINUTES`, 30), how frames are taken
from the page (`VIDEO_STUDIO_FRAME_CAPTURE`, `screenshot`), the CPU
weight of a whole render's lane (`VIDEO_STUDIO_RENDER_CPU_WEIGHT`, 1000), the media and render retention. The public video links
(`video_public_links_enabled`) are a security option; the private-network
switch is the browser profiles' (`browser_private_targets`).

## Tests

`test_video_studio_demo_grammar_6619.py` (no browser, no database:
"Click the layered button: 23 entities, 78 relationships" said three times
naming the words, an instruction told from "open source" or "one click
away", a label its frames show said and the brand's names and the title
not, frames read only for a narration naming something; 9 small-element
steps in 60 s under an automatic camera said as 9 moves, a move in on an
element covering 44% of the page; a 6 s recording under a narration holding
it 14 s said at 0.43x, naming 8 s; a new recording on the Filigran Product
Video template still, crossfading calmly, its callout 3 s, its opening card
centred or left as the spec says; a demo written the skill's way on that
template with no advisory of its grammar), then
`test_video_studio_canvas_pace_6435.py` (no browser: pointer steps
validated, estimated and framed by the camera, a missing node naming the
closest labels; an animation captured at 12 fps said at its step, never a
keystroke's repaint nor a scroll; the warm-up keeping, lowering or refusing;
a slowed scene's blend; a dynamic demo a fifth shorter than the defaults,
its presets under the author's choices and over the template's, a recording
sped up to its narration, the still screen of a 10 s wait under the voice;
`frame.fill` under a frame lock; a scene's own narration speed and the style
presets) and `test_video_studio_canvas_6435_chromium.py` (the
`chromium` CI job: a canvas graph clicked on node B within 2 px after a
hover, right-clicked for its menu, dragged in 24 moves, Control-clicked and
wheeled, the camera zooming toward B; a node not on the graph; a relayout
captured frame after frame, its rate logged and said low only under 20 fps,
at 30 fps where the machine keeps up; a worker short of
the take's rate recording at 1.5 or refusing before step 1; a throttled take
saying `low capture rate at step 1`), then
`test_video_studio_motion_design.py` (the tokens, `precise` equal to
the page's old numbers, every composition on every personality and aspect
valid as elements, the element rules, easings, Lottie checks, the beat grid
of a score and of a detected track, beat-synced cuts and builds, motion
blur's samples and filter, the pre-flight's sub-frames, the capture mode,
the templates' motion) and `test_video_studio_motion_chromium.py`
(the `chromium` CI job: a precise title card drawn as before tokens, an
energetic title letter by letter, every composition under every
personality and aspect with nothing for the audit to say, a frame the same
whatever frame came before it - the page's state exactly, the pixels by
PSNR -, a Lottie seeked to its frame, a motion-blurred render averaging its
sub-frames into a soft moving edge), `test_video_studio_render_gaps_21oct.py` (a page held to its measured
peak and margin, scaled by pixels, threads and inserts; the layout and each
encoder's thread share; the answer's peak, pages and both durations; the
split across workers; a frame chunked into its pipe; a page's caches and the
render process's heap released when a scene ends; frame kinds, rates by kind,
the stage terms, contention, the correction by the last estimates' errors;
a held clip shorter than its narration failing the save-time check; insert
crops outside the transition windows; two videos' previews alternating and
an idle render slot taking the next one), `test_video_studio_render_lane_6339.py`
(the render lane held by its renders and lent previews only, the wait
counted from the last held check, a lent slot taken back and the preview in it
first in its lane; frames timed by scene type, read from renders and previews,
the estimate quoted from the slowest; the drawn digest leaving out a clip's
sound, an insert's legibility and a deck's speaker notes, a render carried
across such a revision and mixing its sound), `test_video_studio_page_peak_chromium.py`
and `test_video_studio_estimate_chromium.py` (the `Video Studio ultra
render` workflow: one page's peak at 2160p held to the committed one; an
estimate within 20% of its render, both on `video_studio_render_fixture.py`),
`test_video_studio_core.py` (spec, time maps, camera, captions, mix graph,
media tokens and ranges, spec edits, profile secrets, URL policy),
`test_video_studio_templates.py` (seeded brands, apply semantics, rule
validation, CSS colours and palettes), `test_browser_profile_totp.py` (RFC 6238
vectors), `test_browser_profile_signin.py` (session status from probes only,
timeouts, renewability, the saved `sessionStorage`, TOTP steps, page
comparison, the tool contracts), `test_browser_profile_signin_chromium.py` (a
mock application behind a mock identity provider in a real Chromium: TOTP,
a challenge answered, a code refused once, the interstitial, a relay that
does not post itself, number match, a slow and a stalled callback, a session
holding only the IdP's cookies, a recording stopped or renewed before step
1; the `chromium` CI job), `test_video_studio_editor.py` (the compositor's preview mode, the
preview page and its policy, planning a draft with media still being made,
retention, the studio binding, the editor routes), `test_video_studio_mixed.py`
(statements and backdrops, failed shots that save and preview, generating a
shot again in place, the Video Production skill and its scenes, the General
Assistant's skills), `test_video_studio_quality.py` (grading the audit, the
report, animated stills, captions shown once, the watermark),
`test_video_studio_layout_chromium.py` (the layout engine in a real Chromium,
each scenario failing when its mechanism is switched off; the `chromium` CI
job), `test_video_studio_gradient_text_chromium.py` (text drawn in the brand
gradient keeps its whole glyphs - descenders, a ring over a capital, the hook
of a j, the tail of an f - on every heading font a built-in template sets;
the `chromium` CI job), `test_video_generation.py` (the request
shapes, readable and retried provider errors, retired models), `test_video_studio_music.py` (Eleven Music
and Lyria requests and refusals, music models never chat, the platform's
choice, the music spec and its template defaults, the scores: tempo, final
chord, sections, determinism, every style across a chunk boundary; beds,
ducking envelope and the master, pronunciations, AI tracks, the preview, the
tools; with ffmpeg: a bed and the master at their loudness, narrations
trimmed and levelled), `test_video_studio_clip_sound.py` (which clip plays
its own sound and why, the plan's fades and windows, the clip's level and
envelope in the mix graph, the preview's reasons and envelope, the sound
summary; with ffmpeg: the dip under the voice measured),
`test_video_studio_voiceover.py` (a render and an AI track made as their
person with no service identity, the `Voice-over:` line, a preview that never
pins the platform's model; `test_video_studio_clip_runs.py` holds the AI
shot's), `test_video_studio_brand_film.py` (start pictures, silent
shots, text drift, a ready shot again, frames kept as pictures, pictures as
conversation files, one-of edits, the planned transitions, the render
bound, jobs listed and cancelled, voice styles, skipped steps, slides
without chrome), `test_video_studio_render_supersede_postgres.py` (a new
render cancelling the video's renders in progress and their executions, the
limit counting them out, `render_video` and `get_video_jobs` naming the
newer render, two racing starts leaving one in progress),
`test_video_studio_clip_reference.py` (a shot's first frame: the checks as a
non-admin, the frame kept and reused, its shape and crop; with ffmpeg: the
frame, the last frame, the cropped middle),
`test_video_studio_start_frame_preview.py` (the settle rule on synthetic
frames, the preview, the tool and the route; with ffmpeg: a synthetic take
whose blank between two pages is flagged), the suites on synthetic data
only - `test_video_studio_screen_insert.py` (the quad and its scenes, the
warp's corners, premultiplied edges, reduction, transparent contain and glow on
synthetic footage, the plan, the frame server; a wall's sources and
refusals, its panes split in perspective with their gutter, planned pane by
pane; in Chromium, the frame placed
on the server's box), `test_video_studio_insert_compositing.py` (on
synthetic footage: the bezel lock snapping a quad off its bezel and
measuring the given quad's error, a person across an edge moving the line
nowhere; a static bezel, a bezel the camera pushes in on with the tracker
pixels off, a green surface under uneven tinted light and a person crossing
the surface each composed frame by frame - aligned, sharp, without residue,
flicker or halo, and reported; a deliberately wrong quad and a quad too
small failing the render, a hard-edged matte - binary, undespilled -
failing it; flicker as an alternation never one turn; the clamp despill and
the colour-difference share; the supersampled warp; the box the page and
the driver share; the crops picked, kept, merged and named in the answer),
`test_video_studio_insert_colour_fidelity.py` (a dark UI inserted into
a blue-lit shot whose screen catches a window's glare failing the colour gate
at `light: 0.6` - its blacks lifted - and passing at 0.15 within the
threshold; the light casting no colour on a dark screen nor a green key, a
surface lit evenly laying none; a tint named, a turned hue measured; `light`
on the spec, the page, a range's bundle and the editor's build; every crop of
a preview attached and named by the conversation's file, its findings'
frames linked; `save_video_frame` cutting a region at 100 %; an ultra
render's insert measured and cropped on 2160p frames, and its answer saying
so),
`test_video_studio_ingredients.py` (each adapter's
path from its own declarations, what the native ones send, the prompt
sentence), `test_video_studio_frame_qc.py` (SSIM, the text match, the kept
frames, the threshold and its failure), `test_video_studio_regeneration.py`
(a ready take kept through a refused, drifted, cancelled or given-up
attempt and replaced once a new one passes, never paid twice, `discard`; a
429 cooling its provider for the batch, the shot deferred and held, the
backoff, `validate_only`'s `provider_state`, the bound; the retry modes; the
Gemini `google.rpc` details), `test_video_studio_content_checks.py`
(each content finding on a synthetic spec, kept when the layout is skipped),
`test_video_studio_scene_preview.py` (a two-scene synthetic video: the
window, the queue, the draft render, the retention; in Chromium, only the
window's frames drawn), `test_video_studio_provider_resilience.py`
(what a refusal says on every error shape, a rate-limited shot deferred for
the provider's wait or a jittered back-off and failed once held too long,
frames linked as files of the conversation), the suites - `test_video_studio_screens.py`
(screen replacement: parsing and fitting a screen, the screen found in a
start picture and found again in another one, the key, the placeholder,
the tracker under motion, shading and a hand across an edge, compositing
under an occluder with the light laid over, the "screen lost" judgement, the
runner's raw take replaced and its first frame measured against the
screenshot drawn in; with ffmpeg: a synthetic shot drawn end to end),
`test_video_studio_animated_still.py` (an image's effects validated, planned
and drawn by the compositor under node), `test_video_studio_figure_still.py`
(a figure's still: its digest, drawing, storing at a save, the preview
playing it or drawing it in the background, its token and route, the
subtitles' band; its steps kept beside it and a save's reveal times; the
editor's build: its digest, drawing, the preview playing it, its range route),
`test_video_studio_speech_timing.py` (a narration's words
timed on synthetic speech), `test_video_studio_figure_chromium.py` (a figure
drawn for real: the nodes shown at 25 / 50 / 75 % of the scene, the camera,
the narration, the band, and a scene - 6 nodes over 17 s at the
draft preset - moving to its end: SSIM under 0.98 between its frames at 80 % and 100 %
where a figure holding still repeats one frame), the drift severities in
`test_video_studio_shot_references.py`, `test_video_studio_film_gaps.py`
(a figure drawn once per save, a slide above the subtitles, a step
named last followed, camera terms never read off a frame, a frame past the
end, text turned unreadable, a failed animation's finding, a shot asked silent
for its video, the media key), the suites - `test_video_studio_shot_cleanup.py`
(on synthetic shots encoded with ffmpeg: a known camera move measured around a
masked region and an exposure drift; a stray screen flattened in every frame
and drawn with a screenshot under a push-in only the measured motion follows,
refused when something passes in front of it; a melted banner drawn back from
the still, a shot not opening on it refused; the runner cleaning, reading
again and failing what a cleanup did not answer, never cleaning twice, keeping
regions before the text check), `test_video_studio_film_gaps_20oct.py` (the
render's music and the line about it, the refit's margin through the tool,
previews queued to the renders' limit, `animate.keep` and the shot options'
refusals), `test_video_studio_combobox_chromium.py` (in Chromium: a portalled
listbox, a list shown only once typed into, a missing option named, a native
`<select>`), `test_video_studio_footage_fill.py` (a shot slowed to fill
its scene down to `SLOWEST_FILL`, never one heard, the scenes' times
unchanged, a recording's compressed pauses slowed (never below
`RELAXED_PAUSE_SPEED`, at its own speed too) before any hold, its clicks,
cursor, camera and address bar on the relaxed map, while a
cut pause and a skipped step stay cut, the `held_frame` finding, and in
Chromium a held frame drifting to its scene's end), the suites on synthetic audio and frames
only - `test_video_studio_stitch.py` (two narrations across a scene boundary:
no sample jump, no duplicated frame at 50-300 ms, each measure proven on a
stem built wrong; resampled once, whole; placement, fades, an overlap
refused), `test_video_studio_voice_check.py` (the word rules, WER, repeated
sequences, the retry ladder and its records), `test_video_studio_audio_qc.py`
(a sine burst repeated, on and off the hop, a single burst, a held tone,
speech-like noise, a duplicated segment, a mix fault against a music loop,
transcript and repeated-sentence findings, the whole check),
`test_video_studio_music_level.py` (BS.1770 gating, a levelled bed 6 dB
under the voice and 16 once ducked, a quiet passage with its times, a silent
track, a track too quiet to level, a volume and a ducking that lose it, a
scene preview's span, the render check's section and warning, the save's
measure kept by digest and bounded, the advisory that never refuses a render,
and against ffmpeg itself the integrated loudness of a real bed),
`test_video_studio_zoom_layout_6356.py` (a 1000 px page recorded at
1600x900 refused at zoom 190 naming 160, accepted at 150 never resized, its
document or its main container read, a real Chromium dashboard measured the
same; the layout probe's exact minimum width; a take refused before its
screencast), `test_video_studio_figure_fit_6356.py` (a 12-node tilted
architecture keeping every node and both legend lines 3 % inside the frame at
every pose of its drift, the save saying its drift; a capped drift and a held
figure said; the plan and the keys; the quality finding; the page's constants
and the compositor's stands pinned, `project` held to Chromium's projection),
`test_video_studio_kept_preview_6356.py` and `_postgres.py` (what a
preview is made from; a real stream-copy encode; against PostgreSQL, a fully
cached figure preview served within 30 s while two insert previews queued
ahead of it are still queued, its job held and ending at once, its files and
checks its own; one whose kept MP4 is gone given back to the lane),
`test_video_studio_music_tail_6352.py` (the closing window; a real
144 s fade-ending AI track under a 147 s video passing with a fact and no
finding, and warning without its window; a stretch before the window and one
mid-track still findings; a warning alone never blocked; a save of a video
outgrown by 2.5 s warned on the scene where it loops, 1.5 s not, the note
saying it once; an extended refit keeping the take on a model that continues
it, refused without one; a refit with extend keeping the first 120 s of the
take bit-identical, the model given the 30 s before the cut; the runner
replacing the kept take once the extended one is stored, and giving it back
on a refusal and a dead letter; the Eleven Music plan and upload),
`test_video_studio_frozen_frames.py` (a known frozen run, cards and a
`hold_end` beat left alone, the page's own drift never frozen, the planner's
`extend_mode` - a loop or a hold never slowed -, the page's drift switch and
loop under node, the frame server keeping a loop's frames),
`test_video_studio_render_checks.py` (the scene digest and its media state,
what a render records on the video, the gate's reasons - a draft's frozen
picture never stopping the final render -, a forced start recorded and
audited only when it overrode something, the tool and the route, a whole
render failed or ready on its checks) and
`test_video_studio_av_checks_chromium.py` (a 3-scene synthetic video rendered
for real - title, a clip shorter than its narration, a statement, tone-coded
speech under a built-in score - with clean checks, and its clip held instead
found frozen; the `chromium` CI job), the suites on
synthetic footage only -
`test_video_studio_shot_rescue.py` (the tracking core against a known
homography through outliers; with ffmpeg, a textured world filmed by a camera
whose motion is known: a drifting take's homographies against the truth, the
take locked to its first frame, a still take left alone, a camera lost at a
named frame, a drift too large to crop; through `run_clip`, a drifting take
rescued with its raw take kept, refused with `stabilise: false`, stabilised
for a prompt asking a locked camera and not for a moving one; a tracked
insert's quad against the monitor's true corners, the render handing the page
its frames and box, two inserts on one footage followed in one decode, the
driver drawing each footage frame into its quad, a surface lost at a named
frame and its scene's time with the frame attached; each frame's map of a stabilised take against the truth, two
inserts on a stabilised take followed through its raw take and kept inside
the frame on every frame, an insert the crop cut failing as "screen
cropped", the crop never cutting into the shot's screen, a wall followed
once with its panes moving with it; a logo found in its
start frame, drawn back over a garbled one through a drifting shot, lost
with its frame when a hand covers it for 4 frames or the shot cuts away,
composited through `run_clip` and a lost one failing the take with its frame
stored), `test_video_studio_insert_over_foreground.py` (a dark
object crossing a dark screen under an unkeyed insert - its head in front
by its link to what crossed the rim, cut at the screen's edge without the
weak change, its mask never wider than its blur; a subject in front on the
frame the insert comes in on read off the clean plate; drawn on none of its
pixels on any frame, no finding, its confidence said low; the insert forced
over it failing `insert over foreground` with its frame and its crop; a
keyed subject read part key never seen through; end to end through the
frame server, the plate read off an MP4), `test_video_studio_insert_matte.py` (a keyed insert on a
green screen with a cat in front kept in front and despilled, an unkeyed one
keeping a hand that enters through the edge in front while covering what the
screen itself shows, an exposure change no occluder, the key's colours and
tolerance; a PNG on transparency with its halo around the mark, a card baked
into a picture found padded; a brand mark by role, each its own file; the
frame server matting on the footage under the insert and auditing each
frame, the audits kept per segment, read back as `insert_foreground`,
`logo_lost`, `insert_padded` and `surface_lost` findings timed in the MP4
with their frames, refusing the next render; a curved surface's local warp,
a take whose pixels are not the asset failed; the preview build drawn by
the same compositor - a VP9 WebM with alpha, keyed, the source moving, the
cat in front - played when drawn and drawn when missing; the insert token),
`test_video_studio_logo_tracking.py` (a region holding two planes
proposing both, `step` adopting the first; the most confident candidate
drawn and continuity telling alike ones apart; a card carrying the logo
across a textured table followed, never the table; a cylinder rolling 180
degrees over 60 frames showing the logo once a frame, faded out over 3
frames as it turns away and drawn nowhere once it faces away; the turn read
off the foreshortening; an object at rest kept at its first stable frame; a
folded mesh never drawn; a logo shown twice found at its tracked size,
recorded on the take with its frame and failing every render's scene playing
it in the scene's time; one ingredient given twice drawn once),
`test_video_studio_tracked_logos.py` (a textured card walking into an
establishing shot gets the logo on its patch from the frame it enters, seen
from the first sampled frame it is in, followed between them and back, and
nothing drawn before; two instances one logo each; a surface covered while
the subject stays failing as `ingredient surface lost` with the frame; a
subject walking out ending its instance; one never seen, or seen for a
flicker, failing as `ingredient subject not found`; the detector's answer
bounded, the sampled frames and the centred smoothing; the native check
finding the asset as given and missing the redraw; the contract - a logo's
subject and nothing else's, the `tracked` path start frame or not, a tracked
logo alone needing no start frame; `run_clip` drawing it, recording
`logo_tracked`, falling back from the native path when its check fails and
keeping the frame of a lost surface), `test_video_studio_text_confidence.py`
(the reading word by word with the older shapes still read; the
expected words the ones the start picture reads clearly; a take keeping the
foreground text and garbling the flags passing, one garbling the foreground
text failing with the word, the frame and its box, one adding a new word
reported and not failed; the start picture's own illegible lettering the
baseline of the unreadable count; the text match on the expected words
alone; `run_clip` keeping the record with the take and storing the failed
word's crop),
`test_video_studio_model_and_check_misfires.py` (a model named alone
asked of the provider listing it, never of the one chosen in Settings, one
several list refused until named, one nobody lists sent to a provider
listing nothing; its limits read on its own provider; a locked-off camera
the model does not hold said before the take is paid for, naming one that
holds it - never asked of it -, the
providers' declarations; a locked-off take whose only motion is a small
subject not frozen, a held frame still frozen; a true-alpha picture with a
soft halo in a contain fit on a tall quad never padded, a card baked in
padded whatever quad pads it, a plate with a mark on it never),
`test_video_studio_gate_tolerances.py` (every file a scene preview's
or a failed render's checks name attached and readable in the turn's files;
a crowded render attaching its frames and each insert's worst finding and
crops first, 12 at most, the rest linked and named;
a flat green screen with a thin halo past its bezel and a cat walking in
front passing, the halo taken out of the margin and said, the footage's
glow on the rim filled with the bezel's colour and its spill on the desk
failing nothing, a 1 % green patch the source itself shows its own,
a residual key under the gate's share said and never failed; a bright line across a
locked-off surface on two frames never moving the insert nor failing it, a
bezel that really moved followed, a tracked quad never held, a real step
never hidden while the prior settles, a finding only past five frames in a
row (frames scattered never); a logo on transparency, a navy plate with a centred logo
and a letterboxed card read as they are; an 8 s locked-off take with a
low-contrast blob covering 5 % of the frame and its crossfade into a still
never frozen, a held frame, a subject that stops and a still held past the
blend still frozen, the same on footage encoded by x264 (ffmpeg); a preview
within those tolerances passing the gate, the next render not refused),
`test_video_studio_insert_gate_contract.py` (the gate contract's
fixtures: a flat panel at 35 degrees of yaw passing, its angle said by
design; a cylindrical panel bowing 8 % failing on its flatness alone, at save
and on the frames drawn; a screenshot with a 100 % green ring leaving no
residual key; a hand crossing that ring over 40 % of the quad passing with
its colours not compared; a panel whose green runs 1 % past its bezel
leaving no residual, its margin the bezel's colour; a 16:9 source on a 4:3
trapezoid with its bands filled; a clip whose camera moves by intent, and
one previewed green, cleared; tolerance 0.4 and 0.88 both passing, drawn
anew; a halo needing five frames, `not a setting` on a residual, a colour,
a halo and a flicker no key or light moved, a curved surface folding what
follows from it, every field of an insert in its segments' digest - its
track and its pane too; F9 a flat panel with a ball over most of one
edge passing, that edge mostly covered and not judged, the ball kept in
front, the save's and the render's readings the same; F10 a cylindrical
panel with the same ball failing its flatness alone on the edges left
visible; F11 one shot at save, at a 1080p preview and at a 2160p render
giving one verdict, flat and curved; F12 a UI whose text reads 4 px a
legibility finding naming the viewport; F13 a failed regeneration said once
as advice; F14 a hand over a dark UI area leaving no halo, its colours
compared and passing; a bow confirmed only by all three readings on five
frames in a row; a render finding a save or a preview passed a warning that
disagrees with it; the shot's own lint of F9, F10 and F11 giving the
verdict of the save and the render, a bow without its two other readings never confirmed, a render
finding the shot's lint passed on the take that plays a warning, a report
carrying the version the studio keys it by; F15 a furry subject
whose edge holds 20 % of a matte key panel painted off the primary leaving
a halo under 2 % after the edge despill, its interior's colours within a
delta E of 1, the share corrected said frame by frame; F16 F15 with
`despill.edge: false` failing its halo as `edge spill N% with the edge
despill off`, its worst stretch kept, its action turning it on; F17 the
insert's light wrapped onto the subject's edge on a 2 px band of a 1080p
frame alone; a wider despill leaving less of the key, a halo after the
despill saying its width and its crop and advising it wider up to its
bound, a drawing at another despill that left the same halo `not a
setting`, the save saying the edge spill before any preview, the despill
carried from the spec to every drawing of the insert; F18 a
pink-nosed subject crossing a green panel from outside the frame, its tip
alone and motion-blurred, keyed frame by frame, passing its halo with its
pink said as its own over the clip and on the edge, and kept on the frame;
F19 a grey subject with a real 3 px green fringe failing it, the fringe
never read as its colour; F20 a magenta jacket passing; F21 the same with
an inverting clamp on a tenth of its edge failing it, counted where the
clamp touched and named so, the compositor's own clamp mask reaching the
gate and the edge despill taking what the clamp made; own hues carried
from the spec to every drawing and never counted, the clamp's included;
the subject read once by its clip and taken for every pane), the
cases of `test_video_studio_regeneration.py` (a term two takes in a row
showed asked in the third take's prompt itself, the made take's notes and the
answer saying so, a take never checked breaking the run, three terms at most
and the prompt never cut, its guidance lowered on its own model's scale where
the model has one; a model named alone asked of its own
provider; `validate_only` with a locked camera saying the model will not
hold it),
`test_video_studio_brand_mark_raster.py` (every `logo` /
`logo_light` / `logo_mark` SVG every built-in template seeds drawn by
`svg_to_png` in the raster worker to a PNG with alpha whose opaque pixels are
the SVG's own fills; a worker spawned under a `__main__` mapping most of its
budget still drawing the logo, the cap its budget past what it maps; the
rasteriser's reasons logged and raised; an insert not drawn an
`insert_missing` finding of its scene, in a preview's window or not at all),
`test_video_studio_insert_key_colour.py` (a named key read off a
(32, 255, 30) surface, a navy picture drawn navy on it, a hue turned past
`MAX_HUE_ROTATION_DEGREES` failing the scene with its worst frame (a
grey judged by its cast), a `#rrggbb` keyed as written, the sampled colour through a
range's bundle), `test_video_studio_insert_memory.py` (the inserts a
page draws at once read off a plan and a spec, every measured run under the
per-insert term, the production run under the ceiling it sizes, a
preview's ceiling, its admission and its pages sized to its scene, the
footage frame decoded once for a page's matted inserts),
`test_video_studio_model_fallback.py` (a take
failing its checks generated again with the fallback through the real
`restart_clip`, every model failing, a kept take through the chain, the
fallbacks checked before paying, a chain started anew by hand naming its own
alone - never one an earlier call named -, a fallback the
shot can no longer take), `test_video_studio_one_model_6577.py` (F47-F50:
an account short of credits refused at `validate_only` with the numbers; a
Vertex take failing its checks left failed on Vertex, its regeneration on
Vertex, another model only named; a 401 `quota_exceeded` cooling its model
with its credits, nothing asked of another provider; Vertex ending five
operations for its load, then making the shot - five waits logged on the
schedule, no failure, the same model; F53 a shot for a video asked of the
provider its shots of that model are made on, a regeneration of its own),
`test_video_studio_inserts_6577.py` (F42-F46, F51: a 1.9:1 quad a
1600x842 window; a high render under motion blur audited on every frame its
standard preview audits - the same verdicts, its bezel found - where the
centre sub-frame left unaudited audits none of a still insert, the crops cut
0-1 of the decoded frame on the screen at any capture size, and the same
render through Chromium; a shifted box `insert_measurement_lost` naming
where its source is, an insert whose bezel the save and the preview found
and the render did not the same block naming each size, one missing from
its first frames `insert_missing`; an insert absent 10 frames
`insert_missing`, one frame the one harness defect; a `#2fbf4a` screen and
a shaded yellow green found, their hue said),
`test_video_studio_covering_render.py` F52 (a ready render drawn at an
older drawing or insert build version, or one that recorded none, drawn
again without `force`, `render_video` saying why),
`test_video_studio_model_and_check_misfires.py` F53 (a model several
providers list asked of the video's provider, then the platform's, the
answer naming it), `test_video_studio_served_models_frozen_parts.py`
(a fallback its provider answers 404 for refused at `validate_only` and
on the real call naming it, the listing dropping it; a generation's 404
recorded by the worker; a job's 404 never the model's; Gemini's probe; a
flickering candle under a static page never frozen, the same footage frozen
from 3 s naming the footage, a page that stops under a still room naming the
insert, the parts placed where the page draws them),
`test_video_studio_template_cards.py` (a template
card's length from the spec to the planned timeline, the finding naming
`template_scenes`), `test_video_studio_brand_kicker.py` (every
built-in template's cards, and their duplicates made with the kickers they
shipped before, show the brand once - rendered in Chromium for the intros -,
the mark and caption rules, `template_scenes.<card>.kicker` and its `null`,
the `brand_repeated` finding and its fixes, the marks read from the assets),
`test_video_studio_render_gate.py` (the reasons in
`render_checks.gate` and `start_render`: a shot not ready or deleted with
what a render does with it, the saved quality check's audience findings
and never an advisory one, the video never played, a forced render
recording them, `render_video` never forcing on a `"false"`, and two
previews saving together each keeping their scene under the row lock), the
suites - `test_video_studio_preview_trust.py` (a shot's finding
cleared only once every scene showing it passed a preview at its current
trim, a render's open finding by a newer passing preview, a video whose
every preview passed rendered without `force`, the take that plays judged by
the tolerance it passed under, one scene edited changing three digests at
most, a shot generated again whose take still plays changing none; an
edit previewed then reverted passing the gate without `force`, the shared
inputs a digest reads, the drawings kept by digest, a byte-identical save
writing nothing), `test_video_studio_revert_postgres.py` (the same
through `edit_video` and `render_video` against PostgreSQL - the revert's
note, the no-op answer, no revision and no audit entry for it, the render
started without `force`),
`test_video_studio_render_reuse.py` (a relaunch on a revision in flight -
one taken back from a lost lease included - answering the same render
without gating or auditing again, a twin found under the lock, another
quality replacing it, the previews of the video asked last put first in
their order, the queue place, a preview's poster) and
`test_video_studio_render_reuse_postgres.py` (the same through the routes
and `render_video` against PostgreSQL, the render history showing it once,
another video's ten queued previews neither refusing nor delaying the one
being edited), `test_video_studio_regeneration_cap.py` (the cap per prompt
and start frame through the real `restart_clip`, a model or a camera
tolerance never counted), `test_video_studio_lettering_rescue.py` (the
located blocks as regions, the runner's rescue and its failures, a kept take
playing on, and a melted banner drawn back on real pixels and read again),
`test_video_studio_render_stall.py` (the segments kept or dropped by their
digest, the supervisor resuming in place with its lease renewed and failing
past its resumes, the stall said, and in Chromium a resumed render drawing
only its unfinished segment) and the player's `useVideoMediaUrl.test.tsx` -, the
suites, derived from the seeded readings only -
`test_video_studio_pronunciation.py` (the seed against its schema and the
confirmed readings, the merge and its overrides, the rewriter - whole words,
punctuation kept, a compound as one entry, letters joined, syllables as given
-, each match mode, captions, the SSML builder per dialect, the IPA and the
English reading as phonemes, the aligner on every accepted and rejected
hearing of every seeded term, a brand failure under the WER threshold, a
`say` that does not sound like its IPA, the save-time warnings; the key
of `Phil Igram` two off, the written form spaces aside, the warning of a term
heard otherwise, the IPA instruction and the respelling note, a seeded term
given its IPA back),
`test_video_studio_pronunciation_voice.py` (one synthetic speech model per
family of mechanism through the real voice pipeline: every seeded term passing
with each, its mechanism and string sent recorded, the provider lexicon
registered once; a deliberately wrong `say` failing after each mechanism, the
retry order and the message, the render's finding naming the segment, term,
hearing and mechanisms; a voice forced to say `Phil Igram` refused by the gate,
the IPA instruction heard as `Filigran`; the play endpoint; the ElevenLabs requests),
`test_video_studio_lexicon_postgres.py` (the seed once, the reads and the
non-admin refusals, versions, validation, the audit, a restore, the merge, a
bare Filigran given its IPA back once),
`test_video_studio_pronunciation_render_chromium.py` (a 3-scene video saying
Filigran, CTEM, XTM One and OpenCTI rendered for real with tone-coded speech:
clean, its pronunciation section all passed; a take right on its own spoilt
before the stem, caught at its time) and the opt-in
`tests/live/test_live_speech_pronunciation.py` (every seeded term spoken by
each configured speech model and heard back by the platform's speech-to-text),
`test_video_studio_preview_fairness.py` (a template's watermark as the
default and the save's note, a preview's quality and memory ceiling,
`quad: auto` staged, refused where it cannot apply and found as the bezel of
a synthetic frame's flat screen, a neighbour's finding never failing a
preview, the lane dealt round-robin, a theme or music edit keeping every
passing preview and never a failed one),
`test_video_studio_reference_drift_6782.py` (synthetic pictures: a
teal-piped prop against a navy collar and light-blue piping a drift with its
crop, the same prop under another light none, a shot naming no reference
never read, a spec naming an unknown reference refused by name; real footage
in `tests/fixtures/video_studio`: the rug seen from another angle and scale
no drift, its piping recoloured light blue a drift, a frame without the prop
an advisory; the references sent as reference images beside a start frame on
a model that takes them, named in the prompt otherwise, the answer saying
which)
- the editor's edits
(`video-editor-model.test.ts`), and the template / seed / channel suites. Capture and render need
Chromium and ffmpeg and are checked end to end outside the unit suite.
