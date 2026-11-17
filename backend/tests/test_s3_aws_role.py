"""``S3_USE_AWS_ROLE`` signs with the pod or instance role instead of static keys."""

from __future__ import annotations

import pytest
from minio.credentials import IamAwsProvider

from app.core.config import settings
from app.services import file_storage as fs


@pytest.fixture
def built(monkeypatch):
    captured: dict = {}

    class _Minio:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bucket_exists(self, _bucket):
            return True

    monkeypatch.setattr(fs, "Minio", _Minio)

    def build(**overrides) -> dict:
        for name, value in overrides.items():
            monkeypatch.setattr(settings, name, value)
        _ = fs.FileStorageService().client
        return captured

    return build


def test_the_aws_role_replaces_the_static_keys(built):
    kwargs = built(s3_use_aws_role=True, s3_access_key="ignored")
    assert isinstance(kwargs["credentials"], IamAwsProvider)
    assert "access_key" not in kwargs


# ``_http_client`` is the SDK's private attribute: a minio upgrade may rename it.
def test_the_role_lookups_are_bounded_and_retried(built):
    pool = built(s3_use_aws_role=True)["credentials"]._http_client
    assert pool.connection_pool_kw["timeout"].connect_timeout == settings.s3_connect_timeout
    assert 503 in pool.connection_pool_kw["retries"].status_forcelist


def test_the_role_lookups_never_inherit_the_s3_tls_bypass(built):
    pool = built(s3_use_aws_role=True, s3_use_ssl=True, s3_reject_unauthorized=False)["credentials"]._http_client
    assert "cert_reqs" not in pool.connection_pool_kw


def test_static_keys_stay_the_default(built):
    kwargs = built(s3_use_aws_role=False, s3_access_key="key", s3_secret_key="secret")
    assert (kwargs["access_key"], kwargs["secret_key"]) == ("key", "secret")
    assert "credentials" not in kwargs
