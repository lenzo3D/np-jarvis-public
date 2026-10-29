from ops.textfmt import plain


def test_strips_markdown_emphasis_headings_and_code():
    src = "## Plan\n**Quiz 2** is on *Friday* with `INV2101`.\n```\nx\n```\n---\nDone"
    assert plain(src) == "Plan\nQuiz 2 is on Friday with INV2101.\nx\nDone"


def test_bullets_are_normalised_and_links_flattened():
    assert plain("- one\n* two\n  - three\nSee [notes](https://x.sg/a)") == "• one\n• two\n  • three\nSee notes (https://x.sg/a)"


def test_emoji_removed_without_leaving_gaps():
    assert plain("✅ Logged in") == "Logged in"
    assert plain("Sir ⚠️ the quiz") == "Sir the quiz"
    assert plain("\U0001F9E0 Working") == "Working"


def test_no_trailing_space_is_left_behind_by_a_removed_emoji():
    assert plain("- Quiz 2 on Saturday \u2705\n\u26a0\ufe0f Heads up") == "\u2022 Quiz 2 on Saturday\nHeads up"


def test_plain_text_untouched():
    t = "Fri 30 Oct 23:29 · Investments: Tutorial 5 → done (2 * 3 = 6)"
    assert plain(t) == t
    assert plain("") == "" and plain(None) == ""
