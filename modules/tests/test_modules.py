import unittest
from pathlib import Path

from modules.engines import (
    calculate_mastery,
    calculate_progress,
    calculate_readiness,
    calculate_risk,
    get_available_topics,
    get_backlog_priority,
    recommend_next_action,
)
from modules.persistence import (
    DEFAULT_STATE,
    StudentStateStore,
    StatePersistenceError,
    load_state,
    save_state,
)
from modules.syllabus import (
    SyllabusError,
    extract_syllabus_text,
    normalize_syllabus_text,
    parse_syllabus_text,
)


class EngineTests(unittest.TestCase):
    def test_risk_and_priority_match_existing_ui_conventions(self):
        self.assertEqual(calculate_risk(0, 8), "Low Risk 🟢")
        self.assertEqual(calculate_risk(1, 6), "Medium Risk 🟡")
        self.assertEqual(calculate_risk(3, 8), "High Risk 🔴")
        self.assertEqual(get_backlog_priority(39), ("Very High Priority", "2 hours/day"))
        self.assertEqual(get_backlog_priority(45), ("High Priority", "1.5 hours/day"))

    def test_mastery_progress_prerequisites_readiness_and_action(self):
        self.assertEqual(calculate_mastery({"A": 60, "B": 80}), 70.0)
        self.assertEqual(calculate_progress([True, {"completed": True}, False]), 66.7)
        self.assertEqual(calculate_progress(2, 4), 50.0)
        topics = ["Foundations", "Applications", "Revision"]
        prerequisites = {"Applications": ["Foundations"]}
        self.assertEqual(
            get_available_topics(topics, prerequisites=prerequisites),
            ["Foundations", "Revision"],
        )
        self.assertEqual(
            recommend_next_action(
                topics,
                completed=["Foundations"],
                mastery_by_topic={"Applications": 30, "Revision": 50},
            )["topic"],
            "Applications",
        )
        self.assertFalse(calculate_readiness(100, 100, prerequisites_met=False)["ready"])

    def test_invalid_engine_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_risk(-1, 6)
        with self.assertRaises(ValueError):
            calculate_progress(5, 4)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).with_name("student-state-test.json")
        self.path.unlink(missing_ok=True)
        self.store = StudentStateStore(self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_defaults_and_atomic_round_trip_allow_unnamed_profile(self):
        self.assertEqual(self.store.load()["profile"], {})
        self.assertEqual(set(self.store.load()), set(DEFAULT_STATE))
        saved = self.store.save({"profile": {"semester": 2}, "subjects": []})
        self.assertEqual(saved["profile"], {"semester": 2})
        self.assertEqual(self.store.load(), saved)
        self.assertFalse(list(self.path.parent.glob(f".{self.path.name}.*.tmp")))

    def test_top_level_load_save_functions_match_store_api(self):
        state = load_state(self.path)
        saved = save_state(state, self.path)
        self.assertEqual(saved, self.store.load())

    def test_invalid_or_corrupt_state_surfaces_errors(self):
        with self.assertRaises(StatePersistenceError):
            self.store.save({"subjects": {}})
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StatePersistenceError):
            self.store.load()

    def test_profile_and_subject_inputs_are_validated_before_save(self):
        with self.assertRaises(StatePersistenceError):
            self.store.save({"profile": {"cgpa": 10.1}})
        with self.assertRaises(StatePersistenceError):
            self.store.save({"profile": {"exam_date": "not-a-date"}})
        with self.assertRaises(StatePersistenceError):
            self.store.save(
                {
                    "subjects": [
                        {
                            "id": "subject-1",
                            "name": "DBMS",
                            "marks": 120,
                            "status": "Backlog",
                        }
                    ]
                }
            )

    def test_syllabus_and_assessment_data_are_validated(self):
        with self.assertRaises(StatePersistenceError):
            self.store.save(
                {
                    "syllabi": [
                        {
                            "id": "syllabus",
                            "subject_id": "subject",
                            "confirmed": True,
                            "units": [
                                {
                                    "id": "unit",
                                    "name": "Unit 1",
                                    "topics": [
                                        {
                                            "id": "topic",
                                            "name": "Topic",
                                            "prerequisites": ["missing-topic"],
                                            "subtopics": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )
        with self.assertRaises(StatePersistenceError):
            self.store.save(
                {
                    "tests": [
                        {
                            "subject_id": "subject",
                            "kind": "mini",
                            "correct": 3,
                            "total": 2,
                            "accuracy": 150,
                            "results": [],
                        }
                    ]
                }
            )


class SyllabusTests(unittest.TestCase):
    def test_normalization_and_parsing_preserve_source_only(self):
        parsed = parse_syllabus_text("  Unit 1  \r\n\n • Arrays\t and Loops \n")
        self.assertEqual(parsed["source_text"], "Unit 1\n\n• Arrays and Loops")
        self.assertEqual(parsed["topics"], ["Unit 1", "• Arrays and Loops"])
        self.assertEqual(normalize_syllabus_text("  A   B  \n"), "A B")

    def test_extraction_enforces_upload_type_size_and_utf8(self):
        self.assertEqual(extract_syllabus_text("topics.TXT", b"Topic one\nTopic two"), "Topic one\nTopic two")
        with self.assertRaises(SyllabusError):
            extract_syllabus_text("topics.csv", b"Topic")
        with self.assertRaises(SyllabusError):
            extract_syllabus_text("topics.txt", b"x" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(SyllabusError):
            extract_syllabus_text("topics.txt", b"\xff")
        with self.assertRaises(SyllabusError):
            extract_syllabus_text("topics.txt", b"  \n\t")


if __name__ == "__main__":
    unittest.main()
