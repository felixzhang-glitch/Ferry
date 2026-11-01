"""Versioned, bounded wire contract for optional iOS read-only device data."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BATCH_BYTES = 256 * 1024
MAX_CONTROL_BYTES = 4096
MAX_BATCH_RECORDS = 200
MAX_BATCHES = 256
MAX_TRANSACTION_BYTES = 64 * 1024 * 1024
MAX_RECORDS = MAX_BATCH_RECORDS * MAX_BATCHES
TRANSACTION_TTL = 3600
Kind = Literal["activity", "heart_rate", "weight", "oxygen", "calendar", "reminders"]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]
OpaqueID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Timestamp = Annotated[float, Field(ge=0, le=4102444800, allow_inf_nan=False)]
Text = Annotated[str, Field(max_length=256)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SyncStart(WireModel):
    version: Literal[1] = 1
    device_id: OpaqueID
    kind: Kind
    window_start: Timestamp
    window_end: Timestamp
    timezone: Annotated[str, Field(min_length=1, max_length=96)]
    sources: Annotated[list[Identifier], Field(max_length=100)] = []

    @model_validator(mode="before")
    @classmethod
    def version_type(cls, value):
        if isinstance(value, dict) and type(value.get("version", 1)) is not int:
            raise ValueError("integer version required")
        return value

    @model_validator(mode="after")
    def window(self):
        if self.window_end <= self.window_start or self.window_end - self.window_start > 62 * 86400:
            raise ValueError("invalid window")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate sources")
        return self


class DateParts(WireModel):
    year: Annotated[int, Field(ge=1970, le=2100)]
    month: Annotated[int, Field(ge=1, le=12)]
    day: Annotated[int, Field(ge=1, le=31)]
    hour: Annotated[int, Field(ge=0, le=23)] | None = None
    minute: Annotated[int, Field(ge=0, le=59)] | None = None
    timezone: Annotated[str, Field(max_length=96)] | None = None

    @model_validator(mode="after")
    def date(self):
        from datetime import date
        date(self.year, self.month, self.day)
        if (self.hour is None) != (self.minute is None):
            raise ValueError("incomplete time")
        return self


class DeviceRecord(WireModel):
    id: Identifier
    source: Identifier
    source_name: Text = ""
    metric: Literal["steps", "distance", "active_energy", "heart_rate", "weight", "oxygen", "event", "reminder"]
    start: Timestamp | None = None
    end: Timestamp | None = None
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    unit: Literal["count", "m", "kcal", "bpm", "kg", "percent"] | None = None
    title: Annotated[str, Field(max_length=500)] | None = None
    all_day: bool | None = None
    start_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    end_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    completed: bool | None = None
    completed_at: Timestamp | None = None
    due: DateParts | None = None
    timezone: Annotated[str, Field(max_length=96)] | None = None

    @model_validator(mode="after")
    def shape(self):
        numeric = {"steps": ("count", 1e7), "distance": ("m", 1e7),
                   "active_energy": ("kcal", 1e6), "heart_rate": ("bpm", 1000),
                   "weight": ("kg", 2000), "oxygen": ("percent", 100)}
        if self.end is not None and (self.start is None or self.end < self.start):
            raise ValueError("invalid interval")
        if self.metric in numeric:
            unit, maximum = numeric[self.metric]
            if self.start is None or self.end is None or self.unit != unit or self.value is None or self.value > maximum:
                raise ValueError("invalid quantity")
            if any(x is not None for x in (self.title, self.all_day, self.start_date, self.end_date,
                                           self.completed, self.completed_at, self.due)):
                raise ValueError("unexpected event fields")
        else:
            if self.value is not None or self.unit is not None or self.title is None:
                raise ValueError("invalid event")
            if self.metric == "event":
                if self.start is None or self.end is None or self.all_day is None:
                    raise ValueError("event interval required")
                if any(x is not None for x in (self.completed, self.completed_at, self.due)):
                    raise ValueError("unexpected reminder fields")
                if self.all_day and (not self.start_date or not self.end_date):
                    raise ValueError("all-day dates required")
                if not self.all_day and (self.start_date or self.end_date):
                    raise ValueError("unexpected all-day dates")
                if self.start_date:
                    from datetime import date
                    if date.fromisoformat(self.end_date) <= date.fromisoformat(self.start_date):
                        raise ValueError("invalid all-day dates")
            else:
                if self.completed is None or (self.completed and self.completed_at is None):
                    raise ValueError("completion required")
                if not self.completed and self.completed_at is not None:
                    raise ValueError("unexpected completion date")
                if any(x is not None for x in (self.all_day, self.start_date, self.end_date, self.start, self.end)):
                    raise ValueError("unexpected event interval")
        return self


class Batch(WireModel):
    records: Annotated[list[DeviceRecord], Field(min_length=1, max_length=MAX_BATCH_RECORDS)]


class SyncCommit(WireModel):
    batch_count: Annotated[int, Field(ge=0, le=MAX_BATCHES)]
    record_count: Annotated[int, Field(ge=0, le=MAX_RECORDS)]
    completeness: Literal["complete", "partial"]
    reason: Literal["none", "no_readable_data", "limit", "cancelled", "read_error", "permission"] = "none"

    @model_validator(mode="after")
    def state(self):
        if self.completeness == "complete" and self.reason != "none":
            raise ValueError("unexpected partial reason")
        if self.completeness == "partial" and self.reason == "none":
            raise ValueError("partial reason required")
        if self.record_count == 0 and (self.completeness != "partial" or self.batch_count != 0):
            raise ValueError("empty reads are uncertain")
        return self


class DeleteData(WireModel):
    device_id: OpaqueID
    kind: Kind


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("non-finite number")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError:
        raise ValueError("JSON nesting too deep") from None
    if not isinstance(result, dict):
        raise ValueError("object required")
    return result
