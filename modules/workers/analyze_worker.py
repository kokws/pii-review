"""analyze_worker — subprocess.Popen target for PiiReview._run_analyze.

Lifted out of pii_review.py to bound C-extension memory retention to a
single PDF's lifetime: the OS reclaims the worker's heap on exit, instead
of the fitz/pdfplumber/PIL allocations accumulating in the long-running
Flask process across N PDFs (the gradual-hang from the 2026-05-23 audit).

Job spec on stdin (single JSON line):
  {
    "client_id":     str,
    "pdf_path":      str,       # source PDF (data dir)
    "out_dir":       str,       # output dir for sidecars
    "stem":          str,
  }

Result on stdout (final JSON line, single line, no trailing newline issues):
  {
    "ok":              true,
    "viz_data_path":   str,
    "clean_pdf_path":  str|null,
    "pii_json_path":   str,
    "strip_ghosts":    int,
  }
or on failure:
  {
    "ok":              false,
    "error":           str,
    "stage":           "viz_data"|"strip_ghosts"|"analyze"|"unknown",
  }

Progress events on stdout (one JSON per line, before result):
  {"event":"progress","stage":<str>,"message":<str>}

stderr: free-form Python logging output, not parsed by parent.
"""

import json
import os
import shutil
import sys
import traceback
from pathlib import Path


def _emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _progress(stage, message):
    _emit({"event": "progress", "stage": stage, "message": message})


def main():
    try:
        job = json.loads(sys.stdin.read())
    except Exception as e:
        _emit({"ok": False, "error": f"job parse failed: {e}", "stage": "unknown"})
        return 1

    try:
        client_id   = job["client_id"]
        pdf_path    = Path(job["pdf_path"])
        out_dir     = Path(job["out_dir"])
        stem        = job["stem"]
    except KeyError as e:
        _emit({"ok": False, "error": f"job missing required key: {e}", "stage": "spec"})
        return 1

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from modules import pdf_analysis_viz_data_lite as viz_mod
        from modules import pdf_tokenizer_v3          as tok_mod
    except Exception as e:
        _emit({"ok": False, "error": f"module import failed: {e}", "stage": "unknown",
               "trace": traceback.format_exc()})
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    lite_sidecar = out_dir / f"{stem}_viz_data.lite.json"
    clean_local  = out_dir / f"{stem}_clean.pdf"
    out_json     = out_dir / f"{stem}_pii.json"

    # ── Stage 1: resolve viz_data (cached lite sidecar or generate) ──
    _progress("viz_data", "Loading viz_data...")
    viz_data = None
    if lite_sidecar.exists():
        try:
            with open(lite_sidecar, "r", encoding="utf-8") as fh:
                vd = json.load(fh)
            if isinstance(vd, dict) and vd.get("pages"):
                viz_data = vd
        except Exception as e:
            sys.stderr.write(f"viz_data read failed {lite_sidecar}: {e}\n")
            viz_data = None
    if viz_data is None:
        _progress("viz_data", "Generating viz_data (heavy)...")
        try:
            summary = viz_mod.process_one_pdf(pdf_path, log=None)
            src = Path(summary.get("output_file", ""))
            if not src.exists():
                _emit({"ok": False, "error": f"viz_data gen produced no file: {summary}",
                       "stage": "viz_data"})
                return 1
            try:
                shutil.move(str(src), str(lite_sidecar))
            except Exception:
                shutil.copyfile(str(src), str(lite_sidecar))
            with open(lite_sidecar, "r", encoding="utf-8") as fh:
                viz_data = json.load(fh)
        except Exception as e:
            _emit({"ok": False, "error": f"viz_data gen failed: {e}",
                   "stage": "viz_data", "trace": traceback.format_exc()})
            return 1

    # ── Stage 2: produce clean PDF (ghost-strip via own viz_data) ──
    _progress("strip_ghosts", "Stripping ghosts...")
    try:
        stripped = tok_mod.strip_ghosts_from_pdf(
            str(pdf_path), str(clean_local), viz_data,
        )
        strip_count = int(stripped) if isinstance(stripped, (int, str)) and str(stripped).isdigit() else 0
        analyze_input = clean_local if clean_local.exists() else pdf_path
        clean_pdf_path = str(clean_local) if clean_local.exists() else None
    except Exception as e:
        sys.stderr.write(f"strip_ghosts failed (non-fatal, falling back to source PDF): {e}\n")
        analyze_input = pdf_path
        clean_pdf_path = None
        strip_count = 0

    # ── Stage 3: analyze_pdf (entity stub — detection itself was stripped from v3) ──
    _progress("analyze", "Analyzing...")
    try:
        tok_mod.analyze_pdf(str(analyze_input), str(out_json), viz_data)
    except Exception as e:
        _emit({"ok": False, "error": f"analyze_pdf failed: {e}",
               "stage": "analyze", "trace": traceback.format_exc()})
        return 1

    _emit({
        "ok":             True,
        "viz_data_path":  str(lite_sidecar),
        "clean_pdf_path": clean_pdf_path,
        "pii_json_path":  str(out_json),
        "strip_ghosts":   strip_count,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
