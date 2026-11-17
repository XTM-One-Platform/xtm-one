"""Two different destination refusals, kept distinct.

FR-035, FR-054 / SC-006. No deployment egress policy applies here — the only
egress allow-list in the repository builds network rules for sandbox
containers, and nothing constrains backend-initiated HTTP.

What the platform can honestly own is split in two, and the split is the
subject of this file:

* **FR-035** — a scoped guard this platform enforces itself, because a card
  location is operator-supplied and fetched with stored credentials; and
* **FR-054** — classifying a refusal made by the DEPLOYMENT's own network
  policy, which the platform does not enforce and must not pretend to.

Collapsing them would tell an operator to fix the wrong thing: one is a bad
URL, the other is their firewall.
"""

from __future__ import annotations

import pytest

from app.a2a.client import destinations, failures
from app.a2a.client.destinations import DestinationRefusedError, assert_allowed
from app.models.a2a import UNUSABLE_REASONS


class TestTheGuardWeEnforce:
    @pytest.mark.parametrize(
        "url",
        [
            "https://169.254.169.254/card.json",
            "https://127.0.0.1/card.json",
            "https://10.0.0.1/card.json",
            # Shared address space: refused by us, and it must land on OUR
            # reason rather than looking like the deployment's firewall — an
            # operator behind carrier-grade NAT would otherwise be sent to
            # inspect a network policy that never saw the request.
            "https://100.64.0.7/card.json",
        ],
    )
    def test_a_forbidden_address_is_ours_to_refuse(self, url):
        with pytest.raises(DestinationRefusedError) as exc:
            assert_allowed(url, resolve=False)
        assert exc.value.reason == "destination_not_permitted"

    def test_an_unencrypted_url_is_a_different_reason(self):
        # Not "not permitted": the operator typed http, which is a typo, not
        # a policy question.
        with pytest.raises(DestinationRefusedError) as exc:
            assert_allowed("http://partner.example/c.json")
        assert exc.value.reason == "transport_unverified"

    def test_an_unresolvable_host_is_a_third_reason(self):
        # A partner that is not there is fixed by correcting the URL, not by
        # changing a network policy.
        with pytest.raises(DestinationRefusedError) as exc:
            assert_allowed("https://nx-does-not-exist.invalid/c.json")
        assert exc.value.reason == "card_unfetchable"


class TestTheRefusalWeOnlyClassify:
    def test_a_deployment_refusal_has_its_own_reason(self):
        # FR-054. Distinct from ours, because the remedy is in a different
        # system and belongs to a different person.
        assert "destination_blocked_by_deployment" in UNUSABLE_REASONS
        assert "destination_not_permitted" in UNUSABLE_REASONS

    def test_both_are_durable_so_neither_opens_the_circuit(self):
        # Retrying a refused destination cannot succeed on either side, so
        # counting it toward a cooldown would delay the operator's only real
        # signal — the registration going unusable with a reason.
        for reason in (
            "destination_not_permitted",
            "destination_blocked_by_deployment",
        ):
            assert failures.durable(reason).is_durable

    def test_the_classifier_keeps_the_guard_s_own_reason(self):
        # Re-deriving a reason here would be a second classification of one
        # failure, disagreeing with the first the moment either changed.
        for reason in (
            "destination_not_permitted",
            "transport_unverified",
            "card_unfetchable",
        ):
            classified = failures.classify_exception(
                DestinationRefusedError("x", reason=reason)
            )
            assert classified.reason == reason


class TestWeDoNotPretendToEnforceNetworkPolicy:
    def test_the_guard_consults_no_deployment_allow_list(self):
        # Borrowing ``opensandbox_network_allow`` would let an operator's
        # sandbox rules silently decide which partners are reachable — a
        # different feature, and a worse one.
        #
        # Checked against EXECUTABLE code, not the source text. Docstrings
        # are stripped first: the module docstring names these settings to
        # explain why it does not use them, and a substring search would
        # match the explanation.
        import ast
        import inspect

        from app.a2a.client import destinations

        tree = ast.parse(inspect.getsource(destinations))
        for node in ast.walk(tree):
            if isinstance(
                node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and ast.get_docstring(node):
                node.body = node.body[1:]
        code = ast.unparse(tree).lower()
        for borrowed in ("opensandbox", "network_allow", "allow_list", "allowlist"):
            assert borrowed not in code, borrowed

    def test_the_known_gap_is_stated_rather_than_implied(self):
        # A guard that resolves DNS and then connects has a window where the
        # name can resolve differently. Not closed here — closing it means
        # pinning the resolved address into the connection, a transport-layer
        # change out of proportion to operator-supplied destinations. Recorded
        # so it is found by reading rather than by an incident.
        assert "DNS-then-connect gap" in assert_allowed.__doc__

    def test_the_gap_is_one_resolution_wide_because_no_hop_is_followed(self):
        """Every outbound client refuses redirects, so there is one lookup.

        This is what bounds the gap above. A followed redirect is a second
        destination — a fresh name, a fresh resolution — and ``httpx``
        follows it inside a single ``await``, where the guard has no seam to
        run in. Origin pinning would refuse a hop off the pinned origin, but
        a hop to *the same name* is not an origin change and would resolve
        again unchecked; the request also carries the partner's bearer, which
        is what makes it worth an attacker's trouble.

        So no hop is followed anywhere in the package, and this asserts it
        of every client rather than of the four that exist today — the next
        one is the one that will forget.
        """
        import ast
        import pathlib as _pathlib

        package = _pathlib.Path(destinations.__file__).parent
        found = 0
        for module in sorted(package.glob("*.py")):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                if not ast.unparse(node.func).endswith("AsyncClient"):
                    continue
                found += 1
                passed = {
                    kw.arg: ast.unparse(kw.value)
                    for kw in node.keywords
                }
                where = f"{module.name}:{node.lineno}"
                assert passed.get("follow_redirects") == "False", (
                    f"{where}: an outbound client must refuse redirects — a "
                    "followed hop resolves a name the destination guard "
                    "never saw, with a partner credential attached"
                )
        assert found >= 4, "the sweep found no clients — has the package moved?"
