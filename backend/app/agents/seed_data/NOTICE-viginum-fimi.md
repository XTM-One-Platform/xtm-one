# Third-party content in the VIGINUM FIMI knowledge bases

The built-in knowledge bases `viginum-fimi-doctrine` and `viginum-fimi-publications` (`seed_data/knowledge_base/`) are built from publications of VIGINUM, the French service for vigilance and protection against foreign digital interference, part of the SGDSN. They ground the `FIMI Analyst` and, for the doctrine, the `FIMI STIX Transformer` (`app/agents/builtin_agents.py`).

## SGDSN / VIGINUM publications

| | |
| --- | --- |
| Used in | `seed_data/knowledge_base/viginum-fimi-doctrine/viginum-doctrine-opencti-fr-annotee.md`, `viginum-moi-definitions-fr-annotee.md`, and every document of `seed_data/knowledge_base/viginum-fimi-publications/` |
| Upstream | <https://www.sgdsn.gouv.fr/viginum/publications> and <https://github.com/VIGINUM-FR/Doctrine-OpenCTI> |
| Snapshot | Publications listed on 4 November 2026 (21 publications, French versions) |
| Terms | SGDSN content is protected by copyright and is reused here with the SGDSN's agreement. Each document opens with the required mention, « source : site internet du secrétariat général de la défense et de la sécurité nationale », and links to the original publication and PDF. |
| Changes | The text of each PDF is kept as extracted. Each figure (diagram, graph, screenshot, chart, map, table image) is followed by a description written from the image, marked as a `Figure` block and ending with « Ce que la figure enseigne ». Pages without a text layer are transcribed. These blocks are descriptions, not VIGINUM text. |

`viginum-fimi-doctrine/sgdsn-viginum-opencti-fimi-methodology.md` is an English operational summary of the OpenCTI doctrine and of the MOI note, written for XTM One; it cites both.

## DISARM framework, French translation by VIGINUM

| | |
| --- | --- |
| Used in | `seed_data/knowledge_base/viginum-fimi-doctrine/viginum-disarm-fr-referentiel-bilingue.md` |
| Upstream | <https://github.com/VIGINUM-FR/DISARM-FR> (`traduction_disarm.xlsx`, sheet "Disarm V1.3"), translating the DISARM framework of the DISARM Foundation (<https://www.disarm.foundation/>) |
| Licence | CC-BY-4.0 (<https://creativecommons.org/licenses/by/4.0/>), as stated by the upstream repository for the original framework and its translation |
| Changes | Converted from the spreadsheet matrix to Markdown, one entry per tactic, technique and sub-technique, French and English side by side; identifiers taken from the English column, French-column discrepancies flagged. |

## FIMI skills

The FIMI skills (`app/agents/fimi_skills.py`, bodies in `seed_data/skill_content/fimi-*.md`) are written for XTM One. Each procedure step cites the doctrine section or the VIGINUM report it is drawn from; they quote no more than short phrases of those documents.
