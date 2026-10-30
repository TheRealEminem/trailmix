"""Text recognition (OCR) on this computer where Apple's isn't: Windows and Linux (and a Mac running the server
without the Trailmix app). attachments.py uses it to read photos and scanned or flattened PDF pages.

It runs PaddleOCR's PP-OCRv6 small models (Apache 2.0) with ONNX Runtime, using only numpy and Pillow around them
(no OpenCV, which has no Windows-on-ARM build): one model finds the lines of text, the other reads each line. The
models, about 31 MB, aren't in the installers; they're downloaded from Hugging Face the first time a picture is
read, and kept with the speech models.
"""
import hashlib
import importlib.util
import logging
import math
import os
import threading
from pathlib import Path

log = logging.getLogger("trailmix")

# Each model's repo, the commit it's pinned to (so a change upstream can't break reading), and its files' SHA-256.
DETECT = ("PaddlePaddle/PP-OCRv6_small_det_onnx", "28fe5895c24fd108c19eb3e8479f4ab385fbfc62",
          {"inference.onnx": "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e"})
READ = ("PaddlePaddle/PP-OCRv6_small_rec_onnx", "b8f84f0b80c529de40b4fbb3544b84fa7233a513",
        {"inference.onnx": "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634",
         "inference.yml": None})  # the characters it reads
DOWNLOAD_MB = 31

# Finding lines (the detection model's settings, from its inference.yml).
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
MIN_SIDE, MAX_SIDE = 736, 2400   # a small picture is enlarged so small print is found; a big one isn't read bigger
THRESH, BOX_THRESH, UNCLIP = 0.2, 0.45, 1.4
MAX_LINES = 1000
# Reading them.
LINE_HEIGHT, MIN_WIDTH = 48, 320
BATCH = 6
MIN_SCORE = 0.5                  # a line read with less confidence than this is left out (usually not text)


class Unavailable(RuntimeError):
    """Text recognition can't run here: not installed, or its models can't be downloaded."""


def available() -> bool:
    """ONNX Runtime is installed (the Windows and Linux apps, and a Docker or Linux server, have it)."""
    return all(importlib.util.find_spec(m) for m in ("onnxruntime", "numpy", "PIL", "yaml", "huggingface_hub"))


# ── The models ──────────────────────────────────────────────────────────

_lock = threading.Lock()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file(model: tuple, name: str, download: bool) -> Path:
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # as models.py does, should this import huggingface_hub first
    from huggingface_hub import hf_hub_download

    repo, commit, files = model
    path = Path(hf_hub_download(repo, name, revision=commit, local_files_only=not download))
    want = files[name]
    if want and _sha256(path) != want:
        path.unlink(missing_ok=True)
        raise Unavailable(f"The text recognition model downloaded wrong ({repo}/{name}); try again")
    return path


def installed() -> bool:
    try:
        for model in (DETECT, READ):
            for name in model[2]:
                _file(model, name, download=False)
        return True
    except Exception:
        return False


def model_files(locked: bool = False) -> dict[str, Path]:
    """{"detect", "read", "characters"}: the models on disk, downloading them the first time unless `locked`
    (Lockdown mode). Raises Unavailable."""
    if not available():
        raise Unavailable("Text recognition isn't installed with this server")
    with _lock:
        if not installed():
            if locked:
                raise Unavailable("Lockdown mode is on, so the text recognition model can't be downloaded. Turn it "
                                  "off in Settings → Privacy to download it once")
            log.info("Downloading the text recognition models (about %d MB)", DOWNLOAD_MB)
            try:
                for model in (DETECT, READ):
                    for name in model[2]:
                        _file(model, name, download=True)
            except Unavailable:
                raise
            except Exception as e:
                log.warning("Couldn't download the text recognition models: %s", e)
                raise Unavailable(f"Couldn't download the text recognition model ({type(e).__name__}). Check your "
                                  "internet connection and try again") from e
        return {"detect": _file(DETECT, "inference.onnx", False), "read": _file(READ, "inference.onnx", False),
                "characters": _file(READ, "inference.yml", False)}


def _characters(path: Path) -> list[str]:
    import yaml

    with open(path, encoding="utf-8") as f:
        chars = yaml.safe_load(f)["PostProcess"]["character_dict"]
    return ["", *(str(c) for c in chars), " "]  # 0 is CTC's blank; the last is a space


# ── Reading ─────────────────────────────────────────────────────────────

class Reader:
    """Loads the models once for a batch of pictures: Reader().text(image) for each. `locked`: Lockdown mode, when
    the models are only used if they're already here."""

    def __init__(self, locked: bool = False):
        import onnxruntime as ort

        files = model_files(locked)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        threads = os.cpu_count() or 4
        opts.intra_op_num_threads = max(1, min(8, threads - 1))
        self.detector = ort.InferenceSession(str(files["detect"]), opts, providers=["CPUExecutionProvider"])
        self.recognizer = ort.InferenceSession(str(files["read"]), opts, providers=["CPUExecutionProvider"])
        self.chars = _characters(files["characters"])

    def text(self, image) -> str:
        """A picture's text (a PIL image), one line of text per line, top to bottom."""
        image = image.convert("RGB")
        boxes = self._find_lines(image)
        if not boxes:
            return ""
        crops = [_crop(image, box) for box in boxes]
        read = self._read_lines(crops)
        found = [(box, text) for box, (text, score) in zip(boxes, read) if text.strip() and score >= MIN_SCORE]
        return "\n".join(_as_lines(found))

    # Finding where the lines are: a map of how likely each spot is to be text, cut into blobs, each blob's
    # smallest rectangle grown a little (the map marks the middle of the text, not its edges).
    def _find_lines(self, image) -> list:
        import numpy as np
        from PIL import Image

        w, h = image.size
        ratio = 1.0
        if min(w, h) < MIN_SIDE:
            ratio = MIN_SIDE / min(w, h)
        if max(w, h) * ratio > MAX_SIDE:
            ratio = MAX_SIDE / max(w, h)
        nw, nh = max(32, round(w * ratio / 32) * 32), max(32, round(h * ratio / 32) * 32)
        x = np.asarray(image.resize((nw, nh), Image.Resampling.BILINEAR), dtype=np.float32)[:, :, ::-1] / 255.0
        x = (x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None], dtype=np.float32)
        prob = self.detector.run(None, {self.detector.get_inputs()[0].name: x})[0][0, 0]
        sx, sy = w / nw, h / nh
        boxes = []
        for rows, starts, ends in _blobs(prob > THRESH)[:MAX_LINES * 4]:
            if (ends - starts).sum() < 6:
                continue
            rect = _min_rect(rows, starts, ends)
            if rect is None:
                continue
            centre, u, v, a, b = rect
            if min(a, b) * 2 < 3 or _mean_inside(prob, rows, starts, ends) < BOX_THRESH:
                continue
            grow = (2 * a) * (2 * b) * UNCLIP / (2 * (2 * a + 2 * b))
            a, b = a + grow, b + grow
            if min(a, b) * 2 < 5:
                continue
            corners = [centre - a * u - b * v, centre + a * u - b * v, centre + a * u + b * v, centre - a * u + b * v]
            pts = np.array(corners) * (sx, sy)
            pts[:, 0] = pts[:, 0].clip(0, w - 1)
            pts[:, 1] = pts[:, 1].clip(0, h - 1)
            boxes.append(pts)
            if len(boxes) >= MAX_LINES:
                break
        return boxes

    def _read_lines(self, crops) -> list[tuple[str, float]]:
        import numpy as np
        from PIL import Image

        out: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: crops[i].width / max(1, crops[i].height))
        name = self.recognizer.get_inputs()[0].name
        for start in range(0, len(order), BATCH):
            batch = order[start:start + BATCH]
            widest = max(MIN_WIDTH / LINE_HEIGHT, *(crops[i].width / max(1, crops[i].height) for i in batch))
            width = int(LINE_HEIGHT * widest)
            x = np.zeros((len(batch), 3, LINE_HEIGHT, width), np.float32)
            for k, i in enumerate(batch):
                crop = crops[i]
                cw = min(width, max(1, math.ceil(LINE_HEIGHT * crop.width / max(1, crop.height))))
                line = np.asarray(crop.resize((cw, LINE_HEIGHT), Image.Resampling.BILINEAR), np.float32)[:, :, ::-1]
                x[k, :, :, :cw] = (line.transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
            probs = self.recognizer.run(None, {name: x})[0]
            for k, i in enumerate(batch):
                out[i] = self._decode(probs[k])
        return out

    def _decode(self, probs) -> tuple[str, float]:
        """CTC: the likeliest character at each step, repeats and blanks dropped."""
        import numpy as np

        best, conf = probs.argmax(axis=1), probs.max(axis=1)
        keep = best != 0
        keep[1:] &= best[1:] != best[:-1]
        if not keep.any():
            return "", 0.0
        text = "".join(self.chars[c] if c < len(self.chars) else "" for c in best[keep])
        return text, float(np.mean(conf[keep]))


def _blobs(bitmap) -> list:
    """The connected patches (touching corners count) of a True/False map, each as its rows' runs:
    (rows, starts, ends) arrays, end exclusive. Done on runs of a row rather than pixel by pixel."""
    import numpy as np

    h, w = bitmap.shape
    padded = np.zeros((h, w + 2), np.int8)
    padded[:, 1:-1] = bitmap
    d = np.diff(padded, axis=1)
    rows, starts = np.nonzero(d == 1)
    _, ends = np.nonzero(d == -1)
    n = len(rows)
    if not n:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = np.searchsorted(rows, np.arange(h + 1))  # where each row's runs begin
    rs, ss, es = rows.tolist(), starts.tolist(), ends.tolist()
    for r in range(1, h):
        i, i_end = first[r], first[r + 1]
        j, j_end = first[r - 1], first[r]
        while i < i_end and j < j_end:
            if ss[i] <= es[j] and ss[j] <= es[i]:  # overlap, or touch at a corner
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
            if es[i] < es[j]:
                i += 1
            else:
                j += 1
    roots = np.array([find(i) for i in range(n)])
    order = np.argsort(roots, kind="stable")
    cuts = np.flatnonzero(np.diff(roots[order])) + 1
    return [(rows[g], starts[g], ends[g]) for g in np.split(order, cuts)]


def _mean_inside(prob, rows, starts, ends) -> float:
    import numpy as np

    total = 0.0
    for r, s, e in zip(rows.tolist(), starts.tolist(), ends.tolist()):
        total += float(prob[r, s:e].sum())
    return total / max(1, int(np.sum(ends - starts)))


def _min_rect(rows, starts, ends):
    """The smallest rectangle around a blob: (centre, u, v, half length along u, half length along v), u the side
    nearer horizontal pointing right, v pointing down. None for a blob that's a single point."""
    import numpy as np

    # A row's leftmost and rightmost pixels are all the outline needs.
    by_row: dict[int, list[int]] = {}
    for r, s, e in zip(rows.tolist(), starts.tolist(), ends.tolist()):
        lo_hi = by_row.setdefault(r, [s, e - 1])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], s), max(lo_hi[1], e - 1)
    pts = sorted({(x, r) for r, (lo, hi) in by_row.items() for x in (lo, hi)})
    hull = _hull(pts)
    if len(hull) < 2:
        return None
    p = np.array(hull, np.float64)
    edges = np.roll(p, -1, axis=0) - p
    lengths = np.hypot(edges[:, 0], edges[:, 1])
    edges, ok = edges[lengths > 0], lengths[lengths > 0]
    if not len(edges):
        return None
    us = edges / ok[:, None]
    vs = np.stack([-us[:, 1], us[:, 0]], axis=1)
    pu, pv = p @ us.T, p @ vs.T
    spans_u, spans_v = pu.max(0) - pu.min(0), pv.max(0) - pv.min(0)
    k = int(np.argmin(spans_u * spans_v))
    u, v = us[k], vs[k]
    centre = u * (pu[:, k].max() + pu[:, k].min()) / 2 + v * (pv[:, k].max() + pv[:, k].min()) / 2
    a, b = spans_u[k] / 2, spans_v[k] / 2
    if abs(u[0]) < abs(u[1]):  # make u the side nearer horizontal
        u, v, a, b = v, u, b, a
    if u[0] < 0:
        u = -u
    if v[1] < 0:
        v = -v
    return centre, u, v, a, b


def _hull(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Convex hull (Andrew's monotone chain) of sorted, distinct points."""
    if len(points) <= 2:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for q in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list = []
    for q in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


def _crop(image, box):
    """The line's rectangle cut out and straightened; a tall, narrow one (text running down) turned on its side."""
    from PIL import Image

    tl, tr, br, bl = box
    width = max(1, round(max(math.dist(tl, tr), math.dist(bl, br))))
    height = max(1, round(max(math.dist(tl, bl), math.dist(tr, br))))
    quad = (*tl, *bl, *br, *tr)
    crop = image.transform((width, height), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)
    if height / width >= 1.5:
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    return crop


def _as_lines(found: list) -> list[str]:
    """Pieces of text side by side (a table's columns, words far apart) joined into one line, top to bottom, along
    the page's own slant (a photo is rarely straight)."""
    if not found:
        return []
    ux = uy = 0.0
    for box, _ in found:  # the slant, the longer lines counting most
        ux, uy = ux + box[1][0] - box[0][0], uy + box[1][1] - box[0][1]
    norm = math.hypot(ux, uy) or 1.0
    ux, uy = ux / norm, uy / norm
    items = []
    for (tl, tr, br, bl), text in found:
        cx, cy = (tl[0] + br[0]) / 2, (tl[1] + br[1]) / 2
        height = max(math.dist(tl, bl), math.dist(tr, br))
        items.append((cy * ux - cx * uy, height, cx * ux + cy * uy, text.strip()))  # (across, height, along)
    items.sort()
    lines: list[list] = []
    for across, height, along, text in items:
        if lines:
            line_across, line_height = lines[-1][0]
            if abs(across - line_across) < 0.5 * min(height, line_height):
                lines[-1][1].append((along, text))
                continue
        lines.append([(across, height), [(along, text)]])
    return [" ".join(t for _, t in sorted(parts)) for _, parts in lines]
