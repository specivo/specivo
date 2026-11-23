"""The published AI usage ingest contract v1: schemas, examples and the pure record rules.

The examples are also replayed through the endpoint in
``tests/integration/test_ai_usage_ingest.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from specivo.core.ai_usage_ingest import (
    OPTIONAL_FIELDS,
    SCHEMA_DIR,
    RecordRejectedError,
    check_schema,
    parse_record,
    record_validator,
    repo_full_name,
    strip_unknown,
)

pytestmark = pytest.mark.unit

PUBLISHED = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "ai-usage" / "v1"
EXAMPLES = PUBLISHED / "examples"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _published() -> dict[str, dict[str, Any]]:
    return {path.name: json.loads(path.read_text()) for path in sorted(PUBLISHED.glob("*.schema.json"))}


def _validator(name: str) -> Draft202012Validator:
    schemas = _published()
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(schemas[name], registry=registry)


def test_server_copy_matches_the_published_schemas() -> None:
    published = _published()
    served = {path.name: path.read_text() for path in sorted(SCHEMA_DIR.glob("*.schema.json"))}
    assert sorted(served) == ["batch.schema.json", "record.schema.json"]
    for name, text in served.items():
        assert json.loads(text) == published[name], name


@pytest.mark.parametrize("name", ["batch.schema.json", "record.schema.json", "result.schema.json", "info.schema.json"])
def test_schemas_are_valid_2020_12(name: str) -> None:
    schema = _published()[name]
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"https://specivo.io/schemas/ai-usage/v1/{name}"


def test_every_example_is_listed_in_expected() -> None:
    expected = json.loads((EXAMPLES / "expected.json").read_text())
    assert sorted(expected["valid"]) == sorted(p.name for p in (EXAMPLES / "valid").glob("*.json"))
    assert sorted(expected["invalid"]) == sorted(p.name for p in (EXAMPLES / "invalid").glob("*.json"))


@pytest.mark.parametrize("path", sorted((EXAMPLES / "valid").glob("*.json")), ids=lambda p: p.name)
def test_valid_examples_validate(path: Path) -> None:
    body = json.loads(path.read_text())
    errors = list(_validator("batch.schema.json").iter_errors(body))
    assert errors == [], [e.message for e in errors]
    for raw in body["records"]:
        parse_record(raw, NOW)


@pytest.mark.parametrize("path", sorted((EXAMPLES / "invalid").glob("*.json")), ids=lambda p: p.name)
def test_invalid_examples_fail_as_documented(path: Path) -> None:
    body = json.loads(path.read_text())
    outcome = json.loads((EXAMPLES / "expected.json").read_text())["invalid"][path.name]
    assert _validator("batch.schema.json").is_valid(body) is outcome["schema_valid"]
    record = _validator("record.schema.json")
    if "records" not in outcome:
        return
    for raw, want in zip(body["records"], outcome["records"], strict=True):
        assert record.is_valid(raw) is want["schema_valid"], raw["idempotency_key"]
        if want["schema_valid"] and want["code"] == "invalid_record":
            with pytest.raises(RecordRejectedError) as caught:
                parse_record(raw, NOW)
            assert caught.value.problem.field == want["field"]


def _record(**kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": "k",
        "tool": "claude-code",
        "model": "m",
        "usage_date": "2026-11-05",
        "tokens": {"input": 1},
    }
    values.update(kw)
    return values


def test_unknown_properties_are_collected_not_refused() -> None:
    raw = _record(extra=1, tokens={"input": 1, "audio": 2}, attribution={"repo": "a/b", "x": 1}, reported_cost=None)
    unknown, problem = check_schema(record_validator(), raw)
    assert problem is None
    assert unknown == ["attribution.x", "extra", "tokens.audio"]
    cleaned = strip_unknown(raw, unknown)
    assert cleaned["tokens"] == {"input": 1}
    assert cleaned["attribution"] == {"repo": "a/b"}
    assert "extra" not in cleaned
    assert raw["extra"] == 1  # the original is untouched


def test_errors_inside_nullable_objects_name_the_field() -> None:
    _unknown, problem = check_schema(record_validator(), _record(reported_cost={"amount": "-1", "currency": "USD"}))
    assert problem is not None
    assert problem.field == "reported_cost.amount"
    _unknown, problem = check_schema(record_validator(), _record(attribution="ACME-1"))
    assert problem is not None
    assert problem.field == "attribution"


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"usage_date": "2026-02-30"}, "usage_date"),
        ({"usage_date": "2026-11-07"}, "usage_date"),
        ({"usage_date": "2025-10-01"}, "usage_date"),
        ({"started_at": "2026-11-04T23:59:59Z"}, "started_at"),
        ({"started_at": "2026-11-05T06:59:59+07:00"}, "started_at"),
        ({"ended_at": "2026-11-06T00:00:01Z"}, "ended_at"),
        ({"started_at": "2026-11-05T10:00:00Z", "ended_at": "2026-11-05T09:59:59Z"}, "ended_at"),
        ({"usage_date": "2026-11-06", "started_at": "2026-11-06T12:30:00Z"}, "started_at"),
    ],
)
def test_time_rules(changes: dict[str, Any], field: str) -> None:
    with pytest.raises(RecordRejectedError) as caught:
        parse_record(_record(**changes), NOW)
    assert caught.value.problem.field == field


def test_times_are_converted_to_utc() -> None:
    parsed = parse_record(
        _record(started_at="2026-11-05T07:00:00+07:00", ended_at="2026-11-06T00:00:00Z", usage_date="2026-11-05"),
        NOW,
    )
    assert parsed.started_at == datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    assert parsed.ended_at == datetime(2026, 11, 6, tzinfo=UTC)
    assert parsed.usage_date == date(2026, 11, 5)
    assert parse_record(_record(usage_date="2025-10-02"), NOW).usage_date == date(2025, 10, 2)


def test_attribution_is_normalised() -> None:
    parsed = parse_record(
        _record(
            attribution={
                "issue_ref": "acme-12",
                "issue_ref_source": "env",
                "project_key": "acme",
                "commits": ["AB" * 20, "ab" * 20],
            }
        ),
        NOW,
    )
    assert parsed.attribution.issue_ref == "ACME-12"
    assert parsed.attribution.issue_method == "env"
    assert parsed.attribution.project_key == "ACME"
    assert parsed.attribution.commits == ["ab" * 20]


def test_optional_fields_are_optional_record_properties() -> None:
    record = _published()["record.schema.json"]
    assert OPTIONAL_FIELDS == ("route", "billing_account", "parent_session_id", "unattended", "profile")
    for name in OPTIONAL_FIELDS:
        assert name in record["properties"], name
        assert name not in record["required"], name
    info = _published()["info.schema.json"]
    assert info["properties"]["optional_fields"]["type"] == "array"
    assert "optional_fields" not in info["required"]


def test_route_and_billing_account_are_parsed() -> None:
    parsed = parse_record(_record(route="  llm-gateway.example.com ", billing_account="acme-gateway"), NOW)
    assert parsed.route == "llm-gateway.example.com"
    assert parsed.billing_account == "acme-gateway"
    bare = parse_record(_record(route=None, billing_account=None), NOW)
    assert (bare.route, bare.billing_account) == (None, None)
    assert parse_record(_record(route="   "), NOW).route is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"route": "r" * 101}, "route"),
        ({"route": ""}, "route"),
        ({"route": "gateway\nhost"}, "route"),
        ({"route": 7}, "route"),
        ({"billing_account": "Acme-Gateway"}, "billing_account"),
        ({"billing_account": "a" * 65}, "billing_account"),
        ({"billing_account": "-acme"}, "billing_account"),
        ({"billing_account": "acme gateway"}, "billing_account"),
        ({"billing_account": ""}, "billing_account"),
    ],
)
def test_route_and_billing_account_bounds(changes: dict[str, Any], field: str) -> None:
    unknown, problem = check_schema(record_validator(), _record(**changes))
    assert unknown == []
    assert problem is not None
    assert problem.field == field


def test_route_and_billing_account_at_their_limits_are_valid() -> None:
    unknown, problem = check_schema(record_validator(), _record(route="r" * 100, billing_account="a" + "-" * 63))
    assert (unknown, problem) == ([], None)


def test_session_context_fields_are_parsed() -> None:
    parsed = parse_record(
        _record(parent_session_id=" 20261105_090000_a1b2c3 ", unattended=True, profile=" research "), NOW
    )
    assert parsed.parent_session_id == "20261105_090000_a1b2c3"
    assert parsed.unattended is True
    assert parsed.profile == "research"


def test_session_context_fields_default_to_an_attended_run_without_parent_or_profile() -> None:
    bare = parse_record(_record(), NOW)
    assert (bare.parent_session_id, bare.unattended, bare.profile) == (None, False, None)
    nulls = parse_record(_record(parent_session_id=None, profile=None, unattended=False), NOW)
    assert (nulls.parent_session_id, nulls.unattended, nulls.profile) == (None, False, None)
    assert parse_record(_record(profile="   "), NOW).profile is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"unattended": "true"}, "unattended"),
        ({"unattended": 1}, "unattended"),
        ({"unattended": None}, "unattended"),
        ({"profile": "p" * 101}, "profile"),
        ({"profile": ""}, "profile"),
        ({"profile": "research\tlab"}, "profile"),
        ({"parent_session_id": "s" * 201}, "parent_session_id"),
        ({"parent_session_id": ""}, "parent_session_id"),
    ],
)
def test_session_context_field_bounds(changes: dict[str, Any], field: str) -> None:
    unknown, problem = check_schema(record_validator(), _record(**changes))
    assert unknown == []
    assert problem is not None
    assert problem.field == field


def test_session_context_fields_at_their_limits_are_valid() -> None:
    unknown, problem = check_schema(
        record_validator(), _record(parent_session_id="s" * 200, profile="p" * 100, unattended=True)
    )
    assert (unknown, problem) == ([], None)


@pytest.mark.parametrize(
    ("hint", "name"),
    [
        ("github.com/acme/app", "acme/app"),
        ("https://github.com/Acme/App.git", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("ssh://git@gitlab.example.com:2222/group/sub/app.git", "group/sub/app"),
        ("https://user@gitea.example.com/acme/app/", "acme/app"),
        ("acme/app", "acme/app"),
        ("app", None),
        ("", None),
        (None, None),
        ("github.com/acme", None),
    ],
)
def test_repo_full_name(hint: str | None, name: str | None) -> None:
    assert repo_full_name(hint) == name
