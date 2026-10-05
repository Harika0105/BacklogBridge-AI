"""Stable public API for BacklogBridge's local academic-planning backend.

Import application-facing functions and state storage from ``modules``;
document extraction uses ``pypdf`` for PDF and ``python-docx`` for DOCX.
"""

from .engines import (
    calculate_mastery,
    calculate_progress,
    calculate_readiness,
    calculate_risk,
    get_available_topics,
    get_backlog_priority,
    recommend_next_action,
)
from .persistence import (
    StudentStateStore,
    StatePersistenceError,
    load_state,
    save_state,
)
from .syllabus import (
    SyllabusError,
    extract_syllabus_text,
    normalize_syllabus_text,
    parse_syllabus_text,
)

__all__ = [
    "StudentStateStore",
    "StatePersistenceError",
    "SyllabusError",
    "calculate_mastery",
    "calculate_progress",
    "calculate_readiness",
    "calculate_risk",
    "extract_syllabus_text",
    "get_available_topics",
    "get_backlog_priority",
    "load_state",
    "normalize_syllabus_text",
    "parse_syllabus_text",
    "recommend_next_action",
    "save_state",
]
