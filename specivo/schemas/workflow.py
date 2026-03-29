"""Pydantic schemas for workflow transitions and field rules."""

from pydantic import BaseModel, field_validator


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
    tracker_id: int
    role_id: int
    status_id: int
    field_name: str
    rule: str

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
