"""The OpenSandbox pool reaper deletes the Pool pods a Pool would never replace (#9490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.services import sandbox_pool_reaper as reaper
from app.services.sandbox_workspace import kube

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
STUCK = timedelta(seconds=600)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat().replace("+00:00", "Z")


def _pod(
    name: str,
    *,
    phase: str = "Running",
    reason: str | None = None,
    age: timedelta = timedelta(hours=1),
    ready: bool = True,
    waiting: str | None = None,
    pool: bool = True,
) -> dict:
    status: dict = {"phase": phase}
    if reason:
        status["reason"] = reason
    if phase == "Running":
        status["conditions"] = [
            {"type": "Ready", "status": "True" if ready else "False", "lastTransitionTime": _iso(age)}
        ]
    if waiting:
        status["initContainerStatuses"] = [{"name": "execd-init", "state": {"waiting": {"reason": waiting}}}]
    labels = {reaper.POOL_LABEL: "copilot-python-sandbox-pool"} if pool else {}
    return {"metadata": {"name": name, "labels": labels, "creationTimestamp": _iso(age)}, "status": status}


@pytest.mark.parametrize(
    ("pod", "verdict"),
    [
        (_pod("ok"), None),
        (_pod("evicted", phase="Failed", reason="Evicted"), "evicted"),
        (_pod("failed", phase="Failed"), "failed"),
        (_pod("completed", phase="Succeeded", age=timedelta(days=6)), "succeeded"),
        (_pod("init-unknown", phase="Pending", waiting="ContainerStatusUnknown", age=timedelta(days=6)), "broken"),
        (_pod("unknown", phase="Unknown", age=timedelta(hours=2)), "stuck"),
        (_pod("pulling", phase="Pending", age=timedelta(seconds=120)), None),
        (_pod("unready-long", ready=False, age=timedelta(hours=1)), "stuck"),
        (_pod("unready-fresh", ready=False, age=timedelta(seconds=30)), None),
        (_pod("not-a-pool-pod", phase="Failed", reason="Evicted", pool=False), None),
    ],
)
def test_pod_verdict(pod, verdict):
    assert reaper.pod_verdict(pod, now=NOW, stuck_after=STUCK) == verdict


def test_a_pod_being_deleted_is_left_alone():
    pod = _pod("going", phase="Failed", reason="Evicted")
    pod["metadata"]["deletionTimestamp"] = _iso(timedelta(seconds=5))
    assert reaper.pod_verdict(pod, now=NOW, stuck_after=STUCK) is None


def test_an_owner_reference_marks_a_pool_pod_without_the_label():
    pod = _pod("owned", phase="Succeeded", pool=False)
    pod["metadata"]["ownerReferences"] = [{"kind": "Pool", "name": "p"}]
    assert reaper.pod_verdict(pod, now=NOW, stuck_after=STUCK) == "succeeded"


def test_batch_sandbox_expiry():
    assert reaper.batch_sandbox_expired(
        {"metadata": {"name": "a"}, "spec": {"expireTime": _iso(timedelta(minutes=1))}}, now=NOW
    )
    assert not reaper.batch_sandbox_expired(
        {"metadata": {"name": "b"}, "spec": {"expireTime": _iso(-timedelta(minutes=5))}}, now=NOW
    )
    assert not reaper.batch_sandbox_expired({"metadata": {"name": "c"}, "spec": {}}, now=NOW)


class _Cluster:
    """A fake API server holding pods and BatchSandboxes of one namespace."""

    def __init__(
        self, pods: list[dict], sandboxes: list[dict], *, forbid_pods: bool = False, forbid_custom: bool = False
    ):
        self.pods = {p["metadata"]["name"]: p for p in pods}
        self.sandboxes = {s["metadata"]["name"]: s for s in sandboxes}
        self.forbid_pods = forbid_pods
        self.forbid_custom = forbid_custom
        self.seen: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append((request.method, path, request.url.params.get("labelSelector", "")))
        if path.startswith("/api/v1/namespaces/sandboxes/pods"):
            if self.forbid_pods:
                return httpx.Response(403, json={"message": "pods is forbidden"})
            if request.method == "GET":
                return httpx.Response(200, json={"items": list(self.pods.values())})
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={}) if self.pods.pop(name, None) else httpx.Response(404, json={})
        if path.startswith("/apis/sandbox.opensandbox.io/v1alpha1/namespaces/sandboxes/batchsandboxes"):
            if self.forbid_custom:
                return httpx.Response(403, json={"message": "batchsandboxes is forbidden"})
            if request.method == "GET":
                return httpx.Response(200, json={"items": list(self.sandboxes.values())})
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={}) if self.sandboxes.pop(name, None) else httpx.Response(404, json={})
        return httpx.Response(404, json={"message": path})

    def client(self, tmp_path) -> kube.KubeClient:
        token = tmp_path / "token"
        token.write_text("sa-token\n")
        config = kube.KubeConfig(
            api_url="https://k8s.example:6443", namespace="sandboxes", token_path=str(token), ca_path=""
        )
        return kube.KubeClient(config, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def namespace(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "opensandbox_k8s_namespace", "sandboxes")
    monkeypatch.setattr(settings, "opensandbox_pool_stuck_seconds", 600)
    monkeypatch.setattr(reaper, "_off_logged", set())


async def test_a_pool_whose_pods_all_died_is_emptied_so_the_controller_refills_it(namespace, tmp_path):
    # The 6-7 Nov 2026 incident: bufferMin 3, two pods Evicted, one Completed, a Node.js pod stuck in init.
    cluster = _Cluster(
        [
            _pod("python-a", phase="Failed", reason="Evicted"),
            _pod("python-b", phase="Failed", reason="Evicted"),
            _pod("python-c", phase="Succeeded", age=timedelta(days=6)),
            _pod("nodejs-a", phase="Pending", waiting="ContainerStatusUnknown", age=timedelta(days=6)),
            _pod("python-warm"),
        ],
        [
            {"metadata": {"name": "expired"}, "spec": {"expireTime": _iso(timedelta(hours=1))}},
            {"metadata": {"name": "live"}, "spec": {"expireTime": _iso(-timedelta(hours=1))}},
        ],
    )
    report = await reaper.reap(client=cluster.client(tmp_path), now=NOW)
    assert report.pods == {"python-a": "evicted", "python-b": "evicted", "python-c": "succeeded", "nodejs-a": "broken"}
    assert report.batch_sandboxes == ["expired"]
    assert set(cluster.pods) == {"python-warm"} and set(cluster.sandboxes) == {"live"}
    # Only Pool pods are listed: the label selector names the Pool label.
    assert ("GET", "/api/v1/namespaces/sandboxes/pods", reaper.POOL_LABEL) in cluster.seen


async def test_the_reaper_is_off_without_a_namespace(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "opensandbox_k8s_namespace", "")
    cluster = _Cluster([_pod("dead", phase="Failed", reason="Evicted")], [])
    assert await reaper.reap(client=cluster.client(tmp_path), now=NOW) == reaper.ReapReport()
    assert cluster.seen == [] and not reaper.enabled()


async def test_no_rbac_logs_one_line_and_deletes_nothing(namespace, tmp_path, monkeypatch, caplog):
    cluster = _Cluster([_pod("dead", phase="Failed", reason="Evicted")], [], forbid_pods=True)

    original = reaper.reap

    async def _reap():
        return await original(client=cluster.client(tmp_path), now=NOW)

    monkeypatch.setattr(reaper, "reap", _reap)
    await reaper.reap_logged()
    await reaper.reap_logged()
    lines = [r for r in caplog.records if "reaper off" in r.getMessage()]
    assert len(lines) == 1 and "sa-token" not in lines[0].getMessage()
    assert "dead" in cluster.pods


async def test_batch_sandboxes_not_readable_still_reap_the_pods(namespace, tmp_path):
    cluster = _Cluster([_pod("dead", phase="Failed", reason="Evicted")], [], forbid_custom=True)
    report = await reaper.reap(client=cluster.client(tmp_path), now=NOW)
    assert report.pods == {"dead": "evicted"} and report.batch_sandboxes == []


async def test_outside_a_cluster_the_reaper_logs_once(namespace, monkeypatch, caplog):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agentic_labs_api_url", "")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    await reaper.reap_logged()
    await reaper.reap_logged()
    assert len([r for r in caplog.records if "reaper off" in r.getMessage()]) == 1


class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


async def test_one_turn_a_minute_and_at_once_on_request(namespace, monkeypatch):
    redis = _Redis()

    async def _client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    assert await reaper.take_turn() is True
    assert await reaper.take_turn() is False  # the minute's window is taken
    await reaper.request_reap()  # a pooled create timed out
    assert await reaper.take_turn() is True
    assert await reaper.take_turn() is False


async def test_without_redis_the_turn_is_unknown(monkeypatch):
    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    assert await reaper.take_turn() is None


async def test_a_pooled_create_timeout_requests_a_reap(monkeypatch):
    from opensandbox.exceptions.sandbox import SandboxApiException

    from app.core.config import settings
    from app.services import _sandbox_selector

    requested: list[bool] = []

    async def _request():
        requested.append(True)

    class _Sandbox:
        @staticmethod
        async def create(**_kwargs):
            raise SandboxApiException("pod not ready", status_code=504)

    monkeypatch.setattr(reaper, "request_reap", _request)
    monkeypatch.setattr("opensandbox.Sandbox", _Sandbox)
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {"python": "copilot-python-sandbox-pool"})
    monkeypatch.setattr(_sandbox_selector, "_CREATE_TIMEOUT_RETRIES", 0)

    async def _quiet(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.sandbox_call_logging.log_sandbox_create_failure", _quiet)
    with pytest.raises(_sandbox_selector.SandboxUnavailableError):
        await _sandbox_selector._create_opensandbox(
            image_key="python",
            timeout_seconds=60,
            kind_label="code-interpreter",
            usage_type="code_interpreter",
            user_id=None,
            egress_allow_list=[],
            resource={"cpu": "1", "memory": "128Mi"},
        )
    assert requested == [True]
