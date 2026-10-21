"""PII detection and masking."""

from __future__ import annotations

from tsql.safety.pii import detect_pii_columns, mask_cell, mask_result


# ------------------------------------------------------------- column level
def test_email_columns_are_detected():
    detected = detect_pii_columns(["id", "name", "email", "region"])
    assert detected == {"email": "email"}


def test_various_pii_columns_are_detected():
    detected = detect_pii_columns(
        ["user_phone", "ssn", "credit_card_number", "date_of_birth", "home_address",
         "password_hash"]
    )
    assert detected["user_phone"] == "phone"
    assert detected["ssn"] == "ssn"
    assert detected["credit_card_number"] == "credit_card"
    assert detected["date_of_birth"] == "dob"
    assert detected["home_address"] == "address"
    assert detected["password_hash"] == "credentials"


def test_ordinary_columns_are_not_flagged():
    detected = detect_pii_columns(["id", "name", "region", "amount", "ordered_at"])
    assert detected == {}


def test_masking_replaces_pii_columns():
    rows = [["Acme", "acme@example.com"], ["Globex", "g@globex.com"]]
    masked, report = mask_result(["name", "email"], rows)
    assert masked[0][0] == "Acme"
    assert masked[0][1] == "[redacted:email]"
    assert report.masked_columns == ["email"]
    assert report.masked_values == 2


def test_null_pii_values_stay_null():
    masked, _ = mask_result(["email"], [[None]])
    assert masked == [[None]]


def test_masking_preserves_row_and_column_count():
    rows = [[1, "a@x.com"], [2, None], [3, "b@y.com"]]
    masked, _ = mask_result(["id", "email"], rows)
    assert len(masked) == 3
    assert all(len(r) == 2 for r in masked)


def test_value_masking_can_be_disabled():
    masked, report = mask_result(["note"], [["reach me at a@x.com"]], enable_value_masking=False)
    assert masked == [["reach me at a@x.com"]]
    assert report.masked_values == 0


# -------------------------------------------------------------- value level
def test_email_values_are_masked():
    assert mask_cell("write to bob@example.org") == ("[redacted:email]", True)


def test_ssn_values_are_masked():
    assert mask_cell("123-45-6789") == ("[redacted:ssn]", True)


def test_credit_card_values_are_masked_by_luhn():
    assert mask_cell("4111 1111 1111 1111") == ("[redacted:credit_card]", True)


def test_a_long_number_that_fails_luhn_is_not_a_card():
    value, masked = mask_cell("1234567890123456")
    assert masked is False
    assert value == "1234567890123456"


def test_phone_numbers_are_masked():
    value, masked = mask_cell("+1 555 123 4567")
    assert masked is True
    assert value == "[redacted:phone]"


def test_ordinary_text_is_untouched():
    value, masked = mask_cell("just some words")
    assert masked is False
    assert value == "just some words"


def test_non_strings_are_untouched():
    assert mask_cell(42) == (42, False)
    assert mask_cell(None) == (None, False)
