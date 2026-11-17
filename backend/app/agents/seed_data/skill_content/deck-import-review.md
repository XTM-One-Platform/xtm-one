# Deck Import Review

How to check that a template you built from someone's deck actually looks like their deck — and how to say what still differs.

A template you have not looked at is a guess. A template you looked at once and pronounced "very close" is a guess with a compliment attached. This skill is the loop that makes the claim real, and the two instruments that make it honest: your eyes for composition, a number for colour.

## The loop

At most **three rounds**, then stop and hand over. Each round:

**1. Capture both sides.**

- Your render: `capture_presentation_slides` on the deck you generated.
- The source: `render_source_pages` on the PDF (the pages you already rendered are still in this conversation — do not re-render them).

Pick the **same three** slides on both sides: the cover, the densest content slide, the closing one. A cover tells you about typography and the accent; a dense slide tells you whether the design survives real content; a closing slide tells you about the chrome.

**2. Judge on named points, not on a feeling.** One line each, saying which side wins:

- **Background and value** — is your deck as dark, or as light, as theirs?
- **Title treatment** — typeface, weight, size relative to the slide, position.
- **Accent** — same hue, and used on the same THINGS. A brand that accents numbers and a template that accents headings are different brands at the same hex value.
- **Density** — does a content slide hold a comparable amount of text, or did yours turn six bullets into two?
- **Chrome** — logo present, on the same side; footer repeating the same line.
- **First impression** — the one thing a reader would notice first if both decks were on a wall.

**3. Measure the colours.** `compare_palette` with the template id and the colours you are matching (what `inspect_pptx_identity` read, or what the user gave you).

**ΔE above 5 on `background`, `foreground` or `accent` is a finding, whatever the images look like.** Two greys that look identical side by side are routinely ΔE 8 apart, and your eye is worse at this than the number is. A role the template stores as `hsl()` or as a font stack comes back unmeasurable — compare those by eye and say you did.

**4. Fix the largest gap, not every gap.** One `update_template` call, then regenerate the SAME deck (`presentation_id`) and capture again. Changing five things at once means you learn nothing about which one mattered.

## When to stop

Stop when every measured colour is under ΔE 5 and no point of the six is a clear loss — or after the third round, whichever comes first.

Then say plainly what still differs and why. Useful:

> "The source uses a condensed display face I have no file for, so the template falls back to Inter — headings run about 15% wider. Everything else is within ΔE 2."

Not useful:

> "The template closely matches the original design."

A named difference lets the user decide whether they care. A compliment does not.

## Things that are not differences

Do not chase these; they are the renderer doing its job, and "fixing" them makes the deck worse:

- **Slide geometry.** The platform lays a slide out; you do not reproduce coordinates. A block that sits 40px lower than the source is not a finding.
- **Font rendering.** Different rasterizer, different hinting. Judge the typeface and its weight, not the antialiasing.
- **Image compression.** A logo that looks slightly softer in a capture is the capture.
- **Slide count.** A source slide that became two, or two that became one, is a composition decision — say you made it, do not undo it to match a count.

## When you cannot look

If `capture_presentation_slides` fails, say the render could not be verified and why. Do not describe a deck you have not seen, and do not ask the user to check it for you — they came here so they would not have to.

If the source is a `.pptx` or an HTML page, you have no rendered source to compare against — only a PDF is rasterized. `inspect_pptx_identity` and `inspect_html_identity` give you declared values instead, so the palette half of this review is stronger than usual and the composition half rests on the outline plus anything the user attached as an image. Say which half you verified.

For an HTML source there is one extra step before the loop: you chose the mapping (which declared colour is the background, which is the accent, which stack is the display face), and the page never said so itself. State that mapping in your first message and let the user correct it — a wrong mapping produces a template that measures perfect against the colours you picked and looks nothing like the page.
