# SPDX-License-Identifier: Apache-2.0

"""The database identities of the platform startup, shared by the code that
takes them (``migrations``) and the code that reports on them
(``startup_watchdog``, ``schema_readiness``)."""

# Arbitrary fixed ID for the distributed migration lock.
# pg_advisory_lock serializes migrations across multiple uvicorn workers.
MIGRATION_LOCK_ID = 294837

# Distinct fixed ID for the distributed startup-bootstrap lock (seeding,
# env provisioning, admin upsert).  MUST differ from MIGRATION_LOCK_ID so
# the migration winner releasing its lock doesn't accidentally unblock a
# bootstrap waiter mid-flight.
BOOTSTRAP_LOCK_ID = 294838

# The Alembic connection's ``application_name``: the startup watchdog finds the
# migration backend by it in ``pg_stat_activity``, and so can an operator.
MIGRATION_APPLICATION_NAME = "xtm-one-migrations"
