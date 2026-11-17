"""Bedrock Claude ids resolve to the price of the endpoint they call.

Bedrock serves Claude as ``anthropic.claude-...`` model ids and through
inference profiles prefixed by a geography (``us.``, ``eu.``, ``apac.``,
``au.``, ``jp.``, ``us-gov.``) or ``global.`` - ids from AWS's model cards.
The seed priced Claude only under bare ``claude-...`` ids, so no Bedrock Claude
call was ever costed. Reads the real seed file.

From Claude Sonnet 4.5, Haiku 4.5 and Opus 4.5 on, for every later model too,
Bedrock prices its global endpoint (``global.`` profiles) at Anthropic's list
price and its regional endpoints (the in-region ``anthropic.`` model ids and
every geography's profile) 10% above it, on every token price. Earlier models
keep one price on every endpoint. Anthropic's pricing page, "Regional and
multi-region endpoint pricing for Claude 4.5 models and beyond":
https://platform.claude.com/docs/en/about-claude/pricing
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.tracing import cost_calculator, price_loader
from app.services.tracing.cost_calculator import _match_price
from app.tools import run_budget

_SEED = price_loader.load_model_prices_payload(price_loader.MODEL_PRICES_PATH)
# An optional inference-profile prefix: us., eu., apac., us-gov., global., ...
_PROFILE = r"(?:[a-z]+(?:-[a-z]+)?\.)?"
_REGIONAL_PREMIUM = Decimal("1.1")
_ENDPOINT_PREFIXES = ("", "us.", "eu.", "jp.", "au.", "apac.", "us-gov.", "global.")
# Retired on the Claude API but still served, and billed, on Bedrock: no
# Anthropic row to derive them from.
_BEDROCK_ONLY = {"anthropic.claude-opus-4-1-20250805"}


def _rows(provider: str | None = None) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(**item.model_dump(), is_custom=False)
        for item in _SEED.prices
        if provider is None or item.provider == provider
    ]


def _prices(price) -> tuple:
    return (
        price.input_price_per_1m,
        price.output_price_per_1m,
        price.cache_read_price_per_1m,
        price.cache_write_price_per_1m,
    )


def _with_regional_premium(prices: tuple) -> tuple:
    return tuple(None if value is None else value * _REGIONAL_PREMIUM for value in prices)


def _has_regional_premium(anthropic_model_id: str) -> bool:
    """Claude Sonnet 4.5, Haiku 4.5, Opus 4.5 and every later model.

    ``claude-<family>-<major>[-<minor>]``: a date is not a minor version, and
    the ``claude-3-5-haiku`` naming only ever named earlier models.
    """
    match = re.match(r"claude-[a-z]+-(\d+)(?:-(\d)(?!\d))?", anthropic_model_id)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (4, 5)


def _matching_rows(rows, model_id: str) -> list:
    """Every row that could price ``model_id``.

    The database hands the rows to the lookup custom first, then by
    ``effective_from`` and ``created_at``, never in seed order: a seeded price
    must not depend on which of two matching rows comes first.
    """
    return [
        row
        for row in rows
        if row.model_id == model_id or (row.model_pattern and re.search(row.model_pattern, model_id))
    ]


def _is_bedrock_claude(row) -> bool:
    return row.provider == "bedrock" and re.match(r"(global\.)?anthropic\.claude-", row.model_id) is not None


def _expected_bedrock_rows(anthropic_row) -> dict[str, tuple[str, tuple]]:
    """``model_id -> (model_pattern, prices)`` of the Bedrock rows an Anthropic row requires."""
    body = r"anthropic\." + anthropic_row.model_pattern.removeprefix("^")
    list_prices = _prices(anthropic_row)
    if not _has_regional_premium(anthropic_row.model_id):
        return {f"anthropic.{anthropic_row.model_id}": ("^" + _PROFILE + body, list_prices)}
    return {
        f"global.anthropic.{anthropic_row.model_id}": (r"^global\." + body, list_prices),
        f"anthropic.{anthropic_row.model_id}": (
            r"^(?!global\.)" + _PROFILE + body,
            _with_regional_premium(list_prices),
        ),
    }


@pytest.fixture
def seeded_lookup(monkeypatch):
    """The lookups the cost callers use, reading the seed instead of the database."""

    async def _load_prices(_db, provider):
        return _rows(provider)

    async def _load_all_prices(_db):
        return _rows()

    monkeypatch.setattr(cost_calculator, "_load_prices", _load_prices)
    monkeypatch.setattr(cost_calculator, "_load_all_prices", _load_all_prices)


def test_every_anthropic_row_has_bedrock_rows_priced_by_endpoint():
    """The seed carries the Bedrock rows as data. This keeps each one derived
    from the Anthropic row of the same model - its pattern behind the endpoint's
    prefix, the list prices on the global endpoint (on every endpoint for an
    earlier model), exactly 1.1x on the regional ones, its ``effective_from`` -
    so a price change cannot reach one side only, and a new Anthropic model
    cannot ship without its Bedrock rows."""
    bedrock = {row.model_id: row for row in _SEED.prices if _is_bedrock_claude(row)}
    expected: set[str] = set()
    for row in _SEED.prices:
        if row.provider != "anthropic":
            continue
        assert row.model_pattern and row.model_pattern.startswith("^"), row.model_id
        for model_id, (pattern, prices) in _expected_bedrock_rows(row).items():
            expected.add(model_id)
            twin = bedrock.get(model_id)
            assert twin is not None, f"no Bedrock row {model_id}"
            assert twin.model_pattern == pattern, model_id
            assert _prices(twin) == prices, model_id
            assert twin.effective_from == row.effective_from, model_id
    assert set(bedrock) - _BEDROCK_ONLY == expected, "a Bedrock Claude row has no Anthropic row"
    for model_id in _BEDROCK_ONLY:
        # One row on every endpoint: only an earlier model can go without its twins.
        assert not _has_regional_premium(model_id.removeprefix("anthropic.")), model_id
        assert bedrock[model_id].model_pattern.startswith("^" + _PROFILE + r"anthropic\."), model_id


@pytest.mark.parametrize("prefix", _ENDPOINT_PREFIXES)
def test_each_endpoint_of_each_model_is_priced_by_exactly_one_row(prefix):
    """No regional row can price a ``global.`` id, no global row a regional one,
    and no row of another provider overlaps either."""
    rows = _rows()
    models = [row.model_id for row in _SEED.prices if row.provider == "anthropic"]
    models += [model_id.removeprefix("anthropic.") for model_id in sorted(_BEDROCK_ONLY)]
    for model in models:
        model_id = f"{prefix}anthropic.{model}"
        if prefix == "global." and _has_regional_premium(model):
            expected = f"global.anthropic.{model}"
        else:
            expected = f"anthropic.{model}"
        assert [match.model_id for match in _matching_rows(rows, model_id)] == [expected], model_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_lookup")
@pytest.mark.parametrize(
    ("bedrock_id", "anthropic_id", "regional"),
    [
        # Claude 4.5 and later: the global endpoint at the list price, every
        # regional one (a geography's profile, the in-region model id) at 1.1x.
        ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929", False),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929", True),
        ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929", True),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929", True),
        ("global.anthropic.claude-opus-4-6-v1", "claude-opus-4-6", False),
        ("jp.anthropic.claude-opus-4-6-v1", "claude-opus-4-6", True),
        ("apac.anthropic.claude-opus-4-6-v1", "claude-opus-4-6", True),
        ("au.anthropic.claude-opus-4-5-20251101-v1:0", "claude-opus-4-5-20251101", True),
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001", False),
        ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001", True),
        ("global.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6", False),
        ("us.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6", True),
        ("global.anthropic.claude-sonnet-5", "claude-sonnet-5", False),
        ("anthropic.claude-sonnet-5", "claude-sonnet-5", True),  # BedrockProvider.default_model
        ("global.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5", False),
        ("anthropic.claude-sonnet-5-5", "claude-sonnet-5-5", True),
        ("global.anthropic.claude-opus-5-5", "claude-opus-5-5", False),
        ("us-gov.anthropic.claude-opus-5-5", "claude-opus-5-5", True),
        ("global.anthropic.claude-fable-5-1", "claude-fable-5-1", False),
        ("eu.anthropic.claude-fable-5", "claude-fable-5", True),
        ("anthropic.claude-mythos-preview", "claude-mythos-preview", True),
        # Earlier models: the list price on every endpoint, the global one included.
        ("global.anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4-20250514", False),
        ("us.anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4-20250514", False),
        ("eu.anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4-20250514", False),
        ("anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4-20250514", False),
        ("apac.anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4-20250514", False),
        ("us.anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku-20241022", False),
        ("anthropic.claude-opus-4-20250514-v1:0", "claude-opus-4-20250514", False),
    ],
)
async def test_a_bedrock_claude_id_is_priced_by_its_endpoint(bedrock_id, anthropic_id, regional):
    list_price = await cost_calculator.resolve_model_price(object(), anthropic_id, "anthropic")
    assert list_price is not None, anthropic_id
    expected = _with_regional_premium(_prices(list_price)) if regional else _prices(list_price)

    # Looked up under the Bedrock provider (what the run budget does) ...
    bedrock = await cost_calculator.resolve_model_price(object(), bedrock_id, "bedrock")
    assert bedrock is not None, bedrock_id
    assert _prices(bedrock) == expected
    # ... and provider-agnostic (traces carry no provider tag).
    anywhere = await cost_calculator.resolve_model_price_any(object(), bedrock_id)
    assert anywhere is not None and anywhere.provider == "bedrock"
    assert _prices(anywhere) == expected
    assert len(_matching_rows(_rows(), bedrock_id)) == 1


@pytest.mark.parametrize(
    ("model_id", "row_id"),
    [
        ("claude-sonnet-4-0", "claude-sonnet-4-20250514"),
        ("claude-sonnet-4-20250514", "claude-sonnet-4-20250514"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-6"),
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ],
)
def test_a_bare_claude_id_still_resolves_to_the_anthropic_row(model_id, row_id):
    """Sonnet 4 and Sonnet 4.5+ have an Anthropic row each (Bedrock prices their
    regional endpoints apart) at the same list price, so every bare Sonnet 4.x
    id costs what it did when one row priced them all."""
    rows = _rows()
    matches = _matching_rows(rows, model_id)
    assert [(match.provider, match.model_id) for match in matches] == [("anthropic", row_id)]
    sonnet_4 = next(row for row in rows if row.model_id == "claude-sonnet-4-20250514")
    assert _prices(matches[0]) == _prices(sonnet_4)


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_lookup")
@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("us.amazon.nova-2-lite-v1:0", "nova-2-lite"),
        ("global.amazon.nova-2-pro-v1:0", "nova-2-pro"),
        ("amazon.nova-2-lite-v1:0", "nova-2-lite"),
        ("nova-2-pro", "nova-2-pro"),
    ],
)
async def test_nova_inference_profiles_resolve(model_id, expected):
    # The Nova rows are seeded under ``amazon``: the run budget's ``bedrock``
    # lookup misses and falls back to the provider-agnostic one.
    price = await run_budget.resolve_budget_price(object(), model_id=model_id, provider="bedrock")
    assert price is not None and price.model_id == expected


# Anthropic's official list prices, https://docs.anthropic.com/en/docs/about-claude/pricing
# (read 2026-09-27; Sonnet 5.5 read 2026-09-29): input, output, cache hits,
# 5-minute cache writes per 1M.
_OFFICIAL = {
    "claude-sonnet-5-5": ("2", "10", "0.2", "2.5"),
    "claude-sonnet-5": ("2", "10", "0.2", "2.5"),
    "claude-opus-5-5": ("4", "20", "0.2", "5"),
    "claude-opus-5": ("5", "25", "0.5", "6.25"),
    "claude-fable-5-1": ("10", "50", "0.25", "12.5"),
    "claude-fable-5": ("10", "50", "1", "12.5"),
    "claude-mythos-5-1": ("10", "50", "0.25", "12.5"),
    "claude-mythos-5": ("10", "50", "1", "12.5"),
}


def _official(model: str) -> tuple:
    return tuple(Decimal(value) for value in _OFFICIAL[model])


@pytest.mark.parametrize(
    ("model_id", "model", "regional"),
    [
        ("claude-sonnet-5-5", "claude-sonnet-5-5", False),
        ("claude-sonnet-5-5-20260928", "claude-sonnet-5-5", False),
        ("claude-sonnet-5", "claude-sonnet-5", False),
        ("claude-opus-5-5", "claude-opus-5-5", False),
        ("claude-opus-5-5-20260922", "claude-opus-5-5", False),
        ("claude-opus-5", "claude-opus-5", False),
        ("claude-fable-5-1", "claude-fable-5-1", False),
        ("claude-fable-5", "claude-fable-5", False),
        ("claude-mythos-5-1", "claude-mythos-5-1", False),
        ("claude-mythos-5", "claude-mythos-5", False),
        ("global.anthropic.claude-sonnet-5", "claude-sonnet-5", False),
        ("anthropic.claude-sonnet-5", "claude-sonnet-5", True),
        ("global.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5", False),
        ("anthropic.claude-sonnet-5-5", "claude-sonnet-5-5", True),
        ("global.anthropic.claude-opus-5-5", "claude-opus-5-5", False),
        ("us.anthropic.claude-opus-5-5", "claude-opus-5-5", True),
        ("us.anthropic.claude-fable-5-1", "claude-fable-5-1", True),
        ("anthropic.claude-mythos-5-1", "claude-mythos-5-1", True),
    ],
)
def test_current_claude_models_carry_the_official_list_price(model_id, model, regional):
    """Opus 5.5, Fable 5.1 and Mythos 5.1 fell under the Opus 5 / Fable 5 /
    Mythos 5 patterns (Opus 5.5 at $5 / $25 instead of $4 / $20, the 5.1
    cache hits at $1 instead of $0.25), and Sonnet 5 kept its launch-era
    $3 / $15 after Anthropic made $2 / $10 the standard price. A Bedrock
    regional endpoint adds its 10% to the official price."""
    price = _match_price(_rows(), model_id)
    assert price is not None, model_id
    official = _official(model)
    assert _prices(price) == (_with_regional_premium(official) if regional else official), model_id


@pytest.mark.parametrize(
    ("model_id", "row_id"),
    [
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("claude-sonnet-5-5-20260928", "claude-sonnet-5-5"),
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("claude-sonnet-5-20260601", "claude-sonnet-5"),
    ],
)
def test_sonnet_5_5_is_priced_by_a_row_of_its_own(model_id, row_id):
    """Sonnet 5.5 lists at Sonnet 5's price, so a price check alone passes while
    the Sonnet 5 pattern swallows it."""
    assert [match.model_id for match in _matching_rows(_rows(), model_id)] == [row_id]


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-opus-4-1-20250805-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "eu.anthropic.claude-opus-4-1-20250805-v1:0",
    ],
)
def test_claude_opus_4_1_on_bedrock_is_priced(model_id):
    """Retired on the Claude API (August 5, 2026) but Legacy on Bedrock until
    its January 8, 2027 EOL, so Bedrock runs still bill it: $15 / $75, cache
    hits $1.50, 5-minute cache writes $18.75 (Anthropic's list price)."""
    for rows in (_rows("bedrock"), _rows()):
        price = _match_price(rows, model_id)
        assert price is not None, model_id
        assert _prices(price) == tuple(Decimal(v) for v in ("15", "75", "1.5", "18.75"))


def test_models_retired_everywhere_are_not_added():
    """Claude Opus 4.1 on the Claude API and Claude 3.7 Sonnet (retired on the
    Claude API February 19, 2026, past its Bedrock EOL of July 30, 2026) are
    deliberately left without a row: nothing serves them any more."""
    assert _match_price(_rows(), "claude-opus-4-1-20250805") is None
    assert _match_price(_rows(), "claude-3-7-sonnet-20250219") is None
    assert _match_price(_rows(), "anthropic.claude-3-7-sonnet-20250219-v1:0") is None


@pytest.mark.asyncio
async def test_seeding_is_idempotent_and_never_overwrites_a_custom_row(monkeypatch):
    custom = SimpleNamespace(
        provider="bedrock",
        model_id="anthropic.claude-sonnet-5",
        model_pattern="^anthropic\\.claude-sonnet-5$",
        input_price_per_1m=1,
        output_price_per_1m=2,
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        is_custom=True,
    )
    existing: list[object] = [custom]

    async def _fake_existing(_db):
        return list(existing)

    monkeypatch.setattr(price_loader, "_load_existing_prices", _fake_existing)

    class _DB:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, obj: object) -> None:
            self.added.append(obj)

        async def commit(self) -> None:
            return None

    first = _DB()
    synced, skipped = await price_loader.sync_model_prices(first, payload=_SEED)
    assert skipped == 1, "the admin's Bedrock row is left alone"
    assert custom.input_price_per_1m == 1
    added_bedrock = [row for row in first.added if row.provider == "bedrock"]
    assert added_bedrock, "the Bedrock rows are seeded"

    # A second sync over what the first one wrote adds nothing: it rewrites
    # every seeded row in place, which it counts like an insert.
    existing.extend(first.added)
    second = _DB()
    synced_again, _ = await price_loader.sync_model_prices(second, payload=_SEED)
    assert second.added == []
    assert synced_again == synced
