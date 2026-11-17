"""The first evaluate after a navigate never runs on another origin than the one it answered (#10491).

After a run resume the session was re-created: the navigate left ``about:blank`` (or a reopened page) and answered
origin A, while the document was back on origin B, the origin the session was on before the resume. The guard of
#10013 compared the page with the address the navigate left only, so the script ran on B.
"""

from __future__ import annotations

import pytest

from app.services import browser_tool
from tests.test_browser_evaluate_after_navigate_10013 import KEY, LaggingPage, env  # noqa: F401  (fixture)

ORIGIN_A = "http://10.0.1.57:3100/operations/state?vis=all"
ORIGIN_B = "http://10.0.1.200:3100/operations/state?vis=all"


def _re_created_page(href: str, lag: int) -> LaggingPage:
    """A page of a session re-created after a resume: committed on about:blank, its document reading *href*."""
    page = LaggingPage("about:blank", lag=lag)
    page.href = href
    return page


async def test_an_evaluate_on_the_previous_origin_after_a_resume_is_refused_unrun(env):  # noqa: F811
    page = _re_created_page(ORIGIN_B, lag=10_000)
    env.install(page)

    await browser_tool.browser_navigate(KEY, ORIGIN_A)
    with pytest.raises(browser_tool.StillOnPreviousPageError) as raised:
        await browser_tool.browser_evaluate(KEY, "() => location.host")

    assert page.scripts == []
    message = str(raised.value)
    assert f"still on {ORIGIN_B}" in message and f"not on {ORIGIN_A}" in message


async def test_an_evaluate_waits_for_the_answered_origin_after_a_resume(env):  # noqa: F811
    page = _re_created_page(ORIGIN_B, lag=3)
    env.install(page)

    await browser_tool.browser_navigate(KEY, ORIGIN_A)
    result = await browser_tool.browser_evaluate(KEY, "() => location.host")

    assert page.scripts == ["() => location.host"]
    assert result.endswith(f"\n{ORIGIN_A}")


async def test_a_new_document_on_another_origin_is_not_taken_for_the_answered_one(env):  # noqa: F811
    page = _re_created_page(ORIGIN_B, lag=10_000)
    page.time_origin = 10**15  # created after the navigate started: still another origin than the one answered
    env.install(page)

    await browser_tool.browser_navigate(KEY, ORIGIN_A)
    with pytest.raises(browser_tool.StillOnPreviousPageError):
        await browser_tool.browser_evaluate(KEY, "() => location.host")
    assert page.scripts == []


async def test_a_same_origin_route_elsewhere_still_runs_the_script(env):  # noqa: F811
    page = _re_created_page("http://10.0.1.57:3100/login", lag=10_000)
    env.install(page)

    await browser_tool.browser_navigate(KEY, ORIGIN_A)
    result = await browser_tool.browser_evaluate(KEY, "() => location.pathname")

    assert page.scripts == ["() => location.pathname"]
    assert result.endswith("\nhttp://10.0.1.57:3100/login")
