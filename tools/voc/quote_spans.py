"""Map whitespace-only quote differences back to an exact original span."""
import re


def whitespace_quote_span(source, quote):
    """No spelling/punctuation edits, fuzzy matching, or cross-window search."""
    def collapse(text):
        chars, starts, ends = [], [], []
        for match in re.finditer(r"\s+|\S", text):
            value = match.group()
            chars.append(" " if value.isspace() else value)
            starts.append(match.start())
            ends.append(match.end())
        return "".join(chars), starts, ends

    normalized, starts, ends = collapse(source)
    needle, _, _ = collapse(quote)
    if not needle.strip():
        return None
    index = normalized.find(needle)
    if index < 0 or normalized.find(needle, index + 1) >= 0:
        return None  # Ambiguous occurrences need a better model quote.
    return starts[index], ends[index + len(needle) - 1]
