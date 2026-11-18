# Third-party data

The JSON files in this directory are derived from third-party datasets. They are not part of the
XTM One Enterprise Edition: each keeps its owner's license, whose text ships beside it.

| File | Upstream | Pinned commit | Upstream files | License |
| --- | --- | --- | --- | --- |
| `misp_galaxy_aliases.json` | [MISP/misp-galaxy](https://github.com/MISP/misp-galaxy) | `47fdb8058f548b285849ab1a07378c7fe804c73b` | `clusters/threat-actor.json`, `ransomware.json`, `tool.json` | CC0 1.0, or BSD-2-Clause ([`LICENSE-misp-galaxy.md`](LICENSE-misp-galaxy.md)) |
| `opencti_geography.json` | [OpenCTI-Platform/datasets](https://github.com/OpenCTI-Platform/datasets) | `709e73e4d28688fe89df94daf99d4204a83d7404` | `data/geography.json` | Apache-2.0 ([`LICENSE-opencti-datasets.txt`](LICENSE-opencti-datasets.txt)) |
| `opencti_sectors.json` | [OpenCTI-Platform/datasets](https://github.com/OpenCTI-Platform/datasets) | `709e73e4d28688fe89df94daf99d4204a83d7404` | `data/sectors.json` | Apache-2.0 ([`LICENSE-opencti-datasets.txt`](LICENSE-opencti-datasets.txt)) |

## Changes made

- `misp_galaxy_aliases.json`: only each cluster's kind, name and synonyms are kept, from the three
  cluster files above (`backend/scripts/stix_misp_galaxy_aliases.py` regenerates it).
- `opencti_geography.json`: only each country's name and ISO 3166-1 alpha-3 code, and the regions'
  names, are kept (`backend/scripts/stix_opencti_geography.py` regenerates it).
- `opencti_sectors.json`: only each sector's name and the names it groups are kept.

Each file records its upstream and commit in its own `source` field. Refreshing one is a
deliberate commit: a regeneration from a newer pinned commit, with this table updated.
