"""The two README palettes, read from the Filigran Brand Deck theme
(xtm-one backend/app/services/template_container_seed.py, base dark palette
and its light variant)."""

DARK = {
    "name": "dark",
    "page": "#0A0C1C",
    "card": "#0D172B",
    "card2": "#111E36",
    "line": "#1F3965",
    "fg": "#FBFBFB",
    "muted": "#A9B1C6",
    "accent": "#0FBCFF",
    "accent2": "#00F1BD",
    "grid": "#1A2A4A",
    "products": {
        "xtm-one": "#A64DFF",
        "opencti": "#0FBCFF",
        "openaev": "#6676E9",
        "opencrq": "#9FE4FF",
        "xtm-hub": "#00F1BD",
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
    "accent": "#001BDA",
    "accent2": "#00B58E",
    "grid": "#CFE3F5",
    "products": {
        "xtm-one": "#5E1AD5",
        "opencti": "#001BDA",
        "openaev": "#0B8DBF",
        "opencrq": "#071578",
        "xtm-hub": "#00B58E",
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
