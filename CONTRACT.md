# PII Review — Output Artefact Contract

Downstream consumers (e.g. a portfolio-consolidator that runs VLM
extraction on the safe PDFs) should pin to this contract by version.
Breaking changes bump the major version; additive changes bump minor.

**Current contract version: 1.0.0**

## Output directory layout

For each PDF processed via Apply:

```
output/pii_review/clients/<client>/
├── <stem>_clean.pdf                 # PyMuPDF re-save of input, metadata stripped
├── <stem>_viz_data.lite.json        # Per-word bounding boxes
├── <stem>_pii.json                  # Candidates + decisions
├── <stem>_tokenized.pdf             # PDF with [TOKEN_N] replacements
├── <stem>_tokenized.pdf.map.json    # token → original value mapping
└── safe/
    └── pii_safe_<uuid>.pdf          # PII fully stripped
```

And per-client:

```
data/pii_review/clients/<client>/_client.json
```

## Schema: `_client.json`

```json
{
  "client_id": "string",
  "client_name": "string",
  "custodian": "string (optional)",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp",
  "literals": [
    {
      "entity": "string (the literal text to redact)",
      "category": "PERSON | ACCOUNT_NUMBER | ADDRESS | EMAIL | PHONE_NUMBER | SG_NRIC | SG_UEN | CREDIT_CARD | IBAN_CODE | OTHER",
      "tokenize": true,
      "token": "string (e.g. ACCOUNT_1) — assigned when tokenize=true",
      "rejected": false,
      "first_seen": "stem of the first PDF this literal appeared in"
    }
  ],
  "applied_outputs": [
    {
      "original_stem": "string",
      "original_filename": "string",
      "applied_at": "ISO 8601 timestamp",
      "safe_filename": "pii_safe_<uuid>.pdf",
      "safe_path": "absolute or relative path",
      "sanitize_summary": {
        "sanitized": true,
        "metadata_stripped": true,
        "xmp_stripped": true,
        "openaction_removed": false,
        "additional_actions_removed": false,
        "javascript_removed": 0,
        "annotations_removed": 0,
        "embedded_files_removed": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "pages_processed": 0
      }
    }
  ]
}
```

## Schema: `<stem>_tokenized.pdf.map.json`

```json
{
  "token_to_value": {
    "[ACCOUNT_1]": "the original literal text",
    "[NAME_1]": "...",
    "[ADDRESS_1]": "..."
  },
  "value_to_token": { "literal text": "[ACCOUNT_1]" },
  "bboxes": [
    {
      "page": 1,
      "left": 100.0,
      "top": 200.0,
      "right": 200.0,
      "bottom": 215.0,
      "token": "[ACCOUNT_1]"
    }
  ]
}
```

Downstream consumers MUST:
- Treat `bboxes[]` as order-independent (sort by `(page, left, top)` for
  any comparison).
- Use `token_to_value` to reverse the tokenization after their extraction
  step — never embed the original values in the cloud-bound prompt.

## Schema: `<stem>_pii.json`

```json
{
  "total_pages": 0,
  "analyzed_at": "ISO 8601 timestamp",
  "recommendations": [
    {
      "entity": "string",
      "category": "string (see _client.json categories)",
      "tokenize": true,
      "token": "string",
      "occurrences": [
        {
          "page": 1,
          "left": 0,
          "top": 0,
          "right": 0,
          "bottom": 0,
          "left_d": 0,
          "right_d": 0,
          "xml_text_id": 0,
          "word_ids": [],
          "context": "..."
        }
      ]
    }
  ],
  "decisions": {
    "entity literal": {
      "category": "string",
      "tokenize": true,
      "rejected": false,
      "token": "string (optional)"
    }
  },
  "manual_additions": []
}
```

## Schema: `<stem>_viz_data.lite.json`

Per-page word bounding boxes + xml_text grouping. Used by downstream
pipelines that want to do their own text/layout reasoning on top of the
safe PDF without re-running PDF text extraction.

```json
{
  "pages": [
    {
      "page": 1,
      "width": 0,
      "height": 0,
      "rotation": 0,
      "chars": [{ "left": 0, "top": 0, "right": 0, "bottom": 0, "char": "x" }],
      "words": [
        {
          "left": 0, "top": 0, "right": 0, "bottom": 0,
          "left_d": 0, "right_d": 0,
          "text": "..."
        }
      ],
      "xml_texts": [
        { "id": 0, "left": 0, "top": 0, "right": 0, "bottom": 0,
          "words": [ /* word objects */ ] }
      ]
    }
  ]
}
```

**Note on `_d` fields**: `left_d` / `right_d` come from `pdftotext -bbox-layout`
(poppler-only flag). If the field is missing or 0 across all words, your
`pdftotext` binary is likely xpdf-derived and missing `-bbox-layout`
support — set `POPPLER_BIN_DIR` in `.env`.

## Stability guarantees

A safe PDF produced by version 1.x.y will always be readable by a
consumer that pinned to version 1.0.0; field additions are permitted in
minor versions, removals/renames require a major version bump.

The `bboxes[]` field uses construction-order ordering and is NOT a
canonical form — consumers comparing two tokenized maps for equality MUST
sort first.

The `sanitize_summary.bytes_before` / `bytes_after` fields are subject to
PyMuPDF per-save byte nondeterminism (typically ±10 bytes per file due to
internal stream encoding choices); consumers SHOULD treat these as
informational only, not as a content-equality check.
