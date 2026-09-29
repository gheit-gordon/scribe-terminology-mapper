# Handoff for next chat

Paste this file (or `@docs/HANDOFF.md`) at the start of the next Cursor chat. Product history and requirements live in [`docs/PRD.md`](PRD.md). Stage picture and text contract: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

## Repo

- Path: `C:\Users\Gordon\onedrivedesktop\Documents\GitHub\scribe-terminology-mapper`
- Remote: `https://github.com/ItsNotGordon/scribe-terminology-mapper.git`
- Branch: `main`, tracking `origin/main`
- Last **local** commit: `0ee0996` — Add context-aware ICD abbreviation handling (abbreviation expansion + context-compatibility filter). **Ahead of origin by 1 commit. Do not push unless the user asks.**
- Last **pushed** commit: `d09d173` — Rank generic ICD-10 hits and refuse unconfirmed, denied, or follow-up diagnoses.
- Uncommitted as of this handoff: `docs/` (`ARCHITECTURE.md`, `PRD.md`, this handoff), `evaluation/synthetic_encounters.json` (SYN-007–SYN-010), `src/ranking.py` (supported `with X` bonus), `tests/test_pipeline.py`, `README.md`. Do not commit or push unless the user asks.

## What this product is

A beginner-friendly **Python prototype** that maps synthetic clinical notes to:

- Diagnoses → ICD-10-CM via [NLM Clinical Tables](https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html)
- Medications → RxNorm via [NLM RxNorm REST](https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html)

It is a **lookup-and-filter layer**, not an EHR, not a coder of record, and **not an LLM**. `suggested_code` is always a human-review candidate. `confidence` is always `null`.

## Hard rules (do not break)

- Synthetic / de-identified data only. Notes are labeled `SYNTHETIC TEST NOTE`. No real PHI.
- Never invent a medical code. Codes come only from those APIs, then format/property checks.
- Never invent a symptom that is not in the note or the phrase list.
- Do not add LLM, audio/STT, frontend, FHIR, AWS, extra code systems, or fine-tuning unless the user explicitly asks. Audio is **deferred this mapping phase** and **confirmed in the final project scope** (transcription emits `note` text only; see [`ARCHITECTURE.md`](ARCHITECTURE.md)).
- Do not commit or push unless the user asks.
- Windows / PowerShell. Run from the project folder.

## Current architecture

```
evaluation/synthetic_encounters.json
  → src/main.py
  → src/pipeline.py
       ├─ src/context.py          sentence cues (negated / historical / uncertain / follow_up)
       ├─ src/abbreviations.py    ICD search rewrite only (UTI, HTN, T2DM, MI)
       ├─ src/terminology/icd10.py   ~20 hits, sf=code,name
       ├─ src/terminology/rxnorm.py  name search + properties (original med phrase)
       └─ src/ranking.py
            ├─ filter_context_compatible_candidates (drop unsupported setting)
            └─ rank_candidates (generic unless the note states extra detail)
  → JSON stdout
```

Stage picture: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). Mapping is implemented. LLM phrase extraction is planned (not built). Live transcription is deferred now, confirmed later.

Phrases are still a **manual JSON cheat sheet**. The pipeline does not NLP-extract phrases from the note. Listed `phrase` values are **gold extraction labels** for that future extractor and must appear in the note. JSON `clinical_context` is **not trusted** at runtime; context is detected from the sentence that contains the phrase. Do **not** edit SYN-005 (including its JSON `current` hint).

Medication → diagnosis only if the note has `{drug} for {reason}` (e.g. lisinopril for blood pressure → search hypertension). Do **not** infer indication from drug class (HCTZ alone ≠ I10). `{drug} for {reason}` rows are mapping inferences, not gold extraction labels.

Denied, uncertain, and follow-up findings: `do_not_code`, **skip the terminology API**, empty `alternatives`. This still applies when the listed phrase is a known abbreviation.

Diagnosis abbreviations expand **only** as the ICD-10 search query, and only after that skip gate. `source_phrase` stays the original listed text. RxNorm queries are **not** rewritten. `_map_phrase` takes explicit `normalize_query=True` for diagnosis / medication-reason ICD searches and `False` for medications.

After ICD search: `filter_context_compatible_candidates(candidates, encounter.note, clinical_context)`, then rank. If NLM returned hits but none are compatible → `no_code_found`, empty alternatives, **no fallback** to the unfiltered list. Cue groups live in `ranking.py`, not pipeline. History-description codes are dropped only when the finding is **current**; historical findings may keep them. Do **not** hardcode N39.0 or E11.65.

Ranking: prefer generic when the note is not specific; **boost** a description `with X` extra when that extra is in the phrase or note (SYN-009 hyperglycemia → E11.65). SYN-001 without hyperglycemia still prefers E11.9.

## Evaluation cases (expected)

| ID | Expected |
| --- | --- |
| SYN-001 | Generic T2DM (not E11.65 unless hyperglycemia is in the note) + metformin |
| SYN-002 | Chest pain denied → do_not_code, no ICD search. Lisinopril RxNorm. Hypertension from “for blood pressure” → prefer **I10** not I15.0 |
| SYN-003 | History of MI → prefer old-MI (I25.2-style) not acute I21.x. Aspirin current |
| SYN-004 | Suggest cough/fever. Pneumonia (CXR pending) and azithromycin (if imaging confirms) → do_not_code, no candidates |
| SYN-005 | `follow-up for UTI` → follow_up / do_not_code, no ICD search (avoids UTI matching “utility” / carbon monoxide). HCTZ RxNorm only. No hypertension. **Do not edit this encounter.** |
| SYN-006 | Current `UTI`; `source_phrase` stays `UTI`; ICD search is `urinary tract infection`; suggest **N39.0**, not O86.20 (delivery) |
| SYN-007 | Multiple current: generic T2DM + **I10** + metformin + lisinopril. No `for`, so no extra inferred hypertension |
| SYN-008 | Narrow negation: T2DM `needs_review`; chest pain `do_not_code` (no ICD search); metformin RxNorm |
| SYN-009 | Note states hyperglycemia → prefer **E11.65** over E11.9; metformin RxNorm |
| SYN-010 | Mixed: history of MI → I25.2-style; current T2DM generic; aspirin + metformin current |

## How to run (Windows)

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest
python -m unittest tests.test_pipeline.AbbreviationTests tests.test_pipeline.ContextCompatibilityTests tests.test_pipeline.PipelineContextCompatibilityTests -v
python -m src.main
python -m src.main --encounter-id SYN-009
python -m src.main --encounter-id SYN-010
```

Tests mock NLM (no network, no PHI). `src.main` calls live NLM. After ranking/context/filter or new-encounter changes, rerun live `src.main` (all ten, at least SYN-005, SYN-006, SYN-007, SYN-009).

Last local run after this slice: **43 tests, OK**. Live all ten matched expected codes:

- SYN-001 E11.9 + metformin 6809
- SYN-002 chest pain skip; lisinopril 29046; I10 from medication_reason
- SYN-003 I25.2 + aspirin 1191
- SYN-004 R05.9 / R50.9; pneumonia and azithromycin skip
- SYN-005 UTI `do_not_code` / follow_up; HCTZ 5487
- SYN-006 N39.0
- SYN-007 E11.9 + I10 + metformin 6809 + lisinopril 29046 (listed_phrase only)
- SYN-008 E11.9; chest pain skip; metformin 6809
- SYN-009 **E11.65** + metformin 6809
- SYN-010 I25.2 historical + E11.9 current + aspirin 1191 + metformin 6809

## Lessons the next agent must not re-learn

- NLM row 1 is lexical rank, not coding. The website autocomplete can show a different top hit (shortest match) than raw JSON.
- Ranking can only reorder **returned** hits. `hypertension` puts I10 around 8th; need `maxList` ~20.
- Ranking also cannot drop obstetric/neonatal/stoma/history codes. Live SYN-006 first suggested **O86.20** (UTI following delivery) with no pregnancy in the note. Filter those descriptions **before** ranking. Empty compatible list → `no_code_found`, never fall back to unfiltered hits.
- After filtering, live SYN-006 `alternatives` can be **empty** (only N39.0 survived). That is expected, not a missing-API bug. Fetching ~20 is still required so I10 can appear for hypertension.
- Searching `UTI` with `sf=code,name` matches **utility** in carbon monoxide descriptions. Expand to `urinary tract infection` for **current** ICD searches; still **do not search** follow-up UTI.
- Whole-token match only (`re.escape` + `\b`). Do not expand `utility` or `mild`.
- “Possible pneumonia” is not a diagnosis until imaging (outpatient/scribe rule). Do not invent cough if the note has no cough.
- “Follow-up for X” is a visit reason, not an active diagnosis.
- Unit tests with tiny fake lists can pass while live API output is wrong. After ranking/context/filter changes, rerun live `src.main` for SYN-005, SYN-006, and the new cases (especially SYN-007 I10 and SYN-009 E11.65).
- Generic +5 used to beat documented `with hyperglycemia`. Ranking now **boosts** supported `with X` extras. Do not hardcode E11.65.

## What is explicitly not built yet

- LLM **phrase extraction only** (still never LLM-invented codes). Gold listed phrases are ready for that eval.
- Live transcription (confirmed **final** scope; deferred this mapping phase; `note` text only)
- Human review UI, FHIR, company backend
- Richer synthetic notes that look like real scribe HPI/assessment
- Trimming `alternatives` in the JSON as a separate display choice (filtering already removes incompatible ICD hits)

## User working style

- Explain simply; they asked to unpack E11.65, website vs API, input vs description, why `alternatives` is long, and how to test.
- Prefer fixing retrieval/ranking/context **before** LLM.
- Girlfriend is a medical scribe; outpatient practice: don’t code unconfirmed pneumonia; code documented symptoms.
- They want markdown artifacts in-repo when they say “in a md file.”
- Ask before major architecture forks (locked: phrase cheat sheet; med→dx only with stated reason; abbreviation table is ICD-only; no hardcoded N39.0 / E11.65; audio deferred this phase).

## Suggested next work (only if they ask)

1. Commit `docs/` (including `ARCHITECTURE.md`), encounters, ranking, and tests if they want this slice on GitHub. Push `0ee0996` only if they ask.
2. Optionally show fewer remaining `alternatives` in JSON while still fetching ~20 for ranking.
3. LLM extract phrases from the note; score against listed gold phrases; still retrieve/validate codes from NLM only.
4. Live transcription **after** extraction: audio → `note` text only.
5. Enrich remaining synthetic notes so they look less bare-bones.
