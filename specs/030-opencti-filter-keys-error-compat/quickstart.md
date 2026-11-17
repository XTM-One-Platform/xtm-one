# Quickstart: validate the hint

From `backend/`:

```bash
.venv/bin/python -m pytest tests/test_opencti_list_entities_filter_keys.py -q
```

Expected: every hint case passes for the old code with 500 and the new code with 400.

Lint (blocking gate):

```bash
.venv/bin/ruff check app tests
```

Live check, against an OpenCTI that carries PR #19096 (once merged): list entities with filter key `labels`; expect OpenCTI's error followed by a hint naming `objectLabel`. Repeat against an older OpenCTI.
