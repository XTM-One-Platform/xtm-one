"""Regression tests for the app-only token cache TTL / expiry-skew guarantee
in ``app.integrations.http``.

``set_cached_app_token`` must never cache a token for longer than
``lifetime - _APP_TOKEN_EXPIRY_SKEW`` so a cached token is never served inside
the skew window of its real expiry. A short-lived token (lifetime ≤ skew) must
not be cached at all — the previous ``max(60, lifetime - skew)`` floor would
round a short lifetime *up* and serve the token inside the skew window.
"""

from __future__ import annotations

import app.integrations.http as http


def _fresh_key(name: str) -> str:
    return http.app_token_cache_key("test", name)


def test_normal_lifetime_is_cached():
    key = _fresh_key("normal")
    http._APP_TOKEN_CACHE.pop(key, None)
    # Lifetime well above the skew → cacheable.
    http.set_cached_app_token(key, "tok-normal", expires_in=3600)
    assert http.get_cached_app_token(key) == "tok-normal"


def test_short_lifetime_not_cached():
    key = _fresh_key("short")
    http._APP_TOKEN_CACHE.pop(key, None)
    # Lifetime shorter than the skew window → must NOT be cached (caching it
    # would inevitably serve it inside the skew window).
    http.set_cached_app_token(key, "tok-short", expires_in=http._APP_TOKEN_EXPIRY_SKEW - 1)
    assert http.get_cached_app_token(key) is None
    assert key not in http._APP_TOKEN_CACHE


def test_lifetime_equal_to_skew_not_cached():
    key = _fresh_key("equal")
    http._APP_TOKEN_CACHE.pop(key, None)
    # ttl = lifetime - skew == 0 → non-positive → not cached.
    http.set_cached_app_token(key, "tok-equal", expires_in=http._APP_TOKEN_EXPIRY_SKEW)
    assert http.get_cached_app_token(key) is None


def test_ttl_respects_skew_upper_bound():
    key = _fresh_key("skew")
    http._APP_TOKEN_CACHE.pop(key, None)
    lifetime = http._APP_TOKEN_EXPIRY_SKEW + 200
    http.set_cached_app_token(key, "tok", expires_in=lifetime)
    import time

    _, expiry = http._APP_TOKEN_CACHE[key]
    remaining = expiry - time.monotonic()
    # Never cached for longer than lifetime - skew (here ~200s), and the older
    # max(60, …) floor is gone so the skew is always honoured.
    assert remaining <= (lifetime - http._APP_TOKEN_EXPIRY_SKEW) + 1
