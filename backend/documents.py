"""A meeting as a document: PDF, Word (.docx), OpenDocument (.odt), Markdown or plain text.

The meeting is first laid out as a list of simple blocks (headings, paragraphs, bullets, ticked or open
tasks, transcript lines); each writer turns those blocks into its format. Word and OpenDocument are written
directly (both are zipped XML), and PDF uses fpdf2 with the Geist font, so nothing else needs installing.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

FORMATS = {
    "pdf": "PDF",
    "docx": "Word",
    "odt": "OpenDocument",
    "md": "Markdown",
    "txt": "Plain text",
}
FONTS = Path(__file__).with_name("assets") / "fonts"


@dataclass
class Block:
    kind: str                 # title | meta | h2 | h3 | p | bullet | task | line
    text: str = ""
    done: bool = False        # task
    time: str = ""            # line: "12:34"
    speaker: str = ""         # line


@dataclass
class Doc:
    title: str
    blocks: list[Block] = field(default_factory=list)


# ── From a meeting to blocks ────────────────────────────────────────────

_TASK = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def summary_blocks(markdown: str) -> list[Block]:
    """The summary's Markdown (## headings, bullets, - [ ] tasks, paragraphs) as blocks."""
    out: list[Block] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(Block("p", " ".join(para)))
            para.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("### "):
            flush()
            out.append(Block("h3", line[4:].strip()))
        elif line.startswith("#"):
            flush()
            out.append(Block("h2", line.lstrip("#").strip()))
        elif (m := _TASK.match(line)):
            flush()
            out.append(Block("task", m.group(2).strip(), done=m.group(1).lower() == "x"))
        elif (m := _BULLET.match(line)) or (m := _NUMBERED.match(line)):
            flush()
            out.append(Block("bullet", m.group(1).strip()))
        else:
            para.append(line.strip())
    flush()
    return out


def meeting_doc(title: str, when: datetime, duration: float, speakers: str, summary: str | None,
                transcript: list[dict] | None, part: str = "all") -> Doc:
    """part: 'all', 'summary' or 'transcript'. transcript items: {time, speaker, text}."""
    doc = Doc(title if part != "transcript" else f"{title}: transcript")
    doc.blocks.append(Block("title", doc.title))
    meta = f"{when:%A, %B %-d, %Y} at {when:%-I:%M %p} · {fmt_duration(duration)}"
    if speakers:
        meta += f" · {speakers}"
    doc.blocks.append(Block("meta", meta))
    if summary and part in ("all", "summary"):
        doc.blocks += summary_blocks(summary)
    if transcript and part in ("all", "transcript"):
        if part == "all":
            doc.blocks.append(Block("h2", "Transcript"))
        doc.blocks += [Block("line", t["text"], time=t["time"], speaker=t.get("speaker") or "") for t in transcript]
    return doc


def fmt_duration(seconds: float) -> str:
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# Inline **bold** in summaries: split into (text, bold) runs.
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def runs(text: str) -> list[tuple[str, bool]]:
    out, pos = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [("", False)]


def plain(text: str) -> str:
    return _BOLD.sub(r"\1", text)


# ── Writers ─────────────────────────────────────────────────────────────

def render(doc: Doc, fmt: str) -> bytes:
    return {"pdf": to_pdf, "docx": to_docx, "odt": to_odt, "md": to_markdown, "txt": to_text}[fmt](doc)


def to_markdown(doc: Doc) -> bytes:
    out = []
    for b in doc.blocks:
        if b.kind == "title":
            out += [f"# {b.text}", ""]
        elif b.kind == "meta":
            out += [f"_{b.text}_", ""]
        elif b.kind == "h2":
            out += ["", f"## {b.text}", ""]
        elif b.kind == "h3":
            out += ["", f"### {b.text}", ""]
        elif b.kind == "p":
            out += [b.text, ""]
        elif b.kind == "bullet":
            out.append(f"- {b.text}")
        elif b.kind == "task":
            out.append(f"- [{'x' if b.done else ' '}] {b.text}")
        elif b.kind == "line":
            who = f" {b.speaker}:" if b.speaker else ""
            out += [f"**[{b.time}]{who}** {b.text}", ""]
    return ("\n".join(out).strip() + "\n").replace("\n\n\n", "\n\n").encode()


def to_text(doc: Doc) -> bytes:
    out = []
    for b in doc.blocks:
        if b.kind == "title":
            out += [b.text.upper(), ""]
        elif b.kind == "meta":
            out += [b.text, ""]
        elif b.kind in ("h2", "h3"):
            out += ["", plain(b.text).upper() if b.kind == "h2" else plain(b.text), ""]
        elif b.kind == "p":
            out += [plain(b.text), ""]
        elif b.kind == "bullet":
            out.append(f"  • {plain(b.text)}")
        elif b.kind == "task":
            out.append(f"  [{'x' if b.done else ' '}] {plain(b.text)}")
        elif b.kind == "line":
            who = f" {b.speaker}:" if b.speaker else ""
            out.append(f"[{b.time}]{who} {b.text}")
    return ("\n".join(out).strip() + "\n").encode()


# ── PDF (fpdf2) ─────────────────────────────────────────────────────────

FOREST = (35, 99, 71)
INK = (29, 42, 36)
SOFT = (91, 106, 98)


def to_pdf(doc: Doc) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("Geist", "", str(FONTS / "Geist-Regular.ttf"))
    pdf.add_font("Geist", "B", str(FONTS / "Geist-SemiBold.ttf"))
    pdf.add_font("Geist", "I", str(FONTS / "Geist-Italic.ttf"))
    pdf.set_title(doc.title)
    pdf.set_creator("Trailmix")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def text(s: str, size: float, style: str = "", color=INK, gap: float = 1.5, indent: float = 0, markdown: bool = True):
        pdf.set_font("Geist", style, size)
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(width - indent, size * 0.5, s, markdown=markdown, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(gap)

    for b in doc.blocks:
        if b.kind == "title":
            text(b.text, 20, "B", gap=1, markdown=False)
        elif b.kind == "meta":
            text(b.text, 9.5, "", SOFT, gap=5, markdown=False)
        elif b.kind == "h2":
            pdf.ln(3)
            text(b.text, 13, "B", FOREST, gap=1.5)
        elif b.kind == "h3":
            pdf.ln(1)
            text(b.text, 11, "B", gap=1)
        elif b.kind == "p":
            text(b.text, 10.5, gap=2.5)
        elif b.kind in ("bullet", "task"):
            mark = "•" if b.kind == "bullet" else ("☑" if b.done else "☐")
            pdf.set_font("Geist", "", 10.5)
            pdf.set_text_color(*(FOREST if b.kind == "task" else SOFT))
            y = pdf.get_y()
            pdf.set_x(pdf.l_margin + 2)
            pdf.cell(5, 5.25, mark if b.kind == "bullet" else ("[x]" if b.done else "[ ]"))
            pdf.set_y(y)
            text(b.text, 10.5, gap=1.2, indent=9)
        elif b.kind == "line":
            pdf.set_font("Geist", "", 8.5)
            pdf.set_text_color(*SOFT)
            y = pdf.get_y()
            pdf.set_x(pdf.l_margin)
            pdf.cell(14, 5, b.time)
            pdf.set_y(y)
            body = f"**{b.speaker}:** {b.text}" if b.speaker else b.text
            text(body.replace("\\", "\\\\"), 10, gap=1.2, indent=15)
    return bytes(pdf.output())


# ── Word (.docx, Office Open XML) ───────────────────────────────────────

def _w_run(text: str, bold: bool = False, italic: bool = False, color: str | None = None, size: int | None = None) -> str:
    props = "".join([
        "<w:b/>" if bold else "", "<w:i/>" if italic else "",
        f'<w:color w:val="{color}"/>' if color else "", f'<w:sz w:val="{size}"/>' if size else "",
    ])
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _w_para(content: str, style: str | None = None, space_after: int | None = None, indent: int | None = None) -> str:
    props = "".join([
        f'<w:pStyle w:val="{style}"/>' if style else "",
        f'<w:spacing w:after="{space_after}"/>' if space_after is not None else "",
        f'<w:ind w:left="{indent}" w:hanging="{indent // 2}"/>' if indent else "",
    ])
    return f'<w:p>{f"<w:pPr>{props}</w:pPr>" if props else ""}{content}</w:p>'


def to_docx(doc: Doc) -> bytes:
    body = []
    for b in doc.blocks:
        if b.kind == "title":
            body.append(_w_para(_w_run(b.text), "Title"))
        elif b.kind == "meta":
            body.append(_w_para(_w_run(b.text, color="5B6A62", size=20), space_after=240))
        elif b.kind in ("h2", "h3"):
            body.append(_w_para("".join(_w_run(t, bold) for t, bold in runs(b.text)), "Heading1" if b.kind == "h2" else "Heading2"))
        elif b.kind == "p":
            body.append(_w_para("".join(_w_run(t, bold) for t, bold in runs(b.text))))
        elif b.kind in ("bullet", "task"):
            mark = "•\t" if b.kind == "bullet" else ("☑\t" if b.done else "☐\t")
            body.append(_w_para(_w_run(mark) + "".join(_w_run(t, bold) for t, bold in runs(b.text)), space_after=60, indent=360))
        elif b.kind == "line":
            parts = _w_run(f"{b.time}   ", color="5B6A62", size=18)
            if b.speaker:
                parts += _w_run(f"{b.speaker}: ", bold=True)
            body.append(_w_para(parts + _w_run(b.text), space_after=80))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(body)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Helvetica Neue" w:hAnsi="Helvetica Neue" w:cs="Arial"/>'
        '<w:sz w:val="22"/><w:color w:val="1D2A24"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="44"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="320" w:after="100"/><w:outlineLvl w:val="0"/></w:pPr>'
        '<w:rPr><w:b/><w:color w:val="236347"/><w:sz w:val="28"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="60"/><w:outlineLvl w:val="1"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>'
        "</w:styles>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>"
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{escape(doc.title)}</dc:title><dc:creator>Trailmix</dc:creator></cp:coreProperties>"
        ),
        "word/document.xml": document,
        "word/styles.xml": styles,
    }
    return _zip(files)


# ── OpenDocument (.odt) ─────────────────────────────────────────────────

def _o_spans(text: str) -> str:
    return "".join(f'<text:span text:style-name="B">{escape(t)}</text:span>' if bold else escape(t) for t, bold in runs(text))


def to_odt(doc: Doc) -> bytes:
    body = []
    for b in doc.blocks:
        if b.kind == "title":
            body.append(f'<text:p text:style-name="Title">{escape(b.text)}</text:p>')
        elif b.kind == "meta":
            body.append(f'<text:p text:style-name="Meta">{escape(b.text)}</text:p>')
        elif b.kind in ("h2", "h3"):
            level = 1 if b.kind == "h2" else 2
            body.append(f'<text:h text:style-name="H{level}" text:outline-level="{level}">{_o_spans(b.text)}</text:h>')
        elif b.kind == "p":
            body.append(f'<text:p text:style-name="Body">{_o_spans(b.text)}</text:p>')
        elif b.kind in ("bullet", "task"):
            mark = "•" if b.kind == "bullet" else ("☑" if b.done else "☐")
            body.append(f'<text:p text:style-name="Item">{mark}<text:tab/>{_o_spans(b.text)}</text:p>')
        elif b.kind == "line":
            who = f'<text:span text:style-name="B">{escape(b.speaker)}: </text:span>' if b.speaker else ""
            body.append(f'<text:p text:style-name="Body"><text:span text:style-name="Time">{escape(b.time)}</text:span>'
                        f"<text:s text:c=\"3\"/>{who}{escape(b.text)}</text:p>")
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
          'xmlns:dc="http://purl.org/dc/elements/1.1/" '
          'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" office:version="1.3"')
    styles = (
        '<office:automatic-styles>'
        '<style:style style:name="Title" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.1cm"/>'
        '<style:text-properties fo:font-size="22pt" fo:font-weight="bold"/></style:style>'
        '<style:style style:name="Meta" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.4cm"/>'
        '<style:text-properties fo:font-size="10pt" fo:color="#5b6a62"/></style:style>'
        '<style:style style:name="H1" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.5cm" fo:margin-bottom="0.15cm" fo:keep-with-next="always"/>'
        '<style:text-properties fo:font-size="14pt" fo:font-weight="bold" fo:color="#236347"/></style:style>'
        '<style:style style:name="H2" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.3cm" fo:margin-bottom="0.1cm" fo:keep-with-next="always"/>'
        '<style:text-properties fo:font-size="12pt" fo:font-weight="bold"/></style:style>'
        '<style:style style:name="Body" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.2cm"/>'
        '<style:text-properties fo:font-size="11pt"/></style:style>'
        '<style:style style:name="Item" style:family="paragraph"><style:paragraph-properties fo:margin-left="0.6cm" fo:text-indent="-0.4cm" fo:margin-bottom="0.1cm"/>'
        '<style:text-properties fo:font-size="11pt"/></style:style>'
        '<style:style style:name="B" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>'
        '<style:style style:name="Time" style:family="text"><style:text-properties fo:font-size="9pt" fo:color="#5b6a62"/></style:style>'
        '</office:automatic-styles>'
    )
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ns}>{styles}'
               f'<office:body><office:text>{"".join(body)}</office:text></office:body></office:document-content>')
    meta = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-meta {ns}><office:meta>'
            f"<dc:title>{escape(doc.title)}</dc:title><meta:generator>Trailmix</meta:generator></office:meta></office:document-meta>")
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    # The mimetype entry must come first and be stored uncompressed.
    return _zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content, "meta.xml": meta,
                 "META-INF/manifest.xml": manifest}, stored_first=True)


def _zip(files: dict[str, str], stored_first: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data) in enumerate(files.items()):
            kind = zipfile.ZIP_STORED if stored_first and i == 0 else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data, compress_type=kind)
    return buffer.getvalue()
