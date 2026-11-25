# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Inside an A2A partner task, list_work_items lists that partner's own tickets."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement
from sqlalchemy.dialects import postgresql

from app.a2a.partner_runs import partner_fleet_work_items_clause, partner_work_items_clause
from app.a2a.partner_scope import PartnerPrincipal, current_partner, partner_task_scope


def _sql(clause: ColumnElement[bool]) -> str:
    pg = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(clause.compile(dialect=pg, compile_kwargs={"literal_binds": True}))


class TestPartnerWorkItemsClause:
    def test_scoped_by_exposure_and_client_through_the_run_tree(self) -> None:
        partner = PartnerPrincipal(exposure_id=uuid.UUID(int=7), client_id="ctem-orchestrator")
        sql = _sql(partner_work_items_clause(partner))
        assert "work_items.origin_run_id IN" in sql
        assert "a2a_tasks.exposure_id" in sql
        assert "'ctem-orchestrator'" in sql
        # Spawned runs (a consult that filed the ticket) are in the tree too.
        assert "parent_run_id" in sql

    def test_two_partners_never_share_a_clause(self) -> None:
        exposure = uuid.UUID(int=7)
        one = _sql(partner_work_items_clause(PartnerPrincipal(exposure_id=exposure, client_id="a")))
        two = _sql(partner_work_items_clause(PartnerPrincipal(exposure_id=exposure, client_id="b")))
        assert one != two

    def test_the_home_fleet_rides_only_behind_the_exposure_opt_in(self) -> None:
        """#2202: the fleet's items are added under the exposure's opt-in, never the run-as reach."""
        partner = PartnerPrincipal(exposure_id=uuid.UUID(int=7), client_id="ctem-orchestrator")
        sql = _sql(partner_work_items_clause(partner))
        assert "work_items.origin_run_id IN" in sql
        assert "a2a_exposures.partner_fleet_reads IS true" in sql
        assert "a2a_exposures.revoked_at IS NULL" in sql
        assert "fleet_members.role = 'member'" in sql
        assert "'00000000-0000-0000-0000-000000000007'" in sql

    def test_the_fleet_clause_is_keyed_by_the_exposure_not_the_client(self) -> None:
        exposure = uuid.UUID(int=7)
        one = _sql(partner_fleet_work_items_clause(PartnerPrincipal(exposure_id=exposure, client_id="a")))
        two = _sql(partner_fleet_work_items_clause(PartnerPrincipal(exposure_id=exposure, client_id="b")))
        # The fleet is the exposed agent's: two clients of one opted-in exposure read the same fleet.
        assert one == two
        assert "work_items.owner_agent_id IN" in one
        assert "work_items.created_by_agent_id IN" in one
        assert "work_items.workspace_id IN" in one

    def test_no_partner_outside_a_partner_task(self) -> None:
        assert current_partner() is None
        partner = PartnerPrincipal(exposure_id=uuid.UUID(int=1), client_id="c")
        with partner_task_scope(principal=partner):
            assert current_partner() == partner
        assert current_partner() is None
