"""The bundled price seed: file formats, the LiteLLM conversion, the refresh script and the append-only plan.

No network: the refresh script reads a recorded LiteLLM sample, or a fake fetch.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from specivo.core.ai_pricing_seed import (
    FIRST_PERIOD_START,
    PRICE_COLUMNS,
    SNAPSHOT_FORMAT,
    Backfill,
    HistoryPeriod,
    SeedFormatError,
    SeedPrice,
    Snapshot,
    StoredSeedRow,
    litellm_to_snapshot,
    load_history,
    load_snapshot,
    parse_history,
    parse_snapshot,
    plan_seed,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "tests" / "fixtures" / "ai_model_prices" / "litellm-sample.json"
AS_OF = datetime(2026, 11, 6, 6, 29, 17, tzinfo=UTC)
LATER = AS_OF + timedelta(days=30)


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "refresh_ai_model_prices", ROOT / "scripts" / "refresh_ai_model_prices.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _price(model: str = "alpha-1", provider: str = "testprov", **prices: str) -> SeedPrice:
    values = {"input": "3", "output": "15", **prices}
    return SeedPrice(
        provider=provider,
        model=model,
        currency="USD",
        prices={c: (Decimal(values[c]) if c in values else None) for c in PRICE_COLUMNS},
    )


def _stored(
    price: SeedPrice,
    row_id: int,
    start: datetime,
    end: datetime | None = None,
    *,
    active: bool = True,
) -> StoredSeedRow:
    return StoredSeedRow(
        id=row_id,
        provider=price.provider,
        model=price.model,
        currency=price.currency,
        prices=price.row_prices(),
        long_context_threshold=price.long_context_threshold,
        aliases=price.aliases,
        effective_from=start,
        effective_to=end,
        is_active=active,
    )


def _snapshot(*prices: SeedPrice, as_of: datetime = AS_OF) -> Snapshot:
    return Snapshot(as_of=as_of, source={}, prices=tuple(prices))


# ---------------------------------------------------------------------------
# The committed files
# ---------------------------------------------------------------------------


def test_committed_snapshot_parses_and_names_its_source() -> None:
    snapshot = load_snapshot()
    assert snapshot.as_of.tzinfo is not None
    assert len(snapshot.prices) > 100
    assert snapshot.source["license"] == "MIT"
    assert "BerriAI/litellm" in snapshot.source["url"]
    providers = {p.provider for p in snapshot.prices}
    assert {"anthropic", "openai", "gemini"} <= providers
    assert all(p.currency == "USD" for p in snapshot.prices)


def test_committed_history_file_parses() -> None:
    assert isinstance(load_history(), tuple)


def test_a_missing_history_file_has_no_periods(tmp_path: Path) -> None:
    assert load_history(tmp_path / "absent.json") == ()


# ---------------------------------------------------------------------------
# LiteLLM conversion
# ---------------------------------------------------------------------------


def _convert() -> tuple[dict, object]:
    raw = json.loads(SAMPLE.read_text())
    return litellm_to_snapshot(raw, as_of=AS_OF, source={"license": "MIT"})


def test_litellm_conversion_keeps_first_party_text_models() -> None:
    document, report = _convert()
    keys = [(m["provider"], m["model"]) for m in document["models"]]
    assert keys == [
        ("anthropic", "claude-haiku-4-5"),
        ("anthropic", "claude-sonnet-4-5"),
        ("deepseek", "deepseek-chat"),
        ("gemini", "gemini-2.5-pro"),
        ("mistral", "mistral-tiny"),
        ("openai", "gpt-5-codex"),
        ("openai", "o3-mini"),
    ]
    assert document["as_of"] == "2026-11-06T06:29:17Z"
    assert document["format"] == SNAPSHOT_FORMAT
    assert report.kept == 7  # type: ignore[attr-defined]


def test_litellm_conversion_maps_prices_per_million() -> None:
    document, _ = _convert()
    by_model = {m["model"]: m for m in document["models"]}
    assert by_model["claude-sonnet-4-5"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "input": "3",
        "output": "15",
        "cache_write_5m": "3.75",
        "cache_write_1h": "6",
        "cache_read": "0.3",
    }
    # No 1h price in the source: the snapshot leaves it out; the loader applies 2x input.
    assert "cache_write_1h" not in by_model["claude-haiku-4-5"]
    assert by_model["o3-mini"]["reasoning"] == "4.4"
    assert by_model["gemini-2.5-pro"] == {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "input": "1.25",
        "output": "10",
        "cache_read": "0.125",
        "long_context_threshold": 200000,
        "long_input": "2.5",
        "long_output": "15",
        "long_cache_read": "0.25",
    }


def test_litellm_conversion_prefers_the_plain_key_and_reports_rounding() -> None:
    document, report = _convert()
    by_model = {m["model"]: m for m in document["models"]}
    assert by_model["deepseek-chat"]["input"] == "0.28"
    assert report.skipped_duplicates == ["deepseek/deepseek-chat"]  # type: ignore[attr-defined]
    assert by_model["mistral-tiny"]["input"] == "1.234568"
    assert report.rounded == ["mistral/mistral-tiny:input_cost_per_token"]  # type: ignore[attr-defined]


def test_refresh_script_writes_a_loadable_snapshot_offline(tmp_path: Path) -> None:
    out = tmp_path / "prices.json"
    script = _script()

    def no_network(url: str) -> bytes:
        raise AssertionError(f"unexpected fetch of {url}")

    code = script.main(
        ["--source", str(SAMPLE), "--as-of", "2026-11-06T13:29:17+07:00", "--commit", "abc123", "--output", str(out)],
        fetch=no_network,
    )
    assert code == 0
    snapshot = load_snapshot(out)
    assert snapshot.as_of == AS_OF  # the Asia/Bangkok offset is converted to UTC
    assert snapshot.source["commit"] == "abc123"
    assert snapshot.source["url"].endswith("/blob/abc123/model_prices_and_context_window.json")
    assert len(snapshot.prices) == 7
    # One model per line, so refresh diffs stay reviewable.
    assert sum(1 for line in out.read_text().splitlines() if line.strip().startswith('{"provider"')) == 7


def test_refresh_script_downloads_through_the_given_fetch(tmp_path: Path) -> None:
    out = tmp_path / "prices.json"
    script = _script()
    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        if "api.github.com" in url:
            return json.dumps([{"sha": "f00d", "commit": {"committer": {"date": "2026-11-06T06:29:17Z"}}}]).encode()
        return SAMPLE.read_bytes()

    assert script.main(["--output", str(out)], fetch=fake_fetch) == 0
    assert "api.github.com" in calls[0] and calls[1].endswith("/f00d/model_prices_and_context_window.json")
    assert load_snapshot(out).as_of == AS_OF


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------


def _doc(**overrides: object) -> dict:
    base: dict = {
        "format": 1,
        "as_of": "2026-11-06T06:29:17Z",
        "currency": "USD",
        "models": [{"provider": "testprov", "model": "alpha-1", "input": "3", "output": "15"}],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "document",
    [
        _doc(format=2),
        _doc(as_of="2026-11-06T06:29:17"),  # no offset
        _doc(models=[{"provider": "testprov", "model": "Alpha-1", "input": "3", "output": "15"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": "3"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": 3.5, "output": "15"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": "-1", "output": "15"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": "0.0000001", "output": "15"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": "3", "output": "15", "long_input": "6"}]),
        _doc(models=[{"provider": "Test Prov", "model": "alpha-1", "input": "3", "output": "15"}]),
        _doc(models=[{"provider": "testprov", "model": "alpha-1", "input": "3", "output": "15", "currency": "ZZZ"}]),
        _doc(models=[{"provider": "testprov", "model": "a", "input": "1", "output": "1"}] * 2),
    ],
)
def test_bad_snapshots_are_refused(document: dict) -> None:
    with pytest.raises(SeedFormatError):
        parse_snapshot(document)


def test_history_periods_must_be_closed_and_not_overlap() -> None:
    item = {"provider": "testprov", "model": "alpha-1", "input": "4", "output": "20"}
    ok = parse_history(
        {
            "periods": [
                {**item, "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-06-01T00:00:00Z"},
                {**item, "effective_from": "2026-06-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z"},
            ]
        }
    )
    assert len(ok) == 2
    with pytest.raises(SeedFormatError):
        parse_history({"periods": [{**item, "effective_from": "2026-01-01T00:00:00Z"}]})
    with pytest.raises(SeedFormatError):
        parse_history(
            {"periods": [{**item, "effective_from": "2026-06-01T00:00:00Z", "effective_to": "2026-01-01T00:00:00Z"}]}
        )
    with pytest.raises(SeedFormatError):
        parse_history(
            {
                "periods": [
                    {**item, "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-06-02T00:00:00Z"},
                    {**item, "effective_from": "2026-06-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z"},
                ]
            }
        )


# ---------------------------------------------------------------------------
# The append-only plan
# ---------------------------------------------------------------------------


def test_one_hour_cache_write_is_twice_input_when_missing() -> None:
    values = _price(cache_write_5m="3.75").row_prices()
    assert values["cache_write_1h"] == Decimal("6")
    explicit = _price(cache_write_1h="5").row_prices()
    assert explicit["cache_write_1h"] == Decimal("5")
    long_price = SeedPrice(
        provider="testprov",
        model="alpha-1",
        currency="USD",
        prices={"input": Decimal("3"), "output": Decimal("15"), "long_input": Decimal("6")},
        long_context_threshold=200_000,
    )
    assert long_price.row_prices()["long_cache_write_1h"] == Decimal("12")


def test_a_new_model_starts_its_first_period_in_the_past() -> None:
    plan = plan_seed([], _snapshot(_price()))
    assert [(p.price.model, p.effective_from, p.effective_to) for p in plan.inserts] == [
        ("alpha-1", FIRST_PERIOD_START, None)
    ]
    assert plan.closes == {} and plan.rebases == {}
    assert plan.backfills == [Backfill(provider="testprov", model="alpha-1", until=AS_OF)]


def test_a_new_model_with_seed_rows_or_history_starts_at_as_of() -> None:
    closed = _stored(_price(), 1, FIRST_PERIOD_START, AS_OF)
    plan = plan_seed([closed], _snapshot(_price(input="2"), as_of=LATER))
    [insert] = plan.inserts
    assert insert.effective_from == LATER and not plan.backfills

    earlier = HistoryPeriod(price=_price(input="4"), effective_from=AS_OF - timedelta(days=90), effective_to=AS_OF)
    plan = plan_seed([], _snapshot(_price()), [earlier])
    assert sorted((p.effective_from, p.from_history) for p in plan.inserts) == [
        (earlier.effective_from, True),
        (AS_OF, False),
    ]
    assert not plan.backfills


def test_a_new_model_priced_earlier_by_an_admin_starts_at_as_of() -> None:
    key = ("testprov", "alpha-1")
    plan = plan_seed([], _snapshot(_price()), admin_starts={key: AS_OF - timedelta(days=5)})
    assert [p.effective_from for p in plan.inserts] == [AS_OF] and not plan.backfills
    later_admin = plan_seed([], _snapshot(_price()), admin_starts={key: LATER})
    assert [p.effective_from for p in later_admin.inserts] == [FIRST_PERIOD_START]


def test_an_unchanged_price_is_a_no_op() -> None:
    price = _price()
    plan = plan_seed([_stored(price, 1, FIRST_PERIOD_START)], _snapshot(price, as_of=LATER))
    assert plan.is_empty and plan.unchanged == 1 and not plan.backfills


def test_a_first_period_stored_from_an_as_of_moves_back_once() -> None:
    old, new = _price(), _price(input="2.5")
    stored = [_stored(old, 7, AS_OF, LATER), _stored(new, 8, LATER)]
    plan = plan_seed(stored, _snapshot(new, as_of=LATER))
    assert plan.rebases == {7: FIRST_PERIOD_START}
    assert plan.backfills == [Backfill(provider="testprov", model="alpha-1", until=AS_OF)]
    assert not plan.inserts and not plan.closes and plan.unchanged == 1
    moved = [_stored(old, 7, FIRST_PERIOD_START, LATER), _stored(new, 8, LATER)]
    again = plan_seed(moved, _snapshot(new, as_of=LATER))
    assert again.is_empty and not again.backfills


def test_a_first_period_is_not_moved_over_admin_history_or_deactivated_rows() -> None:
    price = _price()
    row = _stored(price, 7, AS_OF)
    key = ("testprov", "alpha-1")
    assert plan_seed([row], _snapshot(price), admin_starts={key: AS_OF - timedelta(days=1)}).rebases == {}
    assert plan_seed([row], _snapshot(price), admin_starts={key: AS_OF}).rebases == {7: FIRST_PERIOD_START}
    history = HistoryPeriod(price=_price(input="4"), effective_from=AS_OF - timedelta(days=90), effective_to=AS_OF)
    with_history = plan_seed(
        [row, _stored(history.price, 6, history.effective_from, history.effective_to)], _snapshot(price), [history]
    )
    assert with_history.rebases == {} and with_history.is_empty
    deactivated = _stored(price, 5, AS_OF - timedelta(days=30), AS_OF, active=False)
    assert plan_seed([deactivated, row], _snapshot(price)).rebases == {}


def test_a_changed_price_closes_and_opens_at_as_of() -> None:
    old, new = _price(), _price(input="2.5")
    plan = plan_seed([_stored(old, 7, AS_OF)], _snapshot(new, as_of=LATER))
    assert plan.closes == {7: LATER}
    [insert] = plan.inserts
    assert insert.effective_from == LATER and insert.effective_to is None and insert.supersedes_id == 7
    assert insert.values()["input"] == Decimal("2.5")


def test_a_snapshot_not_newer_than_the_open_period_is_skipped() -> None:
    plan = plan_seed([_stored(_price(), 7, LATER)], _snapshot(_price(input="9"), as_of=AS_OF))
    assert not plan.inserts and not plan.closes and len(plan.skipped) == 1


def test_a_deactivated_open_seed_period_is_left_alone() -> None:
    plan = plan_seed([_stored(_price(), 7, AS_OF, active=False)], _snapshot(_price(input="9"), as_of=LATER))
    assert plan.is_empty


def test_models_missing_from_the_snapshot_keep_their_period() -> None:
    plan = plan_seed([_stored(_price("gone-1"), 3, AS_OF)], _snapshot(_price(), as_of=LATER))
    assert plan.closes == {} and [p.price.model for p in plan.inserts] == ["alpha-1"]


def test_alias_changes_are_written_in_place_without_a_new_period() -> None:
    price = _price()
    aliased = SeedPrice(**{**{f: getattr(price, f) for f in price.__slots__}, "aliases": ("alpha-latest",)})
    plan = plan_seed([_stored(price, 4, AS_OF)], _snapshot(aliased, as_of=LATER))
    assert plan.alias_updates == {4: ("alpha-latest",)} and not plan.inserts and not plan.closes


def test_history_periods_are_inserted_once() -> None:
    earlier = HistoryPeriod(price=_price(input="4"), effective_from=AS_OF - timedelta(days=200), effective_to=AS_OF)
    plan = plan_seed([], _snapshot(_price()), [earlier])
    assert sorted((p.effective_from, p.from_history) for p in plan.inserts) == [
        (AS_OF - timedelta(days=200), True),
        (AS_OF, False),
    ]
    stored = [
        _stored(_price(), 1, AS_OF),
        _stored(earlier.price, 2, earlier.effective_from, earlier.effective_to),
    ]
    again = plan_seed(stored, _snapshot(_price()), [earlier])
    assert again.is_empty and again.unchanged == 2


def test_a_history_period_overlapping_a_seed_period_is_skipped() -> None:
    clash = HistoryPeriod(price=_price(input="4"), effective_from=AS_OF - timedelta(days=10), effective_to=LATER)
    plan = plan_seed([], _snapshot(_price()), [clash])
    assert [p.from_history for p in plan.inserts] == [False]
    assert len(plan.skipped) == 1
