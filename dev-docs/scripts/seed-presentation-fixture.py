#!/usr/bin/env python3
"""Load the synthetic presentation template package for local testing.

The package lives in ``dev-docs/presentation-template-package/``: one template
container, two synthetic SVG assets, and three deck specs (valid / edited /
deliberately broken). See that directory's README for what each file is for.

Three independent modes, smallest infrastructure first:

  validate  Run the real ``PresentationSpec`` validator over a deck file and
            print the result. Needs the backend importable — no database, no
            server, no LLM. Use it to see the exact error an agent would get.

  template  Create the template container and upload its assets through the REST
            API. Needs a running platform and a JWT. Stdlib only.

  deck      Insert a deck row straight into the database through the same
            ``validate_spec`` + ``save_presentation`` path the tool uses, so the
            renderer can be exercised without spending an LLM call. Needs the
            backend environment (DATABASE_URL) — NOT a substitute for testing
            the tool itself, which is the only supported way decks are created
            in production.

Examples:
  # No infrastructure: see the validator's error report for the broken fixture
  PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py \\
      validate --deck deck-invalid.json

  # Container + assets via REST (token from the browser devtools / API key flow)
  python3 dev-docs/scripts/seed-presentation-fixture.py template \\
      --base-url http://localhost:4000 --token "$JWT"

  # A renderable deck for a given user, straight into the DB
  PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py deck \\
      --user-email you@example.com --deck deck-valid.json

  # Then edit it in place (same row, revision + 1) to check the deck updates
  PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py deck \\
      --user-email you@example.com --deck deck-edited.json --presentation-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_PACKAGE = Path(__file__).resolve().parents[1] / "presentation-template-package"


# ── shared helpers ───────────────────────────────────────────────────────────


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"error: no such file: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON: {exc}")


def _resolve_deck(package: Path, deck: str) -> Path:
    """Accept either a bare fixture name or a path."""
    candidate = Path(deck)
    return candidate if candidate.exists() else package / deck


def _require_backend() -> None:
    """Fail with an actionable message when the backend is not importable."""
    try:
        import app.services.presentation_spec  # noqa: F401
    except ImportError:
        sys.exit(
            "error: cannot import the backend. Run from the repository root with\n"
            "  PYTHONPATH=backend python3 dev-docs/scripts/seed-presentation-fixture.py ...\n"
            "(and the backend virtualenv active, so pydantic/sqlalchemy resolve)."
        )


# ── mode: validate ───────────────────────────────────────────────────────────


def cmd_validate(args: argparse.Namespace) -> int:
    _require_backend()
    from app.services.presentation_spec import PresentationSpecError, validate_spec

    path = _resolve_deck(args.package, args.deck)
    payload = _load_json(path)
    try:
        spec = validate_spec(payload)
    except PresentationSpecError as exc:
        # This is verbatim what the tool returns to the agent as a tool error.
        print(f"REJECTED — {path.name}\n")
        print(exc)
        return 1

    print(f"VALID — {path.name}")
    print(
        f"  {spec['title']!r}: {len(spec['slides'])} slides, theme {spec['theme']}, "
        f"{spec['aspect_ratio']}, {spec['transition']} transition"
    )
    for index, slide in enumerate(spec["slides"], start=1):
        title = slide.get("title") or slide.get("subtitle") or "(untitled)"
        print(f"  {index}. [{slide['layout']}] {title}")
    return 0


# ── mode: template (REST) ────────────────────────────────────────────────────


def _api_request(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    url = f"{args.base_url.rstrip('/')}{path}"
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {args.token}")
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - local dev URL
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        sys.exit(f"error: {method} {path} -> HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: cannot reach {url}: {exc.reason}")
    return json.loads(raw) if raw else None


def _multipart(field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body (stdlib has no helper for this)."""
    boundary = f"----fixture{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            payload,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


#: Asset file -> structured ``asset_role`` (see ASSET_ROLES in the model).
_ASSET_ROLES = {
    "synthetic-logo.svg": "logo",
    "synthetic-background.svg": "background",
}


def cmd_template(args: argparse.Namespace) -> int:
    container_payload = _load_json(args.package / "template-container.json")
    container = _api_request(
        args,
        "POST",
        "/api/v1/template-containers",
        body=json.dumps(container_payload).encode("utf-8"),
        content_type="application/json",
    )
    container_id = container["id"]
    print(f"created template container {container_id} ({container['name']!r})")

    for asset_path in sorted((args.package / "assets").glob("*")):
        role = _ASSET_ROLES.get(asset_path.name, "other")
        body, content_type = _multipart("file", asset_path.name, asset_path.read_bytes())
        asset = _api_request(
            args,
            "POST",
            f"/api/v1/template-containers/{container_id}/assets?asset_role={role}",
            body=body,
            content_type=content_type,
        )
        print(f"  uploaded {asset['filename']} as {role} (id {asset['id']})")

    print(
        "\nNext: enable the 'Template Search' and 'Presentation Generation' built-in "
        "tools on an agent, then ask it to build a deck from this template.\n"
        f"  template_id: {container_id}"
    )
    return 0


# ── mode: deck (direct DB insert) ────────────────────────────────────────────


def cmd_deck(args: argparse.Namespace) -> int:
    _require_backend()
    import asyncio

    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.presentation import Presentation
    from app.models.user import User
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentations import load_writable_presentation, save_presentation

    path = _resolve_deck(args.package, args.deck)
    payload = _load_json(path)

    async def _run() -> int:
        async with async_session() as db:
            result = await db.execute(select(User).where(User.email == args.user_email))
            user = result.scalar_one_or_none()
            if user is None:
                sys.exit(f"error: no user with email {args.user_email!r}")

            existing: Presentation | None = None
            if args.presentation_id:
                try:
                    target = uuid.UUID(args.presentation_id)
                except ValueError:
                    sys.exit("error: --presentation-id must be a UUID")
                # The same gate the tool uses: owner or a config-tier grantee.
                existing = await load_writable_presentation(db, target, user_id=user.id)
                if existing is None:
                    sys.exit(
                        f"error: {args.presentation_id} is not a deck {args.user_email} may edit"
                    )

            try:
                deck = await save_presentation(
                    db,
                    user_id=user.id,
                    spec_payload=payload,
                    conversation_id=None,
                    presentation=existing,
                )
            except PresentationSpecError as exc:
                print(f"REJECTED — {path.name}\n")
                print(exc)
                return 1
            deck_id, revision, slides = deck.id, deck.revision, deck.slide_count
            await db.commit()

        verb = "updated" if existing is not None else "created"
        print(f"{verb} deck {deck_id} — revision {revision}, {slides} slides")
        print(f"  viewer:  /presentations/{deck_id}")
        print(f"  chat:    paste [[PRESENTATION:{deck_id}]] into an assistant message")
        if existing is None:
            print(
                "\nTo test the in-place edit, re-run with:\n"
                f"  --deck deck-edited.json --presentation-id {deck_id}"
            )
        return 0

    return asyncio.run(_run())


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--package",
        type=Path,
        default=DEFAULT_PACKAGE,
        help=f"fixture directory (default: {DEFAULT_PACKAGE})",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_validate = sub.add_parser("validate", help="validate a deck spec, no infrastructure")
    p_validate.add_argument("--deck", default="deck-valid.json")
    p_validate.set_defaults(func=cmd_validate)

    p_template = sub.add_parser("template", help="create the container + assets via REST")
    p_template.add_argument("--base-url", default="http://localhost:4000")
    p_template.add_argument("--token", required=True, help="JWT of the owning user")
    p_template.set_defaults(func=cmd_template)

    p_deck = sub.add_parser("deck", help="insert a renderable deck row directly")
    p_deck.add_argument("--user-email", required=True, help="owner of the deck")
    p_deck.add_argument("--deck", default="deck-valid.json")
    p_deck.add_argument(
        "--presentation-id",
        help="edit this existing deck in place instead of creating one",
    )
    p_deck.set_defaults(func=cmd_deck)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
