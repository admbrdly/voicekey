"""Text allowed across an automatic insertion boundary.

Keep the original transcript in the journal; prepare only the delivered copy.
Formatting is a destination permission, never something the model can request.
"""
import re


LINE_BREAKS = re.compile(r"\r\n|[\r\v\f\x85\u2028\u2029]")
WHITESPACE = re.compile(r"[ \t\n]+")
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class UnsafeText(ValueError):
    """Refuse the entire insertion before sending any text."""


def prepare(text: str, *, formatting: bool = False) -> str:
    """Normalize line breaks, flatten when restricted, and reject other controls."""
    text = LINE_BREAKS.sub("\n", text)
    control = CONTROLS.search(text)
    if control:
        raise UnsafeText(f"dictation contains control character U+{ord(control[0]):04X}")
    if formatting:
        return text
    # Collapse runs containing a break or tab, preserving ordinary spaces.
    return WHITESPACE.sub(
        lambda match: " " if "\t" in match[0] or "\n" in match[0] else match[0], text)
