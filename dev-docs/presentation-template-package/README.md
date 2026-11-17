# Synthetic presentation template package (dev fixture)

A ready-made template + deck fixture for exercising the `generate_presentation`
built-in tool and its renderer locally. Everything here is **synthetic**: made-up
company, made-up numbers, hand-drawn SVGs. Nothing is brand material and nothing
should ship to a customer environment.

## What's in it

| File | What it is |
| ---- | ---------- |
| `template-container.json` | A `TemplateContainer` payload with `kind: "presentation"`: searchable metadata (name, tags, aspect ratio) plus a `theme_config` brand palette (dark background, so the template supports the `dark` theme only — see `supported_schemes`) and a descriptive `layouts` catalog. |
| `assets/synthetic-logo.svg` | Asset with `asset_role: "logo"`. |
| `assets/synthetic-background.svg` | Asset with `asset_role: "background"`, sized 1280×720 for a 16:9 slide. |
| `deck-valid.json` | A five-slide `PresentationSpec` exercising **every** layout in the catalog, plus `rich_text`, speaker `notes` and all three metric trends. |
| `deck-edited.json` | The same deck reordered, with one slide inserted and one dropped — feed it back with the same `presentation_id` to test in-place editing. |
| `deck-invalid.json` | Four different validation failures in one file (invented field, unknown layout, script-capable `rich_text`, empty `metrics`). Use it to see the error report an agent receives. |
| `deck-sales-collateral.json` | The reference reproduction of a **sales deck**: six 16:9 slides — title, personas, feature matrix (toned "us" column, check / cross cells, footnote), editions as tier cards, fact sheet (badges + key / value), closing banner. Ships with `theme: "dark"` (a theme is a colour scheme, `dark` or `light`; the v1 `filigran` value still loads as `dark`); the collateral review also ran it with `theme: "light"` against the Filigran Brand Deck's light palette (set the field and the template's id, which is per-environment). |
| `deck-product-brief.json` | The reference reproduction of a **product brief**: two A4 portrait pages (`theme: "light"`, as printed collateral usually is) — hero banner, fact sheet and feature list, then personas, feature matrix, editions and a soft call to action. Exercises the portrait type scale and exports on real A4 (PDF) and as a portrait PowerPoint. |

Loader: [`../scripts/seed-presentation-fixture.py`](../scripts/seed-presentation-fixture.py).
`test_every_reference_deck_fixture_still_validates` (in
`backend/tests/test_presentation_spec_v2.py`) fails the build if a spec change
invalidates any `deck-*.json` here, and
`test_collateral_fixtures_cover_every_collateral_block_on_both_formats` keeps
the two collateral fixtures exercising every collateral block.

> The `layouts` block in `template-container.json` is **descriptive metadata**
> that an agent reads through `search_templates` / `get_template`. The authority
> on which fields a slide may carry is
> `backend/app/services/presentation_spec.py` — the container can drift, the
> validator cannot.

The assets are SVG so they stay reviewable in a diff rather than landing as
binary blobs. A slide renders them through `<img src>`, where SVG cannot execute
script — which is also why an image only ever enters a deck as a platform
`image_file_id`, never as a URL the agent chose.

### Putting an asset on a slide

`template` mode prints each uploaded asset's id. Drop one into a slide to use it:

```json
{ "layout": "title", "title": "…", "background_image_file_id": "<asset id>" }
```

Asset ids are per-environment, so the deck fixtures deliberately ship without
them — add one by hand after loading the template. Assets of a **shared or
company-managed** template resolve for every user the container is visible to
(`_authorize_file_access` grants a template asset exactly its container's
visibility), so a branded deck does not lose its images when someone else opens
it.

### Covers: per-slide themes and hero backdrops

A slide may carry a theme of its own and a title / closing slide a `hero`
backdrop, both drawn in the slide's resolved scheme:

```json
{ "layout": "title", "title": "Uncover Threats.\nTake Action.", "theme": "dark", "hero": "gradient", "hero_accent_word": true }
```

`theme` (`dark` / `light`, absent = inherit the deck theme) always wins over
the deck theme and a deck theme change leaves it alone; `null` removes it. A
slide theme is held to the template like the deck theme: a scheme the
template's `supported_schemes` does not list is refused (or snapped on a
template change). `hero` is `plain` (default), `gradient`, `split` or
`spotlight`; `hero_accent_word` draws the title's last line in the accent tone;
`overlay` (`none` / `soft` / `strong`, any layout) is the scrim over
`background_image_file_id`.

Two things a template package can carry for them:

- **A brand mark.** The `gradient` hero draws the template's mark - the
  symbol without the wordmark - as a faint watermark bottom right, the way the
  Filigran PowerPoint cover does. Attach it with `asset_role: "logo_mark"`
  (the third file-id theme key, `mark_file_id`, resolves from that role when
  the `theme_config` names none, exactly like `logo_file_id` / `logo` and
  `logo_light_file_id` / `logo_light`). Without a mark the wash draws alone.
- **Cover defaults.** `extra_metadata.deck_defaults` accepts, next to the
  string keys `theme` / `transition` / `aspect_ratio`, three OBJECT keys keyed
  by the `title` / `closing` layout:

  ```json
  "deck_defaults": {
    "theme": "light",
    "hero_defaults": { "title": "gradient", "closing": "gradient" },
    "hero_tones": { "title": "accent" },
    "slide_themes": { "title": "dark", "closing": "dark" }
  }
  ```

  They fill the `hero` / `hero_tone` / `theme` of the title and closing slides
  the agent left unset - so a brand opens and closes dark on a light deck by
  default. `hero_tones` names the palette colour the backdrop is drawn in:
  `brand` (the template's `gradient_from` -> `gradient_to`, the default when
  nothing is declared), or one colour alone - `accent`, `gradient_from`,
  `gradient_to`, `info` / `success` / `warning` / `error`, `muted` - the way a
  brand whose gradient ends in a secondary colour still opens on its primary.
  Filled on absent keys only (an explicit `hero: "plain"` or a stored slide's
  own value wins), never on a body slide; an unknown layout or value is
  dropped; a `slide_themes` scheme the template does not support is snapped on
  the write like any slide theme. The seeded Filigran Brand Deck declares the
  hero and theme defaults and leaves the colour on the brand gradient; its
  deck default `theme` stays `dark`, so a new deck from it is dark throughout
  and the covers' own `dark` shows its point on the light decks made from it.

## Fastest path: see the validator, no infrastructure

No database, no server, no LLM — just the backend importable:

```bash
PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py validate --deck deck-invalid.json
```

That prints, verbatim, the tool error an agent would get back:

```
REJECTED — deck-invalid.json

The presentation spec is invalid and was NOT saved. Fix these field(s) and call the tool again:
  - slides.0.body (layout=title): Extra inputs are not permitted
  - slides.1: Input tag 'carousel' found using 'layout' does not match any of the expected tags: …
  - slides.2.rich_text (layout=generic): Value error, rich_text must not contain any of these tags: script, …
  - slides.3.metrics (layout=dashboard): List should have at least 1 item after validation, not 0
```

## Already there without loading anything: the brand deck

In XTM One mode the platform seeds one built-in container on every startup — the
**XTM One Brand Deck** (`backend/app/services/template_container_seed.py`):
product palette, product typefaces, 16:9, and a layout catalog derived from the
validator. It carries no assets, so it is the right template to test *theming*
against, and this fixture is the right one to test asset handling and the
metadata search filters (`kind` / `tags` / `theme` / `aspect_ratio`) with a
second, differently-tagged container in the list.

## Load the template container (REST)

Needs a running platform and the JWT of the user who should own the container:

```bash
python3 dev-docs/scripts/seed-presentation-fixture.py template --base-url http://localhost:4000 --token "$JWT"
```

This creates the container and uploads both assets with their `asset_role`. The
script prints the `template_id` — pass it to `generate_presentation` (or let the
agent find it with `search_templates`) so the deck picks up the palette.

## Render a deck without spending an LLM call

`deck` mode inserts a row through the same `validate_spec` + `save_presentation`
path the tool uses, so an invalid spec is still refused:

```bash
PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py deck --user-email you@example.com
```

It prints the deck id, the viewer URL (`/presentations/<id>`) and the
`[[PRESENTATION:<id>]]` marker. Re-run with `--deck deck-edited.json
--presentation-id <id>` to rewrite the same row and watch `revision` increment.

**This bypasses the agent on purpose**, to iterate on the renderer. The only
supported way decks are created in production is the tool — so test that path
too, per below, before calling a change good.

## Testing the tool end to end

1. Enable **Presentation Generation** (and optionally **Template Search**) on an
   agent: Agent > Config > Tooling, or `builtin_tool_ids: ["generate_presentation",
   "template_search"]`.
2. In chat: *"Build me a deck from the Synthetic Deck template summarizing this
   quarter — a title slide, a KPI dashboard, a two-column what-changed slide, a
   build-vs-buy comparison and next steps."*
3. Then, in the same conversation: *"Move the executive summary to slide 2 and
   drop the comparison."* The card in the chat must update **in place** — same
   deck, new slides, no second card, and the viewer must not jump back to slide 1.

## Acceptance criteria, and what covers each

| Criterion | How to check it with this package |
| --- | --- |
| A valid spec renders as an interactive deck matching slides / layouts / theme | `deck` mode with `deck-valid.json`, then open the chat card or `/presentations/<id>`. All five layouts appear. |
| `rich_text` is sanitized before rendering | `validate --deck deck-invalid.json` shows generation-time rejection; DOMPurify is the render-time layer (`frontend/src/components/presentation/sanitize.ts`). |
| Add / remove / reorder updates the deck without a full re-render | `deck --deck deck-edited.json --presentation-id <id>`, or the two-step chat conversation above. |
| An invalid spec fails clearly instead of rendering something broken | `validate --deck deck-invalid.json` — four addressable field errors, nothing written. |
| The free-form HTML iframe mode is not implemented | No field in the spec can express it; `test_rich_text_is_the_only_html_field_in_the_spec` in `backend/tests/test_presentations.py` is the tripwire. |

Worth exercising by hand as well, since none of the above touches them: PDF
export (the download icon on the card, or `?print-pdf` on the viewer), fullscreen
presentation, and speaker notes (press `S` in the standalone viewer — every slide
in `deck-valid.json` carries `notes`).
