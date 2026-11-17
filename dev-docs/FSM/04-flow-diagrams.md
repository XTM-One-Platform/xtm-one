# Flow Diagrams

## Execution lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: create_execution
    pending --> running: claim_due (lease, attempt++)
    pending --> cancelled: cancel
    running --> completed: adapter COMPLETED
    running --> failed: adapter FAILED (non-retryable)
    running --> pending: adapter FAILED (retry) / lease expired reclaim
    running --> dead_letter: deadline exceeded OR attempts exhausted
    running --> running: AWAIT (release lease, set adapter_state)
    failed --> pending: requeue_execution (operator)
    dead_letter --> pending: requeue_execution (operator)
    completed --> [*]
    failed --> [*]
    dead_letter --> [*]
    cancelled --> [*]
```

## Dispatch + recovery

```mermaid
flowchart TD
    subgraph web [Web process - 60s housekeeping tick]
        cron["scheduled assignments / evaluations"] --> ce["create_execution"]
        manual["manual trigger / webhook / event poller"] --> ce
        evalapi["evaluations API / agent-change"] --> ce
        hitl["HITL respond / eval callback"] --> ce2["emit_outbox_event / create_execution"]
    end
    ce --> tbl[("orchestration_executions")]
    ce2 --> obtbl[("orchestration_outbox_events")]
    subgraph worker [Worker process - reconciler loop, ~2s + Redis wake]
        recon["reconciler cycle"] --> sweep["sweep_deadlines -> on_dead_letter"]
        recon --> reclaim["reclaim_expired_leases"]
        recon --> claim["claim_due -> supervised _run_execution"]
        recon --> periodic["adapter.periodic (eval episode timeouts)"]
        recon --> drive["claim_outbox_events -> adapter.handle_event"]
    end
    tbl --> recon
    obtbl --> recon
    claim --> exec["adapter.execute (heartbeat + asyncio.timeout)"]
    exec --> term["complete / fail / await"]
```

## HITL resume (assignment)

1. `execute_assignment_run` pauses → run `awaiting_human` → adapter returns
   AWAIT → execution stays `running` (lease released), no deadline.
2. User responds → `resume_assignment_run` emits a `resume` outbox event on the
   execution (found by `dedup_key=assignment_run:<run_id>`).
3. Reconciler claims the event → `AssignmentAdapter.handle_event` re-arms the
   execution (`running → pending`).
4. `claim_due` re-runs `execute_assignment_run` under a fresh lease; it resumes
   from the pause and completes (or AWAITs again).
