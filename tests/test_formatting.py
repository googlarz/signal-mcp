from signal_mcp.formatting import parse_styled_text


def test_no_markers_passthrough():
    assert parse_styled_text("plain text") == ("plain text", [])


def test_bold():
    assert parse_styled_text("**hi**") == ("hi", ["0:2:BOLD"])


def test_strikethrough():
    assert parse_styled_text("~~done~~") == ("done", ["0:4:STRIKETHROUGH"])


def test_monospace():
    assert parse_styled_text("`code`") == ("code", ["0:4:MONOSPACE"])


def test_multiple_markers_offsets():
    text, ranges = parse_styled_text("**Title**\nbody `x=1`")
    assert text == "Title\nbody x=1"
    assert ranges == ["0:5:BOLD", "11:3:MONOSPACE"]


def test_emoji_before_marker_uses_utf16_offset():
    # 🏀 is outside the BMP -> 2 UTF-16 code units, 1 Python codepoint.
    text, ranges = parse_styled_text("🏀 **warmup**")
    assert text == "🏀 warmup"
    # "🏀 " is 2 (surrogate pair) + 1 (space) = 3 UTF-16 units.
    assert ranges == ["3:6:BOLD"]


def test_unmatched_markers_left_literal():
    assert parse_styled_text("no **closing here") == ("no **closing here", [])


def test_italic_star_and_underscore():
    assert parse_styled_text("a *b* c") == ("a b c", ["2:1:ITALIC"])
    assert parse_styled_text("a _b_ c") == ("a b c", ["2:1:ITALIC"])


def test_spoiler():
    assert parse_styled_text("secret ||plot twist|| here") == ("secret plot twist here", ["7:10:SPOILER"])


def test_all_styles_together_with_emoji_utf16_offsets():
    text, ranges = parse_styled_text("🎉 **b** *i* ~~s~~ `m` ||x||")
    assert text == "🎉 b i s m x"
    # the emoji is 2 UTF-16 code units, so "b" starts at 3
    assert ranges == ["3:1:BOLD", "5:1:ITALIC", "7:1:STRIKETHROUGH", "9:1:MONOSPACE", "11:1:SPOILER"]


def test_bold_still_wins_over_italic_for_double_asterisks():
    assert parse_styled_text("**hi**") == ("hi", ["0:2:BOLD"])


def test_lookalikes_stay_plain_text():
    for plain in (
        "snake_case_name and my_var_2",
        "2 * 3 * 4 = 24",
        "* bullet one\n* bullet two",
        "https://example.com/a_b_c?x=1",
        "5*3 and 4*5",
        "price: 3 * 2",
        "a || b",
        "_leading and trailing_x",
    ):
        assert parse_styled_text(plain) == (plain, []), plain


def test_italic_does_not_swallow_across_a_code_span():
    text, ranges = parse_styled_text("use `a_b_c` here")
    assert text == "use a_b_c here"
    assert ranges == ["4:5:MONOSPACE"]
