"""Regenerate ``app/services/stix_extraction/data/misp_galaxy_aliases.json``: the names and
synonyms of the MISP galaxy's threat actors, ransomware and tools (MISP/misp-galaxy,
``clusters/threat-actor.json``, ``ransomware.json``, ``tool.json``; CC0 1.0 or BSD-2-Clause,
the MISP project's own clusters).

Two names of one cluster in a report ("Secret Blizzard" and "Turla") are one object of the
extraction, and the cluster's name is an alias OpenCTI can match on. Refresh when the galaxy
changes::

    git clone --depth 1 https://github.com/MISP/misp-galaxy.git /tmp/misp-galaxy
    uv run --no-sync python scripts/stix_misp_galaxy_aliases.py /tmp/misp-galaxy
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CLUSTERS = {"threat-actor": "actor", "ransomware": "ransomware", "tool": "tool"}


def main() -> None:
    checkout = Path(sys.argv[1])
    entries = []
    for cluster, kind in CLUSTERS.items():
        for value in json.loads((checkout / "clusters" / f"{cluster}.json").read_text())["values"]:
            name = " ".join(str(value.get("value") or "").split())
            synonyms = sorted({" ".join(str(s).split()) for s in (value.get("meta") or {}).get("synonyms") or []} - {name, ""})
            if name and synonyms:
                entries.append([kind, name, synonyms])
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = Path(__file__).resolve().parents[1] / "app/services/stix_extraction/data/misp_galaxy_aliases.json"
    out.write_text(json.dumps({
        "source": f"MISP/misp-galaxy@{commit} clusters/threat-actor.json, ransomware.json, tool.json (CC0 1.0 or BSD-2-Clause)",
        "clusters": sorted(entries),
    }, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(entries)} clusters with synonyms written to {out}")


if __name__ == "__main__":
    main()
