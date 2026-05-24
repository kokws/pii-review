"""
pdf_analysis_viz_data_lite.py — PII-Review-scoped viz_data extractor.

Surgically carved from pdf_analysis_viz_data_v4.py, retaining ONLY the
code reachable from `process_one_pdf(..., lite=True)`. The `lite` parameter
is removed (always lite). pdfminer extraction, alignment-matrix analysis,
geometric-cell computation, derived-guides, and the row-info-attach loop
are absent (unused by the PII Review matcher + apply-time tokenizer).
Output `*_viz_data.lite.json` is byte-identical to v4's lite=True output
for any given PDF — verified by SHA-256 A/B test across the canonical
client corpus.
"""

import json
import math
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import time

import fitz  # PyMuPDF
import numpy as np
from PIL import Image as _PILImage, ImageDraw as _PILImageDraw, ImageFont as _PILImageFont
import pdfplumber

THIS_DIR = Path(__file__).parent.resolve()

# ── Configurable tolerances (in pt) ──────────────────────────────────────────
ROW_CLUSTER_TOLERANCE = 3.0    # texts within this Y distance are same row
COL_ANCHOR_TOLERANCE = 3.0     # X positions within this distance merge to one anchor
COL_MIN_TEXTS = 2              # minimum texts sharing an anchor to count as column


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def determine_orientation(width: float, height: float) -> str:
    if width > height:
        return "landscape"
    if width < height:
        return "portrait"
    return "square"


def run_pdftohtml(pdf_path: Path, temp_base: Path) -> Path:
    xml_file = temp_base.with_suffix(".xml")
    cmd = ["pdftohtml", "-xml", str(pdf_path), str(temp_base)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pdftohtml failed for {pdf_path}: {result.stderr}")
    if not xml_file.exists():
        raise FileNotFoundError(f"Expected XML output missing at {xml_file}")
    return xml_file


def run_pdftotext_bbox_layout(pdf_path: Path, temp_base: Path) -> Dict[int, List[Dict[str, Any]]]:
    """Run pdftotext -bbox-layout and parse its HTML output.
    Returns {page_number_1_based: [{text, x0, x1, top, bottom}, ...]}.

    Used to produce Option-D per-word bboxes (poppler-derived advance-width
    extent) alongside Option-A (pdftohtml xml_text frame). Both get written
    into viz_data so downstream consumers can pick whichever they prefer.
    On any failure returns {} — D fields become absent, A still works.
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    html_file = temp_base.with_name(temp_base.name + "_bbox.html")
    try:
        cmd = ["pdftotext", "-bbox-layout", str(pdf_path), str(html_file)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not html_file.exists():
            return out
        xml_text = html_file.read_text(encoding="utf-8", errors="replace")
        # Strip default namespace so ElementTree .iter() works unqualified
        xml_text = re.sub(r'\sxmlns="[^"]+"', "", xml_text, count=1)
        root = ET.fromstring(xml_text)
        for pi, page in enumerate(root.iter("page")):
            page_words: List[Dict[str, Any]] = []
            for w in page.iter("word"):
                try:
                    page_words.append({
                        "text":   (w.text or ""),
                        "x0":     float(w.attrib["xMin"]),
                        "x1":     float(w.attrib["xMax"]),
                        "top":    float(w.attrib["yMin"]),
                        "bottom": float(w.attrib["yMax"]),
                    })
                except (KeyError, ValueError):
                    continue
            out[pi + 1] = page_words
    except Exception:
        return {}
    finally:
        try:
            if html_file.exists():
                html_file.unlink()
        except Exception:
            pass
    return out


def match_poppler_word(word_left: float, word_top: float, word_text: str,
                       poppler_words: List[Dict[str, Any]],
                       tol_y: float = 2.0) -> Optional[Dict[str, Any]]:
    """Find the closest poppler word matching text + position. Returns None
    if no text match on the same row."""
    best = None
    best_d = 1e9
    for pw in poppler_words:
        if pw["text"] != word_text:
            continue
        dy = abs(pw["top"] - word_top)
        if dy > tol_y:
            continue
        d = dy + abs(pw["x0"] - word_left)
        if d < best_d:
            best_d = d
            best = pw
    return best


def parse_fontspecs(root: ET.Element) -> Dict[str, Dict[str, Any]]:
    fontspecs: Dict[str, Dict[str, Any]] = {}
    for fontspec in root.findall(".//fontspec"):
        font_id = fontspec.get("id")
        if not font_id:
            continue
        fontspecs[font_id] = {
            "color": fontspec.get("color"),
            "size": fontspec.get("size"),
            "family": fontspec.get("family"),
        }
    return fontspecs




# ── Surgical ghost-strip via pikepdf (2026-04-25 PB-PDF_QC3) ───────────────
# Walks each page's content stream, decodes every Tj/TJ operator's bytes
# through the active font's ToUnicode CMap (or /Encoding /Differences when
# absent), and deletes ONLY operators whose decoded text matches a ghost
# content string AND whose paint position falls inside the ghost xml_text
# bbox. A visible operator decoding to the same string but painting at a
# different position (e.g. real "Holding name" column header next to a
# ghost "Holding name") survives intact. Replaces the prior bbox-based
# fitz.apply_redactions approach which destroyed visible text in overlap
# zones (PB-REDACT shipped caveat, confirmed visually on AJ Bell p5-9).

import re as _re_strip
import pikepdf as _pikepdf


def _strip_parse_tounicode_cmap(cmap_bytes):
    text = cmap_bytes.decode("latin-1", errors="replace")
    mapping = {}
    for blk in _re_strip.findall(r"beginbfchar(.*?)endbfchar", text, _re_strip.DOTALL):
        for m in _re_strip.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            try:
                src = bytes.fromhex(m.group(1))
                dst = bytes.fromhex(m.group(2)).decode("utf-16-be")
                mapping[src] = dst
            except Exception:
                pass
    for blk in _re_strip.findall(r"beginbfrange(.*?)endbfrange", text, _re_strip.DOTALL):
        for m in _re_strip.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            try:
                src_start_hex = m.group(1); src_end_hex = m.group(2); dst_start_hex = m.group(3)
                src_byte_len = len(src_start_hex) // 2
                dst_byte_len = len(dst_start_hex) // 2
                src_start = int(src_start_hex, 16); src_end = int(src_end_hex, 16); dst_start = int(dst_start_hex, 16)
                for i in range(src_end - src_start + 1):
                    sb = (src_start + i).to_bytes(src_byte_len, "big")
                    db = (dst_start + i).to_bytes(dst_byte_len, "big")
                    try: mapping[sb] = db.decode("utf-16-be")
                    except Exception: pass
            except Exception:
                pass
        for m in _re_strip.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[\s*((?:<[0-9A-Fa-f]+>\s*)+)\]", blk):
            try:
                src_start_hex = m.group(1); src_end_hex = m.group(2); arr_text = m.group(3)
                src_byte_len = len(src_start_hex) // 2
                src_start = int(src_start_hex, 16); src_end = int(src_end_hex, 16)
                for i, dst_hex in enumerate(_re_strip.findall(r"<([0-9A-Fa-f]+)>", arr_text)):
                    if i > src_end - src_start: break
                    sb = (src_start + i).to_bytes(src_byte_len, "big")
                    try: mapping[sb] = bytes.fromhex(dst_hex).decode("utf-16-be")
                    except Exception: pass
            except Exception:
                pass
    return mapping


def _strip_decode_via_cmap(byts, cmap):
    if not cmap: return ""
    out = []; i = 0; n = len(byts)
    while i < n:
        matched = False
        for sz in (2, 1):
            if i + sz <= n:
                key = byts[i:i+sz]
                if key in cmap:
                    out.append(cmap[key]); i += sz; matched = True; break
        if not matched: i += 1
    return "".join(out)


_STRIP_AGL = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quoteright": "'", "parenleft": "(", "parenright": ")",
    "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "quoteleft": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "sterling": "£", "euro": "€", "cent": "¢", "yen": "¥",
    "endash": "–", "emdash": "—", "bullet": "•", "ellipsis": "…",
    "registered": "®", "copyright": "©", "trademark": "™", "degree": "°",
    "plusminus": "±", "multiply": "×", "divide": "÷", "section": "§", "paragraph": "¶",
    "fi": "fi", "fl": "fl",
}


def _strip_glyph_to_char(name):
    n = name.lstrip("/")
    if not n: return ""
    if len(n) == 1: return n
    if n in _STRIP_AGL: return _STRIP_AGL[n]
    if n.startswith("uni") and len(n) == 7:
        try: return chr(int(n[3:], 16))
        except Exception: pass
    if n.startswith("u") and 5 <= len(n) <= 7:
        try: return chr(int(n[1:], 16))
        except Exception: pass
    return ""


def _strip_get_font_cmap(font_obj):
    try:
        if "/ToUnicode" in font_obj:
            return _strip_parse_tounicode_cmap(font_obj["/ToUnicode"].read_bytes())
    except Exception:
        return None
    return None


def _strip_get_encoding_map(font_obj):
    code_map = {}
    base_name = "WinAnsiEncoding"
    try:
        enc = font_obj.get("/Encoding")
        if isinstance(enc, _pikepdf.Dictionary):
            be = enc.get("/BaseEncoding")
            if be is not None: base_name = str(be).lstrip("/")
        elif enc is not None:
            base_name = str(enc).lstrip("/")
    except Exception:
        pass
    if base_name in ("WinAnsiEncoding", "StandardEncoding"):
        for code in range(0x20, 0x100):
            try: code_map[code] = bytes([code]).decode("cp1252")
            except Exception: pass
    elif base_name == "MacRomanEncoding":
        for code in range(0x20, 0x100):
            try: code_map[code] = bytes([code]).decode("mac_roman")
            except Exception: pass
    try:
        enc = font_obj.get("/Encoding")
        if isinstance(enc, _pikepdf.Dictionary):
            diffs = enc.get("/Differences")
            if diffs is not None:
                code = None
                for item in list(diffs):
                    if isinstance(item, int): code = item
                    else:
                        ch = _strip_glyph_to_char(str(item))
                        if code is not None:
                            code_map[code] = ch; code += 1
    except Exception:
        pass
    return code_map


def _strip_decode_via_encoding(byts, code_map):
    return "".join(code_map.get(b, "") for b in byts)


def strip_ghosts_from_pdf(input_path: str, output_path: str, viz_data: Dict[str, Any]) -> int:
    """Surgical content-stream operator removal. Removes only Tj/TJ
    operators whose decoded text matches a ghost content AND whose paint
    position falls inside the ghost xml_text bbox. Returns count of
    operators removed."""
    pdf = _pikepdf.Pdf.open(input_path)
    total_ops_removed = 0
    try:
        for page_no in range(len(pdf.pages)):
            page = pdf.pages[page_no]
            page_data = (viz_data.get("pages", {}).get(str(page_no + 1)) or {})
            ghost_xts = [
                xt for xt in (page_data.get("xml_texts") or [])
                if xt.get("is_ghost") and (xt.get("content") or "").strip()
            ]
            if not ghost_xts:
                continue
            page_h = float(page_data.get("height") or page.MediaBox[3])
            ghosts_by_content = {}
            for xt in ghost_xts:
                content = (xt.get("content") or "").strip()
                l, t = float(xt["left"]), float(xt["top"])
                r, b = float(xt["right"]), float(xt["bottom"])
                ghosts_by_content.setdefault(content, []).append(
                    (l, page_h - b, r, page_h - t))
            try:
                fonts_obj = page.Resources.Font
            except (AttributeError, KeyError):
                continue
            font_decoders = {}
            for fname in fonts_obj.keys():
                try:
                    fobj = fonts_obj[fname]; fkey = str(fname)
                    cm = _strip_get_font_cmap(fobj)
                    if cm: font_decoders[fkey] = ("cmap", cm); continue
                    em = _strip_get_encoding_map(fobj)
                    if em: font_decoders[fkey] = ("enc", em)
                except Exception:
                    pass
            try:
                instructions = list(_pikepdf.parse_content_stream(page))
            except Exception:
                continue
            new_instructions = []
            cur_font = None
            tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            leading = 0.0
            page_ops_removed = 0
            for operands, operator in instructions:
                op_str = str(operator)
                if op_str == "BT":
                    tm = [1.0,0.0,0.0,1.0,0.0,0.0]; tlm = [1.0,0.0,0.0,1.0,0.0,0.0]
                elif op_str == "Tm":
                    try: tm = [float(x) for x in operands[:6]]; tlm = list(tm)
                    except Exception: pass
                elif op_str in ("Td", "TD"):
                    try:
                        tx, ty = float(operands[0]), float(operands[1])
                        tlm[4] = tx*tlm[0] + ty*tlm[2] + tlm[4]
                        tlm[5] = tx*tlm[1] + ty*tlm[3] + tlm[5]
                        tm = list(tlm)
                        if op_str == "TD": leading = -ty
                    except Exception: pass
                elif op_str == "T*":
                    try:
                        tlm[4] += -leading*tlm[2]; tlm[5] += -leading*tlm[3]
                        tm = list(tlm)
                    except Exception: pass
                elif op_str == "TL":
                    try: leading = float(operands[0])
                    except Exception: pass
                elif op_str == "Tf":
                    try: cur_font = str(operands[0])
                    except Exception: cur_font = None
                if op_str in ("Tj", "TJ") and cur_font and cur_font in font_decoders:
                    kind, table = font_decoders[cur_font]
                    raw = b""
                    try:
                        if op_str == "Tj":
                            raw = bytes(operands[0])
                        else:
                            for it in operands[0]:
                                if isinstance(it, _pikepdf.String): raw += bytes(it)
                    except Exception:
                        raw = b""
                    if raw:
                        decoded = (_strip_decode_via_cmap(raw, table) if kind == "cmap"
                                   else _strip_decode_via_encoding(raw, table)).strip()
                        if decoded and decoded in ghosts_by_content:
                            px, py = tm[4], tm[5]
                            tol = 3.0
                            matched = False
                            for (bl, bb, br, bt) in ghosts_by_content[decoded]:
                                if (bl-tol) <= px <= (br+tol) and (bb-tol) <= py <= (bt+tol):
                                    matched = True
                                    break
                            if matched:
                                page_ops_removed += 1
                                continue
                new_instructions.append((operands, operator))
            if page_ops_removed:
                total_ops_removed += page_ops_removed
                page.Contents = pdf.make_stream(_pikepdf.unparse_content_stream(new_instructions))
        pdf.save(output_path)
    finally:
        pdf.close()
    return total_ops_removed


# ── Main extraction ──────────────────────────────────────────────────────────

def process_one_pdf(pdf_path: Path, log=None, progress_cb=None) -> Dict[str, Any]:
    """Extract lite viz_data from a single PDF.

    Always emits the lite sidecar artefact set (no pdfminer, no alignment
    matrix, no geometric cells, no derived guides). Writes
    `<stem>_viz_data.json` under THIS_DIR (caller renames to .lite.json).

    Parameters
    ----------
    pdf_path : Path
        PDF to process.
    log : logging.Logger, optional
        If provided, all progress and errors are written to this logger.
        If None, falls back to print().
    """
    def _log(msg: str) -> None:
        if log is not None:
            log.info(msg)
        else:
            print(msg)

    start_time = time.time()
    pdf_name = pdf_path.name
    json_output_path = THIS_DIR / f"{pdf_path.stem}_viz_data.json"
    temp_base = Path(tempfile.gettempdir()) / f"pdf_analysis_{uuid.uuid4().hex}"

    summary = {
        "pdf": pdf_name,
        "status": "OK",
        "pages": 0,
        "total_text_nodes": 0,
        "total_plumber_rects": 0,
        "total_fitz_rects": 0,
        "total_miner_rects": 0,
        "total_row_clusters": 0,
        "total_col_anchors": 0,
        "time_seconds": 0,
        "output_file": str(json_output_path),
    }

    xml_file = None
    try:
        _log(f"[{pdf_name}] START pdftohtml")
        xml_file = run_pdftohtml(pdf_path, temp_base)
        _log(f"[{pdf_name}] pdftohtml done — parsing XML")
        tree = ET.parse(xml_file)
        root = tree.getroot()
        fontspecs = parse_fontspecs(root)
        _log(f"[{pdf_name}] XML parsed — {len(fontspecs)} fontspecs")

        # pdftotext -bbox-layout produces per-word bboxes that come from the
        # same poppler engine as pdftohtml, but closer to the true advance-
        # width extent per word (used for Option-D word bboxes below).
        _log(f"[{pdf_name}] START pdftotext -bbox-layout")
        poppler_words_by_page = run_pdftotext_bbox_layout(pdf_path, temp_base)
        _log(f"[{pdf_name}] pdftotext done — {sum(len(v) for v in poppler_words_by_page.values())} words across {len(poppler_words_by_page)} pages")

        viz_data: Dict[str, Any] = {
            "schema_version": "v4.10",
            "source_file": str(pdf_path),
            "source_filename": pdf_name,
            "extraction_config": {
                "row_cluster_tolerance_pt": ROW_CLUSTER_TOLERANCE,
                "col_anchor_tolerance_pt": COL_ANCHOR_TOLERANCE,
                "col_min_texts": COL_MIN_TEXTS,
            },
            "pages": {},
            "fontspecs": fontspecs,
        }

        _log(f"[{pdf_name}] Opening with pdfplumber")
        with pdfplumber.open(pdf_path) as plumber_doc:
            _log(f"[{pdf_name}] Opening with PyMuPDF (fitz)")
            fitz_doc = fitz.open(pdf_path)
            try:
                total_pages = len(plumber_doc.pages)
                summary["pages"] = total_pages
                _log(f"[{pdf_name}] {total_pages} pages — starting per-page extraction")

                for page_index in range(total_pages):
                    page_number = page_index + 1
                    _log(f"[{pdf_name}] p{page_number}/{total_pages} START")

                    xml_page = root.find(f".//page[@number='{page_number}']")
                    if xml_page is None:
                        _log(f"[{pdf_name}] p{page_number}/{total_pages} no XML page — skip")
                        continue

                    xml_width = safe_float(xml_page.get("width"), 1.0) or 1.0
                    xml_height = safe_float(xml_page.get("height"), 1.0) or 1.0

                    plumber_page = plumber_doc.pages[page_index]
                    pdf_width = safe_float(plumber_page.width, 1.0) or 1.0
                    pdf_height = safe_float(plumber_page.height, 1.0) or 1.0
                    orientation = determine_orientation(pdf_width, pdf_height)
                    scale_x = pdf_width / xml_width
                    scale_y = pdf_height / xml_height

                    page_data: Dict[str, Any] = {
                        "page_layout": {
                            "width": pdf_width,
                            "height": pdf_height,
                            "orientation": orientation,
                            "units": "pt",
                        },
                        "page_dimensions": {
                            "xml": {"width": xml_width, "height": xml_height},
                            "plumber": {"width": pdf_width, "height": pdf_height},
                        },
                        "xml_texts": [],
                        "plumber_rects": [],
                        "fitz_rects": [],
                        "miner_rects": [],
                        "ocr_texts": [],
                    }

                    # ── Text extraction with anchors ──
                    for text in xml_page.findall(".//text"):
                        text_content = text.text.strip() if text.text else ""
                        is_bold = False
                        is_italic = False

                        for child in text:
                            snippet = child.text.strip() if child.text else ""
                            if child.tag == "b":
                                is_bold = True
                            elif child.tag == "i":
                                is_italic = True
                            text_content += snippet
                            if child.tail:
                                text_content += child.tail.strip()

                        abs_left = safe_float(text.get("left"))
                        abs_top = safe_float(text.get("top"))
                        abs_width = safe_float(text.get("width"))
                        abs_height = safe_float(text.get("height"))
                        font_id = text.get("font")

                        pdf_left = abs_left * scale_x
                        pdf_top = abs_top * scale_y
                        pdf_right = (abs_left + abs_width) * scale_x
                        pdf_bottom = (abs_top + abs_height) * scale_y
                        text_width = pdf_right - pdf_left
                        text_height = pdf_bottom - pdf_top

                        font_spec = fontspecs.get(font_id, {})
                        font_size_pt = safe_float(font_spec.get("size"))
                        font_family = font_spec.get("family") or ""

                        # Detect bold from font family name too
                        family_lower = font_family.lower()
                        if not is_bold and ("bold" in family_lower or "heavy" in family_lower or "black" in family_lower):
                            is_bold = True
                        if not is_italic and ("italic" in family_lower or "oblique" in family_lower):
                            is_italic = True

                        # Anchor points (absolute, in pt)
                        left_anchor = pdf_left
                        right_anchor = pdf_right
                        center_anchor = pdf_left + text_width / 2.0
                        top_anchor = pdf_top
                        bottom_anchor = pdf_bottom   # approx baseline
                        midpoint_y = pdf_top + text_height / 2.0
                        baseline_y = pdf_bottom      # alias for clarity

                        # ── Ghost span filter ──────────────────────────────
                        # Zero-advance-width duplicates from font subset splitting.
                        # Two cases:
                        # 1. Non-empty content with impossible width/char ratio
                        # 2. Empty/space-only span with near-zero width (gets
                        #    filled later by fitz — block that path at the source)
                        _char_count = len(text_content.replace(" ", ""))
                        if text_width <= 3.0 and _char_count == 0:
                            continue  # empty ghost — skip before fill step touches it
                        if _char_count >= 3 and text_width / _char_count < 2.0:
                            continue  # non-empty ghost — skip

                        text_obj = {
                            "content": text_content,
                            # Bounding box (absolute pt)
                            "top": round(pdf_top, 2),
                            "left": round(pdf_left, 2),
                            "width": round(text_width, 2),
                            "height": round(text_height, 2),
                            "bottom": round(pdf_bottom, 2),
                            "right": round(pdf_right, 2),
                            # Anchor points (absolute pt) for alignment analysis
                            "left_anchor": round(left_anchor, 2),
                            "right_anchor": round(right_anchor, 2),
                            "center_anchor": round(center_anchor, 2),
                            "top_anchor": round(top_anchor, 2),
                            "bottom_anchor": round(bottom_anchor, 2),
                            "midpoint_y": round(midpoint_y, 2),
                            "baseline_y": round(baseline_y, 2),
                            # Font attributes
                            "font": font_id,
                            "font_size_pt": font_size_pt,
                            "font_family": font_family,
                            "font_color": font_spec.get("color"),
                            "is_bold": is_bold,
                            "is_italic": is_italic,
                            # Relative positions (0-1 scale)
                            "rel_top": round(pdf_top / pdf_height, 6) if pdf_height else 0.0,
                            "rel_left": round(pdf_left / pdf_width, 6) if pdf_width else 0.0,
                            "rel_bottom": round(pdf_bottom / pdf_height, 6) if pdf_height else 0.0,
                            "rel_right": round(pdf_right / pdf_width, 6) if pdf_width else 0.0,
                            "rel_center_x": round(center_anchor / pdf_width, 6) if pdf_width else 0.0,
                            "rel_midpoint_y": round(midpoint_y / pdf_height, 6) if pdf_height else 0.0,
                        }
                        page_data["xml_texts"].append(text_obj)

                    summary["total_text_nodes"] += len(page_data["xml_texts"])

                    _log(f"[{pdf_name}] p{page_number}/{total_pages} xml_texts done — {len(page_data['xml_texts'])} items. Starting plumber rects")
                    # ── PDFPlumber rectangles (with fill + stroke colors) ──
                    for rect in plumber_page.rects:
                        rect_entry = {
                            "x0": rect["x0"], "y0": rect["top"],
                            "x1": rect["x1"], "y1": rect["bottom"],
                            "rel_x0": round(rect["x0"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y0": round(rect["top"] / pdf_height, 6) if pdf_height else 0.0,
                            "rel_x1": round(rect["x1"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y1": round(rect["bottom"] / pdf_height, 6) if pdf_height else 0.0,
                        }
                        # Fill color (non_stroking_color)
                        nsc = rect.get("non_stroking_color")
                        if nsc is not None:
                            if isinstance(nsc, (list, tuple)) and len(nsc) >= 3:
                                rect_entry["fill_color"] = [round(c, 4) for c in nsc[:3]]
                            elif isinstance(nsc, (int, float)):
                                rect_entry["fill_color"] = [round(nsc, 4)] * 3  # grayscale
                        # Stroke color
                        sc = rect.get("stroking_color")
                        if sc is not None:
                            if isinstance(sc, (list, tuple)) and len(sc) >= 3:
                                rect_entry["stroke_color"] = [round(c, 4) for c in sc[:3]]
                            elif isinstance(sc, (int, float)):
                                rect_entry["stroke_color"] = [round(sc, 4)] * 3
                        # Fill flag
                        if rect.get("fill"):
                            rect_entry["is_filled"] = True
                        if rect.get("stroke"):
                            rect_entry["is_stroked"] = True
                        page_data["plumber_rects"].append(rect_entry)

                    # PDFPlumber actual drawn lines (with stroke color)
                    plumber_lines_raw = []
                    for line in plumber_page.lines:
                        line_entry = {
                            "x0": line["x0"], "y0": line["top"],
                            "x1": line["x1"], "y1": line["bottom"],
                            "rel_x0": round(line["x0"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y0": round(line["top"] / pdf_height, 6) if pdf_height else 0.0,
                            "rel_x1": round(line["x1"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y1": round(line["bottom"] / pdf_height, 6) if pdf_height else 0.0,
                            "type": "horizontal" if abs(line["top"] - line["bottom"]) < 1.0 else
                                    "vertical" if abs(line["x0"] - line["x1"]) < 1.0 else "diagonal",
                        }
                        sc = line.get("stroking_color")
                        if sc is not None:
                            if isinstance(sc, (list, tuple)) and len(sc) >= 3:
                                line_entry["stroke_color"] = [round(c, 4) for c in sc[:3]]
                            elif isinstance(sc, (int, float)):
                                line_entry["stroke_color"] = [round(sc, 4)] * 3
                        lw = line.get("linewidth") or line.get("width")
                        if lw is not None:
                            line_entry["stroke_width"] = round(float(lw), 2)
                        plumber_lines_raw.append(line_entry)
                    page_data["plumber_lines"] = plumber_lines_raw

                    summary["total_plumber_rects"] += len(page_data["plumber_rects"])

                    _log(f"[{pdf_name}] p{page_number}/{total_pages} plumber rects done. Starting plumber lines")
                    # ── PDFPlumber texts (from extract_words) ──
                    plumber_texts = []
                    _log(f"[{pdf_name}] p{page_number}/{total_pages} plumber lines done. Starting plumber extract_words")
                    for word in (plumber_page.extract_words() or []):
                        plumber_texts.append({
                            "content": word.get("text", ""),
                            "top": round(word["top"], 2),
                            "left": round(word["x0"], 2),
                            "right": round(word["x1"], 2),
                            "bottom": round(word["bottom"], 2),
                            "width": round(word["x1"] - word["x0"], 2),
                            "height": round(word["bottom"] - word["top"], 2),
                            "rel_x0": round(word["x0"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y0": round(word["top"] / pdf_height, 6) if pdf_height else 0.0,
                            "rel_x1": round(word["x1"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y1": round(word["bottom"] / pdf_height, 6) if pdf_height else 0.0,
                        })
                    page_data["plumber_texts"] = plumber_texts

                    # ── PDFPlumber images ──
                    plumber_images = []
                    for img in (plumber_page.images or []):
                        plumber_images.append({
                            "x0": img["x0"], "y0": img["top"],
                            "x1": img["x1"], "y1": img["bottom"],
                            "width": round(img["x1"] - img["x0"], 2),
                            "height": round(img["bottom"] - img["top"], 2),
                            "rel_x0": round(img["x0"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y0": round(img["top"] / pdf_height, 6) if pdf_height else 0.0,
                            "rel_x1": round(img["x1"] / pdf_width, 6) if pdf_width else 0.0,
                            "rel_y1": round(img["bottom"] / pdf_height, 6) if pdf_height else 0.0,
                        })
                    page_data["plumber_images"] = plumber_images

                    _log(f"[{pdf_name}] p{page_number}/{total_pages} plumber words done. Starting fitz text")
                    # ── PyMuPDF text extraction (actual font metrics) ──
                    fitz_page = fitz_doc[page_index]
                    fitz_width_raw = fitz_page.mediabox.width or 1.0
                    fitz_height_raw = fitz_page.mediabox.height or 1.0
                    page_data["page_dimensions"]["fitz"] = {
                        "width": fitz_width_raw, "height": fitz_height_raw,
                    }

                    # Detect page rotation. fitz mediabox is the raw un-rotated box.
                    # page.rect accounts for rotation and matches plumber dimensions.
                    # Drawings use mediabox coordinates, so we need to transform them.
                    fitz_rotation = fitz_page.rotation
                    fitz_rect = fitz_page.rect  # rotated dimensions (matches plumber)
                    fitz_width = fitz_rect.width or 1.0
                    fitz_height = fitz_rect.height or 1.0

                    # Transform fitz mediabox coordinates to page.rect (rotated) coordinates.
                    # Verified empirically: for rotation=90, new_x = mediabox_height - old_y, new_y = old_x
                    mw = fitz_width_raw   # mediabox width (short side for landscape)
                    mh = fitz_height_raw  # mediabox height (long side for landscape)

                    def transform_fitz_point(x, y):
                        if fitz_rotation == 0:
                            return x, y
                        elif fitz_rotation == 90:
                            return mh - y, x
                        elif fitz_rotation == 180:
                            return mw - x, mh - y
                        elif fitz_rotation == 270:
                            return y, mw - x
                        return x, y

                    def transform_fitz_rect_coords(x0, y0, x1, y1):
                        if fitz_rotation == 0:
                            return x0, y0, x1, y1
                        tx0, ty0 = transform_fitz_point(x0, y0)
                        tx1, ty1 = transform_fitz_point(x1, y1)
                        return min(tx0, tx1), min(ty0, ty1), max(tx0, tx1), max(ty0, ty1)

                    # Extract text with dict mode for real font sizes
                    fitz_text_dict = fitz_page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
                    fitz_texts = []
                    for block in fitz_text_dict.get("blocks", []):
                        if block.get("type") != 0:  # text blocks only
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                content = span.get("text", "").strip()
                                if not content:
                                    continue
                                # get_text("dict") returns mediabox coords on rotated pages
                                # — apply transform
                                bbox = span.get("bbox", (0, 0, 0, 0))
                                ftx0, fty0, ftx1, fty1 = transform_fitz_rect_coords(bbox[0], bbox[1], bbox[2], bbox[3])
                                flags = span.get("flags", 0)
                                fitz_texts.append({
                                    "content": content,
                                    "top": round(fty0, 2),
                                    "left": round(ftx0, 2),
                                    "right": round(ftx1, 2),
                                    "bottom": round(fty1, 2),
                                    "width": round(ftx1 - ftx0, 2),
                                    "height": round(fty1 - fty0, 2),
                                    "font_size": round(span.get("size", 0), 2),
                                    "font_name": span.get("font", ""),
                                    "font_color_int": span.get("color", 0),
                                    "font_color_hex": "#{:06x}".format(span.get("color", 0)),
                                    "is_bold": bool(flags & 2**4),   # bit 4 = bold
                                    "is_italic": bool(flags & 2**1), # bit 1 = italic
                                    "is_monospace": bool(flags & 2**3),
                                    "is_serif": bool(flags & 2**0),
                                })
                    page_data["fitz_texts"] = fitz_texts

                    # ── Font size correction per page ──
                    # Match XML texts to fitz texts by content, compute
                    # the XML/fitz size ratio, then correct all XML sizes.
                    ratios = []
                    for xt in page_data["xml_texts"]:
                        xc = xt["content"].strip()
                        if not xc or xt["font_size_pt"] <= 0:
                            continue
                        for ft in fitz_texts:
                            fc = ft["content"].strip()
                            if not fc or ft["font_size"] <= 0:
                                continue
                            if xc == fc or (len(xc) >= 5 and xc in fc) or (len(fc) >= 5 and fc in xc):
                                ratios.append(xt["font_size_pt"] / ft["font_size"])
                                break

                    if ratios:
                        correction_ratio = sum(ratios) / len(ratios)
                    else:
                        correction_ratio = 1.0

                    page_data["font_size_correction"] = {
                        "ratio": round(correction_ratio, 4),
                        "matched_pairs": len(ratios),
                    }

                    # Apply correction to all XML texts
                    if correction_ratio > 0 and abs(correction_ratio - 1.0) > 0.01:
                        for xt in page_data["xml_texts"]:
                            if xt["font_size_pt"] > 0:
                                xt["font_size_pt"] = round(xt["font_size_pt"] / correction_ratio, 2)

                    # ── Bbox correction using pdfplumber chars (ground truth) ──
                    # pdftohtml and fitz can both have wrong bboxes on rotated pages.
                    # pdfplumber chars always have correct page-space coordinates.
                    # For each XML text, find matching chars and compute correct bbox.
                    if fitz_rotation != 0:
                        all_chars = plumber_page.chars or []

                        for xt in page_data["xml_texts"]:
                            xc = xt["content"].strip()
                            if not xc or len(xc) < 2:
                                continue

                            # Find the first char of xc in plumber chars near the XML top position
                            first_char = xc[0]
                            candidates = []
                            for ci, ch in enumerate(all_chars):
                                if ch["text"] == first_char:
                                    # Check remaining chars match sequentially
                                    matched = True
                                    match_chars = [ch]
                                    ci2 = ci + 1
                                    for xci in range(1, len(xc)):
                                        # Skip spaces in plumber chars
                                        while ci2 < len(all_chars) and all_chars[ci2]["text"] == " " and xc[xci] != " ":
                                            ci2 += 1
                                        if ci2 >= len(all_chars) or all_chars[ci2]["text"] != xc[xci]:
                                            matched = False
                                            break
                                        match_chars.append(all_chars[ci2])
                                        ci2 += 1
                                    if matched and len(match_chars) >= len(xc.replace(" ", "")):
                                        bbox_x0 = min(c["x0"] for c in match_chars)
                                        bbox_top = min(c["top"] for c in match_chars)
                                        bbox_x1 = max(c["x1"] for c in match_chars)
                                        bbox_bottom = max(c["bottom"] for c in match_chars)
                                        dist = abs(xt["top"] - bbox_top) + abs(xt["left"] - bbox_x0)
                                        candidates.append((dist, bbox_x0, bbox_top, bbox_x1, bbox_bottom))

                            if candidates:
                                candidates.sort()
                                _, bx0, bt, bx1, bb = candidates[0]
                                xt["left"] = round(bx0, 2)
                                xt["top"] = round(bt, 2)
                                xt["right"] = round(bx1, 2)
                                xt["bottom"] = round(bb, 2)
                                xt["width"] = round(bx1 - bx0, 2)
                                xt["height"] = round(bb - bt, 2)
                                xt["left_anchor"] = xt["left"]
                                xt["right_anchor"] = xt["right"]
                                xt["center_anchor"] = round((bx0 + bx1) / 2, 2)
                                xt["top_anchor"] = xt["top"]
                                xt["bottom_anchor"] = xt["bottom"]
                                xt["midpoint_y"] = round((bt + bb) / 2, 2)
                                xt["baseline_y"] = xt["bottom"]
                                xt["rel_top"] = round(bt / pdf_height, 6) if pdf_height else 0.0
                                xt["rel_left"] = round(bx0 / pdf_width, 6) if pdf_width else 0.0
                                xt["rel_bottom"] = round(bb / pdf_height, 6) if pdf_height else 0.0
                                xt["rel_right"] = round(bx1 / pdf_width, 6) if pdf_width else 0.0
                                xt["rel_center_x"] = round(xt["center_anchor"] / pdf_width, 6) if pdf_width else 0.0
                                xt["rel_midpoint_y"] = round(xt["midpoint_y"] / pdf_height, 6) if pdf_height else 0.0

                    # ── Fill empty XML text content from all sources ──
                    # pdftohtml sometimes creates text elements with correct position
                    # but empty content on rotated pages. Fill from fitz and pdfplumber.
                    plumber_words = plumber_page.extract_words() or []
                    for xt in page_data["xml_texts"]:
                        if xt["content"].strip():
                            continue
                        # Ghost spans have near-zero width — skip filling them.
                        # A real empty span that needs filling is always wide enough
                        # to hold whatever text we'd fill it with.
                        if xt["width"] <= 3.0:
                            continue
                        filled = False
                        # Try fitz_texts first
                        for ft in fitz_texts:
                            if not ft["content"].strip():
                                continue
                            if (abs(xt["top"] - ft["top"]) < 5 and
                                abs(xt["left"] - ft["left"]) < 5 and
                                abs(xt["width"] - ft["width"]) < 10):
                                xt["content"] = ft["content"]
                                filled = True
                                break
                        if filled:
                            continue
                        # Try pdfplumber words
                        for pw in plumber_words:
                            if not pw["text"].strip():
                                continue
                            if (abs(xt["top"] - pw["top"]) < 5 and
                                abs(xt["left"] - pw["x0"]) < 5):
                                xt["content"] = pw["text"]
                                filled = True
                                break
                        if filled:
                            continue
                        # Try pdfminer — build text from chars in the bbox area
                        for ft2 in fitz_texts:
                            if not ft2["content"].strip():
                                continue
                            if (abs(xt["top"] - ft2["top"]) < 10 and
                                abs(xt["left"] - ft2["left"]) < 10):
                                xt["content"] = ft2["content"]
                                break

                    # ── Trust-no-one cross-check: merge fitz_texts pdftohtml missed ──
                    # pdftohtml -xml is blind to per-element rotated text (Tm matrix
                    # with non-zero b/c components on an upright page). fitz captures
                    # those with correct content. For each fitz_text whose bbox isn't
                    # already covered by xml_texts, synthesize a new xml_text entry so
                    # the rotated text flows through words[] / chars[] / overlays like
                    # every other line.
                    def _bbox_overlap_area(a, b):
                        x0 = max(a[0], b[0]); y0 = max(a[1], b[1])
                        x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
                        if x0 >= x1 or y0 >= y1: return 0.0
                        return (x1 - x0) * (y1 - y0)

                    _existing = [(xt["left"], xt["top"], xt["right"], xt["bottom"])
                                 for xt in page_data["xml_texts"]]
                    _added = 0
                    for ft in fitz_texts:
                        if not ft["content"].strip():
                            continue
                        ft_bbox = (ft["left"], ft["top"], ft["right"], ft["bottom"])
                        ft_area = max(0.0, (ft_bbox[2] - ft_bbox[0]) * (ft_bbox[3] - ft_bbox[1]))
                        if ft_area <= 0:
                            continue
                        covered = sum(_bbox_overlap_area(ft_bbox, xb) for xb in _existing)
                        if covered / ft_area >= 0.5:
                            continue
                        w = ft["right"] - ft["left"]
                        h = ft["bottom"] - ft["top"]
                        # Rotation heuristic from aspect ratio. Default 90 (CCW) which
                        # is the common "US legal label" orientation. For a CW-rotated
                        # document this would be wrong — but per-element CW rotation is
                        # rare enough that one default gets >95% coverage.
                        rotation = 90 if h > w * 1.5 and h > 6 else 0
                        ft_left = ft["left"]; ft_top = ft["top"]
                        ft_right = ft["right"]; ft_bottom = ft["bottom"]
                        ft_cx = (ft_left + ft_right) / 2
                        ft_my = (ft_top + ft_bottom) / 2
                        new_xt = {
                            "content":      ft["content"],
                            "top":          ft_top,
                            "left":         ft_left,
                            "width":        ft["width"],
                            "height":       ft["height"],
                            "bottom":       ft_bottom,
                            "right":        ft_right,
                            "left_anchor":   ft_left,
                            "right_anchor":  ft_right,
                            "center_anchor": round(ft_cx, 2),
                            "top_anchor":    ft_top,
                            "bottom_anchor": ft_bottom,
                            "midpoint_y":    round(ft_my, 2),
                            "baseline_y":    ft_bottom,
                            "font":          "fitz_fallback",
                            "font_size_pt":  ft.get("font_size", 0) or 0,
                            "font_family":   ft.get("font_name", "Arial"),
                            "font_color":    ft.get("font_color_hex", "#000000"),
                            "is_bold":       ft.get("is_bold", False),
                            "is_italic":     ft.get("is_italic", False),
                            "rel_top":       round(ft_top    / pdf_height, 6) if pdf_height else 0.0,
                            "rel_left":      round(ft_left   / pdf_width,  6) if pdf_width  else 0.0,
                            "rel_bottom":    round(ft_bottom / pdf_height, 6) if pdf_height else 0.0,
                            "rel_right":     round(ft_right  / pdf_width,  6) if pdf_width  else 0.0,
                            "rel_center_x":  round(ft_cx / pdf_width,  6) if pdf_width  else 0.0,
                            "rel_midpoint_y":round(ft_my / pdf_height, 6) if pdf_height else 0.0,
                            "source":        "fitz_fallback",
                            "rotation":      rotation,
                        }
                        page_data["xml_texts"].append(new_xt)
                        _existing.append(ft_bbox)
                        _added += 1
                    if _added:
                        _log(f"[{pdf_name}] p{page_number}: +{_added} xml_texts from fitz (rotated/missed)")

                    # ── Pass 4 (v2): per-character pixel ghost detection ──
                    # Renders the page at 300 DPI, then for each xml_text tests
                    # ink density at each character's own bbox from fitz
                    # rawdict. A character whose tight bbox has <6% local-
                    # contrast ink is unpainted; an xml_text with <30% of its
                    # characters painted is a ghost. If fitz rawdict has no
                    # matching span AND the xml_text isn't an ocr_fallback
                    # entry, mark ghost (pdftohtml sees text, fitz doesn't,
                    # region isn't image-only -> no glyph rendered).
                    #
                    # OCR is not run at all. PaddleOCR is unreliable on
                    # small / white-on-coloured / isolated text and its
                    # output was being stored without validation (e.g.
                    # logo "AJBell" → "A]Bell" at 0.97 confidence). For
                    # custodian statements the text layer is always
                    # present, so OCR-recovered logo text is not worth
                    # polluting viz_data with.
                    ocr_texts = []
                    page_data["ocr_texts"] = ocr_texts

                    _GHOST_DPI = 300
                    _GHOST_ZOOM = _GHOST_DPI / 72.0
                    _CONTRAST_DELTA = 30         # per-pixel contrast for ink fraction
                    _CHAR_INK_MIN = 0.06         # 6% pixel-fraction threshold
                    _CHAR_PEAK_MIN = 80          # OR: any pixel this much darker/lighter
                                                 # than background counts the char painted.
                                                 # Catches thin glyphs (dashes, periods, etc.)
                                                 # whose pixel fraction is naturally low.
                    _CHAR_RATIO_GHOST = 0.30

                    # Render luminance once per page
                    try:
                        _pix = fitz_page.get_pixmap(matrix=fitz.Matrix(_GHOST_ZOOM, _GHOST_ZOOM), alpha=False)
                        _mode = "RGB" if _pix.n >= 3 else "L"
                        _img = _PILImage.frombytes(_mode, (_pix.width, _pix.height), _pix.samples)
                        if _mode == "RGB":
                            _img = _img.convert("L")
                        _lum = np.asarray(_img, dtype=np.uint8)
                    except Exception as _exc:
                        _log(f"[{pdf_name}] p{page_number}: pixel render failed, skipping ghost pass — {type(_exc).__name__}: {_exc}")
                        _lum = None

                    # fitz rawdict returns bboxes in UNROTATED PDF coords on
                    # pages with rotation!=0. pdftohtml xml_texts are in the
                    # DISPLAY (rotated) coord space. Apply the same
                    # transform_fitz_rect_coords used elsewhere in this file
                    # so the two coord systems agree before we compute IoU
                    # or index into the rendered luminance array.
                    # _fitz_spans entries: (span_bbox, [(char_bbox, char_text), ...])
                    # Storing the character text lets the ghost loop skip
                    # whitespace characters when computing the inked ratio —
                    # pdftohtml sometimes emits xml_texts whose matched fitz
                    # span is padded with leading whitespace characters that
                    # correctly register as "no ink" and drag the ratio below
                    # threshold, producing false ghosts (e.g. Vanguard "0.00",
                    # Morgan Stanley emails).
                    _fitz_spans = []
                    try:
                        for _blk in fitz_page.get_text("rawdict")["blocks"]:
                            if _blk.get("type") != 0:
                                continue
                            for _ln in _blk.get("lines", []):
                                for _sp in _ln.get("spans", []):
                                    _sb_raw = _sp.get("bbox") or (0, 0, 0, 0)
                                    _sb = transform_fitz_rect_coords(*_sb_raw)
                                    _cbs = []
                                    for _c in (_sp.get("chars") or []):
                                        _cb_raw = _c.get("bbox") or (0, 0, 0, 0)
                                        _cb = transform_fitz_rect_coords(*_cb_raw)
                                        _ctext = _c.get("c", "")
                                        _cbs.append((_cb, _ctext))
                                    if _cbs:
                                        _fitz_spans.append((_sb, _cbs))
                    except Exception as _exc:
                        _log(f"[{pdf_name}] p{page_number}: fitz rawdict failed — {type(_exc).__name__}: {_exc}")

                    def _char_painted(_cb):
                        """Character is considered painted if EITHER its bbox has
                        >=6% pixels with local-contrast ink (solid glyphs) OR
                        any single pixel is >=80/255 away from local background
                        (thin glyphs like dashes / dots / underscores whose
                        pixel fraction is naturally small)."""
                        l, t, r, b = _cb
                        x0 = max(0, int(l * _GHOST_ZOOM) - 1)
                        y0 = max(0, int(t * _GHOST_ZOOM) - 1)
                        x1 = min(_lum.shape[1], int(r * _GHOST_ZOOM) + 1)
                        y1 = min(_lum.shape[0], int(b * _GHOST_ZOOM) + 1)
                        if x1 <= x0 or y1 <= y0:
                            return False
                        region = _lum[y0:y1, x0:x1]
                        if region.size == 0:
                            return False
                        H, W = _lum.shape
                        rings = []
                        top_r = _lum[max(0, y0 - 2):y0, x0:x1]
                        bot_r = _lum[y1:min(H, y1 + 2), x0:x1]
                        lft_r = _lum[y0:y1, max(0, x0 - 2):x0]
                        rgt_r = _lum[y0:y1, x1:min(W, x1 + 2)]
                        for _r in (top_r, bot_r, lft_r, rgt_r):
                            if _r.size:
                                rings.append(_r.reshape(-1))
                        bg = int(np.median(np.concatenate(rings))) if rings else int(np.median(region))
                        diff = np.abs(region.astype(np.int16) - bg)
                        if diff.max() >= _CHAR_PEAK_MIN:
                            return True
                        return float((diff >= _CONTRAST_DELTA).mean()) >= _CHAR_INK_MIN

                    def _best_fitz_span(_xbbox):
                        xl, xt_, xr, xb_ = _xbbox
                        best = None
                        best_iou = 0.0
                        for _sb, _cbs in _fitz_spans:
                            sl, st_, sr, sbb = _sb
                            il = max(xl, sl); it_ = max(xt_, st_)
                            ir = min(xr, sr); ib = min(xb_, sbb)
                            if ir <= il or ib <= it_:
                                continue
                            inter = (ir - il) * (ib - it_)
                            union = (xr - xl) * (xb_ - xt_) + (sr - sl) * (sbb - st_) - inter
                            iou = inter / union if union > 0 else 0.0
                            if iou > best_iou:
                                best_iou = iou
                                best = _cbs
                        return best

                    _ghost_count = 0
                    _page_w = pdf_width or 0
                    _page_h = pdf_height or 0
                    if _lum is not None:
                        for xt in page_data["xml_texts"]:
                            xt_content = (xt.get("content") or "").strip()
                            if not xt_content:
                                continue
                            _src = xt.get("source", "pdftohtml")
                            _xl = xt["left"]; _xt = xt["top"]
                            _xr = xt["right"]; _xb = xt["bottom"]
                            # Skip pdftohtml artifacts we can't meaningfully
                            # test: zero/negative width or height, or bbox
                            # entirely outside the page. These come up as
                            # collapsed-bbox duplicates or off-page junk
                            # (see AJ Bell p23, Fidelity 'S', Vanguard 'L').
                            if _xr - _xl <= 0 or _xb - _xt <= 0:
                                continue
                            if _page_w > 0 and (_xl >= _page_w or _xr <= 0):
                                continue
                            if _page_h > 0 and (_xt >= _page_h or _xb <= 0):
                                continue
                            _xbbox = (_xl, _xt, _xr, _xb)
                            _chars = _best_fitz_span(_xbbox)
                            if _chars is None:
                                # fitz can't see it. Not a ghost only if the
                                # region is an image-only area that OCR picked up.
                                if _src != "ocr_fallback":
                                    xt["is_ghost"] = True
                                    _ghost_count += 1
                            else:
                                # Test ink only on non-whitespace glyphs. fitz
                                # includes leading spaces in spans like
                                # '                0.00'; those spaces have no
                                # ink by definition and shouldn't be counted.
                                _glyph_chars = [cb for (cb, ct) in _chars
                                                if ct and not ct.isspace()]
                                if not _glyph_chars:
                                    # Matched span is whitespace-only — we
                                    # can't say anything about ink for the
                                    # xml_text's claimed content. Skip.
                                    continue
                                _inked = sum(1 for _cb in _glyph_chars if _char_painted(_cb))
                                if _inked / len(_glyph_chars) < _CHAR_RATIO_GHOST:
                                    xt["is_ghost"] = True
                                    _ghost_count += 1

                    # Pass 5 (OCR fallback merge) removed — OCR is no longer
                    # called. If a PDF is image-only (no text layer), it'll
                    # come out empty rather than with unvalidated OCR guesses.
                    page_data["ocr_summary"] = {
                        "ocr_text_count": 0,
                        "ghost_count": _ghost_count,
                        "disagreement_count": 0,
                        "ocr_fallback_added": 0,
                        "ghost_method": "pixel_per_char_v2",
                    }
                    _log(
                        f"[{pdf_name}] p{page_number}: ghost pass v2 — "
                        f"{_ghost_count} ghosts (OCR disabled)"
                    )

                    # Build a page-level ghost_texts view so downstream
                    # consumers don't need to filter xml_texts by is_ghost.
                    # Each entry is self-contained: bbox + content + font
                    # hints + a pointer back into xml_texts.
                    _ghost_texts = []
                    for _xi, _xt in enumerate(page_data["xml_texts"]):
                        if not _xt.get("is_ghost"):
                            continue
                        _ghost_texts.append({
                            "is_ghost":       True,
                            "ghost_reason":   "pixel_per_char_v2: CMap-decoded text with no glyph painted at claimed bbox",
                            "xml_text_index": _xi,
                            "content":        _xt.get("content", ""),
                            "left":           _xt["left"],
                            "top":            _xt["top"],
                            "right":          _xt["right"],
                            "bottom":         _xt["bottom"],
                            "width":          _xt.get("width"),
                            "height":         _xt.get("height"),
                            "source":         _xt.get("source", "pdftohtml"),
                            "font_family":    _xt.get("font_family"),
                            "font_size_pt":   _xt.get("font_size_pt"),
                            "is_bold":        _xt.get("is_bold", False),
                            "is_italic":      _xt.get("is_italic", False),
                        })
                    page_data["ghost_texts"] = _ghost_texts
                    for _gi, _g in enumerate(_ghost_texts, 1):
                        _log(
                            f"[{pdf_name}] p{page_number}: GHOST G{_gi} "
                            f"bbox=({_g['left']:.1f},{_g['top']:.1f},{_g['right']:.1f},{_g['bottom']:.1f}) "
                            f"content={_g['content']!r}"
                        )

                    # Render a debug PNG per page: 150 DPI page image with
                    # every ghost bbox outlined in red and the ghost
                    # content stretched inside the bbox (same rendering
                    # the viewer uses for xml_texts; pdf_qc.js:778-798).
                    try:
                        _dbg_dir = THIS_DIR / f"{pdf_path.stem}_ghost_debug"
                        _dbg_dir.mkdir(exist_ok=True)
                        _DZ = 150 / 72
                        _dpix = fitz_page.get_pixmap(matrix=fitz.Matrix(_DZ, _DZ), alpha=False)
                        _dmode = "RGB" if _dpix.n >= 3 else "L"
                        _dimg = _PILImage.frombytes(_dmode, (_dpix.width, _dpix.height), _dpix.samples)
                        if _dmode != "RGB":
                            _dimg = _dimg.convert("RGB")
                        _dd = _PILImageDraw.Draw(_dimg)
                        for _gi, _g in enumerate(_ghost_texts, 1):
                            _gl = _g["left"] * _DZ
                            _gt = _g["top"] * _DZ
                            _gr = _g["right"] * _DZ
                            _gb = _g["bottom"] * _DZ
                            if _gr - _gl < 2: _gr = _gl + 2
                            if _gb - _gt < 2: _gb = _gt + 2
                            _dd.rectangle([_gl, _gt, _gr, _gb], outline="red", width=2)
                            _content = (_g.get("content") or "").strip()
                            if not _content:
                                continue
                            _box_w = max(6, int(round(_gr - _gl)))
                            _box_h = max(6, int(round(_gb - _gt)))
                            try:
                                _font_name = "arialbd.ttf" if _g.get("is_bold") else (
                                    "ariali.ttf" if _g.get("is_italic") else "arial.ttf")
                                _f = _PILImageFont.truetype(_font_name, max(6, _box_h))
                            except Exception:
                                _f = _PILImageFont.load_default()
                            _tmp = _PILImage.new("RGBA", (4, 4), (0, 0, 0, 0))
                            _tb = _PILImageDraw.Draw(_tmp).textbbox((0, 0), _content, font=_f)
                            _mw = max(1, _tb[2] - _tb[0])
                            _mh = max(1, _tb[3] - _tb[1])
                            _strip = _PILImage.new("RGBA", (_mw + 4, _mh + 4), (0, 0, 0, 0))
                            _PILImageDraw.Draw(_strip).text(
                                (-_tb[0] + 2, -_tb[1] + 2), _content,
                                fill=(230, 0, 0, 255), font=_f)
                            _scaled = _strip.resize((_box_w, _strip.size[1]), _PILImage.LANCZOS)
                            _py = int(round(_gb - _box_h * 0.12 - _scaled.size[1] + 2))
                            _dimg.paste(_scaled, (int(round(_gl)), _py), _scaled)
                            _dd.rectangle([_gl, _gt, _gr, _gb], outline="red", width=2)
                        _dimg.save(_dbg_dir / f"page_{page_number:02d}.png")
                    except Exception as _dbg_exc:
                        _log(f"[{pdf_name}] p{page_number}: ghost debug render skipped — {type(_dbg_exc).__name__}: {_dbg_exc}")

                    _log(f"[{pdf_name}] p{page_number}/{total_pages} fitz text done. Starting fitz images")
                    # ── PyMuPDF images (extract bytes + save as files) ──
                    images_dir = THIS_DIR / f"{pdf_path.stem}_images"
                    fitz_images = []
                    for img_idx, img_info in enumerate(fitz_page.get_image_info(xrefs=True)):
                        bbox = img_info.get("bbox")
                        xref = img_info.get("xref", 0)
                        if not bbox:
                            continue
                        # xref=0 = inline/Form XObject — no extractable bytes but still record bbox
                        tx0, ty0, tx1, ty1 = transform_fitz_rect_coords(bbox[0], bbox[1], bbox[2], bbox[3])
                        img_entry = {
                            "x0": round(tx0, 2),
                            "y0": round(ty0, 2),
                            "x1": round(tx1, 2),
                            "y1": round(ty1, 2),
                            "width": img_info.get("width", 0),
                            "height": img_info.get("height", 0),
                            "rel_x0": round(tx0 / fitz_width, 6) if fitz_width else 0.0,
                            "rel_y0": round(ty0 / fitz_height, 6) if fitz_height else 0.0,
                            "rel_x1": round(tx1 / fitz_width, 6) if fitz_width else 0.0,
                            "rel_y1": round(ty1 / fitz_height, 6) if fitz_height else 0.0,
                        }
                        # Extract and save image file
                        try:
                            img_filename = f"p{page_number}_img{img_idx}.png"
                            images_dir.mkdir(exist_ok=True)
                            img_path = images_dir / img_filename

                            if xref == 0:
                                # Inline / Form XObject — no raw bytes extractable.
                                # Clip-render the bbox region at 3× for quality.
                                clip = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
                                mat  = fitz.Matrix(3, 3)
                                pix  = fitz_page.get_pixmap(matrix=mat, clip=clip, alpha=True)
                                pix.save(str(img_path))
                                img_entry["image_file"] = img_filename
                                img_entry["image_dir"]  = images_dir.name
                                img_entry["inline"]     = True
                            else:
                                img_data = fitz_doc.extract_image(xref)
                                if img_data and img_data.get("image"):
                                    smask_xref = img_data.get("smask", 0)
                                    if smask_xref:
                                        from PIL import Image as PILImage
                                        import io
                                        base_img = PILImage.open(io.BytesIO(img_data["image"]))
                                        base_img = base_img.convert("RGBA")
                                        mask_data = fitz_doc.extract_image(smask_xref)
                                        if mask_data and mask_data.get("image"):
                                            mask_img = PILImage.open(io.BytesIO(mask_data["image"])).convert("L")
                                            if mask_img.size != base_img.size:
                                                mask_img = mask_img.resize(base_img.size)
                                            base_img.putalpha(mask_img)
                                        base_img.save(img_path, "PNG")
                                    else:
                                        with open(img_path, "wb") as img_f:
                                            img_f.write(img_data["image"])
                                    img_entry["image_file"] = img_filename
                                    img_entry["image_dir"]  = images_dir.name
                        except Exception:
                            pass  # silently skip unextractable images
                        fitz_images.append(img_entry)
                    page_data["fitz_images"] = fitz_images

                    _log(f"[{pdf_name}] p{page_number}/{total_pages} fitz images done. Starting fitz drawings")
                    # ── PyMuPDF rectangles + lines ──

                    fitz_lines_raw = []
                    for drawing in fitz_page.get_drawings():
                        rect = drawing.get("rect")
                        fill_color = drawing.get("fill")
                        stroke_color = drawing.get("color")
                        if rect:
                            tx0, ty0, tx1, ty1 = transform_fitz_rect_coords(rect.x0, rect.y0, rect.x1, rect.y1)
                            rect_entry = {
                                "x0": tx0, "y0": ty0,
                                "x1": tx1, "y1": ty1,
                                "rel_x0": round(tx0 / fitz_width, 6) if fitz_width else 0.0,
                                "rel_y0": round(ty0 / fitz_height, 6) if fitz_height else 0.0,
                                "rel_x1": round(tx1 / fitz_width, 6) if fitz_width else 0.0,
                                "rel_y1": round(ty1 / fitz_height, 6) if fitz_height else 0.0,
                            }
                            if fill_color is not None:
                                rect_entry["fill_color"] = [round(c, 4) for c in fill_color]
                            if stroke_color is not None:
                                rect_entry["stroke_color"] = [round(c, 4) for c in stroke_color]
                            page_data["fitz_rects"].append(rect_entry)
                        # Extract actual line segments from drawing paths
                        line_stroke = drawing.get("color")
                        line_width_val = drawing.get("width") or 1.0
                        for item in drawing.get("items", []):
                            if item[0] == "l":  # line segment
                                p1, p2 = item[1], item[2]
                                tp1x, tp1y = transform_fitz_point(p1.x, p1.y)
                                tp2x, tp2y = transform_fitz_point(p2.x, p2.y)
                                line_entry = {
                                    "x0": tp1x, "y0": tp1y,
                                    "x1": tp2x, "y1": tp2y,
                                    "rel_x0": round(tp1x / fitz_width, 6) if fitz_width else 0.0,
                                    "rel_y0": round(tp1y / fitz_height, 6) if fitz_height else 0.0,
                                    "rel_x1": round(tp2x / fitz_width, 6) if fitz_width else 0.0,
                                    "rel_y1": round(tp2y / fitz_height, 6) if fitz_height else 0.0,
                                    "type": "horizontal" if abs(tp1y - tp2y) < 1.0 else
                                            "vertical" if abs(tp1x - tp2x) < 1.0 else "diagonal",
                                    "stroke_width": round(line_width_val, 2),
                                }
                                if line_stroke is not None:
                                    line_entry["stroke_color"] = [round(c, 4) for c in line_stroke]
                                fitz_lines_raw.append(line_entry)
                    page_data["fitz_lines"] = fitz_lines_raw

                    summary["total_fitz_rects"] += len(page_data["fitz_rects"])

                    # ── Lite: skip pdfminer extract_pages entirely. Stub miner
                    # page-dimensions + empty miner_* lists for schema parity.
                    page_data["page_dimensions"]["miner"] = {
                        "width": page_data["page_dimensions"].get("plumber", {}).get("width", 0.0),
                        "height": page_data["page_dimensions"].get("plumber", {}).get("height", 0.0),
                    }
                    page_data["miner_lines"] = []
                    page_data["miner_texts"] = []
                    page_data["miner_images"] = []
                    summary["total_miner_rects"] += len(page_data["miner_rects"])

                    # ── Split merged text elements at vertical line boundaries ──
                    all_chars = plumber_page.chars or []
                    vlines = []
                    for ln in page_data.get("fitz_lines", []):
                        if ln.get("type") == "vertical" and abs(ln["y1"] - ln["y0"]) > 5:
                            vlines.append({"x": round((ln["x0"]+ln["x1"])/2,2), "y_min": min(ln["y0"],ln["y1"]), "y_max": max(ln["y0"],ln["y1"])})
                    for ln in page_data.get("plumber_lines", []):
                        if ln.get("type") == "vertical" and abs(ln["y1"] - ln["y0"]) > 5:
                            vlines.append({"x": round((ln["x0"]+ln["x1"])/2,2), "y_min": min(ln["y0"],ln["y1"]), "y_max": max(ln["y0"],ln["y1"])})

                    if vlines:
                        split_texts = []
                        for xt in page_data["xml_texts"]:
                            mid_y = xt["midpoint_y"]
                            cuts = sorted([vl["x"] for vl in vlines if xt["left"] < vl["x"] < xt["right"] and vl["y_min"] <= mid_y <= vl["y_max"]])
                            if not cuts:
                                split_texts.append(xt)
                                continue
                            t_chars = sorted([c for c in all_chars if xt["top"]-3 <= c["top"] <= xt["bottom"]+3 and xt["left"]-2 <= c["x0"] <= xt["right"]+2 and c["text"] != ""], key=lambda c: c["x0"])
                            if not t_chars:
                                split_texts.append(xt)
                                continue
                            boundaries = [xt["left"]] + cuts + [xt["right"]+1]
                            any_split = False
                            for si in range(len(boundaries)-1):
                                seg = [c for c in t_chars if boundaries[si]-1 <= c["x0"] < boundaries[si+1]]
                                if not seg: continue
                                content = "".join(c["text"] for c in seg).strip()
                                if not content: continue
                                bx0=min(c["x0"] for c in seg); bx1=max(c["x1"] for c in seg)
                                bt=min(c["top"] for c in seg); bb=max(c["bottom"] for c in seg)
                                sub = dict(xt)
                                sub["content"]=content; sub["left"]=sub["left_anchor"]=round(bx0,2); sub["right"]=sub["right_anchor"]=round(bx1,2)
                                sub["top"]=sub["top_anchor"]=round(bt,2); sub["bottom"]=sub["bottom_anchor"]=round(bb,2)
                                sub["width"]=round(bx1-bx0,2); sub["height"]=round(bb-bt,2)
                                sub["center_anchor"]=round((bx0+bx1)/2,2); sub["midpoint_y"]=round((bt+bb)/2,2); sub["baseline_y"]=sub["bottom"]
                                sub["rel_left"]=round(bx0/pdf_width,6) if pdf_width else 0.0; sub["rel_right"]=round(bx1/pdf_width,6) if pdf_width else 0.0
                                sub["rel_top"]=round(bt/pdf_height,6) if pdf_height else 0.0; sub["rel_bottom"]=round(bb/pdf_height,6) if pdf_height else 0.0
                                sub["rel_center_x"]=round(sub["center_anchor"]/pdf_width,6) if pdf_width else 0.0
                                sub["rel_midpoint_y"]=round(sub["midpoint_y"]/pdf_height,6) if pdf_height else 0.0
                                split_texts.append(sub); any_split=True
                            if not any_split: split_texts.append(xt)
                        page_data["xml_texts"] = split_texts

                    # ── Merge multi-line text within shared cell rectangles ──
                    fitz_rects_all = page_data.get("fitz_rects", [])
                    plumber_rects_all = page_data.get("plumber_rects", [])
                    cell_rects = []
                    for r in fitz_rects_all + plumber_rects_all:
                        rx0=r.get("x0",0); ry0=r.get("y0",0); rx1=r.get("x1",0); ry1=r.get("y1",0)
                        if (rx1-rx0)>15 and (ry1-ry0)>15: cell_rects.append((rx0,ry0,rx1,ry1))
                    unique_cells = []
                    for rc in cell_rects:
                        if not any(abs(rc[0]-u[0])<2 and abs(rc[1]-u[1])<2 and abs(rc[2]-u[2])<2 and abs(rc[3]-u[3])<2 for u in unique_cells):
                            unique_cells.append(rc)

                    if unique_cells:
                        cell_group_id = 0
                        for (rx0,ry0,rx1,ry1) in unique_cells:
                            # Split any text that crosses rx0 (left cell boundary)
                            # e.g. "13 Mar 2026 PineBridge Asia Pacific" crosses x=75.5
                            new_texts = []
                            replaced = False
                            for i, xt in enumerate(page_data["xml_texts"]):
                                if (xt["left"] < rx0 < xt["right"]
                                        and ry0 <= xt["midpoint_y"] <= ry1
                                        and xt["content"].strip()):
                                    t_chars = sorted([c for c in all_chars
                                        if abs(c["top"] - xt["top"]) < 4
                                        and xt["left"]-2 <= c["x0"] <= xt["right"]+2
                                        and c["text"] != ""], key=lambda c: c["x0"])
                                    left_seg  = [c for c in t_chars if c["x0"] <  rx0]
                                    right_seg = [c for c in t_chars if c["x0"] >= rx0]
                                    if left_seg and right_seg:
                                        def _make_sub(xt, seg):
                                            s = dict(xt)
                                            s["content"] = "".join(c["text"] for c in seg).strip()
                                            bx0=min(c["x0"] for c in seg); bx1=max(c["x1"] for c in seg)
                                            bt=min(c["top"] for c in seg); bb=max(c["bottom"] for c in seg)
                                            s["left"]=s["left_anchor"]=round(bx0,2); s["right"]=s["right_anchor"]=round(bx1,2)
                                            s["top"]=s["top_anchor"]=round(bt,2); s["bottom"]=s["bottom_anchor"]=round(bb,2)
                                            s["width"]=round(bx1-bx0,2); s["height"]=round(bb-bt,2)
                                            s["center_anchor"]=round((bx0+bx1)/2,2); s["midpoint_y"]=round((bt+bb)/2,2); s["baseline_y"]=s["bottom"]
                                            s["rel_left"]=round(bx0/pdf_width,6) if pdf_width else 0.0; s["rel_right"]=round(bx1/pdf_width,6) if pdf_width else 0.0
                                            s["rel_top"]=round(bt/pdf_height,6) if pdf_height else 0.0; s["rel_bottom"]=round(bb/pdf_height,6) if pdf_height else 0.0
                                            s["rel_center_x"]=round(s["center_anchor"]/pdf_width,6) if pdf_width else 0.0
                                            s["rel_midpoint_y"]=round(s["midpoint_y"]/pdf_height,6) if pdf_height else 0.0
                                            return s
                                        new_texts.append(_make_sub(xt, left_seg))
                                        new_texts.append(_make_sub(xt, right_seg))
                                        replaced = True
                                        continue
                                new_texts.append(xt)
                            if replaced:
                                page_data["xml_texts"] = new_texts

                            # Exclude ghost xml_texts so cell_merged_value is
                            # ghost-free. Otherwise downstream consumers that
                            # read merged_value (rather than per-xml_text
                            # content) still see CMap-claimed strings like
                            # "B08150S" that aren't actually painted.
                            inside_idx = [i for i,xt in enumerate(page_data["xml_texts"]) if rx0<=xt["left"] and xt["right"]<=rx1+2 and ry0<=xt["midpoint_y"]<=ry1 and xt["content"].strip() and not xt.get("is_ghost")]
                            if len(inside_idx)<2: continue
                            inside = [page_data["xml_texts"][i] for i in inside_idx]
                            y_vals = [xt["midpoint_y"] for xt in inside]
                            if max(y_vals)-min(y_vals) < ROW_CLUSTER_TOLERANCE*2: continue
                            sorted_in = sorted(inside, key=lambda t: (t["midpoint_y"], t["left"]))
                            combined = " ".join(t["content"].strip() for t in sorted_in)
                            for xt in inside:
                                xt["cell_group"] = cell_group_id
                                xt["cell_merged_value"] = combined
                            cell_group_id += 1

                    # ── Per-xml_text word index (rich) ──
                    # For each xml_text, attach a words[] list where every word has
                    # the same field set as a viz_data text element: bbox + anchors
                    # + midpoint/baseline + rel coords + inherited font info.
                    # extract_words(return_chars=True) hands us each word plus
                    # the exact chars pdfplumber built it from — ligature-
                    # expanded, zero reconstruction mismatch. Per-char bboxes
                    # flow straight into the viz_data words[] below, enabling
                    # pixel-accurate partial-word selection downstream.
                    try:
                        # line_dir_rotated + char_dir_rotated make pdfplumber read
                        # CCW-rotated (90°) text in page-paint order rather than
                        # x0-sorted (which reverses the word to e.g. "LANOITAGERGNOC").
                        pw_raw = plumber_page.extract_words(
                            return_chars=True,
                            line_dir_rotated="ltr",
                            char_dir_rotated="btt",
                        ) or []
                    except Exception:
                        pw_raw = []

                    # Strip only TRAILING punctuation per word (".", ",", ";",
                    # ":", "!", "?"). Preserves inline punctuation that is part
                    # of the word itself (e.g. "wenshin@avallis.com" stays
                    # intact; "Shin," splits into "Shin" + ",").
                    TRAIL_PUNCT = {'.', ',', ';', ':', '!', '?'}
                    pw_all = []
                    for w in pw_raw:
                        text = w.get("text", "")
                        if not text:
                            pw_all.append(w); continue
                        trail = 0
                        for ch in reversed(text):
                            if ch in TRAIL_PUNCT:
                                trail += 1
                            else:
                                break
                        if trail == 0 or trail == len(text):
                            pw_all.append(w); continue  # keep as-is
                        # Split the word's own chars[] — no x-range guessing.
                        wchars = w.get("chars") or []
                        if len(wchars) < trail + 1 or len(wchars) != len(text):
                            pw_all.append(w); continue  # can't split reliably
                        main_chars  = wchars[:-trail]
                        trail_chars = wchars[-trail:]
                        pw_all.append({
                            "text":   "".join(c.get("text", "") for c in main_chars),
                            "x0":     min(c.get("x0", 0)     for c in main_chars),
                            "x1":     max(c.get("x1", 0)     for c in main_chars),
                            "top":    min(c.get("top", 0)    for c in main_chars),
                            "bottom": max(c.get("bottom", 0) for c in main_chars),
                            "chars":  main_chars,
                        })
                        pw_all.append({
                            "text":   "".join(c.get("text", "") for c in trail_chars),
                            "x0":     min(c.get("x0", 0)     for c in trail_chars),
                            "x1":     max(c.get("x1", 0)     for c in trail_chars),
                            "top":    min(c.get("top", 0)    for c in trail_chars),
                            "bottom": max(c.get("bottom", 0) for c in trail_chars),
                            "chars":  trail_chars,
                        })
                    TOL_W = 2.0
                    # First pass: stamp each xml_text with id / element_type / page
                    for ti, xt in enumerate(page_data["xml_texts"]):
                        xt["id"]           = ti
                        xt["element_type"] = "text"
                        xt["page"]         = page_number
                    # Second pass: build words[] with full navigation fields
                    for ti, xt in enumerate(page_data["xml_texts"]):
                        # Scale tolerance with the xml_text's font size:
                        # pdfplumber's word bbox includes full glyph extents
                        # (ascenders/descenders) while pdftohtml's xml_text
                        # bbox is closer to the line height. The vertical
                        # discrepancy grows linearly with font size — for a
                        # 34pt heading the word top can sit ~3pt above
                        # xt.top, which a fixed 2pt tolerance rejects.
                        # Use font_size_pt × 0.1 as the per-xml_text floor.
                        _fs = float(xt.get("font_size_pt") or 0)
                        tol = max(TOL_W, 0.1 * _fs)
                        wlist = []
                        for w in pw_all:
                            wx0 = float(w.get("x0", 0))
                            wx1 = float(w.get("x1", 0))
                            wt  = float(w.get("top", 0))
                            wb  = float(w.get("bottom", 0))
                            contained = (
                                xt["left"]   - tol <= wx0 and wx1 <= xt["right"]  + tol
                                and xt["top"] - tol <= wt  and wb  <= xt["bottom"] + tol
                            )
                            src_text  = w.get("text", "")
                            src_chars = w.get("chars") or []
                            if not contained:
                                # Footnote/superscript/marker glyph fallback:
                                # pdftohtml splits "E40591001" + "¹" into two
                                # xml_texts but pdfplumber merges them into one
                                # word "E40591001¹" whose bbox exceeds either
                                # xml_text's bounds — strict containment then
                                # silently drops words[] for BOTH. Filter the
                                # merged word's chars to those that fit inside
                                # this xml_text's bbox; if any contiguous run
                                # remains and is a substring of the original
                                # word text, rebuild a synthetic word from
                                # those chars so each xml_text gets the right
                                # words[] / chars[].
                                kept = [
                                    c for c in src_chars
                                    if (xt["left"]   - tol <= float(c.get("x0", 0))
                                        and float(c.get("x1", 0)) <= xt["right"]  + tol
                                        and xt["top"]    - tol <= float(c.get("top", 0))
                                        and float(c.get("bottom", 0)) <= xt["bottom"] + tol)
                                ]
                                if not kept:
                                    continue
                                kept_text = "".join(c.get("text", "") for c in kept)
                                if not kept_text or kept_text not in src_text:
                                    continue
                                src_text  = kept_text
                                src_chars = kept
                                wx0 = min(float(c.get("x0", 0))     for c in kept)
                                wx1 = max(float(c.get("x1", 0))     for c in kept)
                                wt  = min(float(c.get("top", 0))    for c in kept)
                                wb  = max(float(c.get("bottom", 0)) for c in kept)
                            ww  = wx1 - wx0
                            wh  = wb  - wt
                            cx  = (wx0 + wx1) / 2
                            my  = (wt  + wb) / 2
                            wlist.append({
                                # navigation
                                "element_type":        "word",
                                "page":                page_number,
                                "parent_text_id":      ti,
                                "parent_text_content": xt.get("content", ""),
                                "text":                src_text,
                                # bbox (absolute pt)
                                "left":           round(wx0, 2),
                                "top":            round(wt,  2),
                                "right":          round(wx1, 2),
                                "bottom":         round(wb,  2),
                                "width":          round(ww,  2),
                                "height":         round(wh,  2),
                                # anchors + midpoint/baseline
                                "left_anchor":    round(wx0, 2),
                                "right_anchor":   round(wx1, 2),
                                "center_anchor":  round(cx,  2),
                                "top_anchor":     round(wt,  2),
                                "bottom_anchor":  round(wb,  2),
                                "midpoint_y":     round(my,  2),
                                "baseline_y":     round(wb,  2),
                                # inherited font info
                                "font":           xt.get("font"),
                                "font_color":     xt.get("font_color"),
                                "font_family":    xt.get("font_family"),
                                "font_size_pt":   xt.get("font_size_pt"),
                                "is_bold":        xt.get("is_bold", False),
                                "is_italic":      xt.get("is_italic", False),
                                # rel coords (0-1)
                                "rel_left":       round(wx0 / pdf_width,  6) if pdf_width  else 0.0,
                                "rel_top":        round(wt  / pdf_height, 6) if pdf_height else 0.0,
                                "rel_right":      round(wx1 / pdf_width,  6) if pdf_width  else 0.0,
                                "rel_bottom":     round(wb  / pdf_height, 6) if pdf_height else 0.0,
                                "rel_center_x":   round(cx  / pdf_width,  6) if pdf_width  else 0.0,
                                "rel_midpoint_y": round(my  / pdf_height, 6) if pdf_height else 0.0,
                                # per-codepoint bboxes for partial-word selection
                                "chars":          [{
                                    "text":   c.get("text", ""),
                                    "left":   round(float(c.get("x0", 0)),     3),
                                    "right":  round(float(c.get("x1", 0)),     3),
                                    "top":    round(float(c.get("top", 0)),    3),
                                    "bottom": round(float(c.get("bottom", 0)), 3),
                                } for c in src_chars],
                            })
                        # ── AcroForm / synthetic-fallback path ──
                        # If wlist is still empty but the xml_text has visible
                        # content, the chars come from a source pdfplumber
                        # can't see — typically AcroForm widget appearance
                        # streams (form-field values like names, "0.00",
                        # Yes/No). pdftohtml renders them so the xml_text
                        # exists with content + bbox, but page.chars is
                        # empty at that bbox. To restore word-level selection
                        # and approximate per-char bboxes, synthesise one
                        # word per whitespace-separated token in the content,
                        # distributing chars uniformly across each token's
                        # share of the xml_text bbox. Tagged with
                        # chars_source="synth_uniform" so downstream knows
                        # the per-char bboxes are approximate (good enough
                        # for selection / substring highlighting; not
                        # pixel-accurate vs. a real glyph).
                        _xt_content = (xt.get("content") or "")
                        if not wlist and _xt_content.strip():
                            tokens = _xt_content.split()
                            if tokens:
                                xt_l = float(xt["left"])
                                xt_r = float(xt["right"])
                                xt_t = float(xt["top"])
                                xt_b = float(xt["bottom"])
                                # Allocate width by character count + one
                                # uniform space-unit between tokens.
                                total_units = sum(len(tk) for tk in tokens) + max(0, len(tokens) - 1)
                                unit_w = ((xt_r - xt_l) / total_units) if total_units > 0 else 0.0
                                cursor_x = xt_l
                                for tok in tokens:
                                    n = len(tok)
                                    if n == 0:
                                        continue
                                    tok_l = cursor_x
                                    tok_r = cursor_x + n * unit_w
                                    cursor_x = tok_r + unit_w  # advance over one space
                                    cw = (tok_r - tok_l) / n
                                    synth_chars = [{
                                        "text":   tok[i],
                                        "left":   round(tok_l + i * cw,     3),
                                        "right":  round(tok_l + (i + 1) * cw, 3),
                                        "top":    round(xt_t, 3),
                                        "bottom": round(xt_b, 3),
                                    } for i in range(n)]
                                    ww  = tok_r - tok_l
                                    wh  = xt_b - xt_t
                                    cx  = (tok_l + tok_r) / 2
                                    my  = (xt_t + xt_b) / 2
                                    wlist.append({
                                        "element_type":        "word",
                                        "page":                page_number,
                                        "parent_text_id":      ti,
                                        "parent_text_content": _xt_content,
                                        "text":                tok,
                                        "left":           round(tok_l, 2),
                                        "top":            round(xt_t,  2),
                                        "right":          round(tok_r, 2),
                                        "bottom":         round(xt_b,  2),
                                        "width":          round(ww,    2),
                                        "height":         round(wh,    2),
                                        "left_anchor":    round(tok_l, 2),
                                        "right_anchor":   round(tok_r, 2),
                                        "center_anchor":  round(cx,    2),
                                        "top_anchor":     round(xt_t,  2),
                                        "bottom_anchor":  round(xt_b,  2),
                                        "midpoint_y":     round(my,    2),
                                        "baseline_y":     round(xt_b,  2),
                                        "font":           xt.get("font"),
                                        "font_color":     xt.get("font_color"),
                                        "font_family":    xt.get("font_family"),
                                        "font_size_pt":   xt.get("font_size_pt"),
                                        "is_bold":        xt.get("is_bold", False),
                                        "is_italic":      xt.get("is_italic", False),
                                        "rel_left":       round(tok_l / pdf_width,  6) if pdf_width  else 0.0,
                                        "rel_top":        round(xt_t  / pdf_height, 6) if pdf_height else 0.0,
                                        "rel_right":      round(tok_r / pdf_width,  6) if pdf_width  else 0.0,
                                        "rel_bottom":     round(xt_b  / pdf_height, 6) if pdf_height else 0.0,
                                        "rel_center_x":   round(cx    / pdf_width,  6) if pdf_width  else 0.0,
                                        "rel_midpoint_y": round(my    / pdf_height, 6) if pdf_height else 0.0,
                                        "chars":          synth_chars,
                                        "chars_source":   "synth_uniform",
                                    })
                        wlist.sort(key=lambda w: (w["top"], w["left"]))
                        # Stamp composite ids + word_index in final reading order
                        for wi, ww_dict in enumerate(wlist):
                            ww_dict["word_index"] = wi
                            ww_dict["id"]         = f"{ti}.{wi}"

                        # ── Option D: per-word bbox from pdftotext -bbox-layout ──
                        # Match each word to a poppler word on the same page; if
                        # matched, attach left_d / right_d on the word and on
                        # each char (chars scaled to tile the poppler word bbox).
                        # D runs BEFORE A so both read from raw pdfplumber bounds.
                        # If no match, D fields are absent — consumers fall back
                        # to A (left / right) which always exists.
                        _pop_page = poppler_words_by_page.get(page_number, [])
                        if _pop_page:
                            for ww_dict in wlist:
                                pop = match_poppler_word(
                                    ww_dict["left"], ww_dict["top"],
                                    ww_dict["text"], _pop_page,
                                )
                                if not pop:
                                    continue
                                pL = float(pop["x0"]); pR = float(pop["x1"])
                                ww_dict["left_d"]  = round(pL, 3)
                                ww_dict["right_d"] = round(pR, 3)
                                ww_dict["width_d"] = round(pR - pL, 3)
                                # Scale raw plumber chars to tile [pL, pR]
                                _ch = ww_dict.get("chars") or []
                                if _ch:
                                    src_l = min(c["left"]  for c in _ch)
                                    src_r = max(c["right"] for c in _ch)
                                    src_w = src_r - src_l
                                    tgt_w = pR - pL
                                    if src_w > 0 and tgt_w > 0:
                                        ds = tgt_w / src_w
                                        for c in _ch:
                                            c["left_d"]  = round(pL + (c["left"]  - src_l) * ds, 3)
                                            c["right_d"] = round(pL + (c["right"] - src_l) * ds, 3)

                        # ── Normalize word + char x-extents to the xml_text bbox ──
                        # pdfplumber reports glyph-ink bboxes (visible pixels),
                        # which undershoot render extent by ~0.3-0.5pt/char.
                        # pdftohtml's xml_text bbox uses advance widths (what
                        # actually gets rendered). Proportionally stretch the
                        # internally-consistent pdfplumber layout so the first
                        # word's left hits xt.left and the last word's right
                        # hits xt.right — overlays (whole-word, partial-word,
                        # sub-char) all become render-accurate. Skip multi-row
                        # xml_texts (rare) and rotated xml_texts (the scale
                        # axis differs for rotated text; leave raw bounds).
                        if wlist and not xt.get("rotation"):
                            _tops    = [ww["top"]    for ww in wlist]
                            _bottoms = [ww["bottom"] for ww in wlist]
                            _multi_row = (max(_tops) - min(_tops)) > 2.0 \
                                      or (max(_bottoms) - min(_bottoms)) > 2.0
                            first_left = min(ww["left"]  for ww in wlist)
                            last_right = max(ww["right"] for ww in wlist)
                            span   = last_right - first_left
                            target = xt["right"] - xt["left"]
                            if not _multi_row and span > 0 and target > 0:
                                scale  = target / span
                                origin = first_left
                                anchor = xt["left"]
                                for ww_dict in wlist:
                                    new_l = anchor + (ww_dict["left"]  - origin) * scale
                                    new_r = anchor + (ww_dict["right"] - origin) * scale
                                    new_c = (new_l + new_r) / 2
                                    ww_dict["left"]          = round(new_l, 3)
                                    ww_dict["right"]         = round(new_r, 3)
                                    ww_dict["width"]         = round(new_r - new_l, 3)
                                    ww_dict["left_anchor"]   = ww_dict["left"]
                                    ww_dict["right_anchor"]  = ww_dict["right"]
                                    ww_dict["center_anchor"] = round(new_c, 3)
                                    if pdf_width:
                                        ww_dict["rel_left"]     = round(new_l / pdf_width, 6)
                                        ww_dict["rel_right"]    = round(new_r / pdf_width, 6)
                                        ww_dict["rel_center_x"] = round(new_c / pdf_width, 6)
                                    for c in ww_dict.get("chars") or []:
                                        c["left"]  = round(anchor + (c["left"]  - origin) * scale, 3)
                                        c["right"] = round(anchor + (c["right"] - origin) * scale, 3)

                        xt["words"] = wlist

                    # ── Lite: stub empty alignment artefacts. The matcher
                    # (_find_entity_occurrences) only consumes xml_texts;
                    # row_clusters / column_anchors / alignment_matrix and
                    # the derived geometric_cells / row_bands sidecars are
                    # unused. The row-info attach loop and derived-guides
                    # loop below iterate over these empty collections and
                    # therefore are no-ops; omitted entirely.
                    alignment = {
                        "row_clusters": [],
                        "column_anchors": {"left": [], "right": [], "center": []},
                        "alignment_matrix": [],
                    }
                    page_data["row_clusters"] = alignment["row_clusters"]
                    page_data["column_anchors"] = alignment["column_anchors"]
                    page_data["alignment_matrix"] = alignment["alignment_matrix"]

                    # Lite: derived_guides loop is a no-op (column_anchors +
                    # row_clusters are both empty), so emit the empty list
                    # directly for schema parity with v4's lite=True output.
                    page_data["derived_guides"] = []

                    summary["total_row_clusters"] += len(alignment["row_clusters"])
                    n_col = (len(alignment["column_anchors"]["left"])
                             + len(alignment["column_anchors"]["right"])
                             + len(alignment["column_anchors"]["center"]))
                    summary["total_col_anchors"] += n_col

                    if progress_cb:
                        try:
                            progress_cb(page_number, total_pages)
                        except Exception:
                            pass
                    _log(
                        f"[{pdf_name}] p{page_number}/{total_pages} DONE — "
                        f"{len(page_data['xml_texts'])}t "
                        f"{len(page_data['plumber_rects'])}pr/{len(page_data['fitz_rects'])}fr/{len(page_data['miner_rects'])}mr "
                        f"{len(alignment['row_clusters'])} rows {n_col} cols"
                    )

                    viz_data["pages"][str(page_number)] = page_data
            finally:
                fitz_doc.close()

        with open(json_output_path, "w", encoding="utf-8") as f:
            json.dump(viz_data, f, indent=2, ensure_ascii=False)

        # Surgical clean PDF: remove only ghost text operators, preserve
        # visible text (position-aware match against ghost xml_text bbox).
        # Always emit a <stem>_clean.pdf so downstream can read at a
        # deterministic path.
        try:
            _total_ghosts = sum(
                1 for _p in viz_data["pages"].values()
                for _xt in (_p.get("xml_texts") or [])
                if _xt.get("is_ghost")
            )
            _clean_path = THIS_DIR / f"{pdf_path.stem}_clean.pdf"
            if _total_ghosts > 0:
                _stripped = strip_ghosts_from_pdf(
                    str(pdf_path), str(_clean_path), viz_data,
                )
                _log(f"[{pdf_name}] CLEAN PDF written — surgically removed {_stripped} ghost operator(s) ({_total_ghosts} ghost xml_texts targeted) -> {_clean_path.name}")
            else:
                import shutil as _shutil
                _shutil.copy2(pdf_path, _clean_path)
                _log(f"[{pdf_name}] CLEAN PDF written — 0 ghosts (verbatim copy)")
        except Exception as _strip_exc:
            _log(f"[{pdf_name}] strip_ghosts failed — {type(_strip_exc).__name__}: {_strip_exc}")

        # Ghost debug index.txt: one line per page with its ghost count +
        # bbox + content for every flagged entry. Written next to the
        # per-page debug PNGs.
        try:
            _dbg_dir = THIS_DIR / f"{pdf_path.stem}_ghost_debug"
            if _dbg_dir.exists():
                _idx_lines = []
                _gtotal = 0
                for _pn in sorted(viz_data["pages"], key=int):
                    _p = viz_data["pages"][_pn]
                    _gt_list = _p.get("ghost_texts") or []
                    _idx_lines.append(f"page {int(_pn):02d}: {len(_gt_list)} ghosts")
                    for _gi, _g in enumerate(_gt_list, 1):
                        _c = (_g.get("content") or "").strip()
                        _idx_lines.append(
                            f"  G{_gi}: bbox=({_g['left']:.1f},{_g['top']:.1f},"
                            f"{_g['right']:.1f},{_g['bottom']:.1f}) content={_c!r}")
                    _gtotal += len(_gt_list)
                _idx_lines.append(f"\nTOTAL: {_gtotal}")
                (_dbg_dir / "index.txt").write_text("\n".join(_idx_lines), encoding="utf-8")
        except Exception as _idx_exc:
            _log(f"[{pdf_name}] ghost index.txt write skipped — {type(_idx_exc).__name__}: {_idx_exc}")

        summary["time_seconds"] = round(time.time() - start_time, 1)
        _log(f"[{pdf_name}] COMPLETE — {total_pages}p "
             f"{summary['total_text_nodes']}t "
             f"{summary['total_row_clusters']} rows "
             f"{summary['total_col_anchors']} cols "
             f"{summary['time_seconds']}s")

    except Exception as e:
        import traceback as _tb
        summary["status"] = "ERROR"
        summary["time_seconds"] = round(time.time() - start_time, 1)
        _log(f"[{pdf_name}] ERROR: {e}\n{_tb.format_exc()}")
    finally:
        if xml_file and Path(xml_file).exists():
            try:
                Path(xml_file).unlink()
            except OSError:
                pass

    return summary

