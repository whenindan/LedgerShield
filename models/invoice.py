"""Pydantic models for invoice extraction and audit results."""

from typing import Literal
from pydantic import BaseModel


class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float
    line_total: float


class ExtractedInvoice(BaseModel):
    vendor_name: str
    invoice_number: str
    invoice_date: str
    due_date: str
    line_items: list[LineItem]
    subtotal: float
    tax: float
    total_amount: float
    currency: str = "USD"
    billing_contact_email: str = ""


class AuditFlag(BaseModel):
    field: str
    expected: str
    actual: str
    severity: Literal["warning", "error"]
    description: str


class AuditResult(BaseModel):
    invoice: ExtractedInvoice
    flags: list[AuditFlag]
    passed: bool
    contract_file_used: str
    already_paid: bool = False
