"""One GraphQL query against a lab OpenCTI, from inside the lab (``[check.lab-opencti-graphql]``).

Run with ``sandbox_run_command(check="lab-opencti-graphql", service_id=<the lab>)``: the command runs in the lab's
workspace container, next to the lab's services on the pod's ``localhost``, where a check container has no route.

    --count TYPE        the count of an entity type (Report, Malware, Indicator...): ``stixCoreObjects(types: [TYPE])``
    --query TEXT        a GraphQL document, answered as JSON (``--variables`` a JSON object)
    --base URL          the OpenCTI (default http://127.0.0.1:4000, the lab-opencti service; 4100 for lab-opencti-pr-head)
    --token TOKEN       the API token (default the lab admin token of lab-opencti; the pr-head lab's is detected by port)

Exit 0 with the JSON answer, 1 on a GraphQL error, 2 when the OpenCTI does not answer. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

#: The lab admin tokens declared by the lab services (APP__ADMIN__TOKEN): lab-opencti / lab-opencti-ctem on 4000,
#: lab-opencti-pr-head's backend on 4100 (opencti_pr_head.ADMIN_TOKEN). Lab-only values, never a real credential.
LAB_TOKENS = {
    4000: "00000000-0000-4000-8000-000000000001",
    4100: "00000000-0000-4000-8000-000000000002",
}
DEFAULT_BASE = "http://127.0.0.1:4000"
COUNT_QUERY = "query LabCount($types: [String]) { stixCoreObjects(types: $types, first: 1) { pageInfo { globalCount } } }"


def _port(base: str) -> int:
    from urllib.parse import urlparse

    parsed = urlparse(base)
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _post(base: str, token: str, query: str, variables: dict, timeout: float) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        base.rstrip("/") + "/graphql",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the lab's own localhost
        return json.loads(response.read().decode() or "{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    asked = parser.add_mutually_exclusive_group(required=True)
    asked.add_argument("--count", metavar="TYPE")
    asked.add_argument("--query", metavar="TEXT")
    parser.add_argument("--variables", default="{}")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    token = args.token or LAB_TOKENS.get(_port(args.base), LAB_TOKENS[4000])
    if args.count:
        query, variables = COUNT_QUERY, {"types": [args.count]}
    else:
        try:
            variables = json.loads(args.variables)
        except json.JSONDecodeError as exc:
            print(f"--variables is not JSON: {exc}", file=sys.stderr)
            return 2
        query = args.query
    try:
        answer = _post(args.base, token, query, variables, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"the OpenCTI at {args.base} did not answer: {exc}", file=sys.stderr)
        return 2
    if args.count and not answer.get("errors"):
        total = ((answer.get("data") or {}).get("stixCoreObjects") or {}).get("pageInfo", {}).get("globalCount")
        print(json.dumps({"type": args.count, "count": total}))
        return 0
    print(json.dumps(answer, indent=2)[:20000])
    return 1 if answer.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
