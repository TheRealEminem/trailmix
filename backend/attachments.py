"""Files and links kept with a meeting: slides, PDFs, photos of a whiteboard, a Google Slides link.

They live in DATA_DIR/attachments/<meeting id>/. A big file can be shrunk when it's added (photos resized, PDF pages
flattened to pictures, the pictures inside a PowerPoint recompressed) so meeting notes stay small over the years.

Nothing is read from an attachment unless you tick "Add to searchable notes" on it. Then its text is pulled out, a
short digest is written, and both go into the search index and what Ask reads. Neither ever goes into the summary,
so nothing in an attached deck becomes a task. Who reads it is settings["attachments_engine"]: "local" (text
recognition on this Mac and a model running here; nothing leaves the computer) or "provider" (your summary
provider, which is sent the file's text, or the photo itself).
"""
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import httpx

import database as db
import file_import
import live
import llm_engine
import settings

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
HEIC_EXT = {".heic", ".heif"}
DOCUMENT_EXT = {".docx", ".txt", ".md"}
MAX_TEXT = 200_000           # characters of a file's text kept for search
PHOTO_EDGE = 2400            # a shrunk photo's longest side, in pixels
SLIDE_PICTURE_EDGE = 1920    # the same for pictures inside a PowerPoint
FLATTEN_DPI = 120            # a flattened PDF page: sharp enough to read on screen
PREVIEW_EDGE = 640
OCR_PAGES = 30               # pages of a scanned PDF read on this Mac
VISION_PAGES = 8             # ... or sent to your provider (each one costs)

DIGEST_PROMPT = """Below is the text of "{name}", material attached to a meeting's notes for reference.
In 2 to 6 short bullet points ("- "), say what it covers: its subject, its main points, and any figures, names or dates
that stand out. Only say what the text says: leave a point out rather than saying it isn't there. Don't list tasks,
action items or next steps, and don't add a heading. Reply with the bullets only.

TEXT:
{text}
"""
VISION_PROMPT = """Write out the readable text in {what}, as it appears (keep headings and lists as lines).
Then add one line starting "Picture:" that says what it shows (a whiteboard, a chart, a slide, a receipt...).
Reply with that only."""

GOOGLE = re.compile(r"https://docs\.google\.com/(presentation|document|spreadsheets)/d/([\w-]+)")
_GOOGLE_EXPORT = {"presentation": "export/txt", "document": "export?format=txt", "spreadsheets": "export?format=csv"}
GOOGLE_NAMES = {"presentation": "Google Slides", "document": "Google Doc", "spreadsheets": "Google Sheet"}

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachments")

try:  # iPhone photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


class AttachmentError(Exception):
    pass


# ── Where things are ────────────────────────────────────────────────────

def root() -> Path:
    return db.DATA_DIR / "attachments"


def folder(meeting_id: int) -> Path:
    return root() / str(meeting_id)


def file_of(att: dict) -> Path | None:
    return root() / att["path"] if att.get("path") else None


def preview_of(att: dict) -> Path | None:
    return root() / att["preview_path"] if att.get("preview_path") else None


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".pptx":
        return "slides"
    if ext in DOCUMENT_EXT:
        return "document"
    return "other"


def _mime(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".heic": "image/heic", ".heif": "image/heif", ".md": "text/markdown"}.get(ext) or \
        mimetypes.guess_type(name)[0] or "application/octet-stream"


def _safe(name: str) -> str:
    stem = re.sub(r"[^\w .()-]+", "_", Path(name).stem).strip(" .") or "file"
    return f"{stem[:80]}{Path(name).suffix.lower()}"


def present(att: dict) -> dict:
    """What the UI gets: no paths on disk, and no digest or text for one that isn't in the searchable notes."""
    out = {k: att[k] for k in ("id", "kind", "name", "mime", "size", "original_size", "url", "caption", "status",
                               "error", "created_at")}
    out["searchable"] = bool(att["searchable"])
    out["has_preview"] = bool(att.get("preview_path"))
    out["digest"] = att["digest"] if att["searchable"] else None
    out["has_text"] = bool(att["searchable"] and att.get("text"))
    out["can_shrink"] = att["kind"] in ("image", "pdf", "slides") and not att.get("original_size")
    return out


# ── Shrinking ───────────────────────────────────────────────────────────

def needs_choice(path: Path, name: str, cfg: dict) -> bool:
    """Big enough (or a HEIC photo) to offer shrinking it."""
    if kind_of(name) not in ("image", "pdf", "slides"):
        return False
    return Path(name).suffix.lower() in HEIC_EXT or path.stat().st_size > cfg["attachments_shrink_over_mb"] * 1024**2


def shrunk_name(name: str) -> str:
    """A photo comes out as a JPEG (or a PNG, if it has see-through parts it keeps)."""
    if kind_of(name) != "image":
        return name
    ext = Path(name).suffix.lower()
    return name if ext in (".jpg", ".jpeg", ".png") else f"{Path(name).stem}.jpg"


def shrink(src: Path, name: str, dest: Path) -> str:
    """Writes a smaller copy of `src` to `dest`; returns the shrunk file's name (a photo may become a .jpg).
    Raises AttachmentError when it can't be read."""
    kind = kind_of(name)
    try:
        if kind == "image":
            return _shrink_image(src, name, dest)
        if kind == "pdf":
            _flatten_pdf(src, dest)
            return name
        if kind == "slides":
            _shrink_pptx(src, dest)
            return name
    except AttachmentError:
        raise
    except Exception as e:  # a damaged or unusual file: say so, keep the original
        raise AttachmentError(f"Couldn't shrink {name}: {e}") from e
    raise AttachmentError(f"{name} can't be shrunk")


def _open_image(src: Path):
    from PIL import Image, ImageOps
    try:
        im = Image.open(src)
        im.load()
    except Exception:
        if src.suffix.lower() not in HEIC_EXT or not shutil.which("sips"):
            raise
        # A HEIC photo the bundled decoder can't handle: macOS's own converter can.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "photo.jpg"
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "best", str(src), "--out", str(out)],
                           capture_output=True, check=True, timeout=120)
            im = Image.open(out)
            im.load()
    return ImageOps.exif_transpose(im)


def _see_through(im) -> bool:
    """Has parts you can see through (a screenshot's alpha channel is usually solid all over)."""
    if "transparency" in im.info:
        return True
    return "A" in im.getbands() and im.getchannel("A").getextrema()[0] < 255


def _shrink_image(src: Path, name: str, dest: Path) -> str:
    im = _open_image(src)
    im.thumbnail((PHOTO_EDGE, PHOTO_EDGE))
    out_name = shrunk_name(name)
    if Path(out_name).suffix.lower() == ".png":
        if _see_through(im):
            im.save(dest, "PNG", optimize=True)
            return out_name
        out_name = f"{Path(name).stem}.jpg"  # no see-through parts: a JPEG is far smaller
    im.convert("RGB").save(dest, "JPEG", quality=80, optimize=True, progressive=True)
    return out_name


def _flatten_pdf(src: Path, dest: Path, pages: int | None = None) -> int:
    """Every page (or the first `pages`) as a JPEG picture in a new PDF. Returns the pages written."""
    import pypdfium2 as pdfium
    doc, out = pdfium.PdfDocument(src), pdfium.PdfDocument.new()
    try:
        count = len(doc) if pages is None else min(pages, len(doc))
        for i in range(count):
            page = doc[i]
            width, height = page.get_size()
            picture = page.render(scale=FLATTEN_DPI / 72).to_pil().convert("RGB")
            buf = BytesIO()
            picture.save(buf, "JPEG", quality=72, optimize=True)
            new = out.new_page(width, height)
            image = pdfium.PdfImage.new(out)
            image.load_jpeg(BytesIO(buf.getvalue()), inline=True)
            image.set_matrix(pdfium.PdfMatrix().scale(width, height))
            new.insert_obj(image)
            new.gen_content()
        out.save(dest)
        return count
    finally:
        out.close()
        doc.close()


def _shrink_pptx(src: Path, dest: Path) -> None:
    """The pictures in ppt/media resized and recompressed, under the same names (so every slide still finds them);
    everything else copied as is, packed tighter."""
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for info in z.infolist():
            data = z.read(info)
            ext = Path(info.filename).suffix.lower()
            if info.filename.startswith("ppt/media/") and ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"):
                data = _smaller_picture(data, ext) or data
            out.writestr(info.filename, data, compress_type=zipfile.ZIP_STORED if ext in (".jpg", ".jpeg") else
                         zipfile.ZIP_DEFLATED)
    if not zipfile.is_zipfile(dest):
        raise AttachmentError("the shrunk PowerPoint didn't come out right")


def _smaller_picture(data: bytes, ext: str) -> bytes | None:
    from PIL import Image
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception:
        return None
    im.thumbnail((SLIDE_PICTURE_EDGE, SLIDE_PICTURE_EDGE))
    buf = BytesIO()
    if ext in (".jpg", ".jpeg"):
        im.convert("RGB").save(buf, "JPEG", quality=75, optimize=True)
    elif ext == ".png":
        if not _see_through(im) and im.mode != "P":
            im = im.convert("RGB").quantize(256)  # 256 colours: fine for slides, a fraction of the size
        im.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, im.format or "TIFF")
    return buf.getvalue() if len(buf.getvalue()) < len(data) else None


def _small_path(src: Path) -> Path:
    return src.with_name(f"{src.stem}.small{src.suffix}")


def estimate(src: Path, name: str) -> tuple[int, Path | None]:
    """(about how big it would be shrunk, the shrunk copy if making all of it was quick). A long PDF is estimated
    from its first pages."""
    kind = kind_of(name)
    if kind == "pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(src)
        total = len(doc)
        doc.close()
        if total > 12:
            sample = src.with_suffix(".sample.pdf")
            try:
                made = _flatten_pdf(src, sample, pages=6)
                return int(sample.stat().st_size / made * total), None
            finally:
                sample.unlink(missing_ok=True)
    small = _small_path(src)
    out_name = shrink(src, name, small)
    final = small.with_suffix(Path(out_name).suffix.lower())  # a photo that became a JPEG
    small.rename(final)
    return final.stat().st_size, final


def made_earlier(src: Path) -> Path | None:
    """The shrunk copy estimate() left next to an upload, if it made one."""
    return next(src.parent.glob(f"{src.stem}.small.*"), None)


# ── Adding and removing ─────────────────────────────────────────────────

def store(meeting_id: int, src: Path, name: str, searchable: bool, shrink_it: bool,
          shrunk: Path | None = None) -> int:
    """Moves an uploaded file into the meeting's folder (shrunk, if asked and it helps) and records it.
    `shrunk`: a shrunk copy already made (by estimate)."""
    name = Path(name).name or "file"
    original_size = src.stat().st_size
    original_size_kept = text = None
    if shrink_it:
        if shrunk is None or not shrunk.exists():
            shrunk = _small_path(src)
            new_name = shrink(src, name, shrunk)
        else:
            new_name = f"{Path(name).stem}{shrunk.suffix}" if kind_of(name) == "image" else name
        if shrunk.stat().st_size < original_size:
            text = _text_layer(src) if kind_of(name) == "pdf" else None  # flattening loses it; keep it for search
            src.unlink(missing_ok=True)
            src, name, original_size_kept = shrunk, new_name, original_size
        else:
            shrunk.unlink(missing_ok=True)  # it didn't help (a PDF that's mostly text): keep the original
    elif shrunk is not None:
        shrunk.unlink(missing_ok=True)
    dest_dir = folder(meeting_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:8]}-{_safe(name)}"
    shutil.move(src, dest_dir / fname)
    att_id = db.add_attachment(meeting_id, kind=kind_of(name), name=name, mime=_mime(name),
                               path=f"{meeting_id}/{fname}", size=(dest_dir / fname).stat().st_size,
                               original_size=original_size_kept, searchable=int(searchable), text=text)
    _make_preview(db.get_attachment(att_id))
    if searchable:
        read_later(att_id)
    return att_id


def add_link(meeting_id: int, url: str, name: str, searchable: bool) -> int:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise AttachmentError("A link has to start with http:// or https://")
    google = GOOGLE.match(url)
    name = name.strip() or (GOOGLE_NAMES[google.group(1)] if google else re.sub(r"^https?://(www\.)?", "", url)[:80])
    att_id = db.add_attachment(meeting_id, kind="link", name=name, url=url, searchable=int(searchable))
    if searchable:
        read_later(att_id)
    return att_id


def shrink_existing(att_id: int) -> dict:
    """Shrinks a file already attached ("Shrink" on a file kept as it was)."""
    att = db.get_attachment(att_id)
    if not att or not file_of(att) or not file_of(att).exists():
        raise AttachmentError("That file isn't there any more")
    src = file_of(att)
    tmp = _small_path(src)
    new_name = shrink(src, att["name"], tmp)
    if tmp.stat().st_size >= att["size"]:
        tmp.unlink(missing_ok=True)
        raise AttachmentError(f"{att['name']} is already about as small as it gets")
    fname = f"{uuid.uuid4().hex[:8]}-{_safe(new_name)}"
    text = att["text"] or (_text_layer(src) if att["kind"] == "pdf" else None)
    tmp.rename(src.with_name(fname))
    src.unlink(missing_ok=True)
    db.update_attachment(att_id, name=new_name, mime=_mime(new_name), kind=kind_of(new_name),
                         path=f"{att['meeting_id']}/{fname}", size=src.with_name(fname).stat().st_size,
                         original_size=att["size"], text=text)
    return db.get_attachment(att_id)


def remove(att_id: int) -> None:
    att = db.get_attachment(att_id)
    if not att:
        return
    for p in (file_of(att), preview_of(att)):
        if p:
            p.unlink(missing_ok=True)
    db.delete_attachment(att_id)


def remove_all(meeting_id: int) -> None:
    shutil.rmtree(folder(meeting_id), ignore_errors=True)


def set_searchable(att_id: int, on: bool) -> None:
    """Into the searchable notes (reading it, if it hasn't been) or out of them (it stays attached)."""
    att = db.get_attachment(att_id)
    if not att:
        return
    db.update_attachment(att_id, searchable=int(on))
    if on and (att["digest"] is None or att["status"] == "error"):
        read_later(att_id)


def _make_preview(att: dict) -> None:
    """A small JPEG to show: a photo (browsers can't show HEIC), a PDF's first page, a PowerPoint's thumbnail."""
    src = file_of(att)
    if not src:
        return
    dest = src.with_name(f"{src.stem}.preview.jpg")
    try:
        if att["kind"] == "image":
            im = _open_image(src)
        elif att["kind"] == "pdf":
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(src)
            try:
                im = doc[0].render(scale=1).to_pil()
            finally:
                doc.close()
        elif att["kind"] == "slides":
            from PIL import Image
            with zipfile.ZipFile(src) as z:
                thumb = next((n for n in z.namelist() if n.lower().startswith("docprops/thumbnail")), None)
                if not thumb:
                    return
                im = Image.open(BytesIO(z.read(thumb)))
                im.load()
        else:
            return
        im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
        im.convert("RGB").save(dest, "JPEG", quality=78)
    except Exception:
        return  # no preview: the list shows an icon instead
    db.update_attachment(att["id"], preview_path=f"{att['meeting_id']}/{dest.name}")


# ── Reading (only for the searchable notes) ─────────────────────────────

def read_later(att_id: int) -> None:
    db.update_attachment(att_id, status="reading", error=None)
    _executor.submit(read, att_id)


def read(att_id: int) -> None:
    """Pulls the text out of an attachment, writes its digest, and puts both in the search index."""
    att = db.get_attachment(att_id)
    if not att:
        return
    cfg = settings.get_all()
    try:
        # A flattened PDF's text was kept from before it was flattened; a link is fetched again each time.
        text = (att["text"] if att["text"] and att["kind"] != "link" else extract_text(att, cfg)).strip()[:MAX_TEXT]
    except AttachmentError as e:
        db.update_attachment(att_id, status="error", error=str(e))
        return
    except Exception as e:
        db.update_attachment(att_id, status="error", error=f"Couldn't read {att['name']}: {e}")
        return
    db.update_attachment(att_id, text=text)
    if not text:
        db.update_attachment(att_id, status="ready", digest=None,
                             error="No text found in it, so only its name and caption are searchable")
        return
    try:
        digest, note = write_digest(att["name"], text, cfg)
    except AttachmentError as e:  # its text is searchable already; "Try again" writes the digest
        db.update_attachment(att_id, status="error", error=str(e))
        return
    db.update_attachment(att_id, digest=digest, status="ready", error=note)


def extract_text(att: dict, cfg: dict) -> str:
    kind, src = att["kind"], file_of(att)
    if kind == "link":
        return _link_text(att["url"], cfg)
    if not src or not src.exists():
        raise AttachmentError("The file isn't there any more")
    if kind == "pdf":
        return _pdf_text(src, att["name"], cfg)
    if kind == "slides":
        return pptx_text(src.read_bytes())
    if kind == "image":
        return _picture_text([src], att["name"], cfg)
    if kind == "document":
        data = src.read_bytes()
        return file_import._docx_text(data) if src.suffix.lower() == ".docx" else data.decode("utf-8", "replace")
    return ""


def pptx_text(data: bytes) -> str:
    """Each slide's text in order ("Slide 3: ..."), with the speaker notes."""
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

    def paragraphs(xml: bytes) -> list[str]:
        out = []
        for p in ElementTree.fromstring(xml).iter(f"{a}p"):
            line = "".join(t.text or "" for t in p.iter(f"{a}t")).strip()
            if line:
                out.append(line)
        return out

    def number(name: str) -> int:
        m = re.search(r"(\d+)\.xml$", name)
        return int(m.group(1)) if m else 0

    with zipfile.ZipFile(BytesIO(data)) as z:
        names = set(z.namelist())
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=number)
        out = []
        for n in slides:
            lines = paragraphs(z.read(n))
            notes = f"ppt/notesSlides/notesSlide{number(n)}.xml"
            spoken = [ln for ln in paragraphs(z.read(notes)) if not ln.isdigit()] if notes in names else []
            block = f"Slide {number(n)}: " + "\n".join(lines) if lines else f"Slide {number(n)}"
            if spoken:
                block += "\nSpeaker notes: " + " ".join(spoken)
            out.append(block)
    return "\n\n".join(out)


def _pages_text(doc) -> list[str]:
    pages = []
    for page in doc:
        textpage = page.get_textpage()
        pages.append(textpage.get_text_range().replace("\r\n", "\n"))
        textpage.close()
    return pages


def _text_layer(src: Path) -> str | None:
    """A PDF's own text (none for a scan), without reading any pictures."""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(src)
    except Exception:
        return None
    try:
        text = "\n\n".join(_pages_text(doc)).strip()
        return text[:MAX_TEXT] or None
    finally:
        doc.close()


def _pdf_text(src: Path, name: str, cfg: dict) -> str:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(src)
    try:
        pages = _pages_text(doc)
        if sum(len(p.strip()) for p in pages) >= 40 * len(pages):
            return "\n\n".join(pages)
        # Little or no text: a scan, or a PDF that was flattened. Read the pages as pictures.
        limit = OCR_PAGES if cfg["attachments_engine"] == "local" else VISION_PAGES
        with tempfile.TemporaryDirectory() as tmp:
            shots = []
            for i in range(min(limit, len(doc))):
                shot = Path(tmp) / f"page{i + 1}.jpg"
                doc[i].render(scale=150 / 72).to_pil().convert("RGB").save(shot, "JPEG", quality=85)
                shots.append(shot)
            return _picture_text(shots, name, cfg)
    finally:
        doc.close()


def helper_path() -> str | None:
    """The Trailmix Mac app (whose --ocr uses Apple's text recognition), when there is one."""
    if sys.platform != "darwin":
        return None
    candidates = [os.getenv("TRAILMIX_HELPER"),
                  str(Path(__file__).parent.parent / "helper" / ".build" / "release" / "TrailmixHelper"),
                  str(Path(__file__).parent.parent / "helper" / ".build" / "debug" / "TrailmixHelper")]
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def _picture_text(pictures: list[Path], name: str, cfg: dict) -> str:
    if cfg["attachments_engine"] == "local":
        helper = helper_path()
        if not helper:
            raise AttachmentError("Reading photos on this computer needs the Trailmix Mac app. Switch Settings → "
                                  "Attachments to your summary provider to have it read them")
        try:
            done = subprocess.run([helper, "--ocr", *map(str, pictures)], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as e:
            raise AttachmentError("Text recognition took too long") from e
        if done.returncode != 0:
            raise AttachmentError(f"Text recognition couldn't read {name}: {done.stderr.strip()[:200]}")
        pages = done.stdout.split("\f")
        return "\n\n".join(p.strip() for p in pages if p.strip())
    jpegs = [_jpeg_for_model(p) for p in pictures]
    what = "this picture" if len(jpegs) == 1 else f"these {len(jpegs)} pages"

    def ask(pid: str) -> str:
        return llm_engine.generate(pid, cfg, VISION_PROMPT.format(what=what), images=jpegs)

    try:
        text, _ = llm_engine.run_chain("auto", cfg, ask)
    except llm_engine.LLMError as e:
        raise AttachmentError(f"Your provider couldn't read {name} (it needs a model that can see pictures): {e}") from e
    return text


def _jpeg_for_model(path: Path) -> bytes:
    im = _open_image(path)
    im.thumbnail((1600, 1600))
    buf = BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _link_text(url: str, cfg: dict) -> str:
    google = GOOGLE.match(url)
    if not google:
        return ""  # any other link: its name and caption are what's searchable
    if settings.locked(cfg):
        raise AttachmentError("Lockdown mode is on, so the deck can't be fetched from Google")
    kind, doc_id = google.groups()
    export = f"https://docs.google.com/{kind}/d/{doc_id}/{_GOOGLE_EXPORT[kind]}"
    try:
        r = httpx.get(export, follow_redirects=True, timeout=30)
    except httpx.HTTPError as e:
        raise AttachmentError(f"Couldn't reach Google: {e}") from e
    private = (r.status_code in (401, 403, 404) or "accounts.google.com" in str(r.url)
               or r.headers.get("content-type", "").startswith("text/html"))
    if private:
        raise AttachmentError("Couldn't read it: it isn't shared publicly. In Google, set sharing to \"Anyone with "
                              "the link\", then tick the box again")
    if r.status_code >= 400:
        raise AttachmentError(f"Google returned {r.status_code}")
    return r.text


def _digest_chain(cfg: dict) -> list[str]:
    providers = llm_engine.chain("auto", cfg)
    if cfg["attachments_engine"] == "provider":
        return providers
    here = [p for p in providers if llm_engine.runs_here(p, cfg)]
    return here or (["ollama"] if llm_engine.is_local("ollama", cfg) else [])


def write_digest(name: str, text: str, cfg: dict) -> tuple[str | None, str | None]:
    """(digest, note): a few bullets on what it covers, or None with why there's no AI to write one. Raises
    AttachmentError when the AI couldn't (the text is searchable either way)."""
    import resources
    chain = _digest_chain(cfg)
    if not chain:
        return None, "No AI on this computer to write a digest; its text is still searchable"
    budget = min(min(llm_engine.context_chars(p, cfg) for p in chain), 60_000)
    prompt = DIGEST_PROMPT.format(name=name, text=text[:budget])
    errors = []
    for pid in chain:
        if llm_engine.runs_here(pid, cfg):
            while live.active_count():  # never a local model while a meeting is being recorded
                time.sleep(3)
        use = cfg
        if llm_engine.is_local(pid, cfg) and (short := resources.check(llm_engine.ollama_ram_needed_gb(cfg),
                                                                        "Your local model")):
            # A digest is a simple job: a small model does it well enough, where the usual one doesn't fit.
            small = llm_engine.small_ollama_model(cfg)
            if not small or small["name"] == llm_engine.model_for(pid, cfg) or resources.check(
                    small["size"] / 1024**3 * 1.1 + 1.5, f"The small model {small['name']}"):
                errors.append(short)  # don't make the Mac swap for a digest: try again when there's room
                continue
            use = {**cfg, "ollama_model": small["name"]}
        try:
            return llm_engine.tidy(llm_engine.generate(pid, use, prompt)), None
        except llm_engine.LLMError as e:
            errors.append(f"{llm_engine.label(pid, cfg)}: {e}")
    raise AttachmentError(f"No digest yet ({'; '.join(errors)[:300]}). Its text is searchable already")


# ── For Ask and export ──────────────────────────────────────────────────

def searchable(meeting_id: int) -> list[dict]:
    return [a for a in db.attachments_for(meeting_id) if a["searchable"]]


def context_block(meeting_id: int, budget: int = 8000) -> str:
    """The meeting's searchable attachments, for a question about it: clearly reference, not what was said."""
    parts = []
    for a in searchable(meeting_id):
        body = (a["digest"] or "").strip()
        if a["text"]:
            body += ("\n" if body else "") + a["text"][: max(500, budget // max(1, len(parts) + 1))]
        if body or a["caption"]:
            parts.append(f'### {a["name"]}' + (f' ({a["caption"]})' if a["caption"] else "") + f"\n{body}")
    if not parts:
        return ""
    return ("REFERENCE MATERIAL ATTACHED TO THIS MEETING (slides, documents, photos; not part of what was said):\n"
            + "\n\n".join(parts))[:budget]


def export_names(meeting_id: int) -> dict[int, str]:
    """attachment id -> where its file goes in an export folder ("Attachments/Deck.pptx"), names kept apart."""
    out, taken = {}, set()
    for a in db.attachments_for(meeting_id):
        if not a["path"]:
            continue
        name, n = _safe(a["name"]), 2
        while name.lower() in taken:
            name, n = f"{Path(_safe(a['name'])).stem} ({n}){Path(a['name']).suffix.lower()}", n + 1
        taken.add(name.lower())
        out[a["id"]] = f"Attachments/{name}"
    return out


def records(meeting_id: int) -> list[dict]:
    """The attachments for meeting.json: everything but where Trailmix keeps the file."""
    names = export_names(meeting_id)
    return [{"kind": a["kind"], "name": a["name"], "url": a["url"], "caption": a["caption"],
             "searchable": bool(a["searchable"]), "size": a["size"], "original_size": a["original_size"],
             "text": a["text"], "digest": a["digest"], "created_at": a["created_at"], "file": names.get(a["id"])}
            for a in db.attachments_for(meeting_id)]


def restore(meeting_id: int, saved: list[dict], export_folder: Path | None) -> None:
    """Attachments back from meeting.json, with their files when they're next to it (a link needs none)."""
    for r in saved:
        source = export_folder / r["file"] if export_folder and r.get("file") else None
        if r.get("kind") != "link" and not (source and source.is_file()):
            continue
        fields = dict(kind=r.get("kind") or kind_of(r["name"]), name=r["name"], url=r.get("url"),
                      caption=r.get("caption") or "", searchable=int(bool(r.get("searchable"))), text=r.get("text"),
                      digest=r.get("digest"), original_size=r.get("original_size"))
        if source:
            dest_dir = folder(meeting_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{uuid.uuid4().hex[:8]}-{_safe(r['name'])}"
            shutil.copy2(source, dest_dir / fname)
            fields.update(mime=_mime(r["name"]), path=f"{meeting_id}/{fname}", size=(dest_dir / fname).stat().st_size)
        att_id = db.add_attachment(meeting_id, **fields)
        _make_preview(db.get_attachment(att_id))


def materials_markdown(meeting_id: int) -> str:
    """'## Attached materials' for exported notes: each searchable attachment's digest, and every link."""
    atts = db.attachments_for(meeting_id)
    lines = []
    for a in atts:
        if not (a["searchable"] or a["kind"] == "link"):
            continue
        head = f"[{a['name']}]({a['url']})" if a["url"] else a["name"]
        lines.append(f"**{head}**" + (f": {a['caption']}" if a["caption"] else ""))
        if a["searchable"] and a["digest"]:
            lines.append(a["digest"].strip())
        lines.append("")
    return ("## Attached materials\n\n" + "\n".join(lines)).strip() if lines else ""

