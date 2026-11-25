"""Run the scenarios against the in-process Specivo MCP server, with a model or as a dry run."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from mcp import Client

from scripts.mcp_eval.fixture import (
    SETUP_CALLS,
    Fixture,
    create_fixture,
    fill,
    load_created_ids,
    load_lookup_ids,
)
from scripts.mcp_eval.scenarios import Scenario, Step
from specivo.mcp import server as mcp_server_module
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.server import mcp
from specivo.mcp.tool_help import ToolListing, tool_listing_var
from specivo.mcp.toolsets import mcp_toolsets_var, parse_toolsets
from specivo.services.wiki_utils import slugify

SYSTEM_PROMPT = (
    "You are an AI agent connected to Specivo, a project tracker with a wiki, through the tools given to you. "
    "Complete the user's task by calling the tools. Do not ask the user questions: look up what you need. "
    "When the task is done, answer in one or two sentences."
)
HELP_TOOL = "specivo_setup_guide"
#: Model turns per task before it is given up.
MAX_TURNS = 12


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def mcp_client(fixture: Fixture, listing: ToolListing, toolsets: str) -> AsyncIterator[Client]:
    """An in-process MCP client authenticated as the fixture's administrator, with *listing* and *toolsets*."""
    real_authenticate = mcp_server_module.authenticate_mcp_tool

    async def authenticate(session: Any) -> Any:
        # The in-process client has no HTTP layer to stash the key.
        token = mcp_raw_key_var.set(fixture.raw_key)
        try:
            return await real_authenticate(session)
        finally:
            mcp_raw_key_var.reset(token)

    mcp_server_module.authenticate_mcp_tool = authenticate
    listing_token = tool_listing_var.set(listing)
    toolsets_token = mcp_toolsets_var.set(parse_toolsets(toolsets))
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        mcp_toolsets_var.reset(toolsets_token)
        tool_listing_var.reset(listing_token)
        mcp_server_module.authenticate_mcp_tool = real_authenticate


@dataclass
class CallOutcome:
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    text: str

    @property
    def schema_rejected(self) -> bool:
        """The call never ran: unknown tool, unreadable arguments or arguments failing validation."""
        return self.is_error and (
            "validation error for" in self.text or self.text.startswith(("Unknown tool", "Invalid JSON arguments"))
        )

    @property
    def help_lookup(self) -> bool:
        return self.tool == HELP_TOOL and bool(self.arguments.get("tool"))


async def call(client: Client, tool: str, arguments: dict[str, Any]) -> CallOutcome:
    result = await client.call_tool(tool, arguments)
    text = "\n".join(getattr(part, "text", "") for part in result.content)
    return CallOutcome(tool, arguments, bool(result.is_error), text)


async def prepare(listing: ToolListing) -> Fixture:
    """A fresh fixture with its projects filled through the MCP tools."""
    fixture = await create_fixture()
    await load_lookup_ids(fixture)
    async with mcp_client(fixture, listing, "all") as client:
        for tool, arguments in SETUP_CALLS:
            outcome = await call(client, tool, fill(arguments, fixture))
            if outcome.is_error:
                raise RuntimeError(f"Fixture setup failed at {tool}: {outcome.text}")
            await load_created_ids(fixture)
    return fixture


async def listed_tools(client: Client) -> list[dict[str, Any]]:
    tools = (await client.list_tools()).tools
    return [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in tools]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def matches(actual: Any, expected: Any) -> bool:
    """Loose argument match: substrings for text, values for numbers, items for lists, keys for objects."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        items = actual if isinstance(actual, list) else [actual]
        return all(any(matches(item, value) for item in items) for value in expected)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual == expected)
    if isinstance(expected, int | float):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    if isinstance(actual, list):
        return any(matches(item, expected) for item in actual)
    if actual is None:
        return False
    return str(expected).strip().lower() in str(actual).strip().lower()


#: Arguments that name a wiki page. Specivo resolves them through ``slugify``, so a title
#: ('Release Process') names the same page as its slug ('release-process').
_PAGE_KEYS = frozenset({"slug", "parent_slug", "target_ref", "source_ref"})


def _argument_matches(key: str, actual: Any, expected: Any) -> bool:
    if matches(actual, expected):
        return True
    if key in _PAGE_KEYS and isinstance(actual, str) and isinstance(expected, str):
        return slugify(expected) == slugify(actual)
    return False


def step_matches(step: Step, outcome: CallOutcome, fixture: Fixture) -> bool:
    if outcome.tool not in step.tools or outcome.is_error:
        return False
    expected = fill(step.check, fixture)
    return all(
        key in outcome.arguments and _argument_matches(key, outcome.arguments[key], value)
        for key, value in expected.items()
    )


def first_tools(scenario: Scenario) -> set[str]:
    """The tools a right first call may use: those of the leading optional steps and the first required one."""
    tools: set[str] = set()
    for step in scenario.steps:
        tools.update(step.tools)
        if not step.optional:
            break
    return tools


def succeeded(scenario: Scenario, outcomes: Sequence[CallOutcome], fixture: Fixture) -> bool:
    """Every required step is matched, in order, by a call that did not fail."""
    position = 0
    for step in scenario.steps:
        if step.optional:
            continue
        while position < len(outcomes) and not step_matches(step, outcomes[position], fixture):
            position += 1
        if position == len(outcomes):
            return False
        position += 1
    return True


@dataclass
class TaskResult:
    scenario: str
    listing: str
    success: bool
    first_call_right: bool
    calls: int
    schema_rejected: int
    help_lookups: int
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    seconds: float = 0.0
    tools_called: list[str] = field(default_factory=list)
    error: str | None = None
    #: Every call with its arguments, whether it failed, and the start of its answer (for reading a miss).
    call_log: list[dict[str, Any]] = field(default_factory=list)


def score(
    scenario: Scenario, outcomes: Sequence[CallOutcome], fixture: Fixture, listing: ToolListing, **usage: Any
) -> TaskResult:
    work = [outcome for outcome in outcomes if not outcome.help_lookup]
    return TaskResult(
        scenario=scenario.id,
        listing=listing,
        success=succeeded(scenario, outcomes, fixture),
        first_call_right=bool(work) and work[0].tool in first_tools(scenario),
        calls=len(outcomes),
        schema_rejected=sum(outcome.schema_rejected for outcome in outcomes),
        help_lookups=sum(outcome.help_lookup for outcome in outcomes),
        tools_called=[outcome.tool for outcome in outcomes],
        call_log=[
            {"tool": o.tool, "arguments": o.arguments, "is_error": o.is_error, "text": o.text[:300]} for o in outcomes
        ],
        **usage,
    )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def dry_run(scenarios: Sequence[Scenario], listing: ToolListing, toolsets: str) -> list[TaskResult]:
    """Run every scenario's reference calls and score them: proves the scenarios and the scoring."""
    fixture = await prepare(listing)
    results = []
    async with mcp_client(fixture, listing, toolsets) as client:
        for scenario in scenarios:
            outcomes = [await call(client, step.tools[0], fill(step.ref, fixture)) for step in scenario.steps]
            result = score(scenario, outcomes, fixture, listing)
            failed = [outcome for outcome in outcomes if outcome.is_error]
            if failed:
                result.error = f"{failed[0].tool}: {failed[0].text[:300]}"
            elif not (result.success and result.first_call_right):
                result.error = "the reference calls do not satisfy the scenario's own checks"
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# A model through an OpenAI-compatible chat completions endpoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str | None
    temperature: float = 0.0
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> ModelConfig:
        base_url = os.environ.get("MCP_EVAL_BASE_URL")
        model = os.environ.get("MCP_EVAL_MODEL")
        if not base_url or not model:
            raise SystemExit("Set MCP_EVAL_BASE_URL and MCP_EVAL_MODEL (and MCP_EVAL_API_KEY unless local).")
        return cls(base_url.rstrip("/"), model, os.environ.get("MCP_EVAL_API_KEY"))


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP tool definitions as chat-completions function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


async def run_task(
    http: httpx.AsyncClient,
    config: ModelConfig,
    client: Client,
    tools: list[dict[str, Any]],
    scenario: Scenario,
    fixture: Fixture,
    listing: ToolListing,
) -> TaskResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": fixture.text(scenario.prompt)},
    ]
    outcomes: list[CallOutcome] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    started = time.monotonic()
    error = None
    # X-Title names the run in the endpoint's usage pages (OpenRouter's Activity shows it as the app).
    headers = {"X-Title": "Specivo MCP eval"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    for _ in range(MAX_TURNS):
        payload = {"model": config.model, "messages": messages, "tools": tools, "temperature": config.temperature}
        response = await _post_with_deadline(http, f"{config.base_url}/chat/completions", headers, payload, config)
        if response is None:
            error = f"no answer within {config.timeout:.0f}s ({_ATTEMPTS} attempts)"
            break
        if response.status_code >= 400:
            error = f"HTTP {response.status_code}: {response.text[:300]}"
            break
        body = response.json()
        usage["turns"] += 1
        usage["input_tokens"] += int((body.get("usage") or {}).get("prompt_tokens") or 0)
        usage["output_tokens"] += int((body.get("usage") or {}).get("completion_tokens") or 0)
        message = body["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        messages.append({key: value for key, value in message.items() if value is not None})
        if not tool_calls:
            break
        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            try:
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                outcome = CallOutcome(name, {}, True, f"Invalid JSON arguments: {exc}")
            else:
                outcome = await call(client, name, arguments if isinstance(arguments, dict) else {})
            outcomes.append(outcome)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": outcome.text})
    result = score(scenario, outcomes, fixture, listing, seconds=round(time.monotonic() - started, 1), **usage)
    result.error = error
    return result


async def model_run(
    scenarios: Sequence[Scenario], listing: ToolListing, toolsets: str, config: ModelConfig
) -> tuple[list[TaskResult], int]:
    """Run every scenario with the model; returns the results and the size of tools/list (chars)."""
    fixture = await prepare(listing)
    async with mcp_client(fixture, listing, toolsets) as client, httpx.AsyncClient(timeout=config.timeout) as http:
        listed = await listed_tools(client)
        tools = openai_tools(listed)
        # One task after the other: later scenarios may rely on earlier changes.
        results = [await run_task(http, config, client, tools, scenario, fixture, listing) for scenario in scenarios]
    return results, len(json.dumps({"tools": listed}))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def summary_row(results: Sequence[TaskResult]) -> dict[str, float]:
    count = len(results) or 1
    finished = [result for result in results if result.success]
    tokens = sum(result.input_tokens + result.output_tokens for result in results)
    return {
        "tasks": len(results),
        "first call right %": round(100 * sum(result.first_call_right for result in results) / count, 1),
        "success %": round(100 * len(finished) / count, 1),
        "schema rejected": sum(result.schema_rejected for result in results),
        "calls / task": round(sum(result.calls for result in results) / count, 2),
        "help lookups": sum(result.help_lookups for result in results),
        "input tokens": sum(result.input_tokens for result in results),
        "output tokens": sum(result.output_tokens for result in results),
        "tokens / finished task": round(tokens / len(finished)) if finished else 0,
    }


def comparison_table(rows: dict[str, dict[str, float]]) -> str:
    """A plain-text table with one column per listing mode."""
    if not rows:
        return ""
    metrics = list(next(iter(rows.values())))
    width = max(len(metric) for metric in metrics) + 2
    header = "metric".ljust(width) + "".join(mode.rjust(14) for mode in rows)
    lines = [header, "-" * len(header)]
    lines += [metric.ljust(width) + "".join(str(rows[mode][metric]).rjust(14) for mode in rows) for metric in metrics]
    return "\n".join(lines)


def to_json(results: Sequence[TaskResult]) -> list[dict[str, Any]]:
    return [asdict(result) for result in results]


_ATTEMPTS = 3


async def _post_with_deadline(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any], config: Any
) -> httpx.Response | None:
    """POST with a deadline on the whole request, retried; ``None`` when every attempt timed out.

    httpx's timeout limits each read, and some endpoints (OpenRouter) keep a slow request open by
    sending whitespace, so a stalled answer would otherwise wait forever.
    """
    for _ in range(_ATTEMPTS):
        try:
            return await asyncio.wait_for(http.post(url, headers=headers, json=payload), timeout=config.timeout)
        except (TimeoutError, httpx.TransportError):
            continue
    return None
