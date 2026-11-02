from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
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

METER_CATEGORIES = {
    "27afc1ed": "cons",
    "bd21be01": "cons",
    "4c1e4d42": "cons",
    "8fd192c9": "cons",
    "6bedc031": "cons",
    "5c3492e5": "pros",
    "f4e50206": "pros",
    "f3544a98": "pros",
    "9a685f1a": "pros",
    "53229fee": "pros",
}

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

    @model_validator(mode="after")
    def check_meter_category(self):
        expected_category = METER_CATEGORIES.get(self.device_id)

        if expected_category is None:
            raise ValueError("Compteur inconnu")

        if self.category != expected_category:
            raise ValueError("La catégorie ne correspond pas au compteur")

        return self

    @model_validator(mode="after")
    def check_measurements_and_origins(self):
        allowed_origins = {
            0, 1, 5,
            11, 12, 13,
            21, 22, 23,
            35,
            91, 92, 93, 94, 96,
        }

        groups = (
            ("actual", self.actual_kw, self.actual_origin),
            ("history", self.history_kw, self.history_origin),
        )

        for name, values, origins in groups:
            for index, (value, origin) in enumerate(zip(values, origins)):
                if origin not in allowed_origins:
                    raise ValueError(
                        f"{name}_origin[{index}] : code inconnu {origin}"
                    )

                if origin >= 90 and value is not None:
                    raise ValueError(
                        f"{name}_kw[{index}] doit être null "
                        f"pour le code d'origine {origin}"
                    )

                if origin < 90 and value is None:
                    raise ValueError(
                        f"{name}_kw[{index}] doit contenir une valeur "
                        f"pour le code d'origine {origin}"
                    )

        return self

class DailyMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    category: Literal["cons", "pros"]
    day: date
    model_version: Annotated[int, Field(strict=True, ge=1)]

    n_valid_slots: Annotated[int, Field(strict=True, ge=0, le=96)]
    n_comparison_slots: Annotated[int, Field(strict=True, ge=0, le=96)]

    mae_kw: NonNegativeFinite | None
    mae_comparison_kw: NonNegativeFinite | None
    mae_persistence_kw: NonNegativeFinite | None
    low_performance: bool | None

    # Local model-adoption decision, reported after a cloud release. All five
    # fields are optional so older edge payloads remain valid.
    candidate_model_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    model_decision: Literal["adopted", "rejected", "not_evaluated"] | None = None
    old_model_mae_kw: NonNegativeFinite | None = None
    new_model_mae_kw: NonNegativeFinite | None = None
    model_evaluated_days: Annotated[int, Field(strict=True, ge=0)] | None = None

    sent_at: AwareDatetime

    @model_validator(mode="after")
    def check_consistency(self):
        expected_category = METER_CATEGORIES.get(self.device_id)

        if expected_category is None:
            raise ValueError("Compteur inconnu")

        if self.category != expected_category:
            raise ValueError("La catégorie ne correspond pas au compteur")

        if self.n_comparison_slots > self.n_valid_slots:
            raise ValueError(
                "Les créneaux de comparaison doivent être un "
                "sous-ensemble des créneaux valides"
            )

        if self.n_valid_slots < 90:
            if self.mae_kw is not None:
                raise ValueError(
                    "mae_kw doit être null si moins de 90 créneaux sont valides"
                )
        elif self.mae_kw is None:
            raise ValueError("mae_kw est obligatoire pour une journée évaluable")

        comparison = (
            self.mae_comparison_kw,
            self.mae_persistence_kw,
            self.low_performance,
        )

        if self.n_comparison_slots < 90:
            if any(value is not None for value in comparison):
                raise ValueError(
                    "Les erreurs de comparaison et low_performance "
                    "doivent être null si moins de 90 créneaux sont comparables"
                )
        else:
            if any(value is None for value in comparison):
                raise ValueError(
                    "Les erreurs de comparaison et low_performance "
                    "sont obligatoires pour une comparaison évaluable"
                )

            expected_flag = (
                self.mae_comparison_kw > self.mae_persistence_kw
            )

            if self.low_performance != expected_flag:
                raise ValueError(
                    "low_performance ne correspond pas à la comparaison des MAE"
                )

        return self