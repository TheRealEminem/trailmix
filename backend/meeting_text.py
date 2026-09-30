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


def transcript_text(m: dict, cfg: dict) -> str:
    """The transcript with speakers' real names, as given to the LLM and written by the exporter."""
    segs = segments(m)
    if not segs:
        return m.get("transcript") or ""
    names = speaker_names(m, cfg)
    return "\n".join(
        f"[{fmt(s['start'])}] {names.get(s['speaker'], s['speaker']) + ': ' if s.get('speaker') else ''}{s['text']}"
        for s in segs
    )


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
