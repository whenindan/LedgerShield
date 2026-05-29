"""LedgerShield — full pipeline orchestration."""

import logging
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,  # root at DEBUG so handlers can filter independently
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("ledgershield.log"),
    ],
)

_root = logging.getLogger()
for _handler in _root.handlers:
    if isinstance(_handler, logging.FileHandler):
        _handler.setLevel(logging.DEBUG)
    elif isinstance(_handler, logging.StreamHandler):
        _handler.setLevel(logging.INFO)

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONTRACTS_DIR = DATA_DIR / "contracts"
INVOICES_DIR = DATA_DIR / "inbound_invoices"
BANK_LEDGER = DATA_DIR / "bank_ledger.csv"
AR_LEDGER = DATA_DIR / "accounts_receivable.csv"
EMAIL_HISTORY = DATA_DIR / "client_email_history.json"
OUTPUT_DIR = BASE_DIR / "output"

# ── Print helpers ─────────────────────────────────────────────────────────────


def print_banner(console: Console) -> None:
    console.print(
        Panel(
            "[bold cyan]LedgerShield[/bold cyan]\n"
            "[dim]AI-powered invoice auditing, dispute recovery, and collections automation[/dim]",
            title="[bold white]LEDGERSHIELD[/bold white]",
            border_style="cyan",
            padding=(1, 4),
        )
    )


def print_audit_summary(console: Console, results: list[AuditResult]) -> None:
    table = Table(
        title="Invoice Audit Summary",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Invoice File", style="dim")
    table.add_column("Vendor")
    table.add_column("Total", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Flag Count", justify="right")

    for result in results:
        invoice = result.invoice
        status_text = (
            Text("PASSED", style="bold green")
            if result.passed
            else Text("FAILED", style="bold red")
        )
        table.add_row(
            invoice.invoice_number,
            invoice.vendor_name,
            f"{invoice.currency} {invoice.total_amount:,.2f}",
            status_text,
            str(len(result.flags)),
        )

    console.print(table)


def print_audit_flags(console: Console, result: AuditResult) -> None:
    table = Table(show_header=True, header_style="bold yellow")
    table.add_column("Field")
    table.add_column("Expected")
    table.add_column("Actual")
    table.add_column("Severity", justify="center")

    for flag in result.flags:
        severity_text = (
            Text("warning", style="bold yellow")
            if flag.severity == "warning"
            else Text("error", style="bold red")
        )
        table.add_row(flag.field, flag.expected, flag.actual, severity_text)

    console.print(
        Panel(
            table,
            title=f"[bold yellow]AUDIT FLAGS — {result.invoice.invoice_number}[/bold yellow]",
            border_style="yellow",
        )
    )


def print_dispute_email(console: Console, email: DisputeEmail) -> None:
    title = f"DISPUTE EMAIL — {email.invoice_number}"
    if email.urgent_flag:
        title = f"[URGENT — ALREADY PAID] {title}"

    content = (
        f"[bold]To:[/bold]      {email.recipient}\n"
        f"[bold]Subject:[/bold] {email.subject}\n\n"
        f"{email.body}"
    )

    border = "red" if email.urgent_flag else "blue"
    title_markup = (
        f"[bold red]{title}[/bold red]"
        if email.urgent_flag
        else f"[bold blue]{title}[/bold blue]"
    )

    console.print(Panel(content, title=title_markup, border_style=border))


def print_collections_table(
    console: Console,
    results: list[dict],
    delinquent_df: pd.DataFrame,
) -> None:
    active_clients = {r["client_name"]: r for r in results}

    table = Table(
        title="Collections Queue",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Client")
    table.add_column("Amount Due", justify="right")
    table.add_column("Days Overdue", justify="right")
    table.add_column("Tier")
    table.add_column("Status", justify="center")

    for _, row in delinquent_df.iterrows():
        client_name = str(row["client_name"])
        days_overdue = int(row["days_overdue"])
        amount_due = float(row["amount_due"])

        if client_name in active_clients:
            tier = active_clients[client_name]["escalation_tier"]
            status = Text("ACTIVE", style="bold green")
        else:
            tier = _tier_label(days_overdue)
            status = Text("SNOOZED", style="bold yellow")

        table.add_row(
            client_name,
            f"${amount_due:,.2f}",
            str(days_overdue),
            tier,
            status,
        )

    console.print(table)


def print_collections_email(console: Console, result: dict) -> None:
    email: CollectionsEmail = result["email"]
    tier: str = result["escalation_tier"]

    tier_colors = {
        "firm_reminder": "cyan",
        "legal_notice": "yellow",
        "final_demand": "red",
    }
    color = tier_colors.get(tier, "white")

    content = (
        f"[bold]To:[/bold]      {email.recipient}\n"
        f"[bold]Subject:[/bold] {email.subject}\n\n"
        f"{email.body}"
    )

    console.print(
        Panel(
            content,
            title=f"[bold {color}]COLLECTIONS — {result['client_name']} [{tier.upper()}][/bold {color}]",
            border_style=color,
        )
    )


def print_simulation_result(console: Console, sim_result: dict) -> None:
    parsed: ParsedReply = sim_result["parsed_reply"]
    snooze: SnoozeEntry | None = sim_result["snooze_entry"]
    confirmation: str | None = sim_result["confirmation_email"]

    reply_content = (
        f"[bold]Intent:[/bold]       {parsed.intent}\n"
        f"[bold]Promise Date:[/bold] {parsed.promise_date or 'N/A'}\n"
        f"[bold]Summary:[/bold]      {parsed.parsed_summary}"
    )
    console.print(
        Panel(reply_content, title="[bold cyan]PARSED REPLY[/bold cyan]", border_style="cyan")
    )

    if snooze is not None:
        snooze_content = (
            f"[bold]Client:[/bold]      {snooze.client_name}\n"
            f"[bold]Snooze Until:[/bold] {snooze.snooze_until}\n"
            f"[bold]Reason:[/bold]      {snooze.reason}\n"
            f"[bold]Created At:[/bold]  {snooze.created_at}"
        )
    else:
        snooze_content = "[dim]No snooze entry created.[/dim]"

    console.print(
        Panel(
            snooze_content,
            title="[bold yellow]SNOOZE LOGGED[/bold yellow]",
            border_style="yellow",
        )
    )

    console.print(
        Panel(
            confirmation if confirmation is not None else "[dim]No confirmation email drafted.[/dim]",
            title="[bold green]CONFIRMATION EMAIL[/bold green]",
            border_style="green",
        )
    )


def print_summary(
    console: Console,
    total_invoices: int,
    total_flags: int,
    dispute_count: int,
    collections_count: int,
    snoozed_count: int,
) -> None:
    usage = get_usage_summary()

    content = (
        f"[bold]Total invoices audited:[/bold]        {total_invoices}\n"
        f"[bold]Total flags found:[/bold]             {total_flags}\n"
        f"[bold]Dispute emails drafted:[/bold]        {dispute_count}\n"
        f"[bold]Collections clients processed:[/bold] {collections_count}\n"
        f"[bold]Clients snoozed:[/bold]               {snoozed_count}\n\n"
        f"[bold]Prompt tokens used:[/bold]            {usage['prompt_tokens']:,}\n"
        f"[bold]Completion tokens used:[/bold]        {usage['completion_tokens']:,}\n"
        f"[bold]Estimated cost:[/bold]                ${usage['estimated_cost_usd']:.6f} USD"
    )

    console.print(
        Panel(
            content,
            title="[bold green]RUN COMPLETE[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ── Private helper ────────────────────────────────────────────────────────────


def _tier_label(days_overdue: int) -> str:
    if days_overdue < 30:
        return "firm_reminder"
    if days_overdue < 60:
        return "legal_notice"
    return "final_demand"


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """
    Main orchestration function for the LedgerShield pipeline.
    Runs all three phases sequentially with rich console output.
    """
    console = Console()
    OUTPUT_DIR.mkdir(exist_ok=True)

    print_banner(console)

    # Phase 1: Invoice Audit
    console.rule("PHASE 1 — INVOICE AUDIT")
    audit_results = audit_all_invoices(INVOICES_DIR, CONTRACTS_DIR)
    print_audit_summary(console, audit_results)

    dispute_emails: list[DisputeEmail] = []
    for result in audit_results:
        if not result.passed:
            print_audit_flags(console, result)
            email = run_recovery(
                result,
                BANK_LEDGER if BANK_LEDGER.exists() else None,
                OUTPUT_DIR,
            )
            if email:
                dispute_emails.append(email)
                print_dispute_email(console, email)

    # Phase 2: Collections
    console.rule("PHASE 2 — COLLECTIONS QUEUE")
    collections_results = run_collections(
        AR_LEDGER     if AR_LEDGER.exists()     else None,
        EMAIL_HISTORY if EMAIL_HISTORY.exists() else None,
    )
    delinquent_df = get_delinquent_clients(AR_LEDGER if AR_LEDGER.exists() else None)
    print_collections_table(console, collections_results, delinquent_df)
    for result in collections_results:
        print_collections_email(console, result)

    # Phase 3: Reply Simulation
    console.rule("PHASE 3 — REPLY SIMULATION")
    sim_result = simulate_incoming_reply(
        client_name="Orion Dynamics",
        email_text=(
            "Hi, sorry for the delay. I can get this paid by next Friday. "
            "We've had some internal approvals holding things up but it's "
            "all sorted now. Apologies again."
        ),
        history_path=EMAIL_HISTORY,
    )
    print_simulation_result(console, sim_result)

    # Summary
    console.rule("SUMMARY")
    total_flags = sum(len(r.flags) for r in audit_results)
    snoozed_count = max(len(delinquent_df) - len(collections_results), 0)
    print_summary(
        console,
        total_invoices=len(audit_results),
        total_flags=total_flags,
        dispute_count=len(dispute_emails),
        collections_count=len(collections_results),
        snoozed_count=snoozed_count,
    )


if __name__ == "__main__":
    main()
