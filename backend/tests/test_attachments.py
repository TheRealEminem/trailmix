import json
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from fpdf import FPDF
from PIL import Image

import attachments
import database as db
import exporter
import llm_engine
import main
import meeting_text
import resources
import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    # Read straight away (not on the background thread), with a pretend local model writing the digest.
    monkeypatch.setattr(attachments, "read_later", lambda att_id: attachments.read(att_id))
    monkeypatch.setattr(attachments, "_digest_chain", lambda cfg: ["ollama"])
    monkeypatch.setattr(llm_engine, "generate", lambda pid, cfg, prompt, **kw: "- What the deck covers")
    monkeypatch.setattr(llm_engine, "ollama_ram_needed_gb", lambda cfg: 1.0)
    monkeypatch.setattr(resources, "check", lambda gb, what: None)
    return tmp_path


async def _never():
    pass


@pytest.fixture
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_warm_loop", lambda: _never())
    return TestClient(main.app)


def noise_png(path: Path, size=(2000, 1500)) -> Path:
    Image.fromarray((np.random.default_rng(1).random((size[1], size[0], 3)) * 255).astype("uint8")).save(path)
    return path


def text_pdf(path: Path, picture: Path | None = None, pages: int = 2) -> Path:
    pdf = FPDF()
    for i in range(pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=16)
        pdf.cell(text=f"Pricing roadmap page {i + 1}")
        if picture:
            pdf.image(str(picture), x=10, y=30, w=180)
    pdf.output(str(path))
    return path


def pptx(path: Path, picture: bytes | None = None) -> Path:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    slide = lambda *lines: f'<p:sld {a} xmlns:p="p"><a:p>{"".join(f"<a:r><a:t>{t}</a:t></a:r>" for t in lines)}</a:p></p:sld>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/slides/slide10.xml", slide("Tenth: budget"))
        z.writestr("ppt/slides/slide2.xml", slide("Second: hiring"))
        z.writestr("ppt/slides/slide1.xml", slide("Q3 plan"))
        z.writestr("ppt/notesSlides/notesSlide1.xml", f'<p:notes {a} xmlns:p="p"><a:p><a:r><a:t>Say hello</a:t></a:r></a:p></p:notes>')
        if picture:
            z.writestr("ppt/media/image1.png", picture)
    return path


def meeting_with_tasks() -> int:
    mid = db.create_meeting("Planning", title_auto=False)
    summary = "## Overview\nWe planned.\n\n## Action Items\n- Priya: send the budget"
    db.update_meeting(mid, summary=summary, transcript="x", status="done")
    db.replace_tasks(mid, meeting_text.action_items(summary))
    return mid


# ── Adding and shrinking ──

def test_small_file_is_kept_and_big_one_is_offered_shrinking(client, fresh_db):
    mid = meeting_with_tasks()
    r = client.post(f"/api/meetings/{mid}/attachments?name=agenda.txt", content=b"Agenda: pricing").json()
    assert r["attachment"]["name"] == "agenda.txt" and r["attachment"]["searchable"] is False

    settings.update({"attachments_shrink_over_mb": 1})
    big = noise_png(fresh_db / "whiteboard.png").read_bytes()
    pending = client.post(f"/api/meetings/{mid}/attachments?name=whiteboard.png", content=big).json()["pending"]
    assert pending["size"] == len(big) and pending["estimate"] < pending["size"]
    att = client.post(f"/api/meetings/{mid}/attachments/pending/{pending['id']}",
                      json={"name": "whiteboard.png", "shrink": True}).json()
    assert att["name"] == "whiteboard.jpg" and att["original_size"] == len(big) and att["size"] < len(big)
    assert att["has_preview"]
    assert client.get(f"/api/attachments/{att['id']}/file").status_code == 200
    assert not list((db.DATA_DIR / "incoming").glob("*"))  # nothing left behind


def test_keeping_the_original(client, fresh_db):
    mid = meeting_with_tasks()
    settings.update({"attachments_shrink_over_mb": 1})
    big = noise_png(fresh_db / "board.png").read_bytes()
    pending = client.post(f"/api/meetings/{mid}/attachments?name=board.png", content=big).json()["pending"]
    att = client.post(f"/api/meetings/{mid}/attachments/pending/{pending['id']}",
                      json={"name": "board.png", "shrink": False}).json()
    assert att["size"] == len(big) and att["original_size"] is None and att["can_shrink"]
    shrunk = client.post(f"/api/attachments/{att['id']}/shrink").json()
    assert shrunk["size"] < len(big) and shrunk["original_size"] == len(big)


def test_heic_is_always_offered_and_becomes_a_jpeg(client, fresh_db):
    mid = meeting_with_tasks()
    buf = BytesIO()
    noise_png(fresh_db / "photo.png", (1600, 1200))
    Image.open(fresh_db / "photo.png").save(buf, "HEIF", quality=60)  # small: a HEIC is offered whatever its size
    pending = client.post(f"/api/meetings/{mid}/attachments?name=IMG_1.HEIC", content=buf.getvalue()).json()["pending"]
    att = client.post(f"/api/meetings/{mid}/attachments/pending/{pending['id']}",
                      json={"name": "IMG_1.HEIC", "shrink": True}).json()
    assert att["name"] == "IMG_1.jpg" and att["mime"] == "image/jpeg"


def test_always_shrink_skips_the_question(client, fresh_db):
    mid = meeting_with_tasks()
    settings.update({"attachments_shrink_over_mb": 1, "attachments_always_shrink": True})
    big = noise_png(fresh_db / "board.png").read_bytes()
    att = client.post(f"/api/meetings/{mid}/attachments?name=board.png", content=big).json()["attachment"]
    assert att["original_size"] == len(big) and att["size"] < len(big)


def test_flattened_pdf_stays_searchable(fresh_db):
    mid = meeting_with_tasks()
    src = text_pdf(fresh_db / "deck.pdf", noise_png(fresh_db / "p.png", (1200, 900)))
    size = src.stat().st_size
    att_id = attachments.store(mid, src, "deck.pdf", searchable=False, shrink_it=True)
    att = db.get_attachment(att_id)
    assert att["original_size"] == size and att["size"] < size
    assert attachments._text_layer(attachments.file_of(att)) is None  # pages are pictures now
    attachments.set_searchable(att_id, True)
    assert db.search("pricing roadmap")[0]["id"] == mid
    assert db.get_attachment(att_id)["digest"] == "- What the deck covers"  # its kept text was digested


def test_shrunk_pptx_keeps_every_part(fresh_db):
    picture = BytesIO()
    noise_png(fresh_db / "s.png", (3000, 2000))
    Image.open(fresh_db / "s.png").save(picture, "PNG")
    src = pptx(fresh_db / "deck.pptx", picture.getvalue())
    attachments.shrink(src, "deck.pptx", fresh_db / "small.pptx")
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(fresh_db / "small.pptx") as b:
        assert a.namelist() == b.namelist()
        assert len(b.read("ppt/media/image1.png")) < len(a.read("ppt/media/image1.png"))
        assert b.read("ppt/slides/slide1.xml") == a.read("ppt/slides/slide1.xml")


def test_pptx_text_in_slide_order_with_notes(fresh_db):
    text = attachments.pptx_text(pptx(fresh_db / "d.pptx").read_bytes())
    assert text.index("Q3 plan") < text.index("Second: hiring") < text.index("Tenth: budget")
    assert "Speaker notes: Say hello" in text


# ── Searchable notes, and never tasks ──

def test_search_only_while_searchable(client, fresh_db):
    mid = meeting_with_tasks()
    att = client.post(f"/api/meetings/{mid}/attachments?name=notes.txt", content=b"Kangaroo migration figures").json()
    att_id = att["attachment"]["id"]
    assert db.search("kangaroo") == []
    on = client.patch(f"/api/attachments/{att_id}", json={"searchable": True}).json()
    assert on["digest"] == "- What the deck covers" and on["status"] == "ready"
    assert [h["id"] for h in db.search("kangaroo")] == [mid]
    off = client.patch(f"/api/attachments/{att_id}", json={"searchable": False}).json()
    assert db.search("kangaroo") == [] and off["digest"] is None


def test_attachment_never_becomes_a_task(client, fresh_db):
    mid = meeting_with_tasks()
    before = db.tasks_for(mid)
    body = b"## Action Items\n- Somebody: rebuild the whole website\n"
    att = client.post(f"/api/meetings/{mid}/attachments?name=plan.md&searchable=true", content=body).json()["attachment"]
    m = client.get(f"/api/meetings/{mid}").json()
    assert m["summary"] == db.get_meeting(mid)["summary"] and "rebuild" not in m["summary"]
    assert [t["text"] for t in m["tasks"]] == [t["text"] for t in before] == ["Priya: send the budget"]
    assert m["attachments"][0]["id"] == att["id"]
    assert "rebuild the whole website" in attachments.context_block(mid)
    # ... and exported notes keep it after the tasks, under its own heading
    md = exporter.documents_for(db.get_meeting(mid), {"export_summary": True, "export_transcript": False,
                                                      "export_separate_files": False, "export_formats": ["md"],
                                                      "your_name": ""})
    text = next(iter(md.values())).decode()
    assert text.index("Priya: send the budget") < text.index("Attached materials")


# ── Links ──

def test_google_slides_link_public_and_private(client, fresh_db, monkeypatch):
    mid = meeting_with_tasks()
    url = "https://docs.google.com/presentation/d/abc123/edit#slide=id.p"
    replies = {"public": httpx.Response(200, text="Roadmap deck: three bets", headers={"content-type": "text/plain"},
                                        request=httpx.Request("GET", "https://docs.google.com/presentation/d/abc123/export/txt")),
               "private": httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"},
                                         request=httpx.Request("GET", "https://accounts.google.com/signin"))}
    state = {"now": "private"}
    asked = []
    monkeypatch.setattr(attachments.httpx, "get", lambda u, **kw: asked.append(u) or replies[state["now"]])
    att = client.post(f"/api/meetings/{mid}/attachments/link", json={"url": url, "searchable": False}).json()
    assert att["name"] == "Google Slides" and asked == []  # nothing fetched until it's ticked
    att = client.patch(f"/api/attachments/{att['id']}", json={"searchable": True}).json()
    assert att["status"] == "error" and "shared publicly" in att["error"]
    assert asked == ["https://docs.google.com/presentation/d/abc123/export/txt"]
    state["now"] = "public"
    att = client.post(f"/api/attachments/{att['id']}/read").json()
    assert att["status"] == "ready" and db.search("three bets")[0]["id"] == mid


def test_no_digest_while_memory_is_short(fresh_db, monkeypatch):
    mid = meeting_with_tasks()
    monkeypatch.setattr(resources, "check", lambda gb, what: "Waiting for memory: 1 GB is free")
    att_id = attachments.store(mid, _text_file(fresh_db, "Wombat survey"), "survey.txt", True, False)
    att = db.get_attachment(att_id)
    assert att["status"] == "error" and "Waiting for memory" in att["error"] and att["digest"] is None
    assert db.search("wombat")[0]["id"] == mid  # searchable all the same


def test_small_model_writes_the_digest_when_the_usual_one_doesnt_fit(fresh_db, monkeypatch):
    mid = meeting_with_tasks()
    monkeypatch.setattr(llm_engine, "model_for", lambda pid, cfg: "qwen2.5:7b")
    monkeypatch.setattr(llm_engine, "small_ollama_model", lambda cfg: {"name": "qwen2.5:3b", "size": 2 * 1024**3})
    monkeypatch.setattr(resources, "check", lambda gb, what: None if "small" in what else "Waiting for memory")
    used = []
    monkeypatch.setattr(llm_engine, "generate", lambda pid, cfg, prompt, **kw: used.append(cfg["ollama_model"]) or "- Bullets")
    att_id = attachments.store(mid, _text_file(fresh_db, "Numbat count"), "count.txt", True, False)
    assert used == ["qwen2.5:3b"] and db.get_attachment(att_id)["digest"] == "- Bullets"


def _text_file(folder: Path, text: str) -> Path:
    path = folder / f"{abs(hash(text))}.txt"
    path.write_text(text)
    return path


def test_lockdown_never_fetches(fresh_db, monkeypatch):
    mid = meeting_with_tasks()
    settings.update({"lockdown": True})
    monkeypatch.setattr(attachments.httpx, "get", lambda *a, **kw: pytest.fail("fetched in lockdown"))
    att_id = attachments.add_link(mid, "https://docs.google.com/presentation/d/x/edit", "", True)
    assert "Lockdown" in db.get_attachment(att_id)["error"]


# ── Deleting, exporting ──

def test_deleting_the_meeting_removes_its_files(client, fresh_db):
    mid = meeting_with_tasks()
    client.post(f"/api/meetings/{mid}/attachments?name=a.txt", content=b"hi")
    assert attachments.folder(mid).exists()
    assert client.delete(f"/api/meetings/{mid}").status_code == 204
    assert not attachments.folder(mid).exists() and db.attachments_for(mid) == []


def test_export_and_import_bring_attachments_along(client, fresh_db, monkeypatch):
    mid = meeting_with_tasks()
    db.update_meeting(mid, segments_json=[{"start": 0, "end": 1, "speaker": "You", "text": "Hi"}], transcribed=1)
    client.post(f"/api/meetings/{mid}/attachments?name=deck.txt&searchable=true", content=b"Otter census")
    client.post(f"/api/meetings/{mid}/attachments/link", json={"url": "https://example.com/brief", "name": "Brief"})
    cfg = {"export_dir": str(fresh_db / "Exports"), "export_formats": ["md"], "export_summary": True,
           "export_transcript": False, "export_separate_files": False, "export_audio": False, "your_name": ""}
    paths = [Path(p) for p in exporter.export_meeting(db.get_meeting(mid), cfg)]
    folder = paths[0].parent
    assert (folder / "Attachments" / "deck.txt").read_bytes() == b"Otter census"
    record = json.loads((folder / "meeting.json").read_text())
    assert [a["name"] for a in record["attachments"]] == ["deck.txt", "Brief"]

    client.delete(f"/api/meetings/{mid}")
    new_id = main.archive.restore(record, folder)
    restored = db.attachments_for(new_id)
    assert [(a["name"], bool(a["searchable"])) for a in restored] == [("deck.txt", True), ("Brief", False)]
    assert attachments.file_of(restored[0]).read_bytes() == b"Otter census"
    assert db.search("otter census")[0]["id"] == new_id
