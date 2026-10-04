"""Turning a stored meeting into text: speaker names, flagged moments, action items."""
import json
import re

_ACTION_HEADING = re.compile(r"^#{1,4}\s*action items\b", re.I)
_HEADING = re.compile(r"^#{1,4}\s")
_BULLET = re.compile(r"^\s*[-*+]\s+(?:\[( |x|X)\]\s*)?(.+?)\s*$")


def fmt(t: float) -> str:
    s = int(t)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments(m: dict) -> list[dict]:
    return json.loads(m["segments_json"]) if m.get("segments_json") else []


def bookmarks(m: dict) -> list[dict]:
    return json.loads(m["bookmarks_json"]) if m.get("bookmarks_json") else []


def speaker_names(m: dict, cfg: dict) -> dict:
    """Label -> display name. 'You' defaults to your name from Settings."""
    names = {"You": cfg.get("your_name") or "You", "Them": "Them"}
    if m.get("speaker_names_json"):
        names.update({k: v for k, v in json.loads(m["speaker_names_json"]).items() if v})
    return names


def turns(segs: list[dict], gap: float = 2.0, longest: float = 90.0) -> list[dict]:
    """Consecutive lines by the same speaker joined into turns: {start, end, speaker, text}. A pause longer
    than `gap` seconds, or a turn reaching `longest` seconds, starts a new one."""
    out: list[dict] = []
    for s in segs:
        last = out[-1] if out else None
        if (last and last.get("speaker") == s.get("speaker") and s["start"] - last["end"] <= gap
                and s["end"] - last["start"] <= longest):
            last["text"] += " " + s["text"].strip()
            last["end"] = max(last["end"], s["end"])
        else:
            out.append({"start": s["start"], "end": s["end"], "speaker": s.get("speaker"), "text": s["text"].strip()})
    return out


def transcript_text(m: dict, cfg: dict) -> str:
    """The transcript as speaker turns with real names, as given to the LLM: one timestamp per turn rather
    than per line, which reads better and costs a third fewer tokens."""
    segs = segments(m)
    if not segs:
        return m.get("transcript") or ""
    names = speaker_names(m, cfg)
    return "\n".join(
        f"[{fmt(t['start'])}] {names.get(t['speaker'], t['speaker']) + ': ' if t.get('speaker') else ''}{t['text']}"
        for t in turns(segs)
    )


_STOP = set("""about after again also been before being could does doing from have having here into just like
more most other over said same should some such than that their them then there these they this those through
very want were what when where which while will with would your yours you""".split())


def excerpt(text: str, question: str, limit: int) -> str:
    """The text if it fits; otherwise the lines that share the most words with the question (plus the line
    either side of each), in their original order, up to `limit` characters. "…" marks skipped stretches."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    keys = {w for w in re.findall(r"[a-z0-9']+", question.lower()) if len(w) > 3 and w not in _STOP}
    score = [len(keys & set(re.findall(r"[a-z0-9']+", ln.lower()))) for ln in lines]
    chosen: set[int] = set()
    size = 0
    for i in sorted(range(len(lines)), key=lambda i: -score[i]):
        if score[i] == 0:
            break
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in chosen and size + len(lines[j]) + 1 <= limit:
                chosen.add(j)
                size += len(lines[j]) + 1
    if not chosen:  # nothing matched: the start of the meeting is the best guess
        return text[:limit]
    out, last = [], -1
    for i in sorted(chosen):
        if i != last + 1:
            out.append("…")
        out.append(lines[i])
        last = i
    return "\n".join(out)


def speakers_note(m: dict, cfg: dict) -> str:
    """Who the labels are, for the LLM."""
    segs = segments(m)
    if not any(s.get("speaker") for s in segs):
        return "The transcript has no speaker labels."
    names = speaker_names(m, cfg)
    you, them = names.get("You", "You"), names.get("Them", "Them")
    return (f'"{you}" is the person who recorded the meeting. "{them}" is everyone on the other end of the call '
            "(possibly several people). When people are named in the conversation, use their names in the notes.")


def moments(m: dict, cfg: dict) -> list[str]:
    """Each bookmark as '[mm:ss] what was being said then'."""
    segs = segments(m)
    names = speaker_names(m, cfg)
    out = []
    for b in bookmarks(m):
        t = b["t"]
        # The line being spoken when you pressed the button, else the one just before it.
        seg = next((s for s in segs if s["start"] - 0.5 <= t <= s["end"] + 0.5), None)
        if seg is None:
            before = [s for s in segs if s["start"] <= t]
            seg = before[-1] if before else (segs[0] if segs else None)
        said = f" {names.get(seg['speaker'], '') + ': ' if seg and seg.get('speaker') else ''}{seg['text']}" if seg else ""
        note = f" (note: {b['note']})" if b.get("note") else ""
        out.append(f"[{fmt(t)}]{said}{note}")
    return out


def action_items(summary: str) -> list[str]:
    """Bullets under the '## Action Items' heading, minus a 'None' placeholder."""
    items, inside = [], False
    for line in summary.splitlines():
        if _ACTION_HEADING.match(line):
            inside = True
            continue
        if inside and _HEADING.match(line):
            break
        if inside:
            mt = _BULLET.match(line)
            if mt and mt.group(2).strip(" .").lower() not in ("none", "n/a", "no action items"):
                items.append(mt.group(2))
    return items


def summary_with_tasks(summary: str, tasks: list[dict]) -> str:
    """The summary with each action item's checkbox reflecting whether you've ticked it off."""
    lines, inside, i = summary.splitlines(), False, 0
    out = []
    for line in lines:
        if _ACTION_HEADING.match(line):
            inside = True
        elif inside and _HEADING.match(line):
            inside = False
        mt = _BULLET.match(line) if inside else None
        if mt and i < len(tasks) and mt.group(2).strip(" .").lower() not in ("none", "n/a"):
            line = f"- [{'x' if tasks[i]['done'] else ' '}] {mt.group(2)}"
            i += 1
        out.append(line)
    return "\n".join(out)
