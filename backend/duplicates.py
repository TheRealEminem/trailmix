"""The same meeting twice: recorded by Trailmix while Granola was also taking notes, then imported from Granola (or
pasted in). They're found by their words, and merged into one meeting.

Finding them: two transcripts of the same talk share most of their four-word phrases, whoever transcribed it
(47-81% on real pairs), while different meetings share almost none (under 5%, even the same people a week apart).
Only a meeting without audio (an imported transcript) is matched, against any other meeting.

Merging keeps the meeting that has the recording (its audio, flags, your notes, ticked tasks, workspace, speaker names)
and takes the better transcript and notes: an import from Granola beats a pasted or file transcript, which beats
Trailmix's own. Granola's transcript is shifted to line up with the recording, so a line still plays its moment, and
the transcript it replaces stays in the meeting's versions. The other meeting is folded in, with everything needed to
undo the merge kept in merged_json.
"""
import json
import logging
import re
import statistics
import threading
from collections import defaultdict
from datetime import datetime, UTC

import database as db
import meeting_text

log = logging.getLogger("trailmix.duplicates")

N = 4                 # words per phrase
MATCH = 0.25          # share of the shorter transcript's phrases also in the other: the same meeting
MIN_PHRASES = 150     # too short to tell (a few lines in common could be chance)
BUSY = ("recording", "transcribing", "summarizing", "waiting_confirm")
_WORD = re.compile(r"[a-z0-9']+")
_PREFIX = re.compile(r"^\[[\d:]+\]\s*(?:[^:\n]{1,40}:\s+)?", re.M)
_lock = threading.Lock()  # one merge at a time
_prints: dict[int, tuple[int, frozenset]] = {}


def phrases(transcript: str) -> frozenset:
    words = _WORD.findall(_PREFIX.sub("", transcript).lower())
    return frozenset(tuple(words[i:i + N]) for i in range(len(words) - N + 1))


def _print(m: dict) -> frozenset:
    key = hash(m["transcript"])
    cached = _prints.get(m["id"])
    if not cached or cached[0] != key:
        cached = _prints[m["id"]] = (key, phrases(m["transcript"]))
    return cached[1]


def score(a: frozenset, b: frozenset) -> float:
    if min(len(a), len(b)) < MIN_PHRASES:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def has_audio(m: dict) -> bool:
    return bool(m.get("audio_dir")) and not m.get("audio_deleted")


def _rank(m: dict) -> tuple:
    """Whose transcript and notes win: Granola's, then another import's, then Trailmix's own; with notes before without."""
    return ({"granola": 2}.get(m.get("source") or "", 1 if m.get("source") else 0), bool(m.get("summary")), -m["id"])


def _label(m: dict) -> str:
    return {"granola": "Granola", "paste": "a pasted transcript", "file": "a transcript file"}.get(
        m.get("source") or "", "Trailmix")


def _candidates() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM meetings WHERE transcript IS NOT NULL AND transcript != ''").fetchall()
    return [dict(r) for r in rows]


def _apart(a: dict, b: dict) -> bool:
    """You said these two aren't the same meeting."""
    return (b["id"] in json.loads(a.get("not_duplicate_json") or "[]")
            or a["id"] in json.loads(b.get("not_duplicate_json") or "[]"))


def matches_for(meeting_id: int, pool: list[dict] | None = None) -> list[tuple[dict, float]]:
    """The meetings that look like the same meeting as this one, best first: [(meeting, score)]."""
    pool = pool if pool is not None else _candidates()
    m = next((x for x in pool if x["id"] == meeting_id), None) or db.get_meeting(meeting_id)
    if not m or not m.get("transcript"):
        return []
    mine = _print(m)
    out = []
    for other in pool:
        if other["id"] == m["id"] or (has_audio(m) and has_audio(other)) or _apart(m, other):
            continue
        s = score(mine, _print(other))
        if s >= MATCH:
            out.append((other, round(s, 3)))
    return sorted(out, key=lambda x: -x[1])


def find_all() -> list[dict]:
    """Every pair in the library: [{keep, other, score}] (keep: the meeting that stays)."""
    pool = _candidates()
    seen, pairs = set(), []
    for m in pool:
        if has_audio(m):
            continue  # pairs are found from the side without audio
        for other, s in matches_for(m["id"], pool):
            key = frozenset((m["id"], other["id"]))
            if key in seen:
                continue
            seen.add(key)
            keep, gone = _roles(m, other)
            pairs.append({"keep": _brief(keep), "other": _brief(gone), "score": s})
    return sorted(pairs, key=lambda p: p["keep"]["created_at"], reverse=True)


def _brief(m: dict) -> dict:
    return {"id": m["id"], "title": m["title"], "created_at": m["created_at"], "source": m.get("source") or "trailmix",
            "label": _label(m), "minutes": round((m.get("duration_sec") or 0) / 60), "has_audio": has_audio(m),
            "has_notes": bool(m.get("summary"))}


def _roles(a: dict, b: dict) -> tuple[dict, dict]:
    """(the meeting that stays, the one folded into it): the one with the recording stays; else the better one."""
    if has_audio(a) != has_audio(b):
        return (a, b) if has_audio(a) else (b, a)
    return (a, b) if _rank(a) >= _rank(b) else (b, a)


# ── Lining the transcripts up ───────────────────────────────────────────

def _timed(segments: list[dict]) -> dict[tuple, list[float]]:
    """phrase -> the times it's said at (estimated from where it falls in its line)."""
    out: dict[tuple, list[float]] = defaultdict(list)
    for s in segments:
        words = _WORD.findall((s.get("text") or "").lower())
        span = max(0.0, (s.get("end") or s["start"]) - s["start"])
        for i in range(len(words) - N + 1):
            out[tuple(words[i:i + N])].append(s["start"] + span * i / max(1, len(words)))
    return out


def offset(base: list[dict], moved: list[dict]) -> float:
    """Seconds to add to `moved`'s times so they line up with `base`'s: the median gap between phrases said once in
    each. 0 when too few phrases are shared to tell."""
    a, b = _timed(base), _timed(moved)
    gaps = [a[p][0] - b[p][0] for p in a.keys() & b.keys() if len(a[p]) == 1 and len(b[p]) == 1]
    return round(statistics.median(gaps), 1) if len(gaps) >= 5 else 0.0


def _shift(segments: list[dict], by: float) -> list[dict]:
    return [{**s, "start": round(max(0.0, s["start"] + by), 2), "end": round(max(0.0, (s.get("end") or s["start"]) + by), 2)}
            for s in segments]


# ── Merging ─────────────────────────────────────────────────────────────

def merge(a_id: int, b_id: int, score_: float | None = None) -> int:
    """Folds two copies of a meeting into one. Returns the id of the meeting that stays."""
    import pipeline

    with _lock:
        a, b = db.get_meeting(a_id), db.get_meeting(b_id)
        if not a or not b:
            raise ValueError("Meeting not found")
        if a["status"] in BUSY or b["status"] in BUSY:
            raise ValueError("One of these meetings is still being processed")
        keep, gone = _roles(a, b)
        best = max((keep, gone), key=_rank)
        db.transcript_versions(keep["id"])  # (records keep's transcript as its original, if it has none yet)
        before = next((v["id"] for v in db.transcript_versions(keep["id"]) if v["applied"]), None)
        segs = meeting_text.segments(gone)
        shift = offset(meeting_text.segments(keep), segs) if has_audio(keep) and segs else 0.0
        segs = _shift(segs, shift) if segs else segs
        why = f"merged with the copy from {_label(gone)}"
        version = db.add_transcript_version(
            keep["id"], transcript="\n".join(pipeline._line(s) for s in segs) if segs else gone["transcript"],
            segments_json=json.dumps(segs) if segs else gone.get("segments_json"), engine=gone.get("source") or "trailmix",
            model=None, made_with=gone.get("transcribed_with") or _label(gone), source=gone.get("source") or "recording",
            duration_sec=gone.get("duration_sec") or 0, has_system=bool(gone.get("has_system")),
            note=f"From the copy merged in ({_label(gone)})" + (f", moved {shift:+.0f} s to line up with the recording"
                                                                  if shift else ""))
        if best is gone:
            v = db.get_transcript_version(version)
            db.update_meeting(keep["id"], why=why, transcript=v["transcript"], segments_json=v["segments_json"],
                              transcribed=1, transcribed_with=v["made_with"])
            db.mark_applied(keep["id"], version)
        changes: dict = {}
        if gone.get("summary") and (best is gone or not keep.get("summary")):
            changes.update(summary=gone["summary"], summary_provider=gone.get("summary_provider"),
                           summary_model=gone.get("summary_model"), summary_error=None)
        if keep.get("title_auto") and gone["title"] and gone["title"] != "Imported meeting" and best is gone:
            changes.update(title=gone["title"], title_auto=int(bool(gone.get("title_auto"))))
        for field in ("bookmarks_json", "my_notes_json", "speaker_names_json"):  # flags, your notes, names: if keep has none
            if gone.get(field) and not keep.get(field):
                changes[field] = gone[field]
        if not keep.get("workspace_id") and gone.get("workspace_id"):
            changes.update(workspace_id=gone["workspace_id"], workspace_auto=gone.get("workspace_auto") or 0)
        qa = (json.loads(keep["qa_json"]) if keep.get("qa_json") else []) + (json.loads(gone["qa_json"]) if gone.get("qa_json") else [])
        if gone.get("qa_json"):
            changes["qa_json"] = qa[-50:]
        tags = list(dict.fromkeys((json.loads(keep["tags_json"]) if keep.get("tags_json") else [])
                                  + (json.loads(gone["tags_json"]) if gone.get("tags_json") else [])))
        if tags:
            changes["tags_json"] = tags
        record = {"meeting": {k: v for k, v in gone.items()}, "tasks": db.tasks_for(gone["id"], deleted=True),
                  "before": {"version": before, "summary": keep.get("summary"), "summary_provider": keep.get("summary_provider"),
                             "summary_model": keep.get("summary_model"), "title": keep["title"],
                             "title_auto": keep.get("title_auto"), "workspace_id": keep.get("workspace_id")},
                  "offset": shift, "score": score_, "at": datetime.now(UTC).isoformat()}
        merged = json.loads(keep["merged_json"]) if keep.get("merged_json") else []
        if keep.get("ask_first") and changes.get("summary"):
            changes["ask_first"] = 0  # it has notes now: nothing to choose
        if keep["status"] in ("ready_summarize", "error") and (changes.get("summary") or keep.get("summary")):
            changes["status"] = "done"
        db.update_meeting(keep["id"], why=why, merged_json=[*merged, record], duplicate_of=None, **changes)
        if "summary" in changes:
            db.replace_tasks(keep["id"], meeting_text.action_items(changes["summary"]))
        db.delete_meeting(gone["id"])
        _prints.pop(gone["id"], None)
    log.info("Merged meeting %s into %s (%s)", gone["id"], keep["id"], why)
    pipeline.reexport(keep["id"])
    return keep["id"]


def unmerge(meeting_id: int) -> int:
    """Undoes the last merge into this meeting: the other copy comes back as its own meeting, and this one gets back
    the transcript, notes and title it had. Returns the id of the meeting that came back."""
    import pipeline

    with _lock:
        m = db.get_meeting(meeting_id)
        merged = json.loads(m["merged_json"]) if m and m.get("merged_json") else []
        if not merged:
            raise ValueError("Nothing was merged into this meeting")
        record = merged.pop()
        old = record["meeting"]
        back = db.create_imported_meeting(old["title"], old["created_at"], old.get("source") or "trailmix",
                                          old.get("external_id"), old.get("uid"))
        fields = {k: old[k] for k in ("title_auto", "duration_sec", "transcript", "segments_json", "has_system",
                                      "transcribed", "summary", "summary_provider", "summary_model", "transcribed_with",
                                      "audio_dir", "audio_deleted", "workspace_id", "workspace_auto", "tags_json",
                                      "tags_model", "qa_json", "speaker_names_json", "bookmarks_json", "my_notes_json",
                                      "imported_at")
                  if old.get(k) is not None}
        db.update_meeting(back, why="split off again from the meeting it was merged into", status="done",
                          not_duplicate_json=[meeting_id], **fields)
        with db.connect() as conn:
            conn.executemany("INSERT INTO action_items (meeting_id, position, text, done, deleted, ticket_json) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             [(back, i, t["text"], int(t["done"]), int(t.get("deleted") or 0),
                               json.dumps(t["ticket"]) if t.get("ticket") else None)
                              for i, t in enumerate(record["tasks"])])
        before = record["before"]
        if before.get("version") and db.get_transcript_version(before["version"]):
            pipeline.apply_version(meeting_id, before["version"])
        notes = {"summary": before["summary"], "summary_provider": before["summary_provider"],
                 "summary_model": before["summary_model"]}
        apart = json.loads(m["not_duplicate_json"]) if m.get("not_duplicate_json") else []
        db.update_meeting(meeting_id, why="the merge was undone", merged_json=merged or None, title=before["title"],
                          title_auto=before.get("title_auto") or 0, workspace_id=before.get("workspace_id"),
                          not_duplicate_json=[*apart, back], **notes)
        db.replace_tasks(meeting_id, meeting_text.action_items(before["summary"]) if before["summary"] else [])
    pipeline.reexport(meeting_id)
    return back


def keep_apart(a_id: int, b_id: int) -> None:
    """You said these aren't the same meeting: never suggest or merge them again."""
    for x, y in ((a_id, b_id), (b_id, a_id)):
        m = db.get_meeting(x)
        if m:
            apart = json.loads(m["not_duplicate_json"]) if m.get("not_duplicate_json") else []
            db.update_meeting(x, not_duplicate_json=list(dict.fromkeys([*apart, y])),
                              duplicate_of=None if m.get("duplicate_of") == y else m.get("duplicate_of"))


def check(meeting_id: int, cfg: dict) -> int:
    """After an import or a new transcript: merges (or flags, if you'd rather be asked) a copy of the same meeting.
    Returns the id of the meeting to show (the one that stayed)."""
    mode = cfg.get("duplicates", "merge")
    if mode == "keep":
        return meeting_id
    try:
        found = matches_for(meeting_id)
        if not found:
            return meeting_id
        other, s = found[0]
        if mode == "ask":
            m = db.get_meeting(meeting_id)
            db.update_meeting(m["id"], duplicate_of=other["id"])
            db.update_meeting(other["id"], duplicate_of=m["id"])
            return meeting_id
        return merge(meeting_id, other["id"], s)
    except ValueError as e:  # one of them is busy: it's found again next time, or by "Find duplicates"
        log.info("Didn't merge meeting %s yet: %s", meeting_id, e)
        return meeting_id
    except Exception:
        log.exception("Looking for a duplicate of meeting %s failed", meeting_id)
        return meeting_id
