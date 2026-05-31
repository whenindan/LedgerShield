# LedgerShield

LedgerShield is an AI-powered accounts payable auditor. Upload your vendor invoices and contracts through the web dashboard; LedgerShield extracts structured data, checks every line item against the contract terms, and drafts formal dispute emails for any discrepancies found.

---

## How It Works

### Upload flow

1. **Stage files** — upload one or more files to a session (`POST /api/upload/file`). Files are held in `uploads/raw/` until the session is processed.
2. **Process session** — trigger `POST /api/upload/session/{id}/process`. For each file:
   - Text is extracted (pdfplumber for PDFs, pytesseract OCR for images, direct read for `.txt`/`.md`) and written to `uploads/processed/`.
   - **Contracts** are additionally copied into `library/contracts/` — a persistent library that is shared across all sessions.
   - **Invoices** remain in `uploads/processed/` and are queued for the audit.
3. **Audit** — only the invoices uploaded in the current session are audited. They are matched against every contract in `library/contracts/` using fuzzy vendor-name matching (rapidfuzz, threshold 80). If a match is found, GPT-4o checks:
   - Unit prices against contract ceiling rates
   - Line item categories against permitted billing categories
   - Arithmetic (qty × unit_price = line_total, sum = subtotal, subtotal + tax = total)
   - Tax rate against the contract
4. **Dispute emails** — any invoice that fails audit is passed to the recovery engine, which drafts a formal dispute letter citing each flag, the violated contract clause, and the dollar exposure. Dispute emails are saved as JSON to `output/`.

### Session lifecycle

```
staged → extracting → extracted → (pipeline runs) → complete
                   ↘ failed (on extraction error)
```

A session can be cancelled while in `staging` state. Once `complete`, the files remain queryable via the upload history API.

### Contract library

Uploaded contracts persist in `library/contracts/` and are available to all future sessions. Uploading a new contract for a vendor (e.g., a renewed MSA) will be picked up automatically the next time that vendor's invoices are processed.

> **Note:** The DB and all uploaded files are wiped on every server start. This keeps development state clean. Remove the `_wipe_dir` calls in `init_db()` to persist data across restarts.

---

## File and Folder Reference

```
ledgershield/
│
├── api.py                               # FastAPI server. All endpoints live here.
│                                        # Serves the frontend from /frontend.
│                                        # DB and upload dirs are reset on every start.
│
├── llm_client.py                        # Centralized OpenAI wrapper.
│                                        # extract_structured() calls GPT-4o or GPT-4o-mini
│                                        # and validates the JSON response against a Pydantic
│                                        # model. generate_text() returns a plain string.
│                                        # Both functions track token usage for cost reporting.
│
├── requirements.txt                     # Pinned Python dependencies.
├── .env                                 # Your local secrets — must contain OPENAI_API_KEY.
├── .env.example                         # Template showing which variables are required.
│
├── engine/
│   ├── auditor.py                       # Invoice extraction and contract comparison.
│   │                                    # audit_invoice_files() takes an explicit list of
│   │                                    # paths so only uploaded files are ever audited.
│   │
│   ├── recovery.py                      # Dispute email drafting for failed invoices.
│   │                                    # bank_ledger_path is optional — pass None to skip
│   │                                    # the already-paid check (assumes not yet paid).
│   │
│   └── collections.py                   # AR collections engine (not currently wired into
│                                        # the upload pipeline — available for future use).
│
├── models/
│   ├── invoice.py                       # Pydantic models: LineItem, ExtractedInvoice,
│   │                                    # AuditFlag, AuditResult.
│   │
│   ├── dispute.py                       # Pydantic model: DisputeEmail.
│   │
│   └── collections.py                   # Pydantic models: CollectionsEmail, ParsedReply,
│                                        # SnoozeEntry.
│
├── utils/
│   ├── contract_matcher.py              # Fuzzy vendor-to-contract matching via rapidfuzz.
│   │
│   ├── file_loader.py                   # Text extraction by extension: .txt/.md (direct),
│   │                                    # .pdf (pdfplumber), .png/.jpg (pytesseract).
│   │
│   └── snooze_store.py                  # Thread-safe read/write for snooze_log.json.
│
├── frontend/
│   ├── index.html                       # Single-page dashboard UI. Served at /.
│   └── v3-styles.css                    # Dashboard stylesheet.
│
├── uploads/                             # RUNTIME — wiped on startup.
│   ├── raw/                             # Original uploaded files (binary).
│   └── processed/                       # Extracted .txt versions ready for the pipeline.
│
├── library/
│   └── contracts/                       # RUNTIME — wiped on startup.
│                                        # Extracted contract text, persistent across sessions
│                                        # within a single server run.
│
└── output/                              # RUNTIME — wiped on startup.
                                         # Dispute email JSON files written by recovery engine.
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/status` | Counts of indexed invoices and contracts; API key check |
| `GET`  | `/api/data` | Upload history and bank account placeholder |
| `POST` | `/api/run-pipeline` | Re-audit all processed invoices against the contract library |
| `POST` | `/api/upload/file` | Stage one file into a session |
| `GET`  | `/api/upload/session/{id}` | Get session status and file list |
| `DELETE` | `/api/upload/file/{id}` | Remove a staged file |
| `POST` | `/api/upload/session/{id}/cancel` | Cancel a staging session |
| `POST` | `/api/upload/session/{id}/process` | Extract, route, and audit the session's files |
| `GET`  | `/api/upload/history` | Paginated upload history; filterable by `category` |

### File categories

| Category | Pipeline behaviour |
|----------|--------------------|
| `invoice` | Extracted text is audited against the contract library |
| `contract` | Extracted text is copied to `library/contracts/` for all future sessions |
| `bank_statement` | Extracted and stored; not yet wired into the pipeline |
| `other` | Extracted and stored; not yet wired into the pipeline |

---

## Prerequisites

- Python 3.11 or higher
- An OpenAI API key

---

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your API key
cp .env.example .env
# Open .env and set: OPENAI_API_KEY=sk-...
```

---

## Running

```bash
python api.py
```

Open `http://localhost:8000`. The dashboard lets you upload files, review the contract library, and trigger the audit pipeline. All uploaded files and the database are reset on every restart.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `openai` | GPT-4o and GPT-4o-mini API calls |
| `pydantic` | Structured response validation for all LLM outputs |
| `python-dotenv` | Loads `OPENAI_API_KEY` from `.env` |
| `pandas` | CSV loading |
| `rapidfuzz` | Fuzzy vendor-to-contract filename matching |
| `pdfplumber` | PDF text extraction |
| `pytesseract` | OCR for image files (optional — degrades gracefully if not installed) |
| `Pillow` | Image loading for pytesseract (optional) |
| `tenacity` | Automatic retry on rate limit and connection errors |
| `filelock` | Thread-safe writes to snooze log |
| `fastapi` | REST API server |
| `uvicorn` | ASGI server for FastAPI |
| `python-multipart` | File upload support |
