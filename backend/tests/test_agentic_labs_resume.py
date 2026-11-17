"""A lab a resumed run comes back to: its signed-in sessions repeated, its licences never lapsed.

After a rollout restart the browser tool may come back signed out, and a lab reused the next day carries the
short-lived licences it was created with. The start and status answers repeat the signed-in session URLs the lab
logged (``session_log``), on the preview origin, and a lab whose licences would lapse within the margin is
replaced with fresh ones instead of adopted. The fake Kubernetes client is the one of ``test_agentic_labs.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.sandbox_workspace import checks, labs, licences, services
from tests.test_agentic_labs import (  # noqa: F401
    SERVICES,
    _Kube,
    _no_sleep,
    _workspace,
    lab_settings,
    no_quota,
    redis,
)

LOG = (
    "[lab 40.0s] seeded signed-in session (administrator): http://localhost:3100/login#token=adm\n"
    "[lab 40.1s] seeded agent: http://localhost:3100/agents/1\n"
    "[lab 40.2s] seeded signed-in session (peer): http://localhost:3100/login#token=peer\n"
    "[lab 40.3s] lab up: sign in\n"
)


async def _resolved_with_sessions(workspace):
    text = SERVICES.replace(
        'ready_path = "/api/health"\n',
        'ready_path = "/api/health"\nready_log = "lab up: "\nsession_log = "seeded signed-in session"\n',
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", text.encode()), ("co/backend/app.py", b"print('hi')\n")])
    return await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})


def _logging(fake: _Kube, reads: list[str]) -> None:
    async def logs(name, container, *, tail_lines, limit_bytes):
        reads.append(container)
        return LOG if container == "svc-lab" else "db up\n"

    fake.logs = logs  # type: ignore[method-assign]


def test_session_log_is_declared_per_service():
    specs = checks.load_services('[service.a]\nargv = ["x"]\nport = 1\nsession_log = "seeded signed-in session"\n')
    assert specs["a"].session_log == "seeded signed-in session"
    assert checks.load_services('[service.a]\nargv = ["x"]\nport = 1\n')["a"].session_log == ""


def test_sessions_are_parsed_last_per_label_and_bounded():
    text = (
        LOG
        + "[lab 90s] seeded signed-in session (peer): http://localhost:3100/login#token=peer2\nseeded signed-in session (x): not-a-url\n"
    )
    assert labs.parse_sessions(text, "seeded signed-in session") == [
        ("seeded signed-in session (administrator)", "http://localhost:3100/login#token=adm"),
        ("seeded signed-in session (peer)", "http://localhost:3100/login#token=peer2"),
    ]
    assert labs.parse_sessions("nothing here", "seeded signed-in session") == []


def test_a_loopback_url_is_moved_to_the_preview_origin_and_another_is_kept():
    assert (
        labs._session_url("http://localhost:3100/login#token=a", "http://10.42.0.9:3100/")
        == "http://10.42.0.9:3100/login#token=a"
    )
    assert labs._session_url("http://127.0.0.1:3100/x?y=1", "http://10.42.0.9:3100/") == "http://10.42.0.9:3100/x?y=1"
    assert labs._session_url("https://example.org/a", "http://10.42.0.9:3100/") == "https://example.org/a"
    assert labs._session_url("http://localhost:3100/a", "") == "http://localhost:3100/a"


@pytest.mark.usefixtures("lab_settings", "no_quota", "redis")
async def test_start_and_status_repeat_the_signed_in_sessions_on_the_preview_origin():
    workspace = _workspace()
    fake = _Kube()
    reads: list[str] = []
    _logging(fake, reads)
    resolved = await _resolved_with_sessions(workspace)
    outcome = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    assert "- seeded signed-in session (administrator): http://10.42.0.9:3100/login#token=adm" in outcome.text
    assert "- seeded signed-in session (peer): http://10.42.0.9:3100/login#token=peer" in outcome.text
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["session_logs"] == {"svc-lab": "seeded signed-in session"} and len(held["sessions"]) == 2
    # A resumed run: the status (and a second start) answer the same URLs from the record, the log not read again.
    count = len(reads)
    status = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert "- seeded signed-in session (peer): http://10.42.0.9:3100/login#token=peer" in status.text
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" in again.text and "login#token=adm" in again.text
    assert len(reads) == count


@pytest.mark.usefixtures("lab_settings", "no_quota", "redis")
async def test_a_service_without_session_log_answers_no_session_line():
    workspace = _workspace()
    fake = _Kube()
    _logging(fake, [])
    text = SERVICES.replace('ready_path = "/api/health"\n', 'ready_path = "/api/health"\nready_log = "lab up: "\n')
    await workspace.put_tree([("co/.xtm-one/checks.toml", text.encode()), ("co/backend/app.py", b"print('hi')\n")])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})
    outcome = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready and "Signed-in sessions" not in outcome.text


def test_licences_lapse_within_the_margin_only():
    now = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)
    assert labs._licences_lapsing({}, now=now) == ""
    assert labs._licences_lapsing({"licences_expire_at": "not a date"}, now=now) == ""
    later = (now + timedelta(hours=5)).isoformat()
    assert labs._licences_lapsing({"licences_expire_at": later}, now=now) == ""
    soon = (now + timedelta(minutes=30)).isoformat()
    assert "expire at" in labs._licences_lapsing({"licences_expire_at": soon}, now=now)
    past = (now - timedelta(hours=1)).isoformat()
    assert "expire at" in labs._licences_lapsing({"licences_expire_at": past}, now=now)


@pytest.mark.usefixtures("lab_settings", "no_quota", "redis")
async def test_a_lab_whose_licences_lapse_is_replaced_with_fresh_ones(monkeypatch):
    issued_calls: list[int] = []

    async def issue():
        issued_calls.append(1)
        return licences.Issued(env={"PRODUCT_A_LICENSE": "pem\n"}, hours=24)

    monkeypatch.setattr(licences, "issue", issue)
    workspace = _workspace()
    fake = _Kube()
    resolved = await _resolved_with_sessions(workspace)
    _logging(fake, [])
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert first.ready and "valid until" in first.text, first.text
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    expires = datetime.fromisoformat(held["licences_expire_at"])
    assert timedelta(hours=23) < expires - datetime.now(UTC) <= timedelta(hours=24)
    # Still valid: adopted, no new licence asked.
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.service_id == first.service_id and len(issued_calls) == 1
    # Reused once the licences lapse: a new pod with fresh licences, the old one deleted, the answer says why.
    await labs._remember(
        workspace, "co:lab", {**held, "licences_expire_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}
    )
    renewed = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert renewed.ready and renewed.service_id != first.service_id, renewed.text
    assert first.service_id in fake.deleted and len(issued_calls) == 2
    assert (
        f"the previous one ({first.service_id}) was replaced because its Enterprise Edition licences expire at"
        in renewed.text
    )
    assert "login#token=peer" in renewed.text
