"""Pydantic models for collections emails, reply parsing, and snooze entries."""

from typing import Literal
from pydantic import BaseModel


class CollectionsEmail(BaseModel):
    recipient: str
    client_name: str
    subject: str
    body: str
    escalation_tier: Literal["firm_reminder", "legal_notice", "final_demand"]


class ParsedReply(BaseModel):
    intent: Literal["promise_to_pay", "dispute", "ignore", "paid", "other"]
    promise_date: str | None = None
    parsed_summary: str


class SnoozeEntry(BaseModel):
    client_name: str
    snooze_until: str
    reason: str
    created_at: str
