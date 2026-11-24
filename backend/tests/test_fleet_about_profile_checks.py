"""The ``profile:<field>`` prerequisite checks of a fleet's documentation (#9173, #1878).

The About page of a fleet checks each documented ``profile:<field>``
prerequisite against the saved organization profile. The map lives in
``fleet_about``; the Organization page no longer names who reads each part of
the profile (#1878), so no route serves it.
"""

from __future__ import annotations

from app.api.v1 import organization_context as organization_context_api
from app.services.fleet_about import PROFILE_CHECK_FIELDS
from app.services.fleet_docs import PREREQUISITE_CHECKS
from app.services.organization_context import OrganizationProfile


def test_every_profile_field_check_of_the_documentation_is_mapped_to_a_real_profile_field():
    profile_checks = {
        c for c in PREREQUISITE_CHECKS if c.startswith("profile:") and c != "profile:tracked_organizations"
    }
    assert profile_checks == set(PROFILE_CHECK_FIELDS)
    # #1844: no derived ai_systems field; profile:ai_targets reads the assets of category ai_target.
    fields = set(OrganizationProfile.model_fields)
    assert set(PROFILE_CHECK_FIELDS.values()) <= fields


def test_no_route_names_the_readers_of_the_profile():
    paths = {getattr(route, "path", "") for route in organization_context_api.router.routes}
    assert not any(path.endswith("/readers") for path in paths)
