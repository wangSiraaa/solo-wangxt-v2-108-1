"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


class EventIn(BaseModel):
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: str = "manual"
    created_by: str = "operator"
    value_num: float | None = None
    note: str = ""


class EventOut(EventIn):
    id: int
    batch_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class CalibrationIn(BaseModel):
    """Create a ledger entry (always starts as a draft; records are immutable
    afterwards — a change is a new version via ``replaces_id``)."""

    channel: str
    t_start_s: float = Field(ge=0)
    t_end_s: float = Field(gt=0)
    formula: str = "affine"
    scale: float
    offset_c: float = 0.0
    created_by: str = "operator"
    note: str = ""
    replaces_id: int | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.channel not in ("bean", "env"):
            raise ValueError("channel must be 'bean' or 'env'")
        if self.t_end_s <= self.t_start_s:
            raise ValueError("t_end_s must be greater than t_start_s")
        if self.formula != "affine":
            raise ValueError("only the 'affine' formula is supported")
        if self.scale == 0:
            raise ValueError("scale must be non-zero")
        return self


class CalibrationOut(BaseModel):
    id: int
    batch_id: int
    channel: str
    t_start_s: float
    t_end_s: float
    formula: str
    scale: float
    offset_c: float
    created_by: str
    note: str
    version: int
    status: str
    replaces_id: int | None = None
    superseded_by_id: int | None = None
    created_at: datetime
    status_changed_at: datetime

    class Config:
        from_attributes = True


class BatchMeta(BaseModel):
    id: int
    name: str
    roaster: str
    bean: str
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float
    target_drop_temp_c: float | None = None
    note: str

    class Config:
        from_attributes = True
