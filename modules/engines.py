"""Deterministic academic recovery calculations; no network or model calls."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def calculate_risk(backlog_count: int, cgpa: float) -> str:
    """Return the same risk label format consumed by the existing Streamlit UI."""
    if isinstance(backlog_count, bool) or not isinstance(backlog_count, int) or backlog_count < 0:
        raise ValueError("backlog_count must be a non-negative integer.")
    _bounded_number(cgpa, "cgpa", 0, 10)
    if backlog_count == 0 and cgpa >= 7.5:
        return "Low Risk 🟢"
    if backlog_count <= 2 and cgpa >= 6.0:
        return "Medium Risk 🟡"
    return "High Risk 🔴"


def get_backlog_priority(marks: float) -> tuple[str, str]:
    """Return the existing UI's priority label and daily study recommendation."""
    _bounded_number(marks, "marks", 0, 100)
    if marks < 40:
        return "Very High Priority", "2 hours/day"
    if marks < 50:
        return "High Priority", "1.5 hours/day"
    return "Medium Priority", "1 hour/day"


def calculate_mastery(scores: Iterable[float] | Mapping[str, float]) -> float:
    """Return average mastery as a percentage, rounded to one decimal place.

    Inputs are scores on a 0–100 scale. Empty inputs yield 0.
    """
    values = list(scores.values() if isinstance(scores, Mapping) else scores)
    if not values:
        return 0.0
    checked = [_bounded_number(value, "score", 0, 100) for value in values]
    return round(sum(checked) / len(checked), 1)


def calculate_progress(completed: int | Iterable[Any], total: int | None = None) -> float:
    """Return completion percentage; pass counts or completed and total items."""
    if total is None and not isinstance(completed, int):
        items = list(completed)
        done = sum(bool(item.get("completed", False)) if isinstance(item, Mapping) else bool(item)
                   for item in items)
        total = len(items)
    else:
        done = completed
    if isinstance(done, bool) or not isinstance(done, int):
        raise ValueError("completed must be an integer count or iterable of items.")
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("total must be supplied as an integer when completed is a count.")
    if total < 0 or done < 0 or done > total:
        raise ValueError("Progress counts must satisfy 0 <= completed <= total.")
    return round(done / total * 100, 1) if total else 0.0


def get_available_topics(
    topics: Sequence[str],
    completed: Iterable[str] = (),
    prerequisites: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Return source-ordered, incomplete topics whose prerequisites are complete."""
    done = set(completed)
    requirements = prerequisites or {}
    return [
        topic
        for topic in topics
        if topic not in done and set(requirements.get(topic, ())).issubset(done)
    ]


def calculate_readiness(
    mastery: float,
    progress: float,
    prerequisites_met: bool = True,
) -> dict[str, Any]:
    """Score exam readiness from mastery and syllabus progress (0–100 inputs).

    The score weights mastery 70% and syllabus progress 30%; unmet
    prerequisites cap the result below the ready threshold.
    """
    mastery_value = _bounded_number(mastery, "mastery", 0, 100)
    progress_value = _bounded_number(progress, "progress", 0, 100)
    if not isinstance(prerequisites_met, bool):
        raise ValueError("prerequisites_met must be a boolean.")
    score = round(mastery_value * 0.7 + progress_value * 0.3, 1)
    if not prerequisites_met:
        score = min(score, 59.9)
    label = "Ready" if score >= 80 else "Building" if score >= 60 else "Not ready"
    return {"score": score, "label": label, "ready": score >= 80}


def recommend_next_action(
    topics: Sequence[str],
    completed: Iterable[str] = (),
    mastery_by_topic: Mapping[str, float] | None = None,
    prerequisites: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, str] | None:
    """Select the weakest available syllabus topic, preserving syllabus order.

    Topic names are taken only from ``topics``; this helper never creates topics.
    """
    available = get_available_topics(topics, completed, prerequisites)
    if not available:
        return None
    mastery = mastery_by_topic or {}
    for topic in available:
        if topic in mastery:
            _bounded_number(mastery[topic], f"mastery for {topic!r}", 0, 100)
    selected = min(available, key=lambda topic: (mastery.get(topic, 0), topics.index(topic)))
    return {
        "topic": selected,
        "action": "Review the topic, practise a few questions, and record your mastery.",
    }


def _bounded_number(value: float, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number between {lower} and {upper}.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number between {lower} and {upper}.") from error
    if not math.isfinite(number) or number < lower or number > upper:
        raise ValueError(f"{name} must be a number between {lower} and {upper}.")
    return number
