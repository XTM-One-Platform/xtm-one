"""The dev Redis runs without a password, as the dev backend expects.

``docker-compose.dev.yml`` forces ``REDIS_URL`` to ``redis://redis:6379`` on the
platform and the workers, with no password. The ``redis`` service comes from
``docker-compose.yml``, which turns on ``--requirepass`` when ``REDIS_PASSWORD``
is set: interpolated from ``.env``, a password there locked the dev backend out
(``NOAUTH``) and the platform never reported ready. The dev override clears it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


class _TaggedLoader(yaml.SafeLoader):
    """Reads the ``!reset`` / ``!override`` tags of the dev override as plain values."""


def _untagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_TaggedLoader.add_multi_constructor("!", _untagged)


def _compose(name: str) -> dict:
    return yaml.load((REPO_ROOT / name).read_text(encoding="utf-8"), Loader=_TaggedLoader)  # noqa: S506 - SafeLoader subclass


def test_the_shipped_redis_takes_its_password_from_env():
    redis = _compose("docker-compose.yml")["services"]["redis"]

    assert redis["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:-}"
    assert "--requirepass" in "\n".join(redis["command"])


def test_the_dev_backend_connects_to_the_dev_redis_without_a_password():
    backend = _compose("docker-compose.dev.yml")["x-dev-backend"]["environment"]

    assert backend["REDIS_URL"] == "redis://redis:6379"


def test_the_dev_redis_ignores_a_password_set_in_env():
    redis = _compose("docker-compose.dev.yml")["services"]["redis"]

    # A literal, not interpolated from .env: the server starts without
    # --requirepass and its healthcheck takes the passwordless branch.
    assert redis["environment"]["REDIS_PASSWORD"] == ""
