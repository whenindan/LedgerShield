import streamlit as st
import pandas as pd
import os
import json
import logging
import datetime
from pathlib import Path
from dotenv import load_dotenv

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

# Load environment variables
load_dotenv()

# --- CONSTANTS & PATHS ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data_sandbox"
CONTRACTS_DIR = DATA_DIR / "contracts"
INVOICES_DIR = DATA_DIR / "inbound_invoices"
BANK_LEDGER = DATA_DIR / "bank_ledger.csv"
AR_LEDGER = DATA_DIR / "accounts_receivable.csv"
EMAIL_HISTORY = DATA_DIR / "client_email_history.json"
OUTPUT_DIR = BASE_DIR / "output"

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="LedgerShield",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CUSTOM CSS ---
def inject_custom_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
    
    :root {
        --bg-color: #0f1117;
        --card-bg: #1a1d27;
        --border-color: #2d3748;
        --accent-color: #6366f1;
        --success-color: #10b981;
        --warning-color: #f59e0b;
        --error-color: #ef4444;
        --text-primary: #f1f5f9;
        --text-secondary: #94a3b8;
    }

    .stApp {
        background-color: var(--bg-color);
        font-family: 'Inter', sans-serif;
        color: var(--text-primary);
    }

    [data-testid="stSidebar"] {
        background-color: #13151f;
        border-right: 1px solid var(--border-color);
    }

    .stMetric {
        background-color: var(--card-bg) !important;
        border: 1px solid var(--border-color) !important;
        border-radius: 12px !important;
        padding: 20px !important;
    }

    .stExpander {
        background-color: var(--card-bg);
        border: 1px solid var(--border-color);
        border-radius: 12px;
        margin-bottom: 1rem;
    }

    .email-box {
        background-color: #0a0c10;
        color: #e2e8f0;
        font-family: 'Courier New', Courier, monospace;
        padding: 15px;
        border-radius: 8px;
        border: 1px solid #334155;
        max-height: 300px;
        overflow-y: auto;
        white-space: pre-wrap;
        font-size: 0.9rem;
    }

    .stButton > button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease;
    }

    .stButton > button[kind="primary"] {
        background-color: var(--accent-color);
        border: none;
        color: white;
    }

    .stButton > button:hover {
        opacity: 0.9;
        transform: translateY(-1px);
    }

    /* Metric label visibility */
    [data-testid="stMetricLabel"] {
        color: #cbd5e1 !important;
        font-size: 0.8rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
    }

    [data-testid="stMetricValue"] {
        color: #f1f5f9 !important;
        font-weight: 700 !important;
    }

    [data-testid="stMetricDelta"] svg {
        display: inline-block !important;
    }

    /* Table text contrast */
    .stTable th, thead th {
        color: #e2e8f0 !important;
        background-color: #252836 !important;
        font-weight: 600 !important;
        border-bottom: 1px solid #3d4a5c !important;
    }

    .stTable td, tbody td {
        color: #cbd5e1 !important;
        border-color: #2d3748 !important;
    }

    /* Dataframe text */
    .stDataFrame [data-testid="stDataFrameResizable"] th {
        color: #e2e8f0 !important;
        background-color: #1e2130 !important;
    }

    .stDataFrame [data-testid="stDataFrameResizable"] td {
        color: #cbd5e1 !important;
    }

    /* Sidebar text contrast */
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] label {
        color: #cbd5e1 !important;
    }

    [data-testid="stSidebar"] strong {
        color: #f1f5f9 !important;
    }

    /* Caption text */
    [data-testid="stCaptionContainer"] {
        color: #94a3b8 !important;
    }

    /* Tab text */
    [data-baseweb="tab"] {
        color: #94a3b8 !important;
    }

    [data-baseweb="tab"][aria-selected="true"] {
        color: #f1f5f9 !important;
    }

    /* Expander header text */
    .stExpander summary {
        color: #e2e8f0 !important;
        font-weight: 600 !important;
    }

    /* Custom classes for status chips */
    .status-chip {
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .status-passed { background-color: #064e3b; color: #6ee7b7; }
    .status-failed { background-color: #7f1d1d; color: #fca5a5; }

    /* Scrollbar styling */
    ::-webkit-scrollbar {
        width: 8px;
    }
    ::-webkit-scrollbar-track {
        background: #0f1117;
    }
    ::-webkit-scrollbar-thumb {
        background: #2d3748;
        border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #4a5568;
    }
    </style>
    """, unsafe_allow_html=True)

inject_custom_css()

# --- STATE MANAGEMENT ---
if 'pipeline_run' not in st.session_state:
    st.session_state.pipeline_run = False
if 'audit_results' not in st.session_state:
    st.session_state.audit_results = []
if 'dispute_emails' not in st.session_state:
    st.session_state.dispute_emails = []
if 'collections_results' not in st.session_state:
    st.session_state.collections_results = []
if 'delinquent_df' not in st.session_state:
    st.session_state.delinquent_df = pd.DataFrame()
if 'last_run_timestamp' not in st.session_state:
    st.session_state.last_run_timestamp = None
if 'usage_summary' not in st.session_state:
    st.session_state.usage_summary = None

# --- SIDEBAR ---
with st.sidebar:
    st.markdown("## 🛡️ LedgerShield")
    st.caption("Autonomous AP/AR Intelligence")
    st.markdown("`v0.1.0`")
    st.space()

    # Status Indicators
    invoices_count = len([f for f in INVOICES_DIR.iterdir() if not f.name.startswith(".")])
    ar_df = load_csv_as_dataframe(AR_LEDGER)
    total_clients = len(ar_df)
    overdue_clients = len(ar_df[ar_df['days_overdue'] > 14])
    snooze_log = load_snooze_log()
    active_snoozes = sum(1 for e in snooze_log if is_snoozed(e['client_name']))

    st.markdown("### SYSTEM STATUS")
    st.markdown(f"📁 **Invoices Ready:** {invoices_count} files")
    st.markdown(f"📋 **AR Clients:** {total_clients} total, {overdue_clients} overdue")
    st.markdown(f"💾 **Snooze Log:** {active_snoozes} active snoozes")
    
    st.space()
    
    # Run Controls
    st.markdown("### RUN CONTROLS")
    
    api_key_loaded = os.getenv("OPENAI_API_KEY") is not None
    if not api_key_loaded:
        st.error("⚠️ OPENAI_API_KEY missing from .env")
        run_disabled = True
    else:
        run_disabled = False

    if st.button("▶ Run Full Pipeline", type="primary", use_container_width=True, disabled=run_disabled):
        try:
            progress_bar = st.progress(0)
            status_text = st.empty()

            # Phase 1: Audit
            status_text.markdown("🔍 **Auditing invoices against contracts...**")
            progress_bar.progress(10)
            audit_results = audit_all_invoices(INVOICES_DIR, CONTRACTS_DIR)
            st.session_state.audit_results = audit_results
            progress_bar.progress(33)

            # Phase 2: Recovery
            status_text.markdown("📬 **Processing recovery/dispute emails...**")
            dispute_emails = []
            for res in audit_results:
                if not res.passed:
                    email = run_recovery(res, BANK_LEDGER, OUTPUT_DIR)
                    if email:
                        dispute_emails.append(email)
            st.session_state.dispute_emails = dispute_emails
            progress_bar.progress(66)

            # Phase 3: Collections
            status_text.markdown("📬 **Processing collections queue...**")
            collections_results = run_collections(AR_LEDGER, EMAIL_HISTORY)
            st.session_state.collections_results = collections_results
            st.session_state.delinquent_df = get_delinquent_clients(AR_LEDGER)
            progress_bar.progress(90)

            # Finalize
            st.session_state.pipeline_run = True
            st.session_state.last_run_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.usage_summary = get_usage_summary()
            
            progress_bar.progress(100)
            status_text.markdown("✅ **Pipeline complete**")
            st.balloons()
        except Exception as e:
            st.error(f"Error during pipeline run: {str(e)}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Audit Only", use_container_width=True, disabled=run_disabled):
            st.session_state.audit_results = audit_all_invoices(INVOICES_DIR, CONTRACTS_DIR)
            st.session_state.pipeline_run = True
            st.success("Audit complete")
    with col2:
        if st.button("Collections Only", use_container_width=True, disabled=run_disabled):
            st.session_state.collections_results = run_collections(AR_LEDGER, EMAIL_HISTORY)
            st.session_state.delinquent_df = get_delinquent_clients(AR_LEDGER)
            st.session_state.pipeline_run = True
            st.success("Collections complete")
            
    if st.button("Clear Snooze Log", use_container_width=True):
        if SNOOZE_FILE.exists():
            SNOOZE_FILE.unlink()
        st.rerun()

    # Last Run Section
    if st.session_state.last_run_timestamp:
        st.space()
        st.markdown("### LAST RUN INFO")
        st.caption(f"🕒 {st.session_state.last_run_timestamp}")
        if st.session_state.usage_summary:
            usage = st.session_state.usage_summary
            total_tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
            st.markdown(f"💰 **LLM Cost:** ${usage['estimated_cost_usd']:.4f}")
            st.markdown(f"🔢 **Tokens:** {total_tokens:,}")

    # API Status at bottom
    st.space("medium")
    if api_key_loaded:
        st.markdown("🟢 **OpenAI API Connected**")
    else:
        st.markdown("🔴 **OpenAI API Disconnected**")

# --- MAIN AREA ---
st.title("🛡️ LedgerShield Dashboard")

if st.session_state.pipeline_run:
    # KPI Cards
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    
    # Card 1: Invoices Audited
    audit_results = st.session_state.audit_results
    flagged_count = sum(1 for r in audit_results if not r.passed)
    with kpi1:
        st.metric(
            label="Invoices Audited", 
            value=len(audit_results),
            delta=f"{flagged_count} flagged" if flagged_count > 0 else "All clear",
            delta_color="inverse" if flagged_count > 0 else "normal"
        )
    
    # Card 2: Amount At Risk
    amount_at_risk = sum(r.invoice.total_amount for r in audit_results if not r.passed)
    already_paid_flag = any(r.already_paid for r in audit_results if not r.passed)
    with kpi2:
        st.metric(
            label="Amount At Risk",
            value=f"${amount_at_risk:,.2f}",
            delta="Already paid!" if already_paid_flag else None,
            delta_color="inverse"
        )
    
    # Card 3: Clients Overdue
    delinquent_df = st.session_state.delinquent_df
    snoozed_count = max(len(delinquent_df) - len(st.session_state.collections_results), 0)
    with kpi3:
        st.metric(
            label="Clients Overdue",
            value=len(delinquent_df),
            delta=f"{snoozed_count} snoozed" if snoozed_count > 0 else None
        )
        
    # Card 4: Est. LLM Cost
    usage = st.session_state.usage_summary or {"estimated_cost_usd": 0, "prompt_tokens": 0, "completion_tokens": 0}
    total_tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
    with kpi4:
        st.metric(
            label="Est. LLM Cost",
            value=f"${usage['estimated_cost_usd']:.4f}",
            delta=f"{total_tokens:,} tokens"
        )

    # Injecting border colors for metrics
    st.markdown(f"""
    <style>
    [data-testid="stHorizontalBlock"] > div:nth-child(1) [data-testid="stMetric"] {{ border-top: 5px solid #6366f1 !important; }}
    [data-testid="stHorizontalBlock"] > div:nth-child(2) [data-testid="stMetric"] {{ border-top: 5px solid {"#ef4444" if flagged_count > 0 else "#10b981"} !important; }}
    [data-testid="stHorizontalBlock"] > div:nth-child(3) [data-testid="stMetric"] {{ border-top: 5px solid #f59e0b !important; }}
    [data-testid="stHorizontalBlock"] > div:nth-child(4) [data-testid="stMetric"] {{ border-top: 5px solid #94a3b8 !important; }}
    </style>
    """, unsafe_allow_html=True)

# Tabs
tab1, tab2, tab3 = st.tabs(["🔍 Invoice Audit", "📬 Collections", "🔄 Reply Simulator"])

# --- TAB 1: INVOICE AUDIT ---
with tab1:
    if not st.session_state.pipeline_run:
        st.markdown("<div style='text-align: center; padding: 100px;'>", unsafe_allow_html=True)
        st.markdown("### 🛡️ No audit run yet.")
        st.markdown("Click 'Run Full Pipeline' in the sidebar to begin.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        # Summary Table
        audit_data = []
        for res in st.session_state.audit_results:
            audit_data.append({
                "Invoice File": res.invoice.invoice_number,
                "Vendor": res.invoice.vendor_name,
                "Invoice #": res.invoice.invoice_number,
                "Total": f"{res.invoice.currency} {res.invoice.total_amount:,.2f}",
                "Flags": len(res.flags),
                "Status": "✅ PASSED" if res.passed else "❌ FAILED"
            })
        st.dataframe(pd.DataFrame(audit_data), use_container_width=True, hide_index=True)
        
        # Flagged Expanders
        for res in st.session_state.audit_results:
            if not res.passed:
                with st.expander(f"❌ {res.invoice.invoice_number} — {res.invoice.vendor_name} — {len(res.flags)} flags found"):
                    c1, c2 = st.columns([1, 1])
                    
                    with c1:
                        st.markdown("#### 🚩 AUDIT FLAGS")
                        flags_df = []
                        for f in res.flags:
                            flags_df.append({
                                "Field": f.field,
                                "Expected": f.expected,
                                "Actual": f.actual,
                                "Severity": f.severity.upper()
                            })
                        st.table(flags_df)
                        
                    with c2:
                        st.markdown("#### 📧 DISPUTE EMAIL")
                        # Find the corresponding dispute email
                        email = next((e for e in st.session_state.dispute_emails if e.invoice_number == res.invoice.invoice_number), None)
                        if email:
                            if email.urgent_flag:
                                st.error("⚠️ URGENT: Invoice was already auto-paid. Refund demand included.")
                            st.markdown(f"**To:** {email.recipient}")
                            st.markdown(f"**Subject:** {email.subject}")
                            st.markdown(f"<div class='email-box'>{email.body}</div>", unsafe_allow_html=True)
                            st.code(email.body, language=None)
                            st.success(f"✅ output/dispute_{res.invoice.invoice_number}.json saved")
                        else:
                            st.info("No dispute email drafted for this record.")

# --- TAB 2: COLLECTIONS ---
with tab2:
    if not st.session_state.pipeline_run:
        st.markdown("<div style='text-align: center; padding: 100px;'>", unsafe_allow_html=True)
        st.markdown("### 📬 No collections run yet.")
        st.markdown("Click 'Run Full Pipeline' in the sidebar to begin.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        results = st.session_state.collections_results
        firm_count = sum(1 for r in results if r['escalation_tier'] == 'firm_reminder')
        legal_count = sum(1 for r in results if r['escalation_tier'] == 'legal_notice')
        final_count = sum(1 for r in results if r['escalation_tier'] == 'final_demand')
        
        # Summary row
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("Firm Reminders", firm_count)
        mc2.metric("Legal Notices", legal_count)
        mc3.metric("Final Demands", final_count)
        
        # Collections Table
        del_df = st.session_state.delinquent_df
        table_data = []
        for _, row in del_df.iterrows():
            client = row['client_name']
            snoozed = is_snoozed(client)
            
            # Find in results
            result = next((r for r in results if r['client_name'] == client), None)
            tier = result['escalation_tier'] if result else "N/A"
            
            tier_badge = "🟡 Firm Reminder" if tier == 'firm_reminder' else \
                         "🟠 Legal Notice" if tier == 'legal_notice' else \
                         "🔴 Final Demand" if tier == 'final_demand' else "N/A"
            
            snooze_status = "✅ Snoozed" if snoozed else "🔔 Active"
            
            table_data.append({
                "Client": client,
                "Amount Due": f"${row['amount_due']:,.2f}",
                "Days Overdue": row['days_overdue'],
                "Tier": tier_badge,
                "Snooze Status": snooze_status
            })
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)
        
        # Expanders for active clients
        for res in results:
            client = res['client_name']
            email: CollectionsEmail = res['email']
            tier = res['escalation_tier']
            emoji = "🟡" if tier == 'firm_reminder' else "🟠" if tier == 'legal_notice' else "🔴"
            
            client_row = del_df[del_df['client_name'] == client].iloc[0]
            
            with st.expander(f"{emoji} {client} — ${client_row['amount_due']:,.2f} — {client_row['days_overdue']} days overdue"):
                st.markdown(f"**Amount Due:** ${client_row['amount_due']:,.2f} | **Days Overdue:** {client_row['days_overdue']} | **Contact:** {client_row['contact_email']} | **Tier:** {tier.replace('_', ' ').title()}")
                
                st.space()
                st.markdown("##### 📜 Email History")
                history = load_json_file(EMAIL_HISTORY).get(client, [])
                if history:
                    for h in history[-2:]: # Show last 2
                        st.markdown(f"""
                        <div style='background: #0d1117; padding: 10px; border-radius: 8px; border-left: 3px solid #4a5568; margin-bottom: 5px;'>
                            <small>{h['date']} | From: {h['from']}</small><br>
                            <strong>{h['subject']}</strong><br>
                            <small>{h['body'][:100]}...</small>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.caption("No prior history found.")
                
                st.space()
                st.markdown(f"##### ✍️ Drafted Email: {email.subject}")
                st.markdown(f"<div class='email-box'>{email.body}</div>", unsafe_allow_html=True)
                st.code(email.body, language=None)

# --- TAB 3: REPLY SIMULATOR ---
with tab3:
    st.markdown("### 🔄 Incoming Reply Simulator")
    st.caption("Simulate a client payment response and watch LedgerShield parse intent, log a snooze, and draft a confirmation — in real time.")
    
    col_in, col_out = st.columns([1, 1])
    
    with col_in:
        ar_df = load_csv_as_dataframe(AR_LEDGER)
        overdue_list = ar_df[ar_df['days_overdue'] > 0]['client_name'].tolist()
        
        sim_client = st.selectbox("Select Client", overdue_list)
        sim_text = st.text_area(
            "Paste Client Reply Email", 
            placeholder="Hi, sorry for the delay. I can get this paid by next Friday...",
            height=150
        )
        
        if st.button("▶ Parse Reply & Generate Response", type="primary", use_container_width=True):
            with st.spinner("Parsing reply with AI..."):
                sim_result = simulate_incoming_reply(
                    client_name=sim_client,
                    email_text=sim_text,
                    history_path=EMAIL_HISTORY
                )
                st.session_state.sim_result = sim_result

    with col_out:
        if 'sim_result' in st.session_state:
            res = st.session_state.sim_result
            parsed: ParsedReply = res['parsed_reply']
            snooze: SnoozeEntry = res['snooze_entry']
            conf: str = res['confirmation_email']
            
            # Intent Card
            intent_colors = {
                "promise_to_pay": "#10b981",
                "dispute": "#ef4444",
                "ignore": "#64748b",
                "paid": "#059669",
                "other": "#475569"
            }
            intent_color = intent_colors.get(parsed.intent, "#475569")
            
            st.markdown(f"""
            <div style='background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color); margin-bottom: 10px;'>
                <small style='color: var(--text-secondary)'>🧠 PARSED INTENT</small><br>
                <span style='background: {intent_color}; color: white; padding: 2px 10px; border-radius: 20px; font-size: 0.8rem;'>{parsed.intent.upper()}</span><br>
                <strong>Promise Date:</strong> {parsed.promise_date or 'N/A'}<br>
                <p style='font-size: 0.9rem; margin-top: 5px;'>{parsed.parsed_summary}</p>
            </div>
            """, unsafe_allow_html=True)
            
            # Snooze Card
            if snooze:
                st.markdown(f"""
                <div style='background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color); border-left: 5px solid #f59e0b; margin-bottom: 10px;'>
                    <small style='color: var(--text-secondary)'>💤 SNOOZE LOGGED</small><br>
                    <strong>Snoozed until:</strong> {snooze.snooze_until}<br>
                    <small>Reason: {snooze.reason}</small><br>
                    <div style='color: #10b981; margin-top: 5px; font-size: 0.8rem;'>✅ Snooze entry written to snooze_log.json</div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div style='background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color); margin-bottom: 10px; opacity: 0.6;'>
                    <small style='color: var(--text-secondary)'>💤 NO SNOOZE LOGGED</small><br>
                    <small>Intent was {parsed.intent}</small>
                </div>
                """, unsafe_allow_html=True)
                
            # Confirmation Card
            if conf:
                st.markdown(f"""
                <div style='background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color);'>
                    <small style='color: var(--text-secondary)'>✉️ CONFIRMATION EMAIL DRAFTED</small><br>
                    <div class='email-box' style='margin-top: 5px;'>{conf}</div>
                </div>
                """, unsafe_allow_html=True)
                st.code(conf, language=None)
            else:
                st.markdown(f"""
                <div style='background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color); opacity: 0.6;'>
                    <small style='color: var(--text-secondary)'>✉️ NO CONFIRMATION DRAFTED</small>
                </div>
                """, unsafe_allow_html=True)
