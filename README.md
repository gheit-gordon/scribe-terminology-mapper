# Clinical Terminology Mapper

A prototype that converts clinical text into validated ICD-10-CM diagnosis and RxNorm medication mappings using synthetic test data.

This version is a small, beginner-friendly Python pipeline. It does **not** transcribe audio, open a website, send data to FHIR, deploy to AWS, or fine-tune a model. It also does **not** call an LLM.

## What this version does

1. Reads one synthetic clinical note plus a **manually supplied** list of diagnosis and medication phrases.
2. Checks the sentence around each phrase for denied, historical, unconfirmed, or follow-up wording (rules, not sentiment and not an LLM).
3. Searches official terminology APIs only for findings that may be coded. Current diagnosis abbreviations (such as `UTI`) are expanded for ICD-10 search only; RxNorm medication queries are left as written.
4. Ranks the API hits so a generic code is preferred unless the note adds extra detail.
5. Does **not** suggest a denied finding, an unconfirmed disease, or a follow-up visit reason as an active diagnosis, and does not attach ICD candidates for those rows. Documented symptoms can still be suggested.
6. If a listed medication is written as `{drug} for {reason}`, also searches that stated reason as a diagnosis.
7. Prints structured JSON for a human to review.

The app never invents a medical code or a symptom that is not in the note. If the API fails or returns nothing, the JSON says so.

## Safety rules

- Use only synthetic or fully de-identified test data.
- Never put real patient names, dates of birth, medical record numbers, or notes in this project.
- Do not treat `suggested_code` as a final billed or documented code. Suggested rows stay `needs_review`. Denied or unconfirmed rows are `do_not_code`.
- `confidence` is always `null`. A made-up score would look precise without being valid.

## Project layout

| File | What it is for |
| --- | --- |
| `README.md` | This guide: setup, run, and design notes. |
| `.gitignore` | Tells Git to ignore secrets, virtual environments, and OS junk. |
| `.env.example` | Sample settings you can copy to `.env`. No API key is required. |
| `requirements.txt` | The small list of Python packages to install. |
| `src/__init__.py` | Marks `src` as a Python package so imports like `from src.pipeline import ...` work. |
| `src/main.py` | Command-line entry point. Loads synthetic encounters and prints JSON. |
| `src/schemas.py` | Shared field names and types for inputs and outputs. |
| `src/context.py` | Rule-based negated / historical / uncertain / follow-up checks, plus `{drug} for {reason}`. |
| `src/abbreviations.py` | Small diagnosis abbreviation table used only as an ICD-10 search rewrite. |
| `src/ranking.py` | Reorders official API candidates. Prefer generic unless the note is specific. |
| `src/pipeline.py` | Steps through each phrase, calls the right API, and builds the JSON rows. |
| `src/terminology/__init__.py` | Package file for the API clients. |
| `src/terminology/icd10.py` | ICD-10-CM search using the NLM Clinical Tables API. |
| `src/terminology/rxnorm.py` | Medication search using the official NLM RxNorm API. |
| `tests/__init__.py` | Marks `tests` as a package. |
| `tests/test_pipeline.py` | Tests with fake API responses and synthetic phrases only. |
| `evaluation/synthetic_encounters.json` | Six made-up encounters used for local runs. |

## Important coding decisions

**No LLM.** Phrase extraction is not automated. Each synthetic encounter already lists the phrases to search. Context uses a short cue list on the sentence that contains the phrase.

**Authoritative APIs only.** Diagnoses go to the [NLM Clinical Tables ICD-10-CM API](https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html). Medications go to the [NLM RxNorm REST API](https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html). After validation, `ranking.py` picks a suggested code. Extra validated hits become `alternatives`.

**Generic unless the note is specific.** NLM's first search hit is not a coding decision. The client asks for enough ICD-10 hits that a generic code such as `I10` can appear, then ranking prefers it over `renovascular` / `resistant` when those words are not in the note.

**Context is derived from the note.** Version 2 does not trust the JSON `clinical_context` field as the source of truth. `denies chest pain` is not an active diagnosis. `history of myocardial infarction` prefers old-MI style candidates. `possible pneumonia` with imaging pending is not coded as pneumonia (no pneumonia ICD candidates). `follow-up for UTI` is a visit reason, not an active UTI, and is not searched (so the API cannot return carbon monoxide / utility-gas codes).

**Diagnosis abbreviations are expanded only for ICD-10 search.** A current listed phrase such as `UTI` still appears as `UTI` in `source_phrase`, but the ICD-10 query becomes `urinary tract infection`. Denied, uncertain, and follow-up diagnoses still skip the ICD API entirely, even if the phrase is a known abbreviation. RxNorm medication queries are not rewritten by this table.

**Validate before keeping a code.** ICD-10-CM codes must match the usual code pattern (for example `E11.9`). RxNorm IDs must be numeric RxCUIs, and each ID is checked with RxNorm's properties endpoint so inactive or empty concepts are dropped.

**Medication reason is not drug-class guessing.** `lisinopril for blood pressure` can add a hypertension search. `continues lisinopril` alone does not.

**Modular clients.** `pipeline.py` can accept fake API clients. Tests use those fakes so they do not need the internet or real patient data. A later company backend can call `map_encounter()` the same way.

## Windows setup

Use PowerShell. Run these commands from the project folder
(`scribe-terminology-mapper`).

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

If Windows blocks the virtual environment script, run this once in that PowerShell window, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

You should see `(.venv)` at the start of the prompt after activation.

## Run the mapper

Still inside the activated virtual environment, from the project folder:

```powershell
python -m src.main
```

That maps all six synthetic encounters and prints JSON.

Run one encounter:

```powershell
python -m src.main --encounter-id SYN-001
```

Optional: point at another synthetic JSON file with the same shape:

```powershell
python -m src.main --encounters-file evaluation\synthetic_encounters.json
```

## Run the tests

```powershell
python -m unittest
```

The tests mock terminology APIs. They do not send real patient data anywhere.

## Output fields

Each JSON row contains:

| Field | Meaning |
| --- | --- |
| `encounter_id` | ID from the synthetic file, such as `SYN-001`. |
| `source_text` | The full synthetic note. |
| `source_phrase` | The original listed phrase (or inferred `{drug} for {reason}` query). Diagnosis abbreviations are not rewritten here. |
| `clinical_context` | `current`, `negated`, `historical`, `uncertain`, or `follow_up`, detected from the note. |
| `entity_type` | `diagnosis` or `medication`. |
| `code_system` | `ICD-10-CM` or `RxNorm`. |
| `suggested_code` | Ranked validated API candidate, or `null`. |
| `description` | Official description for that candidate, or `null`. |
| `alternatives` | Other validated API candidates for rows that were actually searched. Empty when the finding is not coded. |
| `review_status` | `needs_review`, `do_not_code`, `no_code_found`, or `api_error`. |
| `confidence` | Always `null` in this version. |
| `error_message` | Details when a lookup fails, is empty, or was not coded. |
| `inference_source` | `listed_phrase`, or `medication_reason` when the diagnosis came from `{drug} for {reason}`. |

## Synthetic evaluation cases

All notes in `evaluation/synthetic_encounters.json` are labeled `SYNTHETIC TEST NOTE` and contain no real patient identifiers.

| ID | What it exercises |
| --- | --- |
| SYN-001 | Straightforward diagnosis (`type 2 diabetes mellitus`) and medication (`metformin`). Ranking should prefer a generic diabetes code if the note has no complication. |
| SYN-002 | Negation (`denies chest pain`) is not coded and is not searched. `lisinopril for blood pressure` adds a hypertension search; ranking should prefer `I10` over specific subtypes. |
| SYN-003 | Historical condition (`history of myocardial infarction`) prefers old-MI style candidates, not acute MI. |
| SYN-004 | Documented `cough` and `fever` can be suggested. `possible pneumonia` with chest x-ray pending is `do_not_code` with no ICD candidates. Azithromycin is only considered if imaging confirms, so it is not a current med. |
| SYN-005 | `follow-up for UTI` is not an active diagnosis (no ICD search, so no carbon monoxide / utility-gas codes). HCTZ has no stated reason, so no extra diagnosis is inferred. HCTZ can still map as a medication. |
| SYN-006 | Current `UTI` keeps `source_phrase` as `UTI` and searches ICD-10 for `urinary tract infection`. |

Diagnosis abbreviations (`UTI`, `HTN`, `T2DM`, `MI`) are expanded only as ICD-10 search queries, and only when the finding may be coded. Follow-up `UTI` in SYN-005 is still not searched.

## Later versions (not built yet)

Possible next steps, without changing this version's scope:

- Use an LLM only to extract phrases, never to invent codes.
- Add a human review UI.
- Send approved codes into a company backend or FHIR resources.
- Add more code systems after the current two are solid.
