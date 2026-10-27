"""Sensitivity labels and what each trust level may receive. Enforced in code, never by the model."""
from __future__ import annotations

from typing import Iterable, TypeVar

LEVELS = ("public", "internal", "confidential", "restricted")
DEFAULT = "internal"            # a document without a label
CLOUD_MAX = "internal"          # Claude / ChatGPT connections

T = TypeVar("T")


class SensitivityError(Exception):
    """Data above the allowed label was about to leave through a channel that may not receive it."""


def rank(label: str | None) -> int:
    """Unknown labels count as the strictest, so a typo never leaks data."""
    label = label or DEFAULT
    return LEVELS.index(label) if label in LEVELS else len(LEVELS) - 1


def highest(labels: Iterable[str | None]) -> str:
    labels = list(labels)
    if not labels:
        return "public"
    return LEVELS[max(rank(x) for x in labels)]


def max_for(trust: str, local_max: str = "restricted") -> str:
    """Highest label a connection of this trust level may receive."""
    if trust == "local":
        return local_max if local_max in LEVELS else DEFAULT
    return CLOUD_MAX          # "cloud" and anything unknown


def allowed_labels(max_label: str) -> list[str]:
    return list(LEVELS[: rank(max_label) + 1])


def is_allowed(label: str | None, max_label: str) -> bool:
    return rank(label) <= rank(max_label)


def filter_allowed(items: Iterable[T], max_label: str, label_of=lambda x: getattr(x, "sensitivity", None)) -> list[T]:
    """The second, in-Python check after the database filter."""
    return [x for x in items if is_allowed(label_of(x), max_label)]


def require_allowed(label: str | None, max_label: str, what: str = "payload") -> None:
    if not is_allowed(label, max_label):
        raise SensitivityError(f"{what} is labelled '{label or DEFAULT}', above the allowed '{max_label}'")
