"""An assignment run that did not delegate is not an operator matter.

Every run of an agent with handover peers that chose not to delegate logged a
WARNING, naming neither the run nor the assignment. Delegation is the model's
choice in an assignment run: no configuration requires a handover tool call (a
mandatory transfer is post-loop routing, never offered to the model). The line
stays, at DEBUG, naming the run.
"""

from __future__ import annotations

import ast
import inspect

from app.services import assignment_engine


def test_a_run_that_did_not_delegate_is_logged_at_debug_and_names_the_run():
    tree = ast.parse(inspect.getsource(assignment_engine))
    [call] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(isinstance(arg, ast.Constant) and "no handover tool was called" in str(arg.value) for arg in node.args)
    ]
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "debug"
    assert any(isinstance(arg, ast.Name) and arg.id == "run_id" for arg in call.args)
