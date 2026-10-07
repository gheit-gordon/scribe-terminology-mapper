"""Shared data shapes for the Clinical Terminology Mapper.

Why this file exists:
    Other modules should agree on the same field names. Putting those
    shapes in one place makes the pipeline easier to plug into a company
    backend later.

Why dataclasses:
    A dataclass is a beginner-friendly way to say "this object has these
    named fields and types" without writing a lot of boilerplate.

Why confidence is always None:
    This version does not score how "sure" a mapping is. Returning null
    is honest. A made-up number would look scientific but would not be
    valid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EntityType = Literal["diagnosis", "symptom", "medication"]
CodeSystem = Literal["ICD-10-CM", "RxNorm"]
ClinicalContext = Literal["current", "negated", "historical", "uncertain", "follow_up"]
ReviewStatus = Literal["needs_review", "do_not_code", "no_code_found", "api_error"]
InferenceSource = Literal["listed_phrase", "medication_reason"]


@dataclass
class PhraseInput:
    """One manually supplied clinical phrase to look up.

    phrase: Text we will send to an official terminology API.
    clinical_context: Optional hint from the JSON file. The pipeline
        re-detects context from the note and does not trust this field.
    """

    phrase: str
    clinical_context: ClinicalContext = "current"


@dataclass
class EncounterInput:
    """One synthetic encounter: a note plus the phrases a human already listed."""

    encounter_id: str
    note: str
    diagnoses: list[PhraseInput] = field(default_factory=list)
    symptoms: list[PhraseInput] = field(default_factory=list)
    medications: list[PhraseInput] = field(default_factory=list)


@dataclass
class CodeCandidate:
    """One code returned by an authoritative terminology API."""

    code: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "description": self.description}


@dataclass
class MappingResult:
    """One mapped entity ready to serialize as JSON.

    suggested_code is a ranked, validated API candidate, never a code that
    we invented. Negated and unconfirmed diseases use do_not_code.
    """

    encounter_id: str
    source_text: str
    entity_type: EntityType
    code_system: CodeSystem
    suggested_code: str | None
    description: str | None
    alternatives: list[CodeCandidate]
    review_status: ReviewStatus
    source_phrase: str
    clinical_context: ClinicalContext
    confidence: None = None
    error_message: str | None = None
    inference_source: InferenceSource = "listed_phrase"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confidence"] = None
        return payload


def encounter_from_dict(raw: dict[str, Any]) -> EncounterInput:
    """Convert one JSON object from the evaluation file into EncounterInput."""

    diagnoses = [
        PhraseInput(
            phrase=str(item.get("phrase", "")).strip(),
            clinical_context=item.get("clinical_context", "current"),
        )
        for item in raw.get("diagnoses", [])
    ]
    symptoms = [
        PhraseInput(
            phrase=str(item.get("phrase", "")).strip(),
            clinical_context=item.get("clinical_context", "current"),
        )
        for item in raw.get("symptoms", [])
    ]
    medications = [
        PhraseInput(
            phrase=str(item.get("phrase", "")).strip(),
            clinical_context=item.get("clinical_context", "current"),
        )
        for item in raw.get("medications", [])
    ]
    return EncounterInput(
        encounter_id=str(raw.get("encounter_id", "")).strip(),
        note=str(raw.get("note", "")).strip(),
        diagnoses=diagnoses,
        symptoms=symptoms,
        medications=medications,
    )
