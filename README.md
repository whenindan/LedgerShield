# LedgerShield

LedgerShield is an AI-powered accounts payable and receivable auditor built for YC Demo Day. It ingests vendor invoices, cross-references contract terms, reconciles charges against your bank ledger, and drafts escalation emails for overdue receivables — all in a single terminal run. The system uses OpenAI to reason about contract language, flag billing anomalies, and generate professional collections correspondence tailored to each client's payment history.

---

## Prerequisites

- Python 3.11 or higher
- An OpenAI API key
- Git

---

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/ledgershield/ledgershield.git
cd ledgershield

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Open .env and set your OpenAI API key:
#   OPENAI_API_KEY=sk-...
```

---

## Running the App

```bash
python main.py
```

The run takes approximately 30–60 seconds depending on API latency. All output is printed to the terminal with rich formatting.

---

## What the Output Looks Like

LedgerShield runs in three sequential phases, each printed to the terminal as it completes:

**Phase 1 – Vendor Invoice Audit**
The system extracts line items from `acme_invoice_oct.txt`, loads the corresponding contract (`acme_corp_agreement.md`), and asks the model to compare every charge against the contract's permitted billing categories and rate ceilings. It prints a formatted audit report listing each anomaly found — including overcharged unit rates, unpermitted fee categories, and arithmetic discrepancies between line-item totals and the stated subtotal. Each finding references the specific contract clause being violated.

**Phase 2 – Bank Ledger Reconciliation**
LedgerShield loads `bank_ledger.csv` and checks whether the invoice total paid matches what was invoiced. If the bank record shows payment of a disputed invoice amount, the system flags the payment and notes the dollar exposure (the difference between what was paid and what should have been owed under contract terms).

**Phase 3 – Accounts Receivable Escalation**
The system loads `accounts_receivable.csv` and `client_email_history.json`, then processes each overdue client. For accounts that are past due, it reads the full email thread and drafts a context-aware collections email. Draft tone escalates automatically based on days overdue: polite reminder for <30 days, firm demand with deadline for 30–60 days, and legal-escalation notice for 60+ days. Each draft is printed to the terminal ready to copy-send.

---

## File Structure

```
ledgershield/
├── main.py                          # Entry point – orchestrates all three phases
├── requirements.txt                 # Pinned Python dependencies
├── .env.example                     # Environment variable template
├── .env                             # Your local secrets (git-ignored)
├── README.md
└── data_sandbox/
    ├── contracts/
    │   └── acme_corp_agreement.md   # Vendor contract with rate ceilings and permitted billing categories
    ├── inbound_invoices/
    │   └── acme_invoice_oct.txt     # October 2024 invoice from Acme Corp (contains intentional errors)
    ├── bank_ledger.csv              # Outbound payment transactions for reconciliation
    ├── accounts_receivable.csv      # Outstanding client invoices with aging data
    └── client_email_history.json    # Prior correspondence threads for delinquent accounts
```
