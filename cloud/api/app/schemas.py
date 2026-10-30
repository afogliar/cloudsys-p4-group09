from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
)


DailyForecast = Annotated[
    list[FiniteFloat],
    Field(min_length=96, max_length=96),
]

DailyActual = Annotated[
    list[FiniteFloat | None],
    Field(min_length=96, max_length=96),
]

HistoryValues = Annotated[
    list[FiniteFloat | None],
    Field(min_length=672, max_length=672),
]

OriginCode = Annotated[int, Field(strict=True, ge=0, le=255)]

DailyOrigins = Annotated[
    list[OriginCode],
    Field(min_length=96, max_length=96),
]

HistoryOrigins = Annotated[
    list[OriginCode],
    Field(min_length=672, max_length=672),
]

NonNegativeFinite = Annotated[FiniteFloat, Field(ge=0)]


class LowPerformanceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    device_id: str
    category: Literal["cons", "pros"]
    model_version: Annotated[int, Field(strict=True, ge=1)]
    day: date

    mae_kw: NonNegativeFinite
    mae_persistence_kw: NonNegativeFinite

    forecast_kw: DailyForecast
    actual_kw: DailyActual
    actual_origin: DailyOrigins

    history_kw: HistoryValues
    history_origin: HistoryOrigins

    sent_at: AwareDatetime