"""Rule-based clinical context around a listed phrase.

This is not sentiment analysis and not an LLM. It looks at the sentence
that contains a phrase and checks for simple cues:

- negated: the note denied the finding
- historical: the finding is in the past
- uncertain: possible / pending / not confirmed
- follow_up: visit reason (follow-up for X), not a new active diagnosis
- current: none of the above

Cue checks stay inside that sentence so a later line like
"No real patient identifiers" cannot negate the whole note.
"""

from __future__ import annotations

import re

from src.schemas import ClinicalContext

NEGATION_BEFORE = (
    "denies",
    "denied",
    "deny",
    "denying",
    "no",
    "not",
    "without",
    "negative for",
    "absent",
)
NEGATION_AFTER = ("denied", "negative", "absent")
UNCERTAIN_CUES = (
    "possible",
    "possibly",
    "probable",
    "probably",
    "suspected",
    "suspect",
    "likely",
    "rule out",
    "r/o",
    "consider",
    "awaiting",
    "pending",
    "if imaging",
    "if the imaging",
    "cannot confirm",
    "not confirmed",
    "versus",
    "vs ",
)
HISTORICAL_CUES = (
    "history of",
    "hx of",
    "h/o",
    "prior",
    "previous",
    "remote",
    "status post",
    "s/p",
    "old",
)
FOLLOW_UP_CUES = (
    "follow-up for",
    "follow up for",
    "f/u for",
    "follow-up of",
    "follow up of",
)

# "drug for high blood pressure" → search hypertension, not invent from drug class.
REASON_TO_DIAGNOSIS = {
    "blood pressure": "hypertension",
    "high blood pressure": "hypertension",
    "bp": "hypertension",
}


def detect_clinical_context(note: str, phrase: str) -> ClinicalContext:
    """Return context for one listed phrase using the sentence it appears in."""

    sentence = sentence_for_phrase(note, phrase)
    if not sentence:
        return "current"

    if _has_cue_near_phrase(sentence, phrase, NEGATION_BEFORE, after_cues=NEGATION_AFTER):
        return "negated"
    if _sentence_has_any(sentence, UNCERTAIN_CUES):
        return "uncertain"
    if _sentence_has_any(sentence, FOLLOW_UP_CUES):
        return "follow_up"
    if _has_cue_near_phrase(sentence, phrase, HISTORICAL_CUES, after_cues=()):
        return "historical"
    return "current"


def sentence_for_phrase(note: str, phrase: str) -> str:
    """Return the sentence that contains the phrase, or empty if not found."""

    cleaned_phrase = phrase.strip()
    if not cleaned_phrase:
        return ""
    needle = cleaned_phrase.lower()
    for sentence in _split_sentences(note):
        if needle in sentence.lower():
            return sentence
    return ""


def extract_medication_reason(note: str, drug: str) -> str | None:
    """Return the stated reason in '{drug} for {reason}', else None."""

    cleaned_drug = drug.strip()
    if not cleaned_drug:
        return None
    pattern = re.compile(
        rf"\b{re.escape(cleaned_drug)}\b\s+for\s+(.+)",
        re.IGNORECASE,
    )
    match = pattern.search(note)
    if not match:
        return None
    reason = match.group(1).strip()
    reason = re.split(r"[.,;:(]|\bno real\b", reason, maxsplit=1, flags=re.IGNORECASE)[0]
    reason = reason.strip(" .,-")
    return reason or None


def diagnosis_query_for_reason(reason: str) -> str:
    """Map a stated med reason to an ICD-10 search phrase."""

    cleaned = re.sub(r"\s+", " ", reason.strip().lower())
    return REASON_TO_DIAGNOSIS.get(cleaned, reason.strip())


def _split_sentences(note: str) -> list[str]:
    sentences: list[str] = []
    for line in re.split(r"[\n\r]+", note.strip()):
        parts = re.split(r"(?<=[.!?])\s+", line.strip())
        sentences.extend(part.strip() for part in parts if part.strip())
    return sentences


def _has_cue_near_phrase(
    sentence: str,
    phrase: str,
    before_cues: tuple[str, ...],
    after_cues: tuple[str, ...],
) -> bool:
    lower = sentence.lower()
    phrase_lower = phrase.lower()
    index = lower.find(phrase_lower)
    if index < 0:
        return False
    before = lower[:index]
    after = lower[index + len(phrase_lower) : index + len(phrase_lower) + 48]
    return _text_has_any(before, before_cues) or _text_has_any(after, after_cues)


def _sentence_has_any(sentence: str, cues: tuple[str, ...]) -> bool:
    return _text_has_any(sentence.lower(), cues)


def _text_has_any(text: str, cues: tuple[str, ...]) -> bool:
    for cue in cues:
        if " " in cue or "/" in cue:
            if cue in text:
                return True
            continue
        if re.search(rf"\b{re.escape(cue)}\b", text):
            return True
    return False
