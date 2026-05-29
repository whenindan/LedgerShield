"""Collections pipeline: delinquency detection and email drafting."""

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from llm_client import extract_structured, generate_text
from models.collections import CollectionsEmail, ParsedReply, SnoozeEntry
from utils.file_loader import load_csv_as_dataframe, load_json_file
from utils.snooze_store import add_snooze_entry, is_snoozed

logger = logging.getLogger(__name__)


def get_delinquent_clients(ar_path: Path | None) -> pd.DataFrame:
    """
    Loads AR ledger and filters for clients more than 14 days overdue.

    Args:
        ar_path: Path to accounts_receivable.csv.

    Returns:
        pd.DataFrame: Filtered rows where days_overdue > 14.
    """
    if ar_path is None:
        logger.info("No AR ledger uploaded — returning empty delinquent list")
        return pd.DataFrame()
    df = load_csv_as_dataframe(ar_path)
    delinquent = df[df["days_overdue"] > 14].copy()
    logger.info("Found %d delinquent client(s) with >14 days overdue", len(delinquent))
    return delinquent


def get_escalation_tier(days_overdue: int) -> str:
    """
    Determines collections escalation tier based on days overdue.

    Args:
        days_overdue: Number of days the invoice is past due.

    Returns:
        str: One of 'firm_reminder', 'legal_notice', 'final_demand'.
    """
    if days_overdue < 30:
        return "firm_reminder"
    if days_overdue < 60:
        return "legal_notice"
    return "final_demand"


def get_client_email_history(client_name: str, history_path: Path | None) -> list[dict]:
    """
    Retrieves prior email thread for a specific client.

    Args:
        client_name: Name of the client to look up.
        history_path: Path to client_email_history.json.

    Returns:
        list[dict]: List of email objects, empty list if client not found.
    """
    if history_path is None:
        logger.info("No email history uploaded — returning empty history for '%s'", client_name)
        return []
    history = load_json_file(history_path)
    if client_name not in history:
        logger.warning("No email history found for client '%s'", client_name)
        return []
    return history[client_name]


def draft_collections_email(
    client_name: str,
    amount_due: float,
    days_overdue: int,
    contact_email: str,
    email_history: list[dict],
    escalation_tier: str,
) -> CollectionsEmail:
    """
    Uses gpt-4o-mini to draft an escalation-appropriate collections email.

    Args:
        client_name: Name of the delinquent client.
        amount_due: Outstanding balance in USD.
        days_overdue: Number of days past due date.
        contact_email: Client's billing contact email address.
        email_history: Prior email exchanges with the client.
        escalation_tier: One of 'firm_reminder', 'legal_notice', 'final_demand'.

    Returns:
        CollectionsEmail: Structured collections email ready to send.
    """
    tone_instructions = {
        "firm_reminder": (
            "Use a professional and direct tone. Reference the client's payment terms explicitly. "
            "Assume good faith — treat the overdue balance as an oversight — but be clear and "
            "unambiguous about the urgency and expectation for immediate payment."
        ),
        "legal_notice": (
            "Use a strict, formal tone. Explicitly reference the contract enforcement clauses "
            "and state the specific consequences of continued non-payment, including suspension "
            "of services and potential legal action."
        ),
        "final_demand": (
            "Use a severe and urgent tone. State plainly that the account will be referred to a "
            "third-party collections agency within 5 business days if the outstanding balance is "
            "not paid in full. This is the final communication before external escalation."
        ),
    }

    if email_history:
        formatted_msgs = []
        for msg in email_history:
            formatted_msgs.append(
                f"  Date: {msg.get('date', 'unknown')}\n"
                f"  From: {msg.get('from', '')}\n"
                f"  To: {msg.get('to', '')}\n"
                f"  Subject: {msg.get('subject', '')}\n"
                f"  Body:\n{msg.get('body', '')}"
            )
        history_text = "\n\n---\n\n".join(formatted_msgs)
    else:
        history_text = "(No prior email history on file.)"

    prompt = f"""You are an accounts receivable specialist drafting a collections email on behalf of ACASO Inc.

CLIENT DETAILS:
  Client Name   : {client_name}
  Amount Due    : ${amount_due:,.2f} USD
  Days Overdue  : {days_overdue} days
  Contact Email : {contact_email}

ESCALATION TIER: {escalation_tier.upper()}
TONE INSTRUCTIONS: {tone_instructions[escalation_tier]}

PRIOR EMAIL HISTORY (oldest first):
{history_text}

INSTRUCTIONS:
Draft a collections email appropriate for the escalation tier. The email must:
1. Open with a clear, direct reference to the outstanding invoice and amount.
2. Reference prior communications where relevant.
3. State a specific payment deadline (within 7 days).
4. Apply the tone described above without deviation.
5. Include a professional sign-off from the ACASO AR team.

Fill all fields of the CollectionsEmail model:
  - recipient: the client's billing contact email ({contact_email})
  - client_name: exactly "{client_name}"
  - subject: a concise subject line referencing the overdue balance
  - body: the complete email body text
  - escalation_tier: exactly "{escalation_tier}"
"""

    return extract_structured(
        prompt=prompt,
        response_model=CollectionsEmail,
        model="gpt-4o-mini",
        feature_name="collections_email_drafting",
    )


def simulate_incoming_reply(
    client_name: str,
    email_text: str,
    history_path: Path,
) -> dict:
    """
    Simulates receiving a client reply and triggers adaptive response logic.

    Parses the client's reply semantically, determines intent, logs a snooze
    if a payment promise is made, and drafts a confirmation email.

    Args:
        client_name: Name of the client who sent the reply.
        email_text: Raw text of the client's reply email.
        history_path: Path to client_email_history.json for context.

    Returns:
        dict with keys: parsed_reply, snooze_entry, confirmation_email
        (snooze_entry and confirmation_email are None if no promise made)
    """
    today = date.today()
    today_str = today.isoformat()

    parse_prompt = f"""You are an accounts receivable assistant parsing an inbound client reply.

TODAY'S DATE: {today_str}

CLIENT NAME: {client_name}

INCOMING EMAIL TEXT:
{email_text}

INSTRUCTIONS:
1. Determine the intent of this email. Choose exactly one of:
   - "promise_to_pay": the client explicitly commits to paying by a specific date
   - "dispute": the client is contesting the invoice amount or charges
   - "ignore": the client is not engaging meaningfully
   - "paid": the client claims they have already paid
   - "other": anything else

2. If the intent is "promise_to_pay", extract the payment promise date.
   - If the date is expressed as a relative term (e.g. "next Friday", "end of next week",
     "by Thursday"), resolve it to an absolute date in YYYY-MM-DD format using
     TODAY'S DATE ({today_str}) as the reference point.
   - If no specific date is mentioned, set promise_date to null.

3. Write a brief parsed_summary (1-2 sentences) describing the client's message
   and its implications for the outstanding invoice.

Return your result in the requested JSON structure.
"""

    parsed_reply: ParsedReply = extract_structured(
        prompt=parse_prompt,
        response_model=ParsedReply,
        model="gpt-4o-mini",
        feature_name="reply_parsing",
    )

    logger.info(
        "Parsed reply from '%s': intent='%s' promise_date=%s",
        client_name,
        parsed_reply.intent,
        parsed_reply.promise_date,
    )

    if parsed_reply.intent == "promise_to_pay" and parsed_reply.promise_date is not None:
        promise_date = date.fromisoformat(parsed_reply.promise_date)
        snooze_until = (promise_date + timedelta(days=2)).isoformat()

        snooze_entry = SnoozeEntry(
            client_name=client_name,
            snooze_until=snooze_until,
            reason=f"Client promised payment by {parsed_reply.promise_date}",
            created_at=today_str,
        )
        add_snooze_entry(snooze_entry)

        confirm_prompt = f"""You are an accounts receivable specialist sending a brief acknowledgment email.

CLIENT NAME: {client_name}
PROMISED PAYMENT DATE: {parsed_reply.promise_date}

Write a short confirmation email body (3-4 sentences maximum) that:
1. Warmly but professionally acknowledges the client's commitment to pay by {parsed_reply.promise_date}.
2. Clearly states that if payment is not received by that date, the account will be escalated for further action.
3. Thanks the client for their prompt communication.

Return only the email body text — no subject line, no metadata, no salutation header.
"""

        confirmation_email = generate_text(
            prompt=confirm_prompt,
            model="gpt-4o-mini",
            feature_name="confirmation_email_drafting",
        )

        return {
            "parsed_reply": parsed_reply,
            "snooze_entry": snooze_entry,
            "confirmation_email": confirmation_email,
        }

    return {
        "parsed_reply": parsed_reply,
        "snooze_entry": None,
        "confirmation_email": None,
    }


def run_collections(ar_path: Path | None, history_path: Path | None) -> list[dict]:
    """
    Orchestrates the full collections flow for all delinquent clients.

    Args:
        ar_path: Path to accounts_receivable.csv.
        history_path: Path to client_email_history.json.

    Returns:
        list[dict]: Collection results per client with email and snooze status.
    """
    if ar_path is None:
        logger.info("No AR ledger in session — skipping collections phase")
        return []
    delinquent = get_delinquent_clients(ar_path)
    results: list[dict] = []

    for _, row in delinquent.iterrows():
        client_name = str(row["client_name"])
        days_overdue = int(row["days_overdue"])

        if is_snoozed(client_name):
            logger.info("Skipping snoozed client: '%s'", client_name)
            continue

        escalation_tier = get_escalation_tier(days_overdue)
        email_history = get_client_email_history(client_name, history_path)

        email = draft_collections_email(
            client_name=client_name,
            amount_due=float(row["amount_due"]),
            days_overdue=days_overdue,
            contact_email=str(row["contact_email"]),
            email_history=email_history,
            escalation_tier=escalation_tier,
        )

        results.append(
            {
                "client_name": client_name,
                "escalation_tier": escalation_tier,
                "email": email,
                "snoozed": False,
            }
        )

    return results
