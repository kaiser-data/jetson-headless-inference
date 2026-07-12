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

    _CLAUSE = re.compile(r'[,;:—]\s+')

    def __init__(self, min_chars: int = 12, first_chunk_chars: int | None = None):
        self.buffer = ""
        self.min_chars = min_chars
        # Emit the FIRST chunk early at a clause boundary once the buffer
        # reaches this size — a long opening sentence otherwise delays first
        # audio by seconds. None disables.
        self.first_chunk_chars = first_chunk_chars
        self._yielded_any = False

    def _early_first_chunk(self) -> str | None:
        if (self._yielded_any or not self.first_chunk_chars
                or len(self.buffer) < self.first_chunk_chars):
            return None
        cuts = [m for m in self._CLAUSE.finditer(self.buffer)
                if m.start() >= self.min_chars]
        if not cuts:
            return None
        cut = cuts[-1]
        chunk = self.buffer[: cut.end()].strip()
        self.buffer = self.buffer[cut.end():]
        return chunk

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

        if sentences:
            self._yielded_any = True
        elif (early := self._early_first_chunk()) is not None:
            sentences.append(early)
            self._yielded_any = True
        return sentences

    def flush(self) -> str | None:
        """Return any buffered text left after the stream ends."""
        text = self.buffer.strip()
        self.buffer = ""
        return text or None
