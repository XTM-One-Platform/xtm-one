"""Regenerate ``app/services/stix_extraction/data/opencti_geography.json`` and
``opencti_sectors.json``: the country and region names of OpenCTI's geography dataset and
its sectors with their aliases (OpenCTI-Platform/datasets, ``data/geography.json`` and
``data/sectors.json``, Apache-2.0).

A location's standard id is its name and type: the extraction names a country the way
OpenCTI's dataset does ("Russian Federation", not "Russia"), so it merges into the
platform's object instead of creating a second one. Refresh when the dataset changes::

    git clone --depth 1 https://github.com/OpenCTI-Platform/datasets.git /tmp/datasets
    uv run --no-sync python scripts/stix_opencti_geography.py /tmp/datasets
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    checkout = Path(sys.argv[1])
    objects = json.loads((checkout / "data/geography.json").read_text())["objects"]
    countries = {}
    for obj in objects:
        if obj.get("type") == "location" and obj.get("x_opencti_location_type") == "Country":
            codes = [a for a in obj.get("x_opencti_aliases") or [] if len(a) == 2 and a.isupper()]
            if codes:
                countries[codes[0]] = {"name": obj["name"], "alpha3": obj.get("country", "")}
    regions = sorted(o["name"] for o in objects if o.get("x_opencti_location_type") == "Region")
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = Path(__file__).resolve().parents[1] / "app/services/stix_extraction/data/opencti_geography.json"
    out.write_text(json.dumps({
        "source": f"OpenCTI-Platform/datasets@{commit} data/geography.json (Apache-2.0)",
        "countries": dict(sorted(countries.items())),
        "regions": regions,
    }, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(countries)} countries, {len(regions)} regions written to {out}")
    sectors = {}
    for obj in json.loads((checkout / "data/sectors.json").read_text())["objects"]:
        if obj.get("type") == "identity" and obj.get("identity_class") == "class" and obj.get("name") != "Filigran":
            sectors[obj["name"]] = sorted(set(obj.get("x_opencti_aliases") or []) - {obj["name"]})
    sectors_out = out.with_name("opencti_sectors.json")
    sectors_out.write_text(json.dumps({
        "source": f"OpenCTI-Platform/datasets@{commit} data/sectors.json (Apache-2.0)",
        "sectors": dict(sorted(sectors.items())),
    }, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(sectors)} sectors written to {sectors_out}")


if __name__ == "__main__":
    main()
