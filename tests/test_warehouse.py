"""Tests for the per-session DuckDB mart and its SQL guard.

The text-to-SQL LLM call is not tested here. These cover the guard, session
isolation, MCP-row coercion, and the SQL passthrough the tool uses when the
question is already a read.
"""

import pytest

from src.warehouse.hydrate import coerce_rows, hydrate_session
from src.warehouse.sql_guard import apply_limit, ensure_readonly, extract_sql, looks_like_sql, strip_internal_ids
from src.warehouse.store import drop_session, fetch_rows, load_session, reset, schema_text
from src.warehouse.tool import run_warehouse_question

_PEOPLE = [
    {"person_id": "P01", "full_name": "Maya Chen", "job_title": "Engineering Manager"},
    {"person_id": "P02", "full_name": "Ivo Petrov", "job_title": "Senior Backend Engineer"},
    {"person_id": "P03", "full_name": "Priya Shah", "job_title": "Data Analyst"},
]
_RECOGNITIONS = [
    {"recognition_id": "R1", "sender_id": "P02", "receiver_id": "P03", "award_reason": "Innovation"},
    {"recognition_id": "R2", "sender_id": "P03", "receiver_id": "P02", "award_reason": "Collaboration"},
    {"recognition_id": "R3", "sender_id": "P02", "receiver_id": "P01", "award_reason": "Innovation"},
]


@pytest.fixture(autouse=True)
def _clean_sessions():
    reset()
    yield
    reset()


class TestSqlGuard:
    def test_extracts_fenced_sql(self):
        assert extract_sql("Here you go:\n```sql\nSELECT 1\n```\n") == "SELECT 1"

    def test_rejects_insert(self):
        with pytest.raises(ValueError, match="read-only"):
            ensure_readonly("INSERT INTO people VALUES ('P99')")

    def test_rejects_copy_and_file_readers(self):
        with pytest.raises(ValueError, match="read-only"):
            ensure_readonly("COPY people TO 'out.csv'")
        with pytest.raises(ValueError, match="read-only"):
            ensure_readonly("SELECT * FROM read_csv_auto('people.csv')")

    def test_rejects_multiple_statements(self):
        with pytest.raises(ValueError, match="one SQL statement"):
            ensure_readonly("SELECT 1; SELECT 2")

    def test_allows_with_and_from_first(self):
        assert ensure_readonly("WITH x AS (SELECT 1 AS n) SELECT n FROM x").startswith("WITH")
        assert ensure_readonly("FROM people SELECT full_name").startswith("FROM")

    def test_appends_limit_when_missing(self):
        assert apply_limit("SELECT full_name FROM people").endswith("LIMIT 50")
        assert "LIMIT 5" in apply_limit("SELECT full_name FROM people LIMIT 5")

    def test_english_select_is_not_sql(self):
        assert not looks_like_sql("Select the best senders from my team")
        assert looks_like_sql("SELECT sender.full_name FROM people sender")

    def test_strips_id_columns(self):
        rows = strip_internal_ids(
            [{"person_id": "P01", "full_name": "Maya Chen", "recognition_id": "R001"}]
        )
        assert rows == [{"full_name": "Maya Chen"}]


class TestCoerceRows:
    def test_json_string_array(self):
        assert coerce_rows('[{"FullName": "Ana"}]') == [{"FullName": "Ana"}]

    def test_wrapped_data_key(self):
        assert coerce_rows({"data": [{"n": 1}]}) == [{"n": 1}]

    def test_content_block(self):
        assert coerce_rows([{"type": "text", "text": '[{"n": 2}]'}]) == [{"n": 2}]

    def test_empty_and_garbage(self):
        assert coerce_rows(None) == []
        assert coerce_rows("not json") == []


class TestStore:
    def test_sessions_are_isolated(self):
        load_session("a", {"people": [{"full_name": "Ana"}]})
        load_session("b", {"people": [{"full_name": "Ben"}]})
        assert fetch_rows("SELECT full_name FROM people", "a") == [{"full_name": "Ana"}]
        assert fetch_rows("SELECT full_name FROM people", "b") == [{"full_name": "Ben"}]

    def test_missing_session_raises(self):
        with pytest.raises(RuntimeError, match="not loaded"):
            fetch_rows("SELECT 1", "missing")

    def test_schema_text_uses_loaded_columns(self):
        load_session("s", {"team_reach": [{"FullName": "Ana", "Sent": 4}]})
        text = schema_text("s")
        assert "team_reach" in text
        assert "FullName" in text
        assert "Sent" in text

    def test_join_on_this_session(self):
        load_session("s", {"people": _PEOPLE, "recognitions": _RECOGNITIONS})
        rows = fetch_rows(
            """
            SELECT sender.full_name AS sender_name, count(*) AS sent
            FROM recognitions r
            JOIN people sender ON sender.person_id = r.sender_id
            GROUP BY sender.full_name
            ORDER BY sent DESC
            LIMIT 1
            """,
            "s",
        )
        assert rows[0]["sender_name"] == "Ivo Petrov"
        assert rows[0]["sent"] == 2


class TestHydrate:
    @pytest.mark.asyncio
    async def test_loads_mcp_rows_once(self, monkeypatch):
        calls = {"n": 0}

        class FakeTool:
            def __init__(self, name, rows):
                self.name = name
                self._rows = rows

            async def ainvoke(self, _args):
                calls["n"] += 1
                return self._rows

        async def fake_load(_email):
            return [
                FakeTool("get_my_teams_reach_data", [{"FullName": "Ana", "Sent": 3}]),
                FakeTool("get_award_reasons_data", [{"AwardReason": "Teamwork"}]),
            ]

        monkeypatch.setattr("src.warehouse.hydrate.load_mcp_tools", fake_load)

        first = await hydrate_session("thread-1", "ana@example.com")
        second = await hydrate_session("thread-1", "ana@example.com")

        assert first["cached"] is False
        assert second["cached"] is True
        assert first["tables"]["team_reach"] == 1
        assert first["tables"]["award_reasons"] == 1
        assert calls["n"] == 2
        assert fetch_rows('SELECT "FullName" FROM team_reach', "thread-1") == [
            {"FullName": "Ana"}
        ]

    @pytest.mark.asyncio
    async def test_two_threads_do_not_share_rows(self, monkeypatch):
        async def fake_load(email):
            name = "Ana" if "ana" in email else "Ben"

            class Tool:
                def __init__(self, tool_name, rows):
                    self.name = tool_name
                    self._rows = rows

                async def ainvoke(self, _args):
                    return self._rows

            return [
                Tool("get_my_teams_reach_data", [{"FullName": name}]),
                Tool("get_award_reasons_data", []),
            ]

        monkeypatch.setattr("src.warehouse.hydrate.load_mcp_tools", fake_load)
        await hydrate_session("t-ana", "ana@example.com")
        await hydrate_session("t-ben", "ben@example.com")
        assert fetch_rows('SELECT "FullName" FROM team_reach', "t-ana") == [{"FullName": "Ana"}]
        assert fetch_rows('SELECT "FullName" FROM team_reach', "t-ben") == [{"FullName": "Ben"}]


@pytest.mark.asyncio
async def test_tool_sql_passthrough_hides_ids_and_returns_rows():
    load_session("s", {"people": _PEOPLE, "recognitions": _RECOGNITIONS})
    result = await run_warehouse_question(
        "SELECT person_id, full_name, job_title FROM people ORDER BY full_name LIMIT 3",
        "s",
    )
    assert result["row_count"] == 3
    assert "person_id" not in result["rows"][0]
    assert "full_name" in result["rows"][0]
    assert "FROM people" in result["sql"]
    drop_session("s")
