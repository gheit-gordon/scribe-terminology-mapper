"""Rank official API candidates instead of trusting row 1.

NLM search order is not a coding decision. This module only reorders codes
the APIs already returned. It never invents a code.

Rules (simple, no LLM):
- Drop ICD candidates that need pregnancy, delivery, neonatal, stoma, or
  (when current) personal-history context the note does not state.
- Prefer generic labels (unspecified, without complications) when the note
  does not mention extra detail.
- Downrank "with hyperglycemia"-style extras that are not in the note.
- Boost the same extras when the note or phrase does state them, so a
  supported specific code can beat a generic unspecified hit.
- If the finding is historical, prefer old/history labels over acute ones.
"""

from __future__ import annotations

import re

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

# Description cues that need matching support in the note. Groups are
# independent: "pregnant" does not unlock a "following delivery" code.
CONTEXT_QUALIFIER_GROUPS = (
    ("pregnancy", "pregnant", "trimester"),
    ("delivery", "postpartum", "puerperium"),
    ("abortion", "termination", "ectopic", "molar pregnancy"),
    ("neonatal", "newborn"),
    ("stoma",),
)
HISTORY_CUES = ("personal history", "history of")


def filter_context_compatible_candidates(
    candidates: list[CodeCandidate],
    supporting_text: str,
    clinical_context: ClinicalContext,
) -> list[CodeCandidate]:
    """Keep only ICD candidates whose extra context is stated in the note."""

    return [
        candidate
        for candidate in candidates
        if _is_context_compatible(candidate, supporting_text, clinical_context)
    ]


def _is_context_compatible(
    candidate: CodeCandidate,
    supporting_text: str,
    clinical_context: ClinicalContext,
) -> bool:
    description = candidate.description
    for cues in CONTEXT_QUALIFIER_GROUPS:
        if _text_has_any_cue(description, cues) and not _text_has_any_cue(
            supporting_text, cues
        ):
            return False
    if clinical_context != "historical":
        if _text_has_any_cue(description, HISTORY_CUES) and not _text_has_any_cue(
            supporting_text, HISTORY_CUES
        ):
            return False
    return True


def _text_has_any_cue(text: str, cues: tuple[str, ...]) -> bool:
    for cue in cues:
        if _text_has_cue(text, cue):
            return True
    return False


def _text_has_cue(text: str, cue: str) -> bool:
    if " " in cue:
        return cue.lower() in text.lower()
    return bool(re.search(rf"\b{re.escape(cue)}\b", text, re.IGNORECASE))


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
        if not extra_text:
            continue
        if extra_text not in haystack:
            score -= 6
        else:
            # Beat the generic +5 so documented specificity can win.
            score += 8

    phrase_tokens = _content_tokens(phrase)
    description_tokens = _content_tokens(description)
    if phrase_tokens:
        overlap = len(phrase_tokens & description_tokens) / len(phrase_tokens)
        score += 6 * overlap

    extra_tokens = description_tokens - phrase_tokens - GENERIC_TOKENS - _content_tokens(note)
    # Extra adjectives (renovascular, resistant, pulmonary) lose to a generic
    # match like essential / unspecified when those words are not in the note.
    score -= 4 * len(extra_tokens)
    return score


def _content_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {token for token in tokens if token not in STOPWORDS and len(token) > 1}
