# SPDX-License-Identifier: Apache-2.0

"""OpenAEV team + player management tools.

* ``manage_openaev_team``   - team CRUD/upsert + set members (``TeamApi``).
* ``manage_openaev_player`` - player (simulation user) CRUD/upsert
  (``PlayerApi``).
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_team(
        action: str = "",
        team_id: str = "",
        name: str = "",
        description: str = "",
        organization_id: str = "",
        tags: list[str] | None = None,
        contextual: bool | None = None,
        player_ids: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        def _team_body() -> dict:
            body: dict = {}
            if name:
                body["team_name"] = name
            if description:
                body["team_description"] = description
            if organization_id:
                body["team_organization"] = organization_id
            if tags is not None:
                body["team_tags"] = tags
            if contextual is not None:
                body["team_contextual"] = contextual
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/teams/search", "search teams",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No teams found."
            lines = [f"Teams ({len(items)}):"]
            for t in items:
                lines.append(f"- {t.get('team_name', '?')} (id: {t.get('team_id')})")
            return "\n".join(lines)

        if action == "get":
            if not team_id:
                return "team_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/teams/{team_id}", "get team"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action in ("create", "upsert"):
            if not name:
                return "name is required for 'create'/'upsert'."
            path = "/api/teams/upsert" if action == "upsert" else "/api/teams"
            resp, err = await _api(
                base_url, headers, "post", path, f"{action} team", json=_team_body()
            )
            if err:
                return err
            t = resp.json()
            verb = "Upserted" if action == "upsert" else "Created"
            return f"{verb} team '{t.get('team_name')}' (id: {t.get('team_id')})."

        if action == "update":
            if not team_id:
                return "team_id is required for action 'update'."
            body = _team_body()
            if not body:
                return "Provide at least one field to update."
            resp, err = await _api(
                base_url, headers, "put", f"/api/teams/{team_id}", "update team",
                json=body,
            )
            if err:
                return err
            return f"Updated team {team_id}."

        if action == "set_players":
            if not team_id or player_ids is None:
                return "team_id and player_ids are required for 'set_players'."
            _, err = await _api(
                base_url, headers, "put", f"/api/teams/{team_id}/players",
                "set team players", json={"team_users": player_ids},
            )
            if err:
                return err
            return f"Set {len(player_ids)} players on team {team_id}."

        if action == "delete":
            if not team_id:
                return "team_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/teams/{team_id}", "delete team"
            )
            if err:
                return err
            return f"Deleted team {team_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "upsert, set_players, delete."
        )

    async def manage_openaev_player(
        action: str = "",
        player_id: str = "",
        email: str = "",
        firstname: str = "",
        lastname: str = "",
        organization_id: str = "",
        tags: list[str] | None = None,
        team_ids: list[str] | None = None,
        phone: str = "",
        country: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        # The autonomous orchestrator is granted this tool ONLY to mint a person
        # (create - idempotent mint-or-resolve) so it can wrap them for delivery
        # through the gated ensure_openaev_target_team. It must never update,
        # delete, or upsert arbitrary OpenAEV users (upsert rewrites an existing
        # user matched by email - profile fields, tags, team memberships); refuse
        # those sub-actions in code (identity-keyed to the orchestrator's own
        # run), since a persona instruction is not an authorization boundary.
        # Chat / other agents keep the full CRUD contract.
        from app.services.builtin_tools.filtering import (
            is_orchestrator_forbidden_player_action,
            is_orchestrator_player_team_binding_forbidden,
        )

        if is_orchestrator_forbidden_player_action(action):
            return (
                f"Error: the Attack Path Orchestrator cannot '{action}' OpenAEV "
                "players itself. It may mint a person (action='create', which "
                "resolves an existing email or creates a new person, to obtain a "
                "player_id) or look one up (action='list' or "
                "search_openaev_players), then wrap them for delivery via the gated "
                "ensure_openaev_target_team - but it must not update, delete, or "
                "upsert OpenAEV users. If a person's details are wrong or a user "
                "should be removed, record a GAP for the operator instead."
            )

        # Even with create allowed, team_ids would be forwarded as user_teams to
        # /api/players and let the orchestrator hand-stitch membership in arbitrary
        # teams - bypassing the run-scoped ensure_openaev_target_team gate that is
        # the ONLY sanctioned way for it to bind a person to a delivery team. Refuse
        # team_ids here (before any REST call leaves), keyed to the orchestrator-run
        # marker; the gated tool wraps players by id and never routes through this
        # parameter, so the legitimate path is unaffected. Chat / other agents keep
        # the full contract.
        if is_orchestrator_player_team_binding_forbidden(team_ids):
            return (
                "Error: the Attack Path Orchestrator cannot set OpenAEV team "
                "membership through manage_openaev_player. Mint the person first "
                "(action='create' with no team_ids to obtain a player_id), then "
                "bind them to a delivery team ONLY through the gated "
                "ensure_openaev_target_team, which enforces the run's scope and the "
                "acting agent's discovery mode. Passing team_ids here would "
                "hand-stitch membership in arbitrary teams and bypass that gate."
            )

        def _player_body() -> dict:
            body: dict = {}
            if email:
                body["user_email"] = email
            if firstname:
                body["user_firstname"] = firstname
            if lastname:
                body["user_lastname"] = lastname
            if organization_id:
                body["user_organization"] = organization_id
            if tags is not None:
                body["user_tags"] = tags
            if team_ids is not None:
                body["user_teams"] = team_ids
            if phone:
                body["user_phone"] = phone
            if country:
                body["user_country"] = country
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/players/search", "search players",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No players found."
            lines = [f"Players ({len(items)}):"]
            for p in items:
                lines.append(
                    f"- {p.get('user_email', '?')} (id: {p.get('user_id')})"
                )
            return "\n".join(lines)

        if action in ("create", "upsert"):
            if not email:
                return "email is required for 'create'/'upsert'."
            path = "/api/players/upsert" if action == "upsert" else "/api/players"
            resp, err = await _api(
                base_url, headers, "post", path, f"{action} player",
                json=_player_body(),
            )
            if err:
                return err
            p = resp.json()
            verb = "Upserted" if action == "upsert" else "Created"
            return f"{verb} player '{p.get('user_email')}' (id: {p.get('user_id')})."

        if action == "update":
            if not player_id:
                return "player_id is required for action 'update'."
            body = _player_body()
            if not body:
                return "Provide at least one field to update."
            _, err = await _api(
                base_url, headers, "put", f"/api/players/{player_id}",
                "update player", json=body,
            )
            if err:
                return err
            return f"Updated player {player_id}."

        if action == "delete":
            if not player_id:
                return "player_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/players/{player_id}",
                "delete player",
            )
            if err:
                return err
            return f"Deleted player {player_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, create, update, upsert, "
            "delete. (Use search_openaev_players to find a player.)"
        )

    executor.register("manage_openaev_team", manage_openaev_team)
    executor.register("manage_openaev_player", manage_openaev_player)

    return [
        ToolDefinition(
            name="manage_openaev_team",
            description=(
                "Manage OpenAEV teams. Actions: 'list' (search), 'get' (team_id), "
                "'create' (name, optional description/organization_id/tags/contextual), "
                "'update' (team_id + fields), 'upsert' (name), 'set_players' (team_id "
                "+ player_ids to set the team membership), 'delete' (team_id). A "
                "contextual team exists only within the scenario/simulation it is "
                "linked to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "upsert", "set_players", "delete"],
                    },
                    "team_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "organization_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "contextual": {"type": "boolean", "description": "Whether the team is contextual (simulation-scoped). Omit on update to leave unchanged."},
                    "player_ids": {"type": "array", "items": {"type": "string"}, "description": "Player IDs (set_players)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_player",
            description=(
                "Manage OpenAEV players (the simulated users / targets). Actions: "
                "'list' (search), 'create' (email + optional firstname/lastname/"
                "organization_id/tags/team_ids/phone/country), 'update' (player_id + "
                "fields), 'upsert' (email), 'delete' (player_id). Use "
                "search_openaev_players to find a player's ID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "upsert", "delete"],
                    },
                    "player_id": {"type": "string", "description": "User/player ID."},
                    "email": {"type": "string"},
                    "firstname": {"type": "string"},
                    "lastname": {"type": "string"},
                    "organization_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "team_ids": {"type": "array", "items": {"type": "string"}},
                    "phone": {"type": "string"},
                    "country": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
