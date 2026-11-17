"""Measure the Nova semantics: calibration of one embedding model.

The community rules' semantic thresholds were tuned for all-MiniLM-L6-v2.
This embeds the labelled set (``app/services/nova_calibration_set.json``) with
the platform's embedding provider - the configured model, or ``--model`` - and
prints the ``SemanticCalibration`` entry to add to
``app/services/nova_calibration.CALIBRATIONS`` for that model, with the set
version it was measured on.

Run from ``backend/`` against a deployment with an embedding provider
configured (it reads the provider from the database, like the engine does, and
never prints a credential):

    python scripts/calibrate_nova_semantics.py
    python scripts/calibrate_nova_semantics.py --model text-embedding-004
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Support ``python scripts/calibrate_nova_semantics.py`` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def separation(matching: list[float], non_matching: list[float]) -> float:
    """How often a matching text outscores a non-matching one (ROC AUC, 0.5 = chance)."""
    wins = sum(
        1.0 if m > n else 0.5 if m == n else 0.0 for m in matching for n in non_matching
    )
    return wins / (len(matching) * len(non_matching))


async def _measure(model_override: str | None) -> None:
    from app.core.database import async_session
    from app.llm.router import get_embedding_provider
    from app.services.nova_calibration import (
        calibration_set_version,
        derive_calibration,
        load_calibration_set,
    )
    from app.services.nova_engine import _cosine
    from app.services.persistent_memory import embed_text_with_model

    entries = load_calibration_set()
    async with async_session() as db:
        provider, default_model, _provider_id = await get_embedding_provider(db)
        model = model_override or default_model
        pair = (provider, model)
        vectors: dict[str, list[float]] = {}

        async def embed(text: str) -> list[float]:
            if text not in vectors:
                vector, _ = await embed_text_with_model(db, text, _provider_model=pair)
                if not vector:
                    raise SystemExit(f"{model} returned no embedding for: {text[:60]}")
                vectors[text] = vector
            return vectors[text]

        matching: list[float] = []
        non_matching: list[float] = []
        for entry in entries:
            pattern = await embed(entry["pattern"])
            for text in entry["matching"]:
                matching.append(_cosine(pattern, await embed(text)))
            for text in entry["non_matching"]:
                non_matching.append(_cosine(pattern, await embed(text)))

    dims = len(next(iter(vectors.values())))
    calibration = derive_calibration(
        model, non_matching, matching, set_version=calibration_set_version(), dimensions=dims
    )
    print(
        f"# {model}: {len(matching)} matching / {len(non_matching)} non-matching texts, "
        f"{dims} dimensions, set {calibration.set_version}; baseline {calibration.baseline}, "
        f"span {calibration.span}, separation {separation(matching, non_matching):.4f}"
    )
    print(
        f'"{calibration.model}": SemanticCalibration(model="{calibration.model}", '
        f"baseline={calibration.baseline}, span={calibration.span}, "
        f'samples={calibration.samples}, set_version="{calibration.set_version}", '
        f"dimensions={calibration.dimensions}),"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="Embedding model to measure (default: the platform's)")
    args = parser.parse_args()
    asyncio.run(_measure(args.model))


if __name__ == "__main__":
    main()
