"""
PII Review Module — multi-client, config-driven PII redaction.
============================================================================
Storage layout (per-client):
    data/pii_review/clients/<client_id>/
        ├─ _client.json                 (CRM record: name, literals, tokens)
        ├─ <stem>.pdf                   (uploaded statements)
    output/pii_review/clients/<client_id>/
        ├─ <stem>_pii.json              (per-PDF decisions + occurrences)
        ├─ <stem>_tokenized.pdf         (after Apply)
        ├─ <stem>_tokenized.pdf.map.json
        └─ <stem>_clean.pdf             (ghost-stripped, optional fallback)

CRM model:
    First time a literal is added on any PDF for client X → it merges into
    X's _client.json. Future PDFs for X analyze against _client.json
    (via _find_entity_occurrences) and pre-populate recommendations[] so
    the adviser only reviews + applies.

Routes (all client-scoped):
    GET  /pii_review                                       — viewer page
    GET  /api/pii-review/clients                           — list clients
    POST /api/pii-review/clients                           — create client {name}
    GET  /api/pii-review/clients/<client_id>/config        — read _client.json
    GET  /api/pii-review/files?client_id=X                 — list PDFs + status
    GET  /api/pii-review/data?stem=X&client_id=Y           — get pii.json
    GET  /api/pii-review/viz-data?stem=X&client_id=Y       — slim viz_data
    POST /api/pii-review/decisions                         — save (auto-merges to client config)
    POST /api/pii-review/apply                             — produce tokenized PDF
    POST /api/pii-review/upload?client_id=X                — upload PDF
    GET  /api/pii-review/pdf/<filename>?client_id=X        — serve PDF
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from flask import jsonify, render_template, request, send_file


def _make_logger(base_dir: str) -> logging.Logger:
    log_dir = Path(base_dir) / 'logs'
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger('pii_review')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        fh = logging.FileHandler(str(log_dir / 'pii_review.log'), encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s  %(levelname)-8s  %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(fh)
    return logger


_CLIENT_ID_RE = re.compile(r'[^A-Za-z0-9_-]+')

def _slugify_client(name: str) -> str:
    s = _CLIENT_ID_RE.sub('_', (name or '').strip())
    s = re.sub(r'_+', '_', s).strip('_')
    return s or 'unnamed'


class PiiReview:
    def __init__(self, app, log, base_dir, helper_dir=None):
        self.app = app
        self.log = log
        self.base_dir = base_dir
        # Multi-client roots. Per-client subdirs live underneath.
        self.clients_data_root   = Path(base_dir) / 'data'   / 'pii_review' / 'clients'
        self.clients_output_root = Path(base_dir) / 'output' / 'pii_review' / 'clients'
        self.clients_data_root.mkdir(parents=True, exist_ok=True)
        self.clients_output_root.mkdir(parents=True, exist_ok=True)

        self.flog = _make_logger(base_dir)
        self.flog.info(f'PiiReview init — clients_data={self.clients_data_root} '
                       f'clients_output={self.clients_output_root}')

        # status keyed by (client_id, stem) tuple
        self._status: dict = {}
        self._status_lock = threading.Lock()
        self._tok_lock    = threading.Lock()

        self._analyze_pool   = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pii_analyze')
        self._apply_pool     = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pii_apply')
        # Propagation pool: when Save fires on PDF X, re-resolve client
        # literals against every OTHER pii.json in the same client so new
        # candidates appear immediately on sibling statements.
        self._propagate_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pii_propagate')

        # Detection (Presidio + multi-model NER) is STRIPPED from
        # pdf_tokenizer_v3. analyze_pdf no longer auto-detects; per-client
        # analyze re-runs each saved literal through _find_entity_occurrences
        # against the new PDF's viz_data and pre-populates recommendations[].
        self._tok_mod = None
        self._ner_models: list = []
        # Force modules_dir to win at sys.path[0] regardless of who else
        # inserted entries earlier, so `import pdf_tokenizer_v3` always
        # binds to the canonical modules\ copy (not any stale helper copy).
        if helper_dir and str(helper_dir) not in sys.path:
            sys.path.insert(0, str(helper_dir))
        modules_dir = str(Path(base_dir) / 'modules')
        while modules_dir in sys.path:
            sys.path.remove(modules_dir)
        sys.path.insert(0, modules_dir)

        self._viz_mod = None
        try:
            import pdf_analysis_viz_data_lite as _viz
            self._viz_mod = _viz
            self.flog.info('pdf_analysis_viz_data_lite imported OK')
        except Exception as e:
            self.flog.warning(f'Could not import pdf_analysis_viz_data_lite: {e}')

        try:
            import pdf_tokenizer_v3 as _tok
            self._tok_mod = _tok
            self.flog.info('pdf_tokenizer_v3 imported OK (detection stripped)')
        except Exception as e:
            self.flog.warning(f'Could not import pdf_tokenizer_v3: {e}')

        threading.Thread(target=self._watch_loop, daemon=True).start()

    # ── Client CRM helpers ───────────────────────────────────────────────────

    def _client_data_dir(self, client_id: str) -> Path:
        return self.clients_data_root / client_id

    def _client_output_dir(self, client_id: str) -> Path:
        return self.clients_output_root / client_id

    def _client_config_path(self, client_id: str) -> Path:
        return self._client_data_dir(client_id) / '_client.json'

    def _safe_client_id(self, raw: str) -> str | None:
        """Filesystem-safe client id check. Returns None if invalid."""
        if not raw:
            return None
        if '..' in raw or '/' in raw or '\\' in raw:
            return None
        if not re.match(r'^[A-Za-z0-9_-]+$', raw):
            return None
        return raw

    def _load_client_config(self, client_id: str) -> dict | None:
        p = self._client_config_path(client_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            self.flog.exception(f'[PiiReview] _load_client_config({client_id}) failed: {e}')
            return None

    def _save_client_config(self, client_id: str, cfg: dict) -> None:
        p = self._client_config_path(client_id)
        cfg['updated_at'] = datetime.now().isoformat()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')

    def _list_clients(self) -> list[dict]:
        out = []
        if not self.clients_data_root.exists():
            return out
        for d in sorted(self.clients_data_root.iterdir()):
            if not d.is_dir():
                continue
            cfg = self._load_client_config(d.name) or {}
            pdf_count = sum(1 for p in d.glob('*.pdf'))
            out.append({
                'client_id':   d.name,
                'client_name': cfg.get('client_name', d.name),
                'pdf_count':   pdf_count,
                'literal_count': len(cfg.get('literals', [])),
                'created_at':  cfg.get('created_at'),
                'updated_at':  cfg.get('updated_at'),
            })
        return out

    def _create_client(self, name: str, custodian: str = '') -> dict:
        """Create a new client. ID is slugified-then-UPPERCASED from name.

        Bug 10 fix (2026-05-19): normalize to UPPERCASE and reject any
        case-insensitive collision with an existing client folder.
        The previous behaviour auto-appended `_2`/`_3`/... on collision,
        which on Windows' case-insensitive filesystem produced confusing
        siblings like `Test_20260519` and `Test_20260519_2` for what the
        user typed as `TEST_20260519`. Now: hard fail. Caller (API
        handler) catches ValueError and returns 409 to the FE.
        """
        base = _slugify_client(name).upper()
        client_id = base
        # Case-insensitive collision check against ALL existing client
        # folders in the clients root. On Windows the filesystem itself
        # is case-insensitive (so `client_data_dir(TEST_X).exists()`
        # returns True even if the on-disk folder is `Test_X`), but be
        # explicit here for portability + clarity in the error message.
        root = self._client_data_dir(client_id).parent
        existing = []
        if root.exists():
            existing = [p.name for p in root.iterdir() if p.is_dir()]
        upper_existing = {n.upper(): n for n in existing}
        if client_id in upper_existing:
            raise ValueError(
                f'client_id collision: requested {client_id!r} matches '
                f'existing folder {upper_existing[client_id]!r} '
                f'(case-insensitive). Use the existing client or pick a '
                f'different name.'
            )
        self._client_data_dir(client_id).mkdir(parents=True, exist_ok=True)
        self._client_output_dir(client_id).mkdir(parents=True, exist_ok=True)
        cfg = {
            'client_id':   client_id,
            'client_name': name,
            'custodian':   custodian,
            'created_at':  datetime.now().isoformat(),
            'updated_at':  datetime.now().isoformat(),
            'literals':    [],
        }
        self._save_client_config(client_id, cfg)
        self.flog.info(f'[PiiReview] created client {client_id} ({name!r})')
        return cfg

    def _merge_into_client_config(self, client_id: str, manual_additions: list,
                                  decisions: dict, source_stem: str,
                                  deleted_entities: list | None = None) -> int:
        """Rebuild _client.json::literals as the UNION of every sibling
        pii.json's currently-active entities. UI is fluid: whatever the user
        sees in any PDF's UI at Save time is what should exist in the CRM.

        `deleted_entities` is the explicit-deletion channel: only entities
        in this list are dropped from the CRM. Entities that are absent
        from the FE POST for any other reason (state drift, transient
        loss, partial curation) are CARRIED FORWARD from the prior CRM,
        so the user's previously-curated literals are never silently lost.
        If the list is None or empty, treat it as "no explicit deletions
        this Save" — never as "delete everything not in the POST."

        Returns rough change count for the toast message.
        """
        cfg = self._load_client_config(client_id) or {
            'client_id':   client_id,
            'client_name': client_id,
            'created_at':  datetime.now().isoformat(),
            'literals':    [],
        }
        old_entities = {l.get('entity') for l in (cfg.get('literals') or [])
                        if l.get('entity')}

        out_dir = self._client_output_dir(client_id)
        union: dict[str, dict] = {}   # entity → literal record

        def _absorb(ent, src, source_stem_for_first_seen):
            """src is a dict with possible category/suggested_category, tokenize, token."""
            if not ent:
                return
            cat = (src.get('category') or src.get('suggested_category')
                   or 'OTHER')
            if cat == 'REJECT':
                return
            tokenize = bool(src.get('tokenize', cat == 'ACCOUNT_NUMBER'))
            token    = src.get('token')
            if ent not in union:
                union[ent] = {
                    'entity':     ent,
                    'category':   cat,
                    'tokenize':   tokenize,
                    'token':      token,
                    'rejected':   False,
                    'first_seen': source_stem_for_first_seen,
                }
            else:
                # Later sources can refine metadata
                if cat and cat != 'OTHER':
                    union[ent]['category'] = cat
                if tokenize:
                    union[ent]['tokenize'] = True
                if token:
                    union[ent]['token'] = token

        if out_dir.exists():
            for json_path in sorted(out_dir.glob('*_pii.json')):
                stem = json_path.name[:-len('_pii.json')]
                try:
                    d = json.loads(json_path.read_text(encoding='utf-8'))
                except Exception:
                    continue
                # Only count CRM-derived recommendations that ACTUALLY match
                # (have at least one occurrence on this PDF). Without this
                # filter, a propagation-injected rec with 0 occurrences keeps
                # the entity in the CRM forever even after the user removes
                # it from the source PDF — defeats the rebuild semantics.
                # Manual additions count regardless of occurrences (user may
                # have added a string that doesn't appear yet).
                for r in (d.get('recommendations') or []):
                    if not (r.get('occurrences') or []):
                        continue
                    _absorb(r.get('entity'), r, stem)
                for m in (d.get('manual_additions') or []):
                    _absorb(m.get('entity'), m, stem)
                # Decisions are only authoritative if the entity is actually
                # present on this PDF (in either rec-with-occs or manual).
                # Skip dangling decisions for entities the user already
                # deleted from this PDF.
                pdf_active = set()
                for r in (d.get('recommendations') or []):
                    if (r.get('occurrences') or []) and r.get('entity'):
                        pdf_active.add(r['entity'])
                for m in (d.get('manual_additions') or []):
                    if m.get('entity'):
                        pdf_active.add(m['entity'])
                for ent, dec in (d.get('decisions') or {}).items():
                    if ent not in pdf_active:
                        continue
                    if dec.get('rejected') or dec.get('category') == 'REJECT':
                        if ent in union:
                            union[ent]['rejected'] = True
                        continue
                    _absorb(ent, dec, stem)
                    if ent in union and dec.get('token'):
                        union[ent]['token'] = dec['token']
                    # Decisions are the user's most-recent explicit choice.
                    # _absorb's "OTHER is not a refinement" rule (line 274)
                    # is right for analyzer/heuristic sources but wrong here:
                    # if the user dropdown-selected OTHER, that IS authoritative.
                    # Override unconditionally so a manual entity initially set
                    # to PERSON in the modal but later changed to OTHER via the
                    # dropdown lands as OTHER in the CRM.
                    if ent in union and dec.get('category'):
                        union[ent]['category'] = dec['category']

        # Carry-forward: entities in prior CRM that did NOT get absorbed
        # from any sibling AND are NOT in the explicit-deletion list are
        # preserved verbatim. Without this, any FE state drift that
        # happens to omit an entity from `recommendations` + `manual` +
        # `decisions` of every sibling would silently strip it from the
        # CRM — the root failure mode behind the M-MERGE collapse bug.
        deleted_set = set(deleted_entities or [])
        for prior_lit in (cfg.get('literals') or []):
            prior_ent = prior_lit.get('entity')
            if not prior_ent or prior_ent in union or prior_ent in deleted_set:
                continue
            union[prior_ent] = dict(prior_lit)
        # Explicit deletions are authoritative — drop them even if some
        # sibling still has them (the user clicked trash here, so they want
        # it gone across siblings too, matching the modal copy).
        for ent in deleted_set:
            union.pop(ent, None)

        new_literals = list(union.values())
        new_entities = set(union.keys())
        added   = new_entities - old_entities
        removed = old_entities - new_entities
        cfg['literals'] = new_literals
        self._save_client_config(client_id, cfg)
        # Rough change count = added + removed (metadata-only changes not
        # counted; toast just needs a heuristic).
        return len(added) + len(removed)

    # ── viz_data resolver ───────────────────────────────────────────────────

    def _resolve_viz_data(self, client_id: str, pdf_path: Path) -> dict | None:
        # Cache-first: read <stem>_viz_data.lite.json from the client output
        # dir if present, else generate via process_one_pdf(lite=True).
        stem = pdf_path.stem
        out_dir = self._client_output_dir(client_id)
        lite_sidecar = out_dir / f'{stem}_viz_data.lite.json'

        if lite_sidecar.exists():
            try:
                with open(lite_sidecar, 'r', encoding='utf-8') as fh:
                    vd = json.load(fh)
                if isinstance(vd, dict) and vd.get('pages'):
                    self.flog.info(f'[PiiReview] viz_data loaded from {lite_sidecar}')
                    return vd
            except Exception as e:
                self.flog.warning(f'[PiiReview] viz_data read failed {lite_sidecar}: {e}')

        if self._viz_mod is None:
            self.flog.warning('[PiiReview] viz_data module unavailable')
            return None

        self.flog.info(f'[PiiReview] lite viz_data missing — generating for {stem}')
        try:
            summary = self._viz_mod.process_one_pdf(pdf_path, log=self.flog)
            src = Path(summary.get('output_file', ''))
            if not src.exists():
                self.flog.error(f'[PiiReview] viz_data gen produced no file: {summary}')
                return None
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(lite_sidecar))
            except Exception:
                shutil.copyfile(str(src), str(lite_sidecar))
            with open(lite_sidecar, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except Exception as e:
            self.flog.exception(f'[PiiReview] viz_data generation failed: {e}')
            return None

    # ── Watcher ─────────────────────────────────────────────────────────────

    def _watch_loop(self):
        self.flog.info('[PiiReview] Watcher started (15s poll, multi-client)')
        while True:
            try:
                self._check_new_pdfs()
            except Exception as e:
                self.flog.error(f'[PiiReview] Watcher error: {e}')
            time.sleep(15)

    def _check_new_pdfs(self):
        if not self._tok_mod:
            return
        for client_dir in sorted(self.clients_data_root.iterdir()):
            if not client_dir.is_dir():
                continue
            client_id = client_dir.name
            for pdf_path in sorted(client_dir.glob('*.pdf')):
                stem = pdf_path.stem
                out_json = self._client_output_dir(client_id) / f'{stem}_pii.json'
                key = (client_id, stem)
                with self._status_lock:
                    cur = self._status.get(key, {}).get('status', 'idle')
                if not out_json.exists() and cur not in ('analyzing',):
                    self.flog.info(f'[PiiReview] Auto-queue: {client_id}/{pdf_path.name}')
                    self._start_analyze(client_id, pdf_path)

    def _start_analyze(self, client_id: str, pdf_path: Path):
        stem = pdf_path.stem
        key = (client_id, stem)
        with self._status_lock:
            self._status[key] = {
                'status':  'analyzing',
                'message': 'Queued',
                'updated': datetime.now().isoformat(),
                'pdf':     pdf_path.name,
                'client_id': client_id,
            }
        self._analyze_pool.submit(self._run_analyze, client_id, pdf_path)

    def _persist_status(self, client_id: str, stem: str, status: str, message: str = "") -> None:
        json_path = self._client_output_dir(client_id) / f'{stem}_pii.json'
        if not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text(encoding='utf-8'))
            data['status']         = status
            data['status_message'] = message
            data['status_updated'] = datetime.now().isoformat()
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            self.flog.exception(f'[PiiReview] _persist_status failed for {client_id}/{stem}')

    def _populate_recommendations_from_client_config(self, client_id: str, viz_data: dict,
                                                     pii_json_path: Path) -> int:
        """Re-resolve every client literal against this PDF's viz_data and
        write recommendations[] into pii.json. Pre-rejected literals also
        get a decisions[entity] = { rejected: true } entry so they show in
        the rejected pool.

        Returns count of literals that produced at least one occurrence.
        """
        cfg = self._load_client_config(client_id)
        if not cfg:
            return 0
        literals = cfg.get('literals') or []
        if not literals:
            return 0

        pages_map = viz_data.get('pages') or {}
        try:
            total_pages = max(int(k) for k in pages_map.keys()) if pages_map else 0
        except Exception:
            total_pages = len(pages_map)

        recommendations = []
        decisions       = {}
        hits = 0
        for i, lit in enumerate(literals):
            ent = lit.get('entity')
            if not ent:
                continue
            occs = []
            for page_no in range(1, total_pages + 1):
                page_data = pages_map.get(str(page_no)) or pages_map.get(page_no) or {}
                xts = self._tok_mod._filter_ghosts_and_taint(page_data.get('xml_texts') or [])
                occs.extend(self._tok_mod._find_entity_occurrences(ent, xts, page_no))
            cat = lit.get('category', 'OTHER')
            recommendations.append({
                'id':                 f'crm_{i}',
                'entity':             ent,
                'suggested_category': cat,
                'tokenize':           bool(lit.get('tokenize', False)),
                'token':              lit.get('token'),
                'models_voted':       ['client_config'],
                'vote_count':         1,
                'consensus_passed':   True,
                'occurrences':        occs,
            })
            if occs:
                hits += 1
            # Carry through user overrides as decisions so the UI honours them
            dec_entry = {
                'category': cat,
                'tokenize': bool(lit.get('tokenize', False)),
            }
            if lit.get('token'):
                dec_entry['token'] = lit['token']
            if lit.get('rejected'):
                dec_entry['rejected'] = True
            decisions[ent] = dec_entry

        # Read the pii.json that analyze_pdf just wrote (empty shell), inject
        # recommendations + decisions, write back.
        try:
            data = json.loads(pii_json_path.read_text(encoding='utf-8'))
        except Exception:
            return 0
        data['recommendations']   = recommendations
        data['decisions']         = decisions
        data['client_id']         = client_id
        data['client_name']       = cfg.get('client_name', client_id)
        data['from_client_config'] = True
        # Bug 9 fix (refined 2026-05-20 after applied-status regression):
        # Refresh `status_message` + `status_updated` so the recorded status
        # message reflects THIS populate run (not the initial analyze's
        # 0-hits snapshot). For the status FIELD itself: NEVER downgrade
        # an existing 'applied' status to 'analyzed'. Apply is a one-way
        # latch — once the tokenized PDF + safe-copy exist on disk, the
        # file is durably 'applied'. Re-runs of _populate (which happen on
        # every read of /api/pii-review/data and every save) used to
        # overwrite this with 'analyzed', killing the green ✅ tick in the
        # file dropdown even though the tokenized PDF was still present.
        # Fix 2: persist `candidates` count so api_files can read it from
        # disk after a service restart (the in-memory _status dict empties
        # on restart, which previously dropped the [N cand.] chip).
        existing_status = data.get('status')
        if existing_status != 'applied':
            data['status'] = 'analyzed'
        data['status_message'] = f'{hits} literal(s) matched from client config'
        data['status_updated'] = datetime.now().isoformat()
        data['candidates']     = hits
        pii_json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        return hits

    def _propagate_to_siblings(self, client_id: str, source_stem: str) -> None:
        """Diff-mode propagation. For each sibling pii.json:

          1) Compare client literals against sibling's existing recommendations
          2) NEW literal (entity not present)        → load viz_data, find
             occurrences, append to recommendations
          3) CHANGED literal (category/token/etc.)   → update metadata in
             existing recommendation + decisions, no viz_data load
          4) Nothing changed                         → SKIP entirely (no read,
             no write, no status touch)

        viz_data is loaded at most once per sibling, and only if there's at
        least one NEW literal to resolve. Most refreshes after the initial
        sync should be no-ops.

        Manual_additions on siblings are NEVER touched.
        """
        cfg = self._load_client_config(client_id)
        if not cfg or not (cfg.get('literals') or []):
            return
        literals = cfg['literals']

        out_dir = self._client_output_dir(client_id)
        if not out_dir.exists():
            return
        pii_suffix = '_pii.json'
        for json_path in sorted(out_dir.glob(f'*{pii_suffix}')):
            stem = json_path.name[:-len(pii_suffix)]
            if stem == source_stem:
                continue
            try:
                self._diff_propagate_one(client_id, stem, json_path, literals)
            except Exception as e:
                self.flog.exception(f'[PiiReview] diff-propagate {client_id}/{stem} failed: {e}')

    def _diff_propagate_one(self, client_id: str, stem: str,
                            json_path: Path, literals: list) -> None:
        """Diff one sibling against client literals. Touches the file ONLY
        when there's an actual change."""
        try:
            data = json.loads(json_path.read_text(encoding='utf-8'))
        except Exception:
            return

        existing_recs_list = list(data.get('recommendations') or [])
        existing_recs = {r.get('entity'): r for r in existing_recs_list
                         if r.get('entity')}
        existing_decisions = dict(data.get('decisions') or {})

        # Removal pass: anything in this sibling's recs/decisions that's no
        # longer in client config gets dropped. Keeps the sibling in sync
        # with CRM after a Save removed an entity in another PDF.
        literal_entities = {l.get('entity') for l in literals if l.get('entity')}
        kept_recs = [r for r in existing_recs_list
                     if r.get('entity') in literal_entities or r.get('entity') is None]
        removed_count = len(existing_recs_list) - len(kept_recs)
        kept_decisions = {ent: dec for ent, dec in existing_decisions.items()
                          if ent in literal_entities}
        # Re-anchor existing_recs map to the kept set
        existing_recs = {r.get('entity'): r for r in kept_recs if r.get('entity')}
        existing_decisions = kept_decisions

        new_lits     = []   # literals not yet in this sibling
        changed_lits = []   # metadata mismatch — needs in-place update only

        for lit in literals:
            ent = lit.get('entity')
            if not ent:
                continue
            cat      = lit.get('category', 'OTHER')
            tok      = lit.get('token')
            tokenize = bool(lit.get('tokenize', False))
            rejected = bool(lit.get('rejected', False))

            er = existing_recs.get(ent)
            if er is None:
                new_lits.append(lit)
                continue
            # Compare metadata
            if (er.get('suggested_category') != cat
                or er.get('token') != tok
                or bool(er.get('tokenize', False)) != tokenize):
                changed_lits.append(lit)
                continue
            ed = existing_decisions.get(ent) or {}
            if bool(ed.get('rejected', False)) != rejected:
                changed_lits.append(lit)

        if not new_lits and not changed_lits and removed_count == 0:
            self.flog.info(f'[PiiReview] diff-propagate {client_id}/{stem}: no change, skipped')
            return  # Nothing to do — file untouched

        # If only removals happened, still need to write the trimmed file
        if not new_lits and not changed_lits:
            data['recommendations'] = list(existing_recs.values())
            data['decisions']       = existing_decisions
            data['from_client_config'] = True
            data['status']             = 'analyzed'
            data['status_message']     = f'-{removed_count} stale literal(s) dropped'
            data['status_updated']     = datetime.now().isoformat()
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
            self.flog.info(f'[PiiReview] diff-propagate {client_id}/{stem}: dropped {removed_count} stale literal(s)')
            return

        # NEW literals require viz_data + per-page occurrence finding.
        # CHANGED literals don't need viz_data.
        viz_data = None
        if new_lits:
            pdf_path = self._client_data_dir(client_id) / f'{stem}.pdf'
            if not pdf_path.exists():
                return
            viz_data = self._resolve_viz_data(client_id, pdf_path)
            if not viz_data:
                return

        recommendations = list(data.get('recommendations') or [])
        decisions       = dict(data.get('decisions') or {})

        # Add new literals
        new_with_hits  = []   # (entity, occ_count)
        new_zero_hits  = []   # entities the literal didn't match anywhere on this PDF
        if new_lits:
            pages_map = viz_data.get('pages') or {}
            try:
                total_pages = max(int(k) for k in pages_map.keys()) if pages_map else 0
            except Exception:
                total_pages = len(pages_map)
            for lit in new_lits:
                ent = lit['entity']
                cat = lit.get('category', 'OTHER')
                occs = []
                for page_no in range(1, total_pages + 1):
                    page_data = pages_map.get(str(page_no)) or pages_map.get(page_no) or {}
                    xts = self._tok_mod._filter_ghosts_and_taint(page_data.get('xml_texts') or [])
                    occs.extend(self._tok_mod._find_entity_occurrences(ent, xts, page_no))
                recommendations.append({
                    'id':                 f'crm_diff_{len(recommendations)}',
                    'entity':             ent,
                    'suggested_category': cat,
                    'tokenize':           bool(lit.get('tokenize', False)),
                    'token':              lit.get('token'),
                    'models_voted':       ['client_config'],
                    'vote_count':         1,
                    'consensus_passed':   True,
                    'occurrences':        occs,
                })
                dec_entry = {'category': cat, 'tokenize': bool(lit.get('tokenize', False))}
                if lit.get('token'):
                    dec_entry['token'] = lit['token']
                if lit.get('rejected'):
                    dec_entry['rejected'] = True
                decisions[ent] = dec_entry
                if occs:
                    new_with_hits.append((ent, len(occs)))
                else:
                    new_zero_hits.append(ent)

        # Update changed literals in place (no viz_data needed)
        for lit in changed_lits:
            ent = lit['entity']
            cat = lit.get('category', 'OTHER')
            for r in recommendations:
                if r.get('entity') == ent:
                    r['suggested_category'] = cat
                    r['tokenize']           = bool(lit.get('tokenize', False))
                    r['token']              = lit.get('token')
                    break
            de = decisions.get(ent) or {}
            de['category'] = cat
            de['tokenize'] = bool(lit.get('tokenize', False))
            if lit.get('token'):
                de['token'] = lit['token']
            else:
                de.pop('token', None)
            if lit.get('rejected'):
                de['rejected'] = True
            else:
                de.pop('rejected', None)
            decisions[ent] = de

        # Build status message — what changed on this sibling
        parts = []
        if new_with_hits:
            preview = [e for e, _ in new_with_hits[:3]]
            extra   = len(new_with_hits) - 3
            if extra > 0:
                preview.append(f'+{extra} more')
            parts.append(f'+{len(new_with_hits)} new candidate(s) [{", ".join(preview)}]')
        if new_zero_hits:
            parts.append(f'{len(new_zero_hits)} new literal(s) found no match here')
        if changed_lits:
            parts.append(f'{len(changed_lits)} updated')
        msg = ' · '.join(parts) or 'refreshed'

        data['recommendations']   = recommendations
        data['decisions']         = decisions
        data['from_client_config'] = True
        data['status']             = 'analyzed'
        data['status_message']     = msg
        data['status_updated']     = datetime.now().isoformat()
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

        key = (client_id, stem)
        with self._status_lock:
            self._status[key] = {
                **self._status.get(key, {}),
                'status':  'analyzed',
                'message': msg,
                'updated': datetime.now().isoformat(),
                'pdf':     f'{stem}.pdf',
                'client_id': client_id,
                'candidates': len(recommendations),
            }
        self.flog.info(f'[PiiReview] diff-propagate {client_id}/{stem}: {msg}')

    # ── Subprocess worker invocation ────────────────────────────────────────
    # Both _run_analyze and _run_apply offload their C-extension heavy work
    # (fitz / pdfplumber / PIL / pikepdf) to standalone workers so the OS
    # reclaims the heap when each PDF's worker exits. Gradual-hang fix
    # (2026-05-23 audit, Review_piireview_67527576 verdict).
    #
    # Workers communicate via JSON on stdin → JSON-per-line on stdout:
    #   {"event":"progress","stage":...,"message":...}     # status updates
    #   {"ok":true|false, ...}                              # final result (last)
    # stderr is logged at debug level; not parsed.

    def _run_worker(self, worker_path: Path, job: dict, status_key: tuple) -> dict:
        """Spawn worker subprocess, stream progress to self._status, return result dict.

        worker_path: absolute path to <worker>.py (analyze_worker / apply_worker)
        job:         JSON-serialisable job spec dict
        status_key:  (client_id, stem) for self._status updates on progress events
        """
        cmd = [sys.executable, str(worker_path)]
        self.flog.info(f'[PiiReview] spawn worker {worker_path.name} for {status_key}')
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.base_dir,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        # Feed job spec then close stdin.
        try:
            proc.stdin.write(json.dumps(job, ensure_ascii=False))
            proc.stdin.close()
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return {'ok': False, 'error': f'failed to send job to worker: {e}', 'stage': 'spawn'}

        result: dict | None = None
        for raw in proc.stdout:
            line = raw.rstrip('\n')
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                # Worker emitted a non-JSON line; log and continue.
                self.flog.debug(f'[worker {worker_path.name}] non-JSON stdout: {line!r}')
                continue
            if evt.get('event') == 'progress':
                with self._status_lock:
                    existing = self._status.get(status_key, {})
                    existing.update({
                        'message': evt.get('message', ''),
                        'stage':   evt.get('stage', ''),
                        'updated': datetime.now().isoformat(),
                    })
                    self._status[status_key] = existing
            else:
                # Anything non-progress is the final result.
                result = evt
        proc.wait()
        stderr_tail = (proc.stderr.read() or '').strip()
        if stderr_tail:
            self.flog.debug(f'[worker {worker_path.name} stderr] {stderr_tail[-2000:]}')
        if result is None:
            return {'ok': False, 'error': f'worker exited with code {proc.returncode} '
                                          f'and no result line', 'stage': 'no_result',
                    'stderr': stderr_tail[-2000:]}
        if proc.returncode != 0 and result.get('ok'):
            # Worker emitted ok=true but non-zero exit — treat as failure.
            return {'ok': False, 'error': f'worker exit code {proc.returncode} '
                                          f'despite ok=true result', 'stage': 'exit_code',
                    'worker_result': result, 'stderr': stderr_tail[-2000:]}
        return result

    @property
    def _worker_dir(self) -> Path:
        return Path(__file__).resolve().parent / 'workers'

    def _run_analyze(self, client_id: str, pdf_path: Path):
        stem = pdf_path.stem
        key = (client_id, stem)
        try:
            with self._status_lock:
                self._status[key].update({'message': 'Spawning analyze worker...',
                                          'updated': datetime.now().isoformat()})

            out_dir = self._client_output_dir(client_id)
            out_dir.mkdir(parents=True, exist_ok=True)

            job = {
                'client_id':   client_id,
                'pdf_path':    str(pdf_path),
                'out_dir':     str(out_dir),
                'stem':        stem,
            }
            result = self._run_worker(self._worker_dir / 'analyze_worker.py', job, key)
            if not result.get('ok'):
                raise RuntimeError(f'analyze worker failed at stage {result.get("stage")}: '
                                   f'{result.get("error")}')

            # ── Worker done. Now run pure-Python populate in this process. ──
            # _populate_recommendations_from_client_config is matcher-driven
            # against the already-cached viz_data sidecar — no C-extension
            # work, no point spawning another subprocess for it.
            viz_data_path = Path(result['viz_data_path'])
            out_json = Path(result['pii_json_path'])
            with open(viz_data_path, 'r', encoding='utf-8') as fh:
                viz_data = json.load(fh)
            hit_count = self._populate_recommendations_from_client_config(
                client_id, viz_data, out_json,
            )

            msg = f'{hit_count} literal(s) matched from client config'
            with self._status_lock:
                self._status[key] = {
                    'status':  'analyzed',
                    'message': msg,
                    'updated': datetime.now().isoformat(),
                    'pdf':     pdf_path.name,
                    'client_id': client_id,
                    'candidates': hit_count,
                }
            self._persist_status(client_id, stem, 'analyzed', msg)
            self.flog.info(f'[PiiReview] {client_id}/{stem}: analyzed via worker '
                           f'(strip_ghosts={result.get("strip_ghosts")}, CRM hits={hit_count})')
        except Exception as e:
            self.flog.exception(f'[PiiReview] Analyze failed {client_id}/{stem}: {e}')
            with self._status_lock:
                self._status[key] = {
                    'status':  'error',
                    'message': str(e),
                    'updated': datetime.now().isoformat(),
                    'pdf':     pdf_path.name,
                    'client_id': client_id,
                }
            self._persist_status(client_id, stem, 'error', f'Analyze failed: {e}')

    # ── Apply (user-triggered) ──────────────────────────────────────────────

    def _run_apply(self, client_id: str, stem: str):
        key = (client_id, stem)
        try:
            with self._status_lock:
                self._status[key] = {
                    **self._status.get(key, {}),
                    'status':  'applying',
                    'message': 'Spawning apply worker...',
                    'updated': datetime.now().isoformat(),
                    'client_id': client_id,
                }
            data_dir = self._client_data_dir(client_id)
            out_dir  = self._client_output_dir(client_id)
            pdf_path = data_dir / f'{stem}.pdf'
            json_path = out_dir / f'{stem}_pii.json'
            viz_data_path = out_dir / f'{stem}_viz_data.lite.json'
            if not viz_data_path.exists():
                raise RuntimeError(f'viz_data sidecar missing: {viz_data_path.name} — '
                                   f'analyze must run before apply')

            # Determine safe_filename: reuse if a previous apply on this
            # stem produced one (idempotent re-apply preserves the opaque
            # mapping); else mint a new UUID-derived name.
            cfg = self._load_client_config(client_id) or {}
            outputs = cfg.get('applied_outputs') or []
            existing = next((o for o in outputs if o.get('original_stem') == stem), None)
            if existing and existing.get('safe_filename'):
                safe_filename = existing['safe_filename']
            else:
                safe_filename = f'pii_safe_{uuid.uuid4().hex[:12]}.pdf'

            job = {
                'client_id':     client_id,
                'pdf_path':      str(pdf_path),
                'out_dir':       str(out_dir),
                'stem':          stem,
                'json_path':     str(json_path),
                'viz_data_path': str(viz_data_path),
                'safe_filename': safe_filename,
            }
            result = self._run_worker(self._worker_dir / 'apply_worker.py', job, key)
            if not result.get('ok'):
                # WP-b honest-surfacing preserved: worker has already cleaned
                # up any partial safe/ artefact on failure.
                raise RuntimeError(f'apply worker failed at stage {result.get("stage")}: '
                                   f'{result.get("error")}')

            stripped  = result.get('sanitize') or {}
            safe_path = Path(result['safe_path'])
            out_pdf   = Path(result['tokenized_path'])
            self.flog.info(f'[PiiReview] {client_id}/{stem}: sanitized → {safe_filename} '
                           f'(stripped: {stripped})')

            # ── Update / append the mapping entry in _client.json ──
            new_entry = {
                'original_stem':     stem,
                'original_filename': pdf_path.name,
                'safe_filename':     safe_filename,
                'safe_path':         str(safe_path),
                'applied_at':        datetime.now().isoformat(),
                'sanitize_summary':  stripped,
            }
            outputs = [o for o in outputs if o.get('original_stem') != stem]
            outputs.append(new_entry)
            cfg['applied_outputs'] = outputs
            self._save_client_config(client_id, cfg)

            msg = f'Applied + sanitized → {safe_filename}'
            with self._status_lock:
                self._status[key] = {
                    **self._status.get(key, {}),
                    'status':         'applied',
                    'message':        msg,
                    'safe_filename':  safe_filename,
                    'updated':        datetime.now().isoformat(),
                    'client_id':      client_id,
                }
            self._persist_status(client_id, stem, 'applied', msg)
            self.flog.info(f'[PiiReview] {client_id}/{stem}: applied via worker → '
                           f'{out_pdf.name} → safe/{safe_filename}')
        except Exception as e:
            self.flog.exception(f'[PiiReview] Apply failed {client_id}/{stem}: {e}')
            with self._status_lock:
                self._status[key] = {
                    **self._status.get(key, {}),
                    'status':  'error',
                    'message': f'Apply failed: {e}',
                    'updated': datetime.now().isoformat(),
                    'client_id': client_id,
                }
            self._persist_status(client_id, stem, 'error', f'Apply failed: {e}')

    # ── Routes ──────────────────────────────────────────────────────────────

    def add_routes(self):

        @self.app.route('/pii_review')
        def pii_review_page():
            return render_template('pii_review.html')

        # ── Client CRM ──
        @self.app.route('/api/pii-review/clients', methods=['GET'])
        def api_clients_list():
            return jsonify({'clients': self._list_clients()})

        @self.app.route('/api/pii-review/clients', methods=['POST'])
        def api_clients_create():
            body = request.get_json() or {}
            name = (body.get('name') or '').strip()
            if not name:
                return jsonify({'error': 'name required'}), 400
            if len(name) > 200:
                return jsonify({'error': 'name too long'}), 400
            try:
                cfg = self._create_client(name, custodian=body.get('custodian', ''))
            except ValueError as e:
                # Bug 10 fix: _create_client raises ValueError on case-
                # insensitive client_id collision. Surface as 409 Conflict
                # rather than the legacy auto-`_2` silent rename.
                return jsonify({'ok': False, 'error': str(e)}), 409
            return jsonify({'ok': True, 'client': cfg})

        @self.app.route('/api/pii-review/clients/<client_id>/config', methods=['GET'])
        def api_client_config(client_id):
            cid = self._safe_client_id(client_id)
            if not cid:
                return jsonify({'error': 'bad client_id'}), 400
            cfg = self._load_client_config(cid)
            if not cfg:
                return jsonify({'error': 'client not found'}), 404
            return jsonify(cfg)

        # ── Files (per client) ──
        @self.app.route('/api/pii-review/files')
        def api_files():
            cid = self._safe_client_id(request.args.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            data_dir = self._client_data_dir(cid)
            out_dir  = self._client_output_dir(cid)
            if not data_dir.exists():
                return jsonify({'files': [], 'ner_loaded': True, 'ner_models': [], 'client_id': cid})
            try:
                files = []
                for pdf_path in sorted(data_dir.glob('*.pdf')):
                    stem = pdf_path.stem
                    json_path = out_dir / f'{stem}_pii.json'
                    tok_path  = out_dir / f'{stem}_tokenized.pdf'
                    key = (cid, stem)
                    with self._status_lock:
                        st = dict(self._status.get(key, {}))
                    live_status = st.get('status')
                    persisted_status = None
                    persisted_message = ''
                    persisted_candidates = 0
                    if json_path.exists():
                        try:
                            jd = json.loads(json_path.read_text(encoding='utf-8'))
                            persisted_status  = jd.get('status')
                            persisted_message = jd.get('status_message', '')
                            # Fix 2 (2026-05-20): persist candidates count
                            # in pii.json so we can survive a service
                            # restart (the in-memory _status dict empties
                            # on restart, dropping the [N cand.] chip).
                            persisted_candidates = int(jd.get('candidates') or 0)
                        except Exception:
                            persisted_status = None

                    # Status resolution order (post applied-status regression
                    # fix 2026-05-20): live > tokenized-file-exists > persisted
                    # > json-exists > idle. tok_path.exists() outranks
                    # persisted_status because the file-on-disk is a more
                    # durable truth than a status field that can be over-
                    # written by re-runs of _populate. Belt-and-braces along
                    # with the source fix in _populate_recommendations_from
                    # _client_config that no longer downgrades 'applied' →
                    # 'analyzed'.
                    if live_status in ('analyzing', 'applying'):
                        status = live_status
                    elif tok_path.exists():
                        status = 'applied'
                    elif persisted_status:
                        status = persisted_status
                    elif json_path.exists():
                        status = 'analyzed'
                    else:
                        status = 'idle'
                    files.append({
                        'filename':      pdf_path.name,
                        'stem':          stem,
                        'has_json':      json_path.exists(),
                        'has_tokenized': tok_path.exists(),
                        'status':        status,
                        'message':       st.get('message', persisted_message),
                        'updated':       st.get('updated', ''),
                        'candidates':    st.get('candidates', persisted_candidates),
                    })
                return jsonify({
                    'files': files,
                    'ner_loaded': True,
                    'ner_models': [],
                    'client_id': cid,
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/pii-review/data')
        def api_data():
            cid = self._safe_client_id(request.args.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            stem = request.args.get('stem', '')
            if not stem or '..' in stem or '/' in stem or '\\' in stem:
                return jsonify({'error': 'bad stem'}), 400
            json_path = self._client_output_dir(cid) / f'{stem}_pii.json'
            if not json_path.exists():
                return jsonify({'error': 'not analyzed yet'}), 404

            # ---- BUG-A (re-derive-on-read; KNOWN_ISSUES §1:101-103) ------
            # Root cause (QA ROOT_CAUSE 20260517_0557 §3; BUG-A.md §5/§7):
            # recommendations[] in a pre-existing pii.json is written once and
            # never refreshed on a read-only open. _check_new_pdfs only
            # analyzes when pii.json is ABSENT; F-1 (api_decisions:1186-1426)
            # refreshes only on Save; /reanalyze must be user-invoked. USER's
            # scenario is opening the PDF read-only — so the stale top-table-
            # only record (DEMO_CLIENT_* p5 xt={23,29,35}/48) is served and
            # the bottom-6 ({65,66,75,76,85,86}) are unboxable in the overlay.
            #
            # Documented systemic alternative (KNOWN_ISSUES §1:101-103):
            # "never persist recommendations[] from frontend POSTs; always
            # re-derive on read." Implemented here by invoking the EXISTING
            # correct routine _populate_recommendations_from_client_config
            # (the SAME 3-arg routine /reanalyze:1484 and F-1:1253 use — no
            # matcher change, brief 06_non_goals.md:15-18 honoured; overlay
            # untouched). manual_additions + the user's decisions are
            # snapshotted and restored around the call (exact /reanalyze
            # :1480-1489 + F-1 happy-path-restore:1263-1266 mirror) so user
            # work is never lost (DEMO_CLIENT_04052026 has manual_additions=1).
            # On ANY exception the pre-call on-disk bytes are restored
            # (FINDING-1 mirror) + a distinct greppable marker is logged; the
            # rolled-back (stale-but-intact) record is still served — the
            # documented acceptable degradation, never a silent swallow.
            #
            # GUARD: only re-derive when viz_data is resolvable from the
            # on-disk CACHE. A GET must NEVER trigger the expensive
            # process_one_pdf regenerate branch (_resolve_viz_data:356-369).
            # ZERO request/response wire/schema delta (brief 06_non_goals.md
            # :25-28 + SC-global): same GET, same body shape, only freshness
            # changes. F-1 region (1186-1426) is NOT touched — order-
            # independent (both sites call the same routine, each restoring
            # user state around it). SA consult filed: SA/inbox
            # 20260517_BE_Build_Lead_CONSULT_rederive_on_read_invariant.md.
            try:
                pdf_path = self._client_data_dir(cid) / f'{stem}.pdf'
                # M-LITE / DECOUPLE-1 locus (C) (SA freeze §3.2; BE_Reviewer
                # NEEDS_FIX 20260517_094228). The BUG-A `viz_cached` pre-check
                # MUST use the SAME cache-set as locus (A) `_resolve_viz_data`,
                # which now cache-resolves ONLY pii_review's own distinctly-
                # named lite sidecar `<stem>_viz_data.lite.json`. The old
                # full-file disjuncts (`pdfqc_vd.exists() or own_vd.exists()`)
                # are DROPPED: with them, a legacy client that has only a full
                # `<stem>_viz_data.json` (no lite sidecar) would pass this gate
                # via the full file, yet locus (A) finds NO lite sidecar and
                # would regenerate via `process_one_pdf(lite=True)` ON THE GET
                # — an absolute INV-RD / ADR-1-RD:755 violation (a GET MUST
                # NEVER trigger process_one_pdf). Aligning the cache-set to the
                # lite sidecar ONLY guarantees: gate True ⇒ lite sidecar
                # present ⇒ locus (A) returns it cache-first WITHOUT entering
                # its regenerate branch (INV-RD preserved). A legacy-full-only
                # client has gate False ⇒ the BUG-A read-path re-derive simply
                # does not fire and the on-disk pii.json is served as-is
                # (exactly today's no-cache behaviour; no GET regeneration).
                # Its lite sidecar self-heals on the next WRITE-path analyze
                # (INV-LITE clause (g)). Same predicate as loci (A)/(B).
                lite_sidecar = self._client_output_dir(cid) / f'{stem}_viz_data.lite.json'
                viz_cached = lite_sidecar.exists()
                if pdf_path.exists() and viz_cached:
                    viz_data = self._resolve_viz_data(cid, pdf_path)
                    if viz_data:
                        pre_rederive_bytes = json_path.read_bytes()
                        try:
                            _existing = json.loads(
                                pre_rederive_bytes.decode('utf-8'))
                            snap_manual    = _existing.get(
                                'manual_additions', [])
                            snap_decisions = _existing.get('decisions', {})
                            self._populate_recommendations_from_client_config(
                                cid, viz_data, json_path,
                            )
                            refreshed = json.loads(
                                json_path.read_text(encoding='utf-8'))
                            refreshed['manual_additions'] = snap_manual
                            refreshed['decisions']        = snap_decisions
                            json_path.write_text(
                                json.dumps(refreshed, indent=2,
                                           ensure_ascii=False),
                                encoding='utf-8')
                        except Exception:
                            try:
                                json_path.write_bytes(pre_rederive_bytes)
                            except Exception:
                                self.flog.exception(
                                    f'[PiiReview] {cid}/{stem}: BUG-A read '
                                    f're-derive rollback write_bytes ALSO '
                                    f'failed')
                            self.flog.error(
                                f'BUGA_READ_REDERIVE_FAILED_ROLLED_BACK '
                                f'{cid}/{stem}')
                            self.flog.exception(
                                f'[PiiReview] {cid}/{stem}: BUG-A read-path '
                                f're-derive failed; rolled back to pre-call '
                                f'on-disk state (manual/decisions preserved, '
                                f'recommendations[] stale)')
            except Exception:
                # Outer guard: any failure in the re-derive scaffold must
                # never break the GET. Serve whatever is on disk.
                self.flog.exception(
                    f'[PiiReview] {cid}/{stem}: BUG-A read-path scaffold '
                    f'error (serving on-disk pii.json as-is)')
            # -------------------------------------------------------------

            with open(json_path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))

        @self.app.route('/api/pii-review/viz-data')
        def api_viz_data():
            cid = self._safe_client_id(request.args.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            stem = request.args.get('stem', '')
            if not stem or '..' in stem or '/' in stem or '\\' in stem:
                return jsonify({'error': 'bad stem'}), 400
            # Cache-first: read the lite sidecar if present, else fall to
            # _resolve_viz_data which generates it via process_one_pdf(lite=True).
            lite_sidecar = self._client_output_dir(cid) / f'{stem}_viz_data.lite.json'

            src_path = None
            if lite_sidecar.exists():
                src_path = lite_sidecar

            if src_path is None:
                pdf_path = self._client_data_dir(cid) / f'{stem}.pdf'
                if not pdf_path.exists():
                    return jsonify({'error': 'source PDF missing'}), 404
                vd = self._resolve_viz_data(cid, pdf_path)
                if vd is None:
                    return jsonify({'error': 'viz_data unavailable'}), 500
            else:
                try:
                    with open(src_path, 'r', encoding='utf-8') as fh:
                        vd = json.load(fh)
                except Exception as e:
                    return jsonify({'error': f'viz_data read failed: {e}'}), 500

            slim = {
                'schema_version':  vd.get('schema_version'),
                'source_filename': vd.get('source_filename'),
                'pages': {},
            }
            for p_str, pdata in (vd.get('pages') or {}).items():
                slim_xts = []
                for xt in (pdata.get('xml_texts') or []):
                    slim_words = [
                        {
                            'id':      w.get('id'),
                            'text':    w.get('text'),
                            'left':    w.get('left'),
                            'top':     w.get('top'),
                            'right':   w.get('right'),
                            'bottom':  w.get('bottom'),
                            **({'left_d':  w['left_d']}  if 'left_d'  in w else {}),
                            **({'right_d': w['right_d']} if 'right_d' in w else {}),
                        }
                        for w in (xt.get('words') or [])
                    ]
                    slim_entry = {
                        'id':       xt.get('id'),
                        'left':     xt.get('left'),
                        'top':      xt.get('top'),
                        'right':    xt.get('right'),
                        'bottom':   xt.get('bottom'),
                        # `content` is the xml_text-level text string. Usually
                        # redundant with words[].text joined, but on rows where
                        # the v4 extractor failed to populate words[] it's the
                        # only place the text survives. Frontend synthesises a
                        # virtual word from this when words[] is empty.
                        'content':  xt.get('content'),
                        # `rotation` (0/90/180/270) — frontend uses it to
                        # order words within a vertical-text xml_text correctly.
                        # Without this, 90°-rotated text reads backwards in
                        # the selection HUD and won't match its own content.
                        'rotation': xt.get('rotation', 0),
                        'words':    slim_words,
                    }
                    if xt.get('is_ghost'):
                        slim_entry['is_ghost'] = True
                    slim_xts.append(slim_entry)
                slim['pages'][p_str] = {
                    'page_layout': pdata.get('page_layout'),
                    'xml_texts':   slim_xts,
                }
            return jsonify(slim)

        @self.app.route('/api/pii-review/decisions', methods=['POST'])
        def api_decisions():
            body = request.get_json() or {}
            cid = self._safe_client_id(body.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            stem = body.get('stem', '')
            decisions = body.get('decisions', {}) or {}
            manual    = body.get('manual_additions', []) or []
            # `recommendations` is optional — None means "don't touch existing".
            # When the UI sends it (modern flow), it's the canonical list
            # reflecting any reject/restore/remove the user just did.
            recs      = body.get('recommendations', None)
            # Explicit-deletion channel (Bug 6 fix). FE sends entities the
            # user actively trashed via the trash button. Only these are
            # removed from the CRM; entities merely absent from FE state
            # are carried forward by _merge_into_client_config.
            deleted_entities = body.get('deleted_entities', []) or []
            if not stem or '..' in stem or '/' in stem or '\\' in stem:
                return jsonify({'error': 'bad stem'}), 400
            json_path = self._client_output_dir(cid) / f'{stem}_pii.json'
            if not json_path.exists():
                return jsonify({'error': 'not analyzed yet'}), 404
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # ---- M-MERGE (ADR-MERGE) B-capture -------------------------
            # B = the entity's PRIOR on-disk recommendations[].occurrences
            # baseline the user curated FROM. `data` was just json.load'd
            # above and STILL holds the pre-POST persisted recommendations[]
            # (the last-persisted-by-_populate/analyze record). Capture it
            # READ-ONLY here, in the window BEFORE PR:1088-1089 overwrites
            # data['recommendations'] with the frontend POST. This is the
            # ONLY new read op: no disk read (data already loaded), no wire
            # field, no schema field (ADR-MERGE "B-capture is the only new
            # read; read-only and schema-free"). Keyed by entity name; per
            # entity B(entity) = set of k(o)=(page,xml_text_id) over its
            # prior on-disk occurrences. Used post-_populate to discriminate
            # a user-TRIM (k ∈ keys(B), k ∉ keys(U) ⇒ NOT re-added) from a
            # genuinely-new matcher hit (k ∉ keys(B) ∧ k ∉ keys(U) ⇒ added,
            # KI §1 staleness stays fixed). M-MERGE is ADDITIVE to M1's
            # restore — M1's decisions/manual snapshot/restore + the
            # FINDING-1 pre_populate_bytes rollback are UNCHANGED.
            def _mm_occ_key(o):
                return (o.get('page'), o.get('xml_text_id'))
            mm_B_keys = {}
            for _r in (data.get('recommendations') or []):
                _ent = _r.get('entity')
                if _ent is None:
                    continue
                mm_B_keys.setdefault(_ent, set()).update(
                    _mm_occ_key(o) for o in (_r.get('occurrences') or []))
            # ------------------------------------------------------------
            data['decisions']           = decisions
            data['manual_additions']    = manual
            if recs is not None:
                data['recommendations'] = recs
            data['decisions_updated_at'] = datetime.now().isoformat()
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Auto-merge into client config so the next PDF for this client
            # picks up these literals automatically.
            merged = self._merge_into_client_config(
                cid, manual, decisions, source_stem=stem,
                deleted_entities=deleted_entities,
            )

            # ---- #1 (M1 / ADR-1 mechanism (a) + SA-F1) -------------------
            # Systemic staleness fix: after the CRM merge, re-derive the
            # SOURCE stem's recommendations[] against its own viz_data using
            # the EXISTING correct routine (the same 3-arg routine /reanalyze
            # invokes at PR:1154) — so a stale frontend-POSTed
            # recommendations[] payload can never durably win on a Save
            # (KI §1; SC-1 A/B). _propagate_to_siblings deliberately SKIPS
            # the source stem (PR:508-509); this closes that gap for the
            # source PDF without forcing the user to remember /reanalyze.
            #
            # SA-F1 (binding): _populate_recommendations_from_client_config
            # ALSO overwrites data['decisions'] (re-derived from client-config
            # literals, PR:473) and data['from_client_config'], and PERSISTS
            # the result to pii.json on disk inside the routine (PR:472-477).
            # The frontend's just-POSTed decisions were written to
            # data['decisions'] at PR:1086 immediately above. Therefore we
            # MUST snapshot BOTH the just-saved decisions AND manual_additions
            # BEFORE the call and restore BOTH AFTER it, using the EXACT
            # re-read-post-call-pii.json pattern /reanalyze uses for
            # manual_additions (PR:1152 / PR:1157-1159) — re-read the
            # post-call (clobbered) pii.json from disk, set decisions +
            # manual_additions back from the snapshots, re-write to disk.
            # This closes the disk-clobber partial-path hole by construction.
            # Net: a Save durably persists the user's just-toggled decisions
            # AND manual_additions AND a freshly-correct recommendations[].
            # from_client_config is left as the routine sets it (True —
            # correct for the source stem). ZERO api_decisions wire delta
            # (purely server-side, internal to this hook).
            #
            # FAILURE-PATH (FINDING-1, reviewer Required Edit 1, approach (b)):
            # _populate_recommendations_from_client_config PERSISTS its own
            # re-derived data['decisions'] (config map, PR:473) to json_path
            # on disk at PR:477 BEFORE it returns. If ANY exception fires
            # AFTER that disk-persist but BEFORE the happy-path restore below
            # completes its re-write, the on-disk pii.json would be left with
            # the routine's re-derived config decisions in place of the user's
            # just-toggled decisions — the exact SA-F1 / SC-1 B / ADR-1 /
            # IG Entry-5 watch-point (e) clobber this milestone exists to
            # eliminate. To make the SA-F1 invariant hold on EVERY outcome,
            # snapshot the FULL pre-_populate on-disk pii.json bytes (the
            # durable frontend state written at PR:1091-1092 above) and, on
            # ANY exception after the _populate call, ROLL BACK by writing
            # those exact bytes back to json_path (⇒ user decisions+manual
            # intact, recommendations[] stale — the documented acceptable
            # degradation), then emit an observable marker and log. The
            # happy-path restore (re-read post-call disk, restore decisions+
            # manual from the in-hand snapshots, re-write) is UNCHANGED.
            pre_populate_bytes = None
            try:
                pdf_path = self._client_data_dir(cid) / f'{stem}.pdf'
                if pdf_path.exists():
                    viz_data = self._resolve_viz_data(cid, pdf_path)
                    if viz_data:
                        # Snapshot the just-saved frontend payload BEFORE the
                        # refresh call (decisions written at PR:1086; manual
                        # written at PR:1087). These are authoritative for
                        # this Save and MUST survive the routine's
                        # decisions-overwrite + disk-persist.
                        snapshot_decisions = decisions
                        snapshot_manual    = manual

                        # Capture the FULL pre-_populate on-disk pii.json
                        # bytes (the PR:1091-1092 durable frontend state:
                        # user decisions + manual + frontend recs). This is
                        # the rollback target for the FINDING-1 failure path.
                        pre_populate_bytes = json_path.read_bytes()

                        self._populate_recommendations_from_client_config(
                            cid, viz_data, json_path,
                        )

                        # Restore via the /reanalyze PR:1157-1159 mirror:
                        # re-read the post-call (decisions-clobbered) pii.json
                        # from disk, set decisions + manual_additions back
                        # from the snapshots, re-write to disk. The routine's
                        # re-derived decisions map is discarded for the source
                        # stem; the user's just-toggled decisions win.
                        refreshed = json.loads(
                            json_path.read_text(encoding='utf-8'))
                        refreshed['decisions']        = snapshot_decisions
                        refreshed['manual_additions'] = snapshot_manual

                        # ---- M-MERGE (ADR-MERGE) occurrences merge-back ----
                        # EXACTLY the binding three-set algebra (ADR-MERGE /
                        # 05 M-MERGE DoD-1):
                        #   merged(entity) = U
                        #     ∪ { r ∈ R : k(r) ∉ keys(U) ∧ k(r) ∉ keys(B) }
                        # with k(o)=(o["page"],o["xml_text_id"]) — #0's exact
                        # equality key (JS:242 / KI:39). U = the user-POSTed
                        # PRE-_populate recommendations[entity].occurrences
                        # parsed from pre_populate_bytes (ALREADY in hand,
                        # captured at PR:1163 — the frontend POST state, which
                        # already INCLUDES the user's #0 adds and EXCLUDES
                        # their trims; U is the membership authority). R = the
                        # _populate-re-derived occurrences (post-call
                        # `refreshed`). B = the prior on-disk baseline
                        # (mm_B_keys, captured READ-ONLY before PR:1088).
                        # Every key in U is kept VERBATIM (the #0 matcher-
                        # unrecoverable cross-xml_text bottom-table adds
                        # survive byte-for-byte — DoD-2(i)). A key the matcher
                        # re-derives but absent from U: ∈ keys(B) ⇒ user
                        # TRIMMED it ⇒ NOT re-added (DoD-2(ii)); ∉ keys(B) ⇒
                        # genuinely-new matcher hit ⇒ ADD (DoD-3, KI §1
                        # staleness stays fixed). Written into
                        # refreshed['recommendations'][i]['occurrences'] in
                        # this SAME M1 restore step, BEFORE the single
                        # write_text below — no separate write pass. M1's
                        # decisions/manual restore (the two lines above) and
                        # the FINDING-1 rollback (except: below) are UNCHANGED.
                        # ZERO wire/schema delta — server-internal, merged
                        # into the existing occurrences array shape (I-P2 /
                        # I-P4). _populate / matcher untouched (§C:58 / §C:71
                        # KEEP — only _populate's OUTPUT consumed via R).
                        try:
                            _U_pre = json.loads(
                                pre_populate_bytes.decode('utf-8'))
                            mm_U_occs = {}
                            mm_U_keys = {}
                            for _r in (_U_pre.get('recommendations')
                                       or []):
                                _ent = _r.get('entity')
                                if _ent is None:
                                    continue
                                _uo = list(_r.get('occurrences') or [])
                                mm_U_occs[_ent] = _uo
                                mm_U_keys[_ent] = {
                                    _mm_occ_key(o) for o in _uo}
                        except Exception:
                            # pre_populate_bytes is the in-hand PR:1163
                            # snapshot; a parse failure here is a real
                            # finding, not silently absorbed. Surface and
                            # re-raise into the FINDING-1 rollback path
                            # (decisions/manual preserved, recs stale —
                            # the documented acceptable degradation).
                            self.flog.exception(
                                f'[PiiReview] {cid}/{stem}: M-MERGE '
                                f'pre_populate_bytes parse failed')
                            raise
                        for _r in (refreshed.get('recommendations')
                                   or []):
                            _ent = _r.get('entity')
                            if _ent is None:
                                continue
                            _R_occs = list(_r.get('occurrences') or [])
                            # Distinguish "entity absent from U" from
                            # "entity in U with zero occurrences". Trim
                            # semantics (excluding R-occs whose key is in
                            # B) require positive evidence the user
                            # interacted with this entity; absence is not
                            # such evidence. When the FE POST omits the
                            # entity entirely (e.g. FE state drift, or a
                            # Save that didn't curate every literal), keep
                            # R verbatim to avoid collapsing occurrences
                            # the user never touched.
                            if _ent not in mm_U_keys:
                                self.flog.info(
                                    f'M-MERGE-EVID {cid}/{stem} '
                                    f'entity={_ent!r} U=ABSENT '
                                    f'B={len(mm_B_keys.get(_ent, set()))} '
                                    f'R={len(_R_occs)} '
                                    f'merged={len(_R_occs)} '
                                    f'added_from_R=0 '
                                    f'trims_excluded=0 (kept R verbatim)')
                                continue
                            _Uk = mm_U_keys[_ent]
                            _Bk = mm_B_keys.get(_ent, set())
                            _U_occs = mm_U_occs[_ent]
                            _added = [
                                o for o in _R_occs
                                if _mm_occ_key(o) not in _Uk
                                and _mm_occ_key(o) not in _Bk]
                            _trims = sum(
                                1 for o in _R_occs
                                if _mm_occ_key(o) not in _Uk
                                and _mm_occ_key(o) in _Bk)
                            _r['occurrences'] = _U_occs + _added
                            self.flog.info(
                                f'M-MERGE-EVID {cid}/{stem} '
                                f'entity={_ent!r} U={len(_U_occs)} '
                                f'B={len(_Bk)} R={len(_R_occs)} '
                                f'merged={len(_r["occurrences"])} '
                                f'added_from_R={len(_added)} '
                                f'trims_excluded={_trims}')
                        # Entities present in U but absent from R keep U
                        # verbatim (the merge never drops a user occurrence):
                        # _populate re-derives one rec per client-config
                        # literal, so a user-curated entity with no current
                        # matcher hit would be absent from R. Re-add such
                        # U-only entities so the user's curated occurrences
                        # are NEVER lost (ADR-MERGE: "Entities present in U
                        # but absent from R keep U verbatim").
                        _R_ents = {
                            _r.get('entity')
                            for _r in (refreshed.get('recommendations')
                                       or [])
                            if _r.get('entity') is not None}
                        for _ent, _U_occs in mm_U_occs.items():
                            if _ent in _R_ents or not _U_occs:
                                continue
                            for _br in (
                                    _U_pre.get('recommendations') or []):
                                if _br.get('entity') == _ent:
                                    refreshed.setdefault(
                                        'recommendations', []).append(
                                            json.loads(
                                                json.dumps(_br)))
                                    self.flog.info(
                                        f'M-MERGE-EVID {cid}/{stem} '
                                        f'entity={_ent!r} '
                                        f'U={len(_U_occs)} B='
                                        f'{len(mm_B_keys.get(_ent, set()))}'
                                        f' R=0 merged={len(_U_occs)} '
                                        f'added_from_R=0 '
                                        f'trims_excluded=0 (U-only)')
                                    break
                        # --------------------------------------------------

                        # ---- BUG-ADDR-XPAGE reconcile-pass (Shape (a)) ----
                        # Invariant established: for every entity E jointly
                        # present in refreshed['recommendations'] AND
                        # refreshed['manual_additions'], manual_additions[E]
                        # is DROPPED — matching JS :217-222 _absorbReAdd dedup
                        # intent + JS :1838-1841 redrawAllBboxes render-dedup
                        # intent. The recs-side is authoritative for entities
                        # that have been promoted. ZERO change to
                        # recommendations[]; the V-1 overwrite at PR:1300 is
                        # preserved; the reconcile operates on the
                        # POST-M-MERGE refreshed state so it sees the canonical
                        # recs, not the frontend POST. Strictly additive to
                        # M-MERGE three-set algebra (:1424-1505) and FINDING-1
                        # rollback. Class-eliminating: contains no entity /
                        # page / client literal; fires on every Save; idempotent
                        # (second Save with no joint-presence ⇒ no-op).
                        # See BE_Build_Lead/BUG-ADDR-XPAGE_DESIGN.md.
                        _rec_entities = {
                            _r.get('entity')
                            for _r in (
                                refreshed.get('recommendations') or [])
                            if _r.get('entity') is not None
                        }
                        _manual_in = refreshed.get('manual_additions') or []
                        _manual_reconciled = []
                        _dropped = []
                        for _m in _manual_in:
                            _ent = (
                                _m.get('entity')
                                if isinstance(_m, dict) else None)
                            if _ent is not None and _ent in _rec_entities:
                                _dropped.append(_ent)
                                continue
                            _manual_reconciled.append(_m)
                        if _dropped:
                            refreshed['manual_additions'] = (
                                _manual_reconciled)
                            self.flog.info(
                                f'BUG-ADDR-XPAGE-RECONCILE {cid}/{stem} '
                                f'dropped_manual_additions_promoted_to_'
                                f'recs={_dropped} '
                                f'manual_before={len(_manual_in)} '
                                f'manual_after={len(_manual_reconciled)}')
                        # ----------------------------------------------------

                        json_path.write_text(
                            json.dumps(refreshed, indent=2,
                                       ensure_ascii=False),
                            encoding='utf-8')
            except Exception:
                # Refresh failure must NOT leave the user's just-saved
                # decisions clobbered on disk. Note: _populate's PR:477
                # disk-write SUPERSEDES the PR:1091-1092 frontend write, so
                # the Save is NOT unconditionally intact — decisions
                # durability depends on the happy-path restore OR this
                # rollback completing. If the exception fired after
                # _populate's disk-persist, on-disk pii.json currently holds
                # the routine's re-derived config decisions; roll back to the
                # full pre-_populate bytes (the PR:1091-1092 durable frontend
                # state: user decisions+manual intact, recommendations[]
                # stale — the documented acceptable degradation) so the
                # SA-F1 invariant holds on this failure path too. (If the
                # exception fired BEFORE _populate ran or before the bytes
                # were captured, pre_populate_bytes is None / equals the
                # current disk state and there is nothing to clobber; the
                # guarded write is a no-harm best-effort restore either way.)
                if pre_populate_bytes is not None:
                    try:
                        json_path.write_bytes(pre_populate_bytes)
                    except Exception:
                        self.flog.exception(
                            f'[PiiReview] {cid}/{stem}: #1 rollback '
                            f'write_bytes ALSO failed')
                # Observable failure marker (reviewer Required Edit 3 / ADR-1
                # / 05 M1 "a finding for PM, never silently absorbed"): a
                # distinctive, greppable status line so an except-path
                # failure is NOT invisible behind the unchanged ok:True
                # response. Log/status marker ONLY — NOT a wire field /
                # response-shape change (that would be a forbidden contract
                # delta).
                self.flog.error(
                    f'M1_REFRESH_FAILED_ROLLED_BACK {cid}/{stem}')
                # Honest surfacing with full traceback (not a silent swallow
                # of the staleness fix — a real refresh failure on a real PDF
                # at the M1 gate is a finding for PM, never a silent defer).
                self.flog.exception(
                    f'[PiiReview] {cid}/{stem}: #1 source-recs refresh '
                    f'failed; rolled back to pre-_populate on-disk state '
                    f'(decisions/manual preserved, recommendations[] stale)')

            # If anything actually changed, propagate to siblings in the
            # background so their recommendations[] reflect the updated CRM
            # immediately. Manual_additions on siblings are preserved.
            sibling_count = 0
            if merged > 0:
                pii_suffix = '_pii.json'
                out_dir = self._client_output_dir(cid)
                if out_dir.exists():
                    sibling_stems = [f.name[:-len(pii_suffix)]
                                     for f in out_dir.glob(f'*{pii_suffix}')
                                     if f.name[:-len(pii_suffix)] != stem]
                    sibling_count = len(sibling_stems)
                    if sibling_count:
                        self._propagate_pool.submit(
                            self._propagate_to_siblings, cid, stem,
                        )
            return jsonify({
                'ok': True,
                'saved_at': data['decisions_updated_at'],
                'client_merged': merged,
                'siblings_refreshing': sibling_count,
            })

        @self.app.route('/api/pii-review/reanalyze', methods=['POST'])
        def api_reanalyze():
            """Re-resolve client literals against this PDF's viz_data and
            overwrite recommendations + decisions in the existing pii.json.
            Manual_additions are preserved.

            Use case: client config grew after this PDF was first analyzed
            (e.g. you added literals on a sibling statement). This refreshes
            recommendations[] without forcing the user to delete files.
            """
            body = request.get_json() or {}
            cid = self._safe_client_id(body.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            stem = body.get('stem', '')
            if not stem or '..' in stem or '/' in stem or '\\' in stem:
                return jsonify({'error': 'bad stem'}), 400

            pdf_path = self._client_data_dir(cid) / f'{stem}.pdf'
            if not pdf_path.exists():
                return jsonify({'error': 'source PDF missing'}), 404
            json_path = self._client_output_dir(cid) / f'{stem}_pii.json'
            if not json_path.exists():
                return jsonify({'error': 'pii.json missing — analyze first'}), 404

            viz_data = self._resolve_viz_data(cid, pdf_path)
            if not viz_data:
                return jsonify({'error': 'viz_data unavailable'}), 500

            # Preserve user work — read existing manual_additions before re-resolving
            existing = json.loads(json_path.read_text(encoding='utf-8'))
            preserved_manual = existing.get('manual_additions', [])

            hits = self._populate_recommendations_from_client_config(cid, viz_data, json_path)

            # Re-merge preserved manual_additions back in
            data = json.loads(json_path.read_text(encoding='utf-8'))
            data['manual_additions'] = preserved_manual
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

            self.flog.info(f'[PiiReview] {cid}/{stem}: re-analyzed against client config (hits={hits})')
            return jsonify({'ok': True, 'hits': hits, 'preserved_manual': len(preserved_manual)})

        @self.app.route('/api/pii-review/apply', methods=['POST'])
        def api_apply():
            body = request.get_json() or {}
            cid = self._safe_client_id(body.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            stem = body.get('stem', '')
            if not stem or '..' in stem or '/' in stem or '\\' in stem:
                return jsonify({'error': 'bad stem'}), 400
            pdf_path = self._client_data_dir(cid) / f'{stem}.pdf'
            if not pdf_path.exists():
                return jsonify({'error': 'source PDF missing'}), 404
            self._apply_pool.submit(self._run_apply, cid, stem)
            return jsonify({'ok': True, 'status': 'applying'})

        @self.app.route('/api/pii-review/upload', methods=['POST'])
        def api_upload():
            cid = self._safe_client_id(request.args.get('client_id', '')
                                       or request.form.get('client_id', ''))
            if not cid:
                return jsonify({'error': 'client_id required'}), 400
            data_dir = self._client_data_dir(cid)
            if not data_dir.exists():
                return jsonify({'error': 'client not found'}), 404
            if 'file' not in request.files:
                return jsonify({'error': 'no file'}), 400
            f = request.files['file']
            fname = os.path.basename(f.filename or '')
            if not fname.lower().endswith('.pdf'):
                return jsonify({'error': 'only PDFs accepted'}), 400
            dest = data_dir / fname
            f.save(str(dest))
            self._start_analyze(cid, dest)
            return jsonify({'ok': True, 'filename': fname, 'stem': dest.stem, 'client_id': cid})

        @self.app.route('/api/pii-review/pdf/<path:filename>')
        def api_pdf(filename):
            cid = self._safe_client_id(request.args.get('client_id', ''))
            if not cid:
                return 'client_id required', 400
            if '..' in filename:
                return 'bad path', 400
            data_dir = self._client_data_dir(cid)
            out_dir  = self._client_output_dir(cid)
            stem = filename[:-4] if filename.lower().endswith('.pdf') else filename
            for cand in (
                out_dir            / f'{stem}_clean.pdf',
                data_dir           / filename,
                out_dir            / filename,
            ):
                if cand.exists():
                    return send_file(str(cand), mimetype='application/pdf')
            return 'not found', 404
