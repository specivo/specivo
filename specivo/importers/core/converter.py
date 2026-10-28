"""``ContentConverter`` protocol — source markup to Specivo Markdown.

Each source can bring its own markup (Textile, a wiki syntax, plain text). A
converter turns one field's raw text into the Markdown Specivo stores,
including any rewriting of source-specific link syntax.

Converters are synchronous and pure so they can be unit-tested without a
database or a live source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ConversionContext:
    """Everything a converter needs beyond the text itself.

    ``attachment_filenames`` lets a converter decide whether a reference points
    at a real attachment.
    """

    project_ref: str | None = None
    attachment_filenames: frozenset[str] = field(default_factory=frozenset)


@runtime_checkable
class ContentConverter(Protocol):
    """Converts one field of source markup to Markdown."""

    def convert(self, raw: str, ctx: ConversionContext) -> str:
        """Return *raw* as Markdown.

        Implementations must never raise: unconvertible input is returned in a
        form that preserves the original text so no content is lost.
        """
        ...


class PassthroughConverter:
    """Returns text unchanged.

    Used for sources that already store Markdown, and as a stand-in in tests
    that exercise loaders rather than conversion.
    """

    def convert(self, raw: str, ctx: ConversionContext) -> str:
        """Return *raw* unchanged."""
        return raw
