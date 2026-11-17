"""What the Filigran AI Catalog hands another user of an MCP server.

A published MCP server travels to every user of the instance: its catalog page,
its download, an agent download, and the copy "Add to my account" writes into
the installer's account (which the owner export then reads back raw). Its
environment, headers and OAuth credentials never travel, but a credential is
just as often passed as a command-line argument or carried in the URL. Those
are masked by ``redact_secret_args`` / ``redact_secret_text``: the MCP API's
token rule, plus the value of a secret-named flag or ``NAME=value``, a URL's
password, token path segment and secret query values, and an
``Authorization`` scheme's token. Everything else is left byte for byte, so a
published server keeps working once the installer supplies their own secret.
"""

from __future__ import annotations

import pytest

from app.core.utils import (
    MaskedArgumentError,
    mask_secret_arg,
    redact_secret_args,
    redact_secret_text,
    restore_masked_args,
    restore_masked_text,
)

TOKEN = "ghp_" + "a" * 36


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--token", "glpat-short"], ["--token", "***"]),
        ([f"--token={TOKEN}"], ["--token=***"]),
        (["--api-key=sk-short"], ["--api-key=***"]),
        (["--client-secret", "s3cret", "--port", "8080"], ["--client-secret", "***", "--port", "8080"]),
        (["-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}"], ["-e", "GITHUB_PERSONAL_ACCESS_TOKEN=***"]),
        (["AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI"], ["AWS_SECRET_ACCESS_KEY=***"]),
        (["PGPASSWORD=hunter2"], ["PGPASSWORD=***"]),
        (["--header", "Authorization: Bearer eyJ.payload.sig"], ["--header", "Authorization: Bearer ***"]),
        (["postgresql://app:S3cret@db/prod"], ["postgresql://app:***@db/prod"]),
        (["--dsn=postgresql://u:p@h:5432/db"], ["--dsn=postgresql://u:***@h:5432/db"]),
        # The MCP API's own token rule still applies to a bare argument.
        (["-y", "server", TOKEN], ["-y", "server", mask_secret_arg(TOKEN)]),
    ],
)
def test_a_credential_in_an_argument_is_masked(args, expected):
    assert redact_secret_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
        ["--max-tokens", "4096", "--token-limit", "10", "--signature-version", "v4"],
        ["--token-file", "/run/secrets/token", "TOKEN_URL=https://idp.example.com/token"],
        ["AWS_ACCESS_KEY_ID=AKIDEXAMPLE", "--region", "eu-west-1"],
        ["--no-auth", "--port", "8080", "--mode=read-only"],
        ["-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
        ["--token", "--verbose"],
        [42, None, "plain"],
    ],
)
def test_an_argument_that_carries_no_credential_is_left_exactly_as_is(args):
    assert redact_secret_args(args) == args


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{TOKEN}@github.com/org/repo.git", "https://***@github.com/org/repo.git"),
        ("https://user:pass@mcp.example.com/sse", "https://user:***@mcp.example.com/sse"),
        ("https://actions.zapier.com/mcp/sk-ak-XYZ/sse", "https://actions.zapier.com/mcp/***/sse"),
        (
            "https://mcp.example.com/sse?api_key=abc&region=eu&apiKey=def",
            "https://mcp.example.com/sse?api_key=***&region=eu&apiKey=***",
        ),
    ],
)
def test_a_credential_in_a_url_is_masked(url, expected):
    assert redact_secret_text(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/sse",
        "https://mcp.example.com/v1/tenants/123e4567-e89b-12d3-a456-426614174000/sse",
        "https://mcp.example.com/sse?region=eu&limit=10",
        "https://host/path?",
        "ssh://git@github.com/org/repo.git",
        None,
    ],
)
def test_a_url_that_carries_no_credential_is_left_exactly_as_is(url):
    assert redact_secret_text(url) == url


def test_anything_but_a_list_of_arguments_is_returned_unchanged():
    assert redact_secret_args(None) is None
    assert redact_secret_args("--token x") == "--token x"


# ---------------------------------------------------------------------------
# restore_masked_args: what an edit sends back as it read it keeps the secret
# ---------------------------------------------------------------------------
# The MCP API shows every server's arguments through ``redact_secret_args``
# (``_to_response``), so an edit that sends them back as read must get each
# stored value back, and a mask that cannot be put back is refused, never saved.

#: Another token with the same first characters, hence the same mask.
OTHER = "ghp_aa" + "b" * 34
NEW = "ghp_" + "c" * 36
LONG = "x" * 40

#: One stored list per masked form the API shows.
MASKED_FORMS = {
    "bare token": ["-y", "srv", TOKEN],
    "long bare value": [LONG, "--port", "1"],
    "NAME=value": ["-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}"],
    "--flag=value": [f"--token={TOKEN}", "--port", "1"],
    "--flag value": ["-y", "srv", "--token", "glpat-short"],
    "two secret flags": ["--token", "t0ken", "--client-secret", "s3cret"],
    "one secret flag twice": ["--token", "t0ken", "--token", "other"],
    "masked flag and its value": ["--some-very-long-option-api-token", "v4lue"],
    "URL password": ["postgresql://app:S3cret@db/prod"],
    "URL in NAME=value": ["--dsn=postgresql://u:p@h:5432/db"],
    "URL query secret": ["https://mcp.example.com/sse?token=abc123&mode=ro"],
    "Authorization scheme": ["--header", "Authorization: Bearer eyJ.payload.sig"],
    "two tokens sharing a mask": [TOKEN, "-e", OTHER],
}


@pytest.mark.parametrize("stored", MASKED_FORMS.values(), ids=MASKED_FORMS.keys())
def test_every_masked_form_sent_back_as_shown_keeps_the_stored_value(stored):
    shown = redact_secret_args(stored)
    assert shown != stored
    assert restore_masked_args(shown, stored) == stored
    # An argument inserted before them moves every one: still restored.
    assert restore_masked_args(["--verbose", *shown], stored) == ["--verbose", *stored]


@pytest.mark.parametrize(
    ("incoming", "stored", "expected"),
    [
        # Each bare ``***`` is told apart by the flag before it, so the pairs
        # may swap places.
        (
            ["--client-secret", "***", "--token", "***"],
            ["--token", "t0ken", "--client-secret", "s3cret"],
            ["--client-secret", "s3cret", "--token", "t0ken"],
        ),
        # A masked NAME=value moved to the front.
        (
            ["GITHUB_PERSONAL_ACCESS_TOKEN=***", "-e"],
            ["-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}"],
            [f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}", "-e"],
        ),
        # Two tokens sharing a mask, both still sent: restored in order.
        (
            ["--verbose", mask_secret_arg(TOKEN), "-e", mask_secret_arg(OTHER)],
            [TOKEN, "-e", OTHER],
            ["--verbose", TOKEN, "-e", OTHER],
        ),
        # The same value twice is one stored value: no ambiguity.
        ([mask_secret_arg(TOKEN)], [TOKEN, TOKEN], [TOKEN]),
        # A stored literal shown like a masked one competes for its place:
        # both sent back keep both, in order.
        (
            [mask_secret_arg(TOKEN), mask_secret_arg(TOKEN)],
            [TOKEN, mask_secret_arg(TOKEN)],
            [TOKEN, mask_secret_arg(TOKEN)],
        ),
        (["--token", "***", "--token", "***"], ["--token", "t0ken", "--token", "***"],
         ["--token", "t0ken", "--token", "***"]),
        (["--token", "***"], ["--token", "same", "--token", "same"], ["--token", "same"]),
        # A genuinely changed value is saved as typed.
        (["-y", "srv", NEW], ["-y", "srv", TOKEN], ["-y", "srv", NEW]),
        (["--token", "n3w"], ["--token", "t0ken"], ["--token", "n3w"]),
        (["--token=n3w"], [f"--token={TOKEN}"], ["--token=n3w"]),
        # A ``***`` the server stores as is, beside a masked one, is kept.
        (["--glob", "***", "--token", "***"], ["--glob", "***", "--token", "t0ken"],
         ["--glob", "***", "--token", "t0ken"]),
        # Every value shown alike replaced: no mask left to put back.
        ([NEW], [TOKEN, mask_secret_arg(TOKEN)], [NEW]),
        ([NEW, "-e", NEW], [TOKEN, "-e", OTHER], [NEW, "-e", NEW]),
        # A stored literal equal to a token's mask, after the token: the order
        # is the stored one.
        (
            [mask_secret_arg(TOKEN), "-e", mask_secret_arg(TOKEN)],
            [mask_secret_arg(TOKEN), "-e", TOKEN],
            [mask_secret_arg(TOKEN), "-e", TOKEN],
        ),
        # A mask-shaped literal with no stored token behind it: kept.
        ([mask_secret_arg(TOKEN)], [mask_secret_arg(TOKEN)], [mask_secret_arg(TOKEN)]),
        # Nothing stored, or nothing stored masked: unchanged.
        ([mask_secret_arg(TOKEN)], None, [mask_secret_arg(TOKEN)]),
        ([mask_secret_arg(TOKEN)], [], [mask_secret_arg(TOKEN)]),
        (["--token", "***"], ["-y", "srv"], ["--token", "***"]),
        (["-y", "srv"], ["-y", "old"], ["-y", "srv"]),
        ([], [TOKEN], []),
    ],
)
def test_a_masked_argument_sent_back_keeps_its_stored_value(incoming, stored, expected):
    assert restore_masked_args(incoming, stored) == expected


@pytest.mark.parametrize(
    ("incoming", "stored"),
    [
        # Two tokens sharing a mask, one dropped: which one is left cannot be
        # told; one more than stored neither.
        (["--verbose", mask_secret_arg(TOKEN)], [TOKEN, "-e", OTHER]),
        ([mask_secret_arg(TOKEN), "-e", NEW], [TOKEN, "-e", OTHER]),
        ([mask_secret_arg(TOKEN)] * 3, [TOKEN, "-e", OTHER]),
        # A token and a stored literal equal to its mask, one dropped.
        ([mask_secret_arg(TOKEN)], [TOKEN, mask_secret_arg(TOKEN)]),
        (["--verbose", mask_secret_arg(TOKEN), "-e", NEW], [TOKEN, mask_secret_arg(TOKEN)]),
        ([mask_secret_arg(TOKEN)] * 3, [TOKEN, mask_secret_arg(TOKEN)]),
        (["--token", "***"], ["--token", "t0ken", "--token", "***"]),
        # The same flag twice with two values, one pair dropped.
        (["--token", "***"], ["--token", "t0ken", "--token", "other"]),
        # A bare ``***`` that no longer follows its flag.
        (["--other", "***"], ["--token", "t0ken"]),
        (["***", "--token"], ["--token", "t0ken"]),
        (["--token-v2", "***"], ["--token", "t0ken"]),
        # A value edited around its mask, or a mask of nothing stored.
        (["postgresql://app:***@db2/prod"], ["postgresql://app:S3cret@db/prod"]),
        (["GH_TOKEN=***"], ["GITHUB_TOKEN=t0ken"]),
        (["--header", "Authorization: Bearer *** "], ["--header", "Authorization: Bearer eyJ.p.s"]),
        (["ghp_zz***"], [TOKEN]),
        # The token mask of earlier releases, after a flag that now shows ``***``.
        (["--token", mask_secret_arg(TOKEN)], ["--token", TOKEN]),
        # The edit form splits an argument holding a space: its ``***`` is
        # left after another word.
        (["--header", "Authorization:", "Bearer", "***"], ["--header", "Authorization: Bearer eyJ.p.s"]),
    ],
)
def test_a_mask_that_cannot_be_put_back_to_one_stored_value_is_refused(incoming, stored):
    with pytest.raises(MaskedArgumentError):
        restore_masked_args(incoming, stored)


@pytest.mark.parametrize(
    ("incoming", "stored", "expected"),
    [
        # Sent back as shown: the stored URL.
        ("https://user:***@mcp.example.com/sse", "https://user:pw@mcp.example.com/sse",
         "https://user:pw@mcp.example.com/sse"),
        ("https://mcp.example.com/sse?token=***", "https://mcp.example.com/sse?token=abc",
         "https://mcp.example.com/sse?token=abc"),
        # Retyped, cleared, or nothing stored masked: as sent.
        ("https://user:n3w@mcp.example.com/sse", "https://user:pw@mcp.example.com/sse",
         "https://user:n3w@mcp.example.com/sse"),
        ("", "https://user:pw@mcp.example.com/sse", ""),
        ("https://other.example.com/***", "https://mcp.example.com/sse", "https://other.example.com/***"),
        ("https://mcp.example.com/sse", None, "https://mcp.example.com/sse"),
        (None, "https://user:pw@mcp.example.com/sse", None),
    ],
)
def test_a_masked_url_sent_back_keeps_the_stored_url(incoming, stored, expected):
    assert restore_masked_text(incoming, stored) == expected


def test_a_url_edited_around_its_mask_is_refused():
    with pytest.raises(MaskedArgumentError):
        restore_masked_text(
            "https://user:***@other.example.com/sse", "https://user:pw@mcp.example.com/sse"
        )
