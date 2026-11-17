"""Every image and every CI job runs the same pinned uv.

The platform, worker and sandbox images each `COPY --from` the uv binary, and
the workflows install uv with astral-sh/setup-uv. A lock written by a newer uv
can carry a revision an older one cannot read: CI running the images' version
fails such a lock on the pull request instead of the image build after merge.

Dependabot does not move these pins: its docker entry opens no version-update
pull requests (open-pull-requests-limit: 0), only security ones, and watches the
repository root only, never backend/sandbox. A bump is made by hand, in all three
Dockerfiles and every setup-uv step at once.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (
    ROOT / "Dockerfile",
    ROOT / "Dockerfile.worker",
    ROOT / "backend" / "sandbox" / "Dockerfile.sandbox.python",
)
WORKFLOWS = ROOT / ".github" / "workflows"
UV_IMAGE = re.compile(r"COPY\s+--from=(ghcr\.io/astral-sh/uv:([^@\s]+)@\S+)")
BUMP_HINT = "bump the same version in all three Dockerfiles and every setup-uv step"


def _image_pins() -> dict[str, re.Match[str]]:
    return {
        f"{path.relative_to(ROOT)}:{n}": m
        for path in DOCKERFILES
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (m := UV_IMAGE.search(line))
    }


def test_every_dockerfile_copies_uv_from_one_pinned_image() -> None:
    pins = {where: m.group(1) for where, m in _image_pins().items()}
    assert {str(path.relative_to(ROOT)) for path in DOCKERFILES} <= {key.rsplit(":", 1)[0] for key in pins}, (
        f"a Dockerfile no longer copies uv from ghcr.io/astral-sh/uv: {pins}"
    )
    assert len(set(pins.values())) == 1, f"the uv image differs between Dockerfiles ({BUMP_HINT}): {pins}"
    assert "@sha256:" in next(iter(pins.values())), "the uv image is not pinned by digest"


def test_every_setup_uv_step_installs_the_images_uv() -> None:
    image_version = next(iter(_image_pins().values())).group(2)
    versions = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for job_id, job in (yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}).items():
            for n, step in enumerate(job.get("steps") or []):
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    versions[f"{workflow.name}:{job_id}[{n}]"] = str((step.get("with") or {}).get("version"))
    assert versions, "no workflow installs uv with astral-sh/setup-uv"
    wrong = {where: v for where, v in versions.items() if v != image_version}
    assert not wrong, f"setup-uv must install uv {image_version}, the images' version ({BUMP_HINT}): {wrong}"
