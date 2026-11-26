"""Shared setup for the concept extraction tests: a fake model client and a project with the extraction on.

Not a test module. Builds on ``link_graph_helpers`` (two projects, an admin and
a reader). No test ever calls a real model: :class:`FakeModel` answers from a
table keyed by a word of the document, and records what it was sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.ai_provider import ProjectAiProvider
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.concept_extraction import ENABLED_BY_SETTING, ENABLED_SETTING
from specivo.services.llm_client import ChatResult, LLMError


@pytest.fixture(autouse=True)
def _no_queued_extraction(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Keep the extraction task from being queued; record the projects it would have run for."""
    queued: list[int] = []

    def record(project_id: int, user_id: int | None = None) -> bool:
        queued.append(project_id)
        return True

    monkeypatch.setattr("specivo.services.concept_extraction.dispatch_extract", record)
    monkeypatch.setattr("specivo.api.v1.concepts.dispatch_extract", record)
    return queued


def concept(name: str, passage: str, kind: str = "feature", confidence: float = 0.9) -> dict:
    return {"name": name, "kind": kind, "passage": passage, "confidence": confidence}


@dataclass
class FakeModel:
    """Answers ``{"concepts": [...]}`` for the first key found in the document, else no concepts.

    ``raw`` answers a key with a literal text instead (malformed output);
    ``fail`` raises an ``LLMError`` for every call. Every document is recorded.
    """

    answers: dict[str, list[dict]] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    fail: bool = False
    input_tokens: int = 100
    output_tokens: int = 20
    sent: list[str] = field(default_factory=list)

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> ChatResult:
        document = messages[-1]["content"]
        self.sent.append(document)
        if self.fail:
            raise LLMError("The provider answered HTTP 503", input_tokens=5)
        for key, text in self.raw.items():
            if key in document:
                return ChatResult(text, self.input_tokens, self.output_tokens)
        for key, items in self.answers.items():
            if key in document:
                return ChatResult(json.dumps({"concepts": items}), self.input_tokens, self.output_tokens)
        return ChatResult('{"concepts": []}', self.input_tokens, self.output_tokens)


async def give_provider(db: AsyncSession, project: Project, model: str = "small-model") -> ProjectAiProvider:
    row = ProjectAiProvider(
        project_id=project.id, provider="openai", base_url="http://models.example.test/v1", model=model
    )
    db.add(row)
    await db.commit()
    return row


async def enable(db: AsyncSession, project: Project, owner: User, *, threshold: float | None = None) -> None:
    """Give *project* a provider and switch the extraction on, owned by *owner*."""
    await give_provider(db, project)
    settings = {**(project.settings or {}), ENABLED_SETTING: True, ENABLED_BY_SETTING: owner.id}
    if threshold is not None:
        settings["concept_confidence_threshold"] = threshold
    project.settings = settings
    await db.commit()
