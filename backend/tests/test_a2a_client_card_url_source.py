"""Where the agents an operator typed become the agents we can reach.

How a saved list of card URLs becomes the set of target rows without
discarding what was already verified about the ones that did not change.

Reconciled, never replaced. Rewriting the set on every save would drop the
verdict, the pinned origin and the accepted-card fingerprint of every target
the operator did not touch — so correcting a typo in one URL would silently
re-open the card-change gate on all the others.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.a2a.client.credentials import configured_card_urls
from app.a2a.providers import reconcile_configured_targets, sync_targets


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Db:
    """Just enough session for the reconciler: read, add, delete, flush."""

    def __init__(self, existing):
        self.rows = list(existing)
        self.added: list = []
        self.deleted: list = []

    async def execute(self, _stmt):
        return _Result(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None


def _existing(*urls):
    return [SimpleNamespace(card_url=u, usability="usable") for u in urls]


class TestTheOperatorsListBecomesTheTargets:
    async def test_a_new_url_becomes_a_target(self):
        db = _Db([])
        added, removed = await sync_targets(db, "i-1", ["https://a.example/c.json"])
        assert (added, removed) == (1, 0)
        assert [t.card_url for t in db.added] == ["https://a.example/c.json"]

    async def test_several_agents_of_one_partner(self):
        # The whole point: one credential, one registration, three reachable
        # agents.
        db = _Db([])
        urls = [f"https://p.example/{n}/card.json" for n in ("a", "b", "c")]
        added, _ = await sync_targets(db, "i-1", urls)
        assert added == 3
        assert [t.card_url for t in db.added] == urls

    async def test_an_unchanged_url_keeps_its_row(self):
        # The reason this reconciles rather than replaces: the untouched
        # target keeps its verdict, its pin and its accepted fingerprint.
        db = _Db(_existing("https://a.example/c.json"))
        added, removed = await sync_targets(
            db, "i-1", ["https://a.example/c.json", "https://b.example/c.json"]
        )
        assert (added, removed) == (1, 0)
        assert db.deleted == []
        assert [t.card_url for t in db.added] == ["https://b.example/c.json"]

    async def test_a_removed_url_takes_its_target_with_it(self):
        # Immediately, not at the next sweep: a target an operator removed
        # must stop contributing a tool now.
        db = _Db(_existing("https://a.example/c.json", "https://b.example/c.json"))
        added, removed = await sync_targets(db, "i-1", ["https://a.example/c.json"])
        assert (added, removed) == (0, 1)
        assert [t.card_url for t in db.deleted] == ["https://b.example/c.json"]

    async def test_a_new_target_does_not_inherit_a_siblings_verdict(self):
        db = _Db(_existing("https://a.example/c.json"))
        await sync_targets(
            db, "i-1", ["https://a.example/c.json", "https://b.example/c.json"]
        )
        # Whatever the sibling proved is about the sibling. Asserting anything
        # about an agent nobody has looked at is the failure this guards.
        assert db.added[0].usability != "usable"



A = "https://p.example/a/card.json"
B = "https://p.example/b/card.json"
C = "https://p.example/c/card.json"
D = "https://p.example/d/card.json"


class TestASaveAppliesOnlyTheOperatorsEdit:
    """The table also holds the agents discovery added; a save must not
    delete them."""

    async def test_a_discovered_target_survives_a_save_that_did_not_touch_it(self):
        # The operator typed A; the partner listed B.
        db = _Db(_existing(A, B))
        added, removed = await reconcile_configured_targets(
            db, "i-1", previous=[A], current=[A]
        )
        assert (added, removed) == (0, 0)
        assert db.deleted == []

    async def test_a_url_the_operator_removed_takes_its_row_with_it(self):
        db = _Db(_existing(A, B, C))
        added, removed = await reconcile_configured_targets(
            db, "i-1", previous=[A, C], current=[A]
        )
        assert (added, removed) == (0, 1)
        assert [t.card_url for t in db.deleted] == [C]

    async def test_a_url_the_operator_added_arrives_unverified(self):
        db = _Db(_existing(A, B))
        added, removed = await reconcile_configured_targets(
            db, "i-1", previous=[A], current=[A, D]
        )
        assert (added, removed) == (1, 0)
        assert [t.card_url for t in db.added] == [D]
        assert db.deleted == []

    async def test_removing_and_re_adding_in_one_save_is_a_no_op(self):
        # Present before and after: not an edit.
        db = _Db(_existing(A, B))
        added, removed = await reconcile_configured_targets(
            db, "i-1", previous=[A, B], current=[B, A]
        )
        assert (added, removed) == (0, 0)


class TestTheOperatorsListIsParsed:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://a/c.json", ["https://a/c.json"]),
            ("https://a/c.json,https://b/c.json", ["https://a/c.json", "https://b/c.json"]),
            ("https://a/c.json\nhttps://b/c.json", ["https://a/c.json", "https://b/c.json"]),
            ("  https://a/c.json ,  ", ["https://a/c.json"]),
            # A paste-twice is a typo, not something to refuse a save over.
            ("https://a/c.json,https://a/c.json", ["https://a/c.json"]),
            ("", []),
        ],
    )
    def test_the_form_carries_a_list_without_a_bespoke_widget(
        self, monkeypatch, raw, expected
    ):
        import app.a2a.client.credentials as credentials

        monkeypatch.setattr(credentials, "_stored_config", lambda _i: {"card_urls": raw})
        assert configured_card_urls(object()) == expected

    def test_a_registration_configured_before_targets_is_not_emptied(
        self, monkeypatch
    ):
        # The singular key is still read, so an existing registration keeps
        # its agent on its next save rather than losing it silently.
        import app.a2a.client.credentials as credentials

        monkeypatch.setattr(credentials, "_stored_config", lambda _i: {"card_url": "https://legacy/c.json"})
        assert configured_card_urls(object()) == ["https://legacy/c.json"]
