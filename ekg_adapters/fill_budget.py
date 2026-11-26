"""How much text one model call may carry, and how a longer text is cut into consecutive windows.

There are no fixed character caps in the LLM stage. Everything is sized from the model's real context
window by one function, `sizes()`:

    window          what the model can hold: prompt + thinking + answer (llm.window_tokens() asks
                    query-master's context_window for it; FALLBACK_WINDOW_TOKENS when nobody knows)
    prompt share    the part of the window a prompt may fill: query-master's own compact_at() (0.60).
                    The rest is the reply's room. A reasoning model thinks before it answers and the
                    thinking counts as output, so the reply keeps 40% of the window and no call
                    carries a max_tokens of its own.
    chars per token query-master's DEFAULT_CHARS_PER_TOKEN (3.0): low on purpose, so a prompt is
                    over-counted rather than refused.

    reply limit     the provider's own hard cap on one reply (OpenRouter's max_completion_tokens), when
                    the backend says: the reply keeps min(window - prompt, that cap). Unknown -> no cap.

    131,072-token window -> 78,643 prompt tokens (235,929 characters), 52,429 tokens for the reply;
    with a 16,384-token reply limit the reply keeps 16,384.

How much TEXT one call may read depends on what the call answers:

  - a judge call (choose a candidate, map a table's columns) answers in a few lines whatever it reads, so its
    text may fill the prompt share;
  - an extraction call (doc_facts, knowledge, doc_knowledge) answers with something for every fact it reads,
    each with a quote copied from the text, and a reasoning model thinks about all of it first. Its thinking
    and its answer both grow with the text, and both must fit the reply. So the text one such call reads is
    bounded by the reply too:  text tokens <= reply_tokens * READ_SHARE_OF_REPLY. With READ_SHARE_OF_REPLY
    = 1/8 the reply keeps eight times the text's size for thinking plus answer (Budget.read_chars).

    131,072-token window, 16,384-token reply limit -> 6,144 characters of text per extraction call.
    (The first real run read 50,000-character documents in one call under a 16,384-token cap and the answers
    stopped after the first pages. With a share of 1/2, 24,576 characters, ACME v16's 18,000-20,000-character
    windows were answered for a few of their passages only: every call stopped at 24-45 records, about 11,000-16,000
    reply tokens with the thinking, and 115 of 227 paragraphs of the longest document were still unread after the
    follow-up readings, while its 5,500-character documents were read whole. A document's answer holds about one
    record per 70 characters of dense text, each with its quote, so a window has to be what one reply can answer.)

A long document therefore becomes many short calls, which run in parallel. A text longer than the room left after the instructions is read in consecutive windows, cut only between
units (a document's pieces, a conversation's messages). `pack()` never cuts inside a unit; a single unit
larger than a whole window is the one exception, and `split_text()` cuts it at line boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass

FALLBACK_WINDOW_TOKENS = 32_768   # used only when neither the backend nor a setting says how large the window is
PROMPT_SHARE = 0.60               # query-master's compact_at() default; llm.bind() replaces it with the live value
CHARS_PER_TOKEN = 3.0             # query-master's DEFAULT_CHARS_PER_TOKEN; llm.bind() replaces it with the live value
TYPICAL_CHARS_PER_TOKEN = 4.0     # for cost estimates only (English prose); sizing always uses CHARS_PER_TOKEN
READ_SHARE_OF_REPLY = 0.125       # an extraction call reads at most this share of its reply room in text (see above)
SLOW_TOKENS_PER_SECOND = 40      # Glimmer on OpenRouter wrote at least ~57 tokens/s (ACME v5: 14 mappings, up to 11,000
                                  # tokens each, all done within 3.5 minutes at 16 in flight); 40 leaves room. Slower when
                                  # this run's calls were slower (fill.slow_pace: ACME v8, three runs at once)
MAPPING_SECONDS_MAX = 1_200       # however slow the provider is, a mapping step is given up after 20 minutes
INSTRUCTION_CHARS = 10_000        # the rules, the reply format and a document's outline, besides the schema and the text


@dataclass(frozen=True)
class Budget:
    window_tokens: int
    prompt_tokens: int      # what a prompt may take
    reply_tokens: int       # what is left for thinking and the answer (capped by the provider's reply limit)
    prompt_chars: int       # prompt_tokens in characters
    source: str = "fallback"
    chars_per_token: float = CHARS_PER_TOKEN
    reply_limit: int = 0            # the provider's cap on one reply, 0 when unknown
    reply_limit_from: str = ""

    def room(self, fixed_chars: int = 0) -> int:
        """Characters of text a prompt can carry next to `fixed_chars` of instructions and schema."""
        return max(self.prompt_chars - int(fixed_chars), self.prompt_chars // 4)

    @property
    def read_chars(self) -> int:
        """Characters of text one extraction call may read: thinking and answer grow with it and must fit the reply."""
        return int(self.reply_tokens * self.chars_per_token * READ_SHARE_OF_REPLY)

    @property
    def schema_chars(self) -> int:
        """Characters the schema section of a document or table prompt may take, the registry's descriptions
        included: what a prompt has left after the most text an extraction call reads and the instructions."""
        return max(self.prompt_chars - self.read_chars - INSTRUCTION_CHARS, self.prompt_chars // 4)

    @property
    def mapping_reply_tokens(self) -> int:
        """A table mapping names columns, never rows: its reply (thinking included) is small whatever the table's
        size, but a reasoning model thinks first: on the first real run with the registry's descriptions, mappings
        that thought for up to 11,000 of the 16,384 tokens a reply may take were sound. One longer than three
        quarters of the reply is a mapping gone round in circles (fill.run reads that table as text)."""
        return max(self.reply_tokens * 3 // 4, 1024)

    @property
    def mapping_seconds(self) -> int:
        """How long a mapping call may run: mapping_reply_tokens at a slow pace (SLOW_TOKENS_PER_SECOND). A reply
        cannot be capped per call, so this is the only bound on one that goes round in circles."""
        return max(int(self.mapping_reply_tokens / SLOW_TOKENS_PER_SECOND), 60)

    def describe(self) -> dict:
        return {"window_tokens": self.window_tokens, "prompt_tokens": self.prompt_tokens, "reply_tokens": self.reply_tokens,
                "prompt_chars": self.prompt_chars, "window_from": self.source,
                "reply_limit_tokens": self.reply_limit or None, "reply_limit_from": self.reply_limit_from or "unknown",
                "extraction_text_chars_per_call": self.read_chars, "schema_chars_per_prompt": self.schema_chars,
                "mapping_reply_tokens_max": self.mapping_reply_tokens,
                "mapping_seconds_max": self.mapping_seconds}


def sizes(window_tokens: int | None = None, source: str = "", prompt_share: float | None = None,
          chars_per_token: float | None = None, max_reply_tokens: int | None = None, reply_source: str = "") -> Budget:
    """THE sizing function: a window in tokens (and the provider's reply limit, when known) -> what a prompt may
    carry and what the reply keeps."""
    known = bool(window_tokens and window_tokens > 0)
    window = int(window_tokens) if known else FALLBACK_WINDOW_TOKENS
    cpt = chars_per_token or CHARS_PER_TOKEN
    prompt = int(window * (prompt_share or PROMPT_SHARE))
    cap = int(max_reply_tokens) if max_reply_tokens and max_reply_tokens > 0 else 0
    reply = min(window - prompt, cap) if cap else window - prompt
    return Budget(window, prompt, reply, int(prompt * cpt),
                  (source or "given") if known else f"fallback ({FALLBACK_WINDOW_TOKENS:,} tokens: the window is unknown)",
                  cpt, cap, (reply_source or "given") if cap else "")


def tokens(chars: int, typical: bool = False) -> int:
    return int(chars / (TYPICAL_CHARS_PER_TOKEN if typical else CHARS_PER_TOKEN)) + 1


def pack(units, room: int, size=len):
    """Consecutive windows of whole units, each window at most `room` (a unit larger than `room` sits alone:
    cut it first with split_text). Order is kept and every unit lands in exactly one window."""
    windows, cur, used = [], [], 0
    for u in units:
        n = size(u)
        if cur and used + n > room:
            windows.append(cur)
            cur, used = [], 0
        cur.append(u)
        used += n
    if cur:
        windows.append(cur)
    return windows


def split_text(text: str, room: int, repeat: str = ""):
    """A text larger than `room` -> parts under it, cut at line ends (at a space only when one line alone is
    too long). `repeat` (a table's header row) starts every part after the first. Nothing is dropped."""
    if len(text) <= room:
        return [text]
    room = max(room - len(repeat) - 1, 200)
    parts, cur, used = [], [], 0

    def flush():
        nonlocal cur, used
        if cur:
            parts.append("\n".join(cur))
            cur, used = [], 0

    for line in text.split("\n"):
        while len(line) > room:      # one line longer than a window: cut at the last space that fits
            flush()
            cut = line.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        if used + len(line) + 1 > room:
            flush()
        cur.append(line)
        used += len(line) + 1
    flush()
    return [p if i == 0 or not repeat else f"{repeat}\n{p}" for i, p in enumerate(parts)]
