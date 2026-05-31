# ACASO

ACASO is an AI-powered accounts payable and receivable auditor. It runs either as a single terminal command or through a modern web dashboard, and executes three sequential phases: auditing inbound vendor invoices against contract terms, detecting whether disputed invoices were already paid, and drafting escalation emails for overdue receivables.

---

## How It Works

### Phase 1 — Invoice Audit

1. ACASO processes every invoice file uploaded in the current session.
2. Each invoice file is sent to `gpt-4o-mini`, which extracts structured data — vendor name, invoice number, line items, subtotal, tax, and total — into an `ExtractedInvoice` object.
3. The vendor name is fuzzy-matched (via `rapidfuzz`) against the contract files uploaded in the same session. A match score below 80 means no contract is found and the invoice is auto-passed.
4. If a contract is found, the extracted invoice JSON and the full contract text are sent together to `gpt-4o`, which checks four things:
   - Whether any unit price exceeds the contract's stated ceiling rate
   - Whether any line item category is prohibited by the contract
   - Whether all arithmetic is correct (qty × unit_price = line_total, sum of line_totals = subtotal, subtotal + tax = total)
   - Whether the tax rate matches the contract
5. Any discrepancy becomes an `AuditFlag` with a field, expected value, actual value, severity (`warning` or `error`), and description.
6. An invoice with zero flags is marked `passed`. Any invoice with flags proceeds to recovery.

### Recovery (within Phase 1)

For each failed invoice:

1. If a **Bank Ledger** was uploaded in the session, ACASO scans it to check whether the invoice was already paid via auto-pay. It matches by invoice number in the transaction description, or by amount + vendor name similarity.
2. `gpt-4o-mini` drafts a formal dispute email citing each flag, the specific contract clause violated, and the dollar exposure. If the invoice was already paid, the email leads with urgency and demands an immediate refund.
3. The drafted email is saved as JSON to `output/dispute_<invoice_number>.json`.

### Phase 2 — Collections Queue

Runs only if an **AR Ledger** was uploaded in the session.

1. ACASO reads the AR ledger CSV, which contains outstanding client invoices with aging data.
2. Any client more than 14 days overdue is flagged as delinquent.
3. Clients in the snooze log (`data/snooze_log.json`) are skipped if their snooze window has not expired.
4. For each active delinquent client, the escalation tier is determined by days overdue:
   - `firm_reminder` — 15 to 29 days overdue
   - `legal_notice` — 30 to 59 days overdue
   - `final_demand` — 60+ days overdue
5. If an **Email History** file was also uploaded, the client's prior thread is loaded to calibrate tone. Without it, the LLM proceeds without prior context.
6. `gpt-4o-mini` drafts a collections email with tone calibrated to the escalation tier.

### Phase 3 — Reply Simulation

This phase demonstrates inbound reply handling. A hardcoded example reply from "Orion Dynamics" is passed through the pipeline:

1. `gpt-4o-mini` parses the reply and classifies its intent (`promise_to_pay`, `dispute`, `ignore`, `paid`, or `other`). If the client promises a date, it is resolved to an absolute ISO date.
2. If the intent is `promise_to_pay`, a snooze entry is written to `data/snooze_log.json` with a `snooze_until` date set two days after the promised payment date.
3. `gpt-4o-mini` drafts a short confirmation email acknowledging the promise.

---

## What to Upload

Every analysis run uses only the files uploaded in the current session. No data carries over from previous sessions.

### Upload categories and what they enable

| Category | Label in UI | Format | What it enables |
|---|---|---|---|
| `invoice` | Invoice | any | Invoice audit (required) |
| `contract` | Contract / MSA | any | Audit against contract terms (required for flags) |
| `bank_ledger` | Bank Ledger | CSV | Payment detection — urgent vs standard dispute |
| `ar_ledger` | AR Ledger | CSV | Collections queue |
| `email_history` | Email History | JSON | Calibrated collections tone (optional with AR Ledger) |
| `other` | Other Document | any | Stored only, not processed |

A session with only **invoice + contract** runs the audit and drafts a dispute email. Add more file types to activate additional pipeline phases.

### Contract filename convention

ACASO fuzzy-matches the contract filename against the vendor name extracted from the invoice. Name your contract file so it contains the vendor name:
- `acme_corp_agreement.md` → matches vendor "Acme Corp"
- `globex_msa_2024.pdf` → matches vendor "Globex Corp"

A match score below 80 means no contract comparison is run and the invoice auto-passes.

---

## Reference Data (`data/` folder)

The `data/` folder contains reference files you can upload to test the full pipeline:

| File | Upload as | What it contains |
|---|---|---|
| `data/acme_invoice_oct.txt` | Invoice | Acme Corp invoice for Oct 2024 with intentional overbilling errors |
| `data/acme_corp_agreement.md` | Contract / MSA | Acme Corp MSA defining rate ceilings and permitted billing categories |
| `data/bank_ledger.csv` | Bank Ledger | Outgoing payment register — includes auto-pay TXN-2024-1071 for INV-2024-0847 |
| `data/accounts_receivable.csv` | AR Ledger | AR aging data with three delinquent clients (Orion Dynamics, Cascade Partners, Meridian Group) |
| `data/client_email_history.json` | Email History | Prior email threads for the three delinquent clients |

To run a complete demo, upload all five files in one session then click **Proceed to Analysis**.

The `data/inbound_invoices/` and `data/contracts/` subdirectories are used by the CLI only.

---

## DB and Upload Lifecycle

- `uploads.db` and the `uploads/` folder accumulate across server restarts — they are never auto-wiped.
- Each run is isolated by session: the pipeline only reads files from the current session being processed.
- To start completely fresh, delete `uploads.db` and the `uploads/` folder. The server recreates both on next start.

---

## File and Folder Reference

```
acaso/
│
├── main.py                              # CLI entry point. Reads from data/inbound_invoices/
│                                        # and data/contracts/. Drop files there to use the CLI.
│
├── api.py                               # FastAPI server. Reads invoices, contracts, bank_ledger,
│                                        # ar_ledger, and email_history from the current upload
│                                        # session only — nothing is read from data/ at pipeline time.
│
├── llm_client.py                        # Centralized OpenAI wrapper. extract_structured() and
│                                        # generate_text() with token tracking and retry logic.
│
├── requirements.txt                     # Pinned Python dependencies.
├── .env                                 # Your local secrets — must contain OPENAI_API_KEY.
├── .env.example                         # Template showing which variables are required.
│
├── engine/
│   ├── auditor.py                       # Phase 1: Invoice extraction and contract audit.
│   │                                    # audit_invoices_from_paths() — used by the API.
│   │                                    # audit_all_invoices() — used by the CLI.
│   │
│   ├── recovery.py                      # Phase 1 recovery: optional bank_ledger payment check,
│   │                                    # dispute email drafting. Skips payment check if no
│   │                                    # bank_ledger is provided.
│   │
│   └── collections.py                   # Phase 2 & 3: AR ledger processing, delinquency detection,
│                                        # escalation tiers, collections email drafting, reply parsing.
│                                        # Returns empty results if no ar_ledger is provided.
│
├── models/
│   ├── invoice.py                       # Pydantic models: LineItem, ExtractedInvoice, AuditFlag, AuditResult.
│   ├── dispute.py                       # Pydantic model: DisputeEmail.
│   └── collections.py                   # Pydantic models: CollectionsEmail, ParsedReply, SnoozeEntry.
│
├── utils/
│   ├── contract_matcher.py              # Fuzzy vendor-to-contract matching using rapidfuzz.
│   │                                    # match_vendor_to_contract() — accepts list[Path] (API).
│   │                                    # match_vendor_to_contract_in_dir() — scans directory (CLI).
│   │
│   ├── file_loader.py                   # Dispatches file reads by extension. Supports: txt, md,
│   │                                    # csv, json, pdf, png, jpg, jpeg.
│   │
│   └── snooze_store.py                  # Thread-safe read/write for data/snooze_log.json.
│
├── frontend/
│   ├── index.html                       # Single-page dashboard UI. Served at /.
│   └── v3-styles.css                    # Dashboard stylesheet.
│   ├── index.html                       # Single-page dashboard UI (Omni design system).
│   │                                    # Served by FastAPI at the root route (/).
│   │                                    # Contains the Executive Authorization Portal with
│   │                                    # an editable email composer, signed-in user identity
│   │                                    # pill, and session approval history.
│   │
│   └── v3-styles.css                    # Dashboard stylesheet. Imported by index.html.
│
├── data/
│   ├── acme_invoice_oct.txt             # Demo invoice — upload as "Invoice"
│   ├── acme_corp_agreement.md           # Demo contract — upload as "Contract / MSA"
│   ├── bank_ledger.csv                  # Demo bank data — upload as "Bank Ledger"
│   ├── accounts_receivable.csv          # Demo AR data — upload as "AR Ledger"
│   ├── client_email_history.json        # Demo email history — upload as "Email History"
│   ├── snooze_log.json                  # GENERATED at runtime by the reply simulation phase
│   ├── inbound_invoices/                # CLI drop folder for invoices
│   └── contracts/                       # CLI drop folder for contracts
│
├── output/                              # GENERATED at runtime. Dispute JSON files written here.
│
├── uploads/
│   ├── raw/                             # Raw uploaded files with UUID prefix.
│   └── processed/                       # Extracted plain-text versions used by the pipeline.
│
├── uploads.db                           # SQLite: session and file upload history.
└── acaso.log                     # GENERATED at runtime. Full debug log.
```

---

## Prerequisites

- Python 3.11 or higher
- An OpenAI API key

---

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your API key
cp .env.example .env
# Open .env and set: OPENAI_API_KEY=sk-...
```

---

## Running

### Web Dashboard

```bash
python api.py
```

Open `http://localhost:8000`. Upload your files using the file drop panel (see **What to Upload** above), then click **Proceed to Analysis**. Use the files in `data/` as a reference dataset to test the full pipeline.

### CLI

```bash
python main.py
```

Drop invoice files into `data/inbound_invoices/` and contract files into `data/contracts/`. The bank ledger, AR ledger, and email history are read from `data/` automatically if present. The full run takes 30–60 seconds depending on API latency.

**Authorization flow:**

1. Click **Run Pipeline** to execute all three phases and populate the alert queue.
2. Click any alert row to open the detail drawer. The drawer shows the discrepancy evidence table and an AI-drafted email.
3. The email composer displays a **From** pill at the top identifying the signed-in user by name and ID. The email body is fully editable — adjust wording, tone, or amounts before sending.
4. Click **Approve & Send** to dispatch the email. The alert is removed from the queue and a timestamped record (vendor, alert title, approving user name and ID) is appended to the **Approval History** panel below the queue.
5. Click **×** to dismiss the drawer without recording any action.

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | GPT-4o and GPT-4o-mini API calls |
| `pydantic` | Structured response validation for all LLM outputs |
| `python-dotenv` | Loads `OPENAI_API_KEY` from `.env` |
| `rich` | Terminal formatting — panels, tables, colored text |
| `pandas` | CSV loading for bank ledger and AR data |
| `rapidfuzz` | Fuzzy vendor-to-contract filename matching |
| `pdfplumber` | PDF text extraction for invoice files |
| `tenacity` | Automatic retry on rate limit and connection errors |
| `filelock` | Thread-safe writes to the snooze log |
| `fastapi` | REST API server backing the web dashboard |
| `uvicorn` | ASGI server for FastAPI |
| `python-multipart` | File upload support for the `/api/upload` endpoint |
