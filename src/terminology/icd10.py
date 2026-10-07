"""ICD-10-CM lookup against the NLM Clinical Tables Search Service.

API docs:
    https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html

Why this API:
    The user asked for the official NLM Clinical Tables ICD-10-CM search.
    Codes and descriptions come from NLM's copy of ICD-10-CM. This module
    never constructs a diagnosis code by hand.

Search behavior:
    We search both the code and name fields (`sf=code,name`) so a phrase
    like "type 2 diabetes mellitus" can match a description.

Validation:
    After the API responds, we keep only items whose code looks like
    ICD-10-CM (letter + digits, optional decimal). That is a safety net
    against a malformed payload. It is not a substitute for a clinician.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import requests

from src.schemas import CodeCandidate

DEFAULT_ICD10_URL = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CANDIDATES = 50
USER_AGENT = "ClinicalTerminologyMapper/0.1 (synthetic-educational-prototype)"

# ICD-10-CM examples: E11.9, J44.9, T58.11XA
ICD10CM_CODE_PATTERN = re.compile(r"^[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?$")

GetJson = Callable[[str, dict[str, str]], Any]


class TerminologyApiError(Exception):
    """Raised when the ICD-10-CM API cannot be reached or returns bad data."""


def is_valid_icd10cm_code(code: str) -> bool:
    """Return True if the string matches the ICD-10-CM code pattern."""

    return bool(ICD10CM_CODE_PATTERN.fullmatch(code.strip().upper()))


def parse_search_response(payload: Any, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> list[CodeCandidate]:
    """Turn the Clinical Tables JSON array into CodeCandidate objects.

    The documented response shape is:
        [
          total_count,
          ["E11.9", "E11.65", ...],
          extra_or_null,
          [["E11.9", "Type 2 diabetes mellitus without complications"], ...]
        ]
    """

    if not isinstance(payload, list) or len(payload) < 4:
        raise TerminologyApiError("ICD-10-CM API returned an unexpected JSON shape.")

    display_rows = payload[3]
    if display_rows is None:
        return []
    if not isinstance(display_rows, list):
        raise TerminologyApiError("ICD-10-CM API display list was not a JSON array.")

    candidates: list[CodeCandidate] = []
    for row in display_rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        code = str(row[0]).strip().upper()
        description = str(row[1]).strip()
        if not code or not description:
            continue
        if not is_valid_icd10cm_code(code):
            continue
        candidates.append(CodeCandidate(code=code, description=description))
        if len(candidates) >= max_candidates:
            break
    return candidates


def _default_get_json(url: str, params: dict[str, str], timeout_seconds: float) -> Any:
    try:
        response = requests.get(
            url,
            params=params,
            timeout=timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise TerminologyApiError("ICD-10-CM API request timed out.") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise TerminologyApiError(f"ICD-10-CM API returned HTTP {status}.") from exc
    except requests.RequestException as exc:
        raise TerminologyApiError("ICD-10-CM API request failed.") from exc
    except ValueError as exc:
        raise TerminologyApiError("ICD-10-CM API returned invalid JSON.") from exc


class Icd10Client:
    """Small wrapper around the NLM ICD-10-CM search endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_ICD10_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        get_json: GetJson | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_candidates = max_candidates
        self._get_json = get_json

    def search(self, phrase: str) -> list[CodeCandidate]:
        """Search ICD-10-CM and return validated candidates only."""

        cleaned = phrase.strip()
        if not cleaned:
            return []

        params = {
            "terms": cleaned,
            "sf": "code,name",
            "df": "code,name",
            "maxList": str(self.max_candidates),
        }
        if self._get_json is None:
            payload = _default_get_json(self.base_url, params, self.timeout_seconds)
        else:
            payload = self._get_json(self.base_url, params)
        return parse_search_response(payload, max_candidates=self.max_candidates)
