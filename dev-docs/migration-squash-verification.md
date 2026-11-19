# Migration squash — verification, recovery & rebase guide

This document covers the Alembic migration squash: how the single
baseline was validated, how to reproduce that validation yourself, how to
recover if an existing deployment fails to boot, and what to do when new
migrations land on `main` while the squash is in flight.

- **What changed:** the entire Alembic history was collapsed into a single
  baseline migration
  (`backend/alembic/versions/d046bcdf345f_baseline_schema.py`, revision
  `d046bcdf345f`, `down_revision = None`) generated from `Base.metadata`.
- **Operator upgrade steps** live in [`UPGRADING.md`](../UPGRADING.md). This doc
  is the developer-facing companion: *why* it is safe and *how* we proved it.

---

## 1. Why this needs careful testing

The squash is a **metadata-only** change: the database *schema* is identical
before and after; only Alembic's `alembic_version` bookkeeping row moves. That
sounds low-risk, but the failure mode is severe — if the baseline does **not**
faithfully reproduce the real production schema, then every fresh install from
now on is subtly wrong, and the error is invisible until something breaks at
runtime.

So the real question is not "does `alembic upgrade head` run?" — it is:

> Does the schema built by the new baseline **exactly** match the schema that
> real databases (built by the old migration chain) already have?

### The trap with `alembic --autogenerate`

Autogenerate only proves **baseline == `Base.metadata`**. It says nothing about
whether `Base.metadata` matches what a *real* historical database looks like.
If the old chain and the ORM models had drifted (a hand-written index, a column
default applied by a data migration, a leftover constraint), autogenerate would
happily report "0 drift" while the baseline silently differs from production.

That is why the strongest test compares the baseline against a **real database
built by the old history** — not against the models.

---

## 2. What "safe" means here — the layered tests

We validate at four increasing levels of confidence:

| Level | Question answered | How |
|-------|-------------------|-----|
| 1 | Does the baseline replay standalone from an empty DB? | `alembic upgrade head` on a fresh DB |
| 2 | Does the baseline match the ORM models? | `alembic revision --autogenerate` → 0 operations |
| 3 | Does the new baseline match a **real historical database** (prod/staging/dev snapshot)? | deep catalog fingerprint diff — **the safest test** |
| 4 | Does a DB stamped at the **last pre-squash head** auto-heal to the baseline with zero schema/row change? | `run_migrations()` on a stamped DB + fingerprint/row diff |

Level 3 is the one that actually protects the schema; level 4 proves the
fully-upgraded upgrade path itself. Levels 1–2 are necessary but not sufficient.

Level 1 is also locked into CI permanently: the `Sequential upgrade` step in
`.github/workflows/ci.yml` upgrades an empty database to each revision in
turn, in one process, until `current` == `head`, so every individual
`upgrade()` body — the baseline today, plus every post-squash migration added
on top — is executed in order whenever a merge group changes the chain. This was impossible before
the squash (the old chain's base revision was an `ALTER TABLE`, so any
upgrade-from-empty died immediately).

### The one nuance: schema-extras indexes

Two composite indexes on `knowledge_documents` — `(status, updated_at)` and
`(knowledge_base_id, status)` — are **not** in `Base.metadata`. They are created
at runtime by `_ensure_schema_extras()` in `app/core/migrations.py`
(a plain, in-place `CREATE INDEX IF NOT EXISTS`), deliberately outside Alembic.

Consequently the **full production schema = baseline + `_ensure_schema_extras()`**.
Any fingerprint comparison against a real DB must run *both* before diffing, or
it will report those two indexes as spurious differences. The verification
script handles this automatically.

---

## 3. Reproducible verification: the script

Everything above is automated in a single harness with clear, sectioned logs:

- `backend/scripts/verify_migration_squash.sh` — the 5-test orchestrator.
- `backend/scripts/schema_fingerprint.py` — a reusable, app-agnostic
  `pg_catalog` fingerprint/diff tool (stricter than autogenerate: it compares
  columns, types, defaults, PKs, FKs, unique/check constraints, full index
  definitions, enums, sequences, triggers and application functions — so the one
  artifact `create_all` does not manage (the `knowledge_documents` updated_at
  trigger + function) is validated too; extension-owned functions such as
  pgvector's are excluded as noise).

### Infrastructure: what you actually need

You do **not** need two servers or two clones.

- **One PostgreSQL server** with the `vector` extension available. The script
  creates several throwaway databases (all prefixed `sqsh_verify_`) and drops
  them on exit.
- **One second code checkout** *only* for the old migrations — and that is
  materialised cheaply with `git worktree`, not a second clone. The script does
  this for you automatically when it builds the oracle.
- **No manual oracle setup.** `oracle-diff` builds its reference DB from
  `origin/main` on its own. Optionally supply your own **real database** (a
  restored prod/staging snapshot) via `ORACLE_DB` to diff against that instead;
  it is only ever read.

### The five tests

The script's actual tests match the **fully-upgraded contract**: the only
"legacy" case is a DB parked at the single last pre-squash head.

| Test | What it proves | Needs |
|------|----------------|-------|
| **T1** | Baseline replays standalone from an empty DB (impossible before the squash). | — |
| **T2** | Baseline schema == `Base.metadata` (autogenerate drift == 0). | — |
| **T3** | New baseline + schema-extras == a **real historical DB** (deep fingerprint diff == 0) — the strongest schema check. | auto-built (`ORACLE_REF`) |
| **T4** | Disaster + recovery: a DB stamped at a deleted revision fails to boot, and `stamp --purge <baseline>` recovers it with zero data/DDL change. | — |
| **T5** | Fully-upgraded bridge: a DB stamped at the **last pre-squash head** (`PRE_SQUASH_FINAL_HEAD`) auto-heals to the baseline via `run_migrations()` with zero schema/row change. | — |

### Running it

```bash
cd backend
./scripts/verify_migration_squash.sh
```

That is all for the common case — the script auto-detects the baseline revision,
the last pre-squash head, and the oracle DB from dev defaults.

### Running it step by step (by hand)

The same script also runs **one step at a time** so you can watch each check and
verify it yourself. Pass a step name instead of running the whole thing:

```bash
./scripts/verify_migration_squash.sh <preflight|fresh-boot|model-drift|oracle-diff|disaster-recovery|bridge-heal|cleanup>
```

These map to tests **T1–T5** above: `fresh-boot` = T1, `model-drift` = T2,
`oracle-diff` = T3, `disaster-recovery` = T4, `bridge-heal` = T5.

Hand-run steps **keep their throwaway databases** so the next step can reuse them
(a full run drops them at the end). `model-drift` and `oracle-diff` read the DB that
`fresh-boot` builds, so run them after `fresh-boot`; `disaster-recovery` and
`bridge-heal` are self-contained. When you are done, drop the
`sqsh_verify_*` databases with the `cleanup` command.

Run this once in your shell (dev defaults), then run the steps below in order:

```bash
cd backend
export PGHOST=localhost PGPORT=5432 PGUSER=copilot PGPASSWORD=copilot_dev PGADMINDB=copilot
```

| # | Command | What it does | What to verify |
|---|---------|--------------|----------------|
| 0 | `./scripts/verify_migration_squash.sh preflight` | Env + working-tree sanity + `PRE_SQUASH_FINAL_HEAD` freshness. | `working tree has exactly one migration file` is **PASS**; `current head` is the baseline (`d046bcdf345f`) and `final head` is the last pre-squash head (`zk9o0p1q2r3s4`); `PRE_SQUASH_FINAL_HEAD (…) is the single Alembic head of origin/main` is **PASS** (see below). |
| 1 | `./scripts/verify_migration_squash.sh fresh-boot` | Creates an empty DB and runs `alembic upgrade head`. | `fresh DB reached current head d046bcdf345f` is **PASS**. Leaves `sqsh_verify_new` for the next steps. |
| 2 | `./scripts/verify_migration_squash.sh model-drift` | Autogenerate drift check against the models. | `model drift check produced no upgrade operations` is **PASS** (baseline == `Base.metadata`). If you see `database sqsh_verify_new not found`, run `fresh-boot` first. |
| 3 | `./scripts/verify_migration_squash.sh oracle-diff` | Deep fingerprint diff of the fresh baseline vs the historical schema. Builds the oracle automatically from `origin/main` (no setup needed). | `columns/constraints/indexes/enums/sequences/triggers/functions` all **OK** and `total differences: 0`. Set `ORACLE_DB=<db>` only to diff against an explicit DB (e.g. a restored prod snapshot). |
| 4 | `./scripts/verify_migration_squash.sh disaster-recovery` | Stamps a fake deleted revision, proves boot fails, then `stamp --purge` recovers. | `manual stamp --purge recovered with zero schema/row change` is **PASS**. |
| 5 | `./scripts/verify_migration_squash.sh bridge-heal` | Builds a fully-upgraded DB, rewinds the marker to the last pre-squash head, re-runs startup. | `last pre-squash head auto-healed to baseline d046bcdf345f (metadata-only)` is **PASS**. |
| 6 | `./scripts/verify_migration_squash.sh cleanup` | Drops all `sqsh_verify_*` databases. | `dropped sqsh_verify_new / _oracle / _disaster / _bridge`. |

Each step prints its own `RESULT: PASSED|FAILED` and exits `0`/`1`, so you can stop
and inspect the moment one fails.

**Preflight guards the frozen constant.** `PRE_SQUASH_FINAL_HEAD` is frozen in
`app/core/_pre_squash_revisions.py`, but `main` keeps moving until the squash
merges. If a migration lands on `main` after the freeze, real deployments will
be stamped at a **newer** head than the constant and every fully-upgraded
install fails closed at boot. A schema-changing late migration would also fail
T3 — but a **data-only** migration (a backfill) changes no models, produces no
fingerprint diff, and would slip through every other step. Preflight therefore
compares the constant against the actual Alembic head of `ORACLE_REF` (computed
by parsing the ref's `versions/` files) and **fails** on any mismatch: re-freeze
the constant and re-fold/rebase before shipping (see §5).

**The oracle is built automatically.** `oracle-diff` builds its reference DB
(`sqsh_verify_oracle`) from `ORACLE_REF` (default `origin/main`) in a throwaway
worktree, then diffs the fresh baseline against it. You do **not** need to build
anything by hand, and it will **never** silently fall back to your local dev DB.

Why it is built from the ref's fresh-install path (`create_all` + `stamp head`)
rather than by replaying the old migrations: the pre-squash chain is **not
standalone** — its base revision is an `ALTER TABLE`, so `alembic upgrade head`
on an empty DB fails immediately. `create_all` + `stamp` is exactly what a fresh
`origin/main` install ran, so it is the correct historical reference.

To diff against a **restored production/staging snapshot** instead (the strongest
possible check), or against a different ref, pass it explicitly:

```bash
ORACLE_DB=my_prod_snapshot ./scripts/verify_migration_squash.sh oracle-diff
ORACLE_REF=origin/release-x ./scripts/verify_migration_squash.sh oracle-diff
```

### Configuration (env vars, dev defaults shown)

```bash
PGHOST=localhost PGPORT=5432 PGUSER=copilot PGPASSWORD=copilot_dev \
PGADMINDB=copilot \                 # maintenance DB for CREATE/DROP DATABASE
ORACLE_DB= \                        # explicit oracle (e.g. prod snapshot); unset = auto-build
ORACLE_REF=origin/main \            # git ref the baseline's fresh-install schema must match
KEEP_DBS=0 \                        # set 1 to leave throwaway DBs for inspection
  ./scripts/verify_migration_squash.sh
```

- **Auto-build (default):** leave `ORACLE_DB` unset and the oracle is built from
  `ORACLE_REF` — reproducible, self-contained, and immune to local dev-DB drift.
- **Strongest guarantee:** set `ORACLE_DB` to a **restored production or staging
  snapshot**. Diffing the baseline against a real customer schema is the closest
  thing to a proof that the squash is safe.
- **`KEEP_DBS=1`** leaves the `sqsh_verify_*` databases in place so you can
  inspect them; otherwise everything is cleaned up on exit (including on error,
  via a `trap`).

### Reading the output

Each test prints a `PASS` / `FAIL` line, and the run ends with a summary and an
overall `RESULT: PASSED|FAILED` plus a matching exit code (0/1). There are no
soft skips: T3 is the strongest schema check in the harness, so a run that
cannot build (or reach) its oracle **fails** rather than silently degrading to
"everything else passed" — a green result always means every check actually ran.

The fingerprint diffs (T3, T5) print the exact catalog-level differences when
they are non-zero, so a failure tells you precisely which column/index/
constraint diverged.

---

## 4. Disaster recovery

### The failure mode

An existing deployment whose `alembic_version` points at a pre-squash revision
would, on startup, run `alembic upgrade head`, fail to find that revision, and
refuse to boot:

```
FAILED: Can't locate revision identified by '<old_revision>'
```

### Auto-heal (default — no manual intervention)

`run_migrations()` self-heals this automatically, but **fail-closed**. Before
upgrading, `_reconcile_pre_squash_revision()` classifies the current
`alembic_version` revision into one of three cases:

1. **Known to the current history** (a genuine pending migration) → left
   untouched; the normal `upgrade head` path handles it.
2. **Exactly the last pre-squash head** (`PRE_SQUASH_FINAL_HEAD` in
   `app/core/_pre_squash_revisions.py` — the single revision that was `head`
   immediately before the squash) → re-stamped to the **baseline** with
   `stamp --purge` and boot continues:

   ```
   Database is stamped at the last pre-squash head '<old_revision>' ...
   Auto-healing: re-stamping to the baseline 'd046bcdf345f' with purge
   (metadata-only, no DDL ...); any post-squash migrations remain pending ...
   Auto-heal complete — continuing startup normally.
   ```

   The stamp target is the baseline, **never `head`**: once post-squash
   migrations exist, `head` is baseline+N, and stamping it would silently mark
   those N migrations as applied without running their bodies. Stamping the
   baseline leaves them pending for the `upgrade head` that immediately
   follows — so a deployment jumps from the pre-squash head straight to
   squash+N in one boot.

3. **Any other revision** (an older/intermediate pre-squash revision, corrupted
   `alembic_version`, a database from a different product, a hand-edited/foreign
   marker) → the app **refuses to boot** with a clear error, so a human
   investigates instead of the app silently stamping a possibly-incomplete
   schema as "up to date".

The single-head check is the key safety choice: we only auto-heal the one
revision we *know* is schema-equivalent to the baseline. This turns the failure
mode for any unexpected marker from "silent wrong-schema" into "loud
refuse-to-boot" — the right behaviour for a migration. See §4.1 below.

**This is what makes air-gapped / on-prem upgrades painless** — operators just
deploy the new version and the app fixes its own version marker on first boot.
The auto-heal is covered by `tests/test_migration_orphaned_revision_heal.py`.

### 4.1 Why only the single last head — the fully-upgraded contract

The squash assumes the **fully-upgraded contract**: every deployment is on the
last pre-squash release before it installs the squashed version. That release
was stamped at exactly one Alembic head (`PRE_SQUASH_FINAL_HEAD`), which is
schema-equivalent to the baseline, so healing it is a pure metadata move.

An earlier design re-stamped **any** revision from a frozen 202-entry pre-squash
allow-list. That was both harder to maintain *and* unsafe: `create_all` only
creates *missing tables* — it never alters an existing one — so stamping a
database whose schema is not actually the baseline marks a subtly-wrong schema
as "up to date", and the error is invisible until something breaks at runtime.
The dangerous case the allow-list did **not** protect against was a database
stamped at an *intermediate* pre-squash revision (not fully upgraded before the
squash): it has an incomplete schema, and `create_all` + `_ensure_model_columns`
cannot replay dropped columns, constraint/index changes, type changes, or data
backfills.

Recognising only the single last head fixes both problems at once:

- An **intermediate** pre-squash revision now fails loud instead of being
  silently stamped — the operator must fully upgrade on the old code first.
- A **corrupted / foreign** marker is not the one head, so it also fails loud.
- There is no 202-entry set and no regenerate script to keep in lock-step with
  rebases.

The only correct fix for an under-upgraded database is to be **fully upgraded on
the old code before deploying the squash**. This is called out in
[`UPGRADING.md`](../UPGRADING.md).

### Manual recovery (fallback)

If you run Alembic directly (outside the startup wrapper) and hit the
"Can't locate revision" error, re-point `alembic_version` at the baseline. This
applies **no DDL** — a pure pointer move, no data risk:

```bash
cd backend
python -m alembic -c alembic/alembic.ini stamp --purge d046bcdf345f
```

`--purge` is **required**: a plain `alembic stamp` tries to resolve the current
(now-deleted) revision first and fails with the same error. `--purge` clears the
version table before stamping.

If you cannot run Alembic against the DB, the equivalent direct SQL is:

```sql
UPDATE alembic_version SET version_num = 'd046bcdf345f';
```

Both are proven by test **T4** in the harness. Full operator steps are in
[`UPGRADING.md`](../UPGRADING.md).

### Rolling back the squash itself

To revert the PR (go back to the old code), stamp the DB back at the revision
the old code expects, then deploy the old code. Again the schema never changed,
so this is a safe pointer move.

---

## 5. Rebase hazard: new migrations landing on `main`

**Question:** if someone merges a migration to `main` and I rebase, do I just
re-run the verification script?

**Answer: no.** Re-running alone is *insufficient* — the script is a **detector,
not a fixer**. A migration merged after you generated the baseline creates two
distinct problems:

1. **Orphaned migration → the whole Alembic graph breaks.** Their file has a
   `down_revision` pointing at a revision you deleted in the squash. Alembic can
   no longer build the revision map, so **every** alembic command
   (`heads`, `current`, `upgrade`) dies with a `KeyError`. The script's
   alembic-based tests (T1/T2/T4/T5) then fail immediately.

   > Note: the `test_alembic_single_head.py` unit test does **not** reliably
   > catch this — it passed even with a simulated orphan present. The real
   > breakage surfaces when actual alembic operations run (i.e. the verify
   > script), not from that unit test alone.

2. **Model drift.** Their migration ships with `app/models` changes. Those are
   now in `Base.metadata` but were **not** in your pre-generated baseline, so
   T2's autogenerate reports the added column/table.

3. **A stale `PRE_SQUASH_FINAL_HEAD` — the silent one.** If you *haven't*
   rebased (their file is not in your tree), nothing breaks locally: the graph
   is intact and, for a **data-only** migration (a backfill), even T2/T3 stay
   green because no model changed. But released deployments that ran their
   migration are now stamped at a head **newer** than the frozen constant, and
   on the squashed version every one of them fails closed at boot. The
   `preflight` step catches exactly this: it compares `PRE_SQUASH_FINAL_HEAD`
   against the actual Alembic head of `ORACLE_REF` and fails on any mismatch.

### The reconciliation workflow

1. **Delete the incoming migration file(s)** from `alembic/versions/`. Their DDL
   gets folded into the baseline.
2. **Fold their change into the baseline** — either regenerate the baseline from
   the now-merged `Base.metadata` (drop `d046bcdf345f`, autogenerate fresh
   against an empty DB) or hand-add their DDL. Keep `down_revision = None` and a
   single head.
3. **Update `PRE_SQUASH_FINAL_HEAD` if the merged migration becomes the new
   pre-squash head.** With the single-head strategy there is no allow-list to
   regenerate. Instead, decide which revision fully-upgraded deployments will be
   stamped at when they install the squash:

   - If you **fold the peer's migration into the baseline** and no released
     deployment ever ran it standalone, `PRE_SQUASH_FINAL_HEAD` is unchanged.
   - If a released deployment **already ran the peer's migration** (so its
     `alembic_version` is now the peer's revision), set `PRE_SQUASH_FINAL_HEAD`
     to that revision in `app/core/_pre_squash_revisions.py`, and make sure the
     baseline is schema-equivalent to that revision.

   There is only ever **one** value here — the single last released head — so
   keep it in lock-step with the baseline (step 2).
4. **Check for non-model artifacts.** If their migration did anything the ORM
   models don't express — partial/functional indexes, triggers, a
   schema-extras-style index, or a **data backfill** — autogenerate-from-metadata
   will silently drop it. Port those deliberately (runtime
   `_ensure_schema_extras`, manual baseline DDL, or keep a real follow-on
   migration for data).
5. **Refresh the oracle.** Your prod/dev snapshot predates their migration, so
   T3 will flag their new column as "only in baseline". Re-snapshot after their
   migration has deployed (or apply it to the oracle) so T3 is meaningful again.
6. **Re-run** the verify script **and**
   `pytest tests/test_migration_integrity.py tests/test_alembic_single_head.py tests/test_migration_orphaned_revision_heal.py`
   until green.

### Important: invariant #13 does not apply

The repo's migration invariant says "after rebasing onto a main that added
migrations, re-point your earliest migration's `down_revision` to main's new
head." That is for **normal stacked migrations**. It does **not** apply to the
squash: the baseline has `down_revision = None`, so you *regenerate* it — you do
not re-point it.

### Practical advice

Treat the squash as a lock-step operation. Any migration merged after you
generate the baseline invalidates it. Coordinate a short **migration freeze**,
or rebase-and-land the squash fast, to minimise the reconciliation window.

---

## 6. Keeping the old migrations around (archiving)

Short answer: **don't** keep them in an archived directory — it is mostly an
anti-pattern.

- Git already preserves the full history; nothing is lost.
- Alembic re-discovery is fragile — a stray archived directory can get scanned.
- CI runs `ruff check .` across the whole backend, so archived files would be
  linted (and they reference deleted models).

Instead:

- **Tag the pre-squash commit** so the old chain is trivially retrievable:

  ```bash
  git tag pre-alembic-squash <commit-before-squash>
  git push origin pre-alembic-squash
  ```

- The reusable DDL idioms from the old migrations already live in
  `app/core/migration_ddl.py` and are documented in `backend/AGENTS.md`.

---

## 7. Quick reference

```bash
# Full verification (from backend/)
./scripts/verify_migration_squash.sh

# Strongest guarantee: diff against a restored prod snapshot
ORACLE_DB=my_prod_snapshot ./scripts/verify_migration_squash.sh

# Inspect the throwaway DBs afterwards
KEEP_DBS=1 ./scripts/verify_migration_squash.sh

# Recover a deployment stuck on a deleted revision
python -m alembic -c alembic/alembic.ini stamp --purge d046bcdf345f

# Confirm state
python -m alembic -c alembic/alembic.ini current      # -> d046bcdf345f (head)
python -m alembic -c alembic/alembic.ini upgrade head  # no-op, exits 0
```
