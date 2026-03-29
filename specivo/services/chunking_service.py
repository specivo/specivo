"""Content-aware chunking service for search indexing.

Splits issues, wiki pages, journal notes, and attachments into searchable
chunks.  Each source type has its own chunking strategy:
- Issues: single chunk (subject + description)
- Wiki pages: split by markdown headings (h1, h2, h3)
- Journals: single atomic chunk per note (respects min length setting)
- Attachments: single chunk (filename + description)
"""

from __future__ import annotations

import re

from specivo.core.config import get_settings


class ChunkingService:
    """Splits entity content into searchable text chunks."""

    def chunk_issue(self, subject: str, description: str | None) -> list[str]:
        """Issue -> single chunk: subject + description concatenated.

        Args:
            subject: Issue subject line.
            description: Optional issue description.

        Returns:
            List containing a single chunk string.
        """
        text = subject
        if description:
            text += "\n\n" + description
        return [text]

    def chunk_wiki_page(self, title: str, text: str) -> list[str]:
        """Wiki page -> split by markdown headings (h1/h2/h3).

        Each chunk includes its heading context. The first chunk
        includes the page title as a prefix.

        Args:
            title: Wiki page title.
            text: Wiki page content (markdown).

        Returns:
            List of chunk strings, one per section.
        """
        if not text:
            return [title]

        # Split by markdown headings (h1, h2, h3)
        sections = re.split(r"(?=^#{1,3}\s)", text, flags=re.MULTILINE)
        chunks: list[str] = []
        for section in sections:
            section = section.strip()
            if section:
                # First chunk gets the title prefix
                if not chunks:
                    chunks.append(f"{title}\n\n{section}")
                else:
                    chunks.append(section)

        return chunks if chunks else [f"{title}\n\n{text}"]

    def chunk_attachment(
        self,
        filename: str,
        description: str | None,
        extracted_text: str | None = None,
    ) -> list[str]:
        """Attachment -> one or more chunks.

        Chunk 0 is always filename + description. When ``extracted_text`` is
        provided, it is split into additional chunks (~1000 chars each with
        ~80 char overlap) appended after the first chunk.

        The filename is normalized for FTS: hyphens, underscores, and dots are
        replaced with spaces so that PostgreSQL tokenizes each part individually
        (e.g. ``jwt-rotation-flow.png`` -> ``jwt rotation flow png``).
        The original filename is preserved on a separate line for exact-match
        snippet highlighting.

        Args:
            filename: Original filename of the attachment.
            description: Optional human-provided description.
            extracted_text: Optional text extracted from the file content
                (e.g. PDF text, OCR output).

        Returns:
            List of chunk strings. First chunk is always filename + description.
        """
        # Normalize filename for better FTS tokenization
        normalized = re.sub(r"[-_.]", " ", filename).strip()
        text = f"{normalized}\n{filename}"
        if description:
            text += "\n\n" + description

        chunks = [text]

        if extracted_text and extracted_text.strip():
            chunks.extend(self._split_text(extracted_text.strip()))

        return chunks

    def _split_text(
        self,
        text: str,
        max_chars: int = 1000,
        overlap: int = 80,
    ) -> list[str]:
        """Split text into chunks of approximately ``max_chars`` with overlap.

        Strategy: split by paragraphs first, then by sentences if a single
        paragraph exceeds ``max_chars``.

        Args:
            text: The text to split.
            max_chars: Target maximum characters per chunk.
            overlap: Number of overlap characters between consecutive chunks.

        Returns:
            List of text chunks.
        """
        if len(text) <= max_chars:
            return [text]

        # Split into paragraphs (double newline)
        paragraphs = re.split(r"\n\n+", text)

        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # If a single paragraph is too long, split by sentences
            if len(para) > max_chars:
                if current:
                    chunks.append(current.strip())
                    current = ""
                chunks.extend(self._split_by_sentences(para, max_chars, overlap))
                continue

            # Try to fit the paragraph into the current chunk
            candidate = f"{current}\n\n{para}" if current else para
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                    # Overlap: take the tail of the previous chunk
                    tail = current.strip()[-overlap:] if overlap else ""
                    current = f"{tail} {para}" if tail else para
                else:
                    current = para

        if current.strip():
            chunks.append(current.strip())

        return chunks

    def _split_by_sentences(
        self,
        text: str,
        max_chars: int,
        overlap: int,
    ) -> list[str]:
        """Split a long paragraph into chunks by sentence boundaries."""
        # Split on sentence-ending punctuation followed by whitespace
        sentences = re.split(r"(?<=[.!?])\s+", text)

        chunks: list[str] = []
        current = ""

        for sentence in sentences:
            candidate = f"{current} {sentence}" if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                    tail = current.strip()[-overlap:] if overlap else ""
                    current = f"{tail} {sentence}" if tail else sentence
                else:
                    # Single sentence exceeds max_chars — force-split
                    for i in range(0, len(sentence), max_chars - overlap):
                        chunks.append(sentence[i : i + max_chars])
                    current = ""

        if current.strip():
            chunks.append(current.strip())

        return chunks

    def chunk_journal(self, notes: str | None) -> list[str]:
        """Journal note -> single atomic chunk.

        Respects ``search_min_comment_length`` setting: comments shorter
        than the threshold are excluded from indexing (returns empty list).

        Args:
            notes: Journal note text.

        Returns:
            List with one chunk, or empty list if notes are empty/None/too short.
        """
        if not notes:
            return []
        settings = get_settings()
        if len(notes) < settings.search_min_comment_length:
            return []
        return [notes]
