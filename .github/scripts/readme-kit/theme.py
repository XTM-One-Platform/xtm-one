"""The two README palettes, read from the Filigran Brand Deck theme
(xtm-one backend/app/services/template_container_seed.py, base dark palette
and its light variant).

Rule G (Sam, 19 Nov 2026): every emblem colour is a fill read from the official
brand lockups (xtm-one backend/app/agents/seed_data/brand_assets/filigran/
*-logo.svg and *-logo-light.svg, the files the Filigran Brand Deck template
syncs from), never invented:
- opencti, openaev, opencrq: #0FBCFF on dark, #0015A8 on light (one colour);
- xtm-one: the Figma lockup (XTM_Hub frame 6685:1130, design-system
  product-logos/xtmone.svg): emblem #A47AF0 on dark (xtm-one/xtm-one-mark.svg),
  #651FE5 on light (filigran/xtm-one-mark-light.svg);
- xtm-hub: white on dark, #18191B on light (its twins carry no colour);
- accent (card bars, edges, arrows, highlights): the same cyan / deep blue,
  one token set for the five READMEs."""

BRAND_CYAN = "#0FBCFF"       # opencti-logo.svg, openaev-logo.svg, opencrq-logo.svg, filigran-logo.svg
BRAND_DEEP_BLUE = "#0015A8"  # the -light twins of the same four
BRAND_PURPLE_DARK = "#A47AF0"   # xtm-one/xtm-one-mark.svg (Figma purple-300)
BRAND_PURPLE_LIGHT = "#651FE5"  # filigran/xtm-one-mark-light.svg (Figma purple-500)
INK_DARK = "#FFFFFF"         # xtm-hub-logo.svg
INK_LIGHT = "#18191B"        # xtm-hub-logo-light.svg, every -light wordmark

DARK = {
    "name": "dark",
    "page": "#0A0C1C",
    "card": "#0D172B",
    "card2": "#111E36",
    "line": "#1F3965",
    "fg": "#FBFBFB",
    "muted": "#A9B1C6",
    "accent": BRAND_CYAN,
    "grid": "#1A2A4A",
    "products": {
        "xtm-one": BRAND_PURPLE_DARK,
        "opencti": BRAND_CYAN,
        "openaev": BRAND_CYAN,
        "opencrq": BRAND_CYAN,
        "xtm-hub": INK_DARK,
    },
}

LIGHT = {
    "name": "light",
    "page": "#E6F7FF",
    "card": "#FFFFFF",
    "card2": "#F3FAFF",
    "line": "#B3BBF4",
    "fg": "#00020C",
    "muted": "#4A5068",
    "accent": BRAND_DEEP_BLUE,
    "grid": "#CFE3F5",
    "products": {
        "xtm-one": BRAND_PURPLE_LIGHT,
        "opencti": BRAND_DEEP_BLUE,
        "openaev": BRAND_DEEP_BLUE,
        "opencrq": BRAND_DEEP_BLUE,
        "xtm-hub": INK_LIGHT,
    },
}

THEMES = (DARK, LIGHT)

PRODUCTS = {
    "xtm-one": {"name": "XTM One", "role": "Core and agentic layer"},
    "opencti": {"name": "OpenCTI", "role": "Threat intelligence"},
    "openaev": {"name": "OpenAEV", "role": "Adversarial exposure validation"},
    "opencrq": {"name": "OpenCRQ", "role": "Cyber risk quantification"},
    "xtm-hub": {"name": "XTM Hub", "role": "Tradecraft library and gateway"},
}
