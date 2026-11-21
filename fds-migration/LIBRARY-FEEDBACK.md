# Library feedback — XTM One integration of `@filigran/design-system`

Every gap, defect and friction point found by adopting the design system in XTM
One, in the form the library's own
`process/PRODUCT-IMPLEMENTATION-PLAYBOOK.md` asks each pilot to file.

- **Product:** XTM One (`XTM-One-Platform/xtm-one`)
- **Pin:** npm `@filigran/design-system@1.4.0` (exact version, public registry; bumped from 1.2.0 on 2026-11-10, xtm-one #10533 slice 1)
- **Scope:** token layer, app shell (Navbar/Header/ProductSwitcher), the
  primitives behind `frontend/src/components/ui/`, white-label branding.
- **Implementation:** `frontend/src/index.css` (token layer),
  `frontend/src/components/layout/` (shell adapters),
  `frontend/src/components/ui/` (primitive adapters),
  `frontend/scripts/fds-migration/` (the codemod, the residue counter and the
  emitted-class guard), `frontend/tests_unit/` (the accessible-name guards, the
  fixed-fill contrast guard, the layer input-background guard and the safelist
  filter).
- **Figures as of 2026-10-13** (main at `e02aca99e`, merged into the PR branch).
  The colour-literal count and the safelist size come from
  `count-legacy.mjs` and `generate-safelist.mjs` on that tree; the claims about
  the library's export surface were re-checked at 1.1.0. The counts move with
  `main`, so quote them with their date: the mechanism is the durable part.

XTM One is the first pilot whose stack already matched the library's — React 19,
Tailwind v4, Radix, CVA — rather than being a MUI codebase. Several entries below
exist *because* of that: they are the Tailwind-host path's problems, and the four
previous pilots could not have hit them.

**Severity.** *Blocker* — the integration cannot proceed, or proceeds on a false
premise. *Major* — costs a round trip or ships something silently wrong.
*Minor* — slows the follower down.

---

## Install and build

> Since 1.0.0 the library is on the public npm registry, and XTM One consumes
> `@filigran/design-system@1.2.0` from there. F00, F01, F02 and F20 all describe
> the git-dependency path — the private repository, its credential and the CI
> plumbing each product had to build around it — so they no longer bite this
> product. They are kept because they still describe what that path costs anyone
> who takes it, and because the playbook still prescribes it.
>
> One thing the move surfaced, for whoever maintains the package: the published
> tarball ships a `*.meta.mjs` documentation module beside every component, and
> its prose reads as class-shaped text to a host's Tailwind scanner — 466
> phantom classes here until the scan learned to skip them. Shipping the
> metadata under a separate export, or out of `dist/`, would spare every
> consumer that discovery.


### F00 — the prescribed secret name is not what a product ends up with · minor

The playbook says the secret is "named exactly `FDS_GIT_TOKEN`" and that it is
already posted in `openaev` and `opencti`. XTM One's was created as
`FDS_GITHUB_TOKEN`, and the mismatch cost a CI round trip: every workflow
referenced the prescribed name, the secret existed under the other one, and the
failure looked identical to the secret being absent.

Worth a line in the playbook: check the name that actually exists with
`gh secret list` before wiring, because `${{ secrets.MISSING }}` silently
resolves to an empty string rather than erroring. XTM One aligned its workflows
on the name it has.

### F01 — Yarn denies every git dependency by default · blocker

`approvedGitRepositories` defaults to an **empty allowlist** in Yarn 4, so the
documented install fails at the resolution step with

```
Request to 'https://github.com/XTM-Foundation/filigran-design-system.git' has
been blocked because it doesn't match any of the patterns in
'approvedGitRepositories'
```

This is Yarn's own default, not a choice made in this repository, so it applies
to any Filigran product installing the library with Yarn 4. It fires *before* any
authentication step, which makes it easy to misread as an auth failure.

Fix in the consumer, worth documenting in `AGENTS.md` § Consumers:

```yaml
# .yarnrc.yml
approvedGitRepositories:
  - "https://github.com/XTM-Foundation/filigran-design-system.git"
```

### F02 — a hardened consumer installs the package empty · blocker

The install proxy builds `dist/` from its `prepare`/`prepack` script. A repository
that disables lifecycle scripts for supply-chain reasons (`enableScripts: false`,
which XTM One sets) therefore installs a package with **no `dist/` at all** — and
no error: every import fails later, on a package that looks installed.

The consumer has to allow this one package to build:

```jsonc
"dependenciesMeta": { "@filigran/design-system": { "built": true } }
```

The install section covers auth thoroughly and does not mention lifecycle
scripts. Both F01 and F02 belong next to it.

### F03 — Tailwind v4 hosts do not reliably compile the library's own classes · major

The documented Tailwind-host path is `tokens/theme.css`, which gives the host the
tokens as utilities. But a Tailwind host compiles the library's **components**
with its own pipeline too, so every class those components carry must be detected
in a scanned source — and `node_modules` is excluded from Tailwind's automatic
source detection.

Adding the bundle explicitly is not enough:

```css
@source ".../design-system/packages/filigran-design-system/dist/*.mjs";
```

With that in place, `bg-gradient-default` and `w-45` were emitted while
`text-input-placeholder`, `content-compact-medium` and
`bg-filigran-brand-primary-transparency-10` — plain string literals in the same
file — were not. Adding those three to `@source inline(…)` emitted them
immediately, which locates the fault in detection rather than in the tokens.

Two further traps found on the way:

- The components live in a **tsup chunk** beside `index.mjs`, so a `@source`
  pointing at the entry point alone misses all of them. That produced a Navbar
  with no background at all (its `bg-gradient-default` was never compiled).
- The failure is silent in the worst way: layout intact, colours and typography
  gone, and nothing in the host's own code to explain it.

Importing the pre-compiled `dist/index.css` **as well** was tried first and had
to be reverted, for a reason worth passing on: an UNLAYERED stylesheet beats
every layered rule in CSS. The library's build safelists `p-0 p-2 p-4 p-6 p-8`
for Paper's padding scale, so its plain `.p-4` won over this app's
`@media(min-width:1024px){.lg\:p-8}` — every responsive variant of a utility the
library also emits was silently dead. The symptom surfaced two screens later as
a 16px horizontal scrollbar on the chat page, where a wrapper cancels the main
padding with a negative margin and the two no longer agreed. Moving that import
into a cascade layer broke Tailwind's own emission instead (153 utilities
vanished, including the host's own).

What XTM One ships instead is a generated safelist, derived from the pinned
build rather than scraped. It started as the exact set of classes the bundle
used and the build did not emit — 93 of them — and had to be widened to every
plausible class token in the bundle (870 at 1.1.0, as of 2026-10-13; 1 003 at 1.2.0), once it became clear a missing
entry fails silently and invisibly. See F21: that widening is the part that has
no upper bound.

Two more traps for the eventual fix:

- `@source inline(…)` is honoured **only in Tailwind's entry stylesheet**. From
  an `@import`ed file it is silently ignored, which made two earlier attempts at
  this look as though safelisting did not work at all.
- Restricting the safelist to *design-system-shaped* class names is not enough,
  and the `Switch` is the proof: its track is painted by
  `group-data-[state=checked]:bg-filigran-brand-primary-transparency-50` and its
  thumb by `size-5`. A bracketed variant and a plain core utility — so a filter
  keyed on token shape misses both, and the control renders as a transparent box
  with a full-width thumb. The list has to be taken verbatim, prefixes included.
  This one reached a reviewer's screen before any gate noticed it.

Suggestions, in preference order: ship that class list as a safelist fragment
consumers can include (it is derivable at build time from the components'
`cva` calls); or document the entry-file constraint plus a recipe; or emit
`dist/index.css` inside a cascade layer so it can coexist with a host's own
utilities.

### F21 — the safelist workaround has no upper bound · minor

A consequence of F03 rather than a separate defect, raised in review of the
consuming PR. With detection unreliable, the only safe host workaround is to
safelist **every plausible class token in the bundle** — 870 of them at 1.1.0 —
because a missing one fails silently and invisibly (layout intact, colour and
typography gone). The list once reached 1 577 entries, about 700 of them words
from the bundle's prose (figma links, a backlog file's name) carried forward from
an early scan; the generator now refuses what cannot be a class and drops what
nothing uses any more. That is the right direction to err in, but it means the host
compiles rules for library strings that merely LOOK like classes, and the set can
only grow with the library.

The clean fix is upstream and small: publish the component classes the library
actually needs compiled, as data rather than as strings a scanner has to guess
at — a generated manifest, or a per-token annotation the host can read. Either
turns "safelist everything that pattern-matches" into "safelist what the library
says it uses", and lets a host verify completeness instead of over-approximating
it.

### F04 — `--color-*: initial` is a breaking change for a Tailwind host, undocumented as such · minor

`tokens/theme.css` disables Tailwind's default palette. That is a defensible
design decision and it is what makes a stray `text-red-400` fail loudly rather
than bypass the design language — but for an existing Tailwind codebase it
invalidates every palette utility at once (1 832 occurrences here), plus
`bg-white` / `text-black`, while `transparent` / `current` / `inherit` survive as
built-ins. Worth one paragraph in the consumer guidance, because the first
symptom is "half my app lost its colours".

### F05 — FDS `--shadow-*` silently break Tailwind's shadow utilities · minor

`--shadow-sm|md|lg|xl` are declared as depth **radii** (4/8/16/20px). In a
Tailwind host those are the same variables `shadow-sm`…`shadow-xl` read, so all
four utilities start emitting an invalid `box-shadow` the moment the theme is
imported (55 occurrences here). A `--depth-*` namespace would avoid the
collision entirely.

---

## Component gaps

### F06 — no status chip · major

`Chip`'s vocabulary is severity (`neutral`, `info`, `low`, `medium`, `high`,
`critical`) and CTI entity families. Products also need **status**: success,
warning, error. XTM One shows run outcomes, health states and validation results
in 602 badge call sites, and none of them are a severity.

The tempting mappings are colour coincidences rather than meanings — `success` →
`low` would label a completed run "low severity" — so XTM One kept a local badge
painted from FDS feedback tokens rather than mis-stating semantics 602 times.
This is the one place the no-fork rule was not followed, deliberately and in the
open (`frontend/src/components/ui/badge.tsx` documents it).

Also: `Chip` takes `label: string`, where a badge routinely wraps a count or an
icon. `children` would widen its reach considerably.

Two things have changed at the current pin and neither closes this. The library
now exports a `Badge`, but it is a MUI-style COUNT badge (`content`, `max`,
`dot`, `circularAnchor`) that decorates an anchor — not a status pill, and its
`BadgeTone` (`brand` | `error` | `success` | `neutral`) is a colour vocabulary
without a component to carry a label. The name is therefore taken: whatever
closes this gap needs a different one (`StatusChip`, `Tag`), and XTM One's local
`Badge` now collides with a library export, which is a rename waiting to happen
on whichever side moves first. `Chip` still takes `label: string`.

CTEM cases (UX round 1 of #857): a fifth case state, "mitigated, watching",
needs a colour of its own beside "resolved" (`low`, success). None of the six
severities is free and meaning-true (`high` and `critical` read as danger), so
the state keeps `low` and is told apart by a DS `Icon` (`eye`) passed as the Chip's
`startIcon` and the `feedback-success-tertiary` token on its bar segment. A status chip
with a `mitigated` or `watching` tone would remove the icon.

### F07 — `Navbar` has no section-heading slot · minor

The nav has labelled sections ("Resources") above groups of items.
`NavbarSeparator` draws a line but carries no label, and `NavbarSubmenu` adds a
chevron and disclosure behaviour that a heading must not have. XTM One renders a
token-styled `<p>`; a `NavbarSection` (or a `label` on the separator) would keep
it in the library.

**No longer needed by XTM One** (2026-10-20): it dropped the headings and
separates its blocks with `NavbarSeparator` alone, as OpenCTI and OpenAEV do.
`NavbarTitle` has since shipped (1.2.0) for a product that does want one.

### F08 — a submenu's hub page is unreachable while expanded · major

`NavbarSubmenu`'s `to`/`href` applies **only** in collapsed mode, on the stated
reasoning that "a submenu group heading is not itself a distinct page when
there's room to show its children inline". In XTM One it *is* a distinct page:
`/agent-studio` and `/tools-and-integrations` are hub screens with their own
create actions, and they were reachable by clicking the group label.

Adopting the library as-is would have silently removed that route from the UI.
XTM One's workaround is to give each hub its own "Overview" row inside the
submenu, which works but is a menu entry invented to route around a component
decision. A prop (`triggerNavigates`) would settle it.

### F09 — product logos are declared but not exported · major

`ProductSwitcher` requires a `logo` per option, and its documentation shows the
product marks. `OpenctiIcon`, `OpenaevIcon`, `OpengrcIcon`, `XtmhubIcon` and
`XtmoneIcon` are declared in the bundle but **absent from the package's export
surface**, so a consumer cannot import them. The only route is
`<Icon name="custom/opencti" />` via the icon registry, which is undocumented for
this purpose. Exporting them — or documenting the registry names in
`ProductSwitcher`'s own docs — would close it.

Meanwhile XTM One does what OpenCTI and OpenAEV do: it ships the products'
wordmark files itself (`frontend/src/assets/products/`, copied from theirs, a
`-light` variant per theme) and lists the suite whether or not it is connected.
OpenCRQ has no such file in any product and keeps its registry glyph and name.
Three products now carry the same files, which is the drift an export would
remove.

### F42 — no screen-share glyph in the icon registry · minor

The deck's "Present to a meeting" entry (More menu, key A) and the speaker
view's "Audience window" control need a glyph that reads "share this screen
to a meeting". The registry of the pinned version (`ICON_NAMES`,
`icon-mapping.json`) has `monitor`, `monitor-down`, `monitor-smartphone` and
`external-link`, none of which says it: `monitor` is the web-asset glyph,
`external-link` already means "open the deck in a new tab" in the same menu.
Ask: a `screen-share` entry (Lucide `screen-share`, Material `present_to_all`).

Meanwhile the two controls keep the direct `lucide-react` `ScreenShare`
import (DeckToolbar.tsx, PresentationSpeaker.tsx), counted in the
`src/components/presentation` and `src/pages` PENDING budgets of
`frontend/src/no-direct-lucide-import.test.ts`; they move to
`<Icon name="screen-share">` in the library upgrade that ships it.

### F43 — 308 product glyphs have no entry in the icon registry · major

The icon codemod (xtm-one #10533, deliverable 5) moves every direct
`lucide-react` import to `<Icon name>`. Against the 1.4.0 registry
(`icon-mapping.json`: 269 mapped entries, 229 distinct Lucide names) the
product's 770 importing files (2026-11-10, main `bb78cca1`, test files
excluded) use 455 distinct glyphs once Lucide's aliases are resolved to their
canonical names (`Loader2` is `loader-circle`, `AlertTriangle` is
`triangle-alert`): **147 are in the registry, 308 are not**. 239 files use only
registered glyphs and are the codemod's next slices; 523 files hold
at least one glyph the registry lacks, so they keep the direct import (counted
in the PENDING budgets of `frontend/src/no-direct-lucide-import.test.ts`)
until the library ships the names (the remaining 8 import only Lucide types).
No approximate glyph is substituted.

Ask: registry entries (Lucide name, Material counterpart where one exists) for
the glyphs below, highest use first. The figure in brackets is the number of
product files importing it; the 91 used in five files or more cover most of
the 523 files:

sparkles (96), globe (53), zap (51), copy (49), message-square (46),
book-open (37), shield-alert (33), workflow (33), history (31), boxes (28),
settings-2 (23), gauge (22), calendar-clock (20), hash (20), radio (19),
brain (18), scan (18), send (17), trending-up (16), layout-template (15),
target (15), hourglass (14), presentation (14), funnel (13), undo-2 (13),
users-round (13), cog (12), git-branch (12), lightbulb (12), coins (11),
cpu (11), folder-kanban (11), power (11), timer (11), wand-sparkles (11),
arrow-left-right (10), clock-3 (10), crown (10), library (10), plug-2 (10),
smartphone (10), archive (9), hand (9), pause (9), pen-line (9), user-cog (9),
arrow-right-left (8), code (8), copy-plus (8), mic (8),
mouse-pointer-click (8), printer (8), scroll-text (8), square (8),
file-braces (7), git-compare (7), git-fork (7), list-filter (7),
octagon-x (7), plug (7), power-off (7), telescope (7), trophy (7),
user-round (7), volume-2 (7), wallet (7), webhook (7), captions (6),
circle-dot (6), crosshair (6), film (6), fingerprint-pattern (6),
gamepad-2 (6), monitor-play (6), scale (6), scan-eye (6), user-plus (6),
variable (6), at-sign (5), brain-circuit (5), camera (5), check-check (5),
circle-dashed (5), laptop (5), list-todo (5), mail-check (5), maximize (5),
mic-off (5), monitor-cog (5), trending-down (5), volume-x (5).

Used in fewer than five files (217): arrow-down-to-line, bell-ring, cable,
calculator, circle-pause, git-pull-request, grid-3x3, keyboard, life-buoy,
link, message-circle-question-mark, minimize-2, music, redo-2, ruler,
scan-search, shield-off, shield-x, tags, unplug, user-x,
arrow-down-from-line, blocks, chart-line, circle-minus, file-code, file-down,
folder, github, hammer, link-2-off, lock-keyhole, lock-open, log-in, minimize,
move, package, panel-bottom, puzzle, ratio, replace, rotate-cw, rows-3,
scissors, shapes, square-pen, stethoscope, test-tube-diagonal, user-minus,
zoom-in, zoom-out, archive-restore, book-marked, chevron-first,
chevrons-left, chevrons-right, circle-slash, circle-stop, clipboard-check,
clipboard-paste, cloud-off, columns-2, corner-down-left, credit-card, equal,
eraser, file-exclamation-point, file-image, folder-plus, folder-tree, gavel,
git-merge, hand-metal, handshake, hard-drive, heart, image-off,
message-circle, message-square-dashed, message-square-off,
message-square-reply, monitor-off, notebook-pen, pen-tool, phone,
screen-share (F42), search-x, server-off, shield-question-mark, sigma,
square-check-big, step-forward, toggle-left, toggle-right, tv, ungroup, vote,
wifi, wifi-off, a-arrow-down, align-center-horizontal, align-center-vertical,
align-end-horizontal, align-end-vertical, align-horizontal-distribute-center,
align-start-horizontal, align-start-vertical,
align-vertical-distribute-center, align-vertical-justify-start,
arrow-down-right, arrow-down-up, arrow-down-wide-narrow, arrow-up-from-line,
arrow-up-to-line, audio-lines, banknote, book-a, book-open-text, book-plus,
bring-to-front, calendar-range, calendar-x, captions-off,
chart-column-decreasing, chart-pie, chevron-last, chevrons-up,
circle-user-round, circuit-board, coffee, columns-4, combine,
corner-down-right, corner-left-up, delete, diamond, door-open, file-badge,
file-chart-column, file-check-corner, file-key, file-pen, file-question-mark,
file-spreadsheet, file-stack, flame, folder-input, forward, frame, gift,
git-pull-request-closed, group, headphones, highlighter, image-minus, import,
iteration-cw, layout-panel-top, leaf, list-plus, list-video, magnet,
mail-minus, medal, memory-stick, menu, message-circle-off,
message-square-plus, monitor-check, mouse-pointer-2, move-right,
move-up-right, move-vertical, octagon-alert, octagon-pause, package-plus,
panel-right, panel-right-close, panel-right-open, party-popper, paw-print,
pencil-line, percent, phone-off, picture-in-picture-2, plane, plug-zap,
qr-code, radio-tower, remove-formatting, reply, rss, satellite, search-check,
send-to-back, server-crash, share, siren, skull, spline, split, sprout,
square-chevron-right, square-code, square-x, step-back, swords, table,
text-wrap, ticket, trash, tree-pine, truck, unfold-vertical, user-check,
vault, volume-1.

Some of these are product-specific (the agent icon picker's extended set,
the deck editor's alignment glyphs) and may stay outside the registry by the
library's decision; the ask is the decision per glyph, not a blanket import.
How the figures were produced: the canonical name of each imported identifier
is read from `lucide-react@0.577.0` (the product's pin) `dist/esm/lucide-react.js`
exports, and compared with the `lucideName` column of the pinned
`icon-mapping.json`. The figures move with `main`; quote them with their date.

### F44 — `Spinner` has no 12px size · minor

The busy glyphs the spinner slices replace sat inside compact controls at
12px (`h-3 w-3`: the MCP package download buttons, the tool test dialog's
"Add file", a chart card's refresh mark). The
smallest `Spinner` size, `sm`, is 16px, so each of these controls grows by
4px while busy or keeps a glyph the library does not own. Ask: an `xs` size
at 12px with the same `motion-safe` animation and the same `label` /
`tone` contract (UX round 1 on xtm-one #10952, advisory A1).

Meanwhile those controls render `<Spinner size="sm" tone="inherit" />` and
accept the 16px glyph; they move to `xs` in the library upgrade that ships it.
Glyphs inside a 12px status chip (the agent page's "Running" badge, the
delegation flow's status map) cannot take the 16px glyph without changing the
chip's height: they keep the legacy glyph until `xs` ships.

### F45 — `Spinner` stops at 32px · minor

Two full-surface loaders drew a 40px glyph (`h-10 w-10`: the run detail page
and the video cinema player). The largest `Spinner` size, `xl`, is 32px, and
the usage contract leaves 40 and 80px out of scope. Both render
`<Spinner size="xl" />` and accept the 32px glyph (UX round 1 on xtm-one
#11018, finding B1). Ask: either confirm 32px as the ceiling for a page loader
in the usage contract, or ship a `2xl` step at 40px with the same contract.

### F47 — the `Badge` counter circle depends on `--radius-lg` · minor

Since 1.5.1 a counter of one or two characters is a 20x20 box meant to be
a true circle, but `Badge` rounds it with `rounded-lg`, which reads
`var(--radius-lg)`: 20px on `:root`, and 8px inside `.filigran-chatbot`,
whose stylesheet redefines `--radius-lg: .5rem`. Every count badge inside
the chatbot scope (the XTM One `/chat` composer's Session tools count) paints
a rounded square, not a circle (UX round 1 on xtm-one #11403, finding B1).
Ask: draw the counter with `rounded-full` (a shape, not a token), so a scope
that retunes the radius scale cannot change it. Meanwhile the composer passes
`className="rounded-full"`, which tailwind-merge keeps over `rounded-lg`.

### F49 · a cut `ComboboxChips` chip shows its full text on hover only · minor

`ComboboxChips` caps a chip at 250px (`max-w-[250px] truncate`) and shows the
full label in a tooltip on pointer hover, but the chip `li` carries
`tabindex="-1"`, so a keyboard user never reaches the tooltip and reads the
cut text only (UX round 2 on xtm-one #11535, advisory A2: the "Priority
intelligence requirements (PIRs)" chip measures 260/250 in the organization
context categories field). Ask: open the same tooltip when the chip's remove
button or the chip itself takes focus, or name the chip with its full label
for assistive technology. Meanwhile the field keeps the full category label.

### F36 — the collapsed rail's icons sit 1px off its own mark · minor

`NavbarItem` reserves its 2px border on all four sides, but its collapsed icon
still carries the `mr-0.5` that balanced the left-only border it used to have.
Measured in Chromium at 1.2.0: the collapsed `ProductSwitcher`'s mark centres at
24px in the 48px rail, every `NavbarItem` icon (the collapse toggle and the
submenu headings included) at 23px. XTM One's own rows follow the library's 23px
rather than correcting it, so the rail stays on one axis; dropping the `mr-0.5`
would fix it at the source.

### F37 — an `asChild` row brings its own anatomy · minor

`NavbarItem` has no link destination (`to` / `linkComponent`), so a router link
is slotted with `asChild`, and the library then renders none of the row's
content: the icon span, its per-state tones, the collapsed `mr-0.5` and the
`sr-only` label are re-declared product-side (`components/layout/NavRowBody.tsx`
here, `NavbarRowContent` in OpenAEV). A row that forgets one drifts silently:
XTM One's leaf rows sat 1px off the library's, at the label's tone instead of
the icon's, and dropped their label in the rail, keeping only a `title` the
library's tooltip cannot read. `NavbarSubmenuItem` already takes `to` +
`linkComponent`; the same on `NavbarItem` would remove the copy. The row's
`[&>*]:truncate` also clips a badge on the icon's corner (a lock, a status dot),
which the body has to opt out of.

### F10 — `Input` narrows `type` and `value` below what the element accepts · major

```ts
type?: "text" | "password" | "number" | "email"
value?: string
```

The component forwards both to a real `<input>`, so `type="date"` and a numeric
`value` work at runtime and only the types disagree. XTM One has date fields and
numeric inputs; the adapter carries one documented cast rather than pushing the
narrowing to 261 call sites. Widening to the element's own union (or at least
adding the date/time family) costs nothing behaviourally.

### F11 — no `DialogHeader`, and no access to the portal or overlay · minor

The library ships `DialogBody` and `DialogFooter` but no header block, so a
title + description + actions cluster (188 call sites here) stays local.
`DialogPortal` and `DialogOverlay` are also unexported, which matters for a
consumer composing a non-dialog overlay — XTM One's ⌘K command palette — that
still wants the same scrim behaviour.

### F12 — `Dialog`'s fixed widths do not meet a `max-w`-capped consumer halfway · minor

Sizes are fixed widths (420/640/960px). XTM One caps dialogs with `max-w-*` at 59
of 63 call sites, which the fixed width then overrides downward: a `max-w-3xl`
dialog silently became 640px under `size="md"`. Defaulting the adapter to `lg`
restores the caps' authority, but a `size="auto"` (or honouring an incoming
`max-width`) would be the direct answer.

### F13 — components a product cannot ship without, still `todo`

Card anatomy, Toast/Snackbar, Skeleton, Pagination and EmptyState have no
counterpart. XTM One keeps local implementations for all five (159, 102, 76, 33
and 30 importing files respectively), painted from FDS tokens. Card is `todo` on
the roadmap; the other four are not on it at all, and every product list screen
needs the last three.

**Toast/Snackbar: closed** (`Snackbar`, released in 1.2.0).
`components/ui/toast.tsx` now renders `SnackbarProvider`, `SnackbarViewport`
and `Snackbar`; the `useToast()` API its call sites use is unchanged. The local
copy's token mapping had turned each `*-950/80` tinted surface into an 80 %
fill of a PRIMARY colour (`feedback-success-primary`, `feedback-error-primary`,
`filigran-tonic-primary`), so the info toast was a solid teal block with its
icon and Reload button in the same hue and its description unreadable on it;
the library's 30 % severity wash and left rule replace it. One host-side setting remains: the
viewport is raised to `z-[100]` through its `className`, because this app's
dialogs sit at `z-50` and portal after it.

### F40 — the list-screen components are still `todo`, so their fallbacks stay local · major

The library ROADMAP (read at 1.4.0) lists DataTable, List, Popover, Pagination,
FilterBar, Accordion and Separator as `todo`. Every XTM One list screen needs the
first five. Under invariant 2 (never fork a missing component) these local
implementations are the documented fallback until the library ships each one:
they are frozen in scope, painted from library tokens only, and are not grown
into a second design system. Counts are non-test source files importing each
fallback, `main` at `64ced369`, 2026-11-10.

| Library component (`todo`) | XTM One fallback | Importing files | Need for the library |
|---|---|---|---|
| DataTable | `components/ui/data-table.tsx` (sort, column picker, CSV export, URL-held filters, selection, skeleton rows), `components/ui/entity-table.tsx` | 29 and 11 | sortable headers, column visibility, row selection with a bulk bar, filters and page held in the URL, a loading row state, a sticky header |
| Pagination | `components/ui/pagination.tsx` (on the library `IconButton`) | 34 | page size choice, total count, previous and next as named icon buttons |
| FilterBar | `components/ui/filter-pill.tsx` (pill trigger and panel), `components/ui/list-control-bar.tsx` (search, filters, selection count) | 16 and 14 | a filter pill with a count, a clear-all action, a keyboard-reachable panel |
| Popover | `components/ui/info-popover.tsx` (portal, placement), `components/ui/handover-type-popover.tsx` | 2 and 1 | a non-modal anchored surface with focus return, distinct from `Tooltip` and `Menu` |
| Accordion | `components/ui/disclosure.tsx`, plus raw `<details>` | 18, plus 21 with a raw `<details>` | a single and multiple expand mode with a heading level prop |
| List | none: lists are rendered as `Card` rows or as the data table | n/a | a row anatomy (leading icon, title, meta line, trailing actions) |
| Separator | `MenuSeparator`, `NavbarSeparator`, `SelectSeparator` cover the composite cases; 3 files keep a raw `<hr>` | 3 | a standalone horizontal and vertical rule |

When the library ships one of them, the next XTM One release-follow PR (one per
library release, within two working days) replaces the fallback and removes its
row here.

### F44 — what the DataTable must ship with, so a product swaps its renderer and keeps its call sites · major

XTM One unified every records table on one standard (issue #10916, `dev-docs/reference/data-tables.md`
"Standard v2" and "FDS transition") and shaped `components/ui/data-table.tsx` as the single adapter:
its column definitions and state map 1:1 to TanStack `ColumnDef` / `TableState`, the shape of the
named reference (`@filigran/ui` DataTable). The library DataTable (ROADMAP priority 1, `todo`) replaces
that renderer in one file when it ships the following; each missing item keeps a local fallback:

| # | Requirement | Why |
|---|---|---|
| 1 | A toolbar anatomy with named slots, one row, one control height: left search, scope switcher (segmented, a count per option), filter pills, tags pill, active-filter count with "Clear all"; right total, Columns, page size, Export, view toggle | every list declares the same kinds the same way; no list draws its own filter control |
| 2 | Filter kinds as pills: enum, date range, number range, boolean, owner, tags; never a plain select | one way to filter across products |
| 3 | The total always shown at the toolbar's right end, "N" or "n of N" when a search or filter narrows it; the footer keeps the range and the pager | the user always knows the size of the list |
| 4 | Columns chooser and page size (10, 25, 50, 100) on every table, both kept per user | the same controls on every list |
| 5 | Two row densities only, as tokens: a one-line row and a two-line row (primary line plus one secondary line), one height per table, never a third line | rows align on one grid; no ragged lists |
| 6 | Height to content: the table hugs its rows up to the page size, sticky header in the page scroll; the same for a grid (card) view under the same toolbar and pager | no empty space, no clipped rows |
| 7 | Server mode: manual pagination, sorting and filtering with a `rowCount` | unbounded lists |
| 8 | State in the URL (search, filters, sort, page, page size) through a controlled `state` / `onStateChange`, the URL codec left to the host | a link and the back button restore the view |
| 9 | Row selection with a bulk bar in the toolbar, a disabled reason per row | bulk actions |

### F19 — an icon passed as a Button child renders on its own line · blocker

`Button` renders `children` inside an **unclassed `<span>`**, whose computed
display is therefore `block`. Any host that ships Tailwind's preflight — which
sets `svg { display: block }` — gets an icon on its own line inside the button:

```
computed on the button:  display flex, flex-wrap nowrap, height 36px   ✔
computed on its span:    display block                                  ✘
                         children: [svg, text]  → two lines in a 36px box
```

Measured in a real browser on XTM One's Terms-of-Use screen: three buttons, each
with the icon stacked above its label. Nothing errors, the button's own geometry
is correct, and the library's tests pass — the fault only appears in a host with
a preflight, which the library deliberately does not ship.

The supported route is `iconStart`/`iconEnd`, and most consumers coming from a
shadcn-style codebase will pass icons as children instead, because that is what
their previous Button accepted. XTM One does it in most of its 780 call sites.
Its stop-gap is a host compensation in its own adapter
(`[&>span]:inline-flex [&>span]:items-center [&>span]:gap-[inherit]`), which is
the wrong place for it.

Two fixes worth considering, either of which removes the class of bug: give the
content span `inline-flex items-center gap-[inherit]` in the component, or make
the component's own base immune by declaring `svg { display: inline }` on its
descendants. The first also makes children-as-icons work as consumers expect.

Severity is *blocker* rather than major because it is not a degradation — it
makes the library's most-used component visibly wrong in any preflight host, on
the first screen a user sees.

### F20 — the CI guard artifact models two build shapes, not three · major

`process/artifacts/ci-design-system-secret.test.ts` enumerates
`docker/build-push-action` steps and `docker run` steps. It does not know about
**`docker compose build`**, and XTM One's end-to-end workflow builds the platform
image that way.

The consequence is the exact failure the artifact exists to prevent, with the
added sting that the test was green while it happened: the credential was wired
into every site the guard checks, CI still failed at "Build Docker images", and
nothing local said so. Its own docstring is what makes this worth reporting —
"enumerating those sites by hand is not reliable: it was done twice on this
migration and missed a call site both times" — because a guard that enumerates
incompletely reproduces the defect it replaced.

XTM One added a third case locally (asserting the step exports the secret when
the compose file declares it, mutation-tested both ways) and would rather send it
upstream than keep a divergent copy. Compose declares build secrets in the file
rather than on the command line, so the check has to read `docker-compose.yml` to
decide whether a step needs the credential.

### F46 — the nine `done` components #10533 left: used, or why not · record

The last criterion of xtm-one #10533 is that every `done` component of the
library is used or its deviation recorded. State at `@filigran/design-system`
1.5.0 (2026-11-12), counted on `frontend/src` without tests:

| Component | State in XTM One | Why |
|---|---|---|
| `ColorPicker` | **Used.** `ColorField` (settings), the video studio fields, the template designer, the flow builder node panel, and the fleet colour on the fleet settings panel (moved off `<input type="color">` here, #10533) | No native colour input is left. |
| `TimePicker` | **Used.** The active-hours windows of an agent account (moved off `<input type="time">` here, #10533; stored as `HH:MM`, 24-hour pattern) | No native time input is left. |
| `Avatar` | **Used** for the presentation clicker holder. `PresenceAvatars` stays local | Its colour per user is the point of the presence row (who holds which slide); the contract puts "a colour per user" out of scope (one fill for everyone). |
| `Textarea` | **Used** in 10 files (fleet settings and creation, member instructions, work item close and decision, workspace knowledge, departments, member presets, both AI draft dialogs). 104 raw `<textarea>` sites remain | 12 are monospace prompt and code editors, which the field anatomy does not model. The other 92 sit in forms whose label and help are drawn by `SettingsField` or a local row: the swap changes each field's label wiring, so it moves form by form with the forms program under the UX gate, not as one codemod. |
| `ButtonGroup` | **Not used.** `ViewToggle` and `SegmentedFilter` stay local | Both sit in the list toolbar at the 32 px control height every toolbar control shares (`filter-pill-class.test.ts`); `ButtonGroup` draws 24 px (`sm`) or 36 px (`md`) items, so either size breaks the toolbar line. `SegmentedFilter` also carries text labels, and the item is icon-only. Asked of the library: a 32 px size. |
| `FileSelect` | **Not used.** 37 `<input type="file">` sites stay | All 37 are hidden (`className="hidden"`) behind an import button, an upload menu entry or the `FileUpload` drop zone: none is a labelled form field, which is what `FileSelect` draws, and drag-and-drop is out of its scope. |
| `Drawer` | **Not used.** `SlideOver` (fleet settings, workspace knowledge, conversation files) and the agent map drawer stay local | The agent map drawer keeps a clear backdrop so the map stays in view with the node ringed, a look the contract's fixed overlay does not offer. The `SlideOver` panels are 420, 460 and 480 px wide and draw their own header with a line under the title; the contract fixes the widths (RULE-14) and puts a description line under the title out of scope. |
| `Banner` | **Not used.** The emergency stop, impersonation, licence and install-app strips stay local | The emergency stop banner carries live facts and a reason inside the message with error colour on the text (RULE-05 forbids colour on content); impersonation is a fixed bar above the shell sized by `IMPERSONATION_BANNER_HEIGHT`, which a `BannerStack` would have to drive; the install-app card and the licence status box of the quotas settings are in-page boxes, not application banners (RULE-01, RULE-09). Revisit with a `BannerStack` at the shell. |
| `Hero` | **Not used.** The Value page head (`ValueHero`, a grid of stat cards) and the security audit score card stay as they are | Both are status reads (figures, a score), not an introduction surface with a header and actions; the brand accents would put decoration behind the figures. |

**Asked of the library (ColorPicker).** The component writes its own input
`id` and `aria-describedby` after the props spread, so a caller that labels
the field from outside (`SettingsField`: `<label for=id>` and a helper by id)
cannot pass either: the label points to nothing and the helper is not
announced. The fleet settings panel names the field with
`aria-labelledby` (label id and helper id) instead. Asked: honour a caller
`id` and merge a caller `aria-describedby`.

---

## Token layer, typography and branding

F22 to F25 were found while re-verifying this document against the code on
2026-08-31. They were never filed: each one lives only as a comment in
`frontend/src/index.css` or `frontend/src/lib/color-utils.ts`, which is the wrong
place for a library gap. F31 was reported from production after the adoption
merged.

### F22 — no host-facing accent seam, and white-label cannot reach the gradients · major

A deployment can be rebranded by its operator (`platform_accent_color`), and the
product itself ships in two identities — Copilot blue, XTM One purple. Neither
can be expressed in a library token: FDS colour tokens are generated from Figma
and deliberately not editable, and an operator's colour is a runtime value that
no generated token can name.

XTM One's answer is a product token, `--color-accent`, that product code paints
from and that resolves against three sources in order — operator colour, product
mode, default. That part works and is arguably where it belongs.

What does not work is the half the library owns. Its own components paint from
`--color-filigran-brand-*` and have no notion of a host accent, so a primary
Button stays Filigran blue on a rebranded deployment unless the host **overrides
a Figma-generated token** — which is precisely the thing consumers are told not
to do. XTM One does it in two places and neither is comfortable:

- `[data-theme="xtm-one"]` remaps all three brand steps onto the `ia` family,
  because otherwise the sign-in button renders `#0015a8` beside a `#651fe5`
  identity.
- `buildCssVarsBlock` injects the operator's colour over
  `--color-filigran-brand-primary` at runtime, under a doubled `:root:root`
  selector — the library's themes are declared under `.light`/`.dark`, same
  specificity as a single `:root`, so without the doubling the winner is decided
  by stylesheet order and a build change would silently drop every branding
  colour.

And one part cannot be reached at all: surfaces painted from the brand
**gradients** — the `ia` and `highlight` Button variants, `bg-gradient-*`. Those
consume multi-stop generated tokens that cannot be rebuilt from a single hex
without inventing a palette, so a white-labelled deployment keeps Filigran's
gradient next to the operator's accent. That is a design decision the library
has to make, not a workaround a consumer can choose.

What would close it: a documented accent seam — one token the library's own
components read for their brand colour, that a host may set — plus a stated rule
for what a gradient does when the brand is overridden (derive, desaturate to
neutral, or opt out).

### F23 — the `content-*` family stops at 14px · major

The composite styles split cleanly by role: `content-*` is IBM Plex body text,
`title-*` is Geologica headings. But the content family's largest step is
`content-base` at 14px, while `title-sm` — the first title step — is already
16px. There is no body style above 14px.

Any product with a lead paragraph, a large stat value or an emphasised sentence
therefore has to either borrow a `title-*` (wrong family, wrong weight) or leave
the system. XTM One left it, in 89 places: 65 `text-base` and 24 `text-lg`.

Two sizes are also absent from the raw scale entirely, which is what makes the
mapping lossy rather than merely awkward:

| app size | px | nearest FDS step | verdict |
|---|---|---|---|
| `text-base` | 16 | `title-sm` (16px, Geologica bold) | right size, wrong family |
| `text-lg` | 18 | — (scale jumps 16 → 20) | **no step exists** |
| `text-xl` | 20 | `title-md` (20px) | usable if a heading is acceptable |
| `text-2xl` | 24 | `title-lg` (24px) | usable, same caveat |
| `text-3xl` | 30 | — (scale jumps 28 → 32) | **no step exists** |

Adopting the nearest style would change a size as well as a family at every one
of the 207 sites, which is why the migration stopped. `content-lg` and
`content-xl` steps — body family, 16px and 18px — would close most of it.

### F24 — no data-visualisation or node-graph palette · major

The design language models feedback, entities and elevation. It does not model a
**series palette** (a chart with n comparable series) or a **node-graph
vocabulary** (node kinds, edge states, canvas fills). Both are unavoidable in
this product: XTM One ships six chart components, a flow builder, an agent-flow
canvas and a trace graph.

That is where most of the remaining colour debt sits. Of the 117 hardcoded
colour literals `count-legacy.mjs` counts (as of 2026-10-13), 56 are exactly
this — `flow-builder.css` (27), `node-visuals.ts` (10), `agent-flow.tsx` (9),
`graph-utils.ts` (6), `trace-graph.tsx` (4) — hand-picked hues chosen so that
adjacent nodes stay distinguishable, with a hand-built light-theme counterpart
for each. They are not lazily-written tokens; there is nothing to write them as.

Two properties matter and neither is satisfiable from the current tokens: the
series have to stay distinguishable from each other (not just legible against the
background), and they have to work in both themes without being re-picked by
hand. A generated categorical ramp of 8–12 steps with a light and a dark
resolution would close it.

A smaller relative of the same gap: a decorative **accent tint** at low alpha,
used for glows and gradient washes, appeared 20+ times as
`hsl(197 100% 53% / 0.15)` and friends. The codemod now maps the ones written in
a utility class onto the accent; the 12 left sit in the graph files above. Those
cannot be tokens today because they must follow the runtime accent (F22), not a
fixed hue.

The remaining 61 are mostly correctly outside the design language and are
recorded here so they are not mistaken for debt: 12 third-party brand colours
(Slack, Teams, WhatsApp, Telegram, Discord, Google) in `constants.ts`, 12 in the
white-label palette derivation formula in `color-utils.ts`, which computes
colours rather than naming them, and 30 in test files that assert on colour
values (the arcade engine, the colour utilities, the deck theme). The last 7 are
single literals in screen code (scrim shades, a neutral chart axis, one amber),
small enough to belong with F25.

### F25 — three values a host must invent · minor

Small, but each one is a host re-deciding something the design language should
settle:

- **A scrim.** FDS paints elevation with colour rather than shadow and has no
  token for the dimming behind a modal. XTM One composes one from `--gray-1000`.
- **A composed overlay shadow.** `--shadow-sm|md|lg|xl` are depth *radii*
  (4/8/16/20px), not box-shadows (see F05). A menu or popover floating over
  content still has to detach from it, so the app composes its one shadow from
  those radii — a reasonable guess, not a design decision.
- **Font fallback stacks.** The typography tokens name one family each and stop
  (`--font-sans-plex: "IBM Plex Sans"`). If the webfont does not arrive — an
  air-gapped deployment, a blocked CDN, a slow first paint — the browser falls
  back to its own default, a serif on most, and the entire app renders in Times.
  Fonts are documented as the host's responsibility, and a fallback stack
  arguably is one; but the failure mode is severe enough that shipping the stacks
  (or documenting the trap) is worth more than the purity.

### F31 — the input backgrounds do not follow `.layer-N` · major · closed in 1.3.0

**Closed** by the library in 1.3.0 (CHANGELOG e6b6414, "fields follow `.layer-N`"):
every `.layer-0` … `.layer-3` block now re-declares the three. At the 1.4.0
pin XTM One deleted its host copy from `index.css`;
`tests_unit/layer-input-aliases.test.ts` keeps holding a field apart from its
surface at every layer in both themes and fails if the host re-declares an
input token on a layer. The history below is kept for the record.

`--bg-input-default`, `--bg-input-hover` and `--bg-input-disabled` are declared
once, on `:root`, as `var()` of the elevation aliases each `.layer-N` block
re-points. A `var()` inside a custom property is resolved on the element that
declares it and inherited as a value, so the three keep their layer-0 value at
every depth. In the dark theme the layer-0 field colour (`#13213e`) is exactly
the layer-2 surface, and `DialogContent` is a layer-2 surface: every `Input`,
`Select`, `SearchField` and `Combobox` in a dialog painted the dialog's own
colour, and with no resting border nothing marked the field. The light
theme has the same collision on layer 3 (`#e4e5e7` on `#e4e5e7`).

The library already describes the mechanism (`process/LEARNINGS-ACTIVE.md`
L133), and OpenCTI filed it as its `LIBRARY-FEEDBACK` #57; OpenCTI and OpenAEV
both compensate on the host side. XTM One now does too: `index.css` re-declares
the three, verbatim, on every `.layer-N`, and
`tests_unit/layer-input-aliases.test.ts` models the cascade from the installed
sheet, holds a field apart from its surface at every layer in both themes, and
fails once the library's layer blocks carry the declarations, so the copy is
deleted then rather than left to shadow the fix.

What would close it: the token generator emitting those three declarations
inside each `.layer-N` block — they are token declarations, so the fix goes
through the Figma export pipeline — and a `validate:theme` rule that fails on
any root alias reading an alias a layer block re-declares, the gate L133 notes
does not exist.

---

## Doctrine and accessibility

### F14 — `Header`'s fixed-only doctrine · minor

The component carries no positioning and its RFC states the bar is always fixed,
never sticky. XTM One keeps `sticky`, because `fixed` moves scrolling into the
content zone and changes scroll restoration, in-page sticky headers and the
assistant panel's geometry all at once — a change worth making on its own, not as
a side effect of adopting a component. Recording it here rather than pretending
compliance.

**Shell audit against `rules/Layout.md` (1.4.0), 11 Nov 2026.** Aligned in the
same pull request: Content padding `p-6` at every width (was `p-4`/`sm:p-6`/
`lg:p-8`, and the three full-bleed pages bled by the same ladder), the shell
surface `bg-gradient-default` (was `bg-elevation-default`). Deviations kept,
each with its reason: (0) no Content minimum of 880 px: with the expanded
navbar it scrolls every page sideways at 1024 px, a width the UX review gates
(1024/1440/1920) and the `*-fit` E2E specs hold without a page scroll (an
`lg:min-w-220` attempt turned `agents-list-fit` red at 1024 px on this PR);
(1) sticky Header, above; (2) the navbar widths 180/48 and the header height 68
are mirrored in `layout-metrics.ts` for the content offset, because the library
publishes `--fds-header-height` but no navbar width property; the two are
pinned to the library by `tests_unit/header-height.test.ts` (request: a
published `--fds-navbar-width` that follows collapse, then the mirror goes);
(3) the Content is not a 12-column grid at the shell level: each page lays out
its own grid, a shell-level grid would wrap every existing page in one column
and change nothing visible; (4) a mobile companion shell and an overlay navbar
below `lg` exist although the contract scopes mobile out.

### F15 — inherited WCAG failures · minor

`Switch` and `Chip` are marked WCAG-failing upstream. XTM One adopted `Switch`
and imports its failure into 137 call sites. It did NOT adopt `Chip` — see F06 —
so the 602 badge call sites are spared, but only as a side effect of a different
gap: close F06 and the failure arrives with it.

Not a request to patch them in the product — a request to know the expected fix
date, since the product's own accessibility posture now depends on the
library's.

### F26 — `Input` overwrites the caller's `aria-invalid` and `aria-required` · major

```js
{ ...props, "aria-required": required || void 0, "aria-invalid": showErrorIcon || void 0 }
```

Both attributes are written after the caller's props are spread, from the
library's own `required` and `error` props. A field that marks itself invalid
with `aria-invalid` (and shows the state another way: a red border, a summary
alert) is announced as valid, and a field whose requirement depends on another
choice (an embedding model required only when the provider cannot list its
models) loses `aria-required`. The library's props are no workaround: `error`
also draws an icon inside the field and a message under it, which a compact
field (a context-window box in a model list) has no room for. Found by a
component test and two end-to-end tests after a merge with main, not by review.
The adapter re-applies the caller's values once the library has rendered. The
fix upstream is to spread the caller's props last, or to let an explicit
`aria-*` win when the library's own prop is absent.

### F27 — Radix is a dependency, not a peer, so a host gets a second copy · major

The library declares its Radix primitives as `dependencies` with caret ranges
(`@radix-ui/react-dialog: ^1.1.23`, `react-tooltip: ^1.2.16`, …). A host that
also uses Radix and pins older versions gets the library a private copy under
`node_modules/@filigran/design-system/node_modules/@radix-ui`, and Radix keeps
its layer stack and tooltip provider in module state: a library `Dialog` and a
host dialog opened over it no longer know which is on top. In XTM One, Escape
in a deck's shortcut sheet also closed the template preview around it, which
only an end-to-end test noticed. The host's fix is to pin the library's
resolved versions (done, and held by `tests_unit/single-radix-copy.test.ts`);
the library's would be to declare Radix as `peerDependencies`, so a mismatch
is a warning at install instead of two silent stacks.

### F28 — `Combobox` warns about its own decorative checkbox · minor

A `Combobox` with `multiple` draws `<Checkbox checked presentational />` in each
option row - decoration, the row itself is the selectable control. The
`Checkbox` still runs its accessible-name check and warns
"`[Checkbox] rendered without a label`" once per row, on every render. A host
cannot silence it: the app renders no `Checkbox`. XTM One's test suite turns
every library warning into a failure (`src/testing/design-system-warnings.ts`)
and has to exempt this one by message. The fix is in the library: skip the
name check when `presentational` is set.

### F29 — `Input`'s name check ignores `<label htmlFor>` · minor

`Input` warns unless it has a `label` prop, an `aria-label` or an
`aria-labelledby`. A field named by a `<label htmlFor={id}>` - a real
accessible name in every browser - is reported as unnamed. XTM One had about
70 such fields; they now carry an `aria-label` repeating the label's text, so
the warning stays meaningful. Checking the DOM for a `label[for]` pointing at
the field after mount would remove the false positive, or the docs could state
that the library's three ways are the only ones it accepts.

### F30 — the dialog-title check cannot see a title a child component renders · minor

`DialogContent` warns when no `DialogTitle` is found, and finds it by walking
the React element tree it was given. A title rendered by a child component
(`<DialogContent><Form/></DialogContent>`, the `DialogTitle` inside `Form`) is
not in that tree, so a named dialog warns. Radix's own check reads the
rendered title through context instead, which has no such blind spot. XTM One
moved its two such titles into the dialogs' own JSX and holds every dialog to
that with `tests_unit/dialog-title.test.ts`.

### F38 — `SearchField` gives its landmark and its searchbox the same name · minor

`SearchField` renders a `role="search"` wrapper around an `<input
type="search">` and writes the one `aria-label` it is given on both:
`aria-label={props["aria-label"] ?? "Search"}` on the wrapper, the same value
on the input (1.2.0 and 1.3.0 alike). A named field is therefore announced
twice, landmark then searchbox: "Filter skills..." on the agent Skills card,
"Search agents" on Agents, and the same on every `SearchInput` consumer. A
host cannot separate them: leaving `aria-label` out names the landmark
"Search" but leaves the input unnamed, the defect XTM One fixed
(`SearchInput` now defaults the label to the placeholder). The fix upstream is
a separate landmark label (a `searchLabel` prop defaulting to a generic
"Search"), or the generic name on the landmark whatever the input's label is;
the specific name belongs to the searchbox alone.

### F32 — `Combobox` ignores a disabled `<fieldset>` · major

A `<fieldset disabled>` disables the input inside a `Combobox`, but the field
shell opens the panel on a click whatever the fieldset says (it reads the
engine's own `disabled` only), and a row of that panel can be picked: a form
rendered read-only that way (XTM One's AI-off settings, a Community Edition
section, a read-only agent account) becomes editable through it. `Select` has
no such gap, its trigger being a native button. XTM One's `SearchableSelect`
reads `closest("fieldset:disabled")` on its input and passes `disabled`;
the library could do the same, or test `:disabled` on its input before
opening.

### F33 — `Combobox` cannot mix ungrouped options with groups · minor

With `groupBy`, every option sits under a header: one whose group is `""`
still gets the 32px header row, empty. A model picker lists "Platform default"
above the provider groups, so it has to invent a header for it ("Defaults",
`SearchableSelect`'s `pinnedGroup`). Options for which `groupBy` returns
`null` could render before the groups, with no header.

### F34 — the single-value change is typed `T | T[] | null` · minor

`onValueChange` never receives an array without `multiple`, but its type does
not narrow on that prop, so every single-value host narrows by hand. An
overload keyed on `multiple` would carry it.

### F35 — `Input` styles its wrapper from `className`, and has no compact size · minor

`Input`'s `className` lands on the wrapper `<div>`, not the `<input>`, so a
caller cannot size the field itself: a `h-6` or `text-center` has no effect,
and a field placed in a list row keeps the library's 36px height and overflows
it. XTM One's `Input` adapter adds `compact`, which reaches the field through
`[&_input]:` variants on the wrapper (a model's context window on a provider
card). A `size` prop, or an `inputClassName`, would carry it.

### F39 — `Input` draws its own 10px character counter whenever it gets `maxLength` · minor

Given `maxLength`, `Input` renders a `52/500` counter in `text-1
text-input-helper` (10px), below the 12px floor XTM One holds for help lines,
and with no opt-out. A host that already shows its own counter tied by
`aria-describedby` gets two counters, so XTM One drops `maxLength` and caps the
value in `onChange` (`components/ui/outcome-review.tsx`, #9365). Every other
`Input` with `maxLength` on main still shows the 10px counter. A
`showCount={false}` prop and a 12px helper size would carry it.

### F16 — `IconButton`'s required label is right, and it exposes our debt

`IconButton` requires an accessible label. Most of XTM One's 58 icon-only
buttons did not have one; many relied on a wrapping tooltip, which is not an
accessible name. That is XTM One's defect, not the library's, and it is recorded here
because the library is what surfaced it.

**Closed since.** The audit ran (follow-up 1): 55 moved to `IconButton`, 3 kept
an explicit `aria-label`, and 53 of the 58 turned out to have had no accessible
name at all. Two more, behind a permission branch the audit never rendered,
turned up later (follow-up 1b), which makes 60 in all: 55 with no name, 57 now
`IconButton`. Two static guards now hold the line
(`frontend/tests_unit/icon-button-accessible-name.test.ts` and
`switch-accessible-name.test.ts`), because the blind spot is permission- and
state-gated UI that a browser pass never renders — see follow-up 1b.

### F17 — `feedback-alert-primary` is unusable for text on a light surface · major

Measured against `--bg-elevation-default` in a real browser
(`frontend/scripts/gallery-axe.mjs`):

| token | light | dark |
|---|---|---|
| `feedback-alert-primary` | **1.54:1** | 11.3:1 |
| `feedback-warning-primary` | 4.32:1 | 6.19:1 |
| `feedback-success-primary` | 4.98:1 | 6.37:1 |
| `feedback-error-primary` | 5.92:1 | 5.18:1 |

Alert is the natural token for the amber this app paints, and in light mode it is
invisible. The split the codemod produced is live (as of 2026-10-13, outside
tests): 786 `feedback-warning-*` classes, nearly all text, and 524
`feedback-alert-*`, nearly all surfaces and borders. XTM One's codemod therefore splits by prefix — text
takes `warning`, surfaces keep `alert` — which is a workaround, not a fix:
`warning` at 4.32:1 still misses AA for normal text. The design language has no
accessible yellow or orange for text on a light surface. `count-legacy.mjs`
holds the split: "alert token used for text" is a category at 0 in CI, after
the navbar's lock icons and nine status colours were found still on `alert`.

Related, and the reason the workaround is only a workaround: a coloured label on
a `/20` tint of its OWN hue never has much contrast to work with. XTM One's
status badges scored 3.73–4.4:1 that way in both themes and now use the
library's `bg-alert-*` surfaces with ordinary `text-default-primary` on top,
which clears 10:1. That pairing is worth documenting as the intended one.

**Text ON the fill is the other half, and the library has no token for it.**
Where the amber stays a solid fill (the impersonation banner, the approvals
badge), the label on it needs a colour that does not move with the theme,
because the fill does not: `feedback-alert-primary` is the same light amber in
both. The candidate a host reaches for, `text-negative-primary`, is the label of
the accent and inverts with the theme, which is right on the accent and wrong
here: on the amber it reads at 10.2:1 in dark and **1.5:1 in light**. XTM One
shipped the impersonation banner that way, and no browser pass saw it, because
the banner only renders while an admin impersonates. The scrim has the mirror
problem (dark in both themes, so the inverting label goes dark on dark).

XTM One now defines `--color-on-feedback-fill` (gray-900) and
`--color-on-scrim` (white) in its token layer, and
`tests_unit/fixed-fill-text-contrast.test.ts` holds both at 4.5:1 in both themes
from the library's own tokens. The orange `feedback-warning-primary` fill has no
such colour: nothing clears 4.5:1 on it in light, so the badge moved to the
amber. The ask: an `on-*` text token for every fill that keeps its colour across
themes, beside the ones that invert.

### F18 — the primary Button's hover surface misses AA in dark · major

The label is `--text-negative-primary`, dark text in the dark theme. The hover
step darkens the surface under it, one shade too far:

| mode | rest | hover |
|---|---|---|
| Copilot (`brand-*`) | blue-400 — **9.32:1** ✓ | blue-700 — **3.6:1** ✗ |
| XTM One (`ia-*`) | purple-300 — 5.55:1 ✓ | purple-500 — 2.41:1 ✗ |

The resting state is comfortable in both; only the interactive state fails. The
light theme is unaffected — its label is light text, and darkening reads at
11.4:1.

Two things make this worth fixing rather than allow-listing further. It cannot
be fixed by choosing a different label colour: on blue-700, dark text is 3.6:1
and light text 4.36:1, so **neither passes** and the surface itself has to move.
And one step is enough — blue-600 clears it at 5.79:1 while keeping the darkening
direction the design intends. Purple has no passing darker step (purple-400 is
3.54:1), so there the hover has to lighten instead.

XTM One overrides both from the host and will delete the override when this
lands. Recorded honestly: an earlier version of this entry claimed the button
failed in its DEFAULT state, which was wrong — that reading came from an axe run
whose own click had left the button active, and the resting contrast is 9.32:1.

The roadmap allow-lists Button's `wcagStatus: fail` until 2026-08-31 for two
other pairs (the highlight gradient's second stop, `tonic-primary` in light at
2.16:1). This is a third, and it is the one a user meets on every click.

### F48 — `feedback-error-primary` error text misses AA on a dark dialog surface · minor

Found in the UX review of xtm-one #11540 (round 2, 2026-11-13). In dark, an
inline field error in `text-feedback-error-primary` (rgb 241 67 55) at 12 px on
the dialog surface (rgb 19 33 62) measures **4.26:1**, below the 4.5:1 AA floor
for normal text. F17 measured the same token at 5.18:1 on
`--bg-elevation-default`; the dialog's raised surface is lighter, so the margin
is gone. Light is unaffected (6.02:1 for the text, 5.25:1 for the error border).

It is not a product choice: XTM One's shared `SettingsField`
(`frontend/src/components/ui/settings-form.tsx`) uses the same token for every
form error, so every dialog form inherits it. The ask: an error text token that
clears 4.5:1 on every elevation surface in dark, or an `on-surface` error
variant for raised surfaces.

Seen again in the UX review of xtm-one #11666 (round 1, 2026-11-13): the
CTEM Cases board's overdue line, 10 px on the layer 2 column (`#13213e`),
same 4.26:1. The product falls back to `text-feedback-error-tertiary`
(`--red-300`, 5.69:1 in dark, 9.86:1 in light) for the text and keeps the
error primary on the icon; `frontend/tests_unit/ctem-cases-board-contrast.test.ts`
holds the ratio.

---

### F41 — product design rules that were stricter than the library · minor

XTM One's own review standards had grown rules the library does not carry.
Under the order of authority recorded in
`dev-docs/reference/frontend-design-system.md` (shipped canonical files, then the
consumer skill and its usage contracts, then product standards only where the
library is silent), a product rule may never be stricter than the library. The
rules below were found in conflict and are withdrawn on the product side as of
2026-11-10; they are listed so the library owners can say whether any of them
should become a library rule instead.

| Product rule (withdrawn) | Library position | Product action |
|---|---|---|
| A 12 px minimum text size on every surface | The token scale ships `content-caption` at 10 px; a `Chip` or `Badge` owns its type size | Floor guard tests removed (xtm-one #10538, squash `64ced369`) |
| A minimum `h-3` icon inside badges and chips | `Chip` and `Badge` own their icon size | Same removal; issue #10531 closed not planned |
| Review and layout width of 1024 px, with columns hidden below 1280 to avoid a horizontal scroll | `rules/Layout.md`: minimum width 1080 px, below it Content goes fixed-width with a global horizontal scroll, the shell never compresses | Screens are reviewed at 1080 and 1728; columns hidden only to dodge a horizontal scroll are re-judged at 1080 by the program's shell slice |

Open question for the library: should a caption-sized text (10 px) carry a
minimum contrast step stronger than body text? Our reviews used the 12 px floor
as a legibility proxy; the library may prefer to state it as a contrast rule on
`content-caption` rather than a size rule.

## What worked, and is worth keeping

- `--fds-header-height` — publishing the bar's height as a custom property with
  the default in the `var()` fallback is exactly the right shape. XTM One drives
  it from its own constant, so the bar, the content offset and the assistant
  panel cannot drift apart. **The navbar widths need the same treatment**:
  `w-45`/`w-12` are internal, the consumer has to mirror 180/48px by hand, and
  `LAYOUT.md` already records that drift risk against the library's own shell.
- `aria-current="page"` as the selected-row mechanism, rather than a prop.
  Routing stays the single source of truth, and the adapter has nothing to keep
  in sync.
- `NavbarSubmenu`'s dual mode deleted ~120 lines of hand-rolled flyout, its
  hover-intent handling and its Escape wiring.
- The elevation model. XTM One's six-step `text-muted-foreground/70…/20` ladder
  and its `bg-foreground/5` hover surfaces were opacity hacks standing in for
  exactly this; collapsing them onto three text levels and four layers is a
  simplification, not a loss.
- `process/artifacts/ci-design-system-secret.test.ts` — copied in with five
  constants changed and it immediately guarded four install sites. Shipping the
  guard alongside the playbook is worth more than the prose.

---

## Follow-ups owned by XTM One

Not library gaps — work this migration deliberately left, recorded here so it is
inherited rather than rediscovered. Each is a separate change with its own
reason to exist.

| # | Follow-up | Why it was not done here |
|---|---|---|
| 1 | ~~Label the icon-only buttons and move them to `IconButton`~~ | **Done.** 55 became `IconButton`; 3 could not (two carry a count badge beside the icon, one is a link) and got an explicit `aria-label` instead. 53 of the 58 had no accessible name at all — 26 relied on a tooltip, which is a description and not a name, and 27 had nothing. 24 new message ids across the four catalogues. |
| 1b | ~~Name the switches, and audit what axe cannot reach~~ | **Done.** Found while verifying the above: a `role="switch"` on the profile page has neither `aria-label` nor a `label[for]`, so it announces as an unnamed switch. The traces filter panel has eight more. **The count in row 1 is the count on the pages axe rendered, not the count in the app** — the end-to-end suite later found two more on the agent header (delete had no name at all, enable/disable had only a tooltip), both behind a `canManage` branch that never rendered during the audit. Permission- and state-gated UI is the blind spot, and a browser pass cannot close it, so the answer was two static checks rather than more fixtures: `switch-accessible-name.test.ts` and `icon-button-accessible-name.test.ts`, both mutation-tested, both re-run on every merge from main (each merge has found new unnamed controls). |
| 2 | ~~`detail-tabs` → `Tabs`, `multi-select` → `Combobox`, `overflow-menu` → `Menu`~~ | **Done.** `TabsTrigger asChild` keeps the tabs as router links, `activationMode="manual"` stops Radix navigating on arrow keys, and `Combobox`'s `multiple` prop replaced the hand-rolled trigger and chips. |
| 3 | The rest of the type scale | Blocked on F23, not on effort. 207 sites across five sizes; two of them (18px, 30px) have no step in the library scale at all, and the body family stops at 14px, so adopting the nearest style changes the family as well as the size. |
| 4 | 117 remaining colour literals (as of 2026-10-13) | Broken down in F24: 56 are chart and node-graph colours the design language does not model (blocked on the library), 12 are third-party provider brand colours (correctly not design-system colours), 12 are the white-label derivation formula and 30 sit in tests that assert on colours. CI holds every category at its baseline (`count-legacy.mjs --check` against `count-legacy-baseline.json`), so the figure can only go down without a deliberate baseline change. `node scripts/fds-migration/count-legacy.mjs --list` enumerates them; every other residue category is at 0. The accent written inside a composed arbitrary value (`shadow-[0_0_30px_hsl(197_100%_53%/0.08)]`) has a rule of its own: the utility-level rules replace a whole class and could not reach a colour that is one term of a glow. The count has still grown with nearly every screen main added (72 when the migration started), which is the shape of the problem: it is a per-screen tax, not a fixed backlog. |
| 5 | Local primitives with no library counterpart | Skeleton, Pagination, EmptyState and the Card anatomy stay local (see F13), and the status badge (see F06). Toast moved onto the library's `Snackbar` at 1.2.0. All are painted from library tokens; none is a library component. Blocked on the library, not on this product. |
| 6 | ~~Run the end-to-end suite~~ | **Done in CI**, which is the only place a stack exists. Running it is what surfaced the selector debt: the migration changed what the controls *are*, so `DetailTabs` tabs stopped matching `getByRole('link')` (an explicit `role="tab"` overrides the implicit link role) and the `Combobox` replaced the "N selected" trigger and its checkmark with chips and `aria-selected`. Retargeted onto the accessibility tree rather than onto markup. |
| 7 | `MultiSelect` should use `ComboboxLabel` | Its input is named by an `aria-label` duplicating the placeholder, because the placeholder alone switches to the search hint once something is selected and the field loses its name exactly when it has content. `ComboboxLabel` wires `htmlFor` to the *visible* label, which is strictly better — one name, and it is the text on screen. It means moving each call site's own `<p>` label inside the component, which is a change to every call site's markup rather than a one-line fix. |
| 8 | Adopt `Slider` and `ProgressBar` (`Spinner` **done**) | **Spinner done** (2026-11-11): every `Loader2`/`LoaderCircle ... animate-spin` site renders the library `Spinner` (xtm-one #10936 to #11018, squash `952b0a06`; the status-icon map stays for the icon codemod; sizes above 32px recorded in F45). Slider and ProgressBar remain. Found while re-verifying this document, not during the migration. All three are exported at the current pin and none is imported: the app still ships 14 raw `<input type="range">` across 9 files, 232 `animate-spin` spinners, and hand-rolled percentage bars in 16 files. Not a library gap — the components exist and were simply missed, because the migration targeted `frontend/src/components/ui/` and these three had no local primitive to replace. |

## Open action

XTM One still owes the library a pull request adding itself as **pilot #5** to
the pilot index in `process/PRODUCT-IMPLEMENTATION-PLAYBOOK.md`, which that
playbook's final checklist requires. Not opened yet — it needs a decision on who
lands cross-repository changes for XTM One.
