"""A failed Playwright job's log answer carries the page the test saw (#11266).

The log of a failed E2E test only says ``expect(locator).toBeVisible() failed``;
the reason (an alert, an error paragraph) is in ``error-context.md`` inside the
run's ``test-results`` artifact. ``get_github_job_logs`` now reads it and
appends the page snapshot, chrome landmarks left out, in the same answer.
"""

import io
import zipfile
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations import playwright_context
from app.integrations.providers.github import build_tools
from app.integrations.providers.github.actions import _context_artifacts

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_REDIS = "app.core.redis.redis_client"

_DIR = "chat-fleet-setup-the-assistant-proposes-a-fleet-core"

_LOG = (
    "2026-11-11T21:40:00.0000000Z ##[group]Run yarn playwright test --project=core --shard=1/6\n"
    "2026-11-11T21:40:00.0000000Z ##[endgroup]\n"
    "2026-11-11T21:41:00.0000000Z   1) [core] > chat/fleet-setup.spec.ts:12:3 > the assistant proposes a fleet\n"
    "2026-11-11T21:41:00.0000000Z     Error: expect(locator).toBeVisible() failed\n"
    "2026-11-11T21:41:00.0000000Z     Locator: getByRole('button', { name: 'Create the fleet' })\n"
    "2026-11-11T21:41:00.0000000Z     Expected: visible\n"
    "2026-11-11T21:41:00.0000000Z     Error: element(s) not found\n"
    f"2026-11-11T21:41:00.0000000Z     Error Context: test-results/{_DIR}/error-context.md\n"
    f"2026-11-11T21:41:01.0000000Z     Error Context: test-results/{_DIR}/error-context.md\n"
    "2026-11-11T21:41:02.0000000Z   1 failed\n"
    "2026-11-11T21:41:02.0000000Z ##[error]Process completed with exit code 1.\n"
)

_CONTEXT = """# Instructions

- Following Playwright test failed.

# Test info

- Name: chat/fleet-setup.spec.ts >> the assistant proposes a fleet

# Error details

```
Error: expect(locator).toBeVisible() failed
```

# Page snapshot

```yaml
- generic [ref=e1]:
  - banner [ref=e2]:
    - link "Home" [ref=e3]
  - navigation "Main" [ref=e4]:
    - link "Chat" [ref=e5]
    - link "Agents" [ref=e6]
  - main [ref=e7]:
    - heading "New chat" [level=1] [ref=e8]
    - paragraph [ref=e9]: "SCRIPT-ERROR: no scripted answer for tool get_fleet_setup_state"
    - alert [ref=e10]: The assistant could not answer.
    - button "Send" [ref=e11] [cursor=pointer]
```

# Test source

```ts
test('the assistant proposes a fleet', async () => {});
```
"""


#: The layout of current Playwright (artifact test-results-core-0 of xtm-one run
#: 43103693748, retry2): no "# Page snapshot" heading, the ARIA snapshot is the
#: second fence of "# Error details", after the error and its call log.
_CONTEXT_CURRENT = """# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.

# Test info

- Name: chat/fleet-setup-chat.spec.ts >> Fleets setup >> the confirmation and the fleet exists

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('button', { name: 'Create the fleet' })
Expected: visible
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 10000ms
```

```yaml
- generic [ref=e2]:
  - navigation "Main" [ref=e3]:
    - link "Chat" [ref=e4]
    - link "Agents" [ref=e5]
  - banner [ref=e6]:
    - button "Account" [ref=e7]
  - main [ref=e8]:
    - region "Conversation transcript" [ref=e9]:
      - paragraph [ref=e10]: "SCRIPT-ERROR: no offered tool matches 'get_fleet_setup'; offered=[list_agents]"
```

# Test source

```ts
test('the confirmation and the fleet exists', async () => {});
```
"""


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_the_paths_a_log_names_are_found_once_each():
    assert playwright_context.error_context_paths(_LOG) == [(f"test-results/{_DIR}/error-context.md", _DIR)]
    assert playwright_context.error_context_paths("no context here") == []


def test_the_snapshot_keeps_the_text_and_leaves_the_chrome_out():
    lines = playwright_context.snapshot_lines(_CONTEXT)
    text = "\n".join(lines)
    assert "SCRIPT-ERROR: no scripted answer for tool get_fleet_setup_state" in text
    assert "alert: The assistant could not answer." in text
    assert 'heading "New chat"' in text
    assert "Agents" not in text and "Home" not in text
    assert "ref=" not in text and "cursor=" not in text
    assert "test(" not in text  # the test source section is not the snapshot


def test_a_file_without_a_snapshot_gives_nothing():
    assert playwright_context.snapshot_lines("# Error details\n\nboom\n") == []
    assert not playwright_context.has_page_snapshot("# Error details\n\n```\nboom\n```\n")


def test_the_current_layout_reads_the_aria_fence_under_error_details():
    assert playwright_context.has_page_snapshot(_CONTEXT_CURRENT)
    text = "\n".join(playwright_context.snapshot_lines(_CONTEXT_CURRENT))
    assert "SCRIPT-ERROR: no offered tool matches 'get_fleet_setup'" in text
    assert 'region "Conversation transcript"' in text
    assert "Agents" not in text and "Account" not in text  # navigation and banner left out
    assert "Call log" not in text and "toBeVisible" not in text  # the error fence is not the snapshot
    assert "test(" not in text


def test_a_file_with_no_snapshot_is_never_called_chrome_only():
    section = playwright_context.format_contexts(
        [("d1", None), ("d2", [])], {"d1": "test-results-core-0 (#13893682516)"}
    )
    assert (
        "(no page snapshot found in d1/error-context.md; download artifact test-results-core-0 (#13893682516)"
        in section
    )
    assert section.count("(no text outside the page chrome)") == 1  # d2 only: a snapshot of chrome alone


def test_a_long_snapshot_is_bounded():
    body = "\n".join(f'  - paragraph: "line {i}"' for i in range(200))
    lines = playwright_context.snapshot_lines(f"# Page snapshot\n\n```yaml\n- main:\n{body}\n```\n")
    assert len(lines) == playwright_context.MAX_SNAPSHOT_LINES + 1
    assert lines[-1].startswith("[snapshot clipped:")


@pytest.mark.parametrize(
    "job_name",
    [
        "E2E (core, 1/6)",  # GitHub appends the matrix values to a name that names none
        "E2E core 1/6",  # a name that spells them out
    ],
)
def test_the_artifact_of_the_job_shard_is_opened_first(job_name):
    artifacts = [
        {"id": 1, "name": "playwright-report-core-0", "size_in_bytes": 10},
        {"id": 2, "name": "test-results-agents-0", "size_in_bytes": 10},
        {"id": 3, "name": "test-results-core-1", "size_in_bytes": 10},
        {"id": 4, "name": "test-results-core-0", "size_in_bytes": 10},
        {"id": 5, "name": "test-results-core-2", "size_in_bytes": 10, "expired": True},
    ]
    picked = _context_artifacts(artifacts, job_name)
    assert picked[0]["id"] == 4
    assert all("test-results" in a["name"] and not a.get("expired") for a in picked)


def _job_logs_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["get_github_job_logs"]


class _FakeRedis:
    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        return None


async def _redis():
    return _FakeRedis()


class _GitHub:
    def __init__(self, artifacts: list[dict], zips: dict[int, bytes]):
        self.artifacts = artifacts
        self.zips = zips
        self.urls: list[str] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        if url.endswith("/logs"):
            return httpx.Response(200, text=_LOG), None
        if url.endswith("/actions/jobs/77"):
            return httpx.Response(200, json={"id": 77, "run_id": 500, "name": "E2E core 1/6"}), None
        if url.endswith("/runs/500/artifacts"):
            return httpx.Response(200, json={"artifacts": self.artifacts}), None
        for aid, data in self.zips.items():
            if url.endswith(f"/artifacts/{aid}/zip"):
                return httpx.Response(200, content=data), None
        return None, "Error: not found"


@pytest.mark.asyncio
async def test_failures_only_appends_the_page_snapshot_in_one_call():
    data = _zip({f"{_DIR}/error-context.md": _CONTEXT, f"{_DIR}/test-failed-1.png": "png"})
    github = _GitHub([{"id": 9, "name": "test-results-core-0", "size_in_bytes": len(data)}], {9: data})
    with patch(_REDIS, new=_redis), patch(_SAFE_REQUEST, new=github):
        answer = await _job_logs_tool()(repo="o/r", job_id=77, failures_only=True)
    assert "toBeVisible() failed" in answer
    assert "page snapshot of each failed Playwright test" in answer
    assert f"[{_DIR}]" in answer
    assert "SCRIPT-ERROR: no scripted answer" in answer
    assert "Agents" not in answer.split("page snapshot of each")[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failures_only", [True, False])
async def test_the_current_layout_reaches_the_answer_in_both_modes(failures_only):
    data = _zip({f"{_DIR}/error-context.md": _CONTEXT_CURRENT})
    github = _GitHub([{"id": 9, "name": "test-results-core-0", "size_in_bytes": len(data)}], {9: data})
    with patch(_REDIS, new=_redis), patch(_SAFE_REQUEST, new=github):
        answer = await _job_logs_tool()(repo="o/r", job_id=77, failures_only=failures_only)
    section = answer.split("page snapshot of each failed Playwright test")[1]
    assert "SCRIPT-ERROR: no offered tool matches 'get_fleet_setup'" in section
    assert "no text outside the page chrome" not in section
    assert "Agents" not in section


@pytest.mark.asyncio
async def test_an_unreadable_artifact_names_where_the_file_is():
    github = _GitHub([{"id": 9, "name": "test-results-core-0", "size_in_bytes": 10, "expired": True}], {})
    with patch(_REDIS, new=_redis), patch(_SAFE_REQUEST, new=github):
        answer = await _job_logs_tool()(repo="o/r", job_id=77, failures_only=True)
    assert "toBeVisible() failed" in answer
    assert f"error-context.md not read: test-results/{_DIR}/error-context.md" in answer
    assert "download_github_workflow_artifact" in answer


@pytest.mark.asyncio
async def test_a_log_without_error_context_reads_no_artifact():
    log = "##[group]Run pytest\n##[endgroup]\nE   AssertionError: boom\n##[error]Process completed with exit code 1.\n"

    async def github(client, method, url, provider, action, **kwargs):
        assert url.endswith("/logs"), url
        return httpx.Response(200, text=log), None

    with patch(_REDIS, new=_redis), patch(_SAFE_REQUEST, new=github):
        answer = await _job_logs_tool()(repo="o/r", job_id=77, failures_only=True)
    assert "boom" in answer
    assert "page snapshot" not in answer
