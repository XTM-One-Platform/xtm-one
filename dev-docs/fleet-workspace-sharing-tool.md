# Sharing a fleet and its workspace from an agent (`share_fleet_workspace`, #24)

A fleet is reached through two entities: the fleet itself (its page, `/fleets/{id}`) and the workspace it coordinates in. A person who sees the orchestrator but not the fleet or its workspace gets "fleet not found". Until #24 only a person could share both, from the Share dialog of each.

## The tool

`share_fleet_workspace` (platform group, fleets sub-area; `app/services/builtin_tools/fleets.py`):

| Argument | Meaning |
| --- | --- |
| `fleet_id` | The fleet (`list_fleets`). Its workspace is `fleet.workspace_id`. |
| `action` | `add` (default), `remove`, `replace`, or `list` (who has the fleet and its workspace; writes nothing). |
| `users` | Emails or user ids of existing accounts (`share_principals.resolve_principals`). |
| `groups` | Group ids or exact names. |
| `permission` | `view` (stored `read_only`) or `edit` (stored `config`), the dialog's two tiers. |

One grant, written on both entities in one transaction, planned by `presentation_sharing.plan_sharing` and stored through the REST routes' own writers (`apply_group_share_fields`, `apply_user_share_fields`): canonical ids, an existing active account for a new grant, an existing group, known tiers. An entry that matches nothing refuses the whole call. The owner of an entity is never granted.

## Who may call it

Never wider than the Share dialog:

1. The fleet must be manageable by the caller (`_manageable_fleet`: owner, `config` grantee, administrator; a built-in fleet is refused).
2. And the caller is a platform administrator, or owns the fleet's workspace. A `config` grantee of the fleet who does not own the workspace is refused: managing a fleet does not hand out its workspace.
3. A company-managed fleet or workspace visible to everyone is refused (nothing to share).

## Company-managed in place (#398)

An administrator's agent turns an existing fleet or workspace company-managed (or personal again) without recreating it: `update_workspace(is_company_managed)` (agent builder, `builtin_tools/workspaces.py`) and `update_fleet(is_company_managed)` (`builtin_tools/fleets.py`), both through `handle_company_managed_toggle` as `PUT /workspaces/{id}` and `PUT /fleets/{id}` do (group grants cleared unless the entity is access-restricted). The id is kept, so personas, assignments and registries keep their references.

- Administrators only; a non-administrator is refused and nothing is saved.
- Workspace: the default workspace is refused (`ensure_can_be_company_managed`); turning it personal is refused while it hosts a company-managed fleet (counted under the hosting lock).
- Fleet: going company-managed is refused, naming them, while a home member is a personal agent, and while its workspace is personal unless the same call moves it to another one (then `_host_workspace` checks that one). Order: make the workspace company-managed first, then the fleet; back: the fleet first, then the workspace.

## Audit

Each entity whose grants change gets one `update` entry (`entity_type` `fleet` or `workspace`), as the actor user, with details: `via: agent`, `agent_id` (the agent acting, passed to `build_fleet_tools` by the chat and the assignment run), `action`, `permission`, `added_users` / `removed_users` / `retiered_users` and the group counterparts, `fleet_id`, `workspace_id`, `sharing_changed: true`. The timestamp is the entry's own.

## Tests

`backend/tests/test_fleet_workspace_share_tool_postgres.py` (integration): an administrator's agent shares a colleague's fleet and workspace, the user then reads the fleet, a stranger is refused with nothing stored, one audit row per entity names the agent, the unshare clears both.

`backend/tests/test_fleet_workspace_share_tool.py`: refusal of a caller who is neither administrator nor workspace owner, share as the owner (both entities, both audit entries), share as an administrator with `edit`, unshare, list, unknown account, company-managed workspace, argument checks.
