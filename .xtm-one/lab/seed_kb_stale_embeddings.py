"""The lab's knowledge bases with stale stored embeddings, so the stale embeddings notice renders on real rows.

Started by ``lab.py`` (``seed_kb_stale_embeddings``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers (#10251). A knowledge base is stale when
``_load_kb_stale_embedding_info`` finds stored embeddings of a ``ready`` document whose model or
effective dimensions differ from the platform target. No API writes such a row (only a change of the
target after ingestion leaves one), so this writes, through the backend's models, :data:`COUNT`
knowledge bases of the administrator, idempotent by name, each with one ``ready`` document and its
chunks stored under a legacy model or dimension:

* every other one carries two stale tuples (two legacy models), the rest one;
* one has a name over 100 characters (:data:`LONG_NAME`);
* one is package-origin: a lab skill and its ``reference`` origin link, so "Re-embed all" skips it.

The platform target is kept when it is set; when it is not, it is set to :data:`LAB_TARGET` (the
stale count needs one). Arguments: the administrator's user id and the JSON file the answer is
written to, ``{"knowledge_bases": [ids], "package_origin": id, "target": [model, dims]}``.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

#: How many stale knowledge bases the seed writes: more than one page of the debug table (10 rows).
COUNT = 14
#: The name prefix of every knowledge base this seed writes (idempotence key with the index).
NAME_PREFIX = "Lab stale embeddings"
#: The one name over 100 characters, so the truncation of the debug table renders.
LONG_NAME = (
    f"{NAME_PREFIX} 01: threat intelligence reports archive with a very long name that keeps going past "
    "one hundred characters"
)
#: The index (0-based) of the package-origin knowledge base.
PACKAGE_INDEX = COUNT - 1
#: The lab skill the package-origin knowledge base hangs off.
PACKAGE_SKILL_NAME = "Lab stale embeddings package"
#: The legacy tuples the stored chunks carry: ``(model, dimensions)``, never the platform target.
LEGACY = (("lab-legacy-embedding-v1", 384), ("lab-legacy-embedding-v2", 768))
#: The platform target written only when none is set.
LAB_TARGET = ("lab-embedding-target", 1024)


def plan(target: tuple[str, int]) -> list[dict]:
    """The knowledge bases to write against the platform *target*: name, package flag and stale tuples.

    Each tuple is ``(model, dimensions, chunks)``; a legacy tuple equal to *target* is moved off it, so
    every planned row is stale whatever the lab's target.
    """
    legacy = [tuple(t) if tuple(t) != tuple(target) else (f"{t[0]}-old", t[1] + 1) for t in LEGACY]
    rows = []
    for index in range(COUNT):
        name = LONG_NAME if index == 0 else f"{NAME_PREFIX} {index + 1:02d}"
        tuples = [(legacy[0][0], legacy[0][1], 2 + index % 4)]
        if index % 2:
            tuples.append((legacy[1][0], legacy[1][1], 1 + index % 3))
        rows.append({"name": name, "package": index == PACKAGE_INDEX, "tuples": tuples})
    return rows


def unit_vector(dims: int, axis: int) -> list[float]:
    """A unit vector of *dims* on *axis* (modulo *dims*): never zero-norm, which cosine indexes refuse."""
    vector = [0.0] * dims
    vector[axis % dims] = 1.0
    return vector


def is_stale(row: dict, target: tuple[str, int]) -> bool:
    """Whether *row* has a tuple ``_load_kb_stale_embedding_info`` reads as stale against *target*."""
    return any((model, dims) != tuple(target) for model, dims, _ in row["tuples"])


async def seed(admin_id: str) -> dict:
    """Write the knowledge bases; answer their ids, the package-origin one and the target."""
    from sqlalchemy import delete, select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.database import async_session, engine
    from app.models.agent_skill import AgentSkill
    from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
    from app.models.embedding import Embedding
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    admin = uuid.UUID(admin_id)
    async with async_session() as db:
        ps = await get_platform_settings(db)
        if not ps.embedding_model or ps.embedding_dimensions is None:
            ps.embedding_model, ps.embedding_dimensions = LAB_TARGET
        target = (ps.embedding_model, ps.embedding_dimensions)
        ids: list[str] = []
        package_kb = ""
        for row in plan(target):
            kb = (await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == row["name"], KnowledgeBase.user_id == admin)
            )).scalars().first()
            if kb is None:
                kb = KnowledgeBase(user_id=admin, name=row["name"], description="Seeded by the lab: stale embeddings.")
                db.add(kb)
                await db.flush()
            kb.embedding_model = row["tuples"][0][0]
            doc = (await db.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb.id)
            )).scalars().first()
            if doc is None:
                doc = KnowledgeDocument(knowledge_base_id=kb.id, filename="lab-stale.md", source_type="markdown")
                db.add(doc)
                await db.flush()
            chunks = sum(n for _, _, n in row["tuples"])
            doc.status, doc.chunk_count, doc.content = "ready", chunks, "Seeded by the lab: stale embeddings."
            await db.execute(delete(Embedding).where(Embedding.knowledge_base_id == kb.id))
            for model, dims, n in row["tuples"]:
                for chunk in range(n):
                    db.add(Embedding(
                        user_id=admin, knowledge_base_id=kb.id, source_type="document", source_id=str(doc.id),
                        embedding_model=model, embedding_dimensions=dims,
                        content=f"Lab stale chunk {chunk + 1} ({model}).", embedding=unit_vector(dims, chunk),
                    ))
            kb.document_count, kb.chunk_count = 1, chunks
            if row["package"]:
                skill = (await db.execute(
                    select(AgentSkill).where(AgentSkill.name == PACKAGE_SKILL_NAME, AgentSkill.user_id == admin)
                )).scalars().first()
                if skill is None:
                    skill = AgentSkill(user_id=admin, name=PACKAGE_SKILL_NAME,
                                       description="Seeded by the lab: owns a package-origin knowledge base.",
                                       content="Lab skill.")
                    db.add(skill)
                    await db.flush()
                origin = (await db.execute(
                    select(AgentSkillPackageOrigin).where(AgentSkillPackageOrigin.skill_id == skill.id)
                )).scalars().first()
                if origin is None:
                    db.add(AgentSkillPackageOrigin(
                        skill_id=skill.id, resource_kind="reference", resource_path="references/lab-stale.md",
                        knowledge_base_id=kb.id, knowledge_document_id=doc.id,
                    ))
                package_kb = str(kb.id)
            ids.append(str(kb.id))
        await db.commit()
    await engine.dispose()
    return {"knowledge_bases": ids, "package_origin": package_kb, "target": list(target)}


def main() -> None:
    admin_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id))))


if __name__ == "__main__":
    main()
