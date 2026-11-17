#!/usr/bin/env python3
"""Mint a Filigran support code.

Two kinds (``--kind``):

* ``agent_failure_pool_recharge`` (default) - every agent failure pool on the
  platform starts full again;
* ``platform_quota_reset`` - the platform-wide usage counters of the current
  windows start again from zero, e.g. for a proof of concept that used the
  license's allowance before the evaluation ended. ``--quota-types``
  and ``--periods`` narrow it; left out, the platform resets ``agentic`` and
  ``sandbox`` in the current daily, monthly and yearly windows.

Run this on the **issuing** side — wherever the product signing key lives
(filigran-licenses). The key never touches a customer platform: the platform
only ever verifies, against the public half already embedded in
``app/enterprise/xtm_ca.py``.

    python scripts/mint_support_code.py \\
        --key /secure/xtm-product-key.pem \\
        --platform 3f2b… \\
        --days 30 \\
        --note "SUPPORT-4821 — provider outage 2026-08-24"

    python scripts/mint_support_code.py \\
        --key /secure/xtm-product-key.pem \\
        --platform 3f2b… \\
        --days 7 \\
        --kind platform_quota_reset \\
        --periods daily monthly \\
        --note "POC-118 — Acme evaluation"

Prints one line: the code to paste into Settings → Quotas. ``--platform``
should be the customer's ``PLATFORM_ID``; omit it (or pass ``global``) only for
a code any platform may redeem, which is a deliberately blunt instrument.

The payload is signed as the exact bytes printed here, so the verifier never
has to agree with this script on a canonical JSON form — reformat this file
freely, it cannot break an already-issued code.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PREFIX = "XTMP-"
KIND = "agent_failure_pool_recharge"
PLATFORM_QUOTA_RESET_KIND = "platform_quota_reset"
KINDS = (KIND, PLATFORM_QUOTA_RESET_KIND)
# What a reset may name - the platform refuses anything else.
QUOTA_TYPES = ("agentic", "sandbox", "llm_proxy")
PERIODS = ("daily", "monthly", "yearly")


def _b64url(raw: bytes) -> str:
    """Base64url without padding — the shape the verifier accepts."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key", required=True, help="PEM private key of the product signing keypair"
    )
    parser.add_argument(
        "--key-password",
        default=None,
        help="Passphrase of the private key, if it is encrypted",
    )
    parser.add_argument(
        "--platform",
        default="global",
        help="Customer PLATFORM_ID this code is valid for ('global' for any)",
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Validity of the code itself, in days"
    )
    parser.add_argument("--note", default=None, help="Support reference, shown to the admin")
    parser.add_argument(
        "--kind", choices=KINDS, default=KIND, help="What the code grants"
    )
    parser.add_argument(
        "--quota-types",
        nargs="+",
        choices=QUOTA_TYPES,
        default=None,
        help=f"{PLATFORM_QUOTA_RESET_KIND} only: the counters to reset "
        "(default: agentic sandbox)",
    )
    parser.add_argument(
        "--periods",
        nargs="+",
        choices=PERIODS,
        default=None,
        help=f"{PLATFORM_QUOTA_RESET_KIND} only: the current windows to reset "
        "(default: all three)",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Code id (max 64 chars). Defaults to a fresh UUID; this is what "
        "makes the code single-use, so never reuse one.",
    )
    args = parser.parse_args()
    if args.kind != PLATFORM_QUOTA_RESET_KIND and (args.quota_types or args.periods):
        parser.error(f"--quota-types and --periods apply to --kind {PLATFORM_QUOTA_RESET_KIND}")

    with open(args.key, "rb") as handle:
        private_key = serialization.load_pem_private_key(
            handle.read(),
            password=args.key_password.encode() if args.key_password else None,
        )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        print("The signing key must be RSA (the license trust anchor is RSA).", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    payload = {
        "v": 1,
        "kind": args.kind,
        "id": (args.id or str(uuid.uuid4()))[:64],
        "platform": args.platform or "global",
        "iat": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "exp": (now + timedelta(days=args.days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    if args.note:
        payload["note"] = args.note[:500]
    if args.quota_types:
        payload["quota_types"] = sorted(set(args.quota_types))
    if args.periods:
        payload["periods"] = sorted(set(args.periods))

    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = private_key.sign(payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    print(f"{PREFIX}{_b64url(payload_bytes)}.{_b64url(signature)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
