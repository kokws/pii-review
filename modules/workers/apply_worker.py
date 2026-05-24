"""apply_worker — subprocess.Popen target for PiiReview._run_apply.

Lifts the heavy C-extension portion of apply (`apply_decisions` + `_sanitize_pdf`)
out of the long-running Flask process so OS reclaims the heap on worker exit.

Job spec on stdin (single JSON line):
  {
    "client_id":     str,
    "pdf_path":      str,        # source PDF (data dir)
    "out_dir":       str,        # output dir for tokenized + safe/
    "stem":          str,
    "json_path":     str,        # path to <stem>_pii.json (decisions)
    "viz_data_path": str,        # path to <stem>_viz_data.lite.json
    "safe_filename": str,        # uuid-derived target name in safe/
  }

Result on stdout (final JSON line):
  {
    "ok":              true,
    "tokenized_path":  str,
    "safe_path":       str,
    "sanitize":        {<sanitize_summary dict>},
  }
or:
  {
    "ok":              false,
    "error":           str,
    "stage":           "load_viz"|"strip_ghosts"|"apply_decisions"|"sanitize"|"unknown",
  }
"""

import json
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
        pdf_path      = Path(job["pdf_path"])
        out_dir       = Path(job["out_dir"])
        stem          = job["stem"]
        json_path     = Path(job["json_path"])
        viz_data_path = Path(job["viz_data_path"])
        safe_filename = job["safe_filename"]
    except KeyError as e:
        _emit({"ok": False, "error": f"job missing required key: {e}", "stage": "spec"})
        return 1

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from modules import pdf_tokenizer_v3 as tok_mod
    except Exception as e:
        _emit({"ok": False, "error": f"module import failed: {e}", "stage": "unknown",
               "trace": traceback.format_exc()})
        return 1

    out_pdf  = out_dir / f"{stem}_tokenized.pdf"
    safe_dir = out_dir / "safe"
    safe_dir.mkdir(parents=True, exist_ok=True)
    safe_path = safe_dir / safe_filename

    # ── Stage 1: load viz_data (always cached at apply time) ──
    _progress("load_viz", f"Loading viz_data from {viz_data_path.name}")
    try:
        with open(viz_data_path, "r", encoding="utf-8") as fh:
            viz_data = json.load(fh)
    except Exception as e:
        _emit({"ok": False, "error": f"viz_data load failed: {e}",
               "stage": "load_viz", "trace": traceback.format_exc()})
        return 1

    # ── Stage 2: resolve apply_input (local clean > on-demand strip > source) ──
    clean_local = out_dir / f"{stem}_clean.pdf"
    if clean_local.exists():
        apply_input = clean_local
    else:
        _progress("strip_ghosts", "On-demand strip (rare for apply)...")
        try:
            tok_mod.strip_ghosts_from_pdf(str(pdf_path), str(clean_local), viz_data)
            apply_input = clean_local if clean_local.exists() else pdf_path
        except Exception as e:
            sys.stderr.write(f"on-demand strip failed (non-fatal): {e}\n")
            apply_input = pdf_path

    # ── Stage 3: apply_decisions (fitz heavy) ──
    _progress("apply_decisions", f"Tokenizing → {out_pdf.name}")
    try:
        tok_mod.apply_decisions(str(apply_input), str(out_pdf), str(json_path), viz_data)
    except Exception as e:
        _emit({"ok": False, "error": f"apply_decisions failed: {e}",
               "stage": "apply_decisions", "trace": traceback.format_exc()})
        return 1

    # ── Stage 4: sanitize (pikepdf heavy). No copy-on-failure fallback. ──
    _progress("sanitize", f"Sanitizing → safe/{safe_filename}")
    try:
        stripped = tok_mod._sanitize_pdf(str(out_pdf), str(safe_path))
    except Exception as e:
        # Honest surfacing — remove any partial safe artefact, error out.
        try:
            if safe_path.exists():
                safe_path.unlink()
        except Exception:
            pass
        _emit({"ok": False, "error": f"sanitize failed; no safe/ file written: {e}",
               "stage": "sanitize", "trace": traceback.format_exc()})
        return 1

    if not stripped.get("sanitized"):
        # Sanitizer returned without raising but did not confirm.
        try:
            if safe_path.exists():
                safe_path.unlink()
        except Exception:
            pass
        if "sanitize_error" not in stripped:
            stripped["sanitize_error"] = "sanitizer did not confirm sanitized:true"
        stripped["sanitized"] = False
        _emit({"ok": False, "error": "sanitize did not confirm success; no safe/ file written",
               "stage": "sanitize", "sanitize_summary": stripped})
        return 1

    _emit({
        "ok":             True,
        "tokenized_path": str(out_pdf),
        "safe_path":      str(safe_path),
        "sanitize":       stripped,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
