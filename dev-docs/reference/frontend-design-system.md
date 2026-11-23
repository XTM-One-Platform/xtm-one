# Frontend design system, theming and editions

The `@filigran/design-system` library and its tokens, the light / dark theming contract and the edition (licence) gate in the UI.

## Order of authority

`@filigran/design-system` (XTM-Foundation/filigran-design-system) is the
single source of truth for every UI decision. When two sources disagree, the
higher one wins:

1. What the installed package ships: the component Meta
   (`@filigran/design-system/meta`), `llms.txt` / `llms-full.txt`,
   `tokens/theme.css` and `icon-mapping.json`.
2. The shipped consumer skill, read from `node_modules`, never copied into
   this repository:
   - `@filigran/design-system/skills/consumer/design-system-usage/SKILL.md`,
     the entry point;
   - `skills/consumer/usage-contracts/<Component>.md`, one contract per shipped
     component (when to use it, its props, its rules);
   - `skills/consumer/rules/Layout.md`, the application shell, the page grid,
     the scroll zone and the minimum width.
3. Product rules, only where the design system is silent.
4. This document and the other `dev-docs/reference` files.

The product is never stricter than the design system: a local rule that
contradicts a usage contract (a type size, an icon size, a minimum width) is
removed, not kept beside it. A need the library does not cover yet is written
to `fds-migration/LIBRARY-FEEDBACK.md` for its owners, and
the local fallback stays documented there, never forked further.

Reviews judge a change against the version pinned in `frontend/package.json`;
an unreleased change on the library's `main` is an advisory, not a blocker.
Each library release is followed by one upgrade pull request.

The sections below describe how this repository wires the library in (the
safelist, the theming seams, the product tokens it leaves to the host, the
editions). For how to use a component, read its usage contract.

## The design system library

- **`@filigran/design-system`** is the Filigran design system, installed from
  the public npm registry at an exact version (bump deliberately, then re-run
  `scripts/fds-migration/generate-safelist.mjs`). It owns the design language:
  tokens, the shell (Navbar / Header / ProductSwitcher) and the primitives. Do
  NOT reimplement one of its components, and do NOT hardcode a value it has a
  token for.
- **Tailwind CSS 4** is configured entirely in CSS (`src/index.css`): there is
  no `tailwind.config.ts`. Tailwind's default colour palette is deliberately
  DISABLED (`--color-*: initial` in the design system's theme), so
  `text-red-400` and `bg-white` do not resolve. That is the point: it makes
  bypassing the design language fail loudly.
- **Radix UI** primitives are reached through the design system rather than
  directly. A raw Radix import in product code is a smell: check whether the
  library already wraps it.

## Design System Quick Reference

Every colour, radius and type style comes from `@filigran/design-system`. The
old app-local names (`bg-card`, `text-foreground`, `text-cyan`, `border-border`,
…) no longer exist.

| Token                        | Usage                                                         |
| ---------------------------- | ------------------------------------------------------------- |
| `bg-elevation-default`       | Surface at the current elevation layer                        |
| `layer-1` … `layer-3`        | Raise a container's elevation context; a card is `layer-1`    |
| `bg-elevation-hover`         | Hover / pressed surface                                       |
| `bg-elevation-highlight`     | Highlighted surface within a container                        |
| `text-default-primary`       | Primary text                                                  |
| `text-default-secondary`     | Secondary text                                                |
| `text-default-disabled`      | Disabled / tertiary text                                      |
| `text-negative-primary`      | Text ON an accent or inverted surface                         |
| `border-elevation-subtle`    | Default hairline                                              |
| `border-elevation-subtle-soft` | Fainter hairline (15% variant)                              |
| `text-accent` / `bg-accent/10` | The product accent — see below                              |
| `text-feedback-warning-tertiary` | Warning text ON a warning or alert wash (`bg-feedback-warning-primary/10`, `bg-feedback-alert-primary/[0.06]`, any tint up to /30): the primary text on its own wash is 3.3:1 to 4.9:1; `tests_unit/warning-chip-contrast.test.ts` holds 4.5:1 on every surface in both themes and keeps the primary off a wash |
| `text-feedback-{error,success,alert,warning,info}-primary` | Status colours. `text-feedback-warning-primary` is never faded with an opacity modifier: at full strength it reads 4.5:1 on the faint amber boxes (`/5` to `/10`) placed on a card or dialog (`--bg-elevation-default-layer-1`) in both themes, faded it does not; a quieter line takes `text-default-secondary`. A warning box drawn straight on the page background (`layer-0`: the dashboard notice, the CTEM registration panel) paints the opaque `bg-warning-box` (`--color-warning-box`: the tint mixed into the card surface), because a translucent tint there stays at 4.20:1 in the light theme (`tests_unit/warning-text-contrast.test.ts`). The same test counts the translucent amber boxes (`bg-feedback-alert-primary/5` or `/10`) per file and fails on a new one, so a page-level box takes `bg-warning-box`. `DashboardNotice`: warning is the only variant whose text keeps its tone colour (amber on the opaque `bg-warning-box`); error, success, progress and info draw `text-default-primary` / `text-default-secondary` with the tone on the icon, border and tint, and a new variant follows that default-text pattern. Warning text in a row that takes `hover:bg-elevation-hover` on `layer-1` takes the shared `WARNING_ON_HOVER_TEXT` from `src/lib/warning-text.ts` (`text-feedback-warning-primary light:group-hover/hover-row:text-feedback-warning-tertiary`), never a copy of the string: the primary reads 3.84:1 on the light hover fill, the tertiary 9.58:1; the dark theme keeps the primary, 5.45:1 on its hover fill. The row carries the named group `group/hover-row`, never only a bare `group`: a bare `group-hover` also follows an outer `group` (the chat message bubble) and recolours rows that are not hovered. Used by the schedule row, the run card and its lightweight row with the approval prompt they render (`action-proposal.tsx`; its tool name on the amber `/5` box takes the tertiary), and the runner activity header; the test gates copies, bare warning text in those rows and the named group |
| `text-filigran-ia-primary`   | AI-generated / AI-adjacent surfaces                           |
| `content-base` / `content-compact` / `content-caption` | Body type styles            |
| `title-xs` … `title-2xl`     | Heading type styles                                           |
| `font-sans-geologica` / `font-sans-plex` / `font-mono-plex` | Font families           |
| `rounded-sm` (4px) / `rounded-md` (12px) | The radius scale. A chip, badge, tag or tone label takes `rounded-sm` with `px-2` and a 1 px edge, the shape of the library `Chip`: draw it with `Badge` / `ToneBadge`, never a hand-made `rounded-full` pill (avatars, dots, bars, spinners, round buttons and counters stay round). A count on its own (a card header or tab count) is a `CountBadge`, the 20 px round counter, never a chip-shaped `Badge`. Guard: `src/chip-radius.test.ts` |
| `ring-focus`                 | Focus ring. Anything that acts as a button draws the design-system Button's ring, 2 px offset plus 2 px `ring-focus` (`focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus`): `BUTTON_ROLE_FOCUS_RING` in `lib/button-role.ts` for an element given `buttonRoleProps()`, `TIP_WRAP_FOCUS_RING` for the Tip wrapper, and `FIELD_FOCUS_RING` for a hand-written text field, so Tab keeps one ring geometry. A KPI tile that fills a DashboardGrid widget is clipped by the widget scroller, so the widget's unclipped wrapper (`WIDGET_WRAPPER` in `components/dashboard-grid/DashboardGrid.tsx`) draws the same ring while its button-role root has keyboard focus. A clickable `StatCard` (`onClick`) draws its own pointer cursor and hover surface; callers add neither. Gate: `src/lib/button-role-focus-ring.test.ts` |
| `stagger-children`           | Staggered entrance animation for child cards                  |
| `animate-fade-in`            | Fade-in entrance animation                                    |
| `animate-appear-late`        | A spinner or busy badge shown only once the wait lasts (0.35 s) |

**Three tokens are the product's own**, declared in `index.css`, because the
design system deliberately does not model them:

| Token                | Why it exists                                                        |
| -------------------- | -------------------------------------------------------------------- |
| `--color-accent`     | The accent seam: blue in Copilot mode, IA purple in XTM One mode, an operator's colour under white-label. Use `text-accent` / `bg-accent/10` / `border-accent/30` and never name the underlying family. |
| `--color-scrim`      | Modal dimming (`bg-scrim`) — FDS paints elevation with colour and has no scrim token. `--color-scrim-strong` is the darker step, for a control's hover on a scrim. |
| `--color-on-feedback-fill`, `--color-on-scrim` | Text on a fill that keeps one colour in both themes: the solid amber `bg-feedback-alert-primary` (gray-900) and the scrim (white). `text-negative-primary` inverts with the theme, so it is right on the accent only; `tests_unit/fixed-fill-text-contrast.test.ts` holds both at 4.5:1. |
| `--color-qr-quiet-zone` | The quiet zone around a QR code (`bg-qr-quiet-zone`): white in both themes, since a scanner reads dark modules on light. |
| `--shadow-overlay`   | The one shadow this app uses, for menus and popovers. FDS `--shadow-*` are depth radii, not box-shadows. |
| `--shadow-glow-accent`, `--shadow-glow-accent-strong` | The intended accent glow of a floating panel that should read lit rather than lifted (the arcade pause and game-over panels, its "answer ready" toast). See "Shadows". |

Never edit the design system's tokens: `theme.css` is generated from Figma; a
host-side override belongs in `index.css` (the product tokens above). A
component the library lacks is filed in `fds-migration/LIBRARY-FEEDBACK.md`
and named in review, never forked locally or approximated with raw Tailwind.

Finding a token: `node_modules/@filigran/design-system/dist/tokens/theme.css`
is the generated source of truth. `node scripts/fds-migration/count-legacy.mjs`
reports styling that still bypasses it; `yarn build && node
scripts/fds-migration/verify-classes.mjs` fails on a class that looks like the
design system's but compiles to nothing — the failure mode where a component
keeps its layout and loses its colours. CI runs both (`count-legacy.mjs --check`
fails a category above `count-legacy-baseline.json`); lower the baseline when
you clear residue, never raise it to pass.

`src/dev/PrimitiveGallery.tsx` is the visual reference of every styling axis
(surfaces, the text ladder, the accent, the feedback palette, the primitives):
a second Vite entry served at `/gallery.html` in dev only (`src/dev/gallery.tsx`,
without the app's providers), absent from the production bundle. Screenshot it
before and after a token change: a diff of two captures is the evidence that a
mapping kept its contrast.

## Page-title actions

The actions beside a page title (a list page's `text-3xl` title row, an
`EntityPageHeader` `actions` slot, what a tab renders through
`FlowsHeaderActions`) follow one order on every page (UX advisory): the secondary actions (outline, secondary, ghost) come first and the
one filled primary action sits at the right end of the labelled actions, as
on Presentations (Import, then New presentation) and Agents (Import, then New
Agent). Icon-only controls (enable, delete) may follow the primary at the far
right. The same order holds in an empty state that repeats the actions and in
a dialog footer (Cancel, then the filled Save). A built-in or read-only entity
keeps this rule: its Duplicate is outline like every other secondary action
(Template detail), and the read-only state is carried by the Read-only
badge, never by a second filled button. A list page's title action row and a
tab's `FlowsHeaderActions` row space their actions with `gap-3` (12 px), as on
Presentations and Agents; the `EntityPageHeader` actions slot (small buttons)
keeps the `gap-2` the component sets.
`components/ui/page-title-actions-order.test.ts` reads every page under
`src/pages` and fails when a labelled secondary `Button` follows a filled one.

## Design-system checks in tests

A design-system warning fails the vitest test that rendered it
(`src/testing/design-system-warnings.ts`, both setups): the library reports a
control with no name or a component used outside its contract on
`console.warn` (`[Input] rendered without a label…`), so fix the component,
never the test. Give every `Input` a `label`, `aria-label` or
`aria-labelledby` (a `<label htmlFor>` beside it is not enough for the
library) and write each `DialogTitle` in its `DialogContent`'s own JSX;
`tests_unit/input-accessible-name.test.ts` and `dialog-title.test.ts` hold both
statically. The only tolerated warnings are the library's own defects, listed
in `LIBRARY_DEFECTS` with the `LIBRARY-FEEDBACK.md` finding that removes them.
A component test that needs the token values (a palette naming
`var(--color-…)`) loads `designTokenStylesheet()` from
`src/testing/design-tokens.ts`: happy-dom does not read Tailwind's `@theme`.

A `<details>` disclosure line is always `DisclosureSummary`
(`components/ui/disclosure.tsx`): the shared `focus-visible:ring-2
ring-focus` ring instead of the browser's 1 px outline across the card, and the
`content-compact` text as wide as its words for the inline "Details" link;
`size="block"` for a full-width header row inside a bordered card.
`components/ui/disclosure.test.tsx` fails on a raw `<summary>` anywhere in
`src/` except the report blocks, which their own stylesheet draws.

No catalog key or value carries an em dash, in en, fr, es or ja: write a
colon, a comma or a full stop (fr ` :`, ja `：` or `、`), never an en dash.
`tests_unit/catalog-no-em-dash.test.ts` fails on a new one with its locale and
key; a string that must keep one goes in its `ALLOWED` list with the reason.

## Forms

Every form that creates or edits an object has FIELDS like the create and
edit forms of OpenCTI and OpenAEV, the two products this one sits beside:
full-size inputs, textareas and selects, a label above, a help line, the
actions at the right end. OpenCTI and OpenAEV are the reference for the
fields only, never for their drawers (Sam, 7 Nov 2026: no drawer in the app).
The reference implementations, read when in doubt:

- OpenCTI `src/components/common/form/FormButtonContainer.tsx` (the actions in
  one row at the right end, 8 px apart, 32 px under the last field) and the
  fields of `entities/organizations/OrganizationCreation.tsx`.
- In this product, the agent page's Config tab and the settings form kit
  (`components/ui/settings-form.tsx`, added by #9374, #9173 S1c).

### Where a form opens

- **Create or edit an object of a list, short form** (an organization, a
  department, a channel, a key): the centred dialog (`components/ui/dialog.tsx`,
  `DialogContent size="md"`, 640 px), never an inline row of fields inside the
  list. The fields go in `DialogBody` inside a `<form>` built from the settings
  form kit, so Enter submits it and the fields scroll while the header and the
  actions stay in view; the actions go in `DialogFooter`. Example: Settings >
  Organization > Tracked organizations (`TrackedOrganizations.tsx`, #9375
  slice 3), with the OpenCTI Organization picked by search (`SearchableSelect`
  with `onSearchChange`, fed by `GET /organizations/opencti-identities`), never
  typed as a raw id.
- **A long create or edit form**: a page of its own or a settings section.
- **No drawer or slide-over for a form, anywhere** (Sam, 7 Nov 2026).
- **Edit the settings of one object** (a settings section, an agent's
  configuration): the fields in place, in cards, read-only until Edit, the
  actions in a sticky bar while editing.

### Anatomy

| Part | Rule | Component |
| --- | --- | --- |
| Dialog | centred, title ("Add an organization", "Edit Acme"), one line of description, the body scrolls, the footer stays in view | `Dialog`, `DialogContent size="md"`, `DialogBody`, `DialogFooter` |
| Group | a card with a title and one line of description; two columns at most, one when narrow | `SettingsCard`, `SettingsFieldGrid` |
| Field | label above at 14 px (`content-base`, the `Label` default), never a size class on it; required fields marked `*`; the full-size design-system control (see "Measured floor"); one help line always visible under it at 12 px (`content-compact`) at least, never `content-caption`; the error in place of the help, at the same size | `SettingsField` |
| Explanation | an info bubble beside the label: what it is, its unit, a real example, which agents or features read it, what happens when it is empty | `InfoTip` |
| Actions | Cancel (secondary or ghost) then the one filled primary at the right end; they never wrap at 1024 px; "Unsaved changes" shown while the form is dirty | `SettingsSaveBar`, `DialogFooter` |
| Error | `aria-invalid` on the field, the message under it ("Form fields: error state") | the `Input` adapter, `INVALID_FIELD_CLASS` |

### The right control per type

- **Measured floor, every control of a form** (`Input`, `Textarea`,
  `SelectTrigger`, a picker, a combobox, `TagInput`): 36 px high or more, the
  control text at body size (14 px, `content-base`). No size class on a form
  control: no `h-6`, `h-7`, `h-8`, `text-xs`, `content-compact`,
  `content-caption`, no `compact` Input, no `size="sm"` on a trigger. A
  picker's inner input follows the same floor. Table cells and list rows keep
  the compact sizes.
- **Text and numbers**: the design-system `Input` at its full size. The
  `compact` Input (24 px, centred caption text) and an `h-6` `SelectTrigger`
  belong to a table cell or a list row, never to a form field.
- **Enums**: `Select` with a one-line meaning per option
  ("Provider: supplies a service or product you depend on").
- **Lists**: `TagInput`, never comma-separated text.
- **Countries, currencies, integrations, projects**: a picker of the known
  values, never a free ISO code.
- **An entity of another platform** (an OpenCTI organization, an OpenAEV
  asset): a picker that searches as you type and stores the id, never a raw id
  field.
- **Amounts**: shown with their unit and currency; a value whose effect can be
  previewed shows it ("1 hour saved = 85 EUR").
- A field nothing reads is wired to a real behaviour or removed.

### Checks

The settings form kit's `pages/settings/settings-form-kit.test.ts` (#9374)
fails on a `compact` Input in a settings form outside its pending list. #9375
widens the gates to every page: a size class on a form control (any class of
the "Measured floor" list: `compact`, `h-6`, `h-7`, `h-8`, `text-xs`,
`content-compact`, `content-caption`, `size="sm"` on a trigger) outside a
table cell (an allow-list of genuine cell editors, each with its reason); a
`Label` or a help line in a form with a type class below its size (a label
under 14 px, a help line under 12 px); a raw `<select>` or `<input>` in a
form (the `INVALID_FIELD_CLASS` exceptions listed); a form field without help
text; a comma-separated list input. Each gate keeps a pending list that only
shrinks as the areas are converted.

`tests_unit/form-controls-gate.test.ts` (#9375 slice 2) holds four of these
gates over every `.tsx` of `src/`, statically:

- `compact`: an `<Input compact>`;
- `trigger`: a `<SelectTrigger>` with `size="sm"` or an `h-6` / `h-7` / `h-8`
  class;
- `raw`: a raw `<select>` or `<input>` (a `type="file"` or `type="hidden"`
  input is not a field and is not counted);
- `comma`: a "comma-separated" list typed as text.

Each gate has an `ALLOWED` map (a genuine table-cell or list-row editor, or
the `Input` adapter itself, each with its reason) and a `PENDING` ratchet of
the files not converted yet with their count. A new file or a count that grows
fails; a file converted below its count fails until its entry is lowered or
removed in the same pull request. The help-text and label-size gates land with
the conversion slices, once every form field is a `SettingsField`.

The floor is the library's own default: in `@filigran/design-system` 1.2.0 the
`Input` is `h-9`, the `SelectTrigger` `h-9` and the `Combobox` `min-h-9`, all
36 px, so a form control at its default size passes and no local size class is
needed. A library default under 36 px goes to
`fds-migration/LIBRARY-FEEDBACK.md`, never to a local class.

## Form fields: error state

The design system's `Input` and `Textarea` put `className` on their wrapper
`div`, not on the field: a feedback tone written there
(`border-feedback-error-primary`, `focus-visible:ring-feedback-…`) paints a
0 px border on a wrapper that never takes focus, and the field looks valid.
An invalid field carries `aria-invalid`, and the adapter draws it: the
`Input` adapter in `components/ui/input.tsx` keeps `aria-invalid` on the
element and paints the invalid border from it
(`[&_input[aria-invalid=true]]:border-feedback-error-primary`), in every mode,
at full tone, the one strength of the error token that reaches 3:1 on the card
in both themes (a `/60` border measured 2.57:1 on dark). A value that
is accepted but deserves a second look (a reserved tag key, an unknown icon
name) passes `invalidTone="alert"` for the amber border, drawn with
`feedback-warning-primary` (4.84:1 on the light card, 5.70:1 on dark), never
`feedback-alert-primary`, which is 1.72:1 on the light card.
The focus ring stays the shared `ring-focus`: the border alone carries the
error. A call site sets `aria-invalid` and never a tone of its own; a raw
`<input>`, `<textarea>` or `<select>` takes `INVALID_FIELD_CLASS.error` or
`.alert` from `components/ui/invalid-field.ts`, the same two tones drawn from
its own `aria-invalid`. Use the library's `error` prop instead when its icon
and message under the field are wanted. `src/input-feedback-border.test.ts`
fails on any `border-`, `ring-` or `outline-feedback-*` token in the
`className` of an `Input` or `Textarea`, reached through `[&_input]:` or not,
or of a raw field that sets `aria-invalid`, and pins the two tones of the
adapter and of `INVALID_FIELD_CLASS`.

## Theming (light + dark) — read before styling surfaces

Light mode is applied by setting `data-mode="light"` on `<html>` (see
`useThemeEffect`), which stays this app's source of truth: the theme preference,
the branding form, `useColorMode()` and the `light:` / `dark:` custom Tailwind
variants of `index.css` (keyed on `data-mode`, replacing Tailwind's built-in
`dark:`) all read the attribute.

The same element also carries a `light` / `dark` **class**, mirrored from the
attribute by `applyTheme`. That class is the only theme switch the design system
reacts to — its tokens live under `.light {}` and `.dark {}` selectors, with no
attribute variant and no `prefers-color-scheme`. Break the mirror and the app
turns light while every design-system token stays dark, with no error anywhere.
`<html>` also carries `layer-0`, the elevation context every `bg-elevation-*`
utility resolves against.

Every surface, hover and border must flip on its own.

The one exception to "the mode lives on `<html>`" is a **forced-dark
subtree**: an element carrying `data-mode="dark"` together with the library's
`dark` class reads dark, with everything under it, whatever the app mode (the
presenting deck stage and the menus portaled into it - a black screen is
dark whatever the app is). The class gives it the design system's dark tokens
(`index.css` declares no token set of its own for the subtree); the attribute
makes the custom variants and the accent follow. The product accent follows it
(`[data-theme="xtm-one"] [data-mode="dark"]`), so does the administrator's
(`ACCENT_SELECTOR` in `lib/color-utils.ts` - the branding block declares the
accent on the subtree and keeps the background palette on `:root`), and the
`light:` / `dark:` custom variants honour it, so a component written with
tokens needs nothing to read right inside one. Reserve it for a surface that is genuinely dark by
nature; a subtree that should simply follow the app's mode carries nothing.

| Need | Use | NOT |
| ---- | --- | --- |
| Hover / pressed / subtle surface overlay | `hover:bg-elevation-hover` | `hover:bg-foreground/5`, `hover:bg-white/5` (neither token exists any more) |
| Subtle / hairline border | `border-elevation-subtle` or `border-elevation-subtle-soft` | `border-foreground/10`, `border-white/10` |
| Popovers, dropdowns, tooltips, menus | `layer-2 bg-elevation-default text-default-primary border-elevation-subtle` | `bg-popover`, `bg-[hsl(220,56%,10%)]` |
| Selected / focused list item | `bg-elevation-highlight` / `data-[highlighted]:bg-elevation-hover` | `focus:bg-white/10` |
| Card / panel background | `Card` from `components/ui/card.tsx` (the design system's `Paper`), or `layer-1 bg-elevation-default` | `bg-card`, `bg-white` |
| Raised menu / popover shadow | `shadow-overlay` | `shadow-lg` (FDS redefines the Tailwind shadow scale as depth radii) |
| SVG `fill`/`stroke` (graph, canvas) | read `useColorMode()` and pick light/dark values, or `var(--text-default-secondary)` in an inline style | hardcoded `fill="white"` / dark `hsl()` |

An opacity modifier works on `--color-*` tokens (`bg-accent/10`,
`text-feedback-error-primary/50`) but NOT on the elevation and text utilities
(`bg-elevation-hover`, `text-default-secondary`): those are hand-written CSS
blocks, not colour tokens, so `bg-elevation-hover/50` silently produces nothing.
That is what the elevation ladder and the three text levels are for — reach for
the next step rather than an opacity.

Library fields paint `--bg-input-*`, which `index.css` re-declares on every
`.layer-N` so a field in a dialog or a `layer-2` popover takes that layer's field
colour (`LIBRARY-FEEDBACK.md` F31). A field that looks invisible on a
surface means that surface is missing its `layer-N` class; never repaint the field.

For the rare case that genuinely needs a per-mode value, use the `light:` (or
`dark:`) custom variant declared in `index.css`. Prefer tokens over variants.

**Provider logos**. The files under `public/logos/` are drawn for the
dark UI. The ones drawn white or pale on a transparent background are darkened
in light mode by ONE rule at the end of `index.css`, keyed on the file
(`img:is([src$="/logos/<file>"], ...)` with `light:invert light:hue-rotate-180`),
so they read whatever call site renders them. A new logo like that adds its
file to that sorted list. Never add a per-call-site `light:invert` class or a
second list. `src/lib/light-theme-logos.test.ts` fails on an entry naming no
file; `tests_e2e/provider-logo-themes.spec.ts` fails on an unlisted white mark
or a listed dark one.

## Overlays, scrims and veils

A layer that covers content belongs to exactly one of three classes.

1. **Modal scrim.** A layer that blocks the content until it is dismissed uses
   `bg-scrim` (`bg-scrim-strong` for a darker cover), as
   `components/ui/dialog.tsx` and `components/ui/slide-over.tsx` do: a dialog,
   a slide-over, the mobile sidebar, a lightbox, and the arcade pause and
   game-over screens. Text on it is `text-on-scrim`.
2. **Translucent page-surface veil.** A non-modal layer that tints content which
   must stay visible underneath uses the page surface with an alpha:
   `bg-[color-mix(in_srgb,var(--bg-elevation-default)_N%,transparent)]`, the
   class `scripts/fds-migration/token-map.mjs` maps `bg-background/N` to. It
   covers a drop target, a disabled card, a hover wash, an intro screen and a
   full-screen voice view. Never `bg-elevation-default` at full opacity (an
   `@utility` with no opacity modifier: it blanks what it covers), a
   literal `rgba()` or an inline style. Values in use, to reuse rather than
   invent: 30 for a disabled card (`integration-catalog-browser.tsx`), 40 for
   an intro (`ArcadeOverlay.tsx`), 50 for a hover wash (`VideoStudio.tsx`), 80
   for a drop target (`ChatDropOverlay.tsx`), 95 with `backdrop-blur-xl` for a
   full-screen takeover (`VoiceConversation.tsx`).
3. **Opaque cover.** A full-opacity surface only when the content beneath must
   be hidden, such as the loading cover over the unpainted iframe in
   `components/video-studio/editor/PreviewStage.tsx`, with a comment next to
   the class saying why.

One modal sits outside the three classes on purpose: the **clear-backdrop
drawer**. `components/ui/agent-flow-drawer.tsx` is `role="dialog"`
`aria-modal="true"` over a `bg-transparent` backdrop, so the agent map stays in
view beside the drawer with the selected node ringed; a click on the backdrop
or Escape closes it. It is allowed only for a side drawer whose
content refers to what stays visible beside it. Any other modal uses the scrim.

`tests_unit/translucent-veil-overlays.test.ts` fails on an `absolute inset-0` or
`fixed inset-0` layer painting the bare page surface; its `allowed` set holds
the justified opaque covers. Every new full-area overlay is checked against it
in the same PR, and a new opaque cover is added to `allowed` with its reason.

## Shadows

`shadow-overlay` (the `--shadow-overlay` token of `index.css`, a real
`0 8px 32px` shadow) goes ONLY on floating surfaces: menus,
popovers, tooltips, dialogs, drawers and slide-overs, the command palette,
sticky floating bars, toolbars and pills drawn over content. Never on a card,
a section, a node, a swatch or a thumb in the page flow: a surface in the page
flow separates itself with its `layer-N bg-elevation-*` surface and a
`border-elevation-subtle` line, not a shadow. No hand-written shadow
(`shadow-[0_0_40px_hsl(...)]`) on a floating surface either: it is the token,
or a named token when a glow is intended. The named glows are
`shadow-glow-accent` (40px, accent at 15%: the arcade pause and game-over
panels) and `shadow-glow-accent-strong` (30px, accent at 25%: the arcade
"answer ready" toast), both in `index.css`. A small glow on a status dot, a
resize handle or a hover state is not a panel and stays as it is.

`src/shadow-overlay-floating.test.ts` enforces it: its `FLOATING` allowlist
names every file that may use the class and how many times, and a second test
pins the in-page cards at zero. A new floating surface is added
to the allowlist in the same PR. A third test fails on a `bg-elevation-default`
panel carrying a plain `shadow-[0_0_<N>px ...]` literal.

## Editions (XTM One only)

Two editions, resolved server-side into `edition` (`EDITION_*` in
`backend/app/api/v1/platform.py`) and read through `useEdition()`:

| `edition`    | Label              | What it means                                                        |
| ------------ | ------------------ | -------------------------------------------------------------------- |
| `community`  | Community Edition  | No Filigran license, no Enterprise Edition platform connected. AI is off. |
| `enterprise` | Enterprise Edition | A valid Filigran license OR a connected EE platform. Everything is on.    |

There is nothing in between. A license carries a quota, not a feature
switch: `agentic_source`
(`xtm_license` / `ee_platform` / `none`) says where the ceilings come from,
and that is the only thing that differs between the two Enterprise grants.

- **One licence question.** `useAgenticAIEnabled()` (reads `agentic_enabled`)
  is THE answer every AI affordance gates on — creation buttons, the
  composer (through `useChatSendEnabled()`), the nav locks, the catalog. Do
  not derive it from quotas (`0/0/0` is both "uncapped" and "off") and do not
  split it per feature: chat, A2A, KB sources and creation flows are one
  gate, and a surface that is "on" while another is gated is a bug.
- **Say it once.** Everything the UI says about Community Edition lives in
  `components/ui/enterprise-upsell.tsx` — `EnterpriseUpsellDialog` /
  `EnterpriseUpsellBody` (what Enterprise unlocks, the two ways to activate,
  trial + sales CTAs, the admin activation form), `EnterpriseUpsellNotice`
  (the dashboard / settings banner), `EnterpriseLockedTip` (the lock
  tooltip) and `EditionBadge` — worded once in
  `hooks/useEnterpriseUpsell.ts` (`useEnterpriseLockedReason()`,
  `useEnterpriseLocked()`, `useOnIncludedAllowance()`). `AgenticGateDialog` /
  `ChatGateDialog` / `AgenticGatePage` in `license-gate-dialog.tsx` open the
  same body. The outbound paths are `XTM_ONE_TRIAL_URL` /
  `FILIGRAN_CONTACT_URL` in `lib/constants.ts` — never inline the URLs.
- **Never render the badge by hand.** `<EditionBadge />` owns the label
  (`EDITION_BADGES` in `hooks/usePlatformConfig.tsx`), the per-edition
  colors and the click-through to the upsell dialog on Community Edition.
  Gate it on `useIsXtmOne()` — Copilot mode has no edition and keeps the
  Licensed / Community badges.
- **Never guide a Community Edition user through a useless setup.** The
  model / provider warnings (`ModelConfigWarnings`, the reasoning-provider
  banner in `KBHealthWarnings`, the AI Models "unavailable until a provider
  is configured" copy) stay silent while AI is not licensed, and the
  `EnterpriseUpsellNotice` at the top of `DashboardNotices` is what is said
  instead. A provider CAN be prepared ahead of activation; the copy says it
  is used as soon as Enterprise Edition is active, not that it unblocks
  anything now.
- **AI off wins over the licence.** The administrator's switch (`useAiEnabled`) is a separate axis; where both apply, `AiDisabledTip` /
  `AiActionGate` speak first (`lockReason` in `nav-model.ts`, the
  `aiDisabled` branches in the gate dialogs, and `useEnterpriseLockedReason()`
  itself answers `null` while AI is off, so the `EnterpriseUpsellNotice` and
  the Community Edition CTAs stay silent) — an upsell would send an
  administrator to buy what they already have.
