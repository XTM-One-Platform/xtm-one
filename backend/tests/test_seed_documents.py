import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import seed
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument


class _MockScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_seed_updates_preserve_document_count(tmp_path, monkeypatch):
    folder_name = "seed-fixtures"
    seed_folder = tmp_path / "knowledge_base" / folder_name
    seed_folder.mkdir(parents=True)

    existing_filename = "existing.md"
    new_filename = "new.md"
    updated_content = "updated content"
    new_content = "new content"
    (seed_folder / existing_filename).write_text(updated_content, encoding="utf-8")
    (seed_folder / new_filename).write_text(new_content, encoding="utf-8")

    kb_id = uuid.uuid4()
    kb = KnowledgeBase(id=kb_id, name="Seed KB", document_count=10)
    existing_doc = KnowledgeDocument(
        knowledge_base_id=kb_id,
        filename=existing_filename,
        source_type="text",
        content="old content",
        content_hash=hashlib.sha256(b"old content").hexdigest(),
        status="ready",
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        # Lookup, the changed document's embeddings dropped, lookup.
        side_effect=[_MockScalarResult(existing_doc), _MockScalarResult(None), _MockScalarResult(None)]
    )
    db.flush = AsyncMock()
    db.add = MagicMock()

    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)
    # The revision itself is tested on PostgreSQL (test_kb_revision_postgres.py).
    monkeypatch.setattr(seed, "bump_kb_revision", AsyncMock())

    changed = await seed._seed_documents_from_folder(db, kb, folder_name)

    assert changed == 2
    assert kb.document_count == 11

    assert existing_doc.content == updated_content
    assert existing_doc.content_hash == hashlib.sha256(
        updated_content.encode()
    ).hexdigest()
    assert existing_doc.status == "pending"
    # The processor keeps a document's vectors until it replaces them, so a
    # content change drops the old ones itself.
    dropped = db.execute.await_args_list[1].args[0]
    assert dropped.is_delete and dropped.table.name == "embeddings"

    db.add.assert_called_once()
    inserted_doc = db.add.call_args[0][0]
    assert isinstance(inserted_doc, KnowledgeDocument)
    assert inserted_doc.filename == new_filename
    assert inserted_doc.content == new_content

    db.flush.assert_awaited_once()


def test_sync_agent_fields_uses_persona_slug_file_when_available(tmp_path, monkeypatch):
    persona_slug = "custom-persona"
    persona_content = "persona from file"
    persona_dir = tmp_path / "agent_persona"
    persona_dir.mkdir()
    (persona_dir / f"{persona_slug}.md").write_text(persona_content, encoding="utf-8")

    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    agent = seed.Agent(name="Test Agent", slug="test-agent")
    data = {
        "slug": "test-agent",
        "persona": persona_slug,
    }

    seed._sync_agent_fields(agent, data)

    assert agent.persona == persona_content


def test_sync_agent_fields_uses_inline_persona_directly(tmp_path, monkeypatch):
    inline_persona = "You are a helpful assistant.\n\nDo great things."
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    agent = seed.Agent(name="Test Agent", slug="test-agent")
    data = {
        "slug": "test-agent",
        "persona": inline_persona,
    }

    seed._sync_agent_fields(agent, data)

    assert agent.persona == inline_persona


def test_resolve_seed_content_loads_file_for_slug(tmp_path, monkeypatch):
    slug = "my-skill"
    content = "# My Skill\n\nContent from file."
    skill_dir = tmp_path / "skill_content"
    skill_dir.mkdir()
    (skill_dir / f"{slug}.md").write_text(content, encoding="utf-8")

    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    assert seed._resolve_seed_content("skill_content", slug) == content


def test_resolve_seed_content_returns_inline_when_not_a_slug(tmp_path, monkeypatch):
    inline = "You are an AI.\n\nHelp the user."
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    assert seed._resolve_seed_content("skill_content", inline) == inline


def test_resolve_seed_content_returns_none_for_missing_slug_file(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    assert seed._resolve_seed_content("skill_content", "nonexistent-skill") is None


def test_resolve_seed_content_returns_none_for_none(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)

    assert seed._resolve_seed_content("skill_content", None) is None
