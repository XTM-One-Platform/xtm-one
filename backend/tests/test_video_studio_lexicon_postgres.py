"""The platform's pronunciation lexicon, versioned in PostgreSQL and behind the REST routes.

* the seed writes the first version from the data file once, and never again -
  but for a seeded term a version holds without its IPA, given back once in a
  version of its own;
* every user reads the newest version (the editor lists it), an ordinary user
  never writes it: saving, listing the versions and restoring are an
  administrator's (rule 19: the refusals are tested as a non-admin);
* a save adds a version, entries validated one by one (one per written form),
  audited; a restore adds a version holding an older one's entries;
* a video's own entries override the newest version for the same written form.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import video_studio
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.video_studio import VideoPronunciationLexicon
from app.services.video_studio import lexicon_store
from app.services.video_studio.spec import Voiceover
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_BASE = "/api/v1/video-studio"


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(video_studio.router, prefix=_BASE)

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def test_the_lexicon_is_seeded_once_read_by_all_and_versioned_by_an_administrator() -> None:
    async with scratch_database(
        "video_lexicon", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            member = User(email="member@example.com", hashed_password="x", full_name="Member", role="user")
            db.add_all([admin, member])
            await db.commit()
            assert await lexicon_store.seed(db) is True
        async with scratch.sessions() as db:
            assert await lexicon_store.seed(db) is False
            assert await db.scalar(select(func.count()).select_from(VideoPronunciationLexicon)) == 1
            seeded = await lexicon_store.current(db)
        assert seeded.version == 1 and seeded.note == lexicon_store.SEED_NOTE
        assert [e.text for e in seeded.entries][:3] == ["Filigran", "CTEM", "XTM"]

        # An ordinary user reads it, never writes it.
        async with _client(scratch.sessions, member) as client:
            read = await client.get(f"{_BASE}/pronunciation-lexicon")
            assert read.status_code == 200 and read.json()["version"] == 1 and read.json()["can_edit"] is False
            entries = read.json()["entries"]
            assert (await client.put(f"{_BASE}/pronunciation-lexicon", json={"entries": entries})).status_code == 403
            assert (await client.get(f"{_BASE}/pronunciation-lexicon/versions")).status_code == 403
            assert (await client.post(f"{_BASE}/pronunciation-lexicon/versions/1/restore")).status_code == 403

        async with _client(scratch.sessions, admin) as client:
            added = [*entries, {"text": "SOAR", "say": "soar", "match": "case_insensitive"}]
            saved = await client.put(f"{_BASE}/pronunciation-lexicon", json={"entries": added, "note": "Add SOAR."})
            assert saved.status_code == 200, saved.text
            assert saved.json()["version"] == 2 and saved.json()["entries"][-1]["text"] == "SOAR"
            bad = await client.put(f"{_BASE}/pronunciation-lexicon", json={"entries": [{"text": "X", "say": "x", "match": "anywhere"}]})
            assert bad.status_code == 422 and "Entry 1 (match)" in bad.json()["detail"]
            twice = await client.put(
                f"{_BASE}/pronunciation-lexicon",
                json={"entries": [{"text": "THREAD", "say": "thread", "match": "case_insensitive"}, {"text": "Thread", "say": "t"}]},
            )
            assert twice.status_code == 422 and 'both write "Thread"' in twice.json()["detail"]
            versions = (await client.get(f"{_BASE}/pronunciation-lexicon/versions")).json()["items"]
            assert [(v["version"], v["entries"], v["created_by"]) for v in versions] == [
                (2, len(added), "admin@example.com"),
                (1, len(entries), "system"),
            ]
            restored = await client.post(f"{_BASE}/pronunciation-lexicon/versions/1/restore")
            assert restored.status_code == 200 and restored.json()["version"] == 3
            assert restored.json()["entries"] == entries and restored.json()["note"] == "Restored version 1."
            assert (await client.post(f"{_BASE}/pronunciation-lexicon/versions/9/restore")).status_code == 404

        async with scratch.sessions() as db:
            audited = await db.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "video_pronunciation_lexicon")
            )
            assert audited == 2
            # The newest version under a video's own entries.
            merged = await lexicon_store.for_voiceover(db, Voiceover(pronunciations=[{"text": "CTEM", "say": "see-tem-program"}]))
        assert merged.version == 3
        assert [e.say for e in merged.entries if e.text == "CTEM"] == ["see-tem-program"]
        assert "SOAR" not in {e.text for e in merged.entries}

        # A version reading Filigran as the bare word, without its IPA: the next startup gives it back.
        async with scratch.sessions() as db:
            bare = [{**e, "say": "Filigran"} if e["text"] == "Filigran" else e for e in entries]
            bare = [{k: v for k, v in e.items() if not (e["text"] == "Filigran" and k == "ipa")} for e in bare]
            await lexicon_store.save(db, bare, user=admin, note="Bare Filigran.")
            await db.commit()
        async with scratch.sessions() as db:
            assert await lexicon_store.seed(db) is True
        async with scratch.sessions() as db:
            assert await lexicon_store.seed(db) is False
            given = await lexicon_store.current(db)
        filigran = next(e for e in given.entries if e.text == "Filigran")
        assert given.version == 5 and given.created_by == "system" and "Filigran" in (given.note or "")
        assert (filigran.say, filigran.ipa, filigran.apply_to) == ("Fil-i-gran", "ˈfɪlɪɡræn", "voice")
        assert [e.text for e in given.entries] == [e["text"] for e in entries]
