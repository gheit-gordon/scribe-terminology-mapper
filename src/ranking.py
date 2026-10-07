"""Rank official API candidates instead of trusting row 1.

NLM search order is not a coding decision. This module only reorders codes
the APIs already returned. It never invents a code.

Rules (simple, no LLM):
- Prefer generic labels (unspecified, without complications) when the note
  does not mention extra detail.
- Downrank "with hyperglycemia"-style extras that are not in the note.
- If the finding is historical, prefer old/history labels over acute ones.
"""

from __future__ import annotations

import re

from src.context import sentence_for_phrase
from src.schemas import ClinicalContext, CodeCandidate

GENERIC_PHRASES = (
    "without complications",
    "uncomplicated",
    "unspecified",
    "not otherwise specified",
    "essential (primary)",
    "essential hypertension",
)

GENERIC_TOKENS = {
    "unspecified",
    "uncomplicated",
    "complications",
    "without",
    "nos",
    "not",
    "otherwise",
    "specified",
    "essential",
    "primary",
    "other",
    "the",
    "of",
    "a",
    "an",
    "and",
    "or",
    "in",
    "due",
    "to",
}

HISTORICAL_DESC = ("old ", "old,", "history", "chronic", "sequela", "healed")
ACUTE_DESC = ("acute",)
STOPWORDS = {"the", "of", "a", "an", "and", "or", "to", "in", "for", "with"}
LATERALITY = {"right", "left", "bilateral"}
SPECIFIC_MANIFESTATIONS = ("pneumonia", "gastrointestinal", "encephalopathy", "myocarditis", "otitis")


def rank_candidates(
    candidates: list[CodeCandidate],
    phrase: str,
    note: str,
    clinical_context: ClinicalContext,
) -> list[CodeCandidate]:
    """Return a new list, best generic-or-matching candidate first."""

    scored = [
        (_score_candidate(candidate, phrase, note, clinical_context), index, candidate)
        for index, candidate in enumerate(candidates)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _score, _index, candidate in scored]


def _score_candidate(
    candidate: CodeCandidate,
    phrase: str,
    note: str,
    clinical_context: ClinicalContext,
) -> float:
    description = candidate.description.lower()
    haystack = f"{phrase} {note}".lower()
    score = 0.0

    if clinical_context == "historical":
        if any(marker in description for marker in HISTORICAL_DESC):
            score += 10
        if any(marker in description for marker in ACUTE_DESC):
            score -= 10

    for marker in GENERIC_PHRASES:
        if marker in description:
            score += 5
            break
    if re.search(r"\bnos\b", description):
        score += 3

    for extra in re.finditer(r"\bwith\s+([a-z0-9]+(?:\s+[a-z0-9]+){0,3})", description):
        extra_text = extra.group(1).strip()
        if extra_text and extra_text not in haystack:
            score -= 6

    phrase_tokens = _content_tokens(phrase)
    description_tokens = _content_tokens(description)
    if phrase_tokens:
        overlap = len(phrase_tokens & description_tokens) / len(phrase_tokens)
        score += 6 * overlap

    sentence = sentence_for_phrase(note, phrase)
    sentence_tokens = _content_tokens(sentence)
    sentence_text = sentence.lower()
    named_laterality = sentence_tokens & LATERALITY
    forgiven = (_content_tokens(note) - LATERALITY) | named_laterality
    extra_tokens = description_tokens - phrase_tokens - GENERIC_TOKENS - forgiven
    # Extra adjectives (renovascular, resistant, pulmonary) lose to a generic
    # match like essential / unspecified when those words are not in the note.
    score -= 4 * len(extra_tokens)

    if named_laterality:
        if "unspecified" in description_tokens:
            score -= 8
        if named_laterality & description_tokens:
            score += 8

    named_manifestation = any(
        manifestation in sentence_text or manifestation in phrase.lower()
        for manifestation in SPECIFIC_MANIFESTATIONS
    )
    for manifestation in SPECIFIC_MANIFESTATIONS:
        if manifestation in description and not named_manifestation:
            score -= 8
    if not named_manifestation and "other respiratory" in description:
        score += 8
    return score


def _content_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {token for token in tokens if token not in STOPWORDS and len(token) > 1}
