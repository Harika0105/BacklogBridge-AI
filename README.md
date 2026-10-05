# BacklogBridge AI — Academic Recovery Intelligence System

BacklogBridge AI helps college students move from knowing they have a backlog to
knowing what to study next. It uses the student's own subject information and
confirmed syllabus to identify learning gaps, prioritize recovery work, and track
evidence of progress. Academic indicators are guidance, not a guarantee of exam
results.

## Problem and solution

Students often know they need to recover a subject but do not know which topic to
start with, which prerequisites they are missing, how to practise, how to measure
improvement, or whether they are ready to revise. BacklogBridge AI connects an
academic profile, student-confirmed syllabus, diagnostic answers, practice and
test results, study activity, and revision into one persistent recovery journey.

## Features

- Academic profile with semester, CGPA, marks and Passed/Backlog subjects.
- Backlog counts are derived from named subjects marked Backlog; students do not
  enter a separate backlog total.
- Syllabus authoring by hand and upload of PDF, TXT, or DOCX for review.
  Extracted content remains a draft until the student confirms it.
- Topic diagnostics, prerequisite-aware sequencing, subject priority and
  recovery recommendations based on recorded information.
- Daily study tasks constrained by the student's available study time.
- Student-supplied topic questions for practice, diagnostic quizzes, mini tests,
  and syllabus-wide mock tests; incorrect-answer review and difficulty analytics.
- Student notes stored locally and downloadable from the matching topic.
- Practice and test attempts, repeated-evidence mastery updates,
  revision tracking and persisted study sessions.
- Progress summaries, recent activity, mock-test workflow, exam readiness
  breakdown and date-aware final revision guidance.
- Optional AI tutor configuration; the learning and tracking workflow does not
  require an AI service.
- JSON-backed local persistence. Do not use the local JSON store as a
  multi-user production database.

## Technology

- Python 3.12
- Streamlit 1.40.1
- python-dotenv and requests for optional AI configuration/API access
- pypdf and python-docx for syllabus file extraction
- JSON files for local development persistence

## Project structure

```text
BacklogBridge AI/
├── app.py
├── modules/
│   ├── ui.py                # Streamlit sections and connected student workflow
│   ├── engines.py           # Deterministic academic calculations
│   ├── persistence.py       # Validated, atomic JSON storage
│   └── syllabus.py          # Bounded PDF/TXT/DOCX extraction
├── data/
│   ├── student_state.json   # Created locally; ignored by Git
│   ├── sample_data.json     # Clearly labelled demo data only; never loaded
│   └── uploads/             # Local student notes; ignored by Git
├── assets/
├── .env                     # Local optional secrets; never commit
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Installation

From the project directory, create and activate a virtual environment if needed:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` only if configuring the optional AI tutor, then add
your own provider's API key and model name. `AI_API_URL` can override the
OpenAI-compatible chat-completions endpoint. Never commit `.env`.

## Run

```powershell
streamlit run app.py
```

The first run starts with empty semester, CGPA, subject count, marks, and daily
study-time fields; no demo/student values are assumed. Enter your semester,
CGPA, subjects, marks, status, and available daily study time in Academic
Profile. Backlog totals are calculated automatically and blank subject names
are ignored.

## Add and confirm a syllabus

Choose a subject and either enter units/topics manually or upload a PDF, TXT, or
DOCX syllabus. Review and correct the extracted draft before confirming it.
Only confirmed syllabus topics should be used for topic-level diagnosis,
practice, planning and readiness. The system must not substitute a generic
university syllabus.

## Diagnostics and recovery roadmap

For each confirmed topic, record your self-assessed knowledge and complete
available diagnostic questions. Results identify topics that may need attention.
Recorded prerequisite relationships are used to place prerequisite topics before
advanced topics and explain the learning order. Roadmap recommendations combine
the subject details, topic evidence, available study time and exam date when one
is provided.

## Learning, practice and progress

Use the daily plan and topic workflow to study, practise, take mini tests, review
mistakes and schedule revision. Completed actions and attempts are saved locally.
Auto-scored questions must be added from student- or college-provided materials;
the app does not fabricate subject questions.
Progress, activity history, mastery and performance summaries are calculated
from saved events; an empty history is shown as empty rather than fabricated.

## Exam readiness and final revision

Readiness is an academic preparation indicator based on available syllabus,
topic and assessment signals. It is withheld when there is insufficient evidence
and never guarantees a pass. Add an exam date to receive a date-aware final
revision schedule; without one, no date is assumed.

## AI integration

AI is optional. Configure `AI_API_KEY` and `AI_MODEL` in `.env` only when using a
compatible provider. API calls should be used for learning assistance (such as
explanations), not as the source of truth for marks, mastery, progress, priority
or readiness. If the service is unavailable or unconfigured, local academic
features should continue to work and the interface should say when it is using
local guidance.

## Data and privacy

Student state is stored locally under `data/` and is excluded from Git. The app
does not need a student name, address, phone number, password or government ID.
Back up the local state file before moving or reinstalling the project. Local
JSON storage is intended for a single-user MVP, not concurrent or multi-user
deployment.

## Testing

Run the project's backend tests from the root directory:

```powershell
python -m unittest discover -s modules\tests
```

Smoke-test the Streamlit app with:

```powershell
streamlit run app.py
```

Check that profile data and progress remain after restarting, empty names do not
inflate backlog counts, only confirmed syllabus is used, and no readiness score
is presented without sufficient assessment evidence.

## Future improvements

Possible follow-up work includes SQLite or PostgreSQL storage, authentication,
teacher tools, notifications, calendar integration, verified resource curation,
and additional AI-assisted learning features. These should not replace the
deterministic and persistent core recovery workflow.
