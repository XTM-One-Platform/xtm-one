"""The run page's one-click approval and the backend approval parser agree.

The run page offers the reply an escalated irreversible action asks for
(``APPROVE <code>``) as one button, built by ``frontend/src/lib/hitl-approval.ts``
from the pending question. That reply must approve here exactly as the typed
reply does, or the person who clicked is asked again. The frontend unit test
(``hitl-approval.test.ts``) pins the same two reply strings.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.hitl_consent import hitl_reply_approves
from app.tools.base import HITL_APPROVAL_KEYWORD

_HELPER = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "hitl-approval.ts"

_A = "M-3F2A9C10"
_B = "M-7D41E0B2"


def _frontend_keyword() -> str:
    match = re.search(
        r'export const HITL_APPROVAL_KEYWORD = "([^"]+)"',
        _HELPER.read_text(encoding="utf-8"),
    )
    assert match, "hitl-approval.ts no longer declares HITL_APPROVAL_KEYWORD"
    return match.group(1)


def test_the_button_approves_with_the_backend_keyword():
    assert _frontend_keyword() == HITL_APPROVAL_KEYWORD


def test_a_one_click_reply_approves_every_code_it_names_and_no_other():
    keyword = _frontend_keyword()
    one = f"{keyword} {_A}"
    both = f"{keyword} {_A}, {keyword} {_B}"

    assert hitl_reply_approves(one, _A)
    assert not hitl_reply_approves(one, _B)
    assert hitl_reply_approves(both, _A)
    assert hitl_reply_approves(both, _B)
