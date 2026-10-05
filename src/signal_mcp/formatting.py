"""Lightweight markdown -> Signal text-style range conversion.

Signal (via signal-cli's `textStyle` send param) supports rich text as
"start:length:STYLE" ranges over the message body, where start/length are
counted in UTF-16 code units (not Python codepoints — matters for text
containing emoji or other characters outside the Basic Multilingual Plane).

This module lets callers write **bold**, *italic* (or _italic_), ~~strikethrough~~,
`monospace` and ||spoiler|| inline in note text; parse_styled_text() strips the
markers and returns the plain text plus the style ranges to pass as the
`textStyle` RPC param.
"""

import re

# Group order must match _STYLES. Italic markers refuse to match inside words or around
# whitespace, so "snake_case_name", "2 * 3 * 4" and "* bullet" stay literal text.
_MARKER_RE = re.compile(
    r"\*\*(.+?)\*\*"
    r"|~~(.+?)~~"
    r"|`(.+?)`"
    r"|\|\|(.+?)\|\|"
    r"|(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])"
    r"|(?<!\w)_(?![\s_])(.+?)(?<![\s_])_(?!\w)",
    re.DOTALL,
)
_STYLES = ("BOLD", "STRIKETHROUGH", "MONOSPACE", "SPOILER", "ITALIC", "ITALIC")


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def parse_styled_text(text: str) -> tuple[str, list[str]]:
    """Strip **bold**/*italic*/~~strike~~/`mono`/||spoiler|| markers, returning (plain_text, textStyle ranges).

    Markers don't nest; the first-matching marker wins for any given span.
    """
    plain, ranges, _ = parse_styled_text_mapped(text)
    return plain, ranges


def parse_styled_text_mapped(text: str):
    """Like parse_styled_text, plus a function mapping an offset in *text* (UTF-16 units,
    markers included) to the matching offset in the returned plain text.

    Needed when other ranges (e.g. @mentions) were computed against the text as written.
    """
    out: list[str] = []
    ranges: list[str] = []
    cuts: list[tuple[int, int]] = []  # (UTF-16 offset in the original, units removed there)
    cursor = 0
    orig_pos = 0  # UTF-16 length of text[:cursor]
    out_len = 0  # running UTF-16 length of the plain-text output built so far

    for m in _MARKER_RE.finditer(text):
        literal = text[cursor : m.start()]
        out.append(literal)
        literal_len = _utf16_len(literal)
        out_len += literal_len
        orig_pos += literal_len

        style_idx = next(i for i, g in enumerate(m.groups()) if g is not None)
        inner = m.groups()[style_idx]
        out.append(inner)
        inner_len = _utf16_len(inner)
        ranges.append(f"{out_len}:{inner_len}:{_STYLES[style_idx]}")

        open_len = m.start(style_idx + 1) - m.start()  # markers are ASCII: chars == UTF-16 units
        close_len = m.end() - m.end(style_idx + 1)
        cuts.append((orig_pos, open_len))
        cuts.append((orig_pos + open_len + inner_len, close_len))
        orig_pos += open_len + inner_len + close_len

        out_len += inner_len
        cursor = m.end()

    out.append(text[cursor:])

    def remap(offset: int) -> int:
        # An offset inside a removed marker collapses onto the marker's boundary, so the
        # result is never negative and never decreases as the offset grows.
        return offset - sum(min(count, offset - pos) for pos, count in cuts if offset > pos)

    return "".join(out), ranges, remap
