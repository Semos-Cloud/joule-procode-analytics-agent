"""Tests for the UI contract.

The contract's job is to make LLM output safe to hand to a client, so most of
these are about *malformed* payloads degrading to something renderable rather
than reaching Joule half-built.
"""

import json

from src.ui_contract import (
    CONTRACT_VERSION,
    build_joule_manifest,
    validate_contract,
)


class TestFallbacks:
    def test_unknown_render_becomes_text(self):
        result = validate_contract("hologram", {"title": "hi"})
        assert result["render"] == "text"

    def test_version_is_always_present(self):
        assert validate_contract("text")["version"] == CONTRACT_VERSION

    def test_missing_required_field_falls_back(self):
        # "card" requires a title; without one there is nothing to draw.
        assert validate_contract("card", {"subtitle": "orphan"})["render"] == "text"

    def test_choice_without_actions_falls_back(self):
        assert validate_contract("choice", {}, actions=[])["render"] == "text"

    def test_list_without_items_falls_back(self):
        assert validate_contract("list", {}, items=[])["render"] == "text"

    def test_fallback_clears_the_payload(self):
        result = validate_contract("chart", {"title": "x"}, items=[{"a": "b"}], chart=None)
        assert result["render"] == "text"
        assert result["fields"] == {}
        assert result["items"] == []


class TestSanitisation:
    def test_non_scalar_fields_are_dropped(self):
        result = validate_contract("status", {"title": "Done", "rows": [1, 2, 3]})
        assert result["fields"] == {"title": "Done"}

    def test_items_are_dropped_for_renders_that_do_not_use_them(self):
        # A choice's options belong in `actions`; items here would render twice.
        result = validate_contract("choice", {}, items=[{"title": "ghost"}], actions=[{"title": "Real"}])
        assert result["items"] == []
        assert len(result["actions"]) == 1

    def test_bare_ordinal_actions_are_dropped(self):
        result = validate_contract("choice", {}, actions=[{"title": "1"}, {"title": "2"}])
        # Numbered buttons are worse than none, so the whole render degrades.
        assert result["render"] == "text"

    def test_mixed_actions_survive(self):
        result = validate_contract("choice", {}, actions=[{"title": "1"}, {"title": "Cancel"}])
        assert result["render"] == "choice"

    def test_actions_duplicating_items_are_removed(self):
        result = validate_contract(
            "list",
            {},
            items=[{"title": "Ana", "value": "a"}, {"title": "Ben", "value": "b"}],
            actions=[{"title": "Ana", "value": "a"}, {"title": "Cancel", "value": "cancel"}],
        )
        assert [a["title"] for a in result["actions"]] == ["Cancel"]


class TestChart:
    def test_valid_chart_survives(self):
        chart = {
            "chart_type": "donut",
            "title": "Engagement",
            "dimensions": ["name"],
            "measures": ["engagement_index"],
            "data": [{"name": "Ana", "engagement_index": 62}],
        }
        result = validate_contract("chart", {}, chart=chart)
        assert result["render"] == "chart"
        assert result["chart"]["chart_type"] == "donut"
        assert result["chart"]["data"] == [{"name": "Ana", "engagement_index": 62}]

    def test_empty_data_falls_back(self):
        chart = {"dimensions": ["name"], "measures": ["v"], "data": []}
        assert validate_contract("chart", {}, chart=chart)["render"] == "text"

    def test_axes_are_recovered_when_omitted(self):
        # Good data with missing metadata is worth saving rather than dropping.
        chart = {"data": [{"name": "Ana", "engagement_index": 62, "sent": 4}]}
        result = validate_contract("chart", {}, chart=chart)
        assert result["render"] == "chart"
        assert result["chart"]["dimensions"] == ["name"]
        assert set(result["chart"]["measures"]) == {"engagement_index", "sent"}

    def test_chart_without_any_numbers_falls_back(self):
        chart = {"data": [{"name": "Ana", "role": "Engineer"}]}
        assert validate_contract("chart", {}, chart=chart)["render"] == "text"

    def test_chart_is_ignored_for_other_renders(self):
        result = validate_contract("status", {"title": "Done"}, chart={"data": [{"a": 1}]})
        assert "chart" not in result


class TestJouleManifest:
    def test_chart_has_no_manifest(self):
        # The chart is built in the capability YAML, not pre-baked.
        assert build_joule_manifest("chart") is None

    def test_native_types_have_no_manifest(self):
        for render in ("text", "confirm", "status"):
            assert build_joule_manifest(render) is None

    def test_card_manifest_shape(self):
        manifest = build_joule_manifest(
            "card",
            fields={"title": "Ana Petrova", "subtitle": "Engineer"},
            items=[{"label": "Engagement", "value": "62%"}],
        )
        assert manifest["type"] == "ui5integrationCard"
        card = manifest["content"]["sap.card"]
        assert card["type"] == "AdaptiveCard"
        body = card["content"]["body"]
        assert body[0]["text"] == "Ana Petrova"
        assert any(b["type"] == "FactSet" for b in body)

    def test_list_rows_are_selectable(self):
        manifest = build_joule_manifest(
            "list", items=[{"title": "Ana", "subtitle": "62%", "value": "ana"}]
        )
        row = manifest["content"]["sap.card"]["content"]["body"][0]
        assert row["selectAction"]["data"] == {"value": "ana"}

    def test_choice_becomes_submit_actions(self):
        manifest = build_joule_manifest(
            "choice", fields={"title": "Pick"}, actions=[{"title": "Yes", "value": "y"}]
        )
        actions = manifest["content"]["sap.card"]["content"]["actions"]
        assert actions[0]["type"] == "Action.Submit"
        assert actions[0]["data"] == {"value": "y"}

    def test_action_value_defaults_to_title(self):
        manifest = build_joule_manifest("choice", actions=[{"title": "Skip"}])
        assert manifest["content"]["sap.card"]["content"]["actions"][0]["data"] == {"value": "Skip"}

    def test_manifest_is_json_serialisable(self):
        # It is sent over the wire as a DataPart, so this is not academic.
        manifest = build_joule_manifest(
            "form",
            fields={"submit_label": "Send"},
            items=[{"name": "note", "label": "Note", "type": "textarea"}],
        )
        assert json.loads(json.dumps(manifest))["type"] == "ui5integrationCard"
