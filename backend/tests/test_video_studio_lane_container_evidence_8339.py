"""A refused recording lane says what its container holds of the NVIDIA driver.

On a GPU node verified clean (driver, libEGL_nvidia, libGLX_nvidia and the Vulkan ICD installed on the host), the lane
read ANGLE on Vulkan on SwiftShader and ANGLE on EGL on Mesa llvmpipe: both loaders fell back, so the NVIDIA libraries
did not load in the container. The refusal could not say whether they were not mounted, failed on a missing dependency
or had no device node. What is proven here:

* a refused lane's reason ends with "In the container: ..." (device nodes, the runtime's environment, each library's
  load or the loader's own error, the vendor and ICD files);
* the load runs for real in a child process and names a missing library;
* a lane that found its GPU never reads it; a failing read never removes the refusal.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.video_studio import capture_rate

LLVMPIPE = "ANGLE (Mesa, llvmpipe (LLVM 19.1.7 256 bits), OpenGL ES 3.2)"


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    monkeypatch.setattr(capture_rate.settings, "video_studio_take_concurrency", 1)
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "gpu")


def _probes(monkeypatch, vulkan: dict, egl: dict) -> None:
    statuses = {capture_rate.GPU_GL_ARGS: vulkan, capture_rate.GPU_EGL_ARGS: egl}

    async def probe(args):
        return dict(statuses[args])

    monkeypatch.setattr(capture_rate, "_probe", probe)


def test_a_missing_library_is_named_by_the_loader():
    said = asyncio.run(capture_rate._load_library("libdefinitely_not_here_8339.so.0"))
    assert said != "loads"
    assert "libdefinitely_not_here_8339.so.0" in said


def test_a_present_library_loads():
    # The C library every Python links: present in any container.
    assert asyncio.run(capture_rate._load_library("libc.so.6")) == "loads"


def test_the_evidence_names_devices_environment_libraries_and_vendor_files(monkeypatch, tmp_path):
    vendor = tmp_path / "egl_vendor.d"
    vendor.mkdir()
    (vendor / "10_nvidia.json").write_text("{}")
    (vendor / "50_mesa.json").write_text("{}")
    monkeypatch.setattr(capture_rate, "GL_VENDOR_DIRS", (str(vendor), str(tmp_path / "absent")))
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "compute,utility,graphics")
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    loads = {
        "libEGL_nvidia.so.0": "libnvidia-gpucomp.so.550.163.01: cannot open shared object file: No such file or directory",
        "libGLX_nvidia.so.0": "loads",
    }

    async def load(name):
        return loads[name]

    monkeypatch.setattr(capture_rate, "_load_library", load)

    async def egl():
        return "client extensions EXT_device_enumeration+ EXT_platform_device+ MESA_platform_surfaceless-, devices 0"

    monkeypatch.setattr(capture_rate, "_egl_check", egl)
    real_listed = capture_rate._listed
    monkeypatch.setattr(
        capture_rate, "_listed", lambda d, p="": ["nvidia0", "nvidiactl"] if d == "/dev" else real_listed(d, p)
    )

    said = asyncio.run(capture_rate.container_gl_evidence())
    assert said == (
        "/dev: nvidia0, nvidiactl; NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics; NVIDIA_VISIBLE_DEVICES=(unset); "
        "libEGL_nvidia.so.0: libnvidia-gpucomp.so.550.163.01: cannot open shared object file: No such file or directory; "
        f"libGLX_nvidia.so.0: loads; {vendor}: 10_nvidia.json, 50_mesa.json; "
        "EGL under the lane's environment: client extensions EXT_device_enumeration+ EXT_platform_device+ "
        "MESA_platform_surfaceless-, devices 0"
    )


def test_no_device_node_is_said(monkeypatch):
    monkeypatch.setattr(capture_rate, "GL_VENDOR_DIRS", ())

    async def load(name):
        return "loads"

    monkeypatch.setattr(capture_rate, "_load_library", load)

    async def egl():
        return "devices 0"

    monkeypatch.setattr(capture_rate, "_egl_check", egl)
    monkeypatch.setattr(capture_rate, "_listed", lambda d, p="": [])
    assert asyncio.run(capture_rate.container_gl_evidence()).startswith("/dev: no nvidia device; ")


def test_a_refused_lane_ends_its_reason_with_the_container_evidence(monkeypatch, lane):
    _probes(
        monkeypatch,
        {"webgl": "unavailable_software", "canvas_2d": "unavailable_software", "renderer": "ANGLE (Google, SwiftShader Device)"},
        {"webgl": "enabled", "canvas_2d": "enabled", "renderer": LLVMPIPE},
    )

    async def evidence():
        return "/dev: no nvidia device; NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics"

    monkeypatch.setattr(capture_rate, "container_gl_evidence", evidence)
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.refused and not backend.gpu
    assert "ANGLE on EGL: webgl enabled, renderer " + LLVMPIPE + ", a CPU rasterizer (llvmpipe)" in backend.why
    assert backend.why.endswith(
        "). In the container: /dev: no nvidia device; NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics"
    )
    assert capture_rate.recording_refused() == backend.why


def test_a_failing_evidence_read_keeps_the_refusal(monkeypatch, lane):
    _probes(monkeypatch, {"webgl": "unavailable_software", "renderer": "SwiftShader"}, {"webgl": "enabled", "renderer": LLVMPIPE})

    async def evidence():
        raise RuntimeError("boom")

    monkeypatch.setattr(capture_rate, "container_gl_evidence", evidence)
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.refused
    assert backend.why.endswith("In the container: not read (boom)")


def test_a_lane_on_its_gpu_reads_no_evidence(monkeypatch, lane):
    _probes(
        monkeypatch,
        {"webgl": "enabled", "canvas_2d": "enabled", "renderer": "ANGLE (NVIDIA, Vulkan 1.3 (Tesla V100S-PCIE-32GB))"},
        {"webgl": "enabled", "renderer": LLVMPIPE},
    )
    read: list = []

    async def evidence():
        read.append(1)
        return ""

    monkeypatch.setattr(capture_rate, "container_gl_evidence", evidence)
    backend = asyncio.run(capture_rate.gl_backend())
    assert backend.gpu and not backend.refused and read == []
