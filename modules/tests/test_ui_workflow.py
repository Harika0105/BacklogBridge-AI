import unittest
from copy import deepcopy

from modules.persistence import DEFAULT_STATE
from modules.ui import (
    academic_risk,
    available_topic_objects,
    mastery_evidence,
    readiness_summary,
)


class RecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.state = deepcopy(DEFAULT_STATE)
        self.subject_id = "subject-1"
        self.syllabus_id = "syllabus-1"
        self.first_topic = {
            "id": "topic-1",
            "name": "Foundations",
            "subtopics": [],
            "prerequisites": [],
        }
        self.next_topic = {
            "id": "topic-2",
            "name": "Applications",
            "subtopics": [],
            "prerequisites": ["topic-1"],
        }
        self.subject = {
            "id": self.subject_id,
            "name": "Course A",
            "marks": 42,
            "status": "Backlog",
        }
        self.syllabus = {
            "id": self.syllabus_id,
            "subject_id": self.subject_id,
            "confirmed": True,
            "units": [
                {
                    "id": "unit-1",
                    "name": "Unit 1",
                    "topics": [self.first_topic, self.next_topic],
                }
            ],
        }
        self.state["profile"] = {
            "semester": 2,
            "cgpa": 6.0,
            "daily_study_minutes": 45,
        }
        self.state["subjects"] = [self.subject]
        self.state["syllabi"] = [self.syllabus]

    def test_prerequisite_sequence_uses_only_confirmed_topics(self):
        available = available_topic_objects(
            self.state, self.subject_id, self.syllabus
        )
        self.assertEqual([topic["name"] for topic in available], ["Foundations"])

        self.state["diagnostics"][self.subject_id] = {
            "syllabus_id": self.syllabus_id,
            "topics": {
                "topic-1": {"score": 100, "level": "Know"},
            },
        }
        self.state["practice_attempts"] = [
            {
                "kind": "practice",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 100,
            }
        ]
        self.state["tests"] = [
            {
                "kind": "mini",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 90,
            },
            {
                "kind": "mini",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 90,
            },
        ]
        self.state["study_sessions"] = [
            {"topic_id": "topic-1", "completed": True}
        ]
        self.assertEqual(
            [topic["name"] for topic in available_topic_objects(
                self.state, self.subject_id, self.syllabus
            )],
            ["Applications"],
        )

    def test_recent_low_test_reopens_topic_for_revision(self):
        self.state["diagnostics"][self.subject_id] = {
            "syllabus_id": self.syllabus_id,
            "topics": {"topic-1": {"score": 100, "level": "Know"}},
        }
        self.state["practice_attempts"] = [
            {
                "kind": "practice",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 90,
            }
        ]
        self.state["tests"] = [
            {
                "kind": "mini",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 85,
                "created_at": "2026-10-04",
            },
            {
                "kind": "mini",
                "subject_id": self.subject_id,
                "syllabus_id": self.syllabus_id,
                "topic_id": "topic-1",
                "accuracy": 35,
                "created_at": "2026-10-05",
            },
        ]
        self.assertEqual(
            mastery_evidence(self.state, self.subject_id, self.first_topic)["status"],
            "Needs Revision",
        )

    def test_readiness_is_withheld_without_real_assessment_evidence(self):
        self.state["diagnostics"][self.subject_id] = {
            "syllabus_id": self.syllabus_id,
            "topics": {
                "topic-1": {"score": 80},
                "topic-2": {"score": 80},
            },
        }
        self.assertIsNone(readiness_summary(self.state, self.subject))

    def test_risk_includes_weak_marks_without_judgmental_language(self):
        label, reasons, weak_subjects = academic_risk(self.state)
        self.assertIn(label, {"Medium Risk 🟡", "High Risk 🔴"})
        self.assertTrue(reasons)
        self.assertEqual(weak_subjects, [self.subject])
        self.assertNotIn("failing", " ".join(reasons).lower())


if __name__ == "__main__":
    unittest.main()
