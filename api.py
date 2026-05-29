import datetime
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from models.dispute import DisputeEmail

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from engine.auditor import audit_all_invoices, audit_invoices_from_paths
from engine.collections import get_delinquent_clients, run_collections
from engine.recovery import run_recovery
from llm_client import get_usage_summary
from models.collections import CollectionsEmail
from utils.file_loader import extract_text_from_upload, load_csv_as_dataframe
from utils.snooze_store import is_snoozed, load_snooze_log

logger = logging.getLogger(__name__)

app = FastAPI()

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
INVOICES_DIR  = DATA_DIR / "inbound_invoices"
CONTRACTS_DIR = DATA_DIR / "contracts"
OUTPUT_DIR    = BASE_DIR / "output"
FRONTEND_DIR  = BASE_DIR / "frontend"
UPLOADS_DIR   = BASE_DIR / "uploads"
RAW_DIR       = UPLOADS_DIR / "raw"
PROCESSED_DIR = UPLOADS_DIR / "processed"
DB_PATH       = BASE_DIR / "uploads.db"

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "md", "txt", "csv", "json"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

VALID_CATEGORIES = {"invoice", "contract", "bank_ledger", "ar_ledger", "email_history", "other"}

# Ensure all directories exist
for _d in [OUTPUT_DIR, FRONTEND_DIR, RAW_DIR, PROCESSED_DIR, INVOICES_DIR, CONTRACTS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ── SQLite DB ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS upload_sessions (
                session_id   TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL,
                user_name    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'staging',
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS uploaded_files (
                file_id           TEXT PRIMARY KEY,
                session_id        TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_filename   TEXT NOT NULL,
                category          TEXT NOT NULL,
                uploaded_by       TEXT NOT NULL,
                uploaded_at       TEXT NOT NULL,
                file_size         INTEGER NOT NULL,
                file_ext          TEXT NOT NULL,
                raw_path          TEXT NOT NULL,
                extracted_path    TEXT,
                pipeline_path     TEXT,
                status            TEXT NOT NULL DEFAULT 'staged',
                error_message     TEXT,
                FOREIGN KEY (session_id) REFERENCES upload_sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_files_session
                ON uploaded_files(session_id);
            CREATE INDEX IF NOT EXISTS idx_files_category
                ON uploaded_files(category);
            CREATE INDEX IF NOT EXISTS idx_files_uploaded_at
                ON uploaded_files(uploaded_at);
        """)


init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_file_dict(row: sqlite3.Row) -> dict:
    return {
        "file_id":           row["file_id"],
        "session_id":        row["session_id"],
        "original_filename": row["original_filename"],
        "category":          row["category"],
        "uploaded_by":       row["uploaded_by"],
        "uploaded_at":       row["uploaded_at"],
        "file_size":         row["file_size"],
        "file_ext":          row["file_ext"],
        "status":            row["status"],
        "error_message":     row["error_message"],
        "pipeline_path":     row["pipeline_path"],
    }


def _row_to_session_dict(row: sqlite3.Row, files: list) -> dict:
    return {
        "session_id":   row["session_id"],
        "created_at":   row["created_at"],
        "user_name":    row["user_name"],
        "status":       row["status"],
        "processed_at": row["processed_at"],
        "files":        files,
    }


def _ensure_session(conn: sqlite3.Connection, session_id: str, user_name: str) -> None:
    """Insert session row if it doesn't exist yet."""
    conn.execute(
        """
        INSERT OR IGNORE INTO upload_sessions (session_id, created_at, user_name, status)
        VALUES (?, ?, ?, 'staging')
        """,
        (session_id, datetime.datetime.now(datetime.timezone.utc).isoformat(), user_name),
    )


def _format_pipeline_alerts(audit_results, dispute_emails, collections_results, delinquent_df):
    """Shared formatting logic for pipeline output → frontend alert shape."""
    alerts = []

    for res in audit_results:
        if not res.passed:
            dispute_email: Optional[DisputeEmail] = next(
                (e for e in dispute_emails if e.invoice_number == res.invoice.invoice_number),
                None,
            )
            micro = [
                {"k": f.field, "v": f.actual, "tone": "bad" if f.severity == "error" else "warn"}
                for f in res.flags[:3]
            ]
            evidence = [
                {"k": f.field, "v": f.expected, "val": f.actual,
                 "tone": "bad" if f.severity == "error" else "warn"}
                for f in res.flags
            ]
            alerts.append({
                "id":        f"AUDIT-{res.invoice.invoice_number}",
                "sev":       "crimson" if any(f.severity == "error" for f in res.flags) else "amber",
                "sevLabel":  "Variance · High" if any(f.severity == "error" for f in res.flags) else "Variance · Warning",
                "vendor":    res.invoice.vendor_name,
                "title":     f"Invoice {res.invoice.invoice_number} failed audit",
                "detected":  "Just now",
                "detectedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%b %d, %Y · %H:%M PT"),
                "micro":     micro,
                "calc":      {
                    "icon":     "alert",
                    "headline": f"${res.invoice.total_amount:,.2f} Discrepancy Detected",
                    "tail":     f"{len(res.flags)} flags found",
                },
                "evidence":  evidence,
                "clauseRef": "Contract Match",
                "clauseNote": "Variance detected against loaded contract terms.",
                "action": {
                    "kind":    "Outbound Compliance Dispute",
                    "to":      dispute_email.recipient if dispute_email else f"billing@{res.invoice.vendor_name.lower().replace(' ', '')}.com",
                    "cc":      "finance-controls@ledgershield.ai",
                    "from":    "treasury-ops@ledgershield.ai",
                    "subject": dispute_email.subject if dispute_email else f"Dispute: Invoice {res.invoice.invoice_number}",
                    "body":    dispute_email.body if dispute_email else "Drafting error.",
                },
            })

    for res in collections_results:
        client     = res["client_name"]
        email: CollectionsEmail = res["email"]  # type: ignore[assignment]
        tier       = res["escalation_tier"]
        client_row = delinquent_df[delinquent_df["client_name"] == client].iloc[0]
        alerts.append({
            "id":        f"COLL-{client_row['invoice_number']}",
            "sev":       "crimson" if tier == "final_demand" else "amber",
            "sevLabel":  tier.replace("_", " ").title(),
            "vendor":    client,
            "title":     f"Account receivable overdue ({client_row['days_overdue']} days)",
            "detected":  "Just now",
            "detectedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%b %d, %Y · %H:%M PT"),
            "micro": [
                {"k": "Balance Outstanding", "v": f"${client_row['amount_due']:,.2f}", "tone": "warn"},
                {"k": "Days Past Due",        "v": f"{client_row['days_overdue']} days",  "tone": "warn"},
            ],
            "calc": {
                "icon":     "snooze",
                "headline": f"${client_row['amount_due']:,.2f} Balance Outstanding",
                "tail":     f"Tier: {tier}",
            },
            "evidence": [
                {"k": "Invoice ID",    "v": client_row["invoice_number"], "val": f"${client_row['amount_due']:,.2f}", "tone": "warn"},
                {"k": "Days past due", "v": "Overdue", "val": f"{client_row['days_overdue']} days", "tone": "warn", "sum": True},
            ],
            "clauseRef":  "Service Agreement",
            "clauseNote": f"Escalated to {tier} based on aging.",
            "action": {
                "kind":    f"Outbound {tier.replace('_', ' ').title()}",
                "to":      client_row["contact_email"],
                "cc":      "legal@ledgershield.ai",
                "from":    "ar@ledgershield.ai",
                "subject": email.subject,
                "body":    email.body,
            },
        })

    return alerts


# ── Existing endpoints ────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    with db() as conn:
        invoices_count = conn.execute(
            "SELECT COUNT(*) FROM uploaded_files WHERE category = 'invoice' AND status = 'extracted'"
        ).fetchone()[0]

    ar_fallback = DATA_DIR / "accounts_receivable.csv"
    if ar_fallback.exists():
        ar_df = load_csv_as_dataframe(ar_fallback)
        total_clients   = len(ar_df)
        overdue_clients = len(ar_df[ar_df["days_overdue"] > 14])
    else:
        total_clients   = 0
        overdue_clients = 0

    snooze_log     = load_snooze_log()
    active_snoozes = sum(1 for e in snooze_log if is_snoozed(e["client_name"]))
    api_connected  = os.getenv("OPENAI_API_KEY") is not None
    return {
        "invoices_count":  invoices_count,
        "total_clients":   total_clients,
        "overdue_clients": overdue_clients,
        "active_snoozes":  active_snoozes,
        "api_connected":   api_connected,
    }


@app.get("/api/data")
async def get_all_data():
    ar_fallback = DATA_DIR / "accounts_receivable.csv"
    ar_book = []
    if ar_fallback.exists():
        ar_df = load_csv_as_dataframe(ar_fallback)
        for _, row in ar_df.iterrows():
            client  = row["client_name"]
            snoozed = is_snoozed(client)
            tier    = "Current"
            if row["days_overdue"] > 60:   tier = "Final Demand"
            elif row["days_overdue"] > 30: tier = "Legal Notice"
            elif row["days_overdue"] > 14: tier = "Firm Reminder"
            ar_book.append({
                "entity":      client,
                "invoice":     row["invoice_number"],
                "amount":      row["amount_due"],
                "daysOverdue": row["days_overdue"],
                "tier":        tier,
                "lock":        "Snoozed" if snoozed else None,
            })

    with db() as conn:
        rows = conn.execute(
            """
            SELECT original_filename, category, file_size, file_ext, uploaded_at, status
            FROM uploaded_files
            WHERE status = 'extracted'
            ORDER BY uploaded_at DESC
            LIMIT 100
            """
        ).fetchall()
    ingest_history = [
        {
            "ts":     row["uploaded_at"],
            "name":   row["original_filename"],
            "size":   f"{row['file_size'] // 1024} KB",
            "ext":    row["file_ext"],
            "cat":    row["category"],
            "status": "indexed",
            "source": "upload",
        }
        for row in rows
    ]

    bank_accounts = [{
        "id": "main-op", "name": "Main Operating Account", "bank": "Mercury",
        "logo": "M", "brand": "mercury", "balance": 1250000, "acctNo": "••4471",
        "apy": "0.05%", "type": "Checking", "lastSync": "Live", "state": "live", "stateLabel": "Live",
    }]

    return {"ar_book": ar_book, "ingest_history": ingest_history, "bank_accounts": bank_accounts}


@app.post("/api/run-pipeline")
async def run_pipeline():
    try:
        bank_ledger_path = DATA_DIR / "bank_ledger.csv"
        ar_ledger_path   = DATA_DIR / "accounts_receivable.csv"
        history_path     = DATA_DIR / "client_email_history.json"

        audit_results  = audit_all_invoices(INVOICES_DIR, CONTRACTS_DIR)
        dispute_emails = []
        for res in audit_results:
            if not res.passed:
                email = run_recovery(
                    res,
                    bank_ledger_path if bank_ledger_path.exists() else None,
                    OUTPUT_DIR,
                )
                if email:
                    dispute_emails.append(email)
        collections_results = run_collections(
            ar_ledger_path if ar_ledger_path.exists() else None,
            history_path   if history_path.exists()   else None,
        )
        delinquent_df = get_delinquent_clients(
            ar_ledger_path if ar_ledger_path.exists() else None
        )
        alerts = _format_pipeline_alerts(audit_results, dispute_emails, collections_results, delinquent_df)
        return {"alerts": alerts, "usage": get_usage_summary()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Upload endpoints ──────────────────────────────────────────────────────────

@app.post("/api/upload/file")
async def upload_file(
    file:       UploadFile = File(...),
    category:   str        = Form(...),
    session_id: str        = Form(...),
    user_name:  str        = Form("Sarah Jenkins (CFO)"),
):
    """Accept one file, persist to disk, record in DB, return file metadata."""
    # Validate category
    if category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}")

    # Validate extension — file.filename is typed str | None by Starlette
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="Upload must include a filename.")
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"File type '.{ext}' not allowed. Accepted: {sorted(ALLOWED_EXTENSIONS)}",
        )

    # Read and size-check
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    file_id        = str(uuid.uuid4())
    stored_name    = f"{file_id}_{filename}"
    raw_path       = RAW_DIR / stored_name
    now            = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Write raw file
    raw_path.write_bytes(content)

    with db() as conn:
        _ensure_session(conn, session_id, user_name)
        conn.execute(
            """
            INSERT INTO uploaded_files
              (file_id, session_id, original_filename, stored_filename,
               category, uploaded_by, uploaded_at, file_size, file_ext, raw_path, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged')
            """,
            (file_id, session_id, filename, stored_name,
             category, user_name, now, len(content), ext, str(raw_path)),
        )

    return {
        "file_id":           file_id,
        "session_id":        session_id,
        "original_filename": filename,
        "category":          category,
        "uploaded_by":       user_name,
        "uploaded_at":       now,
        "file_size":         len(content),
        "file_ext":          ext,
        "status":            "staged",
    }


@app.get("/api/upload/session/{session_id}")
async def get_session(session_id: str):
    """Return session metadata and all files staged in it."""
    with db() as conn:
        session = conn.execute(
            "SELECT * FROM upload_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found.")
        files = conn.execute(
            "SELECT * FROM uploaded_files WHERE session_id = ? ORDER BY uploaded_at",
            (session_id,),
        ).fetchall()
    return _row_to_session_dict(session, [_row_to_file_dict(f) for f in files])


@app.delete("/api/upload/file/{file_id}")
async def delete_file(file_id: str):
    """Remove one staged file from disk and the database."""
    with db() as conn:
        row = conn.execute(
            "SELECT raw_path, status FROM uploaded_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="File not found.")
        if row["status"] not in ("staged", "failed"):
            raise HTTPException(status_code=409, detail="Cannot delete a file that has already been processed.")
        raw = Path(row["raw_path"])
        if raw.exists():
            raw.unlink()
        conn.execute("DELETE FROM uploaded_files WHERE file_id = ?", (file_id,))
    return {"deleted": file_id}


@app.post("/api/upload/session/{session_id}/cancel")
async def cancel_session(session_id: str):
    """Mark session cancelled and delete all raw uploaded files."""
    with db() as conn:
        session = conn.execute(
            "SELECT status FROM upload_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found.")
        if session["status"] not in ("staging",):
            raise HTTPException(status_code=409, detail=f"Session is already '{session['status']}' — cannot cancel.")

        files = conn.execute(
            "SELECT raw_path FROM uploaded_files WHERE session_id = ?", (session_id,)
        ).fetchall()
        for f in files:
            p = Path(f["raw_path"])
            if p.exists():
                p.unlink()

        conn.execute(
            "UPDATE uploaded_files SET status = 'cancelled' WHERE session_id = ?", (session_id,)
        )
        conn.execute(
            "UPDATE upload_sessions SET status = 'cancelled' WHERE session_id = ?", (session_id,)
        )
    return {"session_id": session_id, "status": "cancelled"}


@app.post("/api/upload/session/{session_id}/process")
async def process_session(session_id: str):
    """
    For every staged file in the session:
      1. Extract text (pdfplumber / pytesseract / direct read).
      2. Write extracted .txt to uploads/processed/.
      3. Copy extracted text to the appropriate pipeline directory.
    Then run the full pipeline and return alerts.
    """
    with db() as conn:
        session = conn.execute(
            "SELECT * FROM upload_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found.")
        if session["status"] != "staging":
            raise HTTPException(status_code=409, detail=f"Session status is '{session['status']}' — cannot process.")

        files = conn.execute(
            "SELECT * FROM uploaded_files WHERE session_id = ? AND status = 'staged'",
            (session_id,),
        ).fetchall()

    if not files:
        raise HTTPException(status_code=400, detail="No staged files in this session.")

    # Mark session as processing
    with db() as conn:
        conn.execute(
            "UPDATE upload_sessions SET status = 'processing' WHERE session_id = ?", (session_id,)
        )

    processed_files = []
    extraction_errors = []

    for row in files:
        file_id  = row["file_id"]
        raw_path = Path(row["raw_path"])
        ext      = row["file_ext"]
        category = row["category"]
        orig_name = row["original_filename"]

        # Mark as extracting
        with db() as conn:
            conn.execute(
                "UPDATE uploaded_files SET status = 'extracting' WHERE file_id = ?", (file_id,)
            )

        try:
            # 1. Extract text
            text = extract_text_from_upload(raw_path, ext)

            # 2. Write to processed dir as .txt
            stem           = Path(orig_name).stem
            extracted_name = f"{file_id}_{stem}.txt"
            extracted_path = PROCESSED_DIR / extracted_name
            extracted_path.write_text(text, encoding="utf-8")

            with db() as conn:
                conn.execute(
                    """
                    UPDATE uploaded_files
                    SET status = 'extracted',
                        extracted_path = ?
                    WHERE file_id = ?
                    """,
                    (str(extracted_path), file_id),
                )

            processed_files.append({
                "file_id":      file_id,
                "filename":     orig_name,
                "category":     category,
                "extracted_path": str(extracted_path),
                "status":       "extracted",
            })

        except Exception as exc:
            logger.error("Extraction failed for %s: %s", orig_name, exc)
            extraction_errors.append({"file_id": file_id, "filename": orig_name, "error": str(exc)})
            with db() as conn:
                conn.execute(
                    "UPDATE uploaded_files SET status = 'failed', error_message = ? WHERE file_id = ?",
                    (str(exc), file_id),
                )

    # Collect session-scoped file paths by category
    invoice_paths  = [Path(f["extracted_path"]) for f in processed_files if f["category"] == "invoice"]
    contract_paths = [Path(f["extracted_path"]) for f in processed_files if f["category"] == "contract"]
    bank_ledger    = next((Path(f["extracted_path"]) for f in processed_files if f["category"] == "bank_ledger"), None)
    ar_ledger      = next((Path(f["extracted_path"]) for f in processed_files if f["category"] == "ar_ledger"), None)
    email_history  = next((Path(f["extracted_path"]) for f in processed_files if f["category"] == "email_history"), None)

    if not invoice_paths:
        with db() as conn:
            conn.execute(
                "UPDATE upload_sessions SET status = 'staging' WHERE session_id = ?", (session_id,)
            )
        raise HTTPException(
            status_code=400,
            detail="No invoice files found in this session. Upload at least one file with category 'invoice' before processing.",
        )

    # Run the pipeline using only the files uploaded in this session
    try:
        audit_results  = audit_invoices_from_paths(invoice_paths, contract_paths)
        dispute_emails = []
        for res in audit_results:
            if not res.passed:
                email = run_recovery(res, bank_ledger, OUTPUT_DIR)
                if email:
                    dispute_emails.append(email)
        collections_results = run_collections(ar_ledger, email_history)
        delinquent_df       = get_delinquent_clients(ar_ledger)
        alerts = _format_pipeline_alerts(audit_results, dispute_emails, collections_results, delinquent_df)
        usage  = get_usage_summary()
        pipeline_error = None
    except Exception as exc:
        logger.error("Pipeline failed after upload processing: %s", exc)
        alerts         = []
        usage          = {}
        pipeline_error = str(exc)

    # Mark session complete
    with db() as conn:
        conn.execute(
            """
            UPDATE upload_sessions
            SET status = 'complete', processed_at = ?
            WHERE session_id = ?
            """,
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), session_id),
        )

    return {
        "session_id":       session_id,
        "processed_files":  processed_files,
        "extraction_errors": extraction_errors,
        "alerts":           alerts,
        "usage":            usage,
        "pipeline_error":   pipeline_error,
    }


@app.get("/api/upload/history")
async def get_upload_history(limit: int = 100, offset: int = 0, category: Optional[str] = None):
    """
    Return paginated upload history across all sessions.
    Optionally filter by category.
    """
    with db() as conn:
        if category:
            if category not in VALID_CATEGORIES:
                raise HTTPException(status_code=400, detail=f"Invalid category '{category}'.")
            rows = conn.execute(
                """
                SELECT f.*, s.user_name as session_user, s.status as session_status
                FROM uploaded_files f
                JOIN upload_sessions s ON f.session_id = s.session_id
                WHERE f.category = ?
                ORDER BY f.uploaded_at DESC
                LIMIT ? OFFSET ?
                """,
                (category, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM uploaded_files WHERE category = ?", (category,)
            ).fetchone()[0]
        else:
            rows = conn.execute(
                """
                SELECT f.*, s.user_name as session_user, s.status as session_status
                FROM uploaded_files f
                JOIN upload_sessions s ON f.session_id = s.session_id
                ORDER BY f.uploaded_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0]

    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "files":  [_row_to_file_dict(r) for r in rows],
    }


# ── Static + HTML ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def get_index():
    return (FRONTEND_DIR / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
