"""Invoice extraction and contract audit engine."""

import json
import logging
from pathlib import Path

from pydantic import BaseModel

from llm_client import extract_structured
from models.invoice import AuditFlag, AuditResult, ExtractedInvoice
from utils.contract_matcher import match_vendor_to_contract
from utils.file_loader import load_invoice_file, load_text_file

logger = logging.getLogger(__name__)


def extract_invoice(file_path: Path) -> ExtractedInvoice:
    """
    Loads an invoice file and uses gpt-4o-mini to extract structured data.

    Args:
        file_path: Path to the invoice file (.txt, .md, .csv, or .pdf)

    Returns:
        ExtractedInvoice: Structured invoice data extracted by the LLM.

    Raises:
        ValueError: If the file type is not supported.
        FileNotFoundError: If the file does not exist.
    """
    content = load_invoice_file(file_path)

    prompt = f"""You are an expert accounts-payable clerk. Extract every field
from the invoice below and return them in the requested JSON structure.

Rules:
- Preserve the exact vendor name as it appears on the invoice.
- Parse all line items, including description, quantity, unit_price, and
  line_total.
- subtotal is the sum of all line_item totals before tax.
- total_amount is the final amount due (subtotal + tax).
- If a billing contact email is present anywhere on the invoice, include it;
  otherwise leave it as an empty string.
- Dates should be formatted as YYYY-MM-DD where possible.
- currency defaults to USD unless explicitly stated otherwise.

INVOICE TEXT:
{content}
"""

    invoice = extract_structured(
        prompt=prompt,
        response_model=ExtractedInvoice,
        model="gpt-4o-mini",
        feature_name="invoice_extraction",
    )

    logger.info(
        "Extracted invoice vendor='%s' invoice_number='%s'",
        invoice.vendor_name,
        invoice.invoice_number,
    )

    return invoice


def compare_invoice_to_contract(
    invoice: ExtractedInvoice,
    contract_path: Path,
) -> list[AuditFlag]:
    """
    Uses gpt-4o to compare an extracted invoice against contract terms.

    Args:
        invoice: The structured invoice data to audit.
        contract_path: Path to the matching vendor contract file.

    Returns:
        list[AuditFlag]: All discrepancies found. Empty list if clean.
    """

    class AuditFlagList(BaseModel):
        flags: list[AuditFlag]

    contract_text = load_text_file(contract_path)
    invoice_json = json.dumps(invoice.model_dump(), indent=2)

    prompt = f"""You are a senior accounts-payable auditor. Your job is to
compare the invoice below against the vendor contract and identify every
discrepancy.

Check ALL of the following — be exhaustive:

1. UNIT PRICES: Verify each line item's unit_price does not exceed the price
   ceiling specified in the contract for that service/product category.

2. PERMITTED ITEMS: Verify each line item category is explicitly permitted by
   the contract. Flag any item that is not covered.

3. ARITHMETIC VERIFICATION (re-compute independently):
   - For every line item: quantity × unit_price must equal line_total.
   - Sum of all line_totals must equal subtotal.
   - subtotal + tax must equal total_amount.
   Flag any discrepancy, even a penny.

4. TAX RATE: Verify the tax is consistent with any tax rate or tax cap stated
   in the contract. Flag if the applied rate differs.

For each discrepancy found, produce one flag object with:
  - field: the invoice field or line item description that is wrong
  - expected: what the contract or correct arithmetic requires (be specific,
    include dollar amounts or rates)
  - actual: what the invoice actually shows
  - severity: "error" for overcharges or unpermitted items; "warning" for
    minor arithmetic rounding or unlisted-but-plausible items
  - description: a concise one-sentence explanation of the violation

Return an empty list [] in the "flags" array if the invoice is fully compliant.

CONTRACT:
{contract_text}

INVOICE (JSON):
{invoice_json}
"""

    result = extract_structured(
        prompt=prompt,
        response_model=AuditFlagList,
        model="gpt-4o",
        feature_name="contract_comparison",
    )

    logger.info(
        "Audit complete for invoice '%s': %d flag(s) found",
        invoice.invoice_number,
        len(result.flags),
    )

    return result.flags


def audit_invoice(
    file_path: Path,
    contracts_dir: Path,
) -> AuditResult:
    """
    Full audit pipeline for a single invoice file.

    Args:
        file_path: Path to the invoice file to audit.
        contracts_dir: Path to the directory containing contract files.

    Returns:
        AuditResult: Complete audit outcome including flags and payment status.
    """
    extracted_invoice = extract_invoice(file_path)

    match = match_vendor_to_contract(
        vendor_name=extracted_invoice.vendor_name,
        contracts_dir=contracts_dir,
    )

    if match is None:
        logger.warning(
            "No contract found for vendor '%s' — skipping contract comparison",
            extracted_invoice.vendor_name,
        )
        return AuditResult(
            invoice=extracted_invoice,
            flags=[],
            passed=True,
            contract_file_used="NO CONTRACT FOUND",
        )

    contract_path, confidence = match
    logger.info(
        "Matched vendor '%s' to contract '%s' (confidence=%d)",
        extracted_invoice.vendor_name,
        contract_path.name,
        confidence,
    )

    flags = compare_invoice_to_contract(extracted_invoice, contract_path)

    return AuditResult(
        invoice=extracted_invoice,
        flags=flags,
        passed=len(flags) == 0,
        contract_file_used=str(contract_path),
    )


def audit_all_invoices(
    invoices_dir: Path,
    contracts_dir: Path,
) -> list[AuditResult]:
    """
    Processes every invoice file in the invoices directory.

    Args:
        invoices_dir: Path to directory containing invoice files.
        contracts_dir: Path to directory containing contract files.

    Returns:
        list[AuditResult]: Audit results for all processed invoices.
    """
    results: list[AuditResult] = []

    for file_path in invoices_dir.iterdir():
        if file_path.name.startswith(".") or file_path.is_dir():
            continue
        logger.info("Auditing invoice file: %s", file_path.name)
        result = audit_invoice(file_path, contracts_dir)
        results.append(result)

    logger.info("Audit complete: %d invoice(s) processed", len(results))
    return results
