"""Orchestrate phrase lookup without inventing medical codes.

Version 2 still does not use an LLM. A caller (or the synthetic JSON
file) supplies diagnosis, symptom, and medication phrases. The pipeline then:

1. Detects negated / historical / uncertain / follow-up context from the note.
2. Searches official terminology APIs only for findings that may be coded.
3. Ranks validated candidates (generic unless the note is specific).
4. Does not suggest negated, unconfirmed, or follow-up diseases as codes
   and does not attach API candidates for those rows.
5. If a listed med is written as '{drug} for {reason}', also searches
   that stated reason as a diagnosis.
6. Leaves confidence as null.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from src.context import (
    detect_clinical_context,
    diagnosis_query_for_reason,
    extract_medication_reason,
)
from src.ranking import rank_candidates
from src.schemas import (
    ClinicalContext,
    CodeCandidate,
    EncounterInput,
    InferenceSource,
    MappingResult,
    PhraseInput,
    ReviewStatus,
)
from src.terminology.icd10 import Icd10Client, TerminologyApiError as Icd10ApiError
from src.terminology.rxnorm import RxNormClient, TerminologyApiError as RxNormApiError

SearchFn = Callable[[str], list[CodeCandidate]]


def load_settings_from_env() -> dict[str, Any]:
    """Read optional API settings. Missing values fall back to official NLM URLs."""

    timeout_raw = os.getenv("TERMINOLOGY_API_TIMEOUT_SECONDS", "10").strip()
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = 10.0

    return {
        "timeout_seconds": timeout_seconds,
        "icd10_base_url": os.getenv(
            "ICD10_API_BASE_URL",
            "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search",
        ).strip(),
        "rxnorm_base_url": os.getenv(
            "RXNORM_API_BASE_URL",
            "https://rxnav.nlm.nih.gov/REST",
        ).strip(),
    }


def build_clients(
    icd10_client: Icd10Client | None = None,
    rxnorm_client: RxNormClient | None = None,
) -> tuple[Icd10Client, RxNormClient]:
    """Use injected clients in tests, or real NLM clients in normal runs."""

    settings = load_settings_from_env()
    if icd10_client is None:
        icd10_client = Icd10Client(
            base_url=settings["icd10_base_url"],
            timeout_seconds=settings["timeout_seconds"],
        )
    if rxnorm_client is None:
        rxnorm_client = RxNormClient(
            base_url=settings["rxnorm_base_url"],
            timeout_seconds=settings["timeout_seconds"],
        )
    return icd10_client, rxnorm_client


def map_encounter(
    encounter: EncounterInput,
    icd10_client: Icd10Client | None = None,
    rxnorm_client: RxNormClient | None = None,
) -> list[MappingResult]:
    """Map one synthetic encounter's manually supplied phrases."""

    icd10_client, rxnorm_client = build_clients(icd10_client, rxnorm_client)
    results: list[MappingResult] = []
    listed_diagnosis_phrases = {item.phrase.strip().lower() for item in encounter.diagnoses}

    for phrase in encounter.diagnoses:
        context = detect_clinical_context(encounter.note, phrase.phrase)
        results.append(
            _map_phrase(
                encounter=encounter,
                phrase=phrase,
                entity_type="diagnosis",
                code_system="ICD-10-CM",
                search=icd10_client.search,
                api_error_type=Icd10ApiError,
                clinical_context=context,
            )
        )

    for phrase in encounter.symptoms:
        context = detect_clinical_context(encounter.note, phrase.phrase)
        results.append(
            _map_phrase(
                encounter=encounter,
                phrase=phrase,
                entity_type="symptom",
                code_system="ICD-10-CM",
                search=_symptom_search(icd10_client.search, phrase.phrase),
                api_error_type=Icd10ApiError,
                clinical_context=context,
            )
        )

    for phrase in encounter.medications:
        context = detect_clinical_context(encounter.note, phrase.phrase)
        results.append(
            _map_phrase(
                encounter=encounter,
                phrase=phrase,
                entity_type="medication",
                code_system="RxNorm",
                search=rxnorm_client.search,
                api_error_type=RxNormApiError,
                clinical_context=context,
            )
        )
        extra = _diagnosis_from_medication_reason(
            encounter,
            phrase,
            icd10_client.search,
            listed_diagnosis_phrases,
        )
        if extra is not None:
            results.append(extra)
            listed_diagnosis_phrases.add(extra.source_phrase.lower())

    return results


def _diagnosis_from_medication_reason(
    encounter: EncounterInput,
    medication: PhraseInput,
    search: SearchFn,
    already_listed: set[str],
) -> MappingResult | None:
    """Search a diagnosis only when the note states '{drug} for {reason}'."""

    reason = extract_medication_reason(encounter.note, medication.phrase)
    if not reason:
        return None
    query = diagnosis_query_for_reason(reason)
    if query.lower() in already_listed:
        return None
    inferred = PhraseInput(phrase=query, clinical_context="current")
    return _map_phrase(
        encounter=encounter,
        phrase=inferred,
        entity_type="diagnosis",
        code_system="ICD-10-CM",
        search=search,
        api_error_type=Icd10ApiError,
        clinical_context="current",
        inference_source="medication_reason",
    )


def _symptom_search(search: SearchFn, phrase: str) -> SearchFn:
    """Search the symptom phrase, then the same phrase without a leading qualifier.

    NLM's ICD-10-CM search does not match "dry cough" or "mild fever", but it
    does match "cough" and "fever". The displayed phrase stays unchanged.
    """

    def search_symptom(query: str) -> list[CodeCandidate]:
        candidates = search(query)
        if candidates:
            return candidates
        broader = _broader_symptom_query(phrase)
        if not broader or broader == query.strip().lower():
            return []
        return search(broader)

    return search_symptom


def _broader_symptom_query(phrase: str) -> str:
    lowered = phrase.strip().lower()
    for prefix in ("mild ", "dry ", "slight "):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :].strip()
    return ""


def _map_phrase(
    encounter: EncounterInput,
    phrase: PhraseInput,
    entity_type: str,
    code_system: str,
    search: SearchFn,
    api_error_type: type[Exception],
    clinical_context: ClinicalContext,
    inference_source: InferenceSource = "listed_phrase",
) -> MappingResult:
    if not phrase.phrase:
        return _result(
            encounter,
            phrase,
            entity_type,
            code_system,
            suggested=None,
            alternatives=[],
            review_status="no_code_found",
            error_message="No search phrase was supplied.",
            clinical_context=clinical_context,
            inference_source=inference_source,
        )

    # Denied, unconfirmed, and follow-up findings are not coded. Skip the
    # terminology API so we do not attach pneumonia or "utility gas" hits.
    if _should_not_code(entity_type, clinical_context):
        return _result(
            encounter,
            phrase,
            entity_type,
            code_system,
            suggested=None,
            alternatives=[],
            review_status="do_not_code",
            error_message=_do_not_code_message(entity_type, clinical_context),
            clinical_context=clinical_context,
            inference_source=inference_source,
        )

    try:
        candidates = search(phrase.phrase)
    except api_error_type as exc:
        return _result(
            encounter,
            phrase,
            entity_type,
            code_system,
            suggested=None,
            alternatives=[],
            review_status="api_error",
            error_message=str(exc),
            clinical_context=clinical_context,
            inference_source=inference_source,
        )

    if not candidates:
        return _result(
            encounter,
            phrase,
            entity_type,
            code_system,
            suggested=None,
            alternatives=[],
            review_status="no_code_found",
            error_message="The terminology API returned no validated codes.",
            clinical_context=clinical_context,
            inference_source=inference_source,
        )

    ranked = rank_candidates(candidates, phrase.phrase, encounter.note, clinical_context)
    suggested, *alternatives = ranked
    return _result(
        encounter,
        phrase,
        entity_type,
        code_system,
        suggested=suggested,
        alternatives=alternatives,
        review_status="needs_review",
        error_message=None,
        clinical_context=clinical_context,
        inference_source=inference_source,
    )


def _should_not_code(entity_type: str, clinical_context: ClinicalContext) -> bool:
    if clinical_context in {"negated", "uncertain", "follow_up"}:
        return True
    return False


def _do_not_code_message(entity_type: str, clinical_context: ClinicalContext) -> str:
    if clinical_context == "negated":
        return "Not coded: the note denied this finding."
    if clinical_context == "follow_up":
        return (
            "Not coded: this is a follow-up visit reason, not a confirmed "
            "active diagnosis."
        )
    if entity_type == "medication":
        return "Not coded: this medication is only considered or pending confirmation."
    return (
        "Not coded as a confirmed diagnosis: the note marks this as possible, "
        "pending, or not yet confirmed. Code documented symptoms instead."
    )


def _result(
    encounter: EncounterInput,
    phrase: PhraseInput,
    entity_type: str,
    code_system: str,
    suggested: CodeCandidate | None,
    alternatives: list[CodeCandidate],
    review_status: ReviewStatus,
    error_message: str | None,
    clinical_context: ClinicalContext,
    inference_source: InferenceSource,
) -> MappingResult:
    return MappingResult(
        encounter_id=encounter.encounter_id,
        source_text=encounter.note,
        entity_type=entity_type,  # type: ignore[arg-type]
        code_system=code_system,  # type: ignore[arg-type]
        suggested_code=suggested.code if suggested else None,
        description=suggested.description if suggested else None,
        alternatives=alternatives,
        review_status=review_status,
        source_phrase=phrase.phrase,
        clinical_context=clinical_context,
        confidence=None,
        error_message=error_message,
        inference_source=inference_source,
    )
