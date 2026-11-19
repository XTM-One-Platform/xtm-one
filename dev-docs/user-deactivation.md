# User deactivation cascade

Every deactivation of an account goes through `backend/app/services/user_deactivation.py`, whatever the path that triggered it:

- the administrator's toggle (`PUT /api/v1/users/{id}/active`);
- SCIM `active=false`, and SCIM DELETE, which also sets `users.deprovisioned_at` (a soft state: nothing is purged);
- an SSO lockout.

## What `deactivate_user` does

All steps run in the caller's transaction (it flushes, never commits).

| Step | Effect |
|---|---|
| Account | `is_active` false, `deactivated_at`, `deactivation_source` (`admin`, `scim`, `sso`, `system`). Sessions, API keys, MCP tokens and platform JWTs already refuse an inactive account on their next request (`api/deps.py`, `services/api_key_access.py`). |
| API keys | Every personal key is revoked (`revoked_at`): a reactivation never brings a leaked key back. |
| Live work | Runs, background tasks, chat turns, runner commands and flow runs of the account are cancelled by `panic.stop_owner_live_work`, the emergency-stop arms scoped to one owner. There is no pause and no AI switch. Each cancelled row shows "the account it runs as was deactivated". |
| Timers and one-offs | Armed `platform_timers` rows are cancelled. Scheduled one-offs (`work_items.action_mode` set, not yet run) are cancelled. |
| Private agents | Owned, not shared, not company-managed: `enabled` and `web_enabled` are switched off. Their A2A exposures are kept: they stop resolving on the account's `is_active` and serve the same card URL after a reactivation (`a2a.hooks` skips the disable revocation when the run-as account is inactive). Their assignments keep their own switch; an assignment of a disabled agent never fires. |
| Shared and company-managed objects | Agents, fleets, workspaces and integrations move to the successor administrator: the `ADMIN_EMAIL` account when it is active, else the oldest active administrator. A slug or default-workspace clash is resolved on the successor's side. An assignment that runs as the departed account (`run_as_user_id`) is paused with `owner_deactivated`; the others keep running as the new owner. With no other active administrator nothing moves, and each object is listed `kept_no_successor`. |
| Integrations | The account's own credentials on `per_user` integrations are deleted. Its private OAuth integrations' tokens are cleared. Another person's agent bound to one of them is listed for review (`bound_by_other_agent`). |
| Claims and leases | Work items claimed by the stopped runs and shared-state leases they held are released, through the same sweeps as the end of a run. |
| Grants | Kept for audit: an inactive account resolves nothing, and reactivation needs them back. |
| Record | One `user_deactivations` row holds the snapshot, the review list and the counts. The partial unique index `uq_user_deactivations_open_user` allows one open episode per account. One `deactivate` audit row is written. |
| Administrators | Every active administrator gets a notification-bell entry (`notification_source = user_deactivation`) and, when SMTP is configured, an email. Both are sent after the commit (`after_commit`). |

A second deactivation of an inactive account is a no-op: it answers the open episode (`already`). With `deprovision=True` it only stamps `deprovisioned_at`.

The platform admin account (`ADMIN_EMAIL`) cannot be deactivated, and no administrator can deactivate themselves.

## Reactivation

`reactivate_user` closes the episode (`reactivated_at`) and restores from its snapshot:

- the private agents' `enabled` and `web_enabled`;
- the assignments paused `owner_deactivated` (only while they still carry that reason);
- the transferred objects, unless an administrator reviewed them, or they changed owner since. For an agent, only the assignments the cascade moved with it (their ids are recorded on the agent's transfer entry) go back; an assignment the successor already owned stays the successor's.

The revoked keys, cleared tokens, cancelled timers and one-offs, and stopped runs stay ended. Administrators are told.

## Review list

`review_items` entries are `{key, kind, id, name, action, detail, params, reviewed_at, reviewed_by, resolution}`. `detail` is the English sentence of the audit trail and the admin notification; the review screen never shows it and renders `REVIEW_DETAIL_LABELS[action]` (frontend/src/lib/provisioning-labels.ts) with `params` in the reader's language. An action gaining a parameter updates both sides. `resolve_review_item(..., resolution="keep" | "reassign" | "delete")` stamps one. A reviewed transfer is never taken back on reactivation.

Admin API (`backend/app/api/v1/users.py`): `GET /api/v1/users/deactivations?open=true` lists the episodes newest first with `open_review_count`; `POST /api/v1/users/deactivations/{id}/review` with `{key, resolution}` stamps one item and audits it. The screen is Settings > Access and security > Provisioning (`frontend/src/pages/settings/ProvisioningSection.tsx`).

## Preview

`preview_deactivation(db, user)` reads, without writing, what `deactivate_user` would do: `{refusal, successor, counts, kept_without_successor}`, the counts keyed as the cascade's own. `GET /api/v1/users/{id}/deactivation-preview` serves it to the **Disable** confirmation (`frontend/src/components/users/DeactivateUserDialog.tsx`). Figures the cascade learns only while acting (sub-agent tasks under a cancelled run, other people's agents bound to a private integration) are not previewed. `tests/test_user_deactivation_postgres.py::test_preview_matches_the_cascade_and_writes_nothing` keeps the preview equal to the cascade.

## Agent tools (`user_admin`)

The built-in group `user_admin` (`backend/app/services/builtin_tools/user_admin.py`) gives an administrator's agent what Administration > Users and groups offers. It calls `preview_deactivation`, `deactivate_user`, `reactivate_user` and `resolve_review_item` above, never a second cascade, then `after_commit` once the transaction is committed.

- Bound only by an administrator (`ADMIN_BOUND_BUILTIN_TOOL_IDS`), wired only for an administrator's turn (`offers_user_admin`), and every executor reads the acting person again (active and admin) before anything else.
- `deactivate_platform_users` takes 1 to 100 ids or emails and a required reason; `dry_run` is true unless the caller passes false, and a dry run answers the preview. It refuses the acting account, the `ADMIN_EMAIL` account, the last active administrator and an account flagged `users.deactivation_protected` (a break-glass or service account), per account, the rest of the batch going on.
- A group with a `scim_external_id` is managed by the identity provider: `add_group_members` and `remove_group_members` refuse it.
- Every write is audited (`log_audit`) with `details.tool_group`, the agent name and the run id, so `query_audit_log` reads who did what through which agent.
