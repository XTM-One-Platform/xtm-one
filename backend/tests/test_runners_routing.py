"""Route-ordering regression tests for the runners router.

Starlette matches routes in declaration order, so a single-segment dynamic route
like ``GET /{runner_id}`` declared before the static runner-daemon GET routes
(``/poll``, ``/update-manifest``) would swallow them as ``runner_id`` and 404
them - breaking the long-poll fallback and the download manifest. These tests
pin the resolution so the ordering can't silently regress.
"""

from starlette.routing import Match

from app.api.v1.runners import router


def _match_endpoint(method: str, path: str) -> str | None:
    """Name of the FIRST router endpoint that fully matches (method + path)."""
    scope = {"type": "http", "method": method, "path": path}
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint.__name__
    return None


def test_poll_route_not_shadowed_by_runner_id():
    assert _match_endpoint("GET", "/poll") == "poll_commands"


def test_update_manifest_route_not_shadowed_by_runner_id():
    assert _match_endpoint("GET", "/update-manifest") == "update_manifest"


def test_capabilities_route_not_shadowed_by_runner_id():
    assert _match_endpoint("GET", "/capabilities") == "list_capabilities"


def test_download_manifest_not_swallowed_as_a_platform():
    # /download/manifest must resolve to the manifest alias, not to
    # /download/{platform} with platform="manifest".
    assert _match_endpoint("GET", "/download/manifest") == "download_manifest"


def test_download_platform_routes_resolve():
    assert _match_endpoint("GET", "/download/windows") == "download_runner"
    assert _match_endpoint("GET", "/download/linux/deb") == "download_runner_format"


def test_dist_asset_route_resolves():
    assert (
        _match_endpoint("GET", "/dist/xtm-one-runner-windows-x64-setup.exe")
        == "download_dist_asset"
    )


def test_updater_route_resolves():
    assert (
        _match_endpoint("GET", "/updater/linux-x86_64/x86_64/1.260101.0")
        == "runner_updater"
    )


def test_runner_id_still_resolves_for_a_real_segment():
    assert _match_endpoint("GET", "/2f1c9d7a-0000-4000-8000-000000000000") == "get_runner"
