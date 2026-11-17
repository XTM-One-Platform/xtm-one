/**
 * The rule table for the design-system token migration.
 *
 * Every rule is ordered: more specific patterns come first, because
 * `text-muted-foreground/60` must not be consumed by the rule for
 * `text-muted-foreground`. `migrate.mjs` applies them top to bottom and reports
 * how many times each one fired, so a rule that never matches is visible rather
 * than silently dead.
 *
 * Three kinds of target, and the difference matters:
 *
 *   1. `--color-*` tokens (`bg-accent`, `text-feedback-error-primary`). Tailwind
 *      generates real colour utilities from these, so `/50` opacity modifiers
 *      work and are preserved by the rules below.
 *   2. `@utility` classes (`bg-elevation-default`, `text-default-secondary`,
 *      `border-elevation-subtle`). These are hand-written CSS blocks in the
 *      library's theme, NOT colour tokens, so `bg-elevation-default/50` does
 *      not exist. Any opacity ladder mapping onto them has to collapse onto a
 *      discrete step, which is why the ladders below land on
 *      secondary/disabled rather than carrying their percentage across.
 *   3. Elevation context. `--bg-elevation-default` is only defined inside a
 *      `.layer-0…3` context, so a raised surface needs BOTH the layer class and
 *      the paint utility: `bg-card` becomes `layer-1 bg-elevation-default`.
 *      `<html>` carries `layer-0`, so an unqualified `bg-elevation-default`
 *      paints the page surface.
 *
 * The opacity ladders are the one genuinely lossy part of this migration. XTM
 * One expressed secondary and tertiary text as `text-muted-foreground` at /70,
 * /60, /50, /40, /30 and /20 — six steps — because it had no token for "less
 * important text". The design system has three (primary, secondary, disabled),
 * and that is a deliberate design decision rather than a gap, so the ladder
 * collapses onto it. Same story for the `bg-foreground/5` hover surfaces, which
 * become `bg-elevation-hover`: the opacity hack existed precisely because there
 * was no elevation system.
 */

/** @typedef {{ from: RegExp, to: string, why?: string }} Rule */

/** Variant prefixes (`hover:`, `dark:`, `group-hover:`, `lg:`, …) are never
 * matched by these patterns — every rule anchors on the utility name itself,
 * so a prefix is carried through untouched. */

/** @type {Rule[]} */
/**
 * The amber fill as a SOLID surface - no alpha, or one of /70 and up - as
 * opposed to a tint of it. Text on the solid fill takes `text-on-feedback-fill`
 * (the fill keeps one colour in both themes); on a tint, the page shows through
 * and the theme's own text applies. One-digit alphas (`/5`) are tints too.
 * Shared with `tests_unit/fixed-fill-text-contrast.test.ts`, so the codemod and
 * the guard can never disagree on which class lists carry the fixed fill.
 */
export const SOLID_ALERT_FILL = /\bbg-feedback-alert-primary(?!\/[1-6]?\d\b)(?![\w-])/;

export const rules = [
  // ───────────────────────────────────────────────────────────── surfaces ────
  {
    from: /\bbg-card\/(?:\d+|\[[^\]]+\])/g,
    to: "layer-1 bg-elevation-default",
    why: "translucent card surfaces: the elevation token replaces the fade",
  },
  { from: /\bbg-card\b(?!-)/g, to: "layer-1 bg-elevation-default" },
  { from: /\bbg-popover\b(?!-)/g, to: "layer-2 bg-elevation-default" },
  {
    // A translucent page surface is a VEIL (a backdrop, a drop zone, a hover
    // wash): it keeps its alpha. `bg-elevation-default` is an `@utility` with no
    // opacity modifier, so mapping onto it painted every veil as the opaque page
    // and blanked what it should only dim.
    from: /\bbg-background\/(\d+)\b/g,
    to: "bg-[color-mix(in_srgb,var(--bg-elevation-default)_$1%,transparent)]",
    why: "a translucent page surface keeps its alpha: a veil, never the opaque page",
  },
  {
    from: /\bbg-background\/\[[^\]]+\]/g,
    to: "bg-elevation-default",
    why: "an arbitrary alpha has no fixed percentage to carry: review the call site",
  },
  { from: /\bbg-background\b(?!-)/g, to: "bg-elevation-default" },
  {
    from: /\bbg-muted-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g,
    to: "bg-[var(--text-default-secondary)]",
    why: "declared before the `bg-muted` rule, which would otherwise bite off its prefix and leave `bg-elevation-highlight-foreground`",
  },
  { from: /\bbg-muted\/(?:\d+|\[[^\]]+\])/g, to: "bg-elevation-highlight" },
  { from: /\bbg-muted\b(?!-)/g, to: "bg-elevation-highlight" },
  {
    from: /\bbg-primary\b(?!-)/g,
    to: "bg-elevation-highlight",
    why: "XTM One's `--primary` was a navy SURFACE, not a brand colour: the brand accent lived in `--cyan`",
  },
  { from: /\bbg-secondary\b(?!-)/g, to: "bg-elevation-hover" },
  {
    from: /\bbg-foreground\/(?:\d+|\[[^\]]+\])/g,
    to: "bg-elevation-hover",
    why: "the `bg-foreground/5` idiom was a hover surface without an elevation token to name it",
  },
  { from: /\bbg-input\b(?!-)/g, to: "bg-input-default" },
  // Prefixes with no `@utility` counterpart in the library: gradient stops,
  // `divide-*` and `ring-*`. FDS's alias variables (`--bg-elevation-default`,
  // `--border-elevation-subtle`) are declared outside Tailwind's `--color-*`
  // namespace, so they generate no utilities of their own and have to be
  // referenced as arbitrary values. Still tokens, just spelled the long way —
  // and contextual, so a gradient stop inside a `.layer-1` card follows the
  // card's elevation rather than freezing one layer's value.
  { from: /\b(from|via|to)-card(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "$1-[var(--bg-elevation-default)]" },
  { from: /\b(from|via|to)-background(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "$1-[var(--bg-elevation-default)]" },
  { from: /\b(from|via|to)-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "$1-[var(--text-default-primary)]" },
  { from: /\b(from|via|to)-(?:muted|primary)(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "$1-[var(--bg-elevation-highlight)]" },
  // Solid inverted surfaces: a filled dot, bar or indicator painted in the text
  // colour.
  { from: /\bbg-foreground\b(?!-)/g, to: "bg-[var(--text-default-primary)]" },
  { from: /\bbg-border(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "bg-[var(--border-elevation-subtle)]" },
  { from: /\btext-border\b/g, to: "text-default-disabled" },
  { from: /\bdivide-(?:border|foreground)(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "divide-[var(--border-elevation-subtle)]" },
  {
    from: /\btext-background\b/g,
    to: "text-negative-primary",
    why: "text painted in the page background sits on an accent or inverted surface, which is what `negative` means",
  },
  { from: /\bborder-primary(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "border-elevation-default" },
  { from: /\bborder-muted-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "border-elevation-subtle" },
  // An SVG filled in the secondary text colour - the legend swatch of a chart,
  // a status glyph. `fill-` was the one family of this token with no rule, and
  // a new screen brought one in. It takes the arbitrary form because
  // `fill-` reads the COLOUR namespace and the secondary text colour is not in
  // it - the same reason `ring-border` above is written that way.
  { from: /\bfill-muted-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "fill-[var(--text-default-secondary)]" },
  { from: /\bring-border(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "ring-[var(--border-elevation-subtle)]" },
  {
    // A ring in the CARD's own surface colour, not its border: the status dot on
    // a runner avatar punches a hole in the card behind it. `layer-1
    // bg-elevation-default` — what `bg-card` becomes — is a paint utility and
    // has no ring form, so this names the surface variable directly, the same
    // decision as `ring-border` above. A ring in the PAGE background (the
    // video timeline's playhead knob) is the same hole punched in the surface
    // behind it, and takes the same variable.
    from: /\bring-(?:card|background)\b(?!-)/g,
    to: "ring-[var(--bg-elevation-default)]",
  },
  // A border in the PAGE background colour, punching a shape out of the canvas
  // behind it. Same reasoning as `ring-card`: `bg-background` becomes a paint
  // utility with no border form, so name the surface variable.
  { from: /\bborder-background\b(?!-)/g, to: "border-[var(--bg-elevation-default)]" },
  // SVG paint on the same two surfaces. `fill`/`stroke` have no token utility
  // either, so they name the variables directly like their `ring`/`border`
  // counterparts above — a flow-builder node draws its own card shape in SVG.
  { from: /\bfill-card\b(?!-)/g, to: "fill-[var(--bg-elevation-default)]" },
  { from: /\bstroke-border\b(?!-)/g, to: "stroke-[var(--border-elevation-subtle)]" },
  {
    from: /\bstroke-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g,
    to: "stroke-[var(--border-elevation-subtle)]",
    why: "a faint foreground stroke is the track under a progress ring: a divider, not text",
  },
  { from: /\bring-foreground(?:\/(?:\d+|\[[^\]]+\]))?/g, to: "ring-focus" },

  // ───────────────────────────────────────────────────────────────── text ────
  { from: /\btext-muted-foreground\/(?:70|60|50)\b/g, to: "text-default-secondary" },
  {
    from: /\btext-muted-foreground\/(?:40|30|25|20|15|10)\b/g,
    to: "text-default-disabled",
    why: "the bottom of the muted ladder was disabled text in all but name",
  },
  { from: /\btext-muted-foreground\b/g, to: "text-default-secondary" },
  { from: /\btext-foreground\/(?:9\d|8\d|7\d)\b/g, to: "text-default-secondary" },
  { from: /\btext-foreground\/(?:6\d|5\d|4\d|3\d|2\d|1\d|\d)\b/g, to: "text-default-disabled" },
  { from: /\btext-foreground\b(?!-)/g, to: "text-default-primary" },
  { from: /\btext-card-foreground\b/g, to: "text-default-primary" },
  { from: /\btext-popover-foreground\b/g, to: "text-default-primary" },
  { from: /\btext-secondary-foreground\b/g, to: "text-default-primary" },
  {
    from: /\btext-primary-foreground\b/g,
    to: "text-default-primary",
    why: "`--primary` was a surface, so its foreground was ordinary body text",
  },
  {
    from: /\btext-primary\b(?!-)/g,
    to: "text-accent",
    why: "`--primary` was a navy surface, so text in it never meant that colour: the sites (a link's hover, a nudge's icon) meant the brand accent",
  },
  { from: /\btext-muted\b(?!-)/g, to: "text-default-disabled" },

  // ─────────────────────────────────────────────────────────────── borders ───
  {
    from: /\bborder-border\/(?:\d+|\[[^\]]+\])/g,
    to: "border-elevation-subtle-soft",
    why: "a faded hairline is the soft variant, which is itself defined at 15% opacity",
  },
  { from: /\bborder-border\b(?!-)/g, to: "border-elevation-subtle" },
  { from: /\bborder-foreground\/(?:\d+|\[[^\]]+\])/g, to: "border-elevation-subtle-soft" },
  { from: /\bhover:border-input\b/g, to: "hover:border-input-hover" },
  { from: /\bborder-input\b(?!-)/g, to: "border-elevation-subtle" },

  // ────────────────────────────────────────────────── the accent (was cyan) ──
  // `--color-accent` is a product token: one seam carrying the mode switch
  // (blue in Copilot mode, IA purple in XTM One mode) and an operator's
  // white-label colour. It is `--color-*`-namespaced, so opacity survives.
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|caret|accent)-cyan-glow\b/g, to: "$1-accent" },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|caret|accent)-cyan\b(?!-)/g, to: "$1-accent" },

  // ───────────────────────────────────────────────────────────── feedback ────
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|outline|decoration)-destructive\b(?!-)/g, to: "$1-feedback-error-primary" },
  { from: /\btext-destructive-foreground\b/g, to: "text-negative-primary" },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder|caret)-(?:red|rose|pink)-\d{2,3}\b/g, to: "$1-feedback-error-primary" },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder|caret)-(?:emerald|green|lime)-\d{2,3}\b/g, to: "$1-feedback-success-primary" },
  // Amber/yellow splits by prefix, on measured contrast rather than by name.
  // Against the default surface, `feedback-alert-primary` scores 11.3:1 in dark
  // and 1.54:1 in LIGHT — unreadable. `feedback-warning-primary` scores 6.19 and
  // 4.32. So text takes warning and surfaces keep alert, where the contrast
  // obligation belongs to whatever sits on top rather than to the fill itself.
  // Measured in a browser via scripts/gallery-axe.mjs; 4.32 still misses AA for
  // normal text, which is a gap in the design language and is filed as such.
  { from: /\btext-(?:amber|yellow)-\d{2,3}\b/g, to: "text-feedback-warning-primary" },
  // The same split applied to output an earlier rule table wrote: the codemod
  // regenerates from already-migrated source, so a text use of the alert token
  // never meets the palette rule above again. A lock icon or a status label in
  // the alert amber reads at 1.54:1 in light.
  { from: /\btext-feedback-alert-primary\b/g, to: "text-feedback-warning-primary" },
  { from: /\b(bg|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder)-(?:amber|yellow)-\d{2,3}\b/g, to: "$1-feedback-alert-primary" },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|divide|outline|decoration|placeholder|caret)-orange-\d{2,3}\b/g, to: "$1-feedback-warning-primary" },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder|caret)-(?:blue|sky)-\d{2,3}\b/g, to: "$1-feedback-info-primary" },
  {
    from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder|caret)-(?:violet|purple|indigo|fuchsia)-\d{2,3}\b/g,
    to: "$1-filigran-ia-primary",
    why: "the app used violet for AI-generated and AI-adjacent surfaces, which is exactly what the IA family is for",
  },
  { from: /\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|divide|outline|decoration|placeholder|caret)-(?:teal|cyan)-\d{2,3}\b/g, to: "$1-filigran-tonic-primary" },

  // ───────────────────────────────────────────────────────────── neutrals ────
  // Numbered greys carried no product meaning here — they were reached for when
  // the semantic token was not obvious. Each prefix maps onto the neutral role
  // that prefix actually plays.
  { from: /\btext-(?:slate|gray|zinc|neutral|stone)-(?:[3-9]\d{2})\b/g, to: "text-default-secondary" },
  { from: /\btext-(?:slate|gray|zinc|neutral|stone)-(?:[1-2]\d{2}|50)\b/g, to: "text-default-primary" },
  { from: /\bbg-(?:slate|gray|zinc|neutral|stone)-\d{2,3}\b/g, to: "bg-elevation-highlight" },
  { from: /\b(?:border|divide)-(?:slate|gray|zinc|neutral|stone)-\d{2,3}\b/g, to: "border-elevation-subtle" },

  // black / white survive the palette reset in neither direction: they are
  // ordinary theme colours, and `--color-*: initial` removes them.
  {
    from: /\bbg-black\/(?:\d+|\[[^\]]+\])/g,
    to: "bg-scrim",
    why: "a dimmed black overlay is a scrim; the product token composes it from the darkest FDS primitive",
  },
  { from: /\bbg-black\b/g, to: "bg-scrim" },
  {
    from: /\btext-(?:white|black)\b/g,
    // `negative` inverts with the theme, as the accent does, so it is right on
    // the accent and wrong on a fill that keeps one colour in both themes: on
    // the scrim (dark in both) and on the amber fill (light in both) it turns
    // dark-on-dark or light-on-light in one theme. The rules for those fills
    // run first, so the class string already names them here.
    to: (match, offset, whole) =>
      /\bbg-scrim\b/.test(whole)
        ? "text-on-scrim"
        : SOLID_ALERT_FILL.test(whole)
          ? "text-on-feedback-fill"
          : "text-negative-primary",
    label: "text-negative-primary | text-on-scrim | text-on-feedback-fill",
    why: "FDS calls text that sits on an inverted or brand surface `negative`; on a fill that does not invert with the theme the text keeps one colour too",
  },
  { from: /\bbg-white\b/g, to: "bg-elevation-default" },
  { from: /\bborder-white\/(?:\d+|\[[^\]]+\])/g, to: "border-elevation-subtle-soft" },

  // ────────────────────────────────────────────────────────────── focus ──────
  { from: /\bring-ring\b/g, to: "ring-focus" },
  { from: /\bring-offset-background\b/g, to: "ring-offset-focus" },

  {
    from: /\btext-gradient-cyan\b/g,
    to: "text-gradient-accent",
    why: "the utility itself was renamed with the token it composes",
  },

  // ───────────────────────────────────────────────────────────── shadows ─────
  // FDS redefines `--shadow-sm|md|lg|xl` as depth *radii* (4/8/16/20px), not
  // box-shadows, so these four Tailwind utilities stop producing a valid shadow
  // the moment its theme is imported. One product utility replaces them.
  { from: /\bshadow-(?:sm|md|lg|xl|2xl)\b/g, to: "shadow-overlay" },
  {
    // A shadow COLOUR next to the overlay shadow does nothing: `shadow-overlay`
    // is a whole box-shadow that names its own colour (`--shadow-overlay`, the
    // library's darkest grey at 45%), not a shadow that reads `--tw-shadow-color`.
    from: / shadow-(?:black|white)\/(?:\d+|\[[^\]]+\])(?=[\s"'`]|$)/g,
    to: "",
    label: "(dropped next to shadow-overlay)",
  },

  // ────────────────────────────────────────────────────────── typography ─────
  // `(?!-)` on all three: their replacements are themselves prefixed with the
  // pattern they match (`font-sans` → `font-sans-plex`), so without it a second
  // pass — or the rule below firing on the rule above's output — produces
  // `font-sans-plex-geologica`.
  { from: /\bfont-title\b(?!-)/g, to: "font-sans-geologica" },
  { from: /\bfont-mono\b(?!-)/g, to: "font-mono-plex" },
  { from: /\bfont-sans\b(?!-)/g, to: "font-sans-plex" },

  // ────────────────────────────────────────────────────────── type styles ────
  // FDS ships composite type styles (family + size + weight + line-height +
  // tracking) rather than a bare size scale. Only the two that match Tailwind's
  // size EXACTLY are swapped here:
  //
  //   text-xs  12px → content-compact 12px (--text-2)
  //   text-sm  14px → content-base    14px (--text-3)
  //
  // `text-base` (16px) has no content-* counterpart — the 16px step belongs to
  // `title-sm`, a heading — and `text-lg`/`text-2xl`/`text-3xl` would all change
  // size, so those are left alone and recorded as a follow-up.
  //
  // Line height DOES change: Tailwind's text-xs pins 16px where FDS uses 150%
  // (18px). Rows get marginally taller, everywhere. That is the design
  // language's rhythm, and it is the reason this lands as its own commit.
  { from: /\btext-xs\b/g, to: "content-compact" },
  { from: /\btext-sm\b/g, to: "content-base" },
  { from: /\btext-\[10px\]/g, to: "content-caption" },

  // ───────────────────────────────────────────────────────────── radius ──────
  // FDS's radius scale is 4 / 12 / 20px against Tailwind's 2 / 4 / 6 / 8px, so
  // every `rounded-*` utility silently changes size under its theme. Controls
  // and surfaces both use `rounded-sm` (4px) throughout the library's own
  // components, which is the closest thing to XTM One's previous 8px `rounded-lg`
  // that the design language actually offers.
  //
  // The larger Tailwind radii take the same step. They used to map to the
  // library's `rounded-md` (12px), but `rounded-md` is also a Tailwind input
  // of the rule above, and the codemod regenerates from committed - already
  // migrated - source: the next run turned every such `rounded-md` into
  // `rounded-sm`. The app has been 4px throughout since, which is what this
  // PR shipped; the rule now says so, and a second run changes nothing.
  { from: /\brounded-(?:xs|sm|md|lg|xl|2xl|3xl)\b/g, to: "rounded-sm" },
  {
    from: /\brounded\b(?!-)/g,
    to: "rounded-sm",
    // The only rule whose pattern is also an ordinary English word. In a comment
    // it corrupts prose — "numbers are rounded to a whole number" became
    // "rounded-sm to a whole number", and a reviewer caught it, not a gate. So
    // this one is applied to string literals only. A `rounded` written as a class
    // reference INSIDE a comment is therefore left stale, which is the lesser
    // harm: a wrong word in documentation is worse than an outdated one.
    literalsOnly: true,
  },

  // ─────────────────────────────────── frozen palette literals ──────────────
  // Chart series, graph edges and canvas fills that hardcoded the OLD palette's
  // hex/HSL values instead of reading a token — invisible to the utility rules
  // above because they are values, not classes.
  //
  // Safe as presentation attributes: SVG presentation attributes are parsed as
  // CSS property values, so `fill="var(--color-accent)"` resolves (verified in
  // a browser rather than assumed — a `var()` that did NOT resolve would paint
  // black and look like a data bug).
  //
  // Only the unambiguous ones. Plain white, black and the neutral greys are
  // left alone: whether one is text, a canvas or a chart gridline depends on its
  // call site, and a wrong guess is a silent contrast failure.
  {
    // `--background` held an HSL TRIPLET, so every use was wrapped in hsl(),
    // usually with an alpha. An FDS variable is a whole colour and cannot sit
    // inside hsl(), so the alpha moves to color-mix.
    from: /hsl\(var\(--background\)\s*\/\s*([\d.]+)\)/g,
    to: "color-mix(in srgb, var(--bg-elevation-default) calc($1 * 100%), transparent)",
  },
  { from: /hsl\(197[, ]\s*100%[, ]\s*(?:53|42|38)%\)/g, to: "var(--color-accent)" },
  { from: /#00d4ff/gi, to: "var(--color-accent)" },
  { from: /hsl\(160[, ]\s*84%[, ]\s*(?:39|45)%\)/g, to: "var(--color-feedback-success-primary)" },
  { from: /#34d399/gi, to: "var(--color-feedback-success-primary)" },
  { from: /hsl\(0[, ]\s*84%[, ]\s*(?:60|65)%\)/g, to: "var(--color-feedback-error-primary)" },
  { from: /#f87171/gi, to: "var(--color-feedback-error-primary)" },
  { from: /hsl\(38[, ]\s*92%[, ]\s*(?:50|60|65)%\)/g, to: "var(--color-feedback-alert-primary)" },
  { from: /#fbbf24/gi, to: "var(--color-feedback-alert-primary)" },
  { from: /#fb923c/gi, to: "var(--color-feedback-warning-primary)" },
  { from: /hsl\(263[, ]\s*70%[, ]\s*50%\)/g, to: "var(--color-filigran-ia-primary)" },

  // The same values written as Tailwind arbitrary values, where the right
  // replacement is the whole utility rather than the colour inside it.
  { from: /\b(bg|text|border|ring|from|to|via)-\[hsl\(197_100%_53%\/0\.15\)\]/g, to: "$1-accent/15" },
  { from: /\b(bg|text|border|ring|from|to|via)-\[hsl\(197_100%_53%\/0\.1\)\]/g, to: "$1-accent/10" },
  { from: /\b(bg|text|border|ring|from|to|via)-\[hsl\(197_100%_53%\/0\.25\)\]/g, to: "$1-accent/25" },
  { from: /\b(bg|text|border|ring|from|to|via)-\[hsl\(197_100%_53%\/0\.4\)\]/g, to: "$1-accent/40" },

  // The accent inside a COMPOSED arbitrary value — a glow or a ring, where the
  // colour is one term of a `shadow-[…]` and the rules above (which replace a
  // whole utility) cannot reach it. Tailwind spells such a value with `_` for
  // every space, so the alpha is already attached with `/`; it moves to
  // `color-mix` for the same reason as everywhere else — an FDS variable is a
  // whole colour and cannot sit inside `hsl()`. The spaces this produces are
  // put back as `_` by the repair rule below, which runs after it.
  //
  // Only lightness 53% — the accent itself. The node-graph 45% and 42% variants
  // are a palette the design system does not publish (LIBRARY-FEEDBACK F24) and
  // stay literals until it does, rather than being flattened onto the accent.
  {
    from: /-\[([^\]"'`]*)hsl\(197_100%_53%\/([\d.]+)\)([^\]"'`]*)\]/g,
    to: (_m, before, alpha, after) =>
      `-[${before}color-mix(in srgb, var(--color-accent) calc(${alpha} * 100%), transparent)${after}]`,
    label: "accent inside a composed arbitrary value (glow, ring)",
  },


  // ──────────────────────────────────────── CSS variables used from JS ───────
  // Inline styles and chart props read tokens directly. The old tokens were HSL
  // *component triplets* (`hsl(var(--cyan))`), the new ones are complete
  // colours, so the `hsl()` wrapper has to go with the rename — left in place it
  // produces `hsl(#42caff)`, which is invalid and silently paints nothing.
  { from: /hsl\(var\(--cyan\)\s*\/\s*([\d.]+)\)/g, to: "color-mix(in srgb, var(--color-accent) calc($1 * 100%), transparent)" },
  { from: /hsl\(var\(--foreground\)\s*\/\s*([\d.]+)\)/g, to: "color-mix(in srgb, var(--text-default-primary) calc($1 * 100%), transparent)" },
  { from: /hsl\(var\(--border\)\s*\/\s*([\d.]+)\)/g, to: "color-mix(in srgb, var(--border-elevation-subtle) calc($1 * 100%), transparent)" },
  { from: /hsl\(var\(--cyan\)\)/g, to: "var(--color-accent)" },
  // The bare (alpha-free) counterparts of the triplet rules above. Each one was
  // a shape the residue counter flagged with no rule to fix it, which the
  // presentation speaker view found the hard way: those variables no longer
  // exist after the migration, so the references resolved to nothing and the
  // view rendered with no background, no pace bar and no code chips.
  //
  // `--primary` is deliberately NOT here. It was a navy SURFACE, so a class-level
  // `bg-primary` maps cleanly to `bg-elevation-highlight`, but the same variable
  // used as a `color:` means the accent — and CSS gives a regex no way to tell
  // which property it is sitting on. That one stays a call-site read.
  { from: /hsl\(var\(--background\)\)/g, to: "var(--bg-elevation-default)" },
  { from: /hsl\(var\(--destructive\)\)/g, to: "var(--color-feedback-error-primary)" },
  {
    from: /hsl\(var\(--muted\)\s*\/\s*([\d.]+)\)/g,
    to: "color-mix(in srgb, var(--bg-elevation-highlight) calc($1 * 100%), transparent)",
  },
  { from: /hsl\(var\(--muted\)\)/g, to: "var(--bg-elevation-highlight)" },

  { from: /hsl\(var\(--border\)\)/g, to: "var(--border-elevation-subtle)" },
  { from: /hsl\(var\(--muted-foreground\)\)/g, to: "var(--text-default-secondary)" },
  { from: /hsl\(var\(--card\)\)/g, to: "var(--bg-elevation-default)" },
  { from: /hsl\(var\(--foreground\)\)/g, to: "var(--text-default-primary)" },
  { from: /var\(--cyan\)/g, to: "var(--color-accent)" },
  { from: /var\(--muted-foreground\)/g, to: "var(--text-default-secondary)" },
  { from: /var\(--border\)/g, to: "var(--border-elevation-subtle)" },
  { from: /var\(--card\)/g, to: "var(--bg-elevation-default)" },
  { from: /var\(--foreground\)/g, to: "var(--text-default-primary)" },
  // A Tailwind ARBITRARY value has no spaces: the scanner reads the class out of
  // the source text, so a space ends it and the class is simply never generated
  // — silently, with no build error and nothing in the stylesheet. The rules
  // below rewrite a CSS *value*, and `color-mix(in srgb, …)` is spelled with
  // spaces; where that value sat inside `shadow-[…]` the result compiled to
  // nothing and the glow disappeared (21 sites: every accent glow on the chat
  // bubbles, the trace drawer, the catalogue cards and the deck panels, plus
  // the selected-row ring in the History and Layout panels).
  //
  // So the repair runs AFTER them and puts the value back in the form Tailwind
  // reads: `_` is its escape for a space. Anchored on `color-mix(in srgb,`
  // rather than on spaces in general, because that is the shape those rules
  // produce — a hand-written arbitrary value with a space is a different bug
  // and should be found by `verify-classes`, not quietly rewritten here.
  {
    from: /-\[([^\]"'`]*color-mix\(in srgb,[^\]"'`]*)\]/g,
    to: (_match, value) => `-[${value.replace(/ /g, "_")}]`,
    label: "spaces → _ inside the arbitrary value (or Tailwind emits no class)",
  },
];

/**
 * Utilities that must no longer appear in product code once the migration has
 * run. `count-legacy.mjs` reports them; anything left is either a rule that
 * needs writing or a site that needs a decision, never a thing to leave alone.
 */
export const legacyPatterns = [
  {
    // Text and icons take `warning`, surfaces keep `alert` (the rule above);
    // the alert token as a foreground is unreadable in light.
    label: "alert token used for text",
    pattern: /\btext-feedback-alert-primary\b/g,
  },
  {
    label: "legacy semantic tokens",
    pattern:
      /\b(?:bg|text|border(?:-[trblxyse])?|ring|from|to|via|divide|fill|stroke|outline|decoration|placeholder|caret|shadow)-(?:background|foreground|card|popover|primary|secondary|muted|destructive|border|input(?!-(?:default|hover|focus|error|disabled|helper|label|placeholder|required)\b)|ring|cyan)(?:-foreground)?(?:\/(?:\d+|\[[^\]]+\]))?\b/g,
    // `-accent` is deliberately absent: it is the product token that survives.
    // The design system's field tokens (`bg-input-default`, `border-input-hover`,
    // `border-input-focus`, `border-input-error`, `text-input-disabled`,
    // `text-input-helper`, `text-input-label`, `text-input-placeholder`,
    // `text-input-required`) are the
    // migration's targets, not legacy: only the bare shadcn `-input` is counted.
  },
  {
    label: "Tailwind palette colours",
    pattern:
      /\b(?:bg|text|border(?:-[trblxyse])?|ring|from|to|via|divide|fill|stroke|outline|decoration|placeholder|caret|shadow)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}(?:\/(?:\d+|\[[^\]]+\]))?\b/g,
  },
  {
    label: "black / white colours",
    pattern: /\b(?:bg|text|border(?:-[trblxyse])?|ring|from|to|via|divide|fill|stroke|shadow)-(?:white|black)(?:\/(?:\d+|\[[^\]]+\]))?\b/g,
  },
  {
    label: "hardcoded colour literals",
    pattern: /\[(?:#[0-9a-fA-F]{3,8}|hsl\(|rgba?\()[^\]]*\]|hsl\(\s*\d[^)]*\)/g,
  },
  {
    label: "legacy CSS variables",
    pattern: /var\(--(?:cyan|cyan-glow|background|foreground|card|popover|primary|secondary|muted|muted-foreground|accent-foreground|destructive|border|input|ring)\)/g,
  },
  {
    label: "Tailwind shadow scale overridden by FDS depth tokens",
    pattern: /\bshadow-(?:sm|md|lg|xl|2xl)\b/g,
  },
  {
    // The library's radius scale has no `xs`, and `lg` and above are steps the
    // migration never produces (`rounded-lg` became `rounded-sm`): any of them
    // left - a side-specific one included - was missed.
    label: "radius outside the migrated scale",
    pattern: /\brounded(?:-[trblse]{1,2})?-(?:xs|lg|xl|2xl|3xl)\b/g,
  },
  {
    // `bg-X hover:bg-X`: the hover the mapping collapsed onto its rest state,
    // which gives no hover feedback. The codemod steps it up (`hoverStep`);
    // one left here has no step to take and needs a decision.
    label: "identical rest and hover surface",
    find: (line) =>
      [...line.matchAll(/["'`]([^"'`]*)["'`]/g)].flatMap(([, classes]) => {
        const tokens = classes.split(/\s+/);
        const resting = new Set(tokens.filter((token) => !token.includes(":")));
        return tokens.filter((token) => /^hover:(?:bg|border)-/.test(token) && resting.has(token.slice(6)));
      }),
  },

];

/**
 * A hover the mapping collapsed onto its rest state moves one step up its
 * ladder. Two legacy shades often land on one token (`bg-foreground/5` and
 * `/10` are both the hover surface, `bg-amber-500` and `-600` the same amber),
 * which leaves `bg-X hover:bg-X`: no hover feedback at all. Only classes with
 * a next step are touched; anything else is left for `count-legacy` to report.
 */
const HOVER_STEP = new Map([
  ["bg-elevation-default", "bg-elevation-hover"],
  ["bg-elevation-hover", "bg-elevation-highlight"],
  ["bg-scrim", "bg-scrim-strong"],
  ["border-elevation-subtle-soft", "border-elevation-subtle"],
  ["border-elevation-subtle", "border-elevation-default"],
  ["text-default-disabled", "text-default-secondary"],
  ["text-default-secondary", "text-default-primary"],
]);
const SOLID_FEEDBACK_FILL = /^bg-feedback-(?:alert|error|success|warning|info)-primary$/;

export function hoverStep(cls) {
  if (HOVER_STEP.has(cls)) return HOVER_STEP.get(cls);
  if (SOLID_FEEDBACK_FILL.test(cls)) return `${cls}/90`;
  return null;
}

