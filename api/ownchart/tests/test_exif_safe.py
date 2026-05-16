"""EXIF `_safe()` coercion tests.

Regression coverage for the 2026-05-16 AM photo-upload P0:
`asyncpg.exceptions.UntranslatableCharacterError: \\u0000 cannot be
converted to text.` Pure-function test, no DB, no LLM.
"""

from ownchart.ingest.images import _safe


def test_strips_nul_from_iphone_screenshot_usercomment():
    """The real-world payload: iPhone PNG screenshot EXIF
    UserComment = "ASCII\\x00\\x00\\x00Screenshot" — 8-byte charset
    header with embedded NULs that crash Postgres JSONB insert."""
    raw = "ASCII\x00\x00\x00Screenshot"
    cleaned = _safe(raw)
    assert "\x00" not in cleaned
    assert cleaned == "ASCIIScreenshot"


def test_strips_nul_from_bytes_value():
    """bytes path: same fix on the decode side."""
    raw = b"ASCII\x00\x00\x00Screenshot"
    cleaned = _safe(raw)
    assert isinstance(cleaned, str)
    assert "\x00" not in cleaned


def test_strips_nul_from_nested_dict_values():
    """EXIF GPSInfo and similar arrive as nested dicts; the recursive
    walk must hit every leaf string."""
    raw = {
        "UserComment": "ASCII\x00\x00\x00Screenshot",
        "GPSInfo": {"GPSLatitudeRef": "N\x00"},
        "Other": ["clean", "dirty\x00", "ok"],
    }
    cleaned = _safe(raw)
    assert "\x00" not in cleaned["UserComment"]
    assert "\x00" not in cleaned["GPSInfo"]["GPSLatitudeRef"]
    assert cleaned["Other"] == ["clean", "dirty", "ok"]


def test_strips_nul_from_dict_keys():
    """Pillow has been observed to emit weird keys for unknown tags.
    Keys-with-NUL would explode the JSONB insert too."""
    raw = {"normal\x00key": "value"}
    cleaned = _safe(raw)
    assert "\x00" not in next(iter(cleaned))


def test_clean_string_returns_unchanged():
    """Fast path: no NUL → no replace allocation."""
    assert _safe("hello world") == "hello world"


def test_preserves_numerator_denominator_floats():
    """IFDRational handling must still work after the str/bytes
    fixes were added in front of it. Real IFDRational subclasses
    fractions.Fraction so float() succeeds — mock that behavior."""
    class _Rat:
        numerator = 1
        denominator = 2

        def __float__(self) -> float:
            return self.numerator / self.denominator
    assert _safe(_Rat()) == 0.5


def test_fallback_for_unfloatable_rational():
    """If a duck-typed numerator/denominator object can't be cast to
    float, fall back to the 'num/den' string form rather than raising."""
    class _BadRat:
        numerator = 1
        denominator = 0   # ZeroDivisionError inside float()

        def __float__(self) -> float:
            return self.numerator / self.denominator
    assert _safe(_BadRat()) == "1/0"
