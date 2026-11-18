"""SCIM 2.0 request parsing (``services/scim.py``), on the request shapes
Microsoft Entra sends (#122)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import scim as s


def test_filters_read_eq_clauses_and_refuse_the_rest() -> None:
    assert s.parse_filter('userName eq "Ana@Example.com"', {"username"}) == {"username": "Ana@Example.com"}
    assert s.parse_filter('externalId eq "abc" and userName eq "x"', {"username", "externalid"}) == {
        "externalid": "abc",
        "username": "x",
    }
    assert s.parse_filter('members[value eq "u1"]', {"members.value"}) == {"members.value": "u1"}
    assert s.parse_filter(None, {"username"}) == {}
    for bad in ('userName co "a"', 'title eq "x"', "userName eq x", 'userName eq "a" or userName eq "b"'):
        with pytest.raises(s.ScimError) as exc:
            s.parse_filter(bad, {"username"})
        assert exc.value.status == 400 and exc.value.scim_type == "invalidFilter"


def test_paging_is_one_based_and_capped() -> None:
    assert s.page(None, None) == (0, s.DEFAULT_COUNT)
    assert s.page(11, 5) == (10, 5)
    assert s.page(0, 10_000) == (0, s.MAX_COUNT)
    assert s.page(1, 0) == (0, 0)


def test_entra_booleans_come_as_strings() -> None:
    assert s.as_bool("False") is False
    assert s.as_bool("True") is True
    assert s.as_bool(True) is True
    with pytest.raises(s.ScimError):
        s.as_bool("maybe")


def test_entra_create_body_maps_to_the_account_fields() -> None:
    body = {
        "schemas": [s.USER_SCHEMA, s.ENTERPRISE_SCHEMA],
        "externalId": "0a21f0f2",
        "userName": "Test.User@example.com",
        "active": True,
        "displayName": "Test User",
        "emails": [{"primary": True, "type": "work", "value": "Test.User@example.com"}],
        "name": {"formatted": "Test User", "familyName": "User", "givenName": "Test"},
        "title": "Analyst",
        s.ENTERPRISE_SCHEMA: {"department": "Security", "manager": {"value": "m1"}},
        "phoneNumbers": [{"type": "work", "value": "+33"}],
    }
    changes = s.user_changes_from_resource(body)
    assert changes.user_name == "Test.User@example.com"
    assert changes.external_id == "0a21f0f2"
    assert changes.display_name() == "Test User"
    assert changes.title == "Analyst"
    assert changes.department == "Security"
    assert changes.active is True


def test_entra_patch_replace_without_path_and_capitalised_op() -> None:
    body = {
        "schemas": [s.PATCH_SCHEMA],
        "Operations": [
            {"op": "Replace", "value": {"active": "False"}},
            {"op": "replace", "path": "displayName", "value": "Renamed"},
            {
                "op": "Add",
                "path": f"{s.ENTERPRISE_SCHEMA}:department",
                "value": "Finance",
            },
            {"op": "replace", "path": 'emails[type eq "work"].value', "value": "new@example.com"},
            {"op": "Remove", "path": "title"},
        ],
    }
    changes = s.user_changes_from_patch(body)
    assert changes.active is False
    assert changes.full_name == "Renamed"
    assert changes.department == "Finance"
    assert changes.user_name == "new@example.com"
    assert changes.title_set and changes.title is None


def test_patch_of_one_name_part_keeps_the_stored_name() -> None:
    # Entra PATCHes only the attribute that changed: a surname alone never
    # overwrites the stored display name with that part.
    body = {
        "schemas": [s.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "name.familyName", "value": "Dupont"}],
    }
    changes = s.user_changes_from_patch(body)
    assert changes.display_name("Ana Martin") == "Ana Martin"
    # With no stored name yet (a create), the one part names the account.
    assert changes.display_name(None) == "Dupont"
    # Both parts, or displayName, still rename it.
    both = s.user_changes_from_patch(
        {"Operations": [{"op": "replace", "value": {"name.givenName": "Ana", "name.familyName": "Dupont"}}]}
    )
    assert both.display_name("Ana Martin") == "Ana Dupont"
    shown = s.user_changes_from_patch({"Operations": [{"op": "replace", "path": "displayName", "value": "Ana D."}]})
    assert shown.display_name("Ana Martin") == "Ana D."


def test_patch_refuses_an_unknown_operation() -> None:
    with pytest.raises(s.ScimError) as exc:
        s.user_changes_from_patch({"Operations": [{"op": "move", "path": "title"}]})
    assert exc.value.status == 400
    with pytest.raises(s.ScimError):
        s.user_changes_from_patch({"schemas": [s.PATCH_SCHEMA]})


def test_group_patch_member_add_and_remove_shapes() -> None:
    changes = s.group_changes_from_patch(
        {
            "Operations": [
                {"op": "Add", "path": "members", "value": [{"value": "u1"}, {"value": "u2"}]},
                {"op": "Remove", "path": 'members[value eq "u3"]'},
                {"op": "Remove", "path": "members", "value": [{"value": "u4"}]},
                {"op": "Replace", "path": "displayName", "value": "SOC"},
            ]
        }
    )
    assert changes.add == ["u1", "u2"]
    assert changes.remove == ["u3", "u4"]
    assert changes.display_name == "SOC"
    assert changes.replace is None
    replaced = s.group_changes_from_patch({"Operations": [{"op": "replace", "path": "members", "value": []}]})
    assert replaced.replace == []


def test_resources_carry_the_scim_shapes() -> None:
    now = datetime.now(UTC)
    user = SimpleNamespace(
        id="u1",
        email="a@example.com",
        is_active=False,
        full_name="Ana",
        created_at=now,
        updated_at=now,
        scim_external_id="ext",
        title=None,
        directory_department="Security",
    )
    resource = s.user_resource(user, "https://x/api/v1/scim/v2")
    assert resource["active"] is False
    assert resource["userName"] == "a@example.com"
    assert resource["externalId"] == "ext"
    assert resource[s.ENTERPRISE_SCHEMA] == {"department": "Security"}
    assert resource["meta"]["location"] == "https://x/api/v1/scim/v2/Users/u1"
    group = SimpleNamespace(id="g1", name="SOC", created_at=now, updated_at=None, scim_external_id=None)
    out = s.group_resource(group, [("u1", "Ana")], "https://x/api/v1/scim/v2")
    assert out["members"][0]["value"] == "u1"
    assert "externalId" not in out
    listing = s.list_response([resource], 3, 1)
    assert listing["totalResults"] == 3 and listing["itemsPerPage"] == 1


def test_discovery_documents_name_both_resources() -> None:
    base = "https://x/api/v1/scim/v2"
    assert s.service_provider_config(base)["patch"]["supported"] is True
    assert {r["id"] for r in s.resource_types(base)} == {"User", "Group"}
    assert {d["id"] for d in s.schemas(base)} == {s.USER_SCHEMA, s.GROUP_SCHEMA, s.ENTERPRISE_SCHEMA}
    assert s.ScimError(404, "x").body()["schemas"] == [s.ERROR_SCHEMA]
