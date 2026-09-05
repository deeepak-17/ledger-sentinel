"""Ingestion must fail loudly.

The failure mode this defends against is quiet: a column silently arriving as
None, or a malformed amount coerced to something plausible, turns into an
unmatched row, which turns into an exception, which makes the exception list
look richer and the match rate look honest. Every one of those is a lie the
system tells about itself. So a bad row raises, and the error names the file and
the line so a human can go and look at it.
"""

from __future__ import annotations

import pytest

from src.ingest import IngestError, load_bank, load_dataset, load_settlements, load_truth
from src.pipeline import seed_dir

BANK_HEADER = "txn_id,value_date,description,utr,credit_paise,debit_paise\n"


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestHappyPath:
    def test_a_seed_directory_round_trips(self):
        dataset = load_dataset(seed_dir("A"))
        assert dataset.seed == "A"
        assert len(dataset.orders) == 80
        assert len(dataset.truth) == len(set(t.truth_id for t in dataset.truth))

    def test_an_absent_utr_becomes_none_not_an_empty_string(self):
        bank = load_bank(seed_dir("A") / "bank.csv")
        blank = [line for line in bank if line.utr is None]
        assert blank, "the dataset should contain consolidated payouts with no reference"
        assert all(line.utr != "" for line in bank)

    def test_optional_settlement_columns_survive_being_empty(self):
        rows = load_settlements(seed_dir("A") / "settlements.csv")
        assert all(r.order_id is None or r.order_id for r in rows)


class TestMalformedInputRaises:
    def test_header_drift_is_rejected_rather_than_guessed(self, tmp_path):
        path = _write(tmp_path, "bank.csv", "txn_id,value_date,description,utr,credit_paise\n")
        with pytest.raises(IngestError, match="header"):
            load_bank(path)

    def test_a_bad_amount_names_the_line(self, tmp_path):
        body = BANK_HEADER + "BNK00001,2026-08-05,NEFT CR,UTR1,not-a-number,0\n"
        path = _write(tmp_path, "bank.csv", body)
        with pytest.raises(IngestError, match="line 2"):
            load_bank(path)

    def test_a_negative_amount_is_rejected(self, tmp_path):
        body = BANK_HEADER + "BNK00001,2026-08-05,NEFT CR,UTR1,-100,0\n"
        path = _write(tmp_path, "bank.csv", body)
        with pytest.raises(IngestError):
            load_bank(path)

    def test_an_empty_required_field_is_not_quietly_nulled(self, tmp_path):
        body = BANK_HEADER + "BNK00001,,NEFT CR,UTR1,100,0\n"
        path = _write(tmp_path, "bank.csv", body)
        with pytest.raises(IngestError, match="line 2"):
            load_bank(path)

    def test_a_file_with_no_rows_is_an_error(self, tmp_path):
        path = _write(tmp_path, "bank.csv", BANK_HEADER)
        with pytest.raises(IngestError, match="no data rows"):
            load_bank(path)

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(IngestError, match="not found"):
            load_bank(tmp_path / "bank.csv")

    def test_truth_must_be_an_array(self, tmp_path):
        path = _write(tmp_path, "truth.json", '{"truth_id": "T0001"}')
        with pytest.raises(IngestError, match="array"):
            load_truth(path)

    def test_broken_json_names_the_file(self, tmp_path):
        path = _write(tmp_path, "truth.json", "[")
        with pytest.raises(IngestError, match="not valid JSON"):
            load_truth(path)
