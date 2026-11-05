"""Pydantic schemas for workflow transitions and field rules."""

from pydantic import BaseModel, field_validator

# Issue fields a field rule can name. Metadata keys are not among them: a
# metadata schema's ``required`` covers those.
FIELD_RULE_FIELDS = frozenset(
    {
        "assigned_to_id",
        "category_id",
        "fixed_version_id",
        "sprint_id",
        "priority_id",
        "tracker_id",
        "subject",
        "description",
        "start_date",
        "due_date",
        "estimated_hours",
        "done_ratio",
        "parent_id",
    }
)


class TransitionCreate(BaseModel):
    tracker_id: int
    role_id: int
    old_status_id: int
    new_status_id: int


class TransitionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    tracker_id: int
    role_id: int
    old_status_id: int
    new_status_id: int


class FieldRuleCreate(BaseModel):
    """A field rule: in *status_id*, for *role_id* on *tracker_id*, *field_name* is required or read-only.

    ``field_name`` is one of ``FIELD_RULE_FIELDS``; a rule on any other name
    would never match a field, so it is refused.
    """

    tracker_id: int
    role_id: int
    status_id: int
    field_name: str
    rule: str

    @field_validator("field_name")
    @classmethod
    def validate_field_name(cls, v: str) -> str:
        if v not in FIELD_RULE_FIELDS:
            raise ValueError(f"field_name must be one of: {', '.join(sorted(FIELD_RULE_FIELDS))}")
        return v

    @field_validator("rule")
    @classmethod
    def validate_rule(cls, v: str) -> str:
        if v not in ("required", "readonly"):
            raise ValueError("rule must be 'required' or 'readonly'")
        return v


class FieldRuleOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    tracker_id: int
    role_id: int
    status_id: int
    field_name: str
    rule: str


class BulkTransitionReplace(BaseModel):
    transitions: list[dict]


class AllowedStatusesOut(BaseModel):
    allowed_status_ids: list[int]
