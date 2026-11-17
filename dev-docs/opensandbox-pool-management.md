# Managing a Pool of OpenSandbox VMs

**Date:** 2026-07-23 (Option 1 implemented 2026-08-07/08 — see "Update" note below)
**Context:** The OpenSandbox integration and sandbox call quota management left an expected production cost increase as the main open concern with the Platform team (@SamuelHassine @richard-julien @davidloiret). Cold-start latency and per-call VM creation cost are the two levers pooling addresses.
**Images in scope:** `sandbox-python:0.4`, `sandbox-nodejs:0.4` (`settings.opensandbox_images["python"]` / `["nodejs"]`)

**Update (2026-08-10): Option 1 is implemented.** `OPENSANDBOX_K8S_POOL_NAMES` (per-key `key:pool-name` pairs) and the `extensions={"poolRef": ...}` wiring — originally sketched below as a proposed next step — have since shipped in `_create_opensandbox()` (`backend/app/services/_sandbox_selector.py`), used by all three call sites. The sections below have been updated in place to describe the current, implemented state; only the underlying protocol analysis (why `extensions.poolRef` is the right mechanism, what was verified from the vendored `opensandbox`/`opensandbox-server` source) is unchanged from the original investigation.

---

## Summary

Today every OpenSandbox-routed call (`execute_python_opensandbox`, `execute_js_tool_opensandbox`, `execute_python_tool_opensandbox`) pays a full cold start by default: `Sandbox.create(...)` provisions a fresh microVM/pod per request, runs the code, and the sandbox is torn down. There are **two independent ways** to keep warm sandboxes ready instead of creating one per call, with very different cost/code trade-offs:

1. **Kubernetes-native pre-warming** (`Pool` CRD via `extensions={"poolRef": ...}` on `Sandbox.create()` — **implemented**, opt-in per key via `OPENSANDBOX_K8S_POOL_NAMES`; see below)
2. **Client-side pooling** (the `opensandbox` SDK's `SandboxPoolAsync`, requires new app code + a Redis-backed coordination store — **not implemented**)

This doc sketches both. (1) shipped; (2) remains a possible follow-up if a concrete need for in-app pool control emerges.

---

## Option 1 — Kubernetes-native pre-warming (`Pool` CRD)

OpenSandbox ships a `Pool` custom resource, reconciled by the `opensandbox-controller` operator (installed via its Helm chart alongside the existing `opensandbox-server` deployment — `kubectl get crds` should list both `batchsandboxes.sandbox.opensandbox.io` and `pools.sandbox.opensandbox.io` once installed). A `Pool` keeps a warm buffer of pre-booted pods; when a real request comes in, the controller assigns it one of those pods instead of booting a fresh one.

### The manifests

```yaml
apiVersion: sandbox.opensandbox.io/v1alpha1
kind: Pool
metadata:
  name: sandbox-python-pool
  namespace: opensandbox
spec:
  capacitySpec:
    bufferMin: 2      # never let the warm buffer drop below 2 ready pods
    bufferMax: 5      # keep topping up to 5 ready-and-waiting pods
    poolMin: 0        # pool can shrink to 0 total nodes when idle
    poolMax: 20       # hard ceiling — the actual cost/blast-radius knob
  recycleStrategy:
    type: Delete      # explicit default — see "recycleStrategy choice" below
  template:
    metadata:
      labels: { app: sandbox-python }
    spec:
      tolerations:
        - operator: "Exists"
      # Must mirror whatever opensandbox-server's own batchsandbox_template_file
      # bootstraps today (execd/task-executor init containers) — copy the real
      # shape from the deployed opensandbox-server version rather than treating
      # this as literal, since the Pool pre-creates real pods independent of any
      # specific request and so can't rely on per-request template injection.
      initContainers:
        - name: task-executor-installer
          image: opensandbox/execd:v1.0.20 # <same task-executor/execd images opensandbox-server already uses>
          # ...
      containers:
        - name: sandbox
          image: xtmone/sandbox-python:0.4     # ← defines what this pool's warm pods run; independent of Sandbox.create()'s image arg, which poolRef mode ignores (see below)
          resources:
            requests: { cpu: "1", memory: "128Mi" }   # ← likewise independent of settings.sandbox_cpu/sandbox_ram — poolRef mode doesn't read resourceLimits from the call at all
---
apiVersion: sandbox.opensandbox.io/v1alpha1
kind: Pool
metadata:
  name: sandbox-nodejs-pool
  namespace: opensandbox
spec:
  capacitySpec: { bufferMin: 1, bufferMax: 3, poolMin: 0, poolMax: 10 }
  recycleStrategy: { type: Delete }
  template:
    spec:
      containers:
        - name: sandbox
          image: xtmone/sandbox-nodejs:0.4     # ← defines what this pool's warm pods run, independent of Sandbox.create()'s args (same as above)
          resources:
            requests: { cpu: "1", memory: "128Mi" }   # ← same independence as above
```

(`PoolSpec` fields verified against the upstream Go type,
[`kubernetes/apis/sandbox/v1alpha1/pool_types.go`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/apis/sandbox/v1alpha1/pool_types.go),
and the project's own sample manifest,
[`kubernetes/config/samples/sandbox_v1alpha1_pool.yaml`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/config/samples/sandbox_v1alpha1_pool.yaml).)

### How a plain `Sandbox.create()` call reaches a pool

**Correction (2026-08-07): the previous version of this section was wrong about the mechanism.** `Sandbox.create()` *does* have a way to bind to a pool — an `extensions: dict[str, str] | None` parameter, "passed through to the server as-is" (`opensandbox/sandbox.py:493,514`). Passing `extensions={"poolRef": "<pool-name>"}` is what routes a request to a `Pool`.

Server-side, `opensandbox-server`'s `BatchSandboxProvider.create_workload()` branches explicitly on this (`opensandbox_server/services/k8s/batchsandbox_provider.py:133-153`): if `extensions.poolRef` is set, it calls `_create_workload_from_pool(...)` (`:347-379`), which creates a `BatchSandbox` with `spec.poolRef = "<pool-name>"` — a **direct, name-based binding**, not predicate matching. The request validator (`opensandbox_server/api/schema.py:493-529`) enforces the two modes are mutually exclusive: with `poolRef` set, `image`/`resourceLimits`/`snapshotId` become optional (the `Pool`'s own template defines them instead, line 495-508), and `volumes`/`credentialProxy.enabled` are rejected (`batchsandbox_provider.py:139-143`, `schema.py:502-503`); without `poolRef`, `resourceLimits` is required (`schema.py:528-529`).

**Implemented (2026-08-07/08):** all three call sites now route through a shared helper, `_create_opensandbox()` in `backend/app/services/_sandbox_selector.py`, which looks up `settings.opensandbox_k8s_pool_names.get(pool_key or image_key)` and passes `extensions={"poolRef": pool_name} if pool_name else None` on every `Sandbox.create()` call (`_sandbox_selector.py:183,206`; called from `code_interpreter.py:587`, `custom_tool_executor.py:1243,1365`). `pool_key` exists as a parameter for a future deployment that wants to decouple pool naming from image naming, but no call site passes it today — pool lookup falls back to `image_key` (`"python"`/`"nodejs"`), the same key used for `OPENSANDBOX_IMAGES`.

When `OPENSANDBOX_K8S_POOL_NAMES` has no entry for a key (the default — empty by default, see `configuration.md`), `pool_name` is `None`, `extensions` is `None`, and `create_workload()` takes the non-`poolRef` branch (`:155-291`) exactly as before: a fully self-contained `BatchSandbox` with image/resources/init containers/volumes/egress embedded directly in `spec.template.spec`. Cold start is still the default; pooling is opt-in per key.

The upstream Go `opensandbox-controller` (`kubernetes/internal/controller/poolassign/`) is a separate component not vendored in this repo, so we still can't confirm from source here whether it opportunistically reassigns plain (non-`poolRef`) `BatchSandbox` objects to a matching warm `Pool` by image/resource equality — irrelevant to our own path either way, since we now bind explicitly via `poolRef` rather than relying on that.

**Still open**: what happens when `poolRef` names a `Pool` that doesn't exist or is exhausted (buffer empty, `poolMax` hit). That's controller/live-cluster behavior not visible in the Python code available here and needs to be checked against a running cluster — the code today doesn't special-case it (a bad/exhausted `poolRef` surfaces however the server/controller reports that failure, caught by the same try/except in `_create_opensandbox()` as any other creation failure).

**Observed (2026-11-01): an exhausted or cold Pool answers HTTP 504.** On the Copilot deployment a `sandbox_run_command` on `python-dev` failed after two attempts of about 180 s each (364 s in all): the server waits `[kubernetes] sandbox_create_timeout_seconds` (the helm-xtm-one chart renders `opensandbox.createTimeoutSeconds`, 180 by default; the server's own default is 60) for the pod to be Running with an IP, then answers 504 with `K8S_POD_READY_TIMEOUT` and the last pod state in its body. The dev Pools carry one warm pod (`buffer_max: 1`) and `pool_max: 4`, while a workspace holds its command container up to `max(idle, check timeout + 90 s)` (1890 s for an 1800 s check): past four live workspaces on one dev image the next create waits for capacity until the timeout. How to read it:

- `xtmone_sandbox_create_seconds{image_key, outcome, pooled}` records every attempt: `outcome="timeout"` on one key is that Pool out of warm pods; `outcome="ok"` with a high p95 is a cold node pulling the image.
- The `SandboxUnavailableError` the agent sees carries the server's code and message (`K8S_POD_READY_TIMEOUT: ... Last state: Pending`) and the seconds of each attempt, not a fixed default.
- A Pool also stops refilling when it holds dead pods: the controller (`PoolReconciler.scalePool`) counts every pod it owns that is not being deleted, so pods that ended (`Failed`, `Succeeded`, `Evicted` after node disk pressure) or stuck before Running (`Init:*`, `ContainerStatusUnknown`) fill `bufferMin` and are never replaced (Copilot, 6-7 Nov 2026: 8 hours of 504s on `python`). The worker's pool reaper (`backend/app/services/sandbox_pool_reaper.py`, adapter `sandbox_pool_reaper`, #9490) deletes them in `OPENSANDBOX_K8S_NAMESPACE` every minute and at once after a pooled create timed out, plus the `BatchSandbox` objects past `spec.expireTime`; `xtmone_sandbox_pool_reaped_total{kind, reason}` counts what it deleted. It needs `pods` and `batchsandboxes` get/list/delete in that namespace and is off (one log line) without them.
- A broken Pool never blocks a call (#9503): `_sandbox_selector._create_opensandbox` reads the Pool first (`pools/<name>` status `available` and its pods, `sandbox_pool_reaper.pool_starved`, 3 s, fail-open) and, when it has no pod available and none on its way to Running, creates the sandbox directly at once; a pooled create that times out (504) is retried once directly. The direct create carries the same image, resources, environment and default-deny network policy, without `poolRef`; one fallback per call; `xtmone_sandbox_pool_fallback_total{image_key, reason}` counts it (`starved` or `timeout`). The read also needs `pools` get in the namespace.
- The fix is on the Pool: raise `bufferMin`/`bufferMax` and `poolMax` for the key (FiligranHQ/platform `playbooks/internal/vars/xtmone-copilot.yml`, `copilot_opensandbox.pools`), not the create timeout, which only makes a caller wait longer for a pod that has no room.

### `opensandbox-server` config impact: **none**

Checked the authoritative config reference
([`server/configuration.md`](https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md)) —
the `[kubernetes]` section (`workload_provider`, `batchsandbox_template_file`, `image_pull_policy`, ...) has no pool-related field at all. Pool assignment is a pure `opensandbox-controller` reconciliation concern (predicate-matching `BatchSandbox` against `Pool` objects), fully decoupled from what `opensandbox-server` does. `opensandbox-server`'s job stays exactly what it is today: translate `Sandbox.create(...)` into a `BatchSandbox` CR via the existing `batchsandbox_template_file`, submit it, poll for readiness. Whatever `sandbox.k8s.toml` is already deployed needs **zero edits**.

RBAC is also already covered — `opensandbox-controller`'s Helm install registered both CRDs (and their RBAC) up front; nothing to re-grant.

### `recycleStrategy` choice

Default is `Delete` (pod torn down, fresh one built to refill the buffer). `Restart` reuses the same pod/filesystem across different requests — faster, but a real risk for this feature: custom-tool/code-interpreter sandboxes run arbitrary user/agent code, and reusing a filesystem across two unrelated executions is a state-leak vector between callers. **Recommend `Delete`** (as sketched above) — it preserves the stateless-per-call security posture the sandbox quota check (`specs/002-sandbox-quota-usage-tracking/`) and the rest of this integration already assume.

### Net cost of Option 1

The Python side shipped (see "Implemented" note above) — zero `opensandbox-server` config either. What's left to actually get warm pods: apply the two `Pool` YAML manifests sketched above to a target cluster and set `OPENSANDBOX_K8S_POOL_NAMES` to point at them (unset by default, so this is a pure opt-in — nothing changes for a deployment that never sets it). Same open question as before to verify against a live cluster before relying on it: behavior when the named pool is missing or exhausted.

---

## Option 2 — Client-side pooling (`opensandbox` SDK's `SandboxPoolAsync`)

The installed SDK (`opensandbox==0.1.12`) also ships a client-managed pool: `SandboxPoolAsync`/`SandboxPool`, with `acquire()`, a background reconciler that keeps `max_idle` warm sandboxes topped up (via ordinary `Sandbox.create()` calls done ahead of time, then `connect()`-ed to on `acquire()`), health checks, and graceful drain/shutdown.

### Code impact

- **New lifecycle management**: start each pool in the FastAPI startup lifespan, shut it down gracefully — same shape as existing Redis/DB pool management.
- **One pool per image**: `PoolCreationSpec` is one image/resource/network-policy per pool, so a pool each for `sandbox-python`, `sandbox-nodejs`, and any enriched domain image under `backend/sandbox/`.
- **The non-trivial piece**: the SDK's built-in `InMemoryAsyncPoolStateStore` keeps idle-sandbox membership in one process's memory. This backend runs 2 web workers per pod by default plus multiple replicas, so in-memory state desyncs immediately (duplicate warmups, orphaned VMs, inconsistent idle counts). We'd need a **Redis-backed implementation of the SDK's `AsyncPoolStateStore` protocol** — roughly 10 methods (idle set, a primary-lock-holder for the reconciler, TTL tracking) — the same class of problem the quota-flush advisory lock already solves for this codebase, just against Redis instead of a Postgres advisory lock.
- **The 3 call sites** swap `Sandbox.create(...)` for `pool.acquire(...)`; everything after (upload code, run, capture output) is unchanged since `acquire()` still returns a normal `Sandbox`.
- **The sandbox quota check is unaffected either way** — the per-user combined agentic+sandbox quota check (`check_agentic_quota`) runs at the top of each dispatch function regardless of whether dispatch is `create()` or `pool.acquire()`.

### Net cost of Option 2

A new Redis-backed coordination module, pool lifecycle wiring in app startup/shutdown, and per-image pool configuration — genuine engineering surface, in exchange for fine-grained in-app control (fail-fast-if-empty semantics, custom health checks/preparers, per-tenant pool sizing) that Option 1 can't give.

---

## Recommendation

**Option 1 — implemented.** It directly addresses the cold-start-latency / production-cost concern already on the table with the Platform team, at low engineering cost — the `extensions={"poolRef": ...}` wiring is in, opt-in per key via `OPENSANDBOX_K8S_POOL_NAMES`, no new Redis-coordination surface built or needed. Only remaining work to actually get warm pods is applying the `Pool` YAML manifests to a target cluster and setting that env var. Revisit Option 2 only if a concrete need for in-app pool control emerges (e.g. per-tenant warm-pool sizing, custom acquire semantics, fail-fast-if-empty) that a single named `poolRef` can't express.

---

## References

| Component | Description | Link |
|---|---|---|
| `Pool` CRD Go type | Authoritative `PoolSpec`/`CapacitySpec`/`RecycleStrategy` schema | [`kubernetes/apis/sandbox/v1alpha1/pool_types.go`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/apis/sandbox/v1alpha1/pool_types.go) |
| `Pool` sample manifest | Working example (task-executor/execd init containers, capacitySpec) | [`kubernetes/config/samples/sandbox_v1alpha1_pool.yaml`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/config/samples/sandbox_v1alpha1_pool.yaml) |
| `recycleStrategy: Restart` sample | Alternative recycle policy (not recommended here — see above) | [`kubernetes/config/samples/sandbox_v1alpha1_pool_restart.yaml`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/config/samples/sandbox_v1alpha1_pool_restart.yaml) |
| `poolRef` sample | Direct BatchSandbox→Pool binding — **this is our path**, reachable via `Sandbox.create(extensions={"poolRef": ...})`, see `batchsandbox_provider.py:133-153,347-379` | [`kubernetes/config/samples/sandbox_v1alpha1_pooled_batchsandbox.yaml`](https://github.com/opensandbox-group/OpenSandbox/blob/main/kubernetes/config/samples/sandbox_v1alpha1_pooled_batchsandbox.yaml) |
| Pool assignment predicates | Go controller package name-checked in earlier drafts of this doc; **unverified from this repo** — not vendored here, so its predicate-matching behavior for non-`poolRef` `BatchSandbox` objects is not confirmed | [`kubernetes/internal/controller/poolassign/`](https://github.com/opensandbox-group/OpenSandbox/tree/main/kubernetes/internal/controller/poolassign) |
| `opensandbox-server` config reference | Confirms no pool-related server config exists | [`server/configuration.md`](https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md) |
| SDK client-side pool | `SandboxPoolAsync`, `AsyncPoolStateStore` protocol | `opensandbox` PyPI package (installed: `0.1.12`), `opensandbox.pool_async` / `opensandbox.pool_types` |
| Original advocacy doc | Why OpenSandbox, architecture, resource/cold-start figures | `dev-docs/opensandbox-advocacy.md` |
| Sandbox quota check | Confirms this feature is orthogonal to either pooling option | `specs/002-sandbox-quota-usage-tracking/`, `backend/app/services/quota.py` |
