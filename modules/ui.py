from __future__ import annotations

import os
import uuid
import hashlib
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import streamlit as st
from dotenv import load_dotenv

from modules import (
    SyllabusError,
    StatePersistenceError,
    calculate_mastery,
    calculate_progress,
    calculate_risk,
    extract_syllabus_text,
    get_available_topics,
    get_backlog_priority,
    load_state,
    save_state,
)


MASTERY_THRESHOLDS = {"mastered": 80, "needs_revision": 60}
READINESS_THRESHOLDS = {
    "not_ready": 40,
    "needs_improvement": 60,
    "almost_ready": 75,
    "ready_for_revision": 90,
}
MAX_SUBJECTS = 20
MAX_QUESTIONS_PER_TOPIC = 50
MAX_MOCK_QUESTIONS = 100
STATE_PATH = os.getenv("BACKLOGBRIDGE_STATE_PATH")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


st.set_page_config(
    page_title="BacklogBridge AI",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)
load_dotenv()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_syllabus(state: dict[str, Any], subject_id: str) -> dict[str, Any] | None:
    return next(
        (
            syllabus
            for syllabus in state["syllabi"]
            if syllabus.get("subject_id") == subject_id and syllabus.get("confirmed")
        ),
        None,
    )


def all_topics(syllabus: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not syllabus:
        return []
    result = []
    for unit in syllabus.get("units", []):
        for topic in unit.get("topics", []):
            result.append(
                {
                    **topic,
                    "unit_id": unit.get("id", ""),
                    "unit_name": unit.get("name", ""),
                }
            )
    return result


def topic_question_bank(
    state: dict[str, Any], subject_id: str, topic_id: str
) -> list[dict[str, Any]]:
    return [
        item
        for item in state["resources"]
        if item.get("kind") == "question"
        and item.get("subject_id") == subject_id
        and item.get("topic_id") == topic_id
    ]


def assessment_records(
    state: dict[str, Any], subject_id: str, topic_id: str, kind: str | None = None
) -> list[dict[str, Any]]:
    records = []
    current_syllabus = get_syllabus(state, subject_id)
    current_syllabus_id = current_syllabus.get("id") if current_syllabus else None
    for attempt in state["tests"] + state["practice_attempts"]:
        if attempt.get("subject_id") != subject_id:
            continue
        if current_syllabus_id and attempt.get("syllabus_id") != current_syllabus_id:
            continue
        if attempt.get("topic_id") == topic_id:
            records.append(attempt)
            continue
        if attempt.get("kind") == "mock":
            topic_results = [
                result
                for result in attempt.get("results", [])
                if result.get("topic_id") == topic_id
            ]
            if topic_results:
                records.append(
                    {
                        **attempt,
                        "topic_id": topic_id,
                        "correct": sum(result.get("correct", False) for result in topic_results),
                        "total": len(topic_results),
                        "accuracy": round(
                            sum(result.get("correct", False) for result in topic_results)
                            / len(topic_results)
                            * 100,
                            1,
                        ),
                    }
                )
    if kind:
        records = [record for record in records if record.get("kind") == kind]
    return sorted(records, key=lambda record: record.get("created_at", ""))


def mastery_evidence(
    state: dict[str, Any], subject_id: str, topic: dict[str, Any]
) -> dict[str, Any]:
    topic_id = topic["id"]
    diagnostic_state = state["diagnostics"].get(subject_id, {})
    syllabus = get_syllabus(state, subject_id)
    diagnostic = diagnostic_state.get("topics", {}).get(topic_id)
    if not syllabus or diagnostic_state.get("syllabus_id") != syllabus.get("id"):
        diagnostic = None
    records = assessment_records(state, subject_id, topic_id)
    practice = [record for record in records if record.get("kind") == "practice"]
    tests = [record for record in records if record.get("kind") in {"mini", "mock"}]

    signals: dict[str, float] = {}
    if diagnostic and isinstance(diagnostic.get("score"), (int, float)):
        signals["diagnostic"] = float(diagnostic["score"])
    if diagnostic and isinstance(diagnostic.get("quiz_score"), (int, float)):
        signals["diagnostic quiz"] = float(diagnostic["quiz_score"])
    if practice:
        signals["practice"] = calculate_mastery(
            [record["accuracy"] for record in practice]
        )
    if tests:
        signals["tests"] = calculate_mastery(
            [record["accuracy"] for record in tests[-3:]]
        )

    task_complete = any(
        task.get("topic_id") == topic_id and task.get("status") == "Completed"
        for task in state["daily_tasks"]
    ) or any(
        session.get("topic_id") == topic_id and session.get("completed")
        for session in state["study_sessions"]
    )
    score = calculate_mastery(signals) if signals else None
    assessments = len(signals)
    recent_test = tests[-1]["accuracy"] if tests else None
    if score is None:
        status = "Not Started"
    elif (
        recent_test is not None
        and recent_test < MASTERY_THRESHOLDS["needs_revision"]
    ):
        status = "Needs Revision"
    elif score < MASTERY_THRESHOLDS["needs_revision"]:
        status = "Needs Revision"
    elif recent_test is not None and recent_test < MASTERY_THRESHOLDS["mastered"]:
        status = "Needs Practice"
    elif score < MASTERY_THRESHOLDS["mastered"]:
        status = "Needs Practice"
    elif (
        assessments >= 2
        and len(tests) >= 2
        and task_complete
        and recent_test is not None
        and recent_test >= MASTERY_THRESHOLDS["mastered"]
    ):
        status = "Mastered"
    else:
        status = "Learning"
    return {
        "score": score,
        "signals": signals,
        "assessments": assessments,
        "task_complete": task_complete,
        "recent_test": recent_test,
        "status": status,
    }


def topic_category(evidence: dict[str, Any]) -> str:
    score = evidence["score"]
    if evidence["status"] == "Mastered":
        return "Strong · mastered"
    if score is None:
        return "Not assessed"
    if score < MASTERY_THRESHOLDS["needs_revision"]:
        return "Weak / missed"
    if score < MASTERY_THRESHOLDS["mastered"]:
        return "Needs revision"
    return "Strong signal · repeat assessment needed"


def prerequisite_gaps(
    state: dict[str, Any], subject_id: str, syllabus: dict[str, Any]
) -> dict[str, list[str]]:
    topics = all_topics(syllabus)
    by_id = {topic["id"]: topic for topic in topics}
    gaps: dict[str, list[str]] = {}
    for topic in topics:
        missing = []
        for prerequisite_id in topic.get("prerequisites", []):
            prerequisite = by_id.get(prerequisite_id)
            if prerequisite and mastery_evidence(state, subject_id, prerequisite)["status"] != "Mastered":
                missing.append(prerequisite["name"])
        if missing:
            gaps[topic["id"]] = missing
    return gaps


def available_topic_objects(
    state: dict[str, Any], subject_id: str, syllabus: dict[str, Any]
) -> list[dict[str, Any]]:
    topics = all_topics(syllabus)
    completed_names = [
        topic["name"]
        for topic in topics
        if mastery_evidence(state, subject_id, topic)["status"] == "Mastered"
    ]
    available_names = get_available_topics(
        [topic["id"] for topic in topics],
        [
            topic["id"]
            for topic in topics
            if mastery_evidence(state, subject_id, topic)["status"] == "Mastered"
        ],
        {topic["id"]: topic.get("prerequisites", []) for topic in topics},
    )
    return [topic for topic in topics if topic["id"] in available_names]


def weak_topics(
    state: dict[str, Any], subject_id: str, syllabus: dict[str, Any]
) -> list[dict[str, Any]]:
    result = []
    for topic in all_topics(syllabus):
        evidence = mastery_evidence(state, subject_id, topic)
        mistakes = Counter(
            item.get("concept", topic["name"])
            for attempt in assessment_records(state, subject_id, topic["id"])
            for item in attempt.get("results", [])
            if not item.get("correct")
        )
        repeated_mistakes = [
            concept for concept, count in mistakes.items() if count >= 2
        ]
        if evidence["status"] in {"Needs Revision", "Needs Practice"} or (
            evidence["score"] is None
            and state["diagnostics"].get(subject_id, {}).get("topics", {}).get(
                topic["id"], {}
            ).get("level")
            in {"A little", "Not yet"}
        ) or repeated_mistakes:
            result.append({
                **topic,
                **evidence,
                "repeated_mistakes": repeated_mistakes,
            })
    return result


def subject_priority(
    state: dict[str, Any], subject: dict[str, Any]
) -> tuple[float, str, str]:
    syllabus = get_syllabus(state, subject["id"])
    backlog = subject.get("status") == "Backlog"
    points = 35 if backlog else 0
    reasons = []
    if backlog:
        reasons.append(f"backlog subject ({subject.get('marks', 0)} marks)")
    mark_priority, recommended_study = get_backlog_priority(subject.get("marks", 100))
    if subject.get("marks", 100) < 50:
        points += (50 - subject["marks"]) * 0.5
        reasons.append(
            f"marks-based recovery priority is {mark_priority.lower()} "
            f"({recommended_study} recommended)"
        )
    if syllabus:
        gaps = weak_topics(state, subject["id"], syllabus)
        if gaps:
            points += min(30, len(gaps) * 8)
            reasons.append(f"{len(gaps)} topic(s) currently need attention")
        diag_topics = state["diagnostics"].get(subject["id"], {}).get("topics", {})
        scores = [item.get("score") for item in diag_topics.values() if item.get("score") is not None]
        if scores and calculate_mastery(scores) < 60:
            points += 15
            reasons.append("diagnostic evidence shows foundational gaps")
    exam_date = state["profile"].get("exam_date")
    if backlog and exam_date:
        try:
            days_left = (date.fromisoformat(exam_date) - date.today()).days
            if 0 <= days_left <= 30:
                points += 20
                reasons.append("the entered exam date is approaching")
        except ValueError:
            pass
    label = "HIGH" if points >= 60 else "MEDIUM" if points >= 35 else "LOW"
    reason = "; ".join(reasons) if reasons else "no recorded backlog or weak-topic evidence"
    return points, label, reason


def academic_risk(state: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    subjects = [
        subject for subject in state["subjects"]
        if subject.get("name", "").strip()
    ]
    backlog_count = sum(subject.get("status") == "Backlog" for subject in subjects)
    cgpa = float(state["profile"].get("cgpa", 0))
    base = calculate_risk(backlog_count, cgpa)
    levels = {"Low Risk": 0, "Medium Risk": 1, "High Risk": 2}
    rank = next(value for label, value in levels.items() if label in base)
    reasons = []
    weak_subject_list = []

    for subject in subjects:
        if subject.get("marks", 100) < 50:
            weak_subject_list.append(subject)
        syllabus = get_syllabus(state, subject["id"])
        if syllabus:
            weak_count = len(weak_topics(state, subject["id"], syllabus))
            diagnostic_topics = state["diagnostics"].get(subject["id"], {}).get("topics", {})
            diagnostic_scores = [
                item["score"] for item in diagnostic_topics.values()
                if isinstance(item.get("score"), (int, float))
            ]
            if weak_count >= 3:
                rank = max(rank, 2 if backlog_count else 1)
                reasons.append(f"{subject['name']} has {weak_count} topics needing attention")
            if diagnostic_scores and calculate_mastery(diagnostic_scores) < 40:
                rank = max(rank, 2 if backlog_count else 1)
                reasons.append(f"{subject['name']} diagnostic results show foundational gaps")

    if len(weak_subject_list) >= 3:
        rank = min(2, rank + 1)
        reasons.append(f"{len(weak_subject_list)} subjects have marks below 50")
    for subject in subjects:
        relevant_tests = sorted(
            [
                record for record in state["tests"]
                if record.get("subject_id") == subject["id"]
                and record.get("kind") in {"mini", "mock"}
            ],
            key=lambda record: record.get("created_at", ""),
        )
        if len(relevant_tests) >= 2 and (
            relevant_tests[-1]["accuracy"] - relevant_tests[-2]["accuracy"] <= -15
        ):
            rank = min(2, rank + 1)
            reasons.append(f"{subject['name']} recent test performance has declined")
            break

    label = ["Low Risk 🟢", "Medium Risk 🟡", "High Risk 🔴"][rank]
    if not reasons:
        reasons.append("current risk is based on the saved CGPA and automatically calculated backlog count")
    return label, reasons, weak_subject_list


def readiness_summary(
    state: dict[str, Any], subject: dict[str, Any]
) -> dict[str, Any] | None:
    syllabus = get_syllabus(state, subject["id"])
    if not syllabus:
        return None
    topics = all_topics(syllabus)
    diagnostic_state = state["diagnostics"].get(subject["id"], {})
    diagnostics = diagnostic_state.get("topics", {})
    topic_ids = {topic["id"] for topic in topics}
    if (
        not topics
        or diagnostic_state.get("syllabus_id") != syllabus.get("id")
        or not topic_ids.issubset(diagnostics)
    ):
        return None
    records = [
        record
        for record in state["tests"] + state["practice_attempts"]
        if record.get("subject_id") == subject["id"]
        and record.get("syllabus_id") == syllabus["id"]
    ]
    practice = [record for record in records if record.get("kind") == "practice"]
    mini = [record for record in records if record.get("kind") == "mini"]
    mock = [record for record in records if record.get("kind") == "mock"]
    full_mocks = [record for record in mock if record.get("coverage_percent") == 100]
    if not practice or not mini or not full_mocks:
        return None

    topic_evidence = [mastery_evidence(state, subject["id"], topic) for topic in topics]
    mastered = sum(evidence["status"] == "Mastered" for evidence in topic_evidence)
    completed = sum(evidence["task_complete"] for evidence in topic_evidence)
    gap_map = prerequisite_gaps(state, subject["id"], syllabus)
    unresolved_weak = len(weak_topics(state, subject["id"], syllabus))
    diagnostic_scores = [
        score
        for topic in topics
        for score in (
            diagnostics[topic["id"]].get("score"),
            diagnostics[topic["id"]].get("quiz_score"),
        )
        if isinstance(score, (int, float))
    ]
    unit_completion = calculate_mastery(
        [
            calculate_progress(
                sum(
                    mastery_evidence(state, subject["id"], topic)["task_complete"]
                    for topic in unit["topics"]
                ),
                len(unit["topics"]),
            )
            for unit in syllabus["units"]
            if unit["topics"]
        ]
    )
    trend_records = sorted(mini + mock, key=lambda record: record.get("created_at", ""))
    trend_difference = (
        trend_records[-1]["accuracy"] - trend_records[-2]["accuracy"]
        if len(trend_records) >= 2
        else None
    )
    trend = (
        "Improving"
        if trend_difference is not None and trend_difference > 5
        else "Declining"
        if trend_difference is not None and trend_difference < -5
        else "Stable"
        if trend_difference is not None
        else "Not enough recent tests"
    )
    exam_date = state["profile"].get("exam_date")
    days_until_exam = None
    if exam_date:
        try:
            days_until_exam = (date.fromisoformat(exam_date) - date.today()).days
        except ValueError:
            days_until_exam = None
    breakdown = {
        "Syllabus completion": calculate_progress(completed, len(topics)),
        "Unit completion": unit_completion,
        "Topic mastery": calculate_progress(mastered, len(topics)),
        "Diagnostic performance": calculate_mastery(diagnostic_scores),
        "Practice accuracy": calculate_mastery([item["accuracy"] for item in practice]),
        "Mini-test performance": calculate_mastery([item["accuracy"] for item in mini]),
        "Mock-test performance": calculate_mastery([item["accuracy"] for item in mock]) if mock else None,
        "Revision completion": calculate_progress(
            sum(item.get("status") == "Completed" for item in state["daily_tasks"]
                if item.get("subject_id") == subject["id"] and item.get("kind") == "Revision"),
            sum(item.get("kind") == "Revision" for item in state["daily_tasks"]
                if item.get("subject_id") == subject["id"]),
        ),
        "Weak topics remaining": unresolved_weak,
        "Prerequisite gaps": len(gap_map),
        "Recent performance trend": trend,
        "Days until exam": days_until_exam,
    }
    components = [
        (breakdown["Syllabus completion"], 0.15),
        (breakdown["Unit completion"], 0.05),
        (breakdown["Topic mastery"], 0.20),
        (breakdown["Diagnostic performance"], 0.10),
        (breakdown["Practice accuracy"], 0.15),
        (breakdown["Mini-test performance"], 0.15),
        (breakdown["Revision completion"], 0.10),
    ]
    if breakdown["Mock-test performance"] is not None:
        components.append((breakdown["Mock-test performance"], 0.10))
        weight_total = 1.0
    else:
        weight_total = 0.90
    score = round(sum(value * weight for value, weight in components) / weight_total)
    if trend_difference is not None:
        score += 3 if trend_difference > 5 else -3 if trend_difference < -5 else 0
    score -= min(25, unresolved_weak * 5)
    score -= min(20, len(gap_map) * 7)
    score = max(0, min(100, score))
    if len(mock) < 2:
        score = min(score, READINESS_THRESHOLDS["ready_for_revision"] - 1)
    if score < READINESS_THRESHOLDS["not_ready"]:
        level, advice, action = (
            "NOT READY",
            "Your preparation is still at an early stage. Focus on prerequisite topics and high-priority gaps.",
            "Start with your highest-priority prerequisite topic.",
        )
    elif score < READINESS_THRESHOLDS["needs_improvement"]:
        level, advice, action = (
            "NEEDS IMPROVEMENT",
            "You have made progress, but several topics need focused practice and revision.",
            "Complete weak topics and retake their mini tests.",
        )
    elif score < READINESS_THRESHOLDS["almost_ready"]:
        level, advice, action = (
            "ALMOST READY",
            "Your preparation is progressing. Revise the remaining weak areas before a mock test.",
            "Revise weak topics and take another mock test.",
        )
    elif score < READINESS_THRESHOLDS["ready_for_revision"]:
        level, advice, action = (
            "READY FOR REVISION",
            "Your preparation indicators suggest that you are ready to begin final revision.",
            "Start full-syllabus revision and complete a mock examination.",
        )
    else:
        level, advice, action = (
            "STRONG PREPARATION",
            "Your recorded preparation indicators are strong. Continue revision and timed practice.",
            "Complete final revision and timed mock tests.",
        )
    return {
        "score": score,
        "level": level,
        "advice": advice,
        "action": action,
        "breakdown": breakdown,
        "topics": topics,
        "weak": weak_topics(state, subject["id"], syllabus),
        "gaps": gap_map,
    }


def next_action(state: dict[str, Any]) -> tuple[str, str]:
    subjects = state["subjects"]
    if not subjects:
        return "Add your academic profile", "Enter subjects and select Passed or Backlog."
    backlog_subjects = [subject for subject in subjects if subject.get("status") == "Backlog"]
    target_subjects = backlog_subjects or subjects
    priority_subject = max(
        target_subjects,
        key=lambda subject: subject_priority(state, subject)[0],
    )
    syllabus = get_syllabus(state, priority_subject["id"])
    if not syllabus:
        return "Add the actual syllabus", f"Add and confirm the syllabus for {priority_subject['name']}."
    topics = all_topics(syllabus)
    if not topics:
        return "Review the syllabus", "Add at least one confirmed topic before topic-level planning."
    diagnostic_state = state["diagnostics"].get(priority_subject["id"], {})
    topic_ids = {topic["id"] for topic in topics}
    if (
        diagnostic_state.get("syllabus_id") != syllabus.get("id")
        or not topic_ids.issubset(diagnostic_state.get("topics", {}))
    ):
        return "Complete the diagnostic", f"Assess your current knowledge of {priority_subject['name']} topics."
    gaps = prerequisite_gaps(state, priority_subject["id"], syllabus)
    if gaps:
        topic = next(topic for topic in topics if topic["id"] in gaps)
        return "Study a prerequisite", f"Review {', '.join(gaps[topic['id']])} before {topic['name']}."
    available = get_available_topics(
        [topic["id"] for topic in topics],
        completed=[
            topic["id"]
            for topic in topics
            if mastery_evidence(state, priority_subject["id"], topic)["status"] == "Mastered"
        ],
        prerequisites={
            topic["id"]: topic.get("prerequisites", []) for topic in topics
        },
    )
    if available:
        topic = next(item for item in topics if item["id"] in available)
        evidence = mastery_evidence(state, priority_subject["id"], topic)
        repeated = next(
            (
                item for item in weak_topics(state, priority_subject["id"], syllabus)
                if item["id"] == topic["id"] and item.get("repeated_mistakes")
            ),
            None,
        )
        if repeated:
            return "Review repeated mistakes", f"Revisit {', '.join(repeated['repeated_mistakes'])} in {topic['name']}."
        if evidence["status"] in {"Needs Revision", "Needs Practice"}:
            return "Revise or practise", f"Continue with {topic['name']} in {priority_subject['name']}."
        return "Continue the roadmap", f"Study {topic['name']} next in {priority_subject['name']}."
    return "Take a subject mock test", f"Review overall coverage for {priority_subject['name']}."


def persist() -> bool:
    try:
        st.session_state["state"] = save_state(st.session_state["state"], STATE_PATH)
    except (StatePersistenceError, OSError, ValueError) as error:
        st.error(f"Your changes could not be saved locally: {error}")
        return False
    return True


def subject_options(state: dict[str, Any], backlogs_only: bool = False) -> list[dict[str, Any]]:
    subjects = state["subjects"]
    if backlogs_only:
        backlogs = [subject for subject in subjects if subject.get("status") == "Backlog"]
        return backlogs or subjects
    return subjects


def render_dashboard(state: dict[str, Any]) -> None:
    st.title("Your recovery dashboard")
    st.caption("A supportive, evidence-based view of your academic recovery.")
    subjects = state["subjects"]
    backlogs = [
        subject
        for subject in subjects
        if subject.get("name", "").strip() and subject.get("status") == "Backlog"
    ]
    backlog_count = len(backlogs)
    cgpa = state["profile"].get("cgpa", 0)
    risk, risk_reasons, weak_subject_list = academic_risk(state) if state["profile"] else ("Not assessed", [], [])
    assessments = state["tests"] + state["practice_attempts"]
    latest_test = next(
        (item for item in reversed(sorted(assessments, key=lambda row: row.get("created_at", "")))
         if item.get("kind") in {"mini", "mock"}),
        None,
    )
    completed_tasks = sum(task.get("status") == "Completed" for task in state["daily_tasks"])
    total_tasks = len(state["daily_tasks"])
    progress = calculate_progress(completed_tasks, total_tasks)
    priorities = sorted(
        ((subject_priority(state, subject), subject) for subject in backlogs),
        key=lambda item: item[0][0],
        reverse=True,
    )
    primary = priorities[0][1] if priorities else None
    primary_syllabus = get_syllabus(state, primary["id"]) if primary else None
    primary_weak = weak_topics(state, primary["id"], primary_syllabus) if primary_syllabus else []
    action, explanation = next_action(state)

    metrics = st.columns(6)
    metrics[0].metric("Academic risk", risk)
    metrics[1].metric("Backlogs", backlog_count)
    metrics[2].metric("CGPA", f"{cgpa:.1f}" if state["profile"] else "—")
    metrics[3].metric("Recorded task progress", f"{progress:.0f}%")
    metrics[4].metric("Latest mini/mock test", f"{latest_test['accuracy']:.0f}%" if latest_test else "—")
    metrics[5].metric("Weak subjects", len(weak_subject_list))

    st.subheader("Next best action")
    st.info(f"**{action}:** {explanation}")
    if risk_reasons:
        st.caption("Risk factors: " + "; ".join(risk_reasons))
    if weak_subject_list:
        st.caption("Subjects needing attention: " + ", ".join(subject["name"] for subject in weak_subject_list))
    if primary:
        cols = st.columns(3)
        cols[0].write(f"**Priority subject:** {primary['name']}")
        cols[1].write(f"**Weak topics:** {len(primary_weak)}" if primary_syllabus else "**Weak topics:** add syllabus")
        readiness = readiness_summary(state, primary)
        cols[2].write(f"**Exam readiness:** {readiness['level']}" if readiness else "**Exam readiness:** not enough evidence yet")
    else:
        st.write("Add your academic profile to begin. Backlog count is calculated from named subjects marked Backlog.")

    st.subheader("Today's tasks")
    today = date.today().isoformat()
    today_tasks = [task for task in state["daily_tasks"] if task.get("date") == today]
    if today_tasks:
        for task in today_tasks:
            st.write(f"{'✅' if task.get('status') == 'Completed' else '◻️'} {task['title']} — {task['minutes']} min")
    else:
        st.caption("Your study activity will appear after you create today's plan.")

    st.subheader("Recent activity")
    activities = []
    for session in state["study_sessions"]:
        activities.append((session.get("created_at", ""), f"Studied {session.get('topic_name', 'a topic')} for {session.get('duration_minutes', 0)} min"))
    for record in state["tests"] + state["practice_attempts"]:
        label = "mini test" if record.get("kind") == "mini" else "mock test" if record.get("kind") == "mock" else "practice"
        activities.append((record.get("created_at", ""), f"{label.title()}: {record.get('accuracy', 0):.0f}% on {record.get('topic_name', record.get('subject_name', 'subject'))}"))
    if activities:
        for _, description in sorted(activities, reverse=True)[:5]:
            st.write(f"• {description}")
    else:
        st.caption("No study or assessment activity recorded yet.")


def render_profile(state: dict[str, Any]) -> None:
    st.title("Academic profile")
    st.write("Only academic information needed for recovery planning is requested. No student name is required.")
    profile = state["profile"]
    subjects = state["subjects"]
    initial_count = len(subjects) or None
    with st.form("academic_profile_form"):
        cols = st.columns(3)
        semester = cols[0].number_input(
            "Current semester",
            1,
            20,
            int(profile["semester"]) if profile.get("semester") is not None else None,
            1,
        )
        cgpa = cols[1].number_input(
            "Current CGPA",
            0.0,
            10.0,
            float(profile["cgpa"]) if profile.get("cgpa") is not None else None,
            0.1,
            format="%.1f",
        )
        subject_count = cols[2].number_input(
            "Number of subjects", 1, MAX_SUBJECTS, initial_count, 1
        )
        study_minutes = st.number_input(
            "Available study time per day (minutes)",
            min_value=10,
            max_value=600,
            value=(
                int(profile["daily_study_minutes"])
                if profile.get("daily_study_minutes") is not None
                else None
            ),
            step=5,
        )
        exam_date_value = profile.get("exam_date")
        exam_date = st.date_input(
            "Backlog exam date (optional)",
            value=date.fromisoformat(exam_date_value) if exam_date_value else None,
        )
        rows = []
        st.markdown("#### Subjects")
        for index in range(int(subject_count or 0)):
            previous = subjects[index] if index < len(subjects) else {}
            row = st.columns([2, 1, 1])
            name = row[0].text_input(
                f"Subject {index + 1}",
                value=previous.get("name", ""),
                key=f"profile_subject_name_{index}",
                max_chars=120,
                placeholder="Subject name",
            )
            marks = row[1].number_input(
                "Marks",
                min_value=0,
                max_value=100,
                value=int(previous["marks"]) if previous.get("marks") is not None else None,
                step=1,
                key=f"profile_marks_{index}",
            )
            status = row[2].selectbox(
                "Status",
                options=["Passed", "Backlog"],
                index=1 if previous.get("status") == "Backlog" else 0,
                key=f"profile_status_{index}",
            )
            rows.append({
                "id": previous.get("id", str(uuid.uuid4())),
                "name": name.strip(),
                "marks": int(marks) if marks is not None else None,
                "status": status,
            })
        submitted = st.form_submit_button("Save academic profile", type="primary")
    if submitted:
        if semester is None or cgpa is None or subject_count is None or study_minutes is None:
            st.error(
                "Enter your semester, CGPA, number of subjects, and available daily study time."
            )
            return
        named = [subject for subject in rows if subject["name"]]
        if any(subject["marks"] is None for subject in named):
            st.error("Please enter marks for each named subject.")
            return
        if any(not 0 <= subject["marks"] <= 100 for subject in named):
            st.error("Please enter marks between 0 and 100.")
            return
        state["profile"] = {
            **profile,
            "semester": int(semester),
            "cgpa": float(cgpa),
            "daily_study_minutes": int(study_minutes),
            "exam_date": exam_date.isoformat() if exam_date else None,
        }
        state["subjects"] = named
        if persist():
            st.success(f"Profile saved. Backlog count: {sum(item['status'] == 'Backlog' for item in named)} (calculated from subject status).")
            st.rerun()
    st.caption("Blank subject names are ignored. Backlogs are never entered separately; they are counted from the saved subject statuses.")


def parse_structured_syllabus(text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("unit ") and ":" in line:
            unit_name = line.split(":", 1)[1].strip() or line.strip()
            current = {"id": str(uuid.uuid4()), "name": unit_name, "topics": []}
            units.append(current)
        elif current is not None:
            is_subtopic = line.startswith((">", "--"))
            clean = line.lstrip("-*•> ").strip()
            if not clean:
                continue
            if is_subtopic and current["topics"]:
                current["topics"][-1]["subtopics"].append(clean)
            else:
                current["topics"].append(
                    {
                        "id": str(uuid.uuid4()),
                        "name": clean,
                        "subtopics": [],
                        "prerequisites": [],
                    }
                )
    return units


def has_prerequisite_cycle(graph: dict[str, list[str]]) -> bool:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(topic: str) -> bool:
        if topic in active:
            return True
        if topic in visited:
            return False
        active.add(topic)
        if any(visit(prerequisite) for prerequisite in graph.get(topic, [])):
            return True
        active.remove(topic)
        visited.add(topic)
        return False

    return any(visit(topic) for topic in graph)


def render_syllabus(state: dict[str, Any]) -> None:
    st.title("Syllabus setup")
    st.write("Use your actual college syllabus. Uploaded content is only a draft until you review and confirm it.")
    subjects = subject_options(state)
    if not subjects:
        st.info("Save your academic profile and subjects first.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="syllabus_subject")
    existing = get_syllabus(state, subject["id"])
    draft_key = f"syllabus_draft_{subject['id']}"
    default_text = ""
    if existing:
        lines = []
        for unit_index, unit in enumerate(existing.get("units", []), start=1):
            lines.append(f"Unit {unit_index}: {unit['name']}")
            for topic in unit.get("topics", []):
                lines.append(f"- {topic['name']}")
                lines.extend(f"  > {subtopic}" for subtopic in topic.get("subtopics", []))
        default_text = "\n".join(lines)

    uploaded = st.file_uploader(
        "Optional syllabus file (PDF, TXT, DOCX; max 5 MB)",
        type=["pdf", "txt", "docx"],
        key=f"syllabus_upload_{subject['id']}",
    )
    if uploaded:
        try:
            uploaded_bytes = uploaded.getvalue()
            upload_hash = hashlib.sha256(uploaded_bytes).hexdigest()
            hash_key = f"syllabus_upload_hash_{subject['id']}"
            if st.session_state.get(hash_key) != upload_hash:
                extracted = extract_syllabus_text(uploaded.name, uploaded_bytes)
                st.session_state[draft_key] = extracted
                st.session_state[hash_key] = upload_hash
                st.success("File text extracted locally. Review and edit it below before saving.")
        except SyllabusError as error:
            st.error(f"Could not process this syllabus file: {error}")
    if draft_key not in st.session_state:
        st.session_state[draft_key] = default_text
    text = st.text_area(
        "Edit the syllabus draft",
        key=draft_key,
        height=320,
        max_chars=30000,
        help="Format: Unit 1: Unit title, then one topic per line with '-'. Prefix subtopics with '>'.",
        placeholder="Unit 1: [actual unit title]\n- [topic from your syllabus]\n  > [subtopic, if listed]\n\nUnit 2: [actual unit title]\n- [another syllabus topic]",
    )
    st.caption("No topics are generated or assumed. Include only the topics present in your own syllabus.")
    if st.button("Review extracted structure", key="preview_syllabus"):
        units = parse_structured_syllabus(text)
        topic_count = sum(len(unit["topics"]) for unit in units)
        if not units or not topic_count:
            st.error("Add at least one explicit unit heading (`Unit 1: ...`) and topic before confirming.")
        else:
            st.session_state[f"syllabus_preview_{subject['id']}"] = {
                "source_text": text,
                "units": units,
            }
            st.success(f"Draft structure contains {len(units)} unit(s) and {topic_count} topic(s). Review or edit the source text above.")
    preview = st.session_state.get(f"syllabus_preview_{subject['id']}")
    if preview and preview.get("source_text") != text:
        st.info("The draft has changed since the preview. Review its updated structure before confirming.")
        preview = None
    if preview:
        with st.expander("Preview syllabus structure", expanded=True):
            for unit in preview["units"]:
                st.markdown(f"**{unit['name']}**")
                for topic in unit["topics"]:
                    st.write(f"• {topic['name']}")
                    for subtopic in topic["subtopics"]:
                        st.write(f"  ↳ {subtopic}")
        if st.button("Confirm and save this syllabus", type="primary", key="confirm_syllabus"):
            current_units = parse_structured_syllabus(text)
            if not current_units or not sum(len(unit["topics"]) for unit in current_units):
                st.error("Add at least one explicit unit heading and topic before confirming.")
                return
            state["syllabi"] = [
                item for item in state["syllabi"] if item.get("subject_id") != subject["id"]
            ]
            state["diagnostics"].pop(subject["id"], None)
            saved_syllabus = {
                    "id": str(uuid.uuid4()),
                    "subject_id": subject["id"],
                    "confirmed": True,
                    "confirmed_at": now_iso(),
                    "source": "student-reviewed",
                    "units": current_units,
                }
            state["syllabi"].append(saved_syllabus)
            if persist():
                st.session_state.pop(f"syllabus_preview_{subject['id']}", None)
                st.success("Confirmed syllabus saved. It is now the source for topic-level planning.")
                st.rerun()
    if existing:
        st.subheader("Prerequisite mapping")
        topics = all_topics(existing)
        latest_syllabus = get_syllabus(state, subject["id"])
        if latest_syllabus and latest_syllabus.get("id") == existing.get("id"):
            topics = all_topics(latest_syllabus)
        topic_labels = {
            topic["id"]: f"{topic['unit_name']} — {topic['name']}" for topic in topics
        }
        edited_prerequisites = {}
        for topic in topics:
            possible = [item["id"] for item in topics if item["id"] != topic["id"]]
            selected = st.multiselect(
                f"Topics to understand before {topic['name']}",
                options=possible,
                default=[
                    prerequisite_id
                    for prerequisite_id in topic.get("prerequisites", [])
                    if prerequisite_id in possible
                ],
                format_func=lambda topic_id: topic_labels[topic_id],
                key=f"prereq_{topic['id']}",
            )
            edited_prerequisites[topic["id"]] = selected
        if st.button("Save prerequisite relationships", key="save_prerequisites"):
            if has_prerequisite_cycle(edited_prerequisites):
                st.error("This prerequisite map contains a cycle. Adjust the relationships so every topic has a learnable order.")
                return
            for topic in topics:
                topic["prerequisites"] = edited_prerequisites[topic["id"]]
            if persist():
                st.success("Prerequisite map saved. Topics with gaps will be sequenced after their prerequisites.")


def render_diagnostic(state: dict[str, Any]) -> None:
    st.title("Topic diagnostic")
    st.write("Start with an honest self-check. Your responses guide the plan; they are not a judgement.")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add a subject and confirm its actual syllabus before starting a diagnostic.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="diagnostic_subject")
    syllabus = get_syllabus(state, subject["id"])
    topics = all_topics(syllabus)
    if not topics:
        st.info("No confirmed topics are available yet. Add and confirm this subject's syllabus first.")
        return
    saved = state["diagnostics"].get(subject["id"], {}).get("topics", {})
    with st.form(f"diagnostic_form_{subject['id']}"):
        ratings = {}
        for topic in topics:
            st.caption(topic["unit_name"])
            ratings[topic["id"]] = st.radio(
                f"{topic['name']} — how well do you know this?",
                ["I know this", "I know a little", "I don't know this"],
                index={
                    "I know this": 0,
                    "Know": 0,
                    "A little": 1,
                    "Not yet": 2,
                }.get(saved.get(topic["id"], {}).get("level"), 1),
                key=f"diagnostic_rating_{topic['id']}",
                horizontal=True,
            )
        submitted = st.form_submit_button("Save diagnostic", type="primary")
    if submitted:
        score_map = {"I know this": 100.0, "I know a little": 50.0, "I don't know this": 0.0}
        current_syllabus = get_syllabus(state, subject["id"])
        previous_topics = state["diagnostics"].get(subject["id"], {}).get("topics", {})
        state["diagnostics"][subject["id"]] = {
            "syllabus_id": current_syllabus["id"],
            "completed_at": now_iso(),
            "topics": {
                topic_id: {
                    "level": {"I know this": "Know", "I know a little": "A little", "I don't know this": "Not yet"}[rating],
                    "score": score_map[rating],
                    **(
                        {"quiz_score": previous_topics[topic_id]["quiz_score"]}
                        if topic_id in previous_topics and "quiz_score" in previous_topics[topic_id]
                        else {}
                    ),
                }
                for topic_id, rating in ratings.items()
            },
        }
        if persist():
            st.success("Diagnostic saved. Topic-level ratings have been added to your recovery profile.")
            st.rerun()

    st.subheader("Short diagnostic quiz")
    st.caption("Quiz questions must come from your own notes, course materials, or questions you add in Learning & Practice.")
    topic = st.selectbox("Choose a topic", topics, format_func=lambda item: f"{item['unit_name']} — {item['name']}", key="diagnostic_quiz_topic")
    questions = topic_question_bank(state, subject["id"], topic["id"])
    if questions:
        render_quiz_form(state, subject, topic, questions[:5], "diagnostic")
    else:
        st.info("No student-provided questions exist for this topic yet. Add questions under Learning & Practice to enable a scored quiz.")


def render_quiz_form(
    state: dict[str, Any],
    subject: dict[str, Any],
    topic: dict[str, Any],
    questions: list[dict[str, Any]],
    kind: str,
) -> None:
    if not questions:
        st.info("Add a question from your confirmed syllabus topic to start.")
        return
    timer_key = f"quiz_started_{kind}_{subject['id']}_{topic['id']}"
    active_key = f"quiz_active_{kind}_{subject['id']}_{topic['id']}"
    if not st.session_state.get(active_key):
        if st.button(
            f"Start {kind.replace('_', ' ')}",
            key=f"start_{kind}_{subject['id']}_{topic['id']}",
            type="primary",
        ):
            st.session_state[timer_key] = time.monotonic()
            st.session_state[active_key] = True
            st.rerun()
        st.caption(f"{len(questions)} question(s) · answers are saved after submission.")
        return
    with st.form(f"quiz_{kind}_{subject['id']}_{topic['id']}"):
        answers = {}
        for index, question in enumerate(questions):
            st.markdown(f"**{index + 1}. {question['question']}**")
            answers[question["id"]] = st.radio(
                "Select an answer",
                question["options"],
                key=f"answer_{kind}_{question['id']}",
                label_visibility="collapsed",
            )
        submitted = st.form_submit_button(
            "Submit practice" if kind == "practice" else "Submit diagnostic quiz" if kind == "diagnostic" else "Submit mini test",
            type="primary",
        )
    if submitted:
        results = []
        for question in questions:
            selected = answers[question["id"]]
            correct = selected == question["answer"]
            results.append(
                {
                    "question_id": question["id"],
                    "question": question["question"],
                    "selected": selected,
                    "correct": correct,
                    "concept": question.get("concept", topic["name"]),
                    "explanation": question.get("explanation", ""),
                    "difficulty": question.get("difficulty", "Medium"),
                }
            )
        correct_count = sum(item["correct"] for item in results)
        total = len(results)
        record = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "subject_id": subject["id"],
            "subject_name": subject["name"],
            "syllabus_id": get_syllabus(state, subject["id"])["id"],
            "topic_id": topic["id"],
            "topic_name": topic["name"],
            "correct": correct_count,
            "total": total,
            "score": correct_count,
            "accuracy": round(correct_count / total * 100, 1),
            "duration_seconds": max(
                0,
                round(time.monotonic() - st.session_state.get(timer_key, time.monotonic())),
            ),
            "results": results,
            "created_at": now_iso(),
        }
        st.session_state[active_key] = False
        (state["practice_attempts"] if kind == "practice" else state["tests"]).append(record)
        if kind == "diagnostic":
            diagnostic = state["diagnostics"].setdefault(
                subject["id"],
                {"syllabus_id": get_syllabus(state, subject["id"])["id"], "topics": {}},
            )
            diagnostic.setdefault("topics", {}).setdefault(topic["id"], {})[
                "quiz_score"
            ] = record["accuracy"]
        state["settings"]["last_test_result"] = record
        if persist():
            minutes, seconds = divmod(record["duration_seconds"], 60)
            incorrect_count = total - correct_count
            st.success(
                f"Result saved · questions: {total} · correct: {correct_count} "
                f"· incorrect: {incorrect_count} · score: {correct_count}/{total} "
                f"· accuracy: {record['accuracy']:.0f}% · time: {minutes}m {seconds}s."
            )
            by_difficulty: dict[str, list[bool]] = defaultdict(list)
            for result in results:
                by_difficulty[result["difficulty"]].append(result["correct"])
            if by_difficulty:
                st.caption(
                    "Difficulty performance: "
                    + " · ".join(
                        f"{difficulty}: {sum(outcomes) / len(outcomes) * 100:.0f}%"
                        for difficulty, outcomes in by_difficulty.items()
                    )
                )
            incorrect = [item for item in results if not item["correct"]]
            if incorrect:
                st.warning("Review these concepts before moving forward: " + ", ".join(dict.fromkeys(item["concept"] for item in incorrect)))
                with st.expander("Review incorrect answers"):
                    for item in incorrect:
                        st.write(f"**{item['question']}**")
                        st.write(f"Correct answer: {next(question['answer'] for question in questions if question['id'] == item['question_id'])}")
                        if item["explanation"]:
                            st.write(item["explanation"])


def render_learning_practice(state: dict[str, Any]) -> None:
    st.title("Learning & practice")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add subjects and confirm a syllabus to get started.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="learning_subject")
    syllabus = get_syllabus(state, subject["id"])
    topics = all_topics(syllabus)
    if not topics:
        st.info("Confirm the subject's actual syllabus before selecting a learning topic.")
        return
    eligible_topics = available_topic_objects(state, subject["id"], syllabus)
    if not eligible_topics:
        st.error("No topic is currently available in the prerequisite order. Review the prerequisite map for a cycle or an unresolved prerequisite.")
        return
    topic = st.selectbox("Syllabus topic", eligible_topics, format_func=lambda item: f"{item['unit_name']} — {item['name']}", key="learning_topic")
    evidence = mastery_evidence(state, subject["id"], topic)
    st.metric("Current topic status", evidence["status"], f"{evidence['score']:.0f}% evidence score" if evidence["score"] is not None else "No assessment evidence yet")
    gaps = prerequisite_gaps(state, subject["id"], syllabus).get(topic["id"], [])
    if gaps:
        st.warning(f"Before studying {topic['name']}, review its prerequisites: {', '.join(gaps)}.")
    st.write("**Syllabus detail**")
    st.caption(
        f"This topic is part of the confirmed syllabus unit '{topic['unit_name']}'. "
        "Its prerequisites and your assessment evidence determine when it appears in your recovery sequence."
    )
    if topic.get("subtopics"):
        st.write(", ".join(topic["subtopics"]))
    else:
        st.caption("No subtopics were listed in the confirmed syllabus.")

    st.subheader("Learning resources")
    topic_resources = [
        resource for resource in state["resources"]
        if resource.get("subject_id") == subject["id"]
        and resource.get("topic_id") == topic["id"]
        and resource.get("kind") == "resource"
    ]
    if topic_resources:
        for resource in topic_resources:
            st.write(f"• **{resource['source_type']}:** {resource['title']}")
            if resource.get("file_path"):
                workspace = PROJECT_ROOT
                uploads_dir = (workspace / "data" / "uploads").resolve()
                stored_path = (workspace / resource["file_path"]).resolve()
                if uploads_dir in stored_path.parents and stored_path.is_file():
                    try:
                        note_bytes = stored_path.read_bytes()
                        st.download_button(
                            f"Download {resource['title']}",
                            data=note_bytes,
                            file_name=resource.get("original_filename", stored_path.name),
                            key=f"download_note_{resource['id']}",
                        )
                    except OSError as error:
                        st.error(f"Could not read the saved note file: {error}")
                else:
                    st.info("This note file is no longer available in local storage.")
            elif resource.get("reference"):
                st.code(resource["reference"], language=None)
    else:
        st.info("No verified learning resource is currently available for this topic. Add your notes, college material, or textbook reference below.")
    with st.form("resource_form"):
        source_type = st.selectbox(
            "Resource type",
            ["Student-uploaded notes", "College material", "Textbook reference"],
        )
        resource_title = st.text_input("Resource title", max_chars=200)
        reference = st.text_input(
            "Page, chapter, or local reference (optional)",
            help="External URLs are not linked or verified in this MVP.",
            max_chars=2048,
        )
        notes_file = st.file_uploader(
            "Attach notes (optional; PDF, TXT, DOCX; max 5 MB)",
            type=["pdf", "txt", "docx"],
            key=f"notes_upload_{topic['id']}",
        )
        resource_submit = st.form_submit_button("Save resource")
    if resource_submit:
        if not resource_title.strip():
            st.error("Enter a title for this resource.")
        else:
            file_path = ""
            if notes_file:
                note_bytes = notes_file.getvalue()
                if len(note_bytes) > 5 * 1024 * 1024:
                    st.error("Notes files must be no larger than 5 MB.")
                    return
                extension = Path(notes_file.name).suffix.lower()
                uploads_dir = PROJECT_ROOT / "data" / "uploads"
                target = uploads_dir / f"{uuid.uuid4().hex}{extension}"
                try:
                    uploads_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(note_bytes)
                except OSError as error:
                    st.error(f"Could not save the uploaded note locally: {error}")
                    return
                file_path = str(target.relative_to(PROJECT_ROOT))
                original_filename = Path(notes_file.name).name
            else:
                original_filename = ""
            state["resources"].append({
                "id": str(uuid.uuid4()), "kind": "resource",
                "subject_id": subject["id"], "topic_id": topic["id"],
                "source_type": source_type, "title": resource_title.strip(),
                "reference": reference.strip(), "file_path": file_path,
                "original_filename": original_filename, "created_at": now_iso(),
            })
            if persist():
                st.success("Resource saved locally.")
                st.rerun()

    st.subheader("Add practice questions from your materials")
    with st.form("question_author_form"):
        question_text = st.text_area("Question", max_chars=2000)
        options = [st.text_input(f"Answer choice {letter}", max_chars=500) for letter in ("A", "B", "C", "D")]
        correct_option = st.selectbox("Correct answer", options=list(range(4)), format_func=lambda index: f"{'ABCD'[index]} — {options[index] or '(enter choice text)'}")
        concept = st.text_input("Concept tested (optional)", value=topic["name"])
        difficulty = st.selectbox("Difficulty", ["Easy", "Medium", "Hard"])
        explanation = st.text_area("Explanation for review (optional)", max_chars=2000)
        question_submit = st.form_submit_button("Save syllabus-based question")
    if question_submit:
        clean_options = [option.strip() for option in options]
        if not question_text.strip() or any(not option for option in clean_options):
            st.error("Enter a question and all four answer choices.")
        elif len(set(clean_options)) != len(clean_options):
            st.error("Each answer choice must be different.")
        elif len(topic_question_bank(state, subject["id"], topic["id"])) >= MAX_QUESTIONS_PER_TOPIC:
            st.error(f"A maximum of {MAX_QUESTIONS_PER_TOPIC} questions is allowed per topic.")
        else:
            state["resources"].append({
                "id": str(uuid.uuid4()), "kind": "question",
                "subject_id": subject["id"], "topic_id": topic["id"],
                "question": question_text.strip(), "options": clean_options,
                "answer": clean_options[correct_option],
                "concept": concept.strip() or topic["name"],
                "difficulty": difficulty, "explanation": explanation.strip(),
                "created_at": now_iso(),
            })
            if persist():
                st.success("Question saved. It can now be used in practice and tests.")
                st.rerun()

    question_bank = topic_question_bank(state, subject["id"], topic["id"])
    if question_bank:
        st.subheader("Practice")
        historical = assessment_records(state, subject["id"], topic["id"], "practice")
        accuracy = calculate_mastery([item["accuracy"] for item in historical]) if historical else None
        target_difficulty = "Easy" if accuracy is not None and accuracy < 50 else "Medium" if accuracy is None or accuracy <= 80 else "Hard"
        st.caption(f"Recommended difficulty: {target_difficulty}" + (f" (based on {accuracy:.0f}% practice accuracy)" if accuracy is not None else " (starting level)"))
        preferred = [item for item in question_bank if item.get("difficulty") == target_difficulty]
        st.write(f"{len(question_bank)} question(s) available from student-provided materials.")
        practice_items = preferred or question_bank
        render_quiz_form(state, subject, topic, practice_items[:5], "practice")
        st.subheader("Mini test")
        mini_items = question_bank[:10]
        st.caption(f"{len(mini_items)} question(s); test length depends on the questions you have added.")
        render_quiz_form(state, subject, topic, mini_items, "mini")
    else:
        st.info("Add questions copied or adapted from your actual course materials to enable auto-scored practice and mini tests.")

    st.subheader("Log a study session")
    with st.form("study_session_form"):
        duration = st.number_input("Study duration (minutes)", min_value=1, max_value=600, value=20)
        activity = st.selectbox("Activity", ["Learning", "Revision", "Reading", "Problem solving"])
        complete_topic = st.checkbox("Mark topic learning task completed")
        session_submit = st.form_submit_button("Save study session")
    if session_submit:
        session = {
            "id": str(uuid.uuid4()), "subject_id": subject["id"],
            "subject_name": subject["name"], "topic_id": topic["id"],
            "topic_name": topic["name"], "duration_minutes": int(duration),
            "activity": activity, "completed": complete_topic, "created_at": now_iso(),
        }
        state["study_sessions"].append(session)
        if complete_topic:
            state["daily_tasks"].append({
                "id": str(uuid.uuid4()), "date": date.today().isoformat(),
                "subject_id": subject["id"], "topic_id": topic["id"],
                "title": f"Study {topic['name']}", "minutes": int(duration),
                "kind": activity, "status": "Completed",
            })
        if persist():
            st.success("Study session saved.")
            st.rerun()

    st.subheader("Ask the AI Tutor (optional)")
    st.caption("If configured, submitting a prompt sends it to the selected AI provider with this topic's confirmed syllabus context.")
    prompt = st.text_area(
        "What would you like explained?",
        key=f"ai_prompt_{topic['id']}",
        max_chars=2000,
    )
    if st.button("Ask AI Tutor", key=f"ask_ai_{topic['id']}"):
        api_key = os.getenv("AI_API_KEY", "").strip()
        model = os.getenv("AI_MODEL", "").strip()
        if not api_key or not model:
            st.info("AI Tutor is currently unavailable. You can continue using your syllabus, recovery roadmap, practice, tests, progress, and revision features. Configure AI_API_KEY and AI_MODEL to enable an API request.")
        elif not prompt.strip():
            st.error("Enter a question for the tutor.")
        else:
            topic_mistakes = Counter(
                result.get("concept", topic["name"])
                for attempt in assessment_records(state, subject["id"], topic["id"])
                for result in attempt.get("results", [])
                if not result.get("correct")
            )
            repeated_context = ", ".join(
                f"{concept} ({count} missed attempts)"
                for concept, count in topic_mistakes.most_common(5)
            ) or "none recorded"
            recent_scores = ", ".join(
                f"{record['kind']}: {record['accuracy']:.0f}%"
                for record in assessment_records(state, subject["id"], topic["id"])[-3:]
            ) or "none recorded"
            with st.spinner("Requesting a topic-focused explanation..."):
                try:
                    response = requests.post(
                        os.getenv("AI_API_URL", "").strip()
                        or "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": f"Explain only the confirmed syllabus topic '{topic['name']}' in subject '{subject['name']}', unit '{topic['unit_name']}', at a beginner-friendly level. Do not invent course resources or syllabus topics. Known subtopics: {', '.join(topic.get('subtopics', [])) or 'none listed'}. Prerequisites to address first: {', '.join(gaps) or 'none identified'}. Current mastery evidence: {evidence['score'] if evidence['score'] is not None else 'not yet assessed'}%. Recent assessments: {recent_scores}. Previous missed concepts: {repeated_context}."},
                                {"role": "user", "content": prompt.strip()},
                            ],
                            "temperature": 0.3,
                        },
                        timeout=30,
                    )
                    response.raise_for_status()
                    result = response.json()["choices"][0]["message"]["content"]
                    st.markdown(result)
                except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
                    st.warning(f"AI Tutor is currently unavailable ({error}). Your syllabus, practice, tests, and progress remain available.")


def render_roadmap(state: dict[str, Any]) -> None:
    st.title("Recovery roadmap")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add subjects to create a recovery roadmap.")
        return
    ranked = sorted(subjects, key=lambda subject: subject_priority(state, subject)[0], reverse=True)
    for index, subject in enumerate(ranked, start=1):
        points, label, reason = subject_priority(state, subject)
        with st.expander(f"Priority {index} · {subject['name']} · {label}", expanded=index == 1):
            st.write(f"**Marks:** {subject['marks']}  ·  **Status:** {subject['status']}")
            st.write(f"**Why this priority:** {reason}")
            syllabus = get_syllabus(state, subject["id"])
            topics = all_topics(syllabus)
            if not topics:
                st.info("Add and confirm this subject's syllabus to see a topic-level roadmap.")
                continue
            gaps = prerequisite_gaps(state, subject["id"], syllabus)
            completed = [
                topic["id"]
                for topic in topics
                if mastery_evidence(state, subject["id"], topic)["status"] == "Mastered"
            ]
            prerequisites = {
                topic["id"]: topic.get("prerequisites", []) for topic in topics
            }
            ordered_available = get_available_topics(
                [topic["id"] for topic in topics], completed, prerequisites
            )
            for unit in syllabus.get("units", []):
                st.markdown(f"**{unit['name']}**")
                for topic in unit.get("topics", []):
                    evidence = mastery_evidence(state, subject["id"], topic)
                    missing = gaps.get(topic["id"], [])
                    category = topic_category(evidence)
                    icon = "🟢" if evidence["status"] == "Mastered" else "🔴" if category == "Weak / missed" else "🟡" if category == "Needs revision" else "🔵" if category.startswith("Strong signal") else "⚪"
                    st.write(f"{icon} **{topic['name']}** — {category}" + (f" ({evidence['score']:.0f}%)" if evidence["score"] is not None else ""))
                    if missing:
                        st.caption(f"Study prerequisite(s) first: {', '.join(missing)}. These are required before progressing to {topic['name']}.")
            if ordered_available:
                next_topic = next(topic for topic in topics if topic["id"] == ordered_available[0])
                st.info(f"Next available topic in the prerequisite sequence: {next_topic['name']}.")


def render_daily_plan(state: dict[str, Any]) -> None:
    st.title("Today's study plan")
    daily_limit = int(state["profile"].get("daily_study_minutes", 60))
    st.caption(f"Your selected daily study budget is {daily_limit} minutes. The generated plan stays within this limit.")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add subjects and a confirmed syllabus to build a daily plan.")
        return
    subject = st.selectbox("Plan for subject", subjects, format_func=lambda item: item["name"], key="daily_subject")
    syllabus = get_syllabus(state, subject["id"])
    topics = all_topics(syllabus)
    if not topics:
        st.info("Confirm a syllabus before generating topic-based tasks.")
        return
    eligible_topics = available_topic_objects(state, subject["id"], syllabus)
    if not eligible_topics:
        st.error("No topic is currently available in the prerequisite order. Review the prerequisite relationships.")
        return
    candidates = []
    gap_map = prerequisite_gaps(state, subject["id"], syllabus)
    ordered = [topic["id"] for topic in eligible_topics]
    for topic in eligible_topics:
        evidence = mastery_evidence(state, subject["id"], topic)
        if evidence["status"] != "Mastered":
            candidates.append(topic)
    candidates.sort(key=lambda topic: ordered.index(topic["id"]))
    if not candidates:
        st.success("All topics currently meet the mastery rule. Consider a subject mock test or revision.")
        return
    todays_tasks = [
        task for task in state["daily_tasks"]
        if task.get("date") == date.today().isoformat()
    ]
    already_completed_minutes = sum(
        task.get("minutes", 0)
        for task in todays_tasks
        if task.get("status") == "Completed"
    )
    other_subject_reservations = sum(
        task.get("minutes", 0)
        for task in todays_tasks
        if task.get("subject_id") != subject["id"]
        and task.get("status") != "Completed"
    )
    remaining_minutes = max(
        0, daily_limit - already_completed_minutes - other_subject_reservations
    )
    if remaining_minutes == 0:
        st.success("Today's study budget is already completed or allocated to other subjects.")
        return
    task_minutes = min(20, remaining_minutes)
    tasks = []
    for topic in candidates:
        if sum(item["minutes"] for item in tasks) + task_minutes > daily_limit:
            break
        if gap_map.get(topic["id"]):
            title = f"Review prerequisite(s): {', '.join(gap_map[topic['id']])}"
            kind = "Prerequisite"
        else:
            title = f"Study {topic['name']}"
            kind = "Learning"
        tasks.append({
            "id": str(uuid.uuid4()), "date": date.today().isoformat(),
            "subject_id": subject["id"], "subject_name": subject["name"],
            "topic_id": topic["id"], "topic_name": topic["name"],
            "title": title, "minutes": task_minutes, "kind": kind,
            "status": "Not Started",
        })
    if not tasks:
        st.info("Increase your daily study time slightly to schedule a task.")
        return
    if st.button("Generate today's plan", type="primary"):
        state["daily_tasks"] = [
            task for task in state["daily_tasks"]
            if not (
                task.get("date") == date.today().isoformat()
                and task.get("subject_id") == subject["id"]
                and task.get("status") != "Completed"
            )
        ] + tasks
        if persist():
            st.success(f"Plan saved: {sum(item['minutes'] for item in tasks)} minutes.")
            st.rerun()
    today_tasks = [
        task for task in state["daily_tasks"]
        if task.get("date") == date.today().isoformat() and task.get("subject_id") == subject["id"]
    ]
    if today_tasks:
        st.subheader(
            f"{subject['name']} · "
            f"{sum(task['minutes'] for task in today_tasks)} planned minutes"
        )
        for task in today_tasks:
            st.write(f"**{task['title']}** · {task['minutes']} min · {task['kind']}")
            status = st.selectbox(
                "Task status", ["Not Started", "In Progress", "Completed"],
                index=["Not Started", "In Progress", "Completed"].index(task.get("status", "Not Started")),
                key=f"daily_status_{task['id']}",
            )
            if status != task.get("status"):
                task["status"] = status
                task["updated_at"] = now_iso()
                persist()


def render_tests(state: dict[str, Any]) -> None:
    st.title("Mini tests & subject mock tests")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add subjects and confirm syllabus topics first.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="tests_subject")
    syllabus = get_syllabus(state, subject["id"])
    topics = all_topics(syllabus)
    if not topics:
        st.info("Confirm a syllabus to make tests from its topics.")
        return
    eligible_topics = available_topic_objects(state, subject["id"], syllabus)
    if not eligible_topics:
        st.error("No topic is currently available in the prerequisite order. Review the prerequisite relationships.")
        return
    topic = st.selectbox("Topic for mini test", eligible_topics, format_func=lambda item: f"{item['unit_name']} — {item['name']}", key="mini_topic")
    bank = topic_question_bank(state, subject["id"], topic["id"])
    if bank:
        render_quiz_form(state, subject, topic, bank[:10], "mini")
    else:
        st.info("Add questions from your own materials in Learning & Practice to take a mini test.")

    st.divider()
    st.subheader("Full subject mock test")
    mock_questions = [
        (topic_item, question)
        for topic_item in topics
        for question in topic_question_bank(state, subject["id"], topic_item["id"])
    ]
    questions_by_topic = {
        topic["id"]: topic_question_bank(state, subject["id"], topic["id"])
        for topic in topics
    }
    uncovered_topics = [
        topic["name"] for topic in topics if not questions_by_topic[topic["id"]]
    ]
    st.write(
        f"Coverage: {len(topics)} confirmed topic(s) · "
        f"{len(mock_questions)} student-provided question(s)."
    )
    if uncovered_topics:
        st.warning(
            "A full-syllabus mock needs at least one question for every confirmed "
            "topic. Add questions for: " + ", ".join(uncovered_topics)
        )
    elif len(topics) > MAX_MOCK_QUESTIONS:
        st.warning(
            f"This syllabus has more than {MAX_MOCK_QUESTIONS} topics. "
            "The current full-mock limit cannot cover every topic."
        )
    elif mock_questions:
        first_per_topic = [
            (topic, questions_by_topic[topic["id"]][0]) for topic in topics
        ]
        selected_question_ids = {
            question["id"] for _, question in first_per_topic
        }
        questions_by_unit: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        for topic_item, question in mock_questions:
            if question["id"] not in selected_question_ids:
                questions_by_unit[topic_item["unit_id"]].append((topic_item, question))
        selected = list(first_per_topic)
        while len(selected) < 30 and any(questions_by_unit.values()):
            for questions_in_unit in questions_by_unit.values():
                if questions_in_unit and len(selected) < 30:
                    selected.append(questions_in_unit.pop(0))
        timer_key = f"mock_started_{subject['id']}_{syllabus['id']}"
        active_key = f"mock_active_{subject['id']}_{syllabus['id']}"
        if not st.session_state.get(active_key):
            if st.button(
                "Start subject mock test",
                key=f"start_mock_{subject['id']}",
                type="primary",
            ):
                st.session_state[timer_key] = time.monotonic()
                st.session_state[active_key] = True
                st.rerun()
        else:
            with st.form(f"mock_form_{subject['id']}"):
                answers = {}
                for index, (topic_item, question) in enumerate(selected, start=1):
                    st.markdown(f"**{index}. [{topic_item['name']}] {question['question']}**")
                    answers[question["id"]] = st.radio(
                        "Select an answer",
                        question["options"],
                        key=f"mock_answer_{question['id']}",
                        label_visibility="collapsed",
                    )
                mock_submit = st.form_submit_button(
                    "Submit subject mock test",
                    type="primary",
                )
            if mock_submit:
                results = []
                for topic_item, question in selected:
                    correct = answers[question["id"]] == question["answer"]
                    results.append({
                        "question_id": question["id"],
                        "question": question["question"],
                        "selected": answers[question["id"]],
                        "correct_answer": question["answer"],
                        "explanation": question.get("explanation", ""),
                        "correct": correct,
                        "concept": question.get("concept", topic_item["name"]),
                        "topic_id": topic_item["id"],
                        "topic_name": topic_item["name"],
                        "unit_name": topic_item["unit_name"],
                        "difficulty": question.get("difficulty", "Medium"),
                    })
                correct_count = sum(item["correct"] for item in results)
                record = {
                    "id": str(uuid.uuid4()),
                    "kind": "mock",
                    "subject_id": subject["id"],
                    "subject_name": subject["name"],
                    "syllabus_id": syllabus["id"],
                    "correct": correct_count,
                    "total": len(results),
                    "score": correct_count,
                    "accuracy": round(correct_count / len(results) * 100, 1),
                    "coverage_percent": round(
                        len({result["topic_id"] for result in results})
                        / len(topics)
                        * 100,
                        1,
                    ),
                    "duration_seconds": max(
                        0,
                        round(
                            time.monotonic()
                            - st.session_state.get(timer_key, time.monotonic())
                        ),
                    ),
                    "results": results,
                    "created_at": now_iso(),
                }
                st.session_state[active_key] = False
                state["tests"].append(record)
                state["settings"]["last_mock_result"] = record
                if persist():
                    minutes, seconds = divmod(record["duration_seconds"], 60)
                    st.success(
                        f"Mock result: {correct_count}/{len(results)} "
                        f"({record['accuracy']:.0f}%) · time {minutes}m {seconds}s."
                    )
                    by_topic: dict[str, list[bool]] = defaultdict(list)
                    by_unit: dict[str, list[bool]] = defaultdict(list)
                    by_difficulty: dict[str, list[bool]] = defaultdict(list)
                    for result in results:
                        by_topic[result["topic_name"]].append(result["correct"])
                        by_unit[result["unit_name"]].append(result["correct"])
                        by_difficulty[result["difficulty"]].append(result["correct"])
                    st.markdown("**Topic-wise performance**")
                    for topic_name, outcomes in by_topic.items():
                        accuracy = sum(outcomes) / len(outcomes) * 100
                        st.write(
                            f"**{topic_name}:** {accuracy:.0f}% — "
                            f"{'Strong' if accuracy >= 80 else 'Needs revision'}"
                        )
                    st.markdown("**Unit-wise performance**")
                    for unit_name, outcomes in by_unit.items():
                        st.write(f"**{unit_name}:** {sum(outcomes) / len(outcomes) * 100:.0f}%")
                    st.markdown("**Difficulty-wise performance**")
                    for difficulty, outcomes in by_difficulty.items():
                        st.write(f"**{difficulty}:** {sum(outcomes) / len(outcomes) * 100:.0f}%")
                    incorrect = [item for item in results if not item["correct"]]
                    if incorrect:
                        st.warning(
                            "Recommended revision: "
                            + ", ".join(dict.fromkeys(item["concept"] for item in incorrect))
                        )
                        with st.expander("Review incorrect mock-test answers"):
                            for item in incorrect:
                                st.write(f"**{item['question']}**")
                                st.write(f"Correct answer: {item['correct_answer']}")
                                if item["explanation"]:
                                    st.write(item["explanation"])
    else:
        st.info("A mock test is built only from confirmed syllabus topics with questions you provide. Add questions to cover the syllabus; no questions or exam pattern are invented.")


def render_progress(state: dict[str, Any]) -> None:
    st.title("Progress & revision")
    subjects = state["subjects"]
    if not subjects:
        st.info("Your progress will appear after you add subjects and record activity.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="progress_subject")
    syllabus = get_syllabus(state, subject["id"])
    topics = all_topics(syllabus)
    if not topics:
        st.info("Confirm this subject's syllabus to track topic and unit progress.")
    else:
        st.subheader(f"{subject['name']} progress")
        for unit in syllabus["units"]:
            unit_topics = unit["topics"]
            completed = sum(mastery_evidence(state, subject["id"], topic)["task_complete"] for topic in unit_topics)
            percent = calculate_progress(completed, len(unit_topics))
            unit_scores = [
                evidence["score"]
                for topic in unit_topics
                if (evidence := mastery_evidence(state, subject["id"], topic))["score"] is not None
            ]
            mastery_text = (
                f" · topic mastery {calculate_mastery(unit_scores):.0f}%"
                if unit_scores
                else " · mastery not assessed"
            )
            st.write(f"**{unit['name']}** — completion {percent:.0f}%{mastery_text}")
            st.progress(percent / 100)
        completed_topics = sum(mastery_evidence(state, subject["id"], topic)["task_complete"] for topic in topics)
        total_progress = calculate_progress(completed_topics, len(topics))
        st.metric("Topic completion", f"{total_progress:.0f}%")
        st.progress(total_progress / 100)
        subject_scores = [
            evidence["score"]
            for topic in topics
            if (evidence := mastery_evidence(state, subject["id"], topic))["score"] is not None
        ]
        if subject_scores:
            st.metric("Subject topic mastery", f"{calculate_mastery(subject_scores):.0f}%")
        else:
            st.caption("Subject topic mastery is not yet available; complete a diagnostic to establish a baseline.")

        st.subheader("Topic mastery")
        for topic in topics:
            evidence = mastery_evidence(state, subject["id"], topic)
            value = evidence["score"]
            st.write(f"**{topic['name']}** — {topic_category(evidence)}" + (f" · {value:.0f}%" if value is not None else ""))

        weak = weak_topics(state, subject["id"], syllabus)
        st.subheader("Revision queue")
        if not weak:
            st.success("No topics are currently flagged for revision by the recorded assessment evidence.")
        for item in weak:
            st.write(f"• **{item['name']}** — {item['status']}" + (f" ({item['score']:.0f}%)" if item["score"] is not None else " (self-assessed as unfamiliar)"))
            if item.get("repeated_mistakes"):
                st.caption("Repeatedly missed concepts: " + ", ".join(item["repeated_mistakes"]))
            if st.button(f"Mark revision complete: {item['name']}", key=f"revise_{item['id']}"):
                state["study_sessions"].append({
                    "id": str(uuid.uuid4()), "subject_id": subject["id"],
                    "subject_name": subject["name"], "topic_id": item["id"],
                    "topic_name": item["name"], "duration_minutes": 15,
                    "activity": "Revision", "completed": True, "created_at": now_iso(),
                })
                state["daily_tasks"].append({
                    "id": str(uuid.uuid4()), "date": date.today().isoformat(),
                    "subject_id": subject["id"], "topic_id": item["id"],
                    "title": f"Revise {item['name']}", "minutes": 15,
                    "kind": "Revision", "status": "Completed",
                })
                if persist():
                    st.success("Revision recorded.")
                    st.rerun()

    st.subheader("Assessment history")
    records = sorted(
        [
            item
            for item in state["tests"] + state["practice_attempts"]
            if item.get("subject_id") == subject["id"]
            and (not syllabus or item.get("syllabus_id") == syllabus["id"])
        ],
        key=lambda item: item.get("created_at", ""),
    )
    if not records:
        st.caption("No tests completed yet.")
    else:
        attempted = sum(record.get("total", 0) for record in records)
        correct = sum(record.get("correct", 0) for record in records)
        summary_columns = st.columns(4)
        summary_columns[0].metric("Attempts", len(records))
        summary_columns[1].metric("Questions answered", attempted)
        summary_columns[2].metric("Correct answers", correct)
        summary_columns[3].metric("Overall accuracy", f"{correct / attempted * 100:.0f}%" if attempted else "—")
        st.line_chart([item["accuracy"] for item in records[-10:]])
        by_difficulty: dict[str, list[bool]] = defaultdict(list)
        for record in records:
            for result in record.get("results", []):
                by_difficulty[result.get("difficulty", "Medium")].append(result.get("correct", False))
        if by_difficulty:
            st.markdown("**Difficulty-wise accuracy**")
            for difficulty, outcomes in by_difficulty.items():
                st.write(f"{difficulty}: {sum(outcomes) / len(outcomes) * 100:.0f}% ({len(outcomes)} question(s))")
        for record in reversed(records[-10:]):
            st.write(f"{record.get('created_at', '')[:10]} · {record['kind'].title()} · {record.get('topic_name', record.get('subject_name'))} · {record['correct']}/{record['total']} ({record['accuracy']:.0f}%)")
            wrong = [result for result in record.get("results", []) if not result.get("correct")]
            if wrong:
                with st.expander("Review incorrect answers"):
                    for item in wrong:
                        st.write(f"**{item['question']}**")
                        st.write(f"Concept to review: {item.get('concept', 'see topic')}")
                        if item.get("explanation"):
                            st.write(item["explanation"])

    study_sessions = [session for session in state["study_sessions"] if session.get("subject_id") == subject["id"]]
    study_minutes = sum(session.get("duration_minutes", 0) for session in study_sessions)
    st.metric("Recorded study time", f"{study_minutes} minutes")
    if not study_sessions:
        st.caption("Study activity will appear here after you complete a study session.")
    else:
        st.subheader("Study activity")
        for session in sorted(study_sessions, key=lambda item: item.get("created_at", ""), reverse=True)[:10]:
            st.write(f"• {session['created_at'][:10]} · {session['topic_name']} · {session['duration_minutes']} min · {session['activity']}")


def render_readiness(state: dict[str, Any]) -> None:
    st.title("Exam readiness")
    st.warning("This is an academic preparation indicator based on recorded information. It does not predict or guarantee an exam result.")
    subjects = subject_options(state, backlogs_only=True)
    if not subjects:
        st.info("Add a backlog subject and confirm its syllabus to begin readiness tracking.")
        return
    subject = st.selectbox("Subject", subjects, format_func=lambda item: item["name"], key="readiness_subject")
    summary = readiness_summary(state, subject)
    if not summary:
        st.info("Readiness cannot yet be estimated reliably. Confirm the syllabus, complete the topic diagnostic, record practice, and take a mini test first.")
        return
    st.subheader(f"{summary['level']} · {summary['score']}/100")
    st.write(summary["advice"])
    for label, value in summary["breakdown"].items():
        if value is None:
            st.caption(f"{label}: no data yet")
        elif isinstance(value, (float, int)) and label not in {
            "Weak topics remaining",
            "Prerequisite gaps",
            "Days until exam",
        }:
            st.write(f"**{label}:** {value:.0f}%")
            st.progress(value / 100)
        elif label == "Days until exam":
            st.write(f"**{label}:** {value if value is not None else 'not provided'}")
        else:
            st.write(f"**{label}:** {value}")
    strengths = []
    if summary["breakdown"]["Syllabus completion"] >= 80:
        strengths.append("most topic learning tasks are complete")
    if summary["breakdown"]["Mini-test performance"] >= 80:
        strengths.append("mini-test performance is strong")
    if not summary["gaps"]:
        strengths.append("recorded prerequisite gaps are resolved")
    if strengths:
        st.success("Strengths: " + "; ".join(strengths).capitalize() + ".")
    if summary["weak"]:
        st.warning("Remaining work: " + ", ".join(item["name"] for item in summary["weak"]))
    st.info(f"**Next best action:** {summary['action']}")

    exam_date_value = state["profile"].get("exam_date")
    if not exam_date_value:
        st.info("Add your exam date to create a personalized final revision schedule.")
        return
    try:
        exam_date = date.fromisoformat(exam_date_value)
    except ValueError:
        st.error("The saved exam date is invalid. Update it in Academic Profile.")
        return
    days_left = (exam_date - date.today()).days
    if days_left <= 0:
        st.info("The saved exam date is today or has passed. Update the date if your exam was rescheduled.")
        return
    if summary["score"] < READINESS_THRESHOLDS["almost_ready"]:
        st.info("Continue your recovery roadmap before entering final revision mode.")
        return
    st.subheader(f"Adaptive final revision · {days_left} day(s)")
    remaining_topics = [item["name"] for item in summary["weak"]]
    remaining_topics.extend(
        topic["name"] for topic in summary["topics"]
        if topic["name"] not in remaining_topics
    )
    if days_left == 1:
        focus = ", ".join(remaining_topics) if remaining_topics else "highest-priority weak areas"
        st.write(
            f"**Today · {date.today().isoformat()}** — Review {focus}, then complete timed practice and review mistakes."
        )
        return
    if days_left == 2:
        focus = ", ".join(remaining_topics) if remaining_topics else "the confirmed syllabus"
        st.write(f"**Day 1 · {date.today().isoformat()}** — Revise {focus}.")
        st.write(f"**Day 2 · {(exam_date - timedelta(days=1)).isoformat()}** — Full-syllabus review, timed mock practice, and mistake review.")
        return

    topic_days = days_left - 2
    topics_per_day = max(1, (len(remaining_topics) + topic_days - 1) // topic_days)
    for day_index in range(days_left):
        plan_date = date.today() + timedelta(days=day_index)
        if day_index < topic_days:
            day_topics = remaining_topics[:topics_per_day]
            remaining_topics = remaining_topics[topics_per_day:]
            activity = (
                f"Revise {', '.join(day_topics)}"
                if day_topics
                else "Spaced recall and light practice on previously reviewed topics"
            )
        elif day_index == topic_days:
            activity = "Full-syllabus review"
        else:
            activity = "Timed mock test and review of mistakes"
        st.write(f"**Day {day_index + 1} · {plan_date.isoformat()}** — {activity}")


def render_ai_tutor() -> None:
    st.title("AI Tutor")
    st.write("AI assistance is optional. Academic records, planning, practice, and progress work without it.")
    if not os.getenv("AI_API_KEY") or not os.getenv("AI_MODEL"):
        st.info("AI Tutor is currently unavailable. Configure AI_API_KEY and AI_MODEL in your local .env file to enable an actual API request.")
    st.caption("The tutor receives only the topic context you provide in the learning screen. Never put passwords or private credentials in a prompt.")
    st.write("To ask a syllabus-grounded question, open **Learning & Practice**, choose a confirmed topic, and use its AI Tutor section.")


def main() -> None:
    try:
        if "state" not in st.session_state:
            st.session_state["state"] = load_state(STATE_PATH)
    except StatePersistenceError as error:
        st.error(f"Could not load your saved academic data: {error}")
        st.stop()

    state = st.session_state["state"]
    st.sidebar.title("🎓 BacklogBridge AI")
    st.sidebar.caption("Your academic recovery journey")
    page = st.sidebar.radio(
        "Navigate",
        [
            "Dashboard",
            "Academic Profile",
            "Syllabus",
            "Diagnostic",
            "Recovery Roadmap",
            "Today's Plan",
            "Learning & Practice",
            "Mini & Mock Tests",
            "Progress & Revision",
            "Exam Readiness",
            "AI Tutor",
        ],
    )
    st.sidebar.divider()
    if state["profile"]:
        st.sidebar.metric("Backlogs", sum(
            subject.get("status") == "Backlog" and bool(subject.get("name", "").strip())
            for subject in state["subjects"]
        ))
    if page == "Dashboard":
        render_dashboard(state)
    elif page == "Academic Profile":
        render_profile(state)
    elif page == "Syllabus":
        render_syllabus(state)
    elif page == "Diagnostic":
        render_diagnostic(state)
    elif page == "Recovery Roadmap":
        render_roadmap(state)
    elif page == "Today's Plan":
        render_daily_plan(state)
    elif page == "Learning & Practice":
        render_learning_practice(state)
    elif page == "Mini & Mock Tests":
        render_tests(state)
    elif page == "Progress & Revision":
        render_progress(state)
    elif page == "Exam Readiness":
        render_readiness(state)
    else:
        render_ai_tutor()
    st.divider()
    st.caption("BacklogBridge AI · Local JSON storage · Readiness is an indicator, not a guarantee.")


if __name__ == "__main__":
    main()
