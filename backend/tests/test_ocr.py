"""Text recognition without the Mac app (ocr.py), and attachments reading photos with it.

The parts around the models are tested on their own. Reading real pictures needs ONNX Runtime and the models: it
runs where they're already downloaded, or with TRAILMIX_OCR_TEST=1 (which downloads them, about 31 MB)."""
import math
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import attachments
import database as db
import ocr
import platform_words
import settings

FIXTURES = Path(__file__).parent / "fixtures"


def real_models() -> bool:
    return ocr.available() and (ocr.installed() or os.getenv("TRAILMIX_OCR_TEST") == "1")


needs_models = pytest.mark.skipif(not real_models(), reason="ONNX Runtime or the OCR models aren't here "
                                                           "(TRAILMIX_OCR_TEST=1 downloads them)")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(attachments, "read_later", lambda att_id: attachments.read(att_id))
    monkeypatch.setattr(attachments, "write_digest", lambda name, text, cfg: (None, None))
    monkeypatch.setattr(attachments, "helper_path", lambda: None)  # as on Windows and Linux
    settings.update({"attachments_engine": "local"})
    return tmp_path


# ── The parts around the models ──

def test_blobs_join_touching_runs_and_keep_apart_separate_ones():
    m = np.zeros((8, 12), bool)
    m[1:3, 1:5] = True       # a block
    m[3, 5] = True           # touching it only at a corner: the same blob
    m[6, 8:11] = True        # well away: another
    blobs = ocr._blobs(m)
    sizes = sorted(int((e - s).sum()) for _, s, e in blobs)
    assert sizes == [3, 9]


def test_smallest_rectangle_follows_a_slanted_line():
    # A 200 x 20 bar turned 10 degrees.
    angle = math.radians(10)
    yy, xx = np.mgrid[0:120, 0:260]
    cx, cy = 130, 60
    along = (xx - cx) * math.cos(angle) + (yy - cy) * math.sin(angle)
    across = -(xx - cx) * math.sin(angle) + (yy - cy) * math.cos(angle)
    m = (abs(along) <= 100) & (abs(across) <= 10)
    (rows, starts, ends), = ocr._blobs(m)
    centre, u, v, a, b = ocr._min_rect(rows, starts, ends)
    assert math.degrees(math.atan2(u[1], u[0])) == pytest.approx(10, abs=1.5)
    assert a == pytest.approx(100, abs=3) and b == pytest.approx(10, abs=2)
    assert centre == pytest.approx([cx, cy], abs=2)
    assert u[0] > 0 and v[1] > 0


def test_pieces_side_by_side_are_one_line_top_to_bottom():
    def box(x, y, w, h):
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

    found = [(box(400, 52, 120, 20), "Owner: Priya"), (box(10, 10, 200, 20), "Pricing roadmap"),
             (box(10, 50, 150, 20), "Budget: $48,500")]
    assert ocr._as_lines(found) == ["Pricing roadmap", "Budget: $48,500 Owner: Priya"]


def test_ctc_drops_repeats_and_blanks():
    reader = ocr.Reader.__new__(ocr.Reader)
    reader.chars = ["", "a", "b", " "]
    steps = [1, 1, 0, 1, 2, 2, 3, 2, 0]
    probs = np.full((len(steps), 4), 0.01, np.float32)
    probs[np.arange(len(steps)), steps] = 0.97
    text, score = reader._decode(probs)
    assert text == "aab b" and score == pytest.approx(0.97)


def test_lockdown_never_downloads(monkeypatch):
    if not ocr.available():
        pytest.skip("ONNX Runtime isn't installed")
    monkeypatch.setattr(ocr, "installed", lambda: False)
    monkeypatch.setattr(ocr, "_file", lambda *a, **kw: pytest.fail("downloaded in lockdown"))
    with pytest.raises(ocr.Unavailable, match="Lockdown"):
        ocr.model_files(locked=True)


# ── Attachments reading photos with it ──

def test_photo_is_read_by_text_recognition_off_a_mac(fresh_db, monkeypatch):
    if not ocr.available():
        pytest.skip("ONNX Runtime isn't installed")

    class Fake:
        def __init__(self, locked=False):
            pass

        def text(self, image):
            return f"Page {image.width}\nsecond line\n"

    monkeypatch.setattr(ocr, "Reader", Fake)
    pages = [FIXTURES / "ocr-line.png", FIXTURES / "ocr-line.png"]
    assert attachments._picture_text(pages, "board.png", settings.get_all()) == \
        "Page 640\nsecond line\n\nPage 640\nsecond line"


def test_without_text_recognition_it_says_to_switch(fresh_db, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    with pytest.raises(attachments.AttachmentError, match="summary provider"):
        attachments._picture_text([FIXTURES / "ocr-line.png"], "board.png", settings.get_all())


def test_settings_wording_no_longer_needs_a_mac_off_one(fresh_db, monkeypatch):
    if not ocr.available():
        pytest.skip("ONNX Runtime isn't installed")
    monkeypatch.setattr(ocr, "Reader", lambda locked=False: (_ for _ in ()).throw(ocr.Unavailable("no network")))
    for plat in ("win32", "linux"):
        monkeypatch.setattr(platform_words.sys, "platform", plat)
        with pytest.raises(attachments.AttachmentError) as e:
            attachments._picture_text([FIXTURES / "ocr-line.png"], "board.png", settings.get_all())
        assert "Mac" not in str(e.value)


def test_heic_photo_opens_without_macos(fresh_db, monkeypatch):
    pytest.importorskip("pillow_heif")
    monkeypatch.setattr(attachments.shutil, "which", lambda name: None)  # no sips
    im = attachments._open_image(FIXTURES / "ocr-line.heic")
    assert im.size == (640, 96)


@needs_models
def test_reads_a_line_of_text():
    text = ocr.Reader().text(Image.open(FIXTURES / "ocr-line.png"))
    assert text.strip() == "Pricing roadmap for Q3"


@needs_models
def test_attached_photo_becomes_searchable(fresh_db):
    pytest.importorskip("pillow_heif")
    mid = db.create_meeting("Planning", title_auto=False)
    db.update_meeting(mid, summary="We planned.", transcript="x", status="done")
    photo = fresh_db / "IMG_1.HEIC"
    photo.write_bytes((FIXTURES / "ocr-line.heic").read_bytes())  # store() moves the file it's given
    att_id = attachments.store(mid, photo, "IMG_1.HEIC", searchable=True, shrink_it=False)
    att = db.get_attachment(att_id)
    assert att["status"] == "ready", att["error"]
    assert "Pricing roadmap for Q3" in att["text"]
    assert db.search("roadmap")[0]["id"] == mid
