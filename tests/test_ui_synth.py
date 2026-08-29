"""Tests for the UI synthesizer's deterministic guards.

The LLM call itself is not tested here. These cover the regex and heuristic
guards around it, which is where the demo-visible failures live.
"""

from src.agent.ui_synth import (
    _RENDER_OFFER_RE,
    _named_chart_type,
    _only_offers,
)


def suppressed(reply: str, render: str = "chart") -> bool:
    """Whether the offer guard would veto the widget for this reply."""
    offer_re = _RENDER_OFFER_RE.get(render)
    if not offer_re or not offer_re.search(reply):
        return False
    return _only_offers(reply, render, offer_re)


class TestOfferGuard:
    def test_pure_offer_is_suppressed(self):
        # Nothing has been presented yet, so there is nothing to plot.
        assert suppressed("I can pull that together. Would you like me to chart the engagement index?")

    def test_offer_with_no_numbers_is_suppressed(self):
        assert suppressed(
            "I have the reach data for your team. Shall I chart it for you?"
        )

    def test_presented_chart_survives_a_follow_up_offer_of_the_same_type(self):
        # The system prompt asks for a useful follow-up, so this shape is common.
        # Keying only on the offer sentence used to delete the chart.
        assert not suppressed(
            "Ana moved most, from 41% to 62%. Ben rose from 50% to 55%. "
            "Here is the engagement index per person.\n\n"
            "Would you like me to chart award reasons as well?"
        )

    def test_presented_chart_survives_an_offer_of_a_different_type(self):
        assert not suppressed(
            "Ana moved most, from 41% to 62%.\n\nWould you like me to list their award reasons?"
        )

    def test_no_offer_never_suppresses(self):
        assert not suppressed("Ana moved most, from 41% to 62%.")

    def test_list_offer_with_no_content_is_suppressed(self):
        assert suppressed("Do you want me to list them?", render="list")

    def test_list_survives_when_content_precedes_the_offer(self):
        assert not suppressed(
            "Your team used twelve distinct award reasons last quarter, led by "
            "Teamwork and Customer Focus.\n\nDo you want me to list them all?",
            render="list",
        )


class TestNamedChartType:
    def test_explicit_noun_is_honoured(self):
        assert _named_chart_type("show it as a donut chart") == "donut"

    def test_doughnut_is_normalised(self):
        assert _named_chart_type("a doughnut chart would be clearer") == "donut"

    def test_noun_may_be_dropped(self):
        assert _named_chart_type("plot it as a pie instead") == "pie"

    def test_last_match_wins(self):
        # The concluding sentence declares what is actually being rendered.
        assert _named_chart_type("a bar chart works, but a donut chart is clearer") == "donut"

    def test_table_prose_does_not_override(self):
        # "column" and "line" are ordinary words; the synth saw the real data and
        # its inference must not lose to an incidental mention.
        assert _named_chart_type("shown in a column format, ranked by engagement") is None
        assert _named_chart_type("each line of the table shows one member") is None
        assert _named_chart_type("the first column is the name") is None

    def test_no_mention_returns_none(self):
        assert _named_chart_type("Ana moved most, from 41% to 62%.") is None
        assert _named_chart_type("plot it") is None

    def test_explicit_noun_beats_incidental_word(self):
        assert (
            _named_chart_type("the column headings are names; show it as a donut chart")
            == "donut"
        )
