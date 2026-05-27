"""Pydantic models for vendor dispute emails."""

from pydantic import BaseModel


class DisputeEmail(BaseModel):
    recipient: str
    subject: str
    body: str
    invoice_number: str
    urgent_flag: bool = False
