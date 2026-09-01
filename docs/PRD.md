# Clinical Terminology Mapper — History and PRD

Prototype repo: [scribe-terminology-mapper](https://github.com/ItsNotGordon/scribe-terminology-mapper)

Current branch: `main`. Last **local** commit: `0ee0996` (abbreviation expansion + context-compatibility filter). **Ahead of origin by 1 commit; do not push unless asked.** Last **pushed** commit: `d09d173`. `docs/` (this PRD and the handoff) is still **uncommitted**.

Data policy: **synthetic notes only**. No real patient information.

---

## Part 1 — What happened so far

### Starting point

The GitHub repo was almost empty: a one-line README, a `.gitignore`, and a blank `src/main.py`. The stated product was a beginner-friendly Python pipeline that maps:

- Diagnoses → **ICD-10-CM** (NLM Clinical Tables API)
- Medications → **RxNorm** (official NLM REST API)

Hard constraints from day one:

- Never invent medical codes
- Never put real PHI in the project
- No LLM yet
- No audio, frontend, FHIR, AWS, extra code systems, or fine-tuning
- Keep the code modular for a later company backend

### Version 1 — lookup only

The first build accepted a synthetic note **plus a human-written phrase list**. It did not read the note for meaning. It searched NLM and copied the first validated hit into `suggested_code`. `confidence` was always `null`. Every successful row was `needs_review`.

Five synthetic encounters were created:

| ID | Original intent |
| --- | --- |
| SYN-001 | Straightforward: type 2 diabetes + metformin |
| SYN-002 | Negation: denies chest pain; lisinopril |
| SYN-003 | Historical MI + aspirin |
| SYN-004 | Uncertain: possible pneumonia |
| SYN-005 | Abbreviations: UTI + HCTZ |

That first version was committed as `20d08c5`.

### The first real confusion: who chose E11.65?

A live run of SYN-001 suggested **E11.65** (type 2 diabetes **with hyperglycemia**). The note never mentioned hyperglycemia. The clinically reasonable general match is **E11.9** (without complications).

That was not hardcoded, and it was not an AI. The pipeline sent `type 2 diabetes mellitus` to NLM and kept **row 1 of the JSON**. NLM’s search ranking is not ICD coding.

The NLM **website** search box on the Clinical Tables doc page can show **E11.9** first because the autocomplete widget promotes the **shortest match**. Raw API JSON still had E11.65 first. Same API, different presentation.

Lesson: **search rank ≠ correct code.** Version 1 was a lookup tool, not a coder.

### Version 2 — ranking, context, medication reason (still no LLM)

The decision was to harden the pipeline **before** adding an LLM, because a model that can hallucinate codes is dangerous if the framework is wrong.

“Sentiment” and a custom neural net were considered and rejected. Clinical notes need **negation / history / uncertainty**, not happy/sad. That is a small rule list (NegEx/ConText style), not a trained model.

Locked product decisions:

1. **Phrases stay in the JSON cheat sheet.** The app does not extract words from the note by itself yet.
2. **Infer a diagnosis from a medication only if the note states the reason.** `lisinopril for blood pressure` → search hypertension. `continues lisinopril` → do not invent hypertension from drug class.

Version 2 added:

- `src/ranking.py` — prefer generic codes unless the note has extra detail
- `src/context.py` — sentence-level cues: denied, historical, uncertain
- Medication pattern `{drug} for {reason}`
- SYN-004 rewritten (after scribe feedback) to include **cough, fever, CXR pending**, not just “possible pneumonia”

Scribe feedback that changed SYN-004: **you cannot diagnose pneumonia until imaging is back.** Until then, code documented **symptoms**. The original SYN-004 note had no cough, so searching “coughing” would have been inventing a finding.

### Live runs exposed three more bugs

Unit tests passed because they used **short fake API lists**. Live `python -m src.main` did not.

1. **SYN-002 hypertension**
   Search `hypertension` returned I15.0 Renovascular, I1A.0 Resistant, and other specific subtypes. **I10 Essential hypertension was 8th.** The client only kept 5 hits, so ranking never saw I10.

2. **SYN-004 pneumonia**
   Context marked pneumonia `uncertain` and set `suggested_code` to null, but still **searched ICD-10** and put pneumonia codes in `alternatives`. That still looked like coding pneumonia.

3. **SYN-005 UTI → carbon monoxide**
   Searching `UTI` matches **uti**lity in “Toxic effect of carbon monoxide from **utility** gas.” The API matches letters, not clinical meaning. Also, `follow-up for UTI` is a **checkup**, not a new active UTI. HCTZ had **no stated reason**, so hypertension must not be inferred.

Fixes now on `main` (`d09d173`):

- Keep about 20 ICD-10 hits so I10 can be ranked first
- Skip ICD search entirely for denied / unconfirmed / follow-up diagnoses (empty `alternatives`)
- Treat `follow-up for` as `follow_up` / `do_not_code`
- HCTZ remains a medication lookup only

### Abbreviation expansion for current ICD-10 searches

A small curated table (`src/abbreviations.py`) expands listed diagnosis abbreviations (`UTI`, `HTN`, `T2DM`, `MI`) **only as the ICD-10 search query**, and only after context detection. Matching is case-insensitive whole-token (`re.escape` + word boundaries), so `utility` is not treated as `UTI`. `source_phrase` stays the original listed text. Follow-up, denied, and unconfirmed diagnoses still skip the ICD API, so SYN-005 does not search `UTI`. RxNorm medication queries are not rewritten. SYN-006 is an active-UTI encounter for that path.

Live JSON for a coded diagnosis often lists many `alternatives`. That is leftover NLM hits after ranking (the client asks for about 20 ICD-10 rows so a generic code such as I10 is in the list). It is not a claim that every alternative is a good code. `do_not_code` rows still have empty `alternatives`. After context-compatibility filtering, only **supported** hits remain in `alternatives`.

### Context-compatibility before ranking

A live SYN-006 run suggested **O86.20** (UTI following delivery) even though the note was only “Adult presents with UTI.” N39.0 was in the candidate list. Ranking could not fix that: it only reorders hits; it does not drop codes that need pregnancy, delivery, neonatal, stoma, or personal-history context the note never states.

`filter_context_compatible_candidates` now removes those unsupported ICD descriptions **before** ranking. Remaining generic codes (such as N39.0) can then win. This is not a hardcoded “UTI always means N39.0” rule. If every retrieved ICD candidate needs unsupported context, the row is `no_code_found` with empty alternatives — the unfiltered list is never used as a fallback. Historical findings may still keep a personal-history candidate. RxNorm is not filtered this way.

Live verification after that change: SYN-006 suggested **N39.0**; SYN-005 stayed `do_not_code` for follow-up UTI with HCTZ as RxNorm only. Mocked suite: **36 tests, OK**.

---

## Part 2 — Product requirements document

### 1. Problem

Clinicians and scribes write notes in English. Billing and problem lists need **ICD-10-CM** and **RxNorm**. Humans already make mistakes; software that **invents** codes or treats a search hit as a diagnosis is worse.

NLM APIs return official candidates, but:

- Rank is lexical, not clinical
- Abbreviations match the wrong words (`UTI` → utility)
- Hits can require a setting the note never states (adult UTI → O86.20 following delivery)
- Negation, history, “possible,” and follow-up change whether something should be coded at all

### 2. Product

**Clinical Terminology Mapper** is a local Python prototype that:

1. Takes a **synthetic** encounter (note + listed phrases)
2. Decides whether each phrase is current, denied, historical, unconfirmed, or follow-up
3. Retrieves candidates from **authoritative** APIs
4. Drops ICD candidates that need context the note does not support, then ranks toward **generic** codes unless the note is specific
5. Emits JSON for **human review**

It is **not** an EHR, not a coder of record, and not an LLM coder.

### 3. Users (current)

- The builder, learning how terminology APIs and coding rules interact
- Later: a company backend that calls `map_encounter()` and a human reviewer

### 4. Goals

| Goal | How we know |
| --- | --- |
| Never invent a code | Every `suggested_code` came from NLM and passed a format/property check |
| Never invent a symptom | Only phrases in the note/cheat sheet (or `{drug} for {reason}`) are searched |
| Prefer generic unless specified | Diabetes without hyperglycemia → E11.9-class, not E11.65; hypertension → I10, not I15.0 |
| Do not code denied findings | SYN-002 chest pain → `do_not_code`, no ICD search |
| Do not code unconfirmed disease | SYN-004 pneumonia → `do_not_code`; cough/fever may be suggested |
| Do not code follow-up as active disease | SYN-005 UTI → `do_not_code`, no search |
| Expand current diagnosis abbreviations for ICD search only | SYN-006 `UTI` searches `urinary tract infection`; `source_phrase` stays `UTI`; RxNorm queries are unchanged |
| Do not suggest ICD codes that need unsupported context | Adult UTI → not O86.20 (delivery); prefer a supported generic such as N39.0 |
| Infer diagnosis from med only with a written reason | lisinopril **for blood pressure** → hypertension search; HCTZ alone → no I10 |
| Honest uncertainty | `confidence` is always `null` |
| Synthetic data only | All notes labeled `SYNTHETIC TEST NOTE` |

### 5. Non-goals (now)

- LLM phrase extraction or code selection
- Sentiment analysis / custom neural nets
- Inferring indication from drug class
- A disease-to-required-test encyclopedia
- Audio, UI, FHIR, AWS, extra code systems (CPT, SNOMED, etc.)
- Real patient notes
- Inpatient “code possible diagnoses as if confirmed” rules (this prototype follows **outpatient / scribe** practice)

### 6. Functional requirements

**Input**

- `encounter_id`, synthetic `note`
- Manual lists: `diagnoses[].phrase`, `medications[].phrase`
- JSON `clinical_context` is **not** trusted; context is detected from the note

**Pipeline**

1. For each listed phrase, take the **sentence** that contains it (so “No real patient identifiers” cannot negate the whole note).
2. Cues:
   - negated: denies, no, without, …
   - uncertain: possible, pending, consider, rule out, …
   - follow_up: follow-up for, f/u for, …
   - historical: history of, prior, old, …
   - else current
3. If diagnosis/med is negated, uncertain, or follow_up → `do_not_code`, **do not call** the terminology API, empty alternatives. This still applies when the listed phrase is a known abbreviation.
4. Otherwise search:
   - Diagnoses: expand known whole-token abbreviations (`UTI` → `urinary tract infection`, `HTN` → `hypertension`, `T2DM` → `type 2 diabetes mellitus`, `MI` → `myocardial infarction`), then NLM Clinical Tables ICD-10-CM (`sf=code,name`, about 20 hits). Original phrase stays in `source_phrase`.
   - Medications: RxNorm name search using the **original** medication phrase, then properties lookup to confirm RxCUI
5. Filter ICD-10 candidates for **context compatibility** using the clinical note (not only the search phrase). Drop descriptions that require pregnancy, delivery/postpartum, abortion/ectopic, neonatal/newborn, stoma, or (when the finding is current) personal history, unless that context is in the note. Historical findings may keep history-code candidates. If NLM returned hits but none are compatible → `no_code_found`, empty alternatives, no fallback to the unfiltered list.
6. Rank remaining candidates: prefer unspecified / without complications / essential (primary); downrank extra adjectives not in the note; historical prefers old over acute.
7. If a listed med matches `{drug} for {reason}`, also search the stated reason (for example blood pressure → hypertension) as a diagnosis with `inference_source: medication_reason`. That ICD search may expand abbreviations and uses the same compatibility filter.

**Output (each JSON row)**

`encounter_id`, `source_text`, `source_phrase` (original listed phrase, not the expanded ICD query), `clinical_context`, `entity_type`, `code_system`, `suggested_code`, `description`, `alternatives`, `review_status`, `confidence` (always null), `error_message`, `inference_source`

`review_status`: `needs_review` | `do_not_code` | `no_code_found` | `api_error`

`alternatives`: remaining **context-compatible** validated API hits after the ranked `suggested_code`. Coded ICD-10 rows can still have extras because the client fetches about 20, then filters. Empty when the finding is not coded or no compatible candidate remains.

### 7. Evaluation cases (current expected behavior)

| ID | Note (short) | Expected |
| --- | --- | --- |
| SYN-001 | T2DM, started metformin | Suggest generic diabetes + metformin RxCUI; needs_review |
| SYN-002 | Denies chest pain; lisinopril for blood pressure | Chest pain do_not_code (no search); lisinopril RxNorm; hypertension → prefer **I10** |
| SYN-003 | History of MI; aspirin | Prefer old-MI style code, not acute MI; aspirin current |
| SYN-004 | Cough, fever; possible pneumonia, CXR pending; consider azithromycin if imaging confirms | Suggest cough/fever; pneumonia and azithromycin do_not_code, no disease/med candidates |
| SYN-005 | Follow-up for UTI; continues HCTZ | UTI do_not_code, no ICD search; HCTZ RxNorm only; no hypertension |
| SYN-006 | Adult presents with UTI | `source_phrase` stays `UTI`; ICD search is `urinary tract infection`; suggest N39.0-class unspecified UTI, **not** O86.20 |

Tests: `python -m unittest` (mocked APIs, no PHI, no live NLM). Last run: 36 tests, OK. Live `src.main` for SYN-005 / SYN-006 was rechecked after the compatibility filter.

### 8. Architecture

```
synthetic JSON → main.py → pipeline.py
                      ├─ context.py (cues)
                      ├─ abbreviations.py (ICD search rewrite only)
                      ├─ icd10.py / rxnorm.py (NLM)
                      └─ ranking.py (context-compatible, then generic-first)
                 → JSON rows for a human
```

Dependencies: `requests`, `python-dotenv`. No API key for these public NLM endpoints.

### 9. Safety and coding rules (product, not just implementation)

- `suggested_code` is a **candidate**, never a billed diagnosis
- Do not code findings the note denied
- Do not code disease that is only possible / pending a test
- Do not code a follow-up visit reason as an active problem
- Do not invent symptoms that are not written
- Do not guess why a drug is prescribed unless the note says `for X`
- Do not treat NLM row 1 as the clinically correct code
- Expand known diagnosis abbreviations only for ICD-10 search, and only after deciding the finding may be coded
- Do not suggest an ICD-10 code that requires pregnancy, delivery, neonatal, stoma, or (when current) personal-history context unless the note states that context

### 10. Open issues / next PRD slice

These are **not** built:

- LLM **phrase extraction only**, still never LLM-invented codes
- Human review UI
- Company backend / FHIR
- Richer notes (the synthetic charts are still short compared with real scribe notes)
- A shorter `alternatives` list in the JSON (fetch ~20 for ranking; showing fewer is an optional later product choice)

---

## Bottom line

The product is a **safe lookup-and-filter layer** on official terminology APIs. Version 1 proved retrieval. Version 2 taught **most of the work is deciding what not to code**, then ranking generic official codes for what remains. Abbreviation expansion rewrites the ICD search for current findings only. Context-compatibility then drops official hits that still require a setting the note never describes, so a plain adult UTI cannot surface as UTI following delivery.
