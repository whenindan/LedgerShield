"""Recovery pipeline: payment detection and dispute email drafting."""

import json
import logging
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from llm_client import extract_structured
from models.dispute import DisputeEmail
from models.invoice import AuditResult, ExtractedInvoice
from utils.file_loader import load_csv_as_dataframe

logger = logging.getLogger(__name__)


def check_if_paid(
    invoice: ExtractedInvoice,
    bank_ledger_path: Path,
) -> bool:
    """
    Cross-references an invoice against the bank ledger to detect auto-payment.

    Args:
        invoice: The extracted invoice to check.
        bank_ledger_path: Path to bank_ledger.csv.

    Returns:
        bool: True if the invoice appears to have been paid already.
    """
    df = load_csv_as_dataframe(bank_ledger_path)

    invoice_number_lower = invoice.invoice_number.lower()

    for _, row in df.iterrows():
        description = str(row.get("description", "")).lower()
        if invoice_number_lower in description:
            logger.info(
                "Payment detected for invoice '%s' via description match (row: %s)",
                invoice.invoice_number,
                row.get("transaction_id", "unknown"),
            )
            return True

        try:
            row_amount = float(row.get("amount", -1))
        except (ValueError, TypeError):
            row_amount = -1.0

        amount_match = abs(row_amount - invoice.total_amount) <= 0.01

        vendor_score = fuzz.WRatio(
            str(row.get("vendor", "")),
            invoice.vendor_name,
        )

        if amount_match and vendor_score > 80:
            logger.info(
                "Payment detected for invoice '%s' via amount+vendor match "
                "(row: %s, vendor_score=%d)",
                invoice.invoice_number,
                row.get("transaction_id", "unknown"),
                vendor_score,
            )
            return True

    logger.info(
        "No payment found in ledger for invoice '%s'", invoice.invoice_number
    )
    return False


def draft_dispute_email(
    audit_result: AuditResult,
) -> DisputeEmail:
    """
    Uses gpt-4o-mini to draft a formal vendor dispute email.

    Args:
        audit_result: The complete audit result containing flags and invoice.

    Returns:
        DisputeEmail: Structured dispute email ready to send.
    """
    invoice = audit_result.invoice
    already_paid = audit_result.already_paid

    flags_text = "\n".join(
        f"  - Field: {f.field}\n"
        f"    Expected: {f.expected}\n"
        f"    Actual: {f.actual}\n"
        f"    Severity: {f.severity}\n"
        f"    Detail: {f.description}"
        for f in audit_result.flags
    )

    payment_context = (
        "IMPORTANT: This invoice has ALREADY BEEN PAID via auto-pay. "
        "The tone must be urgent. Demand an immediate refund of the "
        "overcharged amount."
        if already_paid
        else "This invoice has NOT yet been paid. Request a corrected invoice "
        "or credit memo before any payment is released."
    )

    prompt = f"""You are a senior finance manager writing a formal dispute
letter to a vendor on behalf of your company.

INVOICE DETAILS:
  Invoice Number : {invoice.invoice_number}
  Vendor Name    : {invoice.vendor_name}
  Billing Contact: {invoice.billing_contact_email or "(no email on file)"}
  Invoice Date   : {invoice.invoice_date}
  Total Billed   : {invoice.currency} {invoice.total_amount:.2f}

DISCREPANCIES FOUND:
{flags_text}

PAYMENT STATUS:
{payment_context}

INSTRUCTIONS:
Write a professional, firm dispute letter that:
1. Opens with a clear statement of the dispute and the invoice reference.
2. Provides a line-by-line breakdown of each overcharge with exact dollar
   amounts (expected vs. actual).
3. Cites the specific contract clause, section, or pricing term that is
   violated for each item.
4. Requests a credit memo or full refund of the overcharged amount within
   14 business days.
5. If already_paid is True: lead with urgency — state that payment was
   processed in error and demand immediate refund.
6. Closes with a professional escalation warning (e.g., escalation to legal
   or withholding of future payments) if the matter is not resolved within
   the stated timeframe.

Fill all fields of the DisputeEmail model:
  - recipient: the billing contact email (use {invoice.billing_contact_email}
    or "billing@{invoice.vendor_name.lower().replace(' ', '')}.com" if blank)
  - subject: concise subject line referencing invoice number and dispute
  - body: the complete letter text
  - invoice_number: {invoice.invoice_number}
  - urgent_flag: true if already_paid is True or any flag has severity "error",
    otherwise false
"""

    dispute = extract_structured(
        prompt=prompt,
        response_model=DisputeEmail,
        model="gpt-4o-mini",
        feature_name="dispute_email_drafting",
    )

    return dispute


def run_recovery(
    audit_result: AuditResult,
    bank_ledger_path: Optional[Path],
    output_dir: Path,
) -> DisputeEmail | None:
    """
    Orchestrates the full recovery flow for a flagged invoice.

    Args:
        audit_result: The audit result to act on.
        bank_ledger_path: Path to bank_ledger.csv for payment check, or None
                          to skip the payment check (assumes not yet paid).
        output_dir: Directory to write dispute email JSON output.

    Returns:
        DisputeEmail if recovery was triggered, None if invoice passed audit.
    """
    if audit_result.passed:
        logger.info(
            "Invoice '%s' passed — no recovery needed",
            audit_result.invoice.invoice_number,
        )
        return None

    if bank_ledger_path is not None and bank_ledger_path.exists():
        audit_result.already_paid = check_if_paid(
            invoice=audit_result.invoice,
            bank_ledger_path=bank_ledger_path,
        )
    else:
        audit_result.already_paid = False

    dispute = draft_dispute_email(audit_result)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"dispute_{audit_result.invoice.invoice_number}.json"
    output_file.write_text(
        json.dumps(dispute.model_dump(), indent=2),
        encoding="utf-8",
    )

    logger.info("Dispute email written to '%s'", output_file)

    return dispute
