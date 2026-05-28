from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import pandas as pd
import os
import json
import datetime
from typing import List, Optional
from pydantic import BaseModel

# Import existing engine functions and models
from engine.auditor import audit_all_invoices
from engine.collections import (
    get_delinquent_clients,
    run_collections,
    simulate_incoming_reply,
)
from engine.recovery import run_recovery
from llm_client import get_usage_summary
from models.collections import CollectionsEmail, ParsedReply, SnoozeEntry
from models.dispute import DisputeEmail
from models.invoice import AuditResult
from utils.snooze_store import load_snooze_log, is_snoozed, SNOOZE_FILE
from utils.file_loader import load_csv_as_dataframe, load_json_file

app = FastAPI()

# --- CONSTANTS & PATHS ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data_sandbox"
CONTRACTS_DIR = DATA_DIR / "contracts"
INVOICES_DIR = DATA_DIR / "inbound_invoices"
BANK_LEDGER = DATA_DIR / "bank_ledger.csv"
AR_LEDGER = DATA_DIR / "accounts_receivable.csv"
EMAIL_HISTORY = DATA_DIR / "client_email_history.json"
OUTPUT_DIR = BASE_DIR / "output"
FRONTEND_DIR = BASE_DIR / "frontend"

# Ensure directories exist
OUTPUT_DIR.mkdir(exist_ok=True)
FRONTEND_DIR.mkdir(exist_ok=True)

# --- MODELS FOR API ---

class SystemStatus(BaseModel):
    invoices_count: int
    total_clients: int
    overdue_clients: int
    active_snoozes: int
    api_connected: bool

@app.get("/api/status")
async def get_status():
    invoices_count = len([f for f in INVOICES_DIR.iterdir() if not f.name.startswith(".")])
    ar_df = load_csv_as_dataframe(AR_LEDGER)
    total_clients = len(ar_df)
    overdue_clients = len(ar_df[ar_df['days_overdue'] > 14])
    snooze_log = load_snooze_log()
    active_snoozes = sum(1 for e in snooze_log if is_snoozed(e['client_name']))
    api_connected = os.getenv("OPENAI_API_KEY") is not None
    
    return {
        "invoices_count": invoices_count,
        "total_clients": total_clients,
        "overdue_clients": overdue_clients,
        "active_snoozes": active_snoozes,
        "api_connected": api_connected
    }

@app.get("/api/data")
async def get_all_data():
    # This endpoint will provide data formatted for the Omni dashboard
    
    # 1. AR Book
    ar_df = load_csv_as_dataframe(AR_LEDGER)
    ar_book = []
    for _, row in ar_df.iterrows():
        client = row['client_name']
        snoozed = is_snoozed(client)
        
        tier = "Current"
        if row['days_overdue'] > 60: tier = "Final Demand"
        elif row['days_overdue'] > 30: tier = "Legal Notice"
        elif row['days_overdue'] > 14: tier = "Firm Reminder"
        
        ar_book.append({
            "entity": client,
            "invoice": row['invoice_number'],
            "amount": row['amount_due'],
            "daysOverdue": row['days_overdue'],
            "tier": tier,
            "lock": "Snoozed" if snoozed else None
        })
        
    # 2. Ingest History
    ingest_history = []
    for f in INVOICES_DIR.iterdir():
        if f.name.startswith("."): continue
        stat = f.stat()
        ingest_history.append({
            "ts": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "name": f.name,
            "size": f"{stat.st_size // 1024} KB",
            "ext": f.suffix[1:],
            "cat": "invoice",
            "status": "indexed",
            "source": "manual"
        })
    for f in CONTRACTS_DIR.iterdir():
        if f.name.startswith("."): continue
        stat = f.stat()
        ingest_history.append({
            "ts": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "name": f.name,
            "size": f"{stat.st_size // 1024} KB",
            "ext": f.suffix[1:],
            "cat": "contract",
            "status": "indexed",
            "source": "manual"
        })
        
    # 3. Bank Accounts (simplified from bank_ledger.csv)
    bank_df = load_csv_as_dataframe(BANK_LEDGER)
    # Just a mock for now based on the CSV structure
    bank_accounts = [
        { 
            "id": "main-op", "name": "Main Operating Account", "bank": "Mercury", 
            "logo": "M", "brand": "mercury", "balance": 1250000, "acctNo": "••4471", 
            "apy": "0.05%", "type": "Checking", "lastSync": "Live", "state": "live", "stateLabel": "Live"
        }
    ]
    
    return {
        "ar_book": ar_book,
        "ingest_history": ingest_history,
        "bank_accounts": bank_accounts
    }

@app.post("/api/run-pipeline")
async def run_pipeline():
    try:
        # Phase 1: Audit
        audit_results = audit_all_invoices(INVOICES_DIR, CONTRACTS_DIR)
        
        # Phase 2: Recovery
        dispute_emails = []
        for res in audit_results:
            if not res.passed:
                email = run_recovery(res, BANK_LEDGER, OUTPUT_DIR)
                if email:
                    dispute_emails.append(email)
        
        # Phase 3: Collections
        collections_results = run_collections(AR_LEDGER, EMAIL_HISTORY)
        delinquent_df = get_delinquent_clients(AR_LEDGER)
        
        usage = get_usage_summary()
        
        # Format results for Omni ALERTS
        alerts = []
        
        # Add Audit Failures
        for res in audit_results:
            if not res.passed:
                email = next((e for e in dispute_emails if e.invoice_number == res.invoice.invoice_number), None)
                
                micro = []
                for f in res.flags[:3]:
                    micro.append({"k": f.field, "v": f.actual, "tone": "bad" if f.severity == "error" else "warn"})
                
                evidence = []
                for f in res.flags:
                    evidence.append({"k": f.field, "v": f.expected, "val": f.actual, "tone": "bad" if f.severity == "error" else "warn"})
                
                alerts.append({
                    "id": f"AUDIT-{res.invoice.invoice_number}",
                    "sev": "crimson" if any(f.severity == "error" for f in res.flags) else "amber",
                    "sevLabel": "Variance · High" if any(f.severity == "error" for f in res.flags) else "Variance · Warning",
                    "vendor": res.invoice.vendor_name,
                    "title": f"Invoice {res.invoice.invoice_number} failed audit",
                    "detected": "Just now",
                    "detectedAt": datetime.datetime.now().strftime("%b %d, %Y · %H:%M PT"),
                    "micro": micro,
                    "calc": {
                        "icon": "alert",
                        "headline": f"${res.invoice.total_amount:,.2f} Discrepancy Detected",
                        "tail": f"{len(res.flags)} flags found"
                    },
                    "evidence": evidence,
                    "clauseRef": "Contract Match",
                    "clauseNote": "Variance detected against loaded contract terms.",
                    "action": {
                        "kind": "Outbound Compliance Dispute",
                        "to": email.recipient if email else "billing@" + res.invoice.vendor_name.lower().replace(" ", "") + ".com",
                        "cc": "finance-controls@ledgershield.ai",
                        "from": "treasury-ops@ledgershield.ai",
                        "subject": email.subject if email else f"Dispute: Invoice {res.invoice.invoice_number}",
                        "body": email.body if email else "Drafting error."
                    }
                })
        
        # Add Collections
        for res in collections_results:
            client = res['client_name']
            email: CollectionsEmail = res['email']
            tier = res['escalation_tier']
            
            client_row = delinquent_df[delinquent_df['client_name'] == client].iloc[0]
            
            alerts.append({
                "id": f"COLL-{client_row['invoice_number']}",
                "sev": "crimson" if tier == 'final_demand' else "amber",
                "sevLabel": tier.replace('_', ' ').title(),
                "vendor": client,
                "title": f"Account receivable overdue ({client_row['days_overdue']} days)",
                "detected": "Just now",
                "detectedAt": datetime.datetime.now().strftime("%b %d, %Y · %H:%M PT"),
                "micro": [
                    {"k": "Balance Outstanding", "v": f"${client_row['amount_due']:,.2f}", "tone": "warn"},
                    {"k": "Days Past Due", "v": f"{client_row['days_overdue']} days", "tone": "warn"}
                ],
                "calc": {
                    "icon": "snooze",
                    "headline": f"${client_row['amount_due']:,.2f} Balance Outstanding",
                    "tail": f"Tier: {tier}"
                },
                "evidence": [
                    {"k": "Invoice ID", "v": client_row['invoice_number'], "val": f"${client_row['amount_due']:,.2f}", "tone": "warn"},
                    {"k": "Days past due", "v": "Overdue", "val": f"{client_row['days_overdue']} days", "tone": "warn", "sum": True}
                ],
                "clauseRef": "Service Agreement",
                "clauseNote": f"Escalated to {tier} based on aging.",
                "action": {
                    "kind": f"Outbound {tier.replace('_', ' ').title()}",
                    "to": client_row['contact_email'],
                    "cc": "legal@ledgershield.ai",
                    "from": "ar@ledgershield.ai",
                    "subject": email.subject,
                    "body": email.body
                }
            })
            
        return {
            "alerts": alerts,
            "usage": usage
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload")
async def upload_file(kind: str, file: UploadFile = File(...)):
    dest_dir = INVOICES_DIR if kind == "invoice" else CONTRACTS_DIR if kind == "contract" else DATA_DIR
    file_path = dest_dir / file.filename
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())
    return {"filename": file.filename, "status": "uploaded"}

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open(FRONTEND_DIR / "index.html", "r") as f:
        return f.read()

# Serve static files
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
