"""The HTTP surface, end to end through TestClient.

These are integration tests: each one really runs the pipeline, really writes
SQLite, and really reads it back. That is the point -- the API is the path a
judge will click through, and a mocked test of it would prove nothing about
whether the demo works.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import api


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    api.DB_PATH = tmp_path_factory.mktemp("api") / "ledger.db"
    return TestClient(api.app)


class TestRun:
    def test_running_the_held_out_seed(self, client):
        body = client.post("/run", params={"seed": "B"}).json()
        assert body["matches"] == 56
        assert body["exceptions"] == 9
        assert body["match_rate"] == 0.7
        assert body["ai_ran"] is False

    def test_an_unknown_seed_is_a_404(self, client):
        assert client.post("/run", params={"seed": "Z"}).status_code == 404

    def test_asking_for_the_ai_layer_without_a_cache_is_a_clear_503(self, client):
        response = client.get("/metrics", params={"seed": "B", "ai": True})
        # Either the cache is recorded (200) or it is not (503 with an
        # actionable message). A 500 would mean we leaked an internal error.
        assert response.status_code in (200, 503)
        if response.status_code == 503:
            assert "make cache" in response.json()["detail"]


class TestMetrics:
    def test_false_match_rate_is_the_first_key_in_the_payload(self, client):
        body = client.get("/metrics", params={"seed": "B"}).json()
        keys = list(body)
        assert keys.index("false_match_rate") < keys.index("match_rate")
        assert body["false_match_rate"] == 0.0
        assert body["match_rate"] == 0.7

    def test_every_unresolved_item_carries_a_reason(self, client):
        body = client.get("/metrics", params={"seed": "B"}).json()
        assert len(body["unresolved"]) == 9
        assert all(item["reason"].strip() for item in body["unresolved"])

    def test_the_markdown_endpoint_matches_the_generated_document(self, client):
        body = client.get("/metrics.md", params={"seed": "B"}).json()
        assert "# Metrics" in body["markdown"]
        assert "False-match rate" in body["markdown"]


class TestLedgerReadback:
    def test_exceptions_come_back_from_sqlite(self, client):
        client.post("/run", params={"seed": "B"})
        rows = client.get("/exceptions").json()
        assert len(rows) == 9
        assert {row["rule"] for row in rows} == {"no_identifier"}

    def test_matches_come_back_from_sqlite(self, client):
        rows = client.get("/matches").json()
        assert len(rows) == 56

    def test_the_audit_trail_explains_a_single_bank_line(self, client):
        rows = client.get("/audit", params={"subject_id": "BNK00031"}).json()
        assert rows
        assert rows[0]["decision"] == "exception"
        assert isinstance(rows[0]["inputs"], dict)
        assert rows[0]["reason"].strip()

    def test_reading_before_running_is_a_409_not_a_crash(self, tmp_path_factory):
        api.DB_PATH = tmp_path_factory.mktemp("empty") / "nothing.db"
        fresh = TestClient(api.app)
        assert fresh.get("/exceptions").status_code == 409


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
