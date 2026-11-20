"""One README template for the five products (doc 33 v3, s.4: 13 sections, same
headings, same order, same blocks). A product fills a data module
(readme_data/<key>.py exposing DATA); it never changes the layout.

Usage:
    python .github/scripts/readme-kit/readme.py KEY > README.md
    python .github/scripts/readme-kit/readme.py KEY --out PATH       # the same, into a file
    python .github/scripts/readme-kit/readme.py --check KEY ...   # forbidden words + counts

Images (all <picture> pairs, dark then light, under .github/img/readme/):
banner.svg (one file, brand blue, readable in both themes), hero-*, g1..g6-*,
walkthrough-*, flow-*, xtm-platform-ctem-*, stars-*.
"""
import importlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

IMG = "./.github/img/readme"
STAGES = ("Scoping", "Discovery", "Prioritization", "Validation", "Mobilization")
ROLES = ("CTI", "SOC", "Detection engineering", "Red and purple team",
         "Vulnerability and exposure management", "Risk and CISO")

# The five-row product table of "Part of the XTM Platform": identical in all five.
PLATFORM = (
    ("xtm-one", "XTM One", "Core and agentic layer: runs CTEM across the modules and gets the fix done",
     "https://github.com/XTM-One-Platform/xtm-one", "https://filigran.io/products/xtm-one"),
    ("opencti", "OpenCTI", "Scoping and Discovery: know the adversary and the threats that target you",
     "https://github.com/OpenCTI-Platform/opencti", "https://filigran.io/products/opencti"),
    ("opencrq", "OpenCRQ", "Prioritization: turn exposure into financial risk",
     "https://github.com/OpenCRQ-Platform/opencrq", "https://filigran.io/products/opencrq"),
    ("openaev", "OpenAEV", "Validation: prove your defenses against real attacker techniques",
     "https://github.com/OpenAEV-Platform/openaev", "https://filigran.io/products/openaev"),
    ("xtm-hub", "XTM Hub", "Tradecraft library and gateway: integrations, dashboards and scenarios for every product",
     "https://github.com/XTM-Hub/xtm-hub", "https://filigran.io/platforms/xtm-hub/"),
)

SECTIONS = ("Why {name}", "Capabilities", "Gallery", "How it works", "Part of the XTM Platform",
            "Use cases by security role", "Quick start", "Editions", "Integrations and ecosystem",
            "Community", "Contributing", "Security", "License", "About Filigran")

FORBIDDEN = re.compile(r"coming|next release|upcoming|\bsoon\b|preview|in review|new feature|now available|roadmap|"
                       r"pull/|\u2014|\u2013|!(?!\[)|OpenGRC|OpenBAS|XTM Suite|C-TEM|Agentic XTM|"
                       r"star-history|contrib\.rocks|```mermaid", re.I)


def picture(stem: str, alt: str, width: str = "100%") -> str:
    return (f'<picture>\n'
            f'  <source media="(prefers-color-scheme: dark)" srcset="{IMG}/{stem}-dark.{_ext(stem)}">\n'
            f'  <source media="(prefers-color-scheme: light)" srcset="{IMG}/{stem}-light.{_ext(stem)}">\n'
            f'  <img alt="{alt}" src="{IMG}/{stem}-light.{_ext(stem)}" width="{width}">\n'
            f'</picture>')


def _ext(stem: str) -> str:
    return "svg" if stem in ("flow", "xtm-platform-ctem", "stars", "filigran") else "webp"


# Rule H2: every badge value is brand deep blue under white text (contrast > 7:1), every label ink;
# no cyan, green or light fill under white text, same row and colours in all five READMEs.
BADGE_VALUE = "001BDA"
BADGE_LABEL = "0A0C1C"


def badge(label: str, message: str, color: str, link: str, logo: str = "", dynamic: str = "") -> str:
    src = dynamic or f"https://img.shields.io/badge/{_esc(label)}-{_esc(message)}-{color}"
    sep = "&" if "?" in src else "?"
    src += f"{sep}style=flat-square&labelColor={BADGE_LABEL}" + (f"&logo={logo}&logoColor=white" if logo else "")
    if dynamic:
        src += f"&color={color}"
    return f'<a href="{link}"><img src="{src}" alt="{label}"></a>'


def _esc(s: str) -> str:
    return s.replace("-", "--").replace("_", "__").replace(" ", "%20").replace("+", "%2B")


# Rule K1 (doc 33, "Alignment over repo state"): ONE badge row, the same 7 badges in this order in
# all five READMEs, no product-specific omission.
BADGE_ORDER = ("stars", "release", "docker pulls", "license", "CI", "docs", "community")
# Rule K2: the 7 badges fit on ONE line of the github.com article (823 px at 1280, 838 at 1728):
# UX round 1 of openaev #9474 measured 833 px with "Apache 2.0 + EE" and "docs.openaev.io" and
# the community badge wrapped. Keep static values short: license "Apache 2.0", docs "read".


def badges(d: dict) -> str:
    repo, b = d["repo"], []
    ci_wf, ci_branch = d["ci"]
    ci_link = f"https://github.com/{repo}/actions/workflows/{ci_wf}"
    if d.get("private"):
        # shields.io cannot read a private repository ("repo not found"): static badges, same order
        b.append(badge("stars", "on GitHub", BADGE_VALUE, f"https://github.com/{repo}/stargazers", "github"))
        b.append(badge("release", "latest", BADGE_VALUE, f"https://github.com/{repo}/releases/latest"))
    else:
        b.append(badge("stars", "", BADGE_VALUE, f"https://github.com/{repo}/stargazers", "github",
                       f"https://img.shields.io/github/stars/{repo}"))
        b.append(badge("release", "", BADGE_VALUE, f"https://github.com/{repo}/releases/latest", "",
                       f"https://img.shields.io/github/v/release/{repo}"))
    if d.get("docker"):
        b.append(badge("docker pulls", "", BADGE_VALUE, f"https://hub.docker.com/r/{d['docker']}", "docker",
                       f"https://img.shields.io/docker/pulls/{d['docker']}"))
    else:
        # no public image (a hosted service): a static badge in the same slot, never an omission
        msg, link = d["docker_static"]
        b.append(badge("docker pulls", msg, BADGE_VALUE, link, "docker"))
    b.append(badge("license", d["license_badge"], BADGE_VALUE, "LICENSE"))
    if d.get("private"):
        b.append(badge("CI", "GitHub Actions", BADGE_VALUE, ci_link, "githubactions"))
    else:
        b.append(badge("CI", "", BADGE_VALUE, ci_link, "githubactions",
                       f"https://img.shields.io/github/actions/workflow/status/{repo}/{ci_wf}"
                       f"?branch={ci_branch}&label=CI"))
    # Rule K2: the row fits on one line of the github.com article (823 px at 1280): short values,
    # the docs host is the link target, never the badge text.
    b.append(badge("docs", "read", BADGE_VALUE, d["docs"]))
    b.append(badge("community", "slack", BADGE_VALUE, "https://community.filigran.io", "slack"))
    return '<p align="center">\n  ' + "\n  ".join(b) + "\n</p>"


def anchor(h: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")


def render(d: dict) -> str:
    n, repo = d["name"], d["repo"]
    heads = [h.format(name=n) for h in SECTIONS]
    out = []
    a = out.append
    # 1. header, badges, tagline, pitch, star call to action, hero
    a(f'<a href="{d["page"]}"><img src="{IMG}/banner.svg" width="100%" alt="{n} by Filigran"></a>\n')
    a(badges(d) + "\n")
    a(f'<p align="center"><b>{d["tagline"]}</b></p>\n')
    a(d["pitch"] + "\n")
    a(f'<p align="center">If {n} helps your security program, '
      f'<a href="https://github.com/{repo}/stargazers">give it a star</a>: it is how the project grows.</p>\n')
    a(picture("hero", d["hero_alt"]) + "\n")
    # 2. table of contents
    a("## Table of contents\n")
    a("\n".join(f"- [{h}](#{anchor(h)})" for h in heads) + "\n")
    # 3. why
    a(f"## {heads[0]}\n")
    a(d["why_intro"] + "\n")
    a("\n".join(f"- **{t}** {x}" for t, x in d["why"]) + "\n")
    # 4. capabilities by CTEM stage
    a("## Capabilities\n")
    a(f"| CTEM stage | What {n} gives your team |\n|---|---|")
    a("\n".join(f"| **{s}** | {d['capabilities'][s]} |" for s in STAGES) + "\n")
    # 5. gallery (exactly 6, 2 x 3) and walkthrough
    a("## Gallery\n")
    g = d["gallery"]
    assert len(g) == 6, "gallery must hold exactly 6 screenshots"
    rows = []
    for r in range(3):
        cells = []
        for c in range(2):
            i = r * 2 + c
            title, caption, alt = g[i]
            cells.append(f'    <td width="50%" valign="top">\n'
                         + "\n".join("      " + ln for ln in picture(f"g{i + 1}", alt).split("\n"))
                         + f"\n      <p><b>{title}.</b> {caption}</p>\n    </td>")
        rows.append("  <tr>\n" + "\n".join(cells) + "\n  </tr>")
    a("<table>\n" + "\n".join(rows) + "\n</table>\n")
    a(f"**Walkthrough.** {d['walkthrough']}\n")
    a(picture("walkthrough", d["walkthrough_alt"]) + "\n")
    # 6. how it works
    a("## How it works\n")
    a(d["how"] + "\n")
    a(picture("flow", d["flow_alt"]) + "\n")
    # 7. part of the XTM Platform
    a("## Part of the XTM Platform\n")
    a(f"{n} is one of the five products of the [XTM Platform](https://filigran.io), the open platform for "
      "Agentic CTEM. Each product owns stages of the CTEM loop; XTM One runs the loop across them.\n")
    a(picture("xtm-platform-ctem", "The XTM Platform and the CTEM loop: XTM One at the core, OpenCTI, OpenCRQ "
              "and OpenAEV as modules, XTM Hub as the library, around the five stages scoping, discovery, "
              "prioritization, validation and mobilization") + "\n")
    a("| | Product | Role in CTEM | Repository | Product page |\n|---|---|---|---|---|")
    a("\n".join(f'| <picture><source media="(prefers-color-scheme: dark)" srcset="{IMG}/mark-{k}-dark.svg">'
                f'<img src="{IMG}/mark-{k}-light.svg" width="24" alt=""></picture> | **{pn}** | {role} | '
                f"[{rp.split('github.com/')[1]}]({rp}) | [{pg.split('//')[1].rstrip('/')}]({pg}) |"
                for k, pn, role, rp, pg in PLATFORM) + "\n")
    # 8. use cases by security role
    a("## Use cases by security role\n")
    a(f"| Role | What {n} does for it |\n|---|---|")
    a("\n".join(f"| **{r}** | {d['roles'][r]} |" for r in ROLES) + "\n")
    # 9. quick start
    a("## Quick start\n")
    a(d["quickstart"] + "\n")
    # 10. editions
    a("## Editions\n")
    a(d["editions"] + "\n")
    # 11. integrations and ecosystem
    a("## Integrations and ecosystem\n")
    a("\n".join(f"- **{t}** {x}" for t, x in d["integrations"]) + "\n")
    # 12. community
    a("## Community\n")
    a(f"Star {n} to follow it and to help other security teams find it.\n")
    a(picture("stars", f"GitHub stars of {n} over time"))
    a("")
    a(f"- **Questions and discussion**: the Filigran [community Slack](https://community.filigran.io).\n"
      f"- **Bugs and feature requests**: [GitHub issues](https://github.com/{repo}/issues).\n"
      f"- **Learn**: free courses on [Filigran Academy](https://academy.filigran.io).\n")
    # 13. contributing, security, license, about
    a("## Contributing\n")
    a(f"Read the [contributing guide](CONTRIBUTING.md) to set up a development environment and propose a change. "
      f"{n} has adopted a [Code of Conduct](CODE_OF_CONDUCT.md). First contribution: the "
      f"[good first issues](https://github.com/{repo}/labels/good%20first%20issue).\n")
    a("## Security\n")
    a("Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), never in a public issue.\n")
    a("## License\n")
    a(d["license_text"] + "\n")
    a("## About Filigran\n")
    a(f"{n} is designed and developed by [Filigran](https://filigran.io), the company behind the "
      "[XTM Platform](https://filigran.io) for Agentic CTEM.\n")
    # Rule H1: GitHub's sanitizer empties a <picture> nested in <a> (the bare <img> then shows the
    # light logo on the dark theme): the logo stands alone, the link is the word Filigran above.
    a(picture("filigran", "Filigran", "240"))
    return "\n".join(out) + "\n"


def check(text: str) -> list[str]:
    prose = re.sub(r"<[^>]+>|\]\([^)]*\)|https?://\S+", " ", text)
    errs = [f"forbidden: {m.group(0)!r}" for m in FORBIDDEN.finditer(prose)]
    pics = text.count("<picture>")
    # rule G4: 12 visuals + the 5 platform-table emblems, all dark / light pairs
    errs += [f"unpaired picture count {pics}"] if pics != 17 else []
    srcs = text.count('media="(prefers-color-scheme: dark)"')
    errs += [f"pictures without a dark source: {pics - srcs}"] if srcs != pics else []
    lone = len(re.findall(r"<img\b", re.sub(r"<picture>.*?</picture>", "", text, flags=re.S)))
    lone -= text.count("img.shields.io")
    lone -= text.count('/banner.svg"')  # rule G5: the header banner is Filigran blue on both themes
    errs += [f"single-theme <img> outside a <picture>: {lone}"] if lone > 0 else []
    # rule H1: a <picture> inside <a> loses its <source> children on github.com
    nested = len(re.findall(r"<a\b[^>]*>\s*<picture>", text))
    errs += [f"<picture> nested in <a>: {nested}"] if nested else []
    # rule H2: badge values and labels in the two brand tokens only
    for m in re.finditer(r"img\.shields\.io/[^\"]+", text):
        url = m.group(0)
        cols = re.findall(r"(?:[?&](?:color|labelColor)=|/badge/[^\"?]*-)([0-9A-Fa-f]{6})\b", url)
        bad = [c for c in cols if c.upper() not in (BADGE_VALUE, BADGE_LABEL)]
        errs += [f"badge colour off-brand {bad}: {url[:60]}"] if bad else []
    # rule K1: the 7 badges, in the kit's order
    row = re.search(r'<p align="center">(.*?)</p>', text, re.S)
    alts = re.findall(r'alt="([^"]+)"', row.group(1)) if row else []
    errs += [f"badge row {alts}"] if tuple(alts) != BADGE_ORDER else []
    heads = re.findall(r"^## (.+)$", text, re.M)
    errs += [f"headings {len(heads)}"] if len(heads) != 15 else []
    return errs


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--check":
        bad = 0
        for k in args[1:]:
            e = check(render(importlib.import_module(f"readme_data.{k.replace('-', '_')}").DATA))
            print(k, "OK" if not e else e)
            bad += bool(e)
        sys.exit(bad)
    text = render(importlib.import_module(f"readme_data.{args[0].replace('-', '_')}").DATA)
    if len(args) == 3 and args[1] == "--out":
        os.makedirs(os.path.dirname(args[2]) or ".", exist_ok=True)
        with open(args[2], "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"{args[0]}: {len(text)} characters written to {args[2]}")
    else:
        sys.stdout.write(text)
