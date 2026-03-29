"""IssueRelation model — cross-issue relationship records."""

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class IssueRelation(Base, TimestampMixin):
    """A directed relation between two issues.

    Only 5 canonical relation_type values are stored in the database:
    ``relates``, ``duplicates``, ``blocks``, ``precedes``, ``copied_to``.

    The reverse types (``duplicated``, ``blocked``, ``follows``, ``copied_from``)
    are derived at read time: when an issue appears on the ``issue_to_id`` side
    the reverse label is used.

    For ``relates`` the issue with the lower ID is always placed in
    ``issue_from_id`` to guarantee a single canonical row per pair.

    ``delay`` is the number of days applied to ``precedes`` / ``follows``
    relations when scheduling successor start dates.
    """

    __tablename__ = "issue_relations"

    __table_args__ = (
        UniqueConstraint("issue_from_id", "issue_to_id", "relation_type", name="uq_issue_relation"),
        Index("ix_issue_relations_from", "issue_from_id"),
        Index("ix_issue_relations_to", "issue_to_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    issue_from_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
    )

    issue_to_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
    )

    # One of the 5 canonical forms (reverse types normalised before insert)
    relation_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Days offset used by precedes/follows scheduling
    delay: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<IssueRelation id={self.id} from={self.issue_from_id} {self.relation_type} to={self.issue_to_id}>"
