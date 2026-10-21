"""PII detection and masking.

Two layers, because either one alone leaks:

* **Column level** — the catalog is scanned for columns whose *name* suggests
  personal data. This is the primary defence: an analyst who selects a
  customer email gets it masked even if the value itself looks like a name.
* **Value level** — regex detectors over the returned cells catch PII stored
  in an unexpectedly named column.

Masking is **role-based**. A role listed in ``policy.pii_entitled_roles``
sees the real value; everyone else sees ``[redacted:email]``. The masking
decision is computed from the projection — including alias resolution and
``SELECT *`` expansion — so a column cannot be renamed out of reach.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Column-name patterns mapped to the kind of PII they suggest.
COLUMN_PATTERNS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e_mail", "mail_address"),
    "phone": ("phone", "mobile", "telephone", "tel_no", "msisdn"),
    "ssn": ("ssn", "social_security", "national_id", "nin", "tax_id", "tin"),
    "credit_card": ("card_number", "credit_card", "card_no", "pan", "ccnum"),
    "cvv": ("cvv", "cvc", "csc"),
    "iban": ("iban", "bank_account", "account_number", "acct_no"),
    "address": ("address", "addr_line", "street", "postcode", "postal_code", "zip_code"),
    "dob": ("dob", "date_of_birth", "birth_date", "birthdate", "birthday"),
    "passport": ("passport", "license_no", "licence", "driver_license"),
    "credentials": ("password", "passwd", "pwd", "secret", "api_key", "apikey",
                    "token", "private_key", "salt", "hash"),
    "compensation": ("salary", "wage", "compensation", "pay_rate", "bonus"),
}

#: Value-level detectors. Each is (kind, compiled pattern).
VALUE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"(?<![\d.])\+\d[\d\s().-]{7,}\d(?![\d.])")),
]


@dataclass
class MaskingResult:
    masked_columns: list[str] = field(default_factory=list)
    masked_values: int = 0

    def to_dict(self) -> dict:
        return {
            "masked_columns": self.masked_columns,
            "masked_values": self.masked_values,
        }


def detect_pii_columns(column_names: Sequence[str]) -> dict[str, str]:
    """Map column name -> PII kind, for columns that look like personal data."""
    detected: dict[str, str] = {}
    for column in column_names:
        lowered = (column or "").lower()
        for kind, patterns in COLUMN_PATTERNS.items():
            if any(pattern in lowered for pattern in patterns):
                detected[column] = kind
                break
    return detected


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum — so a long number that is not a card is not masked as one."""
    numbers = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(numbers) <= 19:
        return False
    checksum = 0
    parity = len(numbers) % 2
    for index, digit in enumerate(numbers):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def mask_cell(value: Any) -> tuple[Any, bool]:
    """Mask a single cell if it looks like PII. Returns (value, was_masked)."""
    if not isinstance(value, str) or not value:
        return value, False
    for kind, pattern in VALUE_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        if kind == "credit_card" and not _luhn_valid(match.group(0)):
            continue
        if kind == "phone":
            # A phone number has 7-15 digits once punctuation is removed.
            digits = sum(c.isdigit() for c in match.group(0))
            if not 7 <= digits <= 15:
                continue
        return f"[redacted:{kind}]", True
    return value, False


def mask_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    pii_columns: dict[str, str] | None = None,
    enable_value_masking: bool = True,
) -> tuple[list[list[Any]], MaskingResult]:
    """Mask a result set.

    ``pii_columns`` is the column-level detection; when omitted it is computed
    from the column names. Returns the masked rows and a report of what was
    masked.
    """
    report = MaskingResult()
    detected = detect_pii_columns(columns) if pii_columns is None else dict(pii_columns)

    masked_rows: list[list[Any]] = []
    for row in rows:
        new_row: list[Any] = []
        for index, value in enumerate(row):
            column = columns[index] if index < len(columns) else ""
            if column in detected:
                kind = detected[column]
                if value is not None:
                    report.masked_values += 1
                    if column not in report.masked_columns:
                        report.masked_columns.append(column)
                new_row.append(f"[redacted:{kind}]" if value is not None else None)
                continue
            if enable_value_masking:
                masked, was_masked = mask_cell(value)
                if was_masked:
                    report.masked_values += 1
                new_row.append(masked)
            else:
                new_row.append(value)
        masked_rows.append(new_row)

    return masked_rows, report
