"""Notes rewritten by a better model, sent as a Google Batch job: half the price, no waiting around.

submit_notes() builds each meeting's notes request exactly as the live path would (pipeline.notes_inputs and
llm_engine.single_request) and sends them as one job. check() (run every minute or so while a job is open) asks
Google how it's going, and when it's done puts each answer in its meeting: the old notes stay in the history. An answer
is skipped, not used, if the meeting's notes or transcript changed in the meantime (you rewrote them yourself), or the
answer didn't come back.
"""
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

import numpy as np

import audio_store as store
import cleanup
import database as db
import gemini_batch
import gemini_client
import llm_engine
import meeting_text
import mixdown
import pipeline
import pricing
import settings
import transcribe_gemini as tg

log = logging.getLogger("trailmix.batch")
EST_TOKENS_OUT = 4000   # a meeting's notes, including the model's thinking, for the cost guess before it's sent


class NotReady(Exception):
    pass


_checking = threading.Lock()  # the background loop and "Check now" must not apply the same finished job twice


def _fingerprint(m: dict) -> str:
    return hashlib.sha1(f"{m.get('summary') or ''}\x00{m.get('transcript') or ''}".encode()).hexdigest()


def prepare(meeting_ids: list[int], cfg: dict) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """(requests, items, skipped) for these meetings. skipped: [{id, why}] for those that can't go in a batch."""
    requests, items, skipped = [], {}, []
    for mid in meeting_ids:
        m = db.get_meeting(mid)
        if not m or not m.get("transcript"):
            skipped.append({"id": mid, "why": "no transcript"})
            continue
        inputs = pipeline.notes_inputs(m, cfg)
        built = llm_engine.single_request("gemini", cfg, inputs)
        if built is None:
            skipped.append({"id": mid, "why": "too long to write in one go"})
            continue
        key = f"m{mid}"
        requests.append({"key": key, "system": built[0], "prompt": built[1]})
        items[key] = {"meeting_id": mid, "fingerprint": _fingerprint(m)}
    return requests, items, skipped


def estimate(requests: list[dict], model: str) -> dict:
    tokens_in = sum(len(r["prompt"]) + len(r.get("system") or "") for r in requests) / 3.0
    cost = pricing.text_cost(model, int(tokens_in), EST_TOKENS_OUT * len(requests), batch=True)
    return {"meetings": len(requests), "tokens_in": int(tokens_in), "cost": cost,
            "standard_cost": pricing.text_cost(model, int(tokens_in), EST_TOKENS_OUT * len(requests)), "model": model,
            "as_of": pricing.AS_OF}


def submit_notes(meeting_ids: list[int], cfg: dict) -> dict:
    """Sends these meetings' notes to be rewritten at half price. {jobs, submitted, skipped, cost}."""
    if settings.locked(cfg):
        raise NotReady(f"{settings.LOCKDOWN}, so nothing can be sent to Google")
    if not gemini_client.ready(cfg):
        raise NotReady("Add a Google API key first (Settings → AI providers)")
    model = cfg["gemini_model"]
    requests, items, skipped = prepare(meeting_ids, cfg)
    jobs = []
    for group in gemini_batch.split(requests):
        est = estimate(group, model)["cost"]
        name = gemini_batch.create(model, group, gemini_client.credential(cfg), name="trailmix notes")
        jobs.append(db.add_batch_job(name, "notes", model, {r["key"]: items[r["key"]] for r in group}, est))
    return {"jobs": jobs, "submitted": len(requests), "skipped": skipped,
            "cost": estimate(requests, model)["cost"] if requests else 0}


def _apply_notes(job: dict, results: dict, cfg: dict) -> dict:
    """Puts a finished job's answers in their meetings. {applied, failed, skipped: {why: n}, cost}."""
    outcome: dict = {"applied": 0, "failed": 0, "skipped": {}, "cost": 0.0}

    def skip(why: str):
        outcome["skipped"][why] = outcome["skipped"].get(why, 0) + 1

    for key, item in json.loads(job["items_json"]).items():
        answer = results.get(key)
        usage = (answer or {}).get("usage") or {}
        spent = pricing.text_cost(job["model"], usage.get("promptTokenCount", 0),
                                  usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0), batch=True)
        if spent:
            outcome["cost"] += spent
            db.log_usage(item["meeting_id"], "notes (batch)", "gemini", job["model"], 0, spent)
        if not answer or answer["error"]:
            outcome["failed"] += 1
            continue
        m = db.get_meeting(item["meeting_id"])
        if not m:
            skip("the meeting was deleted")
            continue
        if _fingerprint(m) != item["fingerprint"]:
            skip("you changed its notes or transcript meanwhile")
            continue
        try:
            inputs = pipeline.notes_inputs(m, cfg)
            summary = llm_engine.finish_single(answer["text"], "gemini", {**cfg, "gemini_model": job["model"]}, inputs)
        except llm_engine.LLMError:
            outcome["failed"] += 1
            continue
        db.update_meeting(m["id"], why=f"notes rewritten by {job['model']} (batch job)", summary=summary,
                          summary_provider="gemini", summary_model=job["model"])
        db.replace_tasks(m["id"], meeting_text.action_items(summary))
        pipeline.reexport(m["id"])
        outcome["applied"] += 1
    outcome["cost"] = round(outcome["cost"], 4)
    return outcome


def check(cfg: dict | None = None) -> int:
    """Asks Google about every open job and uses the answers of those that have finished. Returns how many
    jobs changed. Quietly does nothing when there are none, or Lockdown is on."""
    cfg = cfg or settings.get_all()
    if not _checking.acquire(blocking=False):  # another check is already at it
        return 0
    try:
        return _check(cfg)
    finally:
        _checking.release()


def _check(cfg: dict) -> int:
    open_jobs = db.batch_jobs(open_only=True)
    if not open_jobs or settings.locked(cfg) or not gemini_client.ready(cfg):
        return 0
    changed = 0
    for job in open_jobs:
        try:
            now = gemini_batch.status(job["name"], gemini_client.credential(cfg))
        except gemini_batch.BatchError as e:
            log.warning("Batch job %s: %s", job["name"], e)
            continue
        if now["state"] != job["state"]:
            changed += 1
        if now["state"] == "succeeded" and not job["applied"]:
            outcome = (_apply_transcripts if job["kind"] == "transcripts" else _apply_notes)(job, now["results"] or {}, cfg)
            db.update_batch_job(job["id"], state="succeeded", applied=1, result_json=json.dumps(outcome),
                                finished_at=datetime.now(timezone.utc).isoformat())
        elif now["state"] in ("failed", "cancelled", "expired"):
            _delete_files(json.loads(job["items_json"]).values(), gemini_client.credential(cfg))
            db.update_batch_job(job["id"], state=now["state"], error=now.get("error"),
                                finished_at=datetime.now(timezone.utc).isoformat())
        elif now["state"] != job["state"]:
            db.update_batch_job(job["id"], state=now["state"])
    return changed


def cancel(job_id: int, cfg: dict) -> None:
    job = next((j for j in db.batch_jobs() if j["id"] == job_id), None)
    if not job or job["state"] not in ("pending", "running"):
        raise NotReady("That job isn't waiting any more")
    try:
        gemini_batch.cancel(job["name"], gemini_client.credential(cfg))
    except gemini_batch.BatchError as e:
        raise NotReady(str(e))
    _delete_files(json.loads(job["items_json"]).values(), gemini_client.credential(cfg))
    db.update_batch_job(job_id, state="cancelled", finished_at=datetime.now(timezone.utc).isoformat())


# ── Transcripts redone by a general model (economy), at half price ──────────

def submit_transcripts(meeting_ids: list[int], cfg: dict, model: str, apply: bool = False) -> dict:
    """Sends these meetings' audio to be transcribed again by `model` as a Batch job. The results are kept as new
    versions of each transcript, or used straight away with `apply`. The audio is uploaded to Google's file store and
    deleted when the answers are in. {jobs, submitted, skipped, cost}."""
    if settings.locked(cfg):
        raise NotReady(f"{settings.LOCKDOWN}, so recordings can't be sent to Google")
    if not gemini_client.ready(cfg):
        raise NotReady("Add a Google API key first (Settings → Transcription)")
    if tg.uses_transcribe_api(model):
        raise NotReady(f"{model} can't run as a batch job; the economy models (like gemini-3.5-flash-lite) can")
    key = gemini_client.credential(cfg)
    requests, items, skipped, minutes = [], {}, [], 0.0
    try:
        for mid in meeting_ids:
            m = db.get_meeting(mid)
            folder = store.track_dir(m) if m else None
            tracks = {}
            for name in store.TRACKS.values():
                audio = store.load_track(folder, name) if folder and folder.exists() else None
                if audio is not None and np.max(np.abs(audio), initial=0) > 0.01:
                    tracks[name] = audio
            if not m or not tracks:
                skipped.append({"id": mid, "why": "no audio kept"})
                continue
            both = "system" in tracks and "mic" in tracks
            audio = mixdown.mix(tracks["mic"], tracks["system"]) if both else next(iter(tracks.values()))
            chunks = tg.plan_chunks(audio)
            minutes += len(audio) / store.SAMPLE_RATE / 60
            for i, (a, b) in enumerate(chunks):
                file = tg.upload(tg.encode(audio[int(a * tg.SR):int(b * tg.SR)]), "audio/ogg", key, name=f"batch-m{mid}-c{i}")
                k = f"t{mid}c{i}"
                requests.append({"key": k, "body": tg.llm_request_body(file, "audio/ogg", local_language())})
                items[k] = {"meeting_id": mid, "chunk": i, "chunks": len(chunks), "start": a, "end": b, "file": file["name"],
                            "apply": apply, "both": both, "fingerprint": _fingerprint(m)}
    except BaseException:
        _delete_files(items.values(), key)  # nothing was sent: don't leave the audio at Google
        raise
    jobs = []
    est = pricing.cost(minutes, "gemini", model)
    est = None if est is None else round(est * pricing.BATCH_DISCOUNT, 4)
    groups = gemini_batch.split(requests)
    for n, group in enumerate(groups):
        try:
            name = gemini_batch.create(model, group, key, name="trailmix transcripts")
        except BaseException:  # the jobs already sent keep their audio; the rest never will
            _delete_files([items[r["key"]] for g in groups[n:] for r in g], key)
            raise
        jobs.append(db.add_batch_job(name, "transcripts", model, {r["key"]: items[r["key"]] for r in group}, est))
    return {"jobs": jobs, "submitted": len({it["meeting_id"] for it in items.values()}), "skipped": skipped, "cost": est or 0}


def _delete_files(items, api_key: str) -> None:
    """The audio uploaded for a transcripts job doesn't stay at Google any longer than it has to."""
    for item in items:
        if item.get("file"):
            tg.delete_file({"name": item["file"]}, api_key)


def local_language() -> str | None:
    import local_engine

    return local_engine.LANGUAGE


def _apply_transcripts(job: dict, results: dict, cfg: dict) -> dict:
    """Makes a version of each meeting's transcript from a finished job's answers (all of a meeting's chunks must have
    come back), and uses it if that was asked for and the transcript hasn't changed since. Then deletes the audio at Google."""
    outcome: dict = {"applied": 0, "failed": 0, "skipped": {}, "cost": 0.0}
    by_meeting: dict[int, list[tuple[str, dict]]] = {}
    for key, item in json.loads(job["items_json"]).items():
        by_meeting.setdefault(item["meeting_id"], []).append((key, item))
    for mid, parts in by_meeting.items():
        usage = {"in": 0, "out": 0}
        answers = [results.get(k) for k, _ in parts]
        for a in answers:
            u = (a or {}).get("usage") or {}
            usage["in"] += u.get("promptTokenCount", 0)
            usage["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        spent = pricing.text_cost(job["model"], usage["in"], usage["out"], batch=True)
        if spent:
            outcome["cost"] += spent
            db.log_usage(mid, "transcript (batch)", "gemini", job["model"], 0, spent)
        m = db.get_meeting(mid)
        if not m:
            outcome["skipped"]["the meeting was deleted"] = outcome["skipped"].get("the meeting was deleted", 0) + 1
            continue
        try:
            if any(not a or a["error"] for a in answers):
                raise tg.GeminiError("an answer didn't come back")
            ordered = sorted(parts, key=lambda p: p[1]["chunk"])
            per_chunk = [((it["start"], it["end"]), tg.parse_turns(results[k]["text"])) for k, it in ordered]
            segs = tg.name_speakers(tg.group_words(tg.stitch(per_chunk)))
        except tg.GeminiError:
            outcome["failed"] += 1
            continue
        first = parts[0][1]
        segs = cleanup.drop_hallucinations(segs)
        folder = store.track_dir(m)
        if first["both"] and folder and folder.exists():
            mic, system = store.load_track(folder, "mic"), store.load_track(folder, "system")
            if mic is not None and system is not None:
                segs = mixdown.name_you(segs, mic, system)
        segs = cleanup.strip_metrics(sorted(segs, key=lambda s: s["start"]))
        db.transcript_versions(mid)  # what's there now is recorded first
        version = db.add_transcript_version(
            mid, transcript="\n".join(pipeline._line(s) for s in segs), segments_json=json.dumps(segs), engine="gemini",
            model=job["model"], made_with=f"{job['model']}, redone later (batch)", source="upgrade",
            duration_sec=m["duration_sec"] or 0, has_system=first["both"],
            note="mic and call mixed into one recording (batch job)" if first["both"] else None)
        if first["apply"]:
            if _fingerprint(m) == first["fingerprint"]:
                pipeline.apply_version(mid, version)
                pipeline.reexport(mid)
            else:
                outcome["skipped"]["you changed it meanwhile (the new transcript is kept as a version)"] = 1 + outcome["skipped"].get(
                    "you changed it meanwhile (the new transcript is kept as a version)", 0)
        outcome["applied"] += 1
    _delete_files(json.loads(job["items_json"]).values(), gemini_client.credential(cfg))
    outcome["cost"] = round(outcome["cost"], 4)
    return outcome
