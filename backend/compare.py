"""Comparing two transcripts of the same meeting: how much they agree, and where they differ, word by word."""
import re
from difflib import SequenceMatcher

_WORD = re.compile(r"[\w'’]+")
CHUNK = 60  # words of agreement kept around a difference, to keep the result readable


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def plain(segments: list[dict] | None, fallback: str = "") -> str:
    """A transcript's spoken words only: no timestamps, no speaker labels."""
    return " ".join(s["text"].strip() for s in segments) if segments else re.sub(r"^\[[\d:]+\]\s*([^:]{1,30}:\s)?", "", fallback, flags=re.M)


def speakers(segments: list[dict] | None) -> list[str]:
    return list(dict.fromkeys(s["speaker"] for s in segments or [] if s.get("speaker")))


def stats(segments: list[dict] | None, text: str) -> dict:
    """{words, lines, speakers} of a transcript."""
    lines = len(segments) if segments else (text.count("\n") + 1 if text else 0)
    return {"words": len(words(plain(segments, text))), "lines": lines, "speakers": speakers(segments)}


def diff(a_text: str, b_text: str) -> dict:
    """Word-level comparison of two transcripts (their spoken words only).

    agreement: the share of words that match, out of the longer one's words (1.0: identical).
    changes: [{"op": "replace" | "delete" | "insert", "a": words only in A, "b": words only in B, "at": where in
    A's words}], the agreeing stretches left out."""
    a, b = words(a_text), words(b_text)
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    changes, matched = [], 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            matched += i2 - i1
            continue
        changes.append({"op": op, "a": " ".join(a[i1:i2]), "b": " ".join(b[j1:j2]), "at": i1})
    longest = max(len(a), len(b)) or 1
    return {"agreement": round(matched / longest, 4), "a_words": len(a), "b_words": len(b), "matched": matched,
            "changes": changes}


def inline(a_text: str, b_text: str) -> list[dict]:
    """The two transcripts as one stream for display: [{"op": "equal"|"delete"|"insert", "text"}], where `delete`
    is only in A and `insert` only in B. Long agreeing stretches are shortened to their ends ("…")."""
    a, b = a_text.split(), b_text.split()
    key = lambda toks: [" ".join(words(t)) for t in toks]  # compare without case and punctuation
    matcher = SequenceMatcher(None, key(a), key(b), autojunk=False)
    out: list[dict] = []
    ops = matcher.get_opcodes()
    for n, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            seg = b[j1:j2]
            if len(seg) > 2 * CHUNK and 0 < n < len(ops) - 1:
                out += [{"op": "equal", "text": " ".join(seg[:CHUNK])}, {"op": "skip", "text": f"… {len(seg) - 2 * CHUNK} matching words …"},
                        {"op": "equal", "text": " ".join(seg[-CHUNK:])}]
            else:
                out.append({"op": "equal", "text": " ".join(seg)})
            continue
        if i2 > i1:
            out.append({"op": "delete", "text": " ".join(a[i1:i2])})
        if j2 > j1:
            out.append({"op": "insert", "text": " ".join(b[j1:j2])})
    return out
