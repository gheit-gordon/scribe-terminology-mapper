"""Tests for version 2 ranking, context, and mapping.

These tests use only synthetic phrases and fake API responses. They do
not include real patient data and they do not require a live network
connection.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.context import (
    detect_clinical_context,
    diagnosis_query_for_reason,
    extract_medication_reason,
)
from src.pipeline import map_encounter
from src.ranking import rank_candidates
from src.schemas import CodeCandidate, EncounterInput, PhraseInput
from src.terminology.icd10 import (
    Icd10Client,
    TerminologyApiError as Icd10ApiError,
    is_valid_icd10cm_code,
    parse_search_response,
)
from src.terminology.rxnorm import (
    RxNormClient,
    TerminologyApiError as RxNormApiError,
    is_valid_rxcui,
    parse_properties_response,
    parse_rxcui_search_response,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENCOUNTERS_FILE = PROJECT_ROOT / "evaluation" / "synthetic_encounters.json"


class FakeIcd10Client:
    def __init__(self, candidates=None, error=None, by_phrase=None) -> None:
        self.candidates = candidates or []
        self.error = error
        self.by_phrase = {key.lower(): value for key, value in (by_phrase or {}).items()}
        self.phrases: list[str] = []

    def search(self, phrase: str):
        self.phrases.append(phrase)
        if self.error:
            raise self.error
        mapped = self.by_phrase.get(phrase.lower())
        if mapped is not None:
            return list(mapped)
        return list(self.candidates)


class FakeRxNormClient:
    def __init__(self, candidates=None, error=None) -> None:
        self.candidates = candidates or []
        self.error = error
        self.phrases: list[str] = []

    def search(self, phrase: str):
        self.phrases.append(phrase)
        if self.error:
            raise self.error
        return list(self.candidates)


class SchemaAndFileTests(unittest.TestCase):
    def test_synthetic_file_covers_five_required_scenarios(self) -> None:
        raw = json.loads(ENCOUNTERS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(raw), 5)
        scenarios = {item["scenario"] for item in raw}
        self.assertEqual(
            scenarios,
            {
                "straightforward diagnosis and medication",
                "negation",
                "historical condition",
                "uncertain diagnosis",
                "abbreviation",
            },
        )
        for item in raw:
            self.assertEqual(item["data_type"], "synthetic")
            self.assertIn("SYNTHETIC TEST NOTE", item["note"])
            self.assertTrue(item["encounter_id"].startswith("SYN-"))

        syn004 = next(item for item in raw if item["encounter_id"] == "SYN-004")
        self.assertIn("cough", syn004["note"].lower())
        self.assertIn("fever", syn004["note"].lower())
        self.assertIn("chest x-ray pending", syn004["note"].lower())
        phrases = {item["phrase"] for item in syn004["diagnoses"]}
        self.assertEqual(phrases, {"cough", "fever", "pneumonia"})

    def test_mapping_json_uses_null_confidence(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-TEST",
            note="SYNTHETIC TEST NOTE: adult with type 2 diabetes mellitus.",
            diagnoses=[PhraseInput(phrase="type 2 diabetes mellitus")],
        )
        results = map_encounter(
            encounter,
            icd10_client=FakeIcd10Client(
                candidates=[CodeCandidate(code="E11.9", description="Type 2 diabetes mellitus without complications")]
            ),
            rxnorm_client=FakeRxNormClient(),
        )
        payload = results[0].to_dict()
        self.assertIsNone(payload["confidence"])
        self.assertEqual(payload["suggested_code"], "E11.9")
        self.assertEqual(payload["review_status"], "needs_review")


class Icd10HelperTests(unittest.TestCase):
    def test_validates_icd10cm_code_shape(self) -> None:
        self.assertTrue(is_valid_icd10cm_code("E11.9"))
        self.assertTrue(is_valid_icd10cm_code("T58.11XA"))
        self.assertFalse(is_valid_icd10cm_code("not-a-code"))
        self.assertFalse(is_valid_icd10cm_code("E11.9-FAKE"))

    def test_parse_keeps_only_validated_api_rows(self) -> None:
        payload = [
            2,
            ["E11.9", "FAKE"],
            None,
            [
                ["E11.9", "Type 2 diabetes mellitus without complications"],
                ["NOT_A_CODE", "Should be dropped"],
            ],
        ]
        candidates = parse_search_response(payload)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].code, "E11.9")

    def test_parse_rejects_unexpected_shape(self) -> None:
        with self.assertRaises(Icd10ApiError):
            parse_search_response({"unexpected": True})

    def test_client_does_not_call_api_for_blank_phrase(self) -> None:
        def fail_if_called(url, params):
            raise AssertionError("Blank phrases must not call the API.")

        client = Icd10Client(get_json=fail_if_called)
        self.assertEqual(client.search("   "), [])

    def test_client_requests_enough_hits_for_unspecified_codes(self) -> None:
        seen: dict[str, str] = {}

        def capture(url, params):
            seen.update(params)
            return [0, [], None, []]

        Icd10Client(get_json=capture).search("meningitis")
        self.assertGreaterEqual(int(seen["maxList"]), 50)


class RxNormHelperTests(unittest.TestCase):
    def test_validates_numeric_rxcui(self) -> None:
        self.assertTrue(is_valid_rxcui("6809"))
        self.assertFalse(is_valid_rxcui("metformin"))
        self.assertFalse(is_valid_rxcui("68-09"))

    def test_parse_search_ids_and_properties(self) -> None:
        ids = parse_rxcui_search_response({"idGroup": {"rxnormId": ["6809", "not-an-id"]}})
        self.assertEqual(ids, ["6809"])
        candidate = parse_properties_response(
            {"properties": {"rxcui": "6809", "name": "metformin"}}
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.code, "6809")
        self.assertEqual(candidate.description, "metformin")

    def test_missing_properties_are_not_treated_as_valid_codes(self) -> None:
        self.assertIsNone(parse_properties_response({"properties": None}))

    def test_client_validates_search_hits_with_properties(self) -> None:
        calls: list[str] = []

        def fake_get(url: str, params: dict[str, str]):
            calls.append(url)
            if url.endswith("/rxcui.json"):
                return {"idGroup": {"rxnormId": ["6809"]}}
            if url.endswith("/rxcui/6809/properties.json"):
                return {"properties": {"rxcui": "6809", "name": "metformin"}}
            raise AssertionError(f"Unexpected URL {url}")

        client = RxNormClient(get_json=fake_get)
        candidates = client.search("metformin")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].code, "6809")
        self.assertTrue(any(url.endswith("/rxcui.json") for url in calls))
        self.assertTrue(any("properties.json" in url for url in calls))


class RankingTests(unittest.TestCase):
    def test_prefers_generic_diabetes_over_hyperglycemia(self) -> None:
        ranked = rank_candidates(
            [
                CodeCandidate(code="E11.65", description="Type 2 diabetes mellitus with hyperglycemia"),
                CodeCandidate(code="E11.9", description="Type 2 diabetes mellitus without complications"),
            ],
            phrase="type 2 diabetes mellitus",
            note="SYNTHETIC TEST NOTE: Adult patient presents with type 2 diabetes mellitus.",
            clinical_context="current",
        )
        self.assertEqual(ranked[0].code, "E11.9")

    def test_prefers_old_mi_when_history_is_documented(self) -> None:
        ranked = rank_candidates(
            [
                CodeCandidate(code="I21.9", description="Acute myocardial infarction, unspecified"),
                CodeCandidate(code="I25.2", description="Old myocardial infarction"),
            ],
            phrase="myocardial infarction",
            note="SYNTHETIC TEST NOTE: Adult patient has a history of myocardial infarction.",
            clinical_context="historical",
        )
        self.assertEqual(ranked[0].code, "I25.2")

    def test_prefers_essential_hypertension_even_when_renovascular_is_first(self) -> None:
        ranked = rank_candidates(
            [
                CodeCandidate(code="I15.0", description="Renovascular hypertension"),
                CodeCandidate(code="I1A.0", description="Resistant hypertension"),
                CodeCandidate(code="I97.3", description="Postprocedural hypertension"),
                CodeCandidate(code="K76.6", description="Portal hypertension"),
                CodeCandidate(code="P29.2", description="Neonatal hypertension"),
                CodeCandidate(code="G93.2", description="Benign intracranial hypertension"),
                CodeCandidate(code="H40.053", description="Ocular hypertension, bilateral"),
                CodeCandidate(code="I10", description="Essential (primary) hypertension"),
            ],
            phrase="hypertension",
            note="SYNTHETIC TEST NOTE: Continues lisinopril for blood pressure.",
            clinical_context="current",
        )
        self.assertEqual(ranked[0].code, "I10")

    def test_prefers_unspecified_meningitis_over_typhoid(self) -> None:
        ranked = rank_candidates(
            [
                CodeCandidate(code="A01.01", description="Typhoid meningitis"),
                CodeCandidate(code="G03.9", description="Meningitis, unspecified"),
            ],
            phrase="meningitis",
            note="SYNTHETIC TEST NOTE: Adult presents with meningitis.",
            clinical_context="current",
        )
        self.assertEqual(ranked[0].code, "G03.9")

    def test_prefers_other_respiratory_novel_influenza_without_pneumonia(self) -> None:
        ranked = rank_candidates(
            [
                CodeCandidate(
                    code="J09.X1",
                    description="Influenza due to identified novel influenza A virus with pneumonia",
                ),
                CodeCandidate(
                    code="J09.X3",
                    description="Influenza due to identified novel influenza A virus with gastrointestinal manifestations",
                ),
                CodeCandidate(
                    code="J09.X9",
                    description="Influenza due to identified novel influenza A virus with other manifestations",
                ),
                CodeCandidate(
                    code="J09.X2",
                    description="Influenza due to identified novel influenza A virus with other respiratory manifestations",
                ),
            ],
            phrase="novel influenza",
            note="SYNTHETIC TEST NOTE: Adult presents with avian flu.",
            clinical_context="current",
        )
        self.assertEqual(ranked[0].code, "J09.X2")

    def test_dry_eye_laterality_follows_the_phrase_sentence_only(self) -> None:
        unspecified = CodeCandidate(
            code="H04.129",
            description="Dry eye syndrome of unspecified lacrimal gland",
        )
        right = CodeCandidate(
            code="H04.121",
            description="Dry eye syndrome of right lacrimal gland",
        )
        left = CodeCandidate(
            code="H04.122",
            description="Dry eye syndrome of left lacrimal gland",
        )
        bilateral = CodeCandidate(
            code="H04.123",
            description="Dry eye syndrome of bilateral lacrimal glands",
        )
        candidates = [right, left, bilateral, unspecified]
        unrelated = rank_candidates(
            candidates,
            phrase="dry eye",
            note="SYNTHETIC TEST NOTE: Wheezing at the right base. Adult has dry eye.",
            clinical_context="current",
        )
        self.assertEqual(unrelated[0].code, "H04.129")
        named = rank_candidates(
            candidates,
            phrase="dry eye",
            note="SYNTHETIC TEST NOTE: Adult has right dry eye.",
            clinical_context="current",
        )
        self.assertEqual(named[0].code, "H04.121")


class ContextTests(unittest.TestCase):
    def test_detects_negation_history_and_uncertainty(self) -> None:
        self.assertEqual(
            detect_clinical_context("Adult patient denies chest pain.", "chest pain"),
            "negated",
        )
        self.assertEqual(
            detect_clinical_context(
                "Adult patient has a history of myocardial infarction.",
                "myocardial infarction",
            ),
            "historical",
        )
        self.assertEqual(
            detect_clinical_context(
                "Assessment: possible pneumonia, chest x-ray pending.",
                "pneumonia",
            ),
            "uncertain",
        )
        self.assertEqual(
            detect_clinical_context("Adult presents with cough and fever.", "cough"),
            "current",
        )
        self.assertEqual(
            detect_clinical_context("Adult follow-up for UTI. Continues HCTZ.", "UTI"),
            "follow_up",
        )

    def test_later_no_identifiers_line_does_not_negate_the_phrase(self) -> None:
        note = (
            "SYNTHETIC TEST NOTE: Adult presents with cough. "
            "No real patient identifiers are included."
        )
        self.assertEqual(detect_clinical_context(note, "cough"), "current")

    def test_medication_reason_only_when_note_states_for(self) -> None:
        self.assertEqual(
            extract_medication_reason(
                "Continues lisinopril for blood pressure. No real patient identifiers.",
                "lisinopril",
            ),
            "blood pressure",
        )
        self.assertIsNone(extract_medication_reason("Continues lisinopril.", "lisinopril"))
        self.assertEqual(diagnosis_query_for_reason("blood pressure"), "hypertension")


class PipelineTests(unittest.TestCase):
    def test_maps_diagnosis_and_medication_without_inventing_codes(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-001",
            note="SYNTHETIC TEST NOTE: type 2 diabetes mellitus treated with metformin.",
            diagnoses=[PhraseInput(phrase="type 2 diabetes mellitus")],
            medications=[PhraseInput(phrase="metformin")],
        )
        results = map_encounter(
            encounter,
            icd10_client=FakeIcd10Client(
                candidates=[
                    CodeCandidate(code="E11.65", description="Type 2 diabetes mellitus with hyperglycemia"),
                    CodeCandidate(code="E11.9", description="Type 2 diabetes mellitus without complications"),
                ]
            ),
            rxnorm_client=FakeRxNormClient(
                candidates=[CodeCandidate(code="6809", description="metformin")]
            ),
        )
        self.assertEqual(len(results), 2)
        diagnosis, medication = results
        self.assertEqual(diagnosis.suggested_code, "E11.9")
        self.assertTrue(any(item.code == "E11.65" for item in diagnosis.alternatives))
        self.assertEqual(diagnosis.code_system, "ICD-10-CM")
        self.assertEqual(medication.suggested_code, "6809")
        self.assertEqual(medication.code_system, "RxNorm")
        self.assertIsNone(diagnosis.confidence)
        self.assertIsNone(medication.confidence)

    def test_empty_results_and_api_errors_are_explicit(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-EMPTY",
            note="SYNTHETIC TEST NOTE: blank lookup.",
            diagnoses=[PhraseInput(phrase="unknown condition xyz")],
            medications=[PhraseInput(phrase="mystery-drug")],
        )
        results = map_encounter(
            encounter,
            icd10_client=FakeIcd10Client(candidates=[]),
            rxnorm_client=FakeRxNormClient(error=RxNormApiError("RxNorm API request timed out.")),
        )
        self.assertEqual(results[0].review_status, "no_code_found")
        self.assertIsNone(results[0].suggested_code)
        self.assertEqual(results[1].review_status, "api_error")
        self.assertIsNone(results[1].suggested_code)
        self.assertIn("timed out", results[1].error_message)

    def test_blank_phrase_does_not_search(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-BLANK",
            note="SYNTHETIC TEST NOTE: missing phrase.",
            diagnoses=[PhraseInput(phrase="")],
        )
        icd10 = FakeIcd10Client()
        map_encounter(encounter, icd10_client=icd10, rxnorm_client=FakeRxNormClient())
        self.assertEqual(icd10.phrases, [])

    def test_negated_phrase_is_not_suggested_as_a_diagnosis(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-002",
            note="SYNTHETIC TEST NOTE: adult denies chest pain.",
            diagnoses=[PhraseInput(phrase="chest pain")],
        )
        icd10 = FakeIcd10Client(
            candidates=[CodeCandidate(code="R07.9", description="Chest pain, unspecified")]
        )
        results = map_encounter(
            encounter,
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(),
        )
        self.assertEqual(results[0].clinical_context, "negated")
        self.assertEqual(results[0].review_status, "do_not_code")
        self.assertIsNone(results[0].suggested_code)
        self.assertEqual(results[0].alternatives, [])
        self.assertEqual(icd10.phrases, [])

    def test_lisinopril_for_blood_pressure_adds_hypertension_search(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-002",
            note="SYNTHETIC TEST NOTE: Adult patient denies chest pain. Continues lisinopril for blood pressure.",
            diagnoses=[PhraseInput(phrase="chest pain")],
            medications=[PhraseInput(phrase="lisinopril")],
        )
        icd10 = FakeIcd10Client(
            by_phrase={
                "chest pain": [CodeCandidate(code="R07.9", description="Chest pain, unspecified")],
                "hypertension": [
                    CodeCandidate(code="I15.0", description="Renovascular hypertension"),
                    CodeCandidate(code="I1A.0", description="Resistant hypertension"),
                    CodeCandidate(code="I97.3", description="Postprocedural hypertension"),
                    CodeCandidate(code="K76.6", description="Portal hypertension"),
                    CodeCandidate(code="P29.2", description="Neonatal hypertension"),
                    CodeCandidate(code="G93.2", description="Benign intracranial hypertension"),
                    CodeCandidate(code="H40.053", description="Ocular hypertension, bilateral"),
                    CodeCandidate(code="I10", description="Essential (primary) hypertension"),
                ],
            }
        )
        results = map_encounter(
            encounter,
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(
                candidates=[CodeCandidate(code="29046", description="lisinopril")]
            ),
        )
        self.assertIn("hypertension", icd10.phrases)
        self.assertNotIn("chest pain", icd10.phrases)
        by_phrase = {item.source_phrase: item for item in results}
        self.assertEqual(by_phrase["chest pain"].review_status, "do_not_code")
        self.assertEqual(by_phrase["chest pain"].alternatives, [])
        self.assertEqual(by_phrase["lisinopril"].suggested_code, "29046")
        hypertension = by_phrase["hypertension"]
        self.assertEqual(hypertension.entity_type, "diagnosis")
        self.assertEqual(hypertension.suggested_code, "I10")
        self.assertEqual(hypertension.inference_source, "medication_reason")
        self.assertEqual(hypertension.review_status, "needs_review")

    def test_does_not_infer_diagnosis_from_drug_without_stated_reason(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-MED",
            note="SYNTHETIC TEST NOTE: Continues lisinopril.",
            medications=[PhraseInput(phrase="lisinopril")],
        )
        icd10 = FakeIcd10Client(
            candidates=[CodeCandidate(code="I10", description="Essential (primary) hypertension")]
        )
        results = map_encounter(
            encounter,
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(
                candidates=[CodeCandidate(code="29046", description="lisinopril")]
            ),
        )
        self.assertEqual(icd10.phrases, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_phrase, "lisinopril")

    def test_historical_mi_prefers_old_over_acute(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-003",
            note="SYNTHETIC TEST NOTE: Adult patient has a history of myocardial infarction.",
            diagnoses=[PhraseInput(phrase="myocardial infarction")],
        )
        results = map_encounter(
            encounter,
            icd10_client=FakeIcd10Client(
                candidates=[
                    CodeCandidate(code="I21.9", description="Acute myocardial infarction, unspecified"),
                    CodeCandidate(code="I25.2", description="Old myocardial infarction"),
                ]
            ),
            rxnorm_client=FakeRxNormClient(),
        )
        self.assertEqual(results[0].clinical_context, "historical")
        self.assertEqual(results[0].suggested_code, "I25.2")
        self.assertEqual(results[0].review_status, "needs_review")

    def test_possible_pneumonia_is_not_coded_documented_cough_is(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-004",
            note=(
                "SYNTHETIC TEST NOTE: Adult presents with cough and fever. "
                "Assessment: possible pneumonia, chest x-ray pending. "
                "Consider azithromycin if imaging confirms the diagnosis."
            ),
            diagnoses=[
                PhraseInput(phrase="cough"),
                PhraseInput(phrase="fever"),
                PhraseInput(phrase="pneumonia"),
            ],
            medications=[PhraseInput(phrase="azithromycin")],
        )
        icd10 = FakeIcd10Client(
            by_phrase={
                "cough": [CodeCandidate(code="R05.9", description="Cough, unspecified")],
                "fever": [CodeCandidate(code="R50.9", description="Fever, unspecified")],
                "pneumonia": [
                    CodeCandidate(code="J18.9", description="Pneumonia, unspecified organism")
                ],
            }
        )
        rxnorm = FakeRxNormClient(
            candidates=[CodeCandidate(code="18631", description="azithromycin")]
        )
        results = map_encounter(
            encounter,
            icd10_client=icd10,
            rxnorm_client=rxnorm,
        )
        by_phrase = {item.source_phrase: item for item in results}
        self.assertEqual(by_phrase["cough"].suggested_code, "R05.9")
        self.assertEqual(by_phrase["cough"].review_status, "needs_review")
        self.assertEqual(by_phrase["fever"].suggested_code, "R50.9")
        self.assertEqual(by_phrase["pneumonia"].review_status, "do_not_code")
        self.assertIsNone(by_phrase["pneumonia"].suggested_code)
        self.assertEqual(by_phrase["pneumonia"].alternatives, [])
        self.assertNotIn("pneumonia", icd10.phrases)
        self.assertEqual(by_phrase["azithromycin"].review_status, "do_not_code")
        self.assertIsNone(by_phrase["azithromycin"].suggested_code)
        self.assertEqual(by_phrase["azithromycin"].alternatives, [])
        self.assertEqual(rxnorm.phrases, [])

    def test_follow_up_uti_has_no_diagnosis_and_hctz_does_not_infer_hypertension(self) -> None:
        encounter = EncounterInput(
            encounter_id="SYN-005",
            note="SYNTHETIC TEST NOTE: Adult follow-up for UTI. Continues HCTZ.",
            diagnoses=[PhraseInput(phrase="UTI")],
            medications=[PhraseInput(phrase="HCTZ")],
        )
        icd10 = FakeIcd10Client(
            candidates=[
                CodeCandidate(
                    code="T58.11XA",
                    description="Toxic effect of carbon monoxide from utility gas, accidental (unintentional), initial encounter",
                )
            ]
        )
        results = map_encounter(
            encounter,
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(
                candidates=[CodeCandidate(code="5487", description="hydrochlorothiazide")]
            ),
        )
        self.assertEqual(icd10.phrases, [])
        by_phrase = {item.source_phrase: item for item in results}
        self.assertEqual(by_phrase["UTI"].review_status, "do_not_code")
        self.assertEqual(by_phrase["UTI"].clinical_context, "follow_up")
        self.assertIsNone(by_phrase["UTI"].suggested_code)
        self.assertEqual(by_phrase["UTI"].alternatives, [])
        self.assertEqual(by_phrase["HCTZ"].suggested_code, "5487")
        self.assertNotIn("hypertension", by_phrase)
        self.assertEqual(len(results), 2)

    def test_avian_and_swine_flu_search_novel_influenza(self) -> None:
        novel = CodeCandidate(
            code="J09.X2",
            description="Influenza due to identified novel influenza A virus with other respiratory manifestations",
        )
        for phrase in ("avian flu", "swine flu"):
            icd10 = FakeIcd10Client(by_phrase={"novel influenza": [novel]})
            results = map_encounter(
                EncounterInput(
                    encounter_id="SYN-FLU",
                    note=f"SYNTHETIC TEST NOTE: Adult presents with {phrase}.",
                    diagnoses=[PhraseInput(phrase=phrase)],
                ),
                icd10_client=icd10,
                rxnorm_client=FakeRxNormClient(),
            )
            self.assertEqual(icd10.phrases, ["novel influenza"])
            self.assertEqual(results[0].source_phrase, phrase)
            self.assertEqual(results[0].suggested_code, "J09.X2")

    def test_lockjaw_searches_jaw_closure_unless_the_note_mentions_tetanus(self) -> None:
        icd10 = FakeIcd10Client(
            by_phrase={
                "abnormal jaw closure": [CodeCandidate(code="M26.51", description="Abnormal jaw closure")],
                "tetanus": [CodeCandidate(code="A35", description="Other tetanus")],
            }
        )
        plain = map_encounter(
            EncounterInput(
                encounter_id="SYN-JAW",
                note="SYNTHETIC TEST NOTE: Adult presents with lockjaw.",
                diagnoses=[PhraseInput(phrase="lockjaw")],
            ),
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(),
        )
        self.assertEqual(icd10.phrases, ["abnormal jaw closure"])
        self.assertEqual(plain[0].source_phrase, "lockjaw")
        self.assertEqual(plain[0].suggested_code, "M26.51")

        icd10.phrases.clear()
        with_tetanus = map_encounter(
            EncounterInput(
                encounter_id="SYN-JAW",
                note="SYNTHETIC TEST NOTE: Adult presents with lockjaw after tetanus.",
                diagnoses=[PhraseInput(phrase="lockjaw")],
            ),
            icd10_client=icd10,
            rxnorm_client=FakeRxNormClient(),
        )
        self.assertEqual(icd10.phrases, ["tetanus"])
        self.assertEqual(with_tetanus[0].source_phrase, "lockjaw")
        self.assertEqual(with_tetanus[0].suggested_code, "A35")


if __name__ == "__main__":
    unittest.main()
