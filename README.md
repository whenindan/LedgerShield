# LedgerShield

LedgerShield is an AI-powered accounts payable and receivable auditor. It runs as a single terminal command and executes three sequential phases: auditing inbound vendor invoices against contract terms, detecting whether disputed invoices were already paid, and drafting escalation emails for overdue receivables. All output is printed to the terminal with rich formatting.

---

## How It Works

### Phase 1 — Invoice Audit

1. LedgerShield reads every file in `data_sandbox/inbound_invoices/`.
2. Each invoice file is sent to `gpt-4o-mini`, which extracts structured data — vendor name, invoice number, line items, subtotal, tax, and total — into an `ExtractedInvoice` object.
3. The vendor name is fuzzy-matched (via `rapidfuzz`) against contract filenames in `data_sandbox/contracts/`. A match score below 80 means no contract is found and the invoice is auto-passed.
4. If a contract is found, the extracted invoice JSON and the full contract text are sent together to `gpt-4o`, which checks four things:
   - Whether any unit price exceeds the contract's stated ceiling rate
   - Whether any line item category is prohibited by the contract
   - Whether all arithmetic is correct (qty × unit_price = line_total, sum of line_totals = subtotal, subtotal + tax = total)
   - Whether the tax rate matches the contract
5. Any discrepancy becomes an `AuditFlag` with a field, expected value, actual value, severity (`warning` or `error`), and description.
6. An invoice with zero flags is marked `passed`. Any invoice with flags proceeds to recovery.

### Recovery (within Phase 1)

For each failed invoice:

1. LedgerShield scans `data_sandbox/bank_ledger.csv` to check whether the invoice was already paid via auto-pay. It matches by invoice number in the transaction description, or by amount + vendor name similarity.
2. `gpt-4o-mini` drafts a formal dispute email citing each flag, the specific contract clause violated, and the dollar exposure. If the invoice was already paid, the email leads with urgency and demands an immediate refund.
3. The drafted email is saved as JSON to `output/dispute_<invoice_number>.json` and printed to the terminal.

### Phase 2 — Collections Queue

1. LedgerShield reads `data_sandbox/accounts_receivable.csv`, which contains all outstanding client invoices with aging data.
2. Any client more than 14 days overdue is flagged as delinquent.
3. Clients in the snooze log (`data_sandbox/snooze_log.json`) are skipped if their snooze window has not expired.
4. For each active delinquent client, the escalation tier is determined by days overdue:
   - `firm_reminder` — 15 to 29 days overdue
   - `legal_notice` — 30 to 59 days overdue
   - `final_demand` — 60+ days overdue
5. The client's prior email thread is loaded from `data_sandbox/client_email_history.json`.
6. `gpt-4o-mini` drafts a collections email with tone and language calibrated to the escalation tier. Tone escalates from professional and direct, to formal with legal consequences, to a final collections-agency warning.

### Phase 3 — Reply Simulation

This phase demonstrates inbound reply handling. A hardcoded example reply from "Orion Dynamics" is passed through the pipeline:

1. `gpt-4o-mini` parses the reply and classifies its intent (`promise_to_pay`, `dispute`, `ignore`, `paid`, or `other`). If the client promises a date, the date is resolved to an absolute ISO date even if stated relatively (e.g., "next Friday").
2. If the intent is `promise_to_pay`, a snooze entry is written to `data_sandbox/snooze_log.json` with a `snooze_until` date set two days after the promised payment date. This client will be skipped in future collections runs until the window expires.
3. `gpt-4o-mini` drafts a short confirmation email acknowledging the promise and warning of escalation if payment is not received.

---

## File and Folder Reference

```
ledgershield/
│
├── main.py                              # Entry point. Orchestrates all three phases
│                                        # and prints all output to the terminal via rich.
│
├── llm_client.py                        # Centralized OpenAI wrapper.
│                                        # extract_structured() calls gpt-4o or gpt-4o-mini
│                                        # and validates the JSON response against a Pydantic
│                                        # model. generate_text() returns a plain string.
│                                        # Both functions track token usage for cost reporting.
│
├── requirements.txt                     # Pinned Python dependencies (see below).
├── .env                                 # Your local secrets — must contain OPENAI_API_KEY.
├── .env.example                         # Template showing which variables are required.
│
├── engine/
│   ├── auditor.py                       # Phase 1: Loads invoice files, sends them to gpt-4o-mini
│   │                                    # for extraction, fuzzy-matches vendor to a contract,
│   │                                    # and sends both to gpt-4o for discrepancy detection.
│   │
│   ├── recovery.py                      # Phase 1 (recovery leg): Cross-references failed invoices
│   │                                    # against bank_ledger.csv to detect prior payment,
│   │                                    # then drafts and saves dispute emails.
│   │
│   └── collections.py                   # Phase 2 & 3: Loads AR ledger, filters delinquent clients,
│                                        # determines escalation tier, drafts collections emails,
│                                        # and handles reply parsing + snooze logging.
│
├── models/
│   ├── invoice.py                       # Pydantic models: LineItem, ExtractedInvoice, AuditFlag,
│   │                                    # AuditResult. These define the JSON schema the LLM must
│   │                                    # return during extraction and audit.
│   │
│   ├── dispute.py                       # Pydantic model: DisputeEmail. Fields: recipient, subject,
│   │                                    # body, invoice_number, urgent_flag.
│   │
│   └── collections.py                   # Pydantic models: CollectionsEmail, ParsedReply, SnoozeEntry.
│                                        # CollectionsEmail enforces the escalation_tier enum.
│                                        # ParsedReply enforces the intent enum.
│
├── utils/
│   ├── contract_matcher.py              # Fuzzy vendor-to-contract matching using rapidfuzz.
│   │                                    # Reads filenames from the contracts/ directory and scores
│   │                                    # them against the vendor name extracted from the invoice.
│   │                                    # Returns None if the best match is below score 80.
│   │
│   ├── file_loader.py                   # Dispatches file reads by extension: .txt and .md via
│   │                                    # plain read, .csv via pandas, .pdf via pdfplumber.
│   │                                    # Also exposes load_csv_as_dataframe for structured data.
│   │
│   └── snooze_store.py                  # Thread-safe read/write for data_sandbox/snooze_log.json.
│                                        # Uses filelock to prevent race conditions. Exposes
│                                        # add_snooze_entry() and is_snoozed() for the collections
│                                        # pipeline.
│
├── data_sandbox/
│   │
│   ├── inbound_invoices/
│   │   └── acme_invoice_oct.txt         # SOURCE: A realistic but synthetic vendor invoice from
│   │                                    # "Acme Corp" for October 2024. Contains intentional errors:
│   │                                    # the API call unit rate ($0.019/call) exceeds the contract
│   │                                    # ceiling ($0.012/call), the "Platform Maintenance Fee" is
│   │                                    # a prohibited billing category under the contract, and the
│   │                                    # "Data Export Service" is not a permitted billing category.
│   │                                    # Drop any .txt, .md, .csv, or .pdf invoice here to audit it.
│   │
│   ├── contracts/
│   │   └── acme_corp_agreement.md       # SOURCE: A synthetic Master Service Agreement (MSA-2024-0312)
│   │                                    # between LedgerShield Inc. and Acme Corp. Defines permitted
│   │                                    # billing categories, the API Call Ceiling Rate ($0.012/call),
│   │                                    # prohibited categories (Platform Maintenance Fee), and Net-30
│   │                                    # payment terms. The filename is used for fuzzy vendor matching —
│   │                                    # "acme_corp_agreement" matches the vendor name "Acme Corp".
│   │                                    # Drop any .md contract here to cover additional vendors.
│   │
│   ├── bank_ledger.csv                  # SOURCE: A synthetic outbound payment register for LedgerShield.
│   │                                    # Columns: transaction_id, date, vendor, description, amount, status.
│   │                                    # TXN-2024-1071 shows an auto-pay of $3,127.50 to Acme Corp with
│   │                                    # description "Auto-pay INV-2024-0847", which triggers the
│   │                                    # urgent-refund path in the dispute email for that invoice.
│   │
│   ├── accounts_receivable.csv          # SOURCE: A synthetic AR aging ledger for LedgerShield's clients.
│   │                                    # Columns: client_name, invoice_number, amount_due, invoice_date,
│   │                                    # days_overdue, contact_email, contract_ref.
│   │                                    # Five clients are listed; three are delinquent (>14 days overdue):
│   │                                    #   Orion Dynamics   — 18 days ($4,200)  → firm_reminder
│   │                                    #   Cascade Partners — 35 days ($11,800) → legal_notice
│   │                                    #   Meridian Group   — 67 days ($28,500) → final_demand
│   │
│   ├── client_email_history.json        # SOURCE: Synthetic prior email threads for delinquent clients.
│   │                                    # Keyed by client_name. Each entry is a list of email objects
│   │                                    # (date, from, to, subject, body). The collections engine reads
│   │                                    # this to give the LLM context about what has already been said
│   │                                    # so it doesn't repeat earlier communications.
│   │                                    # Threads exist for: Orion Dynamics (1 email),
│   │                                    # Cascade Partners (3 emails), Meridian Group (3 emails).
│   │
│   └── snooze_log.json                  # GENERATED at runtime. Written by snooze_store.py when a client
│                                        # replies with a payment promise. Each entry stores client_name,
│                                        # snooze_until date, reason, and created_at. Clients with an
│                                        # active snooze entry are skipped in the collections queue.
│
├── output/                              # GENERATED at runtime. Dispute email JSON files are written here,
│                                        # one per flagged invoice: dispute_<invoice_number>.json.
│
└── ledgershield.log                     # GENERATED at runtime. Full DEBUG-level log of every LLM call,
                                         # token count, match score, file loaded, and pipeline step.
```

---

## Data Sources

All data files are synthetic and purpose-built for this demo:

| File | What it represents | Used by |
|---|---|---|
| `acme_invoice_oct.txt` | Vendor invoice from Acme Corp with intentional overbilling errors | `engine/auditor.py` (Phase 1) |
| `acme_corp_agreement.md` | Vendor contract defining rate ceilings and permitted billing categories | `engine/auditor.py` (Phase 1) |
| `bank_ledger.csv` | Outbound payment transactions showing what has already been paid | `engine/recovery.py` (Phase 1) |
| `accounts_receivable.csv` | Outstanding client invoices with aging and contact data | `engine/collections.py` (Phase 2) |
| `client_email_history.json` | Prior email threads with delinquent clients | `engine/collections.py` (Phase 2) |
| `snooze_log.json` | Auto-generated snooze entries when clients promise payment | `utils/snooze_store.py` (Phase 3) |

To add a new vendor invoice, drop any `.txt`, `.md`, `.csv`, or `.pdf` file into `data_sandbox/inbound_invoices/`. To add a matching contract, drop a `.md` file into `data_sandbox/contracts/` — the filename should contain the vendor name so fuzzy matching can connect them (e.g., `globex_corp_agreement.md` for vendor "Globex Corp").

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

```bash
python main.py
```

The full run takes approximately 30–60 seconds depending on API latency. All output is printed to the terminal. Dispute emails are also saved to `output/`. The full debug log is written to `ledgershield.log`.

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

---

## Streamlit Dashboard (YC Demo)

LedgerShield now includes a professional Streamlit dashboard for high-fidelity demos.

To launch the dashboard:
1. Ensure you have installed the requirements: `pip install -r requirements.txt`
2. Run the Streamlit app:
   ```bash
   streamlit run app.py
   ```
3. Open your browser and navigate to http://localhost:8501
