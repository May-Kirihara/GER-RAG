"""Heuristic extractor for auto_remember candidates.

Given a transcript (free-form text — typically a conversation segment),
return ranked candidates worth saving to long-term memory.

The extractor is intentionally simple and dependency-free so it can run
without a model call. It is designed to be replaced by an LLM-based
extractor behind the same interface in the future.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Keywords (Japanese + English) that suggest a line is worth remembering.
_DECISION_KEYWORDS = (
    "決定", "結論", "方針", "採用", "却下", "選択",
    "decided", "decision", "concluded", "conclude", "chose", "pick",
)
_OUTCOME_KEYWORDS = (
    "失敗", "成功", "解決", "解消", "エラー", "バグ", "原因",
    "failed", "succeeded", "fixed", "resolved", "error", "bug", "root cause",
)
_PREFERENCE_KEYWORDS = (
    "禁止", "必ず", "好き", "嫌い", "嫌", "推奨", "避ける",
    "must not", "never", "always", "prefer", "禁じ",
)
_LESSON_KEYWORDS = (
    "次回", "今後", "教訓", "学び", "気をつけ", "注意",
    "next time", "lesson", "learned", "watch out", "remember to", "TODO", "todo",
)
_USER_VOICE_PATTERNS = (
    re.compile(r"^\s*(?:user|ユーザー|私)[\s::はがも]"),
    re.compile(r"(?:してほしい|してください|お願いします)\s*$"),
)
_NUMERIC_PATTERN = re.compile(r"\d")

# Lines that look like noise (greetings, fillers, tool noise)
_NOISE_PATTERNS = (
    re.compile(r"^\s*(?:ok|okay|了解|ありがとう|thanks?|sure|はい|いいえ)\s*[!.。]*\s*$", re.I),
    re.compile(r"^[\s\-=*_#>]+$"),
)


@dataclass(frozen=True)
class Candidate:
    """A single extracted memory candidate."""
    content: str
    score: float
    reasons: tuple[str, ...]
    suggested_source: str = "agent"
    suggested_tags: tuple[str, ...] = ()


def _split_segments(text: str) -> list[str]:
    """Split transcript into candidate segments (one per non-empty line)."""
    raw = re.split(r"\n+", text)
    return [line.strip() for line in raw if line.strip()]


def _score_segment(
    segment: str,
    *,
    min_chars: int,
    max_chars: int,
) -> tuple[float, list[str], list[str], str]:
    """Score a single segment. Returns (score, reasons, tags, suggested_source)."""
    if any(p.search(segment) for p in _NOISE_PATTERNS):
        return 0.0, [], [], "agent"

    length = len(segment)
    if length < min_chars or length > max_chars:
        return 0.0, [], [], "agent"

    score = 0.0
    reasons: list[str] = []
    tags: list[str] = []
    source = "agent"

    lower = segment.lower()

    if any(kw in segment or kw in lower for kw in _DECISION_KEYWORDS):
        score += 1.5
        reasons.append("決定/結論キーワード")
        tags.append("design-decision")

    if any(kw in segment or kw in lower for kw in _OUTCOME_KEYWORDS):
        score += 1.4
        reasons.append("失敗/成功/エラーキーワード")
        tags.append("troubleshooting")

    if any(kw in segment or kw in lower for kw in _LESSON_KEYWORDS):
        score += 1.2
        reasons.append("教訓/次回への申し送り")
        tags.append("letter-to-future-self")

    if any(kw in segment or kw in lower for kw in _PREFERENCE_KEYWORDS):
        score += 1.3
        reasons.append("好み/禁止/制約")
        tags.append("preference")
        source = "user"

    if any(p.search(segment) for p in _USER_VOICE_PATTERNS):
        score += 0.6
        reasons.append("ユーザー発話の可能性")
        if source != "user":
            source = "user"

    if _NUMERIC_PATTERN.search(segment):
        score += 0.3
        reasons.append("数値を含む（メトリクス候補）")

    # Slight preference for medium-length lines (concise but substantive)
    if 30 <= length <= 200:
        score += 0.4
        reasons.append("適度な長さ")

    return score, reasons, tags, source


def extract_candidates(
    transcript: str,
    *,
    max_candidates: int = 5,
    min_chars: int = 12,
    max_chars: int = 400,
    min_score: float = 0.7,
) -> list[Candidate]:
    """Extract top candidates from a transcript.

    Returns at most `max_candidates` items, sorted by descending score.
    Items below `min_score` are discarded.
    """
    seen: set[str] = set()
    scored: list[Candidate] = []
    for segment in _split_segments(transcript):
        if segment in seen:
            continue
        seen.add(segment)
        score, reasons, tags, source = _score_segment(
            segment, min_chars=min_chars, max_chars=max_chars,
        )
        if score < min_score:
            continue
        scored.append(
            Candidate(
                content=segment,
                score=round(score, 3),
                reasons=tuple(reasons),
                suggested_source=source,
                suggested_tags=tuple(tags),
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:max_candidates]
