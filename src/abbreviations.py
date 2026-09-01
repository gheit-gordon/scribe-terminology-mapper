"""Curated clinical abbreviation expansion for ICD-10 search queries.

This is not NLP and not an LLM. Known abbreviations are replaced as whole
tokens so ICD-10 search uses a clinical phrase (UTI → urinary tract
infection) instead of letters that match unrelated words like utility.

Expansion is only a search-query rewrite. Original listed phrases stay in
source_phrase. Medication / RxNorm queries are not expanded here.
"""

from __future__ import annotations

import re

ABBREVIATIONS = {
    "uti": "urinary tract infection",
    "htn": "hypertension",
    "t2dm": "type 2 diabetes mellitus",
    "mi": "myocardial infarction",
}

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(key) for key in ABBREVIATIONS) + r")\b",
    re.IGNORECASE,
)


def expand_abbreviations(phrase: str) -> str:
    """Replace known whole-token abbreviations; leave other text unchanged."""

    if not phrase:
        return phrase

    def _replace(match: re.Match[str]) -> str:
        return ABBREVIATIONS[match.group(0).lower()]

    return _PATTERN.sub(_replace, phrase)
