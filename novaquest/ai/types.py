"""Value objects passed between services and AI engines."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TopicCtx:
    """Everything an engine needs to know about one syllabus topic."""

    id: int
    code: str
    title: str
    outcome: str
    terms: tuple[str, ...]
    chapter_no: int
    chapter_title: str
    ai_concept: str | None
    passages: tuple[dict, ...]
    misconceptions: tuple[dict, ...] = ()
    ai_moments: tuple[dict, ...] = ()
    sibling_terms: tuple[str, ...] = ()
    other_wrong: tuple[str, ...] = ()
    sibling_text: tuple[str, ...] = ()  # other topics of the chapter (for believable wrong options)
    course: str = "Grade 8 Science"  # e.g. "Grade 9 Biology (IGCSE)": said in every prompt about this topic


@dataclass(slots=True)
class QuestionDraft:
    """An AI-generated question awaiting teacher review."""

    topic_id: int
    kind: str
    prompt: str
    difficulty: str
    thinking_level: str
    outcome: str
    options: list[str] | None = None
    answer: dict[str, Any] | None = None
    rubric: list[dict[str, Any]] | None = None
    scenario: str | None = None
    ai_concept: str | None = None
    misconception_codes: list[str] = field(default_factory=list)
    citation: list[dict[str, Any]] = field(default_factory=list)
    written_by: str | None = None  # 'claude', or 'offline' when Claude was configured but couldn't write it


@dataclass(frozen=True, slots=True)
class Mark:
    """AI marking of one answer."""

    score: float
    max_score: float
    rationale: str
    confidence: float
    misconception_codes: tuple[str, ...] = ()

    @property
    def flagged(self) -> bool:
        """Low-confidence marks go to the teacher's review queue (NR-4)."""
        return self.confidence < 0.7


@dataclass(frozen=True, slots=True)
class TutorReply:
    """A tutor message with its sources."""

    text: str
    citations: tuple[str, ...] = ()
    declined: bool = False


@dataclass(frozen=True, slots=True)
class TeachVerdict:
    """Evaluator judgement of one teaching turn."""

    verdict: str  # correct | partial | vague | incorrect | off_topic
    covered: tuple[int, ...]
    has_example: bool
    note: str
