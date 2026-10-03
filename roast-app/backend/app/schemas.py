"""Pydantic request/response schemas."""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


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


# ---------------------------------------------------------------------------
# calibration ledger
# ---------------------------------------------------------------------------

class CalibrationIn(BaseModel):
    """Create a ledger record (always starts as a draft) or the payload for a
    new superseding version.  corrected = gain * raw + offset."""

    channel: str
    valid_from_s: float = Field(ge=0)
    valid_to_s: float = Field(gt=0)
    formula: str = "linear"
    gain: float = 1.0
    offset: float = 0.0
    created_by: str = "operator"
    note: str = ""

    @field_validator("channel")
    @classmethod
    def _channel(cls, v: str) -> str:
        if v not in ("bean", "env"):
            raise ValueError("channel 必须是 bean（豆温）或 env（环境温度）")
        return v

    @field_validator("formula")
    @classmethod
    def _formula(cls, v: str) -> str:
        if v != "linear":
            raise ValueError("目前仅支持 linear：corrected = gain * raw + offset")
        return v

    @model_validator(mode="after")
    def _check(self):
        if self.valid_to_s <= self.valid_from_s:
            raise ValueError("valid_to_s 必须大于 valid_from_s")
        if not (math.isfinite(self.gain) and math.isfinite(self.offset)):
            raise ValueError("gain/offset 必须为有限数值")
        if self.gain <= 0:
            raise ValueError("gain 必须为正数（比例校正）")
        if not self.created_by.strip():
            raise ValueError("created_by 必填（校准记录必须可追溯创建人）")
        return self


class CalibrationActionIn(BaseModel):
    acted_by: str = "operator"
    note: str = ""


class CalibrationHistoryOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    acted_by: str
    note: str
    created_at: datetime

    class Config:
        from_attributes = True


class CalibrationOut(BaseModel):
    id: int
    batch_id: int
    channel: str
    valid_from_s: float
    valid_to_s: float
    formula: str
    gain: float
    offset: float
    version: int
    status: str
    created_by: str
    note: str
    supersedes_id: int | None = None
    superseded_by_id: int | None = None
    created_at: datetime
    updated_at: datetime
    history: list[CalibrationHistoryOut] = []

    class Config:
        from_attributes = True
