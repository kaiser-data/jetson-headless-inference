"""Streaming sentence boundary detector for LLM token streams."""

import re

ABBREVS = frozenset({
    # English
    'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'vs', 'etc', 'no',
    'fig', 'approx', 'inc', 'ltd', 'corp', 'dept', 'est', 'govt', 'rep',
    # German
    'bzw', 'ca', 'usw', 'ggf', 'inkl', 'exkl', 'max', 'min', 'nr',
    'str', 'evtl', 'bsp', 'bzgl', 'hrsg', 'd.h', 'z.b', 'u.a', 'o.g',
})

_BOUNDARY = re.compile(r'([.!?])\s+')


class StreamingSentenceSplitter:
    """Accumulate streaming LLM tokens and yield complete sentences.

    Feed tokens one by one; call flush() when the stream ends to
    retrieve any trailing fragment.
    """

    def __init__(self, min_chars: int = 12):
        self.buffer = ""
        self.min_chars = min_chars

    def feed(self, token: str) -> list[str]:
        """Add a token and return any newly complete sentences."""
        self.buffer += token
        sentences = []
        search_from = 0  # advance past skip-worthy boundaries without losing text
        while True:
            m = _BOUNDARY.search(self.buffer, search_from)
            if not m:
                break
            candidate = self.buffer[: m.end()].strip()

            # Too short — skip past this boundary but keep the text
            if len(candidate) < self.min_chars:
                search_from = m.end()
                continue

            # Decimal number: digit immediately before '.'
            if m.group(1) == "." and re.search(r"\d\.$", candidate):
                search_from = m.end()
                continue

            # Known abbreviation: last alphabetic token before punctuation
            pre = candidate[:-1]
            last_word = re.search(r"([A-Za-zäöüÄÖÜß]+)$", pre)
            if last_word and last_word.group(1).lower() in ABBREVS:
                search_from = m.end()
                continue

            sentences.append(candidate)
            self.buffer = self.buffer[m.end():]
            search_from = 0  # reset for next search within remaining buffer

        return sentences

    def flush(self) -> str | None:
        """Return any buffered text left after the stream ends."""
        text = self.buffer.strip()
        self.buffer = ""
        return text or None
