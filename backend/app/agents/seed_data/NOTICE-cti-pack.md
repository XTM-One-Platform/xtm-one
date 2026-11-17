# Third-party content in the CTI pack

The CTI agent templates and skills seeded in XTM One mode are adapted from two
public sources. They are rewritten for XTM One: the integrations replace the
sources' scripts and API keys, platform objects (OpenCTI, agent memory, reports)
replace their local files, and the templates read their inputs from manual
assignment parameters. The wiring lives in `app/agents/cti_templates.py` and
`app/agents/cti_skills.py`; the design is in `dev-docs/cti-pack.md`.

## Feedly - CTI Prompt Library

| | |
| --- | --- |
| Used in | `seed_data/cti_templates/*/` (personas and assignment prompts) |
| Upstream | Feedly TI Essentials: [CTI Prompt Library](https://feedly.com/ti-essentials/posts/cyber-threat-intel-prompt-library), [Volume 2](https://feedly.com/ti-essentials/posts/cti-prompt-library-volume-2), [How to Automate Common CTI Workflows](https://feedly.com/ti-essentials/posts/how-to-automate-common-cti-workflows), [Accelerating BFSI CTI Workflows](https://feedly.com/ti-essentials/posts/accelerating-banking-financial-services-and-insurance-cti-workflows-with-prompt-driven) |
| Snapshot | "Feedly's Complete CTI Prompt Library", July 2026 (44 prompts) |
| Terms | Shared freely by Feedly with the cybersecurity community, no licence or sign-up required; attribution appreciated. Provided as-is, without warranty. |

## Liberty91 - cti-skills

| | |
| --- | --- |
| Used in | `seed_data/skill_content/cti-*.md`, `seed_data/skill_resources/cti-*/` |
| In every copy | Each skill carries the copyright notice as its `license` (the Agent Skills frontmatter field, shown in the skill's Catalog tab, never in the text the agent reads) and bundles the licence below as `LICENSE.md` (`seed_data/licenses/cti-skills-LICENSE.md`); a JSON or agent-bundle export and a public-catalog clone carry both. |
| Upstream | <https://github.com/Liberty91LTD/cti-skills> (pack version 1.10.0, 2026-07-02) |
| Licence | MIT (below) |

```
MIT License

Copyright (c) 2026 Liberty91 Ltd

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
