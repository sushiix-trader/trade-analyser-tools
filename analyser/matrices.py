"""Labelled matrix values used by portfolio analytics."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .serialization import deterministic_json, to_primitive


@dataclass(frozen=True)
class AnalysisMatrix:
    """An immutable labelled matrix with deterministic serialization."""

    row_labels: tuple[str, ...]
    column_labels: tuple[str, ...]
    values: tuple[tuple[float | None, ...], ...]
    value_type: str

    def __post_init__(self) -> None:
        rows = tuple(tuple(value for value in row) for row in self.values)
        if len(rows) != len(self.row_labels):
            raise ValueError("matrix row count does not match row_labels")
        if any(len(row) != len(self.column_labels) for row in rows):
            raise ValueError("matrix column count does not match column_labels")
        object.__setattr__(self, "row_labels", tuple(str(label) for label in self.row_labels))
        object.__setattr__(self, "column_labels", tuple(str(label) for label in self.column_labels))
        object.__setattr__(self, "values", rows)

    @classmethod
    def from_array(
        cls,
        row_labels: Iterable[str],
        column_labels: Iterable[str],
        values: np.ndarray,
        value_type: str,
    ) -> "AnalysisMatrix":
        array = np.asarray(values, dtype=float)
        rows: list[tuple[float | None, ...]] = []
        for row in array:
            rows.append(tuple(None if not np.isfinite(value) else float(value) for value in row))
        return cls(tuple(row_labels), tuple(column_labels), tuple(rows), value_type)

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.row_labels), len(self.column_labels)

    def to_numpy(self, fill_value: float = np.nan) -> np.ndarray:
        result = np.full(self.shape, fill_value, dtype=float)
        for row_index, row in enumerate(self.values):
            for column_index, value in enumerate(row):
                if value is not None:
                    result[row_index, column_index] = value
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_labels": self.row_labels,
            "column_labels": self.column_labels,
            "values": self.values,
            "value_type": self.value_type,
        }

    def to_json(self) -> str:
        return deterministic_json(self.to_dict())

    def to_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["row"] + list(self.column_labels))
        for label, values in zip(self.row_labels, self.values):
            writer.writerow([label] + list(values))
        return output.getvalue()

    def __repr__(self) -> str:
        return f"AnalysisMatrix(value_type={self.value_type!r}, shape={self.shape!r})"
