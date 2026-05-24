#!/usr/bin/env python3
"""
pdf_tokenizer_v3.py — In-place PII tokenization with multi-model NER consensus.

Extends v2 with a multi-model approach for PERSON detection to reduce false
positives (e.g. "Quantity Price", "AJ Bell", "Rel Bmark" being tagged as names).

Architecture
------------
  ACCOUNT_NUMBER — pattern-only (Presidio regex, no NLP needed, unchanged from v2)
  PERSON         — multi-model consensus: only entities that 2+ models agree on
                   are accepted. Random financial terms rarely survive consensus.

Supported NER models (all optional except spaCy lg):
  spacy/en_core_web_lg   — required baseline
  spacy/en_core_web_trf  — transformer spaCy (more accurate, slower)
                           pip install spacy && python -m spacy download en_core_web_trf
  dslim/bert-base-NER    — HuggingFace BERT NER
                           pip install transformers torch
  ner-english-large      — Flair NER
                           pip install flair
  stanza en              — Stanford Stanza
                           pip install stanza && python -c "import stanza; stanza.download('en')"

Models that are not installed are silently skipped. The consensus threshold
scales with how many models are loaded: at least min(2, n_loaded) must agree.

Usage
-----
    python pdf_tokenizer_v3.py   (interactive, same as v2)

Requirements (minimum)
----------------------
    pip install pymupdf pikepdf pdfplumber presidio-analyzer spacy
    pip install pdfminer.six
    python -m spacy download en_core_web_lg
    -- pdftohtml must be on PATH (part of poppler-utils) --
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import fitz
import pikepdf
import pdfplumber
# presidio_analyzer is imported lazily inside _build_analyzer — the top-level
# import takes ~15s (registers all built-in recognizers + cascades to spacy)
# and would block Flask startup. Deferred so the cost is paid on first
# analyze/tokenize call, not at process boot.

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ── Category normaliser ──────────────────────────────────────────────────────
# Presidio's entity_type names don't always match the frontend dropdown.
# Normalise here so UI shows the right category and dropdown defaults
# (which silently fall back to the first option if the name is unknown).
_CAT_NORMALISE = {
    "EMAIL_ADDRESS": "EMAIL",
    "SG_PHONE":      "PHONE_NUMBER",
}


def _normalise_category(cat: str) -> str:
    return _CAT_NORMALISE.get(cat, cat)


def _filter_ghosts_and_taint(all_xml_texts: list[dict]) -> list[dict]:
    """Drop ghost xml_texts.

    NOTE: this does NOT scrub cell_merged_value from non-ghost siblings of
    ghosts. If the upstream viz_data extractor (pdf_qc) computed
    cell_merged_value without excluding ghost content, that ghost text
    reaches Presidio via the surviving sibling's merged_value field. That
    is a viz_data extractor bug — it should be fixed at source so
    cell_merged_value never includes ghost content. Tracked in BUG report
    to PB-PDF_QC3.
    """
    return [xt for xt in (all_xml_texts or []) if not xt.get("is_ghost")]


def _phone_match_is_disjoint(occurrences: list) -> bool:
    """True if a PHONE_NUMBER match was assembled from glyph runs that sit
    on the same visual line but with a gap wider than the matched pieces
    themselves. A real phone renders as one continuous glyph run; a "phone"
    that Presidio stitched together from two chart-axis labels ("6000",
    gap, "8000") fails this test.

    Gap is measured against the widths of the adjacent pieces, never a
    hard-coded pt value — a 5-digit pair with a 40pt gap is suspicious,
    but a 20-digit pair with a 40pt gap is legitimate.
    """
    if not occurrences or len(occurrences) < 2:
        return False
    ordered = sorted(
        occurrences,
        key=lambda o: (round(o.get("top", 0)), o.get("left", 0)),
    )
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if abs(a.get("top", 0) - b.get("top", 0)) > 2:
            continue  # different lines — line-broken phones are legitimate
        a_w = max(0.1, a.get("right", 0) - a.get("left", 0))
        b_w = max(0.1, b.get("right", 0) - b.get("left", 0))
        gap = b.get("left", 0) - a.get("right", 0)
        if gap > max(a_w, b_w):
            return True
    return False


def _suppress_contained(pat_results):
    """Drop Presidio results entirely CONTAINED inside another higher-or-equal
    scored result of a different entity type.

    Presidio returns overlapping matches of different types by default:
      PHONE_NUMBER "800-767-7462" (score 0.90)
      ACCOUNT_NUMBER "767-7462"   (score 0.85, substring of the above)
    Both land in agg as separate candidates, which pollutes the review list
    with a fragment of an already-tagged phone. Keep only the "outer" match.
    """
    if not pat_results:
        return []
    # Sort highest-score first, then longest span, so outer wins before
    # any contained is evaluated.
    ordered = sorted(pat_results,
                     key=lambda r: (-r.score, -(r.end - r.start), r.start))
    kept = []
    for r in ordered:
        contained = any(
            k.start <= r.start and r.end <= k.end
            and k.entity_type != r.entity_type
            and k.score >= r.score
            for k in kept
        )
        if not contained:
            kept.append(r)
    return kept


# ── Token registry ────────────────────────────────────────────────────────────

_CAT_PREFIX: dict[str, str] = {
    "PERSON":         "NAME",
    "ACCOUNT_NUMBER": "ACCOUNT",
    "PHONE_NUMBER":   "PHONE",
    "SG_PHONE":       "PHONE",
    "EMAIL":          "EMAIL",
    "EMAIL_ADDRESS":  "EMAIL",
    "ADDRESS":        "ADDRESS",
    "DATE_OF_BIRTH":  "DOB",
    "DATE_TIME":      "DATE",
    "CREDIT_CARD":    "CARD",
    "IBAN_CODE":      "IBAN",
    "URL":            "URL",
    "SG_NRIC":        "NRIC",
    "SG_UEN":         "UEN",
    "ISIN":           "ISIN",
    "OTHER":          "OTHER",
}

_counters: dict[str, int] = {}
_value_to_token: dict[str, str] = {}
_token_to_value: dict[str, str] = {}


def _assign_token(entity_type: str, value: str) -> str:
    norm = value.strip()
    if norm in _value_to_token:
        return _value_to_token[norm]
    prefix = _CAT_PREFIX.get(entity_type, "OTHER")
    _counters[prefix] = _counters.get(prefix, 0) + 1
    tok = f"[{prefix}_{_counters[prefix]}]"
    _value_to_token[norm] = tok
    _token_to_value[tok] = norm
    return tok


# ── Presidio — pattern-only (ACCOUNT_NUMBER) ─────────────────────────────────

def _build_analyzer():
    """Pattern-only analyzer for ACCOUNT_NUMBER. PERSON is handled by multi-model NER.

    presidio imports are done here (not at module top) to keep the
    pdf_tokenizer_v3 module import fast — otherwise Flask boot stalls ~15s
    every time a module imports us, even if we never run tokenization.
    """
    from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    cfg = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    }
    nlp_engine = NlpEngineProvider(nlp_configuration=cfg).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="ACCOUNT_NUMBER",
            patterns=[
                Pattern("DASHED_ACCOUNT", r"\b\d{3,6}-\d{4,8}(?:-\d{1,4})?\b", 0.85),
                Pattern("PREFIX_ACCOUNT", r"\b[A-Z]{2,4}\d{5,12}\b",            0.75),
                # Single-letter prefix + digits + single-letter suffix — AJ
                # Bell portfolio IDs (B08150S, B44034D, etc.) use this format.
                # Lives in repeating page headers where users often miss them
                # when adding manually; without this pattern the recogniser
                # doesn't flag them, so they leak through tokenisation.
                Pattern("LETTER_DIGITS_LETTER", r"\b[A-Z]\d{4,8}[A-Z]\b",       0.70),
                Pattern("PLAIN_ACCOUNT",  r"\b\d{8,12}\b",                       0.60),
            ],
            name="CustomAccountRecognizer",
        )
    )

    # ISIN — public security identifier, not PII. Scored higher than
    # PREFIX_ACCOUNT (0.75) so Presidio picks ISIN for things like
    # "US1729675561" instead of flagging it as an account number.
    # Format: 2-letter country + 9 alphanumeric + 1 check digit = 12 chars.
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="ISIN",
            patterns=[
                Pattern("ISIN_12", r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", 0.95),
            ],
            name="IsinRecognizer",
        )
    )

    # ── SG-specific recognizers ─────────────────────────────────────────
    # Presidio has built-ins for US_SSN / US_ITIN / UK_NHS but nothing for
    # Singapore NRIC/FIN (national ID) or UEN (entity number). Regex is
    # authoritative for these — the checksum digit is deterministic.
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="SG_NRIC",
            patterns=[
                # NRIC/FIN: [STFG] + 7 digits + checksum letter
                Pattern("SG_NRIC_FIN", r"\b[STFGMstfgm]\d{7}[A-Za-z]\b", 0.85),
            ],
            name="SgNricRecognizer",
        )
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="SG_UEN",
            patterns=[
                # UEN: 8-10 digit + letter, various formats
                Pattern("SG_UEN", r"\b(?:\d{8,10}[A-Z]|T\d{2}[A-Z]{2}\d{4}[A-Z])\b", 0.80),
            ],
            name="SgUenRecognizer",
        )
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="SG_PHONE",
            patterns=[
                # SG mobile: starts 8 or 9, 8 digits, with optional +65 prefix
                Pattern("SG_MOBILE", r"(?:\+?65[-\s]?)?[89]\d{3}[-\s]?\d{4}\b", 0.80),
                # SG landline: starts 6, 8 digits, with optional +65 prefix
                Pattern("SG_LANDLINE", r"(?:\+?65[-\s]?)?6\d{3}[-\s]?\d{4}\b", 0.70),
            ],
            name="SgPhoneRecognizer",
        )
    )

    # US phone patterns — common formats used in statements. Scored higher
    # than DASHED_ACCOUNT (0.85) so Presidio's overlap-resolution picks
    # PHONE_NUMBER when e.g. "800-544-1766" could otherwise match the
    # account regex on the 544-1766 suffix.
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="PHONE_NUMBER",
            patterns=[
                # 1-800-544-1766 / 800-544-1766 / 1 (800) 544-1766 / 800.544.1766
                Pattern(
                    "US_TOLLFREE",
                    r"(?:\+?1[\s\-.]?)?(?:\(?(?:800|888|877|866|855|844|833|822)\)?"
                    r"[\s\-.]?\d{3}[\s\-.]?\d{4})\b",
                    0.90,
                ),
                # General US 10-digit: (212) 555-1234 / 212-555-1234 / 212.555.1234
                Pattern(
                    "US_PHONE_10",
                    r"(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}\b",
                    0.90,
                ),
            ],
            name="UsPhoneRecognizer",
        )
    )

    # ── ADDRESS recognizer (SG + US + generic postal anchors) ───────────
    # full_text joins xml_texts with \n, so patterns cross line breaks via
    # \s+ (matches newlines). We anchor on postal/ZIP codes + optional
    # backward-context lines — the full matched substring then becomes the
    # ADDRESS candidate the user can accept / alias / reject.
    _STREET_TYPES = (
        r"(?:Road|Street|Avenue|Ave|Drive|Lane|Place|Crescent|Close|Walk|"
        r"Terrace|Quay|Way|Boulevard|Blvd|Rd|St|Dr|Ln|Pl|Court|Ct|Circle|"
        r"Cir|Parkway|Pkwy|Loop|Row|Alley)"
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="ADDRESS",
            patterns=[
                # Singapore: <num> <street words> <type> (+ #unit-sub)? (+ Singapore)? <6-digit postal>
                Pattern(
                    "SG_FULL_ADDRESS",
                    rf"\b\d+[A-Z]?\s+(?:[A-Za-z]+\s+){{1,6}}{_STREET_TYPES}"
                    r"(?:\s*[,\n]?\s*#\d{1,3}-\d{1,4}\w?)?"
                    r"(?:\s*[,\n]?\s*Singapore)?\s*[,\n]?\s*\d{6}\b",
                    0.85,
                ),
                # Singapore HDB-style: Blk <num> <street> (+ #unit-sub) + postal
                Pattern(
                    "SG_BLK_ADDRESS",
                    r"\bBlk\s+\d+[A-Z]?\s+(?:[A-Za-z0-9]+\s+){1,6}"
                    r"(?:#\d{1,3}-\d{1,4}\w?\s+)?"
                    r"(?:Singapore\s+)?\d{6}\b",
                    0.85,
                ),
                # US: <num> <street words> <type> (direction)? [.,]? <city>, <ST> <ZIP>(-ZIP4)?
                Pattern(
                    "US_FULL_ADDRESS",
                    rf"\b\d+\s+(?:[A-Za-z0-9]+\s+){{1,6}}{_STREET_TYPES}"
                    r"(?:\s+(?:NE|NW|SE|SW|N|S|E|W))?"
                    r"[.,]?\s+(?:[A-Za-z]+\s*){1,4},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
                    0.85,
                ),
                # PO Box (SG / US)
                Pattern(
                    "PO_BOX",
                    r"\bP\.?\s?O\.?\s*Box\s+\d+(?:\s*,?\s*[A-Za-z]+){0,3}"
                    r"(?:\s*,?\s*[A-Z]{2})?\s*\d{5,6}\b",
                    0.75,
                ),
                # Anchor: bare "Singapore <6-digit>" (low score; backup if the
                # full-address regexes miss — user can extend manually).
                Pattern("SG_POSTAL_ANCHOR",
                        r"\bSingapore\s+\d{6}\b", 0.40),
                # Anchor: bare "<ST> <ZIP>" (US) — 2-letter state + 5-digit zip
                Pattern("US_ZIP_ANCHOR",
                        r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", 0.35),
                # ZIP+4 with state context — scored ABOVE DASHED_ACCOUNT
                # (0.85) so _suppress_contained drops the account fragment
                # that lives inside a "CAMBRIDGE MA 02142-1057" address tail.
                Pattern("US_STATE_ZIP4",
                        r"\b[A-Z]{2}\s+\d{5}-\d{4}\b", 0.90),
            ],
            name="CustomAddressRecognizer",
        )
    )

    return analyzer


# ── Multi-model NER for PERSON ────────────────────────────────────────────────

# Module-level cache — multiple Flask module instances (PdfQc + PiiReview
# both call this) share the same loaded models instead of each spending
# minutes loading spaCy / BERT / Stanza independently in parallel.
_NER_CACHE: list | None = None
_NER_CACHE_LOCK = threading.Lock()


def _load_ner_models() -> list[tuple[str, Any]]:
    """
    Load all available NER models. Each model is tried; failures are skipped.
    Returns list of (name, model_object) tuples.

    Thread-safe singleton: first caller does the heavy load; later callers
    (from another module in the same process) get the cached result.
    """
    global _NER_CACHE
    if _NER_CACHE is not None:
        return _NER_CACHE
    with _NER_CACHE_LOCK:
        if _NER_CACHE is not None:
            return _NER_CACHE
        _NER_CACHE = _load_ner_models_uncached()
        return _NER_CACHE


def _load_ner_models_uncached() -> list[tuple[str, Any]]:
    loaded: list[tuple[str, Any]] = []

    # 1. spaCy en_core_web_lg — DISABLED (spacy_trf is more accurate on
    # names; lg flags too many false positives). Presidio still loads its
    # own en_core_web_lg as its NLP engine — dropping it here only removes
    # it from the PERSON consensus vote.
    # try:
    #     import spacy
    #     nlp_lg = spacy.load("en_core_web_lg")
    #     loaded.append(("spacy_lg", nlp_lg))
    #     print("  [NER] spacy/en_core_web_lg         ✓", flush=True)
    # except Exception as e:
    #     print(f"  [NER] spacy/en_core_web_lg         ✗  {e}", flush=True)

    # 2. spaCy en_core_web_trf (transformer-based, optional)
    try:
        import spacy
        nlp_trf = spacy.load("en_core_web_trf")
        loaded.append(("spacy_trf", nlp_trf))
        print("  [NER] spacy/en_core_web_trf        ✓", flush=True)
    except Exception:
        print("  [NER] spacy/en_core_web_trf        — not installed", flush=True)

    # 3. HuggingFace dslim/bert-base-NER — DISABLED
    # hf_bert emits subword fragments ("Te", "##o", bare "T") as PERSON
    # that pollute the candidate list. spaCy + Stanza give cleaner results.
    # Re-enable here if someone adds a subword-filter post-processor.
    # try:
    #     from transformers import pipeline as hf_pipeline
    #     hf_ner = hf_pipeline("ner", model="dslim/bert-base-NER",
    #                           aggregation_strategy="simple", device=-1)
    #     loaded.append(("hf_bert", hf_ner))
    #     print("  [NER] dslim/bert-base-NER          ✓", flush=True)
    # except Exception:
    #     print("  [NER] dslim/bert-base-NER          — not installed", flush=True)

    # 4. Flair ner-english-large (optional)
    try:
        from flair.models import SequenceTagger
        flair_tagger = SequenceTagger.load("ner-english-large")
        loaded.append(("flair", flair_tagger))
        print("  [NER] flair/ner-english-large      ✓", flush=True)
    except Exception:
        print("  [NER] flair/ner-english-large      — not installed", flush=True)

    # 5. Stanza en (optional)
    try:
        import stanza
        stanza_nlp = stanza.Pipeline("en", processors="tokenize,ner", verbose=False)
        loaded.append(("stanza", stanza_nlp))
        print("  [NER] stanza/en                    ✓", flush=True)
    except Exception:
        print("  [NER] stanza/en                    — not installed", flush=True)

    print(f"  [NER] {len(loaded)} model(s) active — consensus requires "
          f"≥{min(2, len(loaded))} vote(s)\n", flush=True)
    return loaded


def _run_single_ner(name: str, model: Any, text: str) -> set[str]:
    """Run one NER model on text, return set of PERSON entity strings."""
    found: set[str] = set()
    try:
        if name.startswith("spacy"):
            doc = model(text)
            for ent in doc.ents:
                if ent.label_ == "PERSON" and ent.text.strip():
                    found.add(ent.text.strip())
        elif name == "hf_bert":
            results = model(text[:512])
            for r in results:
                if r.get("entity_group") in ("PER", "PERSON"):
                    word = r.get("word", "").strip()
                    if word:
                        found.add(word)
        elif name == "flair":
            from flair.data import Sentence
            sentence = Sentence(text)
            model.predict(sentence)
            for ent in sentence.get_spans("ner"):
                if ent.tag in ("PER", "PERSON") and ent.text.strip():
                    found.add(ent.text.strip())
        elif name == "stanza":
            doc = model(text)
            for sent in doc.sentences:
                for ent in sent.ents:
                    if ent.type == "PERSON" and ent.text.strip():
                        found.add(ent.text.strip())
    except Exception:
        pass
    return found


def _find_persons_consensus(
    text: str, models: list[tuple[str, Any]]
) -> dict[str, list[str]]:
    """
    Run all loaded NER models on text IN PARALLEL (threaded — each model
    releases the GIL during inference). Returns dict:
    entity → [model_names_that_voted_for_it].
    """
    if not models:
        return {}

    from concurrent.futures import ThreadPoolExecutor

    per_model: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=len(models)) as ex:
        futures = {ex.submit(_run_single_ner, name, m, text): name
                   for name, m in models}
        for fut, name in [(f, futures[f]) for f in futures]:
            try:
                found = fut.result()
            except Exception:
                found = set()
            for entity in found:
                per_model.setdefault(entity, []).append(name)
    return per_model


# ── Log writer — console + file simultaneously ────────────────────────────────

class _LogWriter:
    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        # PyMuPDF's diagnostic callback can close sys.stdout mid-run, after
        # which any print() raises ValueError("I/O operation on closed
        # file."). The log file is the source of truth; tolerate stdout
        # being unavailable so analyze_pdf doesn't crash on what is purely
        # a tracing concern.
        try:
            print(line, flush=True)
        except Exception:
            pass
        self._f.write(line + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


# ── pdftohtml ─────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _run_pdftohtml(pdf_path: str) -> ET.Element | None:
    temp_base = Path(tempfile.gettempdir()) / f"pdftok_{uuid.uuid4().hex}"
    xml_file  = temp_base.with_suffix(".xml")
    try:
        result = subprocess.run(
            ["pdftohtml", "-xml", pdf_path, str(temp_base)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not xml_file.exists():
            return None
        return ET.parse(xml_file).getroot()
    except Exception:
        return None
    finally:
        if xml_file.exists():
            xml_file.unlink(missing_ok=True)


# ── pdf_qc-enhanced page text extraction ─────────────────────────────────────

_ROW_TOL = 3.0   # pt — same as pdf_analysis_viz_data_v3 ROW_CLUSTER_TOLERANCE


def _qc_page_texts(
    xml_root:      ET.Element,
    page_no:       int,           # 1-based
    scale_x:       float,
    scale_y:       float,
    fitz_rects:    list[dict],    # from fitz_page.get_drawings() rects
    plumber_rects: list[dict],    # from plumber_page.rects
    fitz_lines:    list[dict],    # line segments from fitz_page.get_drawings() items
    plumber_lines: list[dict],    # from plumber_page.lines
    all_chars:     list[dict],    # from plumber_page.chars or []
    fitz_rotation: int,           # fitz_page.rotation
) -> list[dict]:
    """
    Full v3-equivalent per-page text extraction pipeline:
      - Ghost span filtering
      - Step 4: rotation bbox correction (when fitz_rotation != 0)
      - Step 1: vertical line splitting
      - Step 2: cell boundary splitting
      - Step 3: cell group merging (fitz_rects + plumber_rects combined)

    Returns list of dicts:
        content, left, top, right, bottom, midpoint_y,
        cell_group (int|None), cell_merged_value (str|None)
    """
    xml_page = xml_root.find(f".//page[@number='{page_no}']")
    if xml_page is None:
        return []

    texts: list[dict] = []
    for node in xml_page.findall(".//text"):
        content = node.text.strip() if node.text else ""
        for child in node:
            snippet = child.text.strip() if child.text else ""
            content += snippet
            if child.tail:
                content += child.tail.strip()
        content = content.strip()

        left   = _safe_float(node.get("left"))
        top    = _safe_float(node.get("top"))
        width  = _safe_float(node.get("width"))
        height = _safe_float(node.get("height"))

        pdf_left   = left * scale_x
        pdf_top    = top  * scale_y
        pdf_right  = (left + width)  * scale_x
        pdf_bottom = (top  + height) * scale_y
        text_width = pdf_right - pdf_left

        # ── Ghost span filter (thresholds unchanged from original) ─────────
        char_count = len(content.replace(" ", ""))
        if text_width <= 3.0 and char_count == 0:
            continue
        if char_count >= 3 and text_width > 0 and text_width / char_count < 2.0:
            continue

        texts.append({
            "content":           content,
            "left":              pdf_left,
            "top":               pdf_top,
            "right":             pdf_right,
            "bottom":            pdf_bottom,
            "midpoint_y":        (pdf_top + pdf_bottom) / 2,
            "cell_group":        None,
            "cell_merged_value": None,
        })

    if not texts:
        return texts

    # ── Step 4: Rotation bbox correction (v3 lines ~615-670) ──────────────
    # pdftohtml gives pre-rotation coordinates on rotated pages.
    # Use pdfplumber chars (ground truth) to correct each element's bbox.
    if fitz_rotation != 0:
        for xt in texts:
            xc = xt["content"].strip()
            if not xc or len(xc) < 2:
                continue
            first_char = xc[0]
            candidates = []
            for ci, ch in enumerate(all_chars):
                if ch["text"] == first_char:
                    matched = True
                    match_chars = [ch]
                    ci2 = ci + 1
                    for xci in range(1, len(xc)):
                        while ci2 < len(all_chars) and all_chars[ci2]["text"] == " " and xc[xci] != " ":
                            ci2 += 1
                        if ci2 >= len(all_chars) or all_chars[ci2]["text"] != xc[xci]:
                            matched = False
                            break
                        match_chars.append(all_chars[ci2])
                        ci2 += 1
                    if matched and len(match_chars) >= len(xc.replace(" ", "")):
                        bx0 = min(c["x0"]     for c in match_chars)
                        bt  = min(c["top"]    for c in match_chars)
                        bx1 = max(c["x1"]     for c in match_chars)
                        bb  = max(c["bottom"] for c in match_chars)
                        dist = abs(xt["top"] - bt) + abs(xt["left"] - bx0)
                        candidates.append((dist, bx0, bt, bx1, bb))
            if candidates:
                candidates.sort()
                _, bx0, bt, bx1, bb = candidates[0]
                xt["left"]       = round(bx0, 2)
                xt["top"]        = round(bt,  2)
                xt["right"]      = round(bx1, 2)
                xt["bottom"]     = round(bb,  2)
                xt["midpoint_y"] = round((bt + bb) / 2, 2)

    # ── Step 1: Vertical line splitting (v3 lines ~954-996) ───────────────
    # Collect vertical lines from both fitz_lines and plumber_lines.
    vlines: list[dict] = []
    for ln in fitz_lines:
        if ln.get("type") == "vertical" and abs(ln["y1"] - ln["y0"]) > 5:
            vlines.append({"x": round((ln["x0"] + ln["x1"]) / 2, 2),
                           "y_min": min(ln["y0"], ln["y1"]),
                           "y_max": max(ln["y0"], ln["y1"])})
    for ln in plumber_lines:
        if ln.get("type") == "vertical" and abs(ln["y1"] - ln["y0"]) > 5:
            vlines.append({"x": round((ln["x0"] + ln["x1"]) / 2, 2),
                           "y_min": min(ln["y0"], ln["y1"]),
                           "y_max": max(ln["y0"], ln["y1"])})

    def _seg_to_xt(base: dict, seg: list) -> dict | None:
        content = "".join(c["text"] for c in seg).strip()
        if not content:
            return None
        bx0 = min(c["x0"]     for c in seg)
        bx1 = max(c["x1"]     for c in seg)
        bt  = min(c["top"]    for c in seg)
        bb  = max(c["bottom"] for c in seg)
        s = dict(base)
        s["content"]    = content
        s["left"]       = round(bx0, 2)
        s["right"]      = round(bx1, 2)
        s["top"]        = round(bt,  2)
        s["bottom"]     = round(bb,  2)
        s["midpoint_y"] = round((bt + bb) / 2, 2)
        return s

    if vlines:
        split_texts: list[dict] = []
        for xt in texts:
            mid_y = xt["midpoint_y"]
            cuts = sorted([
                vl["x"] for vl in vlines
                if xt["left"] < vl["x"] < xt["right"]
                and vl["y_min"] <= mid_y <= vl["y_max"]
            ])
            if not cuts:
                split_texts.append(xt)
                continue
            t_chars = sorted([
                c for c in all_chars
                if xt["top"] - 3 <= c["top"] <= xt["bottom"] + 3
                and xt["left"] - 2 <= c["x0"] <= xt["right"] + 2
                and c["text"] != ""
            ], key=lambda c: c["x0"])
            if not t_chars:
                split_texts.append(xt)
                continue
            boundaries = [xt["left"]] + cuts + [xt["right"] + 1]
            any_split = False
            for si in range(len(boundaries) - 1):
                seg = [c for c in t_chars if boundaries[si] - 1 <= c["x0"] < boundaries[si + 1]]
                sub = _seg_to_xt(xt, seg)
                if sub:
                    split_texts.append(sub)
                    any_split = True
            if not any_split:
                split_texts.append(xt)
        texts = split_texts

    # ── Step 2 + 3: Cell boundary splitting + merging (v3 lines ~1012-1060) ─
    # Step 3 fix: use fitz_rects + plumber_rects combined (not fitz_rects only).
    combined_rects = list(fitz_rects) + list(plumber_rects)
    cell_rects: list[tuple] = []
    for r in combined_rects:
        rx0 = r.get("x0", 0); ry0 = r.get("y0", 0)
        rx1 = r.get("x1", 0); ry1 = r.get("y1", 0)
        if (rx1 - rx0) > 15 and (ry1 - ry0) > 15:
            cell_rects.append((rx0, ry0, rx1, ry1))

    unique_cells: list[tuple] = []
    for rc in cell_rects:
        if not any(
            abs(rc[0]-u[0]) < 2 and abs(rc[1]-u[1]) < 2 and
            abs(rc[2]-u[2]) < 2 and abs(rc[3]-u[3]) < 2
            for u in unique_cells
        ):
            unique_cells.append(rc)

    cell_group_id = 0
    for (rx0, ry0, rx1, ry1) in unique_cells:
        # Step 2: split any text crossing rx0 (left cell boundary)
        new_texts: list[dict] = []
        replaced = False
        for xt in texts:
            if (xt["left"] < rx0 < xt["right"]
                    and ry0 <= xt["midpoint_y"] <= ry1
                    and xt["content"].strip()):
                t_chars = sorted([
                    c for c in all_chars
                    if abs(c["top"] - xt["top"]) < 4
                    and xt["left"] - 2 <= c["x0"] <= xt["right"] + 2
                    and c["text"] != ""
                ], key=lambda c: c["x0"])
                left_seg  = [c for c in t_chars if c["x0"] <  rx0]
                right_seg = [c for c in t_chars if c["x0"] >= rx0]
                if left_seg and right_seg:
                    sub_l = _seg_to_xt(xt, left_seg)
                    sub_r = _seg_to_xt(xt, right_seg)
                    if sub_l: new_texts.append(sub_l)
                    if sub_r: new_texts.append(sub_r)
                    replaced = True
                    continue
            new_texts.append(xt)
        if replaced:
            texts = new_texts

        # Step 3: cell group merging using both rect sources
        inside = [
            t for t in texts
            if rx0 <= t["left"]
            and t["right"] <= rx1 + 2
            and ry0 <= t["midpoint_y"] <= ry1
            and t["content"]
        ]
        if len(inside) < 2:
            continue
        y_vals = [t["midpoint_y"] for t in inside]
        if max(y_vals) - min(y_vals) < _ROW_TOL * 2:
            continue
        sorted_in = sorted(inside, key=lambda t: (t["midpoint_y"], t["left"]))
        combined  = " ".join(t["content"].strip() for t in sorted_in)
        for t in inside:
            t["cell_group"]        = cell_group_id
            t["cell_merged_value"] = combined
        cell_group_id += 1

    return texts


def _find_qc_match(qc_texts: list[dict], left: float, top: float, tol: float = 6.0) -> str:
    """Find the qc text node closest to (left, top). Returns content or ''."""
    best, best_dist = "", float("inf")
    for t in qc_texts:
        dist = abs(t["left"] - left) + abs(t["top"] - top)
        if dist < best_dist:
            best_dist = dist
            best = t["content"]
    return best if best_dist <= tol * 2 else ""


def _build_full_text(xml_texts: list[dict]) -> str:
    """
    Build ghost-free, cell-merged full_text for Presidio.

    - xml_texts inside a cell_group contribute the merged value ONCE.
    - Others contribute their own content.
    Ensures Presidio sees whole fund names, not line fragments, and sees
    each entity exactly once.

    viz_data v4 xml_texts may or may not carry cell_group / cell_merged_value;
    .get() handles both cases.
    """
    seen_groups: set[int] = set()
    lines: list[str] = []
    for t in xml_texts:
        cg = t.get("cell_group")
        if cg is not None:
            if cg in seen_groups:
                continue
            seen_groups.add(cg)
            merged = t.get("cell_merged_value")
            if merged:
                lines.append(merged)
                continue
        content = t.get("content")
        if content:
            lines.append(content)
    return "\n".join(lines)


# ── Ordered span extraction (unchanged from v1) ───────────────────────────────

def _ordered_spans(
    fitz_page,
    pdftohtml_texts: list[dict],
    plumber_page,
) -> list[str]:
    rotation = fitz_page.rotation
    page_h   = fitz_page.rect.height

    spans = []
    for block in fitz_page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                content = span.get("text", "").strip()
                bbox    = span.get("bbox", (0, 0, 0, 0))

                if rotation == 0:
                    left, top = bbox[0], bbox[1]
                elif rotation == 90:
                    left, top = bbox[1], page_h - bbox[2]
                elif rotation == 180:
                    left, top = fitz_page.rect.width - bbox[2], page_h - bbox[3]
                elif rotation == 270:
                    left, top = page_h - bbox[3], bbox[0]
                else:
                    left, top = bbox[0], bbox[1]

                if not content:
                    content = _find_qc_match(pdftohtml_texts, left, top)

                if not content and plumber_page is not None:
                    for word in (plumber_page.extract_words() or []):
                        if abs(word["x0"] - left) < 6 and abs(word["top"] - top) < 6:
                            content = word["text"]
                            break

                if content:
                    spans.append(content)

    return spans


# ── pikepdf stream rewriting (unchanged from v1) ─────────────────────────────

def _add_helvetica(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    if "/Resources" not in page:
        page["/Resources"] = pikepdf.Dictionary()
    res = page["/Resources"]
    if "/Font" not in res:
        res["/Font"] = pikepdf.Dictionary()
    if "/Helv" not in res["/Font"]:
        res["/Font"]["/Helv"] = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/Type1"),
                BaseFont=pikepdf.Name("/Helvetica"),
                Encoding=pikepdf.Name("/WinAnsiEncoding"),
            )
        )


def _scan_tjs_with_positions(pik_page, page_h, page_w=0.0, rotation=0):
    result = []
    cur_font = "/TT0"
    cur_size = 12.0
    leading  = 0.0
    tm  = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]

    for operands, operator in pikepdf.parse_content_stream(pik_page):
        op = str(operator)
        if op == "BT":
            tm  = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            leading = 0.0
        elif op == "Tf":
            cur_font = str(operands[0]); cur_size = float(operands[1])
        elif op == "TL":
            leading = float(operands[0])
        elif op == "Tm":
            tm = [float(o) for o in operands]; tlm = tm[:]
        elif op in ("Td", "TD"):
            tx, ty = float(operands[0]), float(operands[1])
            if op == "TD": leading = -ty
            new_e = tx*tlm[0] + ty*tlm[2] + tlm[4]
            new_f = tx*tlm[1] + ty*tlm[3] + tlm[5]
            tlm[4], tlm[5] = new_e, new_f; tm = tlm[:]
        elif op == "T*":
            tx, ty = 0.0, -leading
            new_e = tx*tlm[0] + ty*tlm[2] + tlm[4]
            new_f = tx*tlm[1] + ty*tlm[3] + tlm[5]
            tlm[4], tlm[5] = new_e, new_f; tm = tlm[:]
        elif op in ("Tj", "TJ"):
            if op == "Tj":
                raw = bytes(operands[0])
            else:
                arr = operands[0]
                raw_parts = [bytes(item) for item in arr if isinstance(item, pikepdf.String)]
                raw = b"".join(raw_parts)
                if not raw: continue
            px, py = tm[4], tm[5]
            result.append({"x": px, "y_top": page_h - py, "raw": raw,
                           "font": cur_font, "size": cur_size, "op": op})
    return result


def _build_replacement_index(pik_page, replacements, xml_texts, plumber_page,
                              page_h, page_w=0.0, rotation=0):
    """
    Pass 2 uses viz_data xml_texts (ghost-filtered, rotation-corrected) for
    position-based entity lookup. Reads content / left / top only.
    """
    hits = []
    found_entities: set[str] = set()

    # ── Pass 1: Content-based ──────────────────────────────────────────────
    cur_font = "/TT0"; cur_size = 12.0
    remaining = dict(replacements)
    for operands, operator in pikepdf.parse_content_stream(pik_page):
        if not remaining: break
        op = str(operator)
        if op == "Tf":
            cur_font = str(operands[0]); cur_size = float(operands[1])
        elif op in ("Tj", "TJ"):
            if op == "Tj":
                raw = bytes(operands[0])
            else:
                arr = operands[0]
                raw_parts = [bytes(item) for item in arr if isinstance(item, pikepdf.String)]
                raw = b"".join(raw_parts)
            if not raw: continue
            decoded = ""
            for enc in ("latin-1", "utf-8", "ascii", "utf-16-be"):
                try:
                    candidate = raw.decode(enc, errors="ignore").replace("\x00", "").strip()
                    if candidate: decoded = candidate; break
                except Exception:
                    pass
            if not decoded: continue
            for entity_text in list(remaining.keys()):
                if entity_text.lower() in decoded.lower():
                    token = remaining.pop(entity_text)
                    hits.append((raw, cur_font, cur_size, token))
                    found_entities.add(entity_text)
                    break

    # ── Pass 2: Position-based fallback using xml_texts ───────────────────
    # viz_data xml_texts give ghost-filtered, rotation-corrected positions
    # so entity anchoring stays accurate even on rotated pages.
    still_remaining = {k: v for k, v in replacements.items() if k not in found_entities}
    if still_remaining:
        tj_list = _scan_tjs_with_positions(pik_page, page_h, page_w, rotation)

        for entity_text, token in still_remaining.items():
            ex: float | None = None
            ey: float | None = None

            for t in xml_texts:
                if entity_text.lower() in t["content"].lower():
                    ex, ey = t["left"], t["top"]
                    break

            # Fallback to pdfplumber if not found in xml_texts
            if ex is None and plumber_page is not None:
                for w in (plumber_page.extract_words() or []):
                    if entity_text.lower() in w["text"].lower():
                        ex, ey = float(w["x0"]), float(w["top"]); break

            if ex is None:
                continue

            best_tj = None; best_dist = float("inf")
            for tj in tj_list:
                dist = abs(tj["x"] - ex) + abs(tj["y_top"] - ey)
                if dist < best_dist:
                    best_dist = dist; best_tj = tj

            if best_tj is not None and best_dist < 30.0:
                hits.append((best_tj["raw"], best_tj["font"], best_tj["size"], token))
                found_entities.add(entity_text)

    return hits


def _escape_pdf_string(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _normalize_stream(b: bytes) -> bytes:
    return re.sub(rb"\\\r?\n", b"", b)


def _rewrite_stream(stream_bytes: bytes, hits: list[tuple]) -> bytes:
    normalized = _normalize_stream(stream_bytes)
    result = normalized; changed = False

    for raw, orig_font, orig_size, token in hits:
        size_b = f"{orig_size:.4f}".encode()
        font_b = orig_font.encode()
        tok_b  = _escape_pdf_string(token.encode("latin-1", errors="replace"))
        replacement = (
            b"/Helv " + size_b + b" Tf\n"
            b"(" + tok_b + b")Tj\n"
            + font_b + b" " + size_b + b" Tf"
        )
        matched = False

        lit_re  = re.escape(b"(" + _escape_pdf_string(raw) + b")") + rb"\s*Tj"
        new_res = re.sub(lit_re, replacement, result)
        if new_res != result:
            result = new_res; changed = True; matched = True

        if not matched:
            for hex_str in (raw.hex().upper().encode(), raw.hex().lower().encode()):
                hex_re  = re.escape(b"<" + hex_str + b">") + rb"\s*Tj"
                new_res = re.sub(hex_re, replacement, result)
                if new_res != result:
                    result = new_res; changed = True; matched = True; break

        if not matched:
            for hex_str in (raw.hex().upper().encode(), raw.hex().lower().encode()):
                tj_hex_re = re.escape(b"[<" + hex_str + b">]") + rb"\s*TJ"
                new_res   = re.sub(tj_hex_re, replacement, result)
                if new_res != result:
                    result = new_res; changed = True; matched = True; break

        if not matched:
            lit_tj_re = re.escape(b"[(" + _escape_pdf_string(raw) + b")]") + rb"\s*TJ"
            new_res   = re.sub(lit_tj_re, replacement, result)
            if new_res != result:
                result = new_res; changed = True

    return result if changed else stream_bytes


def _get_content_streams(pdf: pikepdf.Pdf, page: pikepdf.Page) -> list:
    contents = page.get("/Contents")
    if contents is None: return []
    if isinstance(contents, pikepdf.Array):
        return [pdf.get_object(c.objgen) for c in contents]
    return [pdf.get_object(contents.objgen)]


# ── Ghost stripping (pre-analyze) ─────────────────────────────────────────────

def strip_ghosts_from_pdf(input_path: str, output_path: str, viz_data: dict) -> int:
    """Physically delete every CMap-ghost text operator from the PDF.

    Strip semantics: the underlying glyph-drawing operators are removed
    from each page's content stream. After this returns, no text extractor
    can recover the ghost strings — they are simply gone, the same way
    sanitisation removes document metadata. The page renders identically
    because no visible glyph backed those operators.

    Implementation uses PyMuPDF's content-stream rewrite path (the PDF spec
    operation is named "redaction" but the result is byte-level removal —
    no rectangle is drawn because fill=None). This is the only mechanism
    that handles CID / Type0 / CMap-encoded fonts; pikepdf-level Tj-pattern
    matching can't recover the source text for those fonts.

    Returns the count of ghost xml_texts whose glyph rect was located and
    removed.
    """
    pages_map = viz_data.get("pages") or {}
    doc = fitz.open(input_path)
    total_stripped = 0
    try:
        for page_no in range(len(doc)):
            page = doc[page_no]
            page_data = (pages_map.get(str(page_no + 1))
                         or pages_map.get(page_no + 1) or {})
            ghosts = [
                xt for xt in (page_data.get("xml_texts") or [])
                if xt.get("is_ghost") and xt.get("content")
            ]
            if not ghosts:
                continue

            xform = page.derotation_matrix if page.rotation else None
            staged = 0
            for gt in ghosts:
                bbox = fitz.Rect(
                    gt.get("left", 0),  gt.get("top", 0),
                    gt.get("right", 0), gt.get("bottom", 0),
                )
                if bbox.is_empty or not bbox.is_valid:
                    continue
                clip = bbox * xform if xform is not None else bbox
                content = (gt.get("content") or "").strip()
                # Locate the exact glyph rect of the ghost string within
                # its xml_text bbox. search_for returns rects in user-space
                # so it works on rotated pages once the bbox is transformed.
                tight = []
                if content:
                    try:
                        tight = page.search_for(content, clip=clip)
                    except Exception:
                        tight = []
                # Without a tight match, fall back to the row bbox so we
                # still strip something. Per user spec: every flagged ghost
                # MUST be removed even if the bbox is wider than the glyph.
                rects = tight if tight else [clip]
                for r in rects:
                    if r.is_empty or not r.is_valid:
                        continue
                    page.add_redact_annot(r, fill=None)
                    staged += 1

            if staged:
                # text=PDF_REDACT_TEXT_REMOVE → glyphs deleted from content
                # stream. images/graphics=NONE → nothing visual touched, so
                # charts and vectors under the ghost rect survive intact.
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                )
                total_stripped += len(ghosts)

        doc.save(output_path, deflate=True, garbage=4)
    finally:
        doc.close()
    return total_stripped


# ── Main ──────────────────────────────────────────────────────────────────────

def tokenize_pdf(input_path: str, output_path: str,
                 viz_data: dict,
                 ner_models: list | None = None) -> tuple[dict, dict, list]:
    """
    One-shot detect-and-rewrite: regex ACCOUNT_NUMBER + multi-model PERSON
    consensus. Consumes viz_data v4 xml_texts (with per-word subindex) so no
    re-extraction happens. pikepdf still needs the live PDF for content
    stream rewrites.

    Returns (value_to_token, token_to_value, token_bboxes). Each token_bbox
    carries xml_text_id + word_ids + partial so the pdf_qc viewer can overlay
    tokens at exact word positions instead of line-level.
    """
    from datetime import datetime as _dt
    from time import perf_counter as _pc

    analyzer  = _build_analyzer()
    log_path  = output_path + ".log"
    log       = _LogWriter(log_path)
    t0_total  = _pc()
    _models   = ner_models or []
    min_votes = min(2, len(_models)) if _models else 1

    # ── Log header ────────────────────────────────────────────────────────
    sep = "=" * 80
    log.write(sep)
    log.write("PDF PII Tokenizer v3 — Run Log")
    log.write(sep)
    log.write(f"File    : {input_path}")
    log.write(f"Output  : {output_path}")
    log.write(f"Log     : {log_path}")
    log.write(f"Started : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    model_names = [n for n, _ in _models]
    log.write(f"Models  : {', '.join(model_names) if model_names else '(none)'} "
              f"({len(_models)} loaded, min {min_votes} vote(s) for PERSON)")
    log.write(f"viz_data: schema={viz_data.get('schema_version','?')} "
              f"source={viz_data.get('source_filename','?')}")
    log.write(sep)

    fitz_doc = fitz.open(input_path)
    pik_pdf  = pikepdf.open(input_path)

    pages_map = viz_data.get("pages") or {}
    total_pages = len(fitz_doc)
    log.write(f"\n  {total_pages} page(s) to process...")
    total_replacements = 0
    token_bboxes: list[dict] = []

    with pdfplumber.open(input_path) as plumber_doc:
        for page_no in range(total_pages):
            fitz_page    = fitz_doc[page_no]
            pik_page     = pik_pdf.pages[page_no]
            plumber_page = plumber_doc.pages[page_no]

            page_h   = float(fitz_page.rect.height)
            page_w   = float(fitz_page.rect.width)
            rotation = fitz_page.rotation

            page_data = (pages_map.get(str(page_no + 1))
                         or pages_map.get(page_no + 1) or {})
            # viz_data v4.9 marks CMap-ghost xml_texts with is_ghost=True.
            # Filter at boundary so Presidio / NER / occurrence finders never
            # see ghost content. Also scrubs cell_merged_value on non-ghost
            # siblings of ghosts (otherwise the merged-value shortcut leaks
            # the ghost text through).
            xml_texts: list[dict] = _filter_ghosts_and_taint(
                page_data.get("xml_texts") or []
            )

            # ── (3) Ghost-free, cell-merged full_text ─────────────────────
            if xml_texts:
                full_text = _build_full_text(xml_texts)
            else:
                full_text = fitz_page.get_text()

            if not full_text.strip():
                continue

            log.write(f"\nPAGE {page_no+1}/{total_pages}")
            log.write("-" * 40)

            # ── Pattern-based recognizers ──────────────────────────────────
            # Broad Presidio built-ins + our custom SG/US/account/address.
            replacements: dict[str, str] = {}
            pat_results = analyzer.analyze(
                text=full_text,
                language="en",
                entities=[
                    "ACCOUNT_NUMBER", "ADDRESS",
                    "EMAIL_ADDRESS", "PHONE_NUMBER",
                    "CREDIT_CARD", "IBAN_CODE",
                    "SG_NRIC", "SG_UEN", "SG_PHONE",
                "ISIN",
                ],
                score_threshold=0.50,
            )
            pat_found: dict[str, list] = {"ACCOUNT_NUMBER": [], "ADDRESS": []}
            # Suppress overlapping substring matches first — e.g. drop
            # ACCOUNT_NUMBER "767-7462" when PHONE_NUMBER "800-767-7462" is
            # present at the same position with a higher score.
            for r in _suppress_contained(pat_results):
                if r.entity_type == "ISIN":
                    continue
                entity = full_text[r.start : r.end].strip()
                cat = _normalise_category(r.entity_type)
                # Reject phones glued from disjoint glyph runs (chart axis
                # labels, multi-column cells). Gap measured against widths.
                if cat in ("PHONE_NUMBER", "SG_PHONE"):
                    occs_now = _find_entity_occurrences(
                        entity, xml_texts, page_no + 1
                    )
                    if _phone_match_is_disjoint(occs_now):
                        continue
                if entity and entity not in replacements:
                    token = _assign_token(cat, entity)
                    replacements[entity] = token
                    pat_found.setdefault(cat, []).append((entity, r.score, token))

            for cat, hits in pat_found.items():
                if hits:
                    log.write(f"  [{cat} — pattern]")
                    for ent, score, tok in hits:
                        log.write(f"    {repr(ent):<40} score={score:.2f}  →  {tok}  ✓ accepted")
                else:
                    log.write(f"  [{cat}] none found")

            # ── PERSON — multi-model consensus ─────────────────────────────
            person_votes = _find_persons_consensus(full_text, _models)

            if person_votes:
                log.write("  [PERSON — multi-model]")
                # Log what each model found
                all_model_findings: dict[str, list[str]] = {}
                for entity, voters in person_votes.items():
                    for m in voters:
                        all_model_findings.setdefault(m, []).append(entity)
                for mname in ([n for n, _ in _models]):
                    found_by = all_model_findings.get(mname, [])
                    log.write(f"    [{mname}]  {found_by if found_by else '(none)'}")

                log.write("  [consensus]")
                for entity, voters in sorted(person_votes.items(),
                                              key=lambda x: -len(x[1])):
                    vote_str = f"{len(voters)}/{len(_models)} votes  [{', '.join(voters)}]"
                    if len(voters) >= min_votes and entity not in replacements:
                        token = _assign_token("PERSON", entity)
                        replacements[entity] = token
                        log.write(f"    {repr(entity):<35} {vote_str}  →  {token}  ✓ accepted")
                    elif entity in replacements:
                        log.write(f"    {repr(entity):<35} {vote_str}  (already mapped)")
                    else:
                        log.write(f"    {repr(entity):<35} {vote_str}  ✗ rejected (below threshold)")
            else:
                log.write("  [PERSON] no candidates from any model")

            # ── Known entities from previous pages ─────────────────────────
            for known_value, known_token in list(_value_to_token.items()):
                if known_value in full_text and known_value not in replacements:
                    replacements[known_value] = known_token
                    log.write(f"  [carry-over]  {repr(known_value)}  →  {known_token}")

            if not replacements:
                log.write("  → no PII on this page")
                continue

            # ── Collect per-word bboxes for approved entities on this page ──
            # xml_text_id / word_ids / partial are propagated so pdf_qc.html
            # can overlay tokens at exact word positions (not line-level).
            for entity, token in replacements.items():
                for occ in _find_entity_occurrences(entity, xml_texts, page_no + 1):
                    token_bboxes.append({
                        "token": token, "value": entity,
                        "page":   occ["page"],
                        "left":   occ["left"],   "top":    occ["top"],
                        "right":  occ["right"],  "bottom": occ["bottom"],
                        "xml_text_id": occ.get("xml_text_id"),
                        "word_ids":    occ.get("word_ids", []),
                        "partial":     occ.get("partial", False),
                    })

            # ── Match entities to raw Tj bytes ─────────────────────────────
            hits = _build_replacement_index(
                pik_page, replacements,
                xml_texts, plumber_page,
                page_h, page_w, rotation,
            )

            if not hits:
                log.write("  → entities detected but no Tj stream matches found")
                continue

            # ── Rewrite content streams ────────────────────────────────────
            _add_helvetica(pik_pdf, pik_page)
            streams  = _get_content_streams(pik_pdf, pik_page)
            replaced = 0
            for stream_obj in streams:
                raw     = stream_obj.read_bytes()
                new_raw = _rewrite_stream(raw, hits)
                if new_raw != raw:
                    stream_obj.write(new_raw)
                    replaced += 1

            total_replacements += len(hits)
            log.write(f"  → {len(hits)} replacement(s) written to {replaced} stream(s)")

    pik_pdf.save(output_path)
    pik_pdf.close()
    fitz_doc.close()

    # ── Log summary ───────────────────────────────────────────────────────
    elapsed = _pc() - t0_total
    sep = "=" * 80
    log.write(f"\n{sep}")
    log.write("SUMMARY")
    log.write(sep)
    log.write(f"Finished    : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.write(f"Elapsed     : {elapsed:.1f}s")
    log.write(f"Pages       : {total_pages}")
    log.write(f"Replacements: {total_replacements}")
    log.write(f"Unique tokens ({len(_value_to_token)}):")
    for val, tok in sorted(_value_to_token.items(), key=lambda x: x[1]):
        log.write(f"  {tok:<20}  {repr(val)}")
    log.write(sep)
    log.write(f"Log saved   : {log_path}")
    log.close()

    return _value_to_token, _token_to_value, token_bboxes


# ── ANALYZE (no rewrite) — for pii_review UI ──────────────────────────────────

_WORD_STRIP = ".,;:!?()[]\"'"

def _find_entity_occurrences(entity: str, xml_texts: list[dict], page_no: int) -> list[dict]:
    """
    Find occurrences of `entity` on one page using viz_data v4 xml_texts.
    EVERY occurrence carries an `xml_text_id` (always) and optionally `word_ids[]`
    when the entity maps to whole words cleanly.

    Occurrence schema:
      {
        page, left, top, right, bottom,
        xml_text_id:    int (always),
        word_ids:       [str] (only when entity is whole-word match),
        partial:        bool (True when entity is a sub-word fragment like "Te"),
        full_text:      str (the matched text source)
      }

    LIMITATION (v1): exact string match only. No aliasing — "Mr K Adams" and
    "K Adams" are separate tokens even though they're the same person.
    Same for phone/address/account variants. Any semantic equivalence must
    be resolved in a downstream step outside this tool.
    """
    occurrences: list[dict] = []
    needle_full = entity.strip()
    if not needle_full:
        return occurrences

    needle_lower = needle_full.lower()
    target_words = [w.strip(_WORD_STRIP).lower() for w in needle_full.split()]
    target_words = [w for w in target_words if w]

    # ── Strategy 1: whole-word match across page words (exact sequence) ───
    # Build reading-order flat list of (word, parent_xml_text) across page.
    flat: list[tuple[dict, dict]] = []
    for t in xml_texts:
        for w in t.get("words", []) or []:
            if w.get("text"):
                flat.append((w, t))
    flat.sort(key=lambda wt: (round(wt[0]["top"]), round(wt[0]["left"])))

    if flat and target_words:
        flat_texts = [w["text"].strip(_WORD_STRIP).lower() for w, _ in flat]
        n = len(target_words)
        i = 0
        while i <= len(flat) - n:
            if all(flat_texts[i + j] == target_words[j] for j in range(n)):
                # Group matched words by their parent xml_text so each
                # occurrence represents one xml_text row of the match.
                current_parent = None
                bucket: list[dict] = []
                def _flush():
                    if not bucket: return
                    xt = bucket[0]["_parent"]
                    xt_words = xt.get("words") or []
                    matched_ids = [b["w"].get("id") for b in bucket]
                    a_left  = min(b["w"]["left"]  for b in bucket)
                    a_right = max(b["w"]["right"] for b in bucket)
                    d_left  = min(b["w"].get("left_d",  b["w"]["left"])  for b in bucket)
                    d_right = max(b["w"].get("right_d", b["w"]["right"]) for b in bucket)
                    if len(bucket) == len(xt_words):
                        a_left, a_right = xt["left"], xt["right"]
                        d_left, d_right = xt["left"], xt["right"]
                    # top/bottom = xml_text y band. Authoritative.
                    occurrences.append({
                        "page":        page_no,
                        "left":        round(a_left,  2),
                        "top":         round(xt["top"],    2),
                        "right":       round(a_right, 2),
                        "bottom":      round(xt["bottom"], 2),
                        "left_d":      round(d_left,  2),
                        "right_d":     round(d_right, 2),
                        "xml_text_id": xt.get("id"),
                        "word_ids":    [wid for wid in matched_ids if wid is not None],
                        "partial":     False,
                        "full_text":   " ".join(b["w"]["text"] for b in bucket),
                    })
                for k in range(n):
                    w, xt = flat[i + k]
                    if current_parent is not None and xt is not current_parent:
                        _flush()
                        bucket = []
                    current_parent = xt
                    bucket.append({"w": w, "_parent": xt})
                _flush()
                i += n
            else:
                i += 1
        # NB: do NOT early-return here. Strategy 1c (below) runs
        # additively so footer renderings on the same page (in a different
        # xml_text from the Strategy-1 body match) still get matched. The
        # early-return after 1c handles the "found something, skip looser
        # strategies (1b / substring 2 / 3)" cutoff.

    # ── Strategy 1b: subsequence match (multi-column / multi-line safe) ───
    # Allows non-target words to interleave — needed when a page has two
    # columns and reading-order sort mixes words from both. A proximity cap
    # on the matched words' vertical span keeps false positives out:
    # matched tops must span <= 100pt (≈ 10 lines), enough for a 4-line
    # address but tight enough that scattered target words across a page
    # don't form a bogus match.
    # Skip if Strategy 1 already produced exact matches — 1b would re-emit
    # the same words as duplicates (subsequence-of-same-sequence).
    if flat and len(target_words) >= 2 and not occurrences:
        flat_texts = [w["text"].strip(_WORD_STRIP).lower() for w, _ in flat]
        n = len(target_words)
        start = 0
        while start <= len(flat_texts) - n:
            i = start
            seq: list[int] = []
            for tw in target_words:
                while i < len(flat_texts) and flat_texts[i] != tw:
                    i += 1
                if i >= len(flat_texts):
                    seq = []
                    break
                seq.append(i)
                i += 1
            if not seq or len(seq) != n:
                break
            tops = [flat[k][0]["top"] for k in seq]
            if max(tops) - min(tops) <= 100.0:
                # Group matched words by parent xml_text
                current_parent = None
                bucket: list[dict] = []
                def _flush_sub():
                    if not bucket: return
                    xt = bucket[0]["_parent"]
                    xt_words = xt.get("words") or []
                    matched_ids = [b["w"].get("id") for b in bucket]
                    a_left  = min(b["w"]["left"]  for b in bucket)
                    a_right = max(b["w"]["right"] for b in bucket)
                    d_left  = min(b["w"].get("left_d",  b["w"]["left"])  for b in bucket)
                    d_right = max(b["w"].get("right_d", b["w"]["right"]) for b in bucket)
                    if len(bucket) == len(xt_words):
                        a_left, a_right = xt["left"], xt["right"]
                        d_left, d_right = xt["left"], xt["right"]
                    occurrences.append({
                        "page":        page_no,
                        "left":        round(a_left,  2),
                        "top":         round(xt["top"],    2),
                        "right":       round(a_right, 2),
                        "bottom":      round(xt["bottom"], 2),
                        "left_d":      round(d_left,  2),
                        "right_d":     round(d_right, 2),
                        "xml_text_id": xt.get("id"),
                        "word_ids":    [wid for wid in matched_ids if wid is not None],
                        "partial":     False,
                        "full_text":   " ".join(b["w"]["text"] for b in bucket),
                    })
                for idx in seq:
                    w, xt = flat[idx]
                    if current_parent is not None and xt is not current_parent:
                        _flush_sub()
                        bucket = []
                    current_parent = xt
                    bucket.append({"w": w, "_parent": xt})
                _flush_sub()
            start = seq[-1] + 1
        # NB: do NOT early-return here. Strategy 1c runs additively below
        # so footer renderings on the same page (different xml_text from
        # the body match) still get matched.

    # ── Strategy 1c: whitespace-variant match ───────────────────────────
    # Same logical identifier often renders with different whitespace in
    # different parts of a PDF — e.g. an account 'LL 51161 96' appears
    # spaced in the body table but run-together as 'LL5116196' or
    # 'LL51161960' (with a trailing footer-delimiter digit) in the page
    # footer. Build the whitespace-free join of the needle and search for
    # it as (a) a single PDF word, or (b) the concatenation of consecutive
    # words within one xml_text. Either direction (spaced→run-together OR
    # run-together→spaced) is recovered without forcing the user to add
    # both forms manually.
    # Lower bound (>=4 chars) prevents pathological short-needle matches.
    needle_stripped = "".join(target_words)
    # Track xml_texts already hit by Strategy 1 so we don't duplicate
    # their match as a whitespace-variant hit.
    _strat1_hit_xt = {o.get("xml_text_id") for o in occurrences
                      if o.get("xml_text_id") is not None}
    if needle_stripped and len(needle_stripped) >= 4:
        for t in xml_texts:
            if t.get("id") in _strat1_hit_xt:
                continue
            words = t.get("words") or []
            if not words:
                continue
            norm = [(w, (w.get("text") or "").strip(_WORD_STRIP).lower())
                    for w in words]
            for start_i in range(len(norm)):
                if not norm[start_i][1]:
                    continue
                join_acc = ""
                seg = []
                for end_i in range(start_i, len(norm)):
                    tok = norm[end_i][1]
                    if not tok:
                        continue
                    join_acc += tok
                    seg.append(norm[end_i][0])
                    if join_acc == needle_stripped:
                        # Exact whitespace-variant match across consecutive
                        # words. Skip if it's already a Strategy-1 hit (a
                        # single-segment window whose text matches the needle
                        # token-for-token — Strategy 1 above would have caught
                        # it). Detection: needle already had no spaces AND
                        # window is a single word — that's a Strategy 1 dup.
                        if len(seg) == 1 and len(target_words) == 1:
                            break
                        matched_ids = [s.get("id") for s in seg]
                        a_left  = round(min(s["left"]  for s in seg), 2)
                        a_right = round(max(s["right"] for s in seg), 2)
                        d_left  = round(min(s.get("left_d",  s["left"])  for s in seg), 2)
                        d_right = round(max(s.get("right_d", s["right"]) for s in seg), 2)
                        if len(seg) == len(words):
                            a_left, a_right = t["left"], t["right"]
                            d_left, d_right = t["left"], t["right"]
                        occurrences.append({
                            "page":        page_no,
                            "left":        a_left,
                            "top":         round(t["top"],    2),
                            "right":       a_right,
                            "bottom":      round(t["bottom"], 2),
                            "left_d":      d_left,
                            "right_d":     d_right,
                            "xml_text_id": t.get("id"),
                            "word_ids":    [wid for wid in matched_ids if wid is not None],
                            "partial":     False,
                            "full_text":   " ".join(s.get("text", "") for s in seg),
                        })
                        break
                    if len(join_acc) > len(needle_stripped):
                        # Overshot. If the overshoot is a single-word
                        # prefix match (needle is a prefix of this word),
                        # emit char-tight prefix bbox via chars[]. This
                        # catches footer rendering 'LL51161960' for needle
                        # 'LL 51161 96' (stripped 'LL5116196').
                        if len(seg) == 1 and join_acc.startswith(needle_stripped):
                            w = seg[0]
                            wtext = w.get("text") or ""
                            chars = w.get("chars") or []
                            if chars and len(chars) == len(wtext):
                                cseg = chars[:len(needle_stripped)]
                                a_left  = round(min(c["left"]  for c in cseg), 2)
                                a_right = round(max(c["right"] for c in cseg), 2)
                                d_left  = round(min(c.get("left_d",  c["left"])  for c in cseg), 2)
                                d_right = round(max(c.get("right_d", c["right"]) for c in cseg), 2)
                            else:
                                a_left  = round(w["left"],  2)
                                a_right = round(w["right"], 2)
                                d_left  = round(w.get("left_d",  w["left"]),  2)
                                d_right = round(w.get("right_d", w["right"]), 2)
                            occurrences.append({
                                "page":        page_no,
                                "left":        a_left,
                                "top":         round(t["top"],    2),
                                "right":       a_right,
                                "bottom":      round(t["bottom"], 2),
                                "left_d":      d_left,
                                "right_d":     d_right,
                                "xml_text_id": t.get("id"),
                                "partial":     True,
                                "full_text":   wtext,
                            })
                        break
        # NB: do NOT early-return here — Strategies 2 and 3 below must run
        # additively. Strategy 3 in particular catches xml_texts whose
        # words[] is sorted by storage-Y rather than logical order (vertical
        # text), which Strategies 1/1b/1c miss. The per-strategy duplicate
        # guards below prevent re-emission of xml_texts already matched.

    # ── Strategy 2: substring of a single word — char-tight bbox ────────
    # xml_text_id always set; word_ids unset; partial=True.
    # viz_data v4.5+ carries per-character bboxes via word["chars"]. We
    # compute a CHARACTER-tight bbox (all four sides from the matched char
    # range).
    # Bug 7: anchor the substring at a non-alphanumeric boundary on both
    # ends. Without this, a 9-char identifier like 'LL5116196' would falsely
    # match inside a 10-char run-together footer 'LL51161960' (whose
    # trailing '0' is part of an adjacent field, not the needle). The
    # boundary rule keeps deliberate sub-word redactions like 'te' inside
    # 'sta_te_ment' working when delimiters are present in the source, but
    # refuses pure alphanumeric-run prefix/suffix matches that almost
    # always mean two different identifiers happen to share a prefix.
    for t in xml_texts:
        for w in t.get("words", []) or []:
            wtext = w.get("text") or ""
            if not wtext:
                continue
            wlower = wtext.lower()
            idx = wlower.find(needle_lower)
            if idx >= 0 and needle_lower != wlower:
                # Boundary check: the char immediately before the match
                # (if any) and the char immediately after (if any) must
                # both be non-alphanumeric. Falls through to word-edge
                # case naturally (idx == 0 ⇒ no preceding char ⇒ ok;
                # idx + len == len(wlower) ⇒ no following char ⇒ ok). But
                # since we already excluded the full-word equality case
                # (Strategy 1), at least one side has a neighbouring char.
                before_ok = idx == 0 or not wlower[idx - 1].isalnum()
                end = idx + len(needle_lower)
                after_ok  = end >= len(wlower) or not wlower[end].isalnum()
                if not (before_ok and after_ok):
                    continue
                chars = w.get("chars") or []
                if chars and len(chars) == len(wtext) and \
                   idx + len(needle_lower) <= len(chars):
                    seg = chars[idx : idx + len(needle_lower)]
                    a_left  = round(min(c["left"]  for c in seg), 2)
                    a_right = round(max(c["right"] for c in seg), 2)
                    d_left  = round(min(c.get("left_d",  c["left"])  for c in seg), 2)
                    d_right = round(max(c.get("right_d", c["right"]) for c in seg), 2)
                else:
                    a_left  = round(w["left"],  2)
                    a_right = round(w["right"], 2)
                    d_left  = round(w.get("left_d",  w["left"]),  2)
                    d_right = round(w.get("right_d", w["right"]), 2)
                occurrences.append({
                    "page":        page_no,
                    "left":        a_left,
                    "top":         round(t["top"],    2),
                    "right":       a_right,
                    "bottom":      round(t["bottom"], 2),
                    "left_d":      d_left,
                    "right_d":     d_right,
                    "xml_text_id": t.get("id"),
                    "partial":     True,
                    "full_text":   wtext,
                })
    # ── Strategy 3: substring of xml_text content ─────────────────────────
    # ADDITIVE (no early-return gate from Strategy 2). Strategy 3 must catch
    # xml_texts whose words[] array is sorted by storage-Y instead of logical
    # reading order — happens for vertical/rotated text. Strategy 1's flat
    # (top, left) sort breaks for those because the first logical word ends
    # up last in the sort. The xml_text's content field carries the correct
    # logical reading regardless of word physical layout, so a substring
    # check on content recovers the missing occurrence. Duplicate-guard
    # against xml_texts already produced by Strategies 1/1b/1c/2.
    _strat3_already_hit = {o.get("xml_text_id") for o in occurrences
                            if o.get("xml_text_id") is not None}
    for t in xml_texts:
        if t.get("id") in _strat3_already_hit:
            continue
        if needle_lower in t.get("content", "").lower():
            occurrences.append({
                "page":        page_no,
                "left":        round(t["left"],   2),
                "top":         round(t["top"],    2),
                "right":       round(t["right"],  2),
                "bottom":      round(t["bottom"], 2),
                "xml_text_id": t.get("id"),
                "partial":     True,
                "full_text":   t.get("content", ""),
            })

    # ── Strategy 4: multi-xml_text span fallback (no per-word data) ──────
    if not occurrences:
        sorted_texts = sorted(
            [t for t in xml_texts if t.get("content", "").strip()],
            key=lambda t: (round(t.get("top", 0)), round(t.get("left", 0))),
        )
        concat_parts: list[str] = []
        spans: list[tuple[int, int, dict]] = []
        cursor = 0
        for t in sorted_texts:
            c = t["content"].strip()
            if not c: continue
            if cursor > 0: concat_parts.append(" "); cursor += 1
            start = cursor
            concat_parts.append(c); cursor += len(c)
            spans.append((start, cursor, t))
        concat = "".join(concat_parts).lower()
        pos = concat.find(needle_lower)
        while pos != -1:
            end = pos + len(needle_lower)
            for s, e, t in spans:
                if e > pos and s < end:
                    occurrences.append({
                        "page":        page_no,
                        "left":        round(t["left"],   2),
                        "top":         round(t["top"],    2),
                        "right":       round(t["right"],  2),
                        "bottom":      round(t["bottom"], 2),
                        "xml_text_id": t.get("id"),
                        "partial":     True,
                        "full_text":   t.get("content", ""),
                    })
            pos = concat.find(needle_lower, end)

    # Dedupe by bbox
    seen = set(); unique: list[dict] = []
    for occ in occurrences:
        key = (occ["page"], occ["left"], occ["top"], occ["right"], occ["bottom"])
        if key not in seen:
            seen.add(key); unique.append(occ)
    return unique


def analyze_pdf(input_path: str, output_json_path: str,
                viz_data: dict,
                ner_models: list | None = None,
                *,
                enable_regex_detect: bool = False) -> dict:
    """
    Run detection (all 5 NER models + pattern regex) on every page, using the
    pre-extracted viz_data v4 dict (produced by pdf_analysis_viz_data_v4).

    enable_regex_detect (keyword-only, default False): when False (the
    default for every current caller), the Presidio/regex pattern
    recognizers and the multi-model NER detect loop DO NOT run; analyze_pdf
    writes the empty-shell result (recommendations: []) that
    _populate_recommendations_from_client_config expects. PII detection is
    therefore CONFIG-ONLY (per-client saved literals via
    _find_entity_occurrences). USER directive 2026-05-17: "auto detect pii
    via regex which i have disabled since the beginning ... please remove
    auto detect by regex. only auto detect by config." Set True ONLY to
    re-enable autonomous regex/NER auto-detect (no current caller does).

    viz_data["pages"][str(page_no)]["xml_texts"] is the source of truth for
    text elements. Each xml_text carries an `id` and a `words[]` subindex
    with per-word bboxes. _find_entity_occurrences consumes this directly.

    Writes `<output_json_path>` (recommendations) + `<output_json_path>.log`.
    """
    from datetime import datetime as _dt
    from time import perf_counter as _pc

    log_path = output_json_path + ".log"
    log      = _LogWriter(log_path)
    t0       = _pc()

    # CONFIG-ONLY gate (USER directive 2026-05-17). When enable_regex_detect
    # is False (the default for every current caller), the regex/Presidio
    # analyzer is NOT constructed and the per-page detect loop below is
    # skipped — analyze_pdf produces the empty shell (recommendations: [])
    # and detection is driven solely by per-client saved literals via
    # _populate_recommendations_from_client_config. See analyze_pdf docstring.
    analyzer  = _build_analyzer() if enable_regex_detect else None
    _models   = (ner_models or []) if enable_regex_detect else []
    min_votes = min(2, len(_models)) if _models else 1

    sep = "=" * 80
    log.write(sep)
    log.write("PDF PII Analyzer v3 — Run Log")
    log.write(sep)
    log.write(f"File     : {input_path}")
    log.write(f"Output   : {output_json_path}")
    log.write(f"Started  : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.write(f"Models   : {[n for n,_ in _models]} (min {min_votes} vote(s))")
    log.write(f"viz_data : schema={viz_data.get('schema_version','?')} "
              f"source={viz_data.get('source_filename','?')}")
    log.write(sep)

    pages_map: dict = viz_data.get("pages") or {}
    try:
        total_pages = max(int(k) for k in pages_map.keys()) if pages_map else 0
    except Exception:
        total_pages = len(pages_map)
    log.write(f"\n  {total_pages} page(s)\n")

    agg: dict[str, dict] = {}

    # CONFIG-ONLY gate: when regex/NER auto-detect is disabled (default),
    # skip the entire per-page detect loop. `agg` stays empty so the
    # period-alias / cross-page-sweep blocks below iterate nothing and
    # `recommendations` resolves to []. _populate_recommendations_from_client_config
    # then supplies the only recommendations, from per-client saved literals.
    _detect_pages = range(1, total_pages + 1) if enable_regex_detect else range(0)
    if not enable_regex_detect:
        log.write("\n  [config-only mode] regex/NER auto-detect disabled "
                  "(enable_regex_detect=False) — no autonomous PII detection; "
                  "recommendations come solely from client config literals.")

    for page_1based in _detect_pages:
        page_data = pages_map.get(str(page_1based)) or pages_map.get(page_1based) or {}
        # Filter ghosts at boundary — see tokenize_pdf for rationale.
        xml_texts: list[dict] = _filter_ghosts_and_taint(
            page_data.get("xml_texts") or []
        )
        t_page = _pc()

        full_text = _build_full_text(xml_texts) if xml_texts else ""
        if not full_text.strip():
            log.write(f"PAGE {page_1based}: (empty)")
            continue

        log.write(f"\nPAGE {page_1based}/{total_pages}")
        log.write("-" * 40)

        # Pattern recognizers — broad built-ins + custom SG/address/account
        pat_results = analyzer.analyze(
            text=full_text, language="en",
            entities=[
                "ACCOUNT_NUMBER", "ADDRESS",
                "EMAIL_ADDRESS", "PHONE_NUMBER",
                "CREDIT_CARD", "IBAN_CODE",
                "SG_NRIC", "SG_UEN", "SG_PHONE",
                "ISIN",
            ],
            score_threshold=0.50,
        )
        if pat_results:
            log.write("  [pattern recognizers]")
        # Suppress overlapping substring matches — e.g. drop ACCOUNT_NUMBER
        # "767-7462" when PHONE_NUMBER "800-767-7462" already covers it.
        for r in _suppress_contained(pat_results):
            if r.entity_type == "ISIN":
                continue
            ent = full_text[r.start:r.end].strip()
            if not ent:
                continue
            # Normalise Presidio entity names to what the frontend dropdown
            # knows. Mismatched names (e.g. EMAIL_ADDRESS vs EMAIL) make the
            # UI silently fall back to the first dropdown option (PERSON).
            cat = _normalise_category(r.entity_type)
            new_occs = _find_entity_occurrences(ent, xml_texts, page_1based)
            # Reject phone matches that were glued together from disjoint
            # glyph runs — chart axis labels, multi-column cells, etc. The
            # gap between pieces is measured against their own widths.
            if (cat in ("PHONE_NUMBER", "SG_PHONE")
                    and _phone_match_is_disjoint(new_occs)):
                log.write(f"    [{cat:<14}] {repr(ent):<50}  score={r.score:.2f}  ✗ disjoint bboxes (gap > width)")
                continue
            log.write(f"    [{cat:<14}] {repr(ent):<50}  score={r.score:.2f}")
            entry = agg.setdefault(ent, {
                "entity": ent, "suggested_category": cat,
                "models_voted": [], "vote_count": 0,
                "consensus_passed": True, "occurrences": [],
                "regex_score": r.score,
            })
            if "regex" not in entry["models_voted"]:
                entry["models_voted"].append("regex")
            entry["vote_count"] = len(entry["models_voted"])
            entry["occurrences"].extend(new_occs)

        # PERSON (multi-model)
        person_votes = _find_persons_consensus(full_text, _models)
        if person_votes:
            log.write("  [PERSON — multi-model]")
            by_model: dict[str, list[str]] = {}
            for ent, voters in person_votes.items():
                for m in voters:
                    by_model.setdefault(m, []).append(ent)
            for mname, _m in _models:
                found = by_model.get(mname, [])
                log.write(f"    [{mname}]  {found if found else '(none)'}")
            log.write("  [consensus]")
            for ent, voters in sorted(person_votes.items(), key=lambda x: -len(x[1])):
                tag = "✓ accepted" if len(voters) >= min_votes else "✗ below threshold"
                log.write(f"    {repr(ent):<35}  {len(voters)}/{len(_models)} votes "
                          f"[{', '.join(voters)}]  {tag}")
                entry = agg.setdefault(ent, {
                    "entity": ent, "suggested_category": "PERSON",
                    "models_voted": [], "vote_count": 0,
                    "consensus_passed": False, "occurrences": [],
                })
                for v in voters:
                    if v not in entry["models_voted"]:
                        entry["models_voted"].append(v)
                entry["vote_count"] = len(entry["models_voted"])
                entry["consensus_passed"] = entry["vote_count"] >= min_votes
                entry["occurrences"].extend(
                    _find_entity_occurrences(ent, xml_texts, page_1based)
                )
        else:
            log.write("  [PERSON] no candidates")

        # ── Title auto-capture ────────────────────────────────────────────
        # For each PERSON detected, scan the page text for "{TITLE} {name}"
        # occurrences and register them as separate candidates linked via
        # alias_of. NER models don't attach titles to their span; without
        # this the bare name gets tokenized but Mr/Mrs/Mdm/Ms/Dr prefixes
        # leak the customer's gender/title in the tokenized PDF.
        title_re = re.compile(
            r"\b(Mr|Mrs|Mdm|Ms|Miss|Dr|Prof|Sir|Madam)\.?\s+",
            re.IGNORECASE,
        )
        variants_found: list[tuple[str, str]] = []  # (variant, base)
        current_persons = [k for k, v in agg.items()
                            if v.get("suggested_category") == "PERSON"
                            and "alias_of" not in v]
        for person in current_persons:
            plow = person.lower()
            for m in title_re.finditer(full_text):
                tail = full_text[m.end() : m.end() + len(person)]
                if tail.lower() == plow:
                    title_tok = m.group(0).rstrip()
                    variant = f"{title_tok} {person}"
                    if variant != person:
                        variants_found.append((variant, person))

        if variants_found:
            log.write("  [PERSON — title auto-capture]")
            for variant, base in variants_found:
                entry = agg.setdefault(variant, {
                    "entity": variant,
                    "suggested_category": "PERSON",
                    "models_voted": ["title_expansion"],
                    "vote_count": 1,
                    "consensus_passed": True,
                    "occurrences": [],
                    "alias_of": base,
                })
                # Keep alias_of correct if an earlier page registered the
                # same variant against a different base (shouldn't happen
                # in practice; first wins).
                entry.setdefault("alias_of", base)
                entry["occurrences"].extend(
                    _find_entity_occurrences(variant, xml_texts, page_1based)
                )
                log.write(f"    {repr(variant):<40}  → alias_of {base!r}")

        log.write(f"  → page elapsed {_pc() - t_page:.1f}s")

    # ── Period-initial alias ─────────────────────────────────────────────
    # Middle-initial punctuation varies between documents:
    #   NER emits     "John W. Doe"
    #   PDF renders   "John W Doe"  (or vice-versa)
    # Register the period-stripped variant as an alias so both tokenize to
    # the same [NAME_K]. Only fires when the stripped form actually differs
    # and isn't already in agg under some other relationship.
    _INITIAL_RE = re.compile(r"\b([A-Z])\.(?=\s|$)")
    for base_ent in list(agg.keys()):
        base_info = agg[base_ent]
        if base_info.get("suggested_category") != "PERSON":
            continue
        if base_info.get("alias_of"):
            continue  # don't chain aliases
        stripped = _INITIAL_RE.sub(r"\1", base_ent)
        if stripped == base_ent:
            continue
        # Skip if stripped form already exists (either as standalone entity
        # or as another alias)
        if stripped in agg:
            continue
        agg[stripped] = {
            "entity": stripped,
            "suggested_category": "PERSON",
            "models_voted": ["period_alias"],
            "vote_count": 1,
            "consensus_passed": True,
            "occurrences": [],
            "alias_of": base_ent,
        }
        log.write(f"\n[period-alias]  {stripped!r} → alias_of {base_ent!r}")

    # ── Cross-page occurrence sweep ──────────────────────────────────────
    # Per-page detection only finds entities via NER/regex on that page.
    # A short entity like "John W. Doe" is often flagged bare on some
    # pages and only as part of a longer string (e.g. "John W. Doe -
    # Individual TOD") on others — the bare name hides as a prefix of the
    # longer string and never gets searched on those pages. Sweep every
    # entity across every page now to backfill those missed occurrences.
    log.write("\n[cross-page sweep]")
    t_sweep = _pc()
    for ent, info in list(agg.items()):
        for page_1based in range(1, total_pages + 1):
            pdata = pages_map.get(str(page_1based)) or pages_map.get(page_1based) or {}
            xts = pdata.get("xml_texts") or []
            if not xts:
                continue
            info["occurrences"].extend(
                _find_entity_occurrences(ent, xts, page_1based)
            )
    log.write(f"  cross-page sweep elapsed {_pc() - t_sweep:.1f}s")

    # Dedupe occurrences per entity
    for ent, info in agg.items():
        seen = set()
        unique = []
        for occ in info["occurrences"]:
            key = (occ["page"], round(occ["left"],1), round(occ["top"],1),
                   round(occ["right"],1), round(occ["bottom"],1))
            if key not in seen:
                seen.add(key)
                unique.append(occ)
        info["occurrences"] = unique

    recommendations = sorted(agg.values(),
                             key=lambda r: (-r["vote_count"], r["entity"].lower()))
    for i, r in enumerate(recommendations):
        r["id"] = f"e{i}"

    result = {
        "pdf": Path(input_path).name,
        "analyzed_at": _dt.now().isoformat(),
        "models_used": [n for n, _ in _models],
        "min_votes_for_consensus": min_votes,
        "total_pages": total_pages,
        "recommendations": recommendations,
        "decisions": {},          # user fills these in via UI
        "manual_additions": [],   # user can add entities the models missed
        "log_file": log_path,
        # viz_data is the source of truth for UI click-to-select.
        # The review frontend loads it separately from <stem>_viz_data.json.
    }

    # ── Log summary ───────────────────────────────────────────────────────
    sep = "=" * 80
    log.write(f"\n{sep}")
    log.write("SUMMARY")
    log.write(sep)
    log.write(f"Finished     : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.write(f"Elapsed      : {_pc() - t0:.1f}s")
    log.write(f"Pages        : {total_pages}")
    log.write(f"Unique candidates: {len(recommendations)}")
    for r in recommendations:
        log.write(f"  [{r['suggested_category']:<15}] {repr(r['entity']):<40} "
                  f"{r['vote_count']} vote(s) {r['models_voted']}")
    log.write(sep)
    log.close()

    Path(output_json_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def _mrot_hit_inside(hits: list, box) -> bool:
    """True if any search_for hit-rect is geometrically contained in `box`.
    Used only for the deterministic both-hit tie-break (ADR-ROT-CAVEAT §6.2).
    """
    for h in hits:
        try:
            if box.contains(h):
                return True
        except Exception:
            continue
    return False


def _mrot_derotate_choice(page, occ, xform, page_dir=None):
    """M-ROT W9-d per-occurrence derotation discriminator (ADR-ROT +
    ADR-ROT-CAVEAT, ARCH v1.3, 03_adr.md / 01_architecture.md §6.2).

    Decide the redaction/label `fitz.Rect` for ONE occurrence on a page:
      - non-rotated page (xform is None) -> return the EXACT pre-M-ROT raw
        `fitz.Rect(occ[...])` object (DoD-3 byte-identical by construction;
        no probe, no behaviour change);
      - rotated page -> probe the occurrence's own glyph text with the
        in-repo-proven user-space `page.search_for` discriminator (mirrors
        strip_ghosts_from_pdf TOK:1146/1150) and pick raw-vs-derotated by
        which clip actually lands on the glyphs:
          * der-only hit  -> der  (bbox was pre-rotation; the reproducer)
          * raw-only hit  -> raw  (Step-4 already glyph-aligned this cell —
                                   the USER MANDATORY caveat: a blanket
                                   transform WOULD have broken it)
          * both hit      -> deterministic tighter/containing tie-break
          * neither hit   -> page-direction fallback (fragmented
                              cross-xml_text ID-3, e.g. a two-word
                              entity split across XML text nodes)

    `page_dir`: optional pre-established page-direction default ("der"/"raw")
    for un-probeable occurrences (computed once per rotated page by the
    caller). Returns (rect, choice_str) where choice_str is one of
    "raw" | "der" | "page-direction-default" for the DoD-4 evidence log.
    """
    raw = fitz.Rect(occ["left"], occ["top"],
                     occ["right"], occ["bottom"])
    if xform is None:
        # Non-rotated page: IDENTITY. Exact pre-M-ROT expression/object.
        return raw, "raw"
    der = raw * xform                                  # mirrors TOK:1142
    probe = (occ.get("full_text") or "").strip()
    # NOTE: ARCH v1.3 ADR-ROT-CAVEAT listed a secondary `occ.get("content")`
    # fallback; SA_Reviewer v1.3 (PM/inbox/20260516_155045) flagged it as
    # dead (no `content` key in the Occurrence shape) — dropped here so it
    # can never produce a wrong silent pick. Absent/short probe degrades
    # SAFELY to the page-direction fallback below, never to a wrong rect.
    if probe and len(probe) >= 2:
        # search_for returns rects in user-space so it works on rotated
        # pages once the bbox is transformed (mirrors TOK:1146/1150).
        try:
            hit_der = page.search_for(probe, clip=der)
        except Exception:
            hit_der = []
        try:
            hit_raw = page.search_for(probe, clip=raw)
        except Exception:
            hit_raw = []
        if hit_der and not hit_raw:
            return der, "der"          # derotation lands on the glyphs
        if hit_raw and not hit_der:
            return raw, "raw"          # already glyph-aligned (Step-4 won)
        if hit_der and hit_raw:
            # Both clips contain the text — deterministic tie-break: prefer
            # `der` ONLY if its hit is contained in `der` and `raw`'s hit is
            # NOT contained in `raw`; else `raw` (ADR-ROT-CAVEAT §6.2).
            if _mrot_hit_inside(hit_der, der) and not _mrot_hit_inside(
                    hit_raw, raw):
                return der, "der"
            return raw, "raw"
        # neither clip finds the text (fragmented cross-xml_text, ID-3):
        # fall through to the page-direction fallback below.
    # Page-direction fallback for un-probeable occurrences. The caller
    # establishes `page_dir` once per rotated page from this page's
    # probeable occurrences; if none probeable, caller passes "der"
    # (SA §1.3 proved the reproducer direction = derotation required).
    if page_dir == "raw":
        return raw, "page-direction-default"
    return der, "page-direction-default"


def apply_decisions(input_path: str, output_path: str,
                    decisions_json_path: str,
                    viz_data: dict) -> tuple[dict, dict]:
    """
    Read a recommendations+decisions JSON file, apply user's decisions to
    rewrite the PDF content streams via pikepdf.

    viz_data (v4) supplies xml_texts per page so we do NOT re-extract. The
    pikepdf stream rewrite still needs the original PDF open via fitz /
    pikepdf / pdfplumber (for position-based replacement fallback).

    Uses PURELY LOCAL state — no module globals — so concurrent calls on
    different PDFs are safe and each PDF has its own independent [NAME_1],
    [ACCOUNT_1] counters starting from 1.
    """
    with open(decisions_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    recommendations: list = data.get("recommendations", []) or []
    decisions:       dict = data.get("decisions", {}) or {}
    manual:          list = data.get("manual_additions", []) or []

    # Per-call local state — no module globals
    local_value_to_token: dict[str, str] = {}
    local_token_to_value: dict[str, str] = {}
    cat_counters: dict[str, int] = {}

    PREFIX_MAP = {
        "PERSON": "NAME", "ACCOUNT_NUMBER": "ACCOUNT",
        "PHONE_NUMBER": "PHONE", "EMAIL": "EMAIL",
        "EMAIL_ADDRESS": "EMAIL",
        "ADDRESS": "ADDRESS", "DATE_OF_BIRTH": "DOB",
        "DATE_TIME": "DATE",
        "CREDIT_CARD": "CARD", "IBAN_CODE": "IBAN",
        "URL": "URL",
        "SG_NRIC": "NRIC", "SG_UEN": "UEN", "SG_PHONE": "PHONE",
        "ISIN": "ISIN",
        "OTHER": "OTHER",
    }

    def _token_for(category: str, value: str) -> str:
        """Auto-assign token when no explicit one was supplied."""
        norm = value.strip()
        if norm in local_value_to_token:
            return local_value_to_token[norm]
        cat_counters[category] = cat_counters.get(category, 0) + 1
        prefix = PREFIX_MAP.get(category, "OTHER")
        tok = f"[{prefix}_{cat_counters[category]}]"
        local_value_to_token[norm] = tok
        local_token_to_value[tok] = norm
        return tok

    def _register_explicit_token(category: str, value: str, token_bare: str) -> str:
        """Use the frontend-assigned token (aliases allowed — many values → one token)."""
        norm = value.strip()
        if norm in local_value_to_token:
            # already registered — validate it matches
            existing = local_value_to_token[norm]
            if existing != f"[{token_bare}]":
                raise ValueError(
                    f"token conflict for value {norm!r}: {existing} vs [{token_bare}]"
                )
            return existing
        tok = f"[{token_bare}]"
        local_value_to_token[norm] = tok
        # token_to_value keeps the FIRST value under each token as canonical;
        # aliases share the same token but don't overwrite the reverse mapping.
        if tok not in local_token_to_value:
            local_token_to_value[tok] = norm
        # Bump counter if the explicit K exceeds current counter (keeps auto
        # assignment sensible for any still-unassigned entities later).
        m = re.match(r"^([A-Z_]+)_(\d+)$", token_bare)
        if m:
            prefix_found, k_found = m.group(1), int(m.group(2))
            # Map prefix back to category group by reverse lookup
            cat_counters[category] = max(cat_counters.get(category, 0), k_found)
        return tok

    def _resolve_token(category: str, value: str, explicit: str | None,
                       alias_of: str | None = None) -> str:
        if explicit:
            bare = explicit.strip()
            if bare.startswith("[") and bare.endswith("]"):
                bare = bare[1:-1]
            return _register_explicit_token(category, value, bare)
        # Alias: reuse the target entity's token if it's already been
        # assigned. This is how "Mr Teo Zi Xiang" latches onto the same
        # [NAME_K] as "Teo Zi Xiang" without the user clicking anything.
        if alias_of:
            norm_target = alias_of.strip()
            if norm_target in local_value_to_token:
                tok = local_value_to_token[norm_target]
                local_value_to_token[value.strip()] = tok
                return tok
        return _token_for(category, value)

    # Gather approved values.
    #
    # Precedence (high → low):
    #   1. decisions[entity]                — user explicit choice (may include token)
    #   2. manual_additions[]               — user-added (may include token)
    #   3. recommendations[]                — auto-approved unless explicitly rejected
    #
    # Each decision / manual may carry a "token" field (e.g. "NAME_2")
    # set by the frontend's token dropdown. Aliasing = two values → same
    # token. If absent, we auto-assign the smallest unused K per category.
    # TWO-AXIS DECISION MODEL:
    #   accept/reject — do we touch this entity at all? Drives both redaction
    #                   AND tokenization. REJECT means raw text survives.
    #   tokenize flag — for accepted entities only: does the black box get a
    #                   label like "[ACCOUNT_1]" or stay blank (redact-only).
    #
    # These are independent. An entity can be accepted + not-tokenized
    # (destroy bytes, draw black rect, no label) — that is STILL a successful
    # redaction. Do not conflate "no label" with "leave raw PII in place".
    approved: dict[str, str] = {}           # value → token (always assigned)
    redact_only_set: set[str] = set()       # accepted but no label on black box
    explicitly_rejected: set[str] = set()

    for entity, dec in decisions.items():
        cat = dec.get("category")
        if dec.get("rejected") or cat == "REJECT":
            explicitly_rejected.add(entity)
            continue
        if cat:
            approved[entity] = _resolve_token(cat, entity, dec.get("token"))
            # Default: redact-only UNLESS ACCOUNT_NUMBER. Mirrors the
            # frontend defaultTokenizeFor rule — a missing tokenize flag
            # must NOT mean "slap a label on it".
            tokenize = dec.get("tokenize", cat == "ACCOUNT_NUMBER")
            if not tokenize:
                redact_only_set.add(entity)

    for m in manual:
        cat = m.get("category")
        if not cat or cat == "REJECT":
            continue
        approved[m["entity"]] = _resolve_token(cat, m["entity"], m.get("token"))
        # Manual additions default to REDACT-ONLY (black box, no label).
        # Only ACCOUNT_NUMBER auto-tokenises by default, because account
        # numbers are the one category where a stable [ACCOUNT_K] token
        # is actually useful downstream. For everything else the user
        # must set tokenize=true explicitly to get a label.
        tokenize = m.get("tokenize", cat == "ACCOUNT_NUMBER")
        if not tokenize:
            redact_only_set.add(m["entity"])

    # Recommendations: every non-rejected rec is accepted. No category-level
    # gate — that gate was the bug that left EMAIL / PHONE / PERSON / ADDRESS
    # / DATE / NRIC / UEN / URL raw in the output Tj stream, making tokenized
    # PDFs still leak PII via any text extractor.
    #
    # Two-pass so alias variants (alias_of = "Teo Zi Xiang") resolve AFTER
    # their base entity has a token assigned.
    def _accept_rec(r):
        ent = r.get("entity")
        if not ent or ent in approved or ent in explicitly_rejected:
            return
        cat = r.get("suggested_category")
        if not cat or cat == "REJECT":
            return
        dec = decisions.get(ent) or {}
        # Mirror the frontend auto-reject rule for below-threshold PERSON
        # candidates. The UI already shows these in the Rejected pool on
        # load; if the user never interacted with them (no decision), the
        # backend must treat them as rejected too — otherwise Apply would
        # tokenize entities the user sees as rejected.
        # Explicit un-reject (rejected=false) escapes the rule.
        if (cat == "PERSON"
                and r.get("consensus_passed") is False
                and dec.get("rejected") is not False):
            return
        approved[ent] = _resolve_token(cat, ent, dec.get("token"),
                                       alias_of=r.get("alias_of"))
        # Default: redact-only unless ACCOUNT_NUMBER. Any missing or
        # falsey tokenize flag means no label on the black box.
        tokenize = dec.get("tokenize", cat == "ACCOUNT_NUMBER")
        if not tokenize:
            redact_only_set.add(ent)

    # Pass A — non-alias recommendations first
    for r in recommendations:
        if not r.get("alias_of"):
            _accept_rec(r)
    # Pass B — alias recommendations (their target should be resolved by now)
    for r in recommendations:
        if r.get("alias_of"):
            _accept_rec(r)

    # Pre-captured occurrences from the frontend (recommendations AND manual
    # additions). The frontend already did the hit-testing against viz_data
    # when the user selected text and clicked Add. Trust those positions —
    # do NOT re-run _find_entity_occurrences for entities that came with
    # their bboxes attached, because:
    #   (1) manual multi-line addresses concatenate text across xml_texts,
    #       which _find_entity_occurrences cannot re-assemble from a single
    #       joined string; those entities end up in `approved` with a token
    #       name but ZERO bboxes drawn — invisible in the output PDF.
    #   (2) the frontend's findOccurrencesInPdf already handles substring
    #       fallbacks the backend cannot replicate without re-parsing.
    frontend_occs: dict[str, list[dict]] = {}
    for src in list(recommendations) + list(manual):
        ent = src.get("entity")
        pre = src.get("occurrences") or []
        if ent and pre:
            frontend_occs.setdefault(ent, []).extend(pre)

    # Per-token per-word bboxes collected as we process pages
    token_bboxes: list[dict] = []

    # REAL redaction via PyMuPDF. add_redact_annot + apply_redactions
    # destroys the text glyphs under each rect (they disappear from the
    # content stream, not just the visible layer) and fills the rect with
    # the chosen colour. Every text extractor (pdfplumber, pdfminer, Acrobat
    # copy, VLM OCR of text layer) then returns blanks at those positions —
    # this is what "redacted" actually means in the PDF spec.
    #
    # pikepdf's Tj-payload swap (the old approach) left the visible glyphs
    # intact unless EVERY entity was explicitly marked tokenize=true, which
    # is why EMAIL / PHONE / PERSON / ADDRESS leaked through in 4 of 5
    # sample statements. fitz.apply_redactions does not care about our
    # category gates — if the rect is added, the text under it is gone.
    fitz_doc    = fitz.open(input_path)
    pages_map   = viz_data.get("pages") or {}
    total_pages = len(fitz_doc)

    for page_no in range(total_pages):
        page = fitz_doc[page_no]

        page_data = (pages_map.get(str(page_no + 1))
                     or pages_map.get(page_no + 1) or {})
        all_xml_texts = page_data.get("xml_texts") or []
        # Filter ghosts AND scrub cell_merged_value where ghosts shared a
        # cell_group with a non-ghost — otherwise the merged-value shortcut
        # leaks the ghost back into entity-finding logic.
        xml_texts: list[dict] = _filter_ghosts_and_taint(all_xml_texts)
        xml_texts_ghost: list[dict] = [
            xt for xt in all_xml_texts if xt.get("is_ghost")
        ]
        # Per-span rotation lookup. viz_data v4+ records `rotation` on
        # xml_texts whose glyphs are rendered rotated within an
        # otherwise-non-rotated page (Schwab statements: page.rotation=0,
        # but the company-name in the margin has xt.rotation=90). Absent
        # field means horizontal-relative-to-page (default 0). This is the
        # deterministic per-occurrence orientation signal — combined with
        # page.rotation at insert_text time it gives the correct rotate.
        _xt_rotation_by_id: dict = {
            xt.get("id"): int(xt.get("rotation") or 0)
            for xt in all_xml_texts if xt.get("id") is not None
        }

        # Ghost stripping is NOT done here. The source PDF passed in to
        # apply_decisions has already been ghost-stripped by
        # `strip_ghosts_from_pdf()` upstream of analyze (called by the
        # watcher in pii_review._run_analyze). No fitz redact_annot is used
        # for ghosts — strip is direct content-stream surgery via pikepdf,
        # the same mechanism sanitisation uses for metadata removal.

        # Collect per-entity occurrences on this page. Prefer the frontend's
        # pre-captured bboxes (it did hit-testing during user selection)
        # and fall back to _find_entity_occurrences only when the frontend
        # had none. xml_texts drive the y band + word bboxes for x.
        page_ops: list[tuple] = []    # (entity, token, redact_only, occ)
        for entity, token in approved.items():
            pre = frontend_occs.get(entity)
            if pre:
                page_occs = [o for o in pre if o.get("page") == page_no + 1]
            else:
                page_occs = _find_entity_occurrences(
                    entity, xml_texts, page_no + 1
                )
            for occ in page_occs:
                page_ops.append(
                    (entity, token, entity in redact_only_set, occ)
                )

        if not page_ops:
            continue

        # === M-ROT (QA-FIND-1, ARCH v1.3 ADR-ROT + ADR-ROT-CAVEAT) =========
        # Per-occurrence derotation: decide the redaction/label rect ONCE per
        # occurrence here, then have BOTH Pass-1 and the label pass consume
        # the IDENTICAL decided rect (keyed by id(occ)) so the black rect and
        # the white label/sidecar geometry stay co-spatial. Computed once per
        # page (mirrors strip_ghosts_from_pdf TOK:1133/1142). Non-rotated
        # pages: xform is None -> decided rect == the exact pre-M-ROT
        # fitz.Rect(occ[...]) object (DoD-3 byte-identical by construction).
        xform = page.derotation_matrix if page.rotation else None
        # First pass over page_ops: resolve every PROBEABLE occurrence so we
        # can establish this rotated page's dominant bbox-space direction for
        # the un-probeable (fragmented cross-xml_text, ID-3) occurrences.
        _mrot_choice: dict = {}      # id(occ) -> (rect, choice_str)
        _dir_der = 0
        _dir_raw = 0
        for _e, _t, _ro, occ in page_ops:
            _rect, _choice = _mrot_derotate_choice(page, occ, xform)
            _mrot_choice[id(occ)] = (_rect, _choice)
            if xform is not None and _choice == "der":
                _dir_der += 1
            elif xform is not None and _choice == "raw":
                _dir_raw += 1
        # Establish the page-direction default for un-probeable occs (only
        # relevant on a rotated page). If this page has a clear probed
        # dominant use it; if NO occ was probeable, default to "der" when
        # page.rotation (SA §1.3: the reproducer direction = derotation
        # required) — _mrot_derotate_choice already defaults der when
        # page_dir is None, so we only need to override toward "raw" when
        # raw strictly dominated the probed occurrences.
        _page_dir = "raw" if (xform is not None and _dir_raw > _dir_der) \
            else "der"
        if xform is not None:
            # Re-resolve ONLY the un-probeable occurrences (those whose
            # choice came back "page-direction-default") with the now-known
            # page direction, so the fallback is page-consistent.
            for _e, _t, _ro, occ in page_ops:
                _rect, _choice = _mrot_choice[id(occ)]
                if _choice == "page-direction-default":
                    _rect, _choice = _mrot_derotate_choice(
                        page, occ, xform, page_dir=_page_dir)
                    _mrot_choice[id(occ)] = (_rect, _choice)
                _r = _mrot_choice[id(occ)][0]
                _c = _mrot_choice[id(occ)][1]
                # Greppable per-occurrence DoD-4 evidence line (log/status
                # only — NOT a wire field; QA builds the binding evidence
                # table from these). Tag: M-ROT-EVID.
                _raw_dbg = fitz.Rect(occ["left"], occ["top"],
                                     occ["right"], occ["bottom"])
                _der_dbg = _raw_dbg * xform
                try:
                    _hr = bool(page.search_for(
                        (occ.get("full_text") or "").strip(),
                        clip=_raw_dbg)) if (
                        occ.get("full_text") or "").strip() else None
                except Exception:
                    _hr = None
                try:
                    _hd = bool(page.search_for(
                        (occ.get("full_text") or "").strip(),
                        clip=_der_dbg)) if (
                        occ.get("full_text") or "").strip() else None
                except Exception:
                    _hd = None
                # PII redaction: log entity length only, never the literal
                # value, since this stream is captured in flask logs. Gate
                # behind PII_REVIEW_DEBUG_MROT env so it doesn't fire in
                # normal runs (hundreds of lines per Apply otherwise).
                if os.environ.get("PII_REVIEW_DEBUG_MROT"):
                    print(
                        f"M-ROT-EVID page={page_no + 1} "
                        f"rotation={page.rotation} entity_len={len(_e)} "
                        f"xml_text_id={occ.get('xml_text_id')} "
                        f"raw={tuple(round(v, 2) for v in _raw_dbg)} "
                        f"der={tuple(round(v, 2) for v in _der_dbg)} "
                        f"hit_raw={_hr} hit_der={_hd} choice={_c}"
                        + (" DIRECTION-BY-DEFAULT"
                           if _c == "page-direction-default" else ""),
                        flush=True,
                    )
        # ==================================================================

        # Pass 1 — stage redaction rects (destroys underlying text + black fill)
        for _entity, _token, _ro, occ in page_ops:
            rect = _mrot_choice[id(occ)][0]
            page.add_redact_annot(rect, fill=(0, 0, 0))

        # Apply the redactions — this is the destructive step. After this
        # call, the original glyphs under every staged rect are gone from
        # the content stream, replaced by a filled black rectangle.
        page.apply_redactions()

        # Pass 2 — overlay the token label in white on the black rect for
        # entities where tokenize is on. Redact-only entities stay as plain
        # black rects (no label).
        #
        # We use insert_text (baseline-anchored) and not insert_textbox
        # because insert_textbox refuses to render if the text doesn't fit
        # its internal padding model — it returns a negative value and
        # draws nothing at all. insert_text always renders and we shrink
        # the fontsize ourselves using get_text_length so the glyphs stay
        # inside the redaction rect.
        for entity, token, redact_only, occ in page_ops:
            # M-ROT: IDENTICAL decided rect as Pass-1 (same id(occ) key) so
            # the white label stays co-spatial with the black rect.
            rect = _mrot_choice[id(occ)][0]
            if not redact_only:
                # M-ROT-TEXT: orient the white label to match the original
                # glyph orientation. Read DETERMINISTICALLY from viz_data
                # (per-span `xt.rotation`) + page.rotation. No bbox-aspect
                # heuristic, no fitz re-probe.
                #   total_rotation = (xt.rotation + page.rotation) % 360
                # Verified rotate→dir mapping (fitz insert_text, CCW about
                # anchor):
                #   rotate=0   → dir=(1, 0)   horizontal LTR
                #   rotate=90  → dir=(0, -1)  vertical bottom-to-top
                #   rotate=180 → dir=(-1, 0)  horizontal RTL
                #   rotate=270 → dir=(0, 1)   vertical top-to-bottom
                # Anchor follows the advance direction. With rotate=N:
                #   N=0 advances right, 90 up, 180 left, 270 down.
                # Descent margin is taken off the side OPPOSITE advance.
                xt_rot = _xt_rotation_by_id.get(occ.get("xml_text_id"), 0)
                rot = (xt_rot + (page.rotation % 360)) % 360
                tall = rot in (90, 270)
                line_h = rect.width  if tall else rect.height
                line_l = rect.height if tall else rect.width
                fontsize = min(10.0, max(3.0, line_h * 0.85))
                # Shrink until the rendered token fits along the line.
                while fontsize > 3.0 and fitz.get_text_length(
                        token, fontsize=fontsize, fontname="helv"
                ) > line_l:
                    fontsize -= 0.5
                desc = max(0.5, line_h * 0.15)
                if rot == 0:
                    anchor = (rect.x0 + 0.5, rect.y1 - desc)
                elif rot == 90:
                    anchor = (rect.x1 - desc, rect.y1 - 0.5)
                elif rot == 180:
                    anchor = (rect.x1 - 0.5, rect.y0 + desc)
                else:  # rot == 270
                    anchor = (rect.x0 + desc, rect.y0 + 0.5)
                page.insert_text(
                    anchor,
                    token,
                    fontname="helv",
                    fontsize=fontsize,
                    color=(1, 1, 1),
                    rotate=rot,
                )
            # Sidecar map bbox — both redact-only and tokenized go here so
            # pdf_qc viewer can always draw the overlay regardless of label.
            token_bboxes.append({
                "token": token, "value": entity,
                "page":   occ["page"],
                "left":   occ["left"],   "top":    occ["top"],
                "right":  occ["right"],  "bottom": occ["bottom"],
                "xml_text_id": occ.get("xml_text_id"),
                "word_ids":    occ.get("word_ids", []),
                "partial":     occ.get("partial", False),
                "redact_only": redact_only,
            })

    fitz_doc.save(output_path, deflate=True, garbage=4)
    fitz_doc.close()

    # ── #2 (M2) honour tokenize:false — ADR-2 implementation (i), the SAFE
    # DEFAULT (project_state WP-c; arch 01 §2.3; ADR-2 03_adr.md). USER
    # decision-direction: KI:111-118 / SC-2 / OBS-C. NOT a self-authorised
    # change — gated pack/arch/ADR direction.
    #
    # OBS-C confirmed: the on-PDF label is ALREADY suppressed for redact-only
    # entities at the `if not redact_only:` insert_text gate above (KEEP — do
    # not touch). The residual #2 defect is that a token is STILL registered
    # for a redact-only (tokenize:false) entity via _resolve_token and leaks
    # into .map.json value_to_token / token_to_value (pack 01 §3).
    #
    # Impl (i) — byte-identical-by-construction: every _resolve_token call
    # (incl. redact-only) ran exactly as before, so cat_counters advanced
    # identically and EVERY non-redact-only entity's token string is
    # unchanged (SC-2 B / I-AB3 guaranteed by construction — impl (ii), the
    # skip-the-call variant, is NOT used: it is permitted ONLY with a real
    # SC-2 B regression proof per WP-c / ADR-2, which is downstream QA's, not
    # this build's, to produce). Here, just before the .map.json write,
    # delete every redact-only entity's key from local_value_to_token and
    # any token left orphaned (no remaining non-redact-only source) from
    # local_token_to_value. The redaction Pass-1 black box and the bboxes[]
    # entry are NOT removed (bbox != token; SC-2 A allows the box to remain
    # with redact_only:true). SA-F3 (binding, arch 01 §2.3): the retained
    # redact-only bboxes[] entry's `token` field SHALL be the empty string
    # "" — schema field retained (I-P2), not a registered token, cannot be
    # mistaken for a real mapping.
    if redact_only_set:
        for _ent in redact_only_set:
            _norm = _ent.strip()
            _leaked_tok = local_value_to_token.pop(_norm, None)
            if _leaked_tok is not None:
                # Orphan-check: drop the reverse mapping ONLY if no remaining
                # (non-redact-only) value still maps to that token. A token
                # shared with a NON-redact-only entity (aliasing) is NOT
                # orphaned and MUST stay (SC-2 B byte-identical for that
                # entity).
                if not any(_t == _leaked_tok
                           for _t in local_value_to_token.values()):
                    local_token_to_value.pop(_leaked_tok, None)
        # SA-F3: blank the token field on retained redact-only bbox entries
        # (the black box still draws; the entry just carries no token).
        for _bb in token_bboxes:
            if _bb.get("value", "").strip() in redact_only_set:
                _bb["token"] = ""

    # Write sidecar map — now with per-word bboxes so pdf_qc UI can overlay
    # tokens at exact word positions instead of full xml_text positions.
    map_path = output_path + ".map.json"
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump({
            "token_to_value": local_token_to_value,
            "value_to_token": local_value_to_token,
            "bboxes": token_bboxes,
        }, f, indent=2, ensure_ascii=False)

    return local_value_to_token, local_token_to_value


def _sanitize_pdf(input_path: str, output_path: str) -> dict:
    """#3 (M3) genuine PDF sanitizer — ADR-2-san / arch 01 §2.4.

    Strip the invisible-vector attack/leak surface from the tokenized PDF
    and write a genuinely-sanitized copy to `output_path`. Uses ONLY the
    already-in-env `pikepdf` (imported at module top — TOK:53; the same
    mechanism the codebase already uses for metadata/content-stream
    surgery and ghost stripping) — NO new dependency (U-3 RESOLVED).

    Vectors stripped: DocumentInfo metadata, XMP (/Metadata), /OpenAction
    + /AA, the /JavaScript name tree, per-page /Annots, and the
    /EmbeddedFiles name tree. Saved with garbage-collection +
    linearization so removed objects are NOT recoverable.

    Returns a TRUTHFUL summary dict: every `*_stripped` / `*_removed`
    field reflects the ACTUAL post-strip state — a vector that was ABSENT
    in the source is reported false/0 (NEVER a spurious true), so
    sanitize_summary cannot lie (SC-3 B / FS-3 / FS-5). On a genuine
    failure this raises (NO swallow here) — the caller (_run_apply) is
    responsible for honest failure surfacing and MUST NOT copy an
    unsanitized file into safe/ (SC-3 C / WP-b: no copy-on-failure
    masquerade; would resurrect I-3a).
    """
    _in_p = Path(input_path)
    src_size = _in_p.stat().st_size if _in_p.exists() else 0
    summary: dict = {
        "sanitized":             False,
        "metadata_stripped":     False,
        "xmp_stripped":          False,
        "openaction_removed":    False,
        "additional_actions_removed": False,
        "javascript_removed":    0,
        "annotations_removed":   0,
        "embedded_files_removed": 0,
        "bytes_before":          src_size,
        "bytes_after":           0,
        "pages_processed":       0,
    }

    with pikepdf.open(input_path) as pdf:
        # ── metadata: DocumentInfo dict ──
        try:
            had_info = False
            try:
                had_info = bool(len(pdf.docinfo)) if pdf.docinfo is not None else False
            except Exception:
                had_info = "/Info" in pdf.trailer
            if had_info:
                try:
                    del pdf.trailer["/Info"]
                except Exception:
                    pdf.docinfo = pikepdf.Dictionary()
                summary["metadata_stripped"] = True
        except Exception:
            # No Info dict present → nothing to strip; honest false.
            summary["metadata_stripped"] = summary["metadata_stripped"]

        root = pdf.Root

        # ── XMP: /Metadata stream off the document catalog ──
        if "/Metadata" in root:
            del root["/Metadata"]
            summary["xmp_stripped"] = True

        # ── OpenAction + document-level Additional Actions ──
        if "/OpenAction" in root:
            del root["/OpenAction"]
            summary["openaction_removed"] = True
        if "/AA" in root:
            del root["/AA"]
            summary["additional_actions_removed"] = True

        # ── embedded JS + embedded files (the /Names name trees) ──
        if "/Names" in root:
            names = root["/Names"]
            if "/JavaScript" in names:
                js_count = 0
                try:
                    jstree = names["/JavaScript"]
                    if "/Names" in jstree:
                        js_count = len(jstree["/Names"]) // 2
                except Exception:
                    js_count = 1
                del names["/JavaScript"]
                summary["javascript_removed"] = js_count
            if "/EmbeddedFiles" in names:
                ef_count = 0
                try:
                    eftree = names["/EmbeddedFiles"]
                    if "/Names" in eftree:
                        ef_count = len(eftree["/Names"]) // 2
                except Exception:
                    ef_count = 1
                del names["/EmbeddedFiles"]
                summary["embedded_files_removed"] = ef_count

        # ── per-page annotations + per-page AA ──
        annots_removed = 0
        pages_processed = 0
        for page in pdf.pages:
            pages_processed += 1
            if "/Annots" in page:
                try:
                    annots_removed += len(page["/Annots"])
                except Exception:
                    annots_removed += 1
                del page["/Annots"]
            if "/AA" in page:
                del page["/AA"]
        summary["annotations_removed"] = annots_removed
        summary["pages_processed"] = pages_processed

        # Save with garbage-collection + linearization so the removed
        # objects are unrecoverable from the safe/ copy.
        pdf.save(output_path,
                 linearize=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)

    _out_p = Path(output_path)
    summary["bytes_after"] = _out_p.stat().st_size if _out_p.exists() else 0
    summary["sanitized"] = True
    return summary


def _clean_quoted_path(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s.strip()


def _load_or_generate_viz_data(pdf_path: Path) -> dict:
    """CLI helper: load `<stem>_viz_data.json` next to the PDF, else call
    pdf_analysis_viz_data_lite.process_one_pdf to generate one. Resolves the
    sibling module via this file's own directory — portable across installs."""
    sidecar = pdf_path.with_name(f"{pdf_path.stem}_viz_data.json")
    if sidecar.exists():
        with open(sidecar, "r", encoding="utf-8") as f:
            return json.load(f)

    _self_modules_dir = str(Path(__file__).resolve().parent)
    if _self_modules_dir not in sys.path:
        sys.path.insert(0, _self_modules_dir)
    import pdf_analysis_viz_data_lite as _viz
    summary = _viz.process_one_pdf(pdf_path)
    src = Path(summary.get("output_file", ""))
    if not src.exists():
        raise RuntimeError(f"viz_data generation produced no file: {summary}")
    with open(src, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    import unicodedata
    from time import perf_counter

    print("[pdf_tokenizer_v3] loading NLP models...")
    _build_analyzer()
    ner_models = _load_ner_models()
    print("[pdf_tokenizer_v3] ready.\n")

    print("Paste PDF paths (one per line). Press Enter on an empty line to start processing:")

    # Collect all paths first — supports multi-line paste in one go
    raw_lines: list[str] = []
    while True:
        line = input("> ").strip()
        if not line:
            break
        raw_lines.append(line)

    if not raw_lines:
        print("No files entered. Exiting.")
        return

    # Validate all paths before processing anything
    pdfs: list[Path] = []
    for raw in raw_lines:
        raw = unicodedata.normalize("NFC", _clean_quoted_path(raw))
        p = Path(raw)
        if not p.exists():
            print(f"  ❌ File not found: {p}")
        elif p.suffix.lower() != ".pdf":
            print(f"  ❌ Not a PDF: {p.name}")
        else:
            pdfs.append(p)

    if not pdfs:
        print("No valid PDF files. Exiting.")
        return

    print(f"\n{len(pdfs)} file(s) queued — processing...\n")

    session_total = 0
    for idx, inp in enumerate(pdfs, 1):
        out      = str(inp.parent / (inp.stem + "_tokenized.pdf"))
        map_path = out + ".map.json"

        print(f"[{idx}/{len(pdfs)}] {inp.name}")
        print(f"  output : {out}")

        t0 = perf_counter()
        try:
            viz_data = _load_or_generate_viz_data(inp)
            fwd, rev, bboxes = tokenize_pdf(str(inp), out, viz_data, ner_models=ner_models)
        except Exception as exc:
            print(f"  ❌ Failed: {exc}\n")
            continue

        with open(map_path, "w", encoding="utf-8") as f:
            json.dump({"token_to_value": rev, "value_to_token": fwd, "bboxes": bboxes},
                      f, indent=2, ensure_ascii=False)

        elapsed = perf_counter() - t0
        session_total += len(fwd)

        print(f"  ✅ done in {elapsed:.1f}s — {len(fwd)} value(s) tokenized")
        print(f"  map    -> {map_path}")

        if fwd:
            col = max(len(t) for t in rev)
            print(f"\n  {'TOKEN':<{col}}    REAL VALUE")
            print("  " + "-" * (col + 34))
            for tok, real in rev.items():
                print(f"  {tok:<{col}}  ->  {real}")
        print()

    print(f"[pdf_tokenizer_v3] session done — {session_total} unique value(s) tokenized across {len(pdfs)} file(s).")


if __name__ == "__main__":
    main()
