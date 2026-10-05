"""Safe, local JSON persistence for one BacklogBridge student state."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping


class StatePersistenceError(Exception):
    """Raised when student state cannot be read, validated, or saved."""


DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "profile": {},
    "subjects": [],
    "syllabi": [],
    "diagnostics": {},
    "practice_attempts": [],
    "tests": [],
    "study_sessions": [],
    "daily_tasks": [],
    "resources": [],
    "settings": {},
}


class StudentStateStore:
    """Read and atomically write a single local student state JSON file.

    No name or other identifying field is required. The default file is
    ``data/student_state.json`` under the project root; callers may supply a
    different path for tests or alternate local storage.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else (
            Path(__file__).resolve().parent.parent / "data" / "student_state.json"
        )

    def load(self) -> dict[str, Any]:
        """Load state, returning defaults if no state file exists."""
        try:
            with self.path.open("r", encoding="utf-8") as state_file:
                value = json.load(state_file)
        except FileNotFoundError:
            return _copy_defaults()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StatePersistenceError(f"Could not read student state: {error}") from error
        return _validate_state(value)

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and atomically persist state, returning its normalized copy."""
        normalized = _validate_state(dict(state))
        parent = self.path.parent
        temporary_path: str | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as state_file:
                temporary_path = state_file.name
                json.dump(normalized, state_file, ensure_ascii=False, indent=2, allow_nan=False)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as error:
            raise StatePersistenceError(f"Could not save student state: {error}") from error
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return normalized

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Merge top-level changes into the current state and save it."""
        if not isinstance(changes, Mapping):
            raise StatePersistenceError("State changes must be a mapping.")
        updated = self.load()
        updated.update(changes)
        return self.save(updated)


def _copy_defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_STATE))


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatePersistenceError("Student state must be a JSON object.")
    state = _copy_defaults()
    state.update(value)
    if state["schema_version"] != 1:
        raise StatePersistenceError(
            f"Unsupported student state schema version: {state['schema_version']!r}."
        )
    if not isinstance(state["profile"], dict):
        raise StatePersistenceError("'profile' must be an object.")
    if not isinstance(state["subjects"], list):
        raise StatePersistenceError("'subjects' must be a list.")
    for key in (
        "subjects",
        "syllabi",
        "practice_attempts",
        "tests",
        "study_sessions",
        "daily_tasks",
        "resources",
    ):
        if not isinstance(state[key], list):
            raise StatePersistenceError(f"'{key}' must be a list.")
    for key in ("profile", "diagnostics", "settings"):
        if not isinstance(state[key], dict):
            raise StatePersistenceError(f"'{key}' must be an object.")
    profile = state["profile"]
    semester = profile.get("semester")
    if semester is not None and (
        isinstance(semester, bool)
        or not isinstance(semester, int)
        or not 1 <= semester <= 20
    ):
        raise StatePersistenceError("'profile.semester' must be an integer from 1 to 20.")
    cgpa = profile.get("cgpa")
    if cgpa is not None:
        if isinstance(cgpa, bool):
            raise StatePersistenceError("'profile.cgpa' must be a number from 0 to 10.")
        try:
            cgpa_value = float(cgpa)
        except (TypeError, ValueError) as error:
            raise StatePersistenceError("'profile.cgpa' must be a number from 0 to 10.") from error
        if not 0 <= cgpa_value <= 10 or not math.isfinite(cgpa_value):
            raise StatePersistenceError("'profile.cgpa' must be a number from 0 to 10.")
    study_minutes = profile.get("daily_study_minutes")
    if study_minutes is not None and (
        isinstance(study_minutes, bool)
        or not isinstance(study_minutes, int)
        or not 10 <= study_minutes <= 600
    ):
        raise StatePersistenceError(
            "'profile.daily_study_minutes' must be an integer from 10 to 600."
        )
    exam_date = profile.get("exam_date")
    if exam_date is not None:
        if not isinstance(exam_date, str):
            raise StatePersistenceError("'profile.exam_date' must be an ISO date or null.")
        try:
            date.fromisoformat(exam_date)
        except ValueError as error:
            raise StatePersistenceError("'profile.exam_date' must be an ISO date or null.") from error
    subject_ids = set()
    for index, subject in enumerate(state["subjects"]):
        if not isinstance(subject, dict):
            raise StatePersistenceError(f"'subjects[{index}]' must be an object.")
        if not isinstance(subject.get("id"), str) or not subject["id"].strip():
            raise StatePersistenceError(f"'subjects[{index}].id' is required.")
        if subject["id"] in subject_ids:
            raise StatePersistenceError("Subject IDs must be unique.")
        subject_ids.add(subject["id"])
        if not isinstance(subject.get("name"), str) or not subject["name"].strip():
            raise StatePersistenceError(f"'subjects[{index}].name' is required.")
        marks = subject.get("marks")
        if isinstance(marks, bool) or not isinstance(marks, int) or not 0 <= marks <= 100:
            raise StatePersistenceError(f"'subjects[{index}].marks' must be an integer from 0 to 100.")
        if subject.get("status") not in {"Passed", "Backlog"}:
            raise StatePersistenceError(f"'subjects[{index}].status' must be Passed or Backlog.")
    for key in (
        "syllabi",
        "practice_attempts",
        "tests",
        "study_sessions",
        "daily_tasks",
        "resources",
    ):
        if any(not isinstance(item, dict) for item in state[key]):
            raise StatePersistenceError(f"Every entry in '{key}' must be an object.")
    syllabus_ids = set()
    for index, syllabus in enumerate(state["syllabi"]):
        if (
            not isinstance(syllabus.get("id"), str)
            or not syllabus["id"].strip()
            or syllabus["id"] in syllabus_ids
            or not isinstance(syllabus.get("subject_id"), str)
            or not isinstance(syllabus.get("confirmed"), bool)
            or not isinstance(syllabus.get("units"), list)
        ):
            raise StatePersistenceError(
                f"'syllabi[{index}]' must have a unique id, subject_id, confirmed flag, and units list."
            )
        syllabus_ids.add(syllabus["id"])
        topic_ids = set()
        for unit_index, unit in enumerate(syllabus["units"]):
            if (
                not isinstance(unit, dict)
                or not isinstance(unit.get("id"), str)
                or not unit["id"].strip()
                or not isinstance(unit.get("name"), str)
                or not unit["name"].strip()
                or not isinstance(unit.get("topics"), list)
            ):
                raise StatePersistenceError(
                    f"'syllabi[{index}].units[{unit_index}]' is invalid."
                )
            for topic_index, topic in enumerate(unit["topics"]):
                if (
                    not isinstance(topic, dict)
                    or not isinstance(topic.get("id"), str)
                    or not topic["id"].strip()
                    or not isinstance(topic.get("name"), str)
                    or not topic["name"].strip()
                    or not isinstance(topic.get("prerequisites", []), list)
                    or not isinstance(topic.get("subtopics", []), list)
                    or any(not isinstance(value, str) for value in topic.get("prerequisites", []))
                    or any(not isinstance(value, str) for value in topic.get("subtopics", []))
                    or topic["id"] in topic_ids
                ):
                    raise StatePersistenceError(
                        f"'syllabi[{index}].units[{unit_index}].topics[{topic_index}]' is invalid."
                    )
                topic_ids.add(topic["id"])
        if any(
            prerequisite not in topic_ids
            for unit in syllabus["units"]
            for topic in unit["topics"]
            for prerequisite in topic.get("prerequisites", [])
        ):
            raise StatePersistenceError(
                f"'syllabi[{index}]' contains a prerequisite outside its topic list."
            )
    for index, resource in enumerate(state["resources"]):
        if resource.get("kind") == "question":
            options = resource.get("options")
            if (
                not isinstance(resource.get("question"), str)
                or not resource["question"].strip()
                or not isinstance(options, list)
                or len(options) < 2
                or any(not isinstance(option, str) or not option.strip() for option in options)
                or len(set(options)) != len(options)
                or resource.get("answer") not in options
                or resource.get("difficulty") not in {"Easy", "Medium", "Hard"}
                or not isinstance(resource.get("subject_id"), str)
                or not isinstance(resource.get("topic_id"), str)
            ):
                raise StatePersistenceError(f"'resources[{index}]' is an invalid practice question.")
    for index, attempt in enumerate(state["tests"] + state["practice_attempts"]):
        correct = attempt.get("correct")
        total = attempt.get("total")
        accuracy = attempt.get("accuracy")
        if (
            not isinstance(attempt.get("subject_id"), str)
            or not isinstance(attempt.get("kind"), str)
            or isinstance(correct, bool)
            or not isinstance(correct, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total <= 0
            or not 0 <= correct <= total
            or isinstance(accuracy, bool)
            or not isinstance(accuracy, (int, float))
            or not math.isfinite(float(accuracy))
            or not 0 <= accuracy <= 100
            or not isinstance(attempt.get("results", []), list)
        ):
            raise StatePersistenceError(
                f"Assessment record {index} is invalid."
            )
        for result_index, result in enumerate(attempt.get("results", [])):
            if not isinstance(result, dict) or not isinstance(
                result.get("correct"), bool
            ):
                raise StatePersistenceError(
                    f"Assessment record {index} result {result_index} is invalid."
                )
    for index, task in enumerate(state["daily_tasks"]):
        if (
            not isinstance(task.get("title"), str)
            or not task["title"].strip()
            or isinstance(task.get("minutes"), bool)
            or not isinstance(task.get("minutes"), int)
            or task["minutes"] <= 0
            or task.get("status")
            not in {"Not Started", "In Progress", "Completed"}
        ):
            raise StatePersistenceError(f"Daily task {index} is invalid.")
    for index, session in enumerate(state["study_sessions"]):
        duration = session.get("duration_minutes")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
        ):
            raise StatePersistenceError(f"Study session {index} has invalid duration.")
    try:
        json.dumps(state, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise StatePersistenceError(f"Student state must contain finite JSON values: {error}") from error
    return state


def load_state(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the local student state, using defaults if its file is absent."""
    return StudentStateStore(path).load()


def save_state(
    state: Mapping[str, Any],
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate and atomically save the local student state."""
    return StudentStateStore(path).save(state)
