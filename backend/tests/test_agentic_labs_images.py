"""A lab dependency on its own product image (AGENTIC_LABS_IMAGES, xtm-one #9646 slice 2).

A lab pod runs each service of a chain in a container of its own, so a dependency may run on a product image
the operator allows (an OpenCTI, a search engine, a broker) while the started service keeps the chain's image.
The OpenSandbox path, one container per chain, keeps refusing a mixed image.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings, settings
from app.services.sandbox_workspace import checks, labs, services
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

SERVICES = """
[service.search]
argv = ["/usr/share/search/bin/run"]
port = 9200
image = "search"

[service.product]
argv = ["node", "build/back.js"]
port = 4000
image = "product"
depends_on = ["search"]

[service.lab]
argv = ["start-lab"]
port = 3100
image = "python-dev"
depends_on = ["product"]
"""


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=9), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1024 * 1024, max_files=20))


@pytest.fixture
def lab_images(monkeypatch):
    monkeypatch.setattr(
        settings, "agentic_labs_images", {"search": "registry/search:8", "product": "registry/product:6"}
    )
    monkeypatch.setattr(settings, "agentic_labs_deadline_hours", 0)


def test_the_chain_accepts_a_dependency_on_a_lab_image_only():
    specs = checks.load_services(SERVICES)
    with pytest.raises(checks.ChecksError, match="same image"):
        checks.service_chain(specs, "lab")
    chain = checks.service_chain(specs, "lab", own_images={"search", "product"})
    assert [spec.name for spec in chain] == ["search", "product", "lab"]
    # A key the lab images lack is named with the service that needs it, and the configured keys are listed (#11799).
    with pytest.raises(checks.ChecksError) as refused:
        checks.service_chain(specs, "lab", own_images={"product"})
    text = str(refused.value)
    assert "search runs on image key 'search'" in text and "product runs on" not in text
    assert "AGENTIC_LABS_IMAGES" in text and "(configured: product)" in text and "Nothing was started" in text
    # The started service itself never runs on a lab image of its own: its chain's image is the one checked.
    with pytest.raises(checks.ChecksError, match="same image"):
        checks.service_chain(specs, "product", own_images={"product"})


async def test_resolve_binds_the_chain_on_the_started_service_image(lab_images):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    with pytest.raises(checks.ChecksError, match="same image"):
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    resolved = await services.resolve(
        workspace, checkout="co", service="lab", known_images={"python-dev"}, lab_images={"search", "product"}
    )
    assert resolved.image == "python-dev"


async def test_the_pod_runs_each_dependency_on_its_own_image_and_the_service_on_the_chain_image(lab_images):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    resolved = await services.resolve(
        workspace, checkout="co", service="lab", known_images={"python-dev"}, lab_images={"search", "product"}
    )
    manifest = labs.pod_manifest(resolved, name="lab-1", image="registry/python-dev:2", workspace_id="w")
    images = {c["name"]: c["image"] for c in manifest["spec"]["containers"]}
    assert images == {
        "workspace": "registry/python-dev:2",
        "svc-search": "registry/search:8",
        "svc-product": "registry/product:6",
        "svc-lab": "registry/python-dev:2",
    }
    scripts = {c["name"]: c["command"][2] for c in manifest["spec"]["containers"][1:]}
    # A product image may hold no Python: it gets no probe loop, it waits for its dependencies itself.
    # Each service runs its argv under the restart supervisor (#12104), no longer through exec.
    assert "python -c" not in scripts["svc-product"] and "setsid node build/back.js &" in scripts["svc-product"]
    # The checkout's own service still waits for the product's port before it starts.
    assert "4000" in scripts["svc-lab"] and scripts["svc-lab"].index("4000") < scripts["svc-lab"].index(
        "setsid start-lab"
    )


async def test_starting_a_product_image_service_names_what_depends_on_it(lab_images: None) -> None:
    """A service on an AGENTIC_LABS_IMAGES key is refused as started, with its dependants named (#1371)."""
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    with pytest.raises(checks.ChecksError) as refused:
        await services.resolve(
            workspace, checkout="co", service="product", known_images={"python-dev"}, lab_images={"search", "product"}
        )
    text = str(refused.value)
    assert "product image key 'product'" in text and "AGENTIC_LABS_IMAGES" in text
    assert "Start a service that depends on it instead: lab." in text and "OPENSANDBOX_IMAGES" not in text
    # A key neither setting knows keeps the OPENSANDBOX_IMAGES refusal.
    with pytest.raises(checks.ChecksError, match="which OPENSANDBOX_IMAGES does not configure"):
        await services.resolve(workspace, checkout="co", service="search", known_images={"python-dev"})


def test_the_setting_parses_key_image_pairs_keeping_the_tag():
    parsed = Settings(agentic_labs_images="opencti:docker.io/opencti/platform:6.9.0,search:reg:5000/os:2")
    assert parsed.agentic_labs_images == {"opencti": "docker.io/opencti/platform:6.9.0", "search": "reg:5000/os:2"}
