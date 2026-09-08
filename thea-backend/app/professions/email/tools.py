"""
`email` profession — search, draft, send, and (rarely) delete email.

`email.delete_email`'s schema is the handoff doc's own concrete example:
"a delete_email tool's schema can require an explicit, narrowly-typed
confirmation field that's structurally hard to satisfy by accident." Here
that's `confirm_delete: Literal[True]` — no default, only the literal
boolean `true` validates, so a plan step missing or misspelling it fails
Stage 2's schema validation before this handler ever runs.

Provider access is behind a small `EmailClient` protocol so a concrete
provider (Gmail API, Microsoft Graph, plain IMAP/SMTP) is swappable in
`_get_client()` without touching schemas or the registry. Only
`send_email` is wired to a real (SMTP) implementation today —
`search_inbox` and `delete_email` need an IMAP or provider-API client and
currently return a clear "not wired up yet" failure rather than doing
nothing silently.

error_type choices (see app/stages/action.py's _RETRYABLE_ERROR_TYPES /
_TERMINAL_ERROR_TYPES):
  - "not_configured" — SMTP credentials aren't set. Not fixable by
    adjusting args; needs an operator/config change.
  - "not_implemented" — search_inbox/delete_email have no client wired up
    yet. Same reasoning as web_search's stub in app/tools/shared/web_search.py.
  - "auth_failure"    — SMTP login rejected. Not fixable by retrying with
    different message args.
  - unset (defaults to "transient")   — network/connection issues at the
    SMTP layer. Worth an identical retry (attempt 1) before giving up.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Literal, Protocol

from pydantic import BaseModel, EmailStr, Field

from app.config import get_settings
from app.models import ToolExecutionResult
from app.tools.registry import register_tool


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class EmailClient(Protocol):
    def search(self, query: str, folder: str, max_results: int) -> list[dict]: ...

    def send(self, to: list[str], subject: str, body: str, cc: list[str], bcc: list[str]) -> str:
        """Returns the provider's message id for the sent message."""
        ...

    def delete(self, message_id: str) -> None: ...


class SMTPEmailClient:
    """SMTP-only: covers `send`. `search`/`delete` need an IMAP or
    provider-API client this class doesn't open."""

    def __init__(self) -> None:
        settings = get_settings()
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_address = settings.smtp_from_address or settings.smtp_username

    def _require_config(self) -> None:
        if not all([self.host, self.username, self.password, self.from_address]):
            raise _NotConfiguredError(
                "SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_ADDRESS "
                "must all be set in the environment before send_email can run."
            )

    def search(self, query: str, folder: str, max_results: int) -> list[dict]:
        raise NotImplementedError("search_inbox needs an IMAP or provider-API client — not wired up yet.")

    def send(self, to: list[str], subject: str, body: str, cc: list[str], bcc: list[str]) -> str:
        self._require_config()
        msg = MIMEMultipart()
        msg["From"] = self.from_address
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.attach(MIMEText(body, "plain"))

        all_recipients = list(to) + list(cc) + list(bcc)
        with smtplib.SMTP(self.host, self.port) as server:
            server.starttls()
            server.login(self.username, self.password)
            server.sendmail(self.from_address, all_recipients, msg.as_string())

        return msg.get("Message-Id", "unknown")

    def delete(self, message_id: str) -> None:
        raise NotImplementedError("delete_email needs an IMAP or provider-API client — not wired up yet.")


class _NotConfiguredError(Exception):
    pass


def _get_client() -> EmailClient:
    return SMTPEmailClient()


# ---------------------------------------------------------------------------
# search_inbox
# ---------------------------------------------------------------------------


class SearchInboxArgs(BaseModel):
    query: str
    folder: str = "inbox"
    max_results: int = Field(default=10, ge=1, le=50)


def _search_inbox_sync(args: SearchInboxArgs) -> ToolExecutionResult:
    try:
        results = _get_client().search(args.query, args.folder, args.max_results)
    except NotImplementedError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_implemented")
    except Exception as exc:
        return ToolExecutionResult(success=False, error=f"search_inbox failed: {exc}")
    return ToolExecutionResult(success=True, result={"query": args.query, "results": results})


async def _search_inbox(args: SearchInboxArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_search_inbox_sync, args)


# ---------------------------------------------------------------------------
# draft_email — pure, no side effects
# ---------------------------------------------------------------------------


class DraftEmailArgs(BaseModel):
    to: list[EmailStr]
    subject: str
    body: str
    cc: list[EmailStr] = Field(default_factory=list)


async def _draft_email(args: DraftEmailArgs) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=True,
        result={"to": [str(a) for a in args.to], "cc": [str(a) for a in args.cc], "subject": args.subject, "body": args.body},
    )


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class SendEmailArgs(BaseModel):
    to: list[EmailStr]
    subject: str
    body: str
    cc: list[EmailStr] = Field(default_factory=list)
    bcc: list[EmailStr] = Field(default_factory=list)


def _send_email_sync(args: SendEmailArgs) -> ToolExecutionResult:
    try:
        message_id = _get_client().send(
            to=[str(a) for a in args.to],
            subject=args.subject,
            body=args.body,
            cc=[str(a) for a in args.cc],
            bcc=[str(a) for a in args.bcc],
        )
    except _NotConfiguredError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_configured")
    except smtplib.SMTPAuthenticationError as exc:
        return ToolExecutionResult(success=False, error=f"SMTP auth failed: {exc}", error_type="auth_failure")
    except smtplib.SMTPException as exc:
        return ToolExecutionResult(success=False, error=f"SMTP error: {exc}")  # transient by default

    return ToolExecutionResult(success=True, result={"message_id": message_id, "to": [str(a) for a in args.to]})


async def _send_email(args: SendEmailArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_send_email_sync, args)


# ---------------------------------------------------------------------------
# delete_email — narrowly-typed confirmation field
# ---------------------------------------------------------------------------


class DeleteEmailArgs(BaseModel):
    message_id: str
    confirm_delete: Literal[True] = Field(
        ...,
        description=(
            "Must be the literal boolean true. No default and no other "
            "accepted value — PLAN must explicitly assert intent to "
            "delete for this step to pass schema validation at all."
        ),
    )


def _delete_email_sync(args: DeleteEmailArgs) -> ToolExecutionResult:
    try:
        _get_client().delete(args.message_id)
    except NotImplementedError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_implemented")
    except Exception as exc:
        return ToolExecutionResult(success=False, error=f"delete_email failed: {exc}")
    return ToolExecutionResult(success=True, result={"deleted_message_id": args.message_id})


async def _delete_email(args: DeleteEmailArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_delete_email_sync, args)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_tool(
    name="email.search_inbox",
    args_schema=SearchInboxArgs,
    handler=_search_inbox,
    description="Search email in a folder by query, returning matching messages.",
    skill_doc=(
        "email.search_inbox(query, folder='inbox', max_results=10). "
        "Currently fails with error_type=not_implemented — no IMAP/"
        "provider-API client wired up yet. Not retryable until that "
        "changes."
    ),
)

register_tool(
    name="email.draft_email",
    args_schema=DraftEmailArgs,
    handler=_draft_email,
    description="Compose an email draft without sending it.",
    skill_doc=(
        "email.draft_email(to, subject, body, cc=[]) -> the composed "
        "draft. Pure — no side effects, safe to retry freely."
    ),
)

register_tool(
    name="email.send_email",
    args_schema=SendEmailArgs,
    handler=_send_email,
    description="Send an email immediately.",
    skill_doc=(
        "email.send_email(to, subject, body, cc=[], bcc=[]). Requires "
        "SMTP_HOST/PORT/USERNAME/PASSWORD/FROM_ADDRESS in the environment "
        "— missing config fails with error_type=not_configured (not "
        "retryable). This is a non-idempotent, side-effecting call from "
        "the recipient's point of view — never treat a failure that "
        "happened AFTER the message actually left the SMTP server as "
        "retryable."
    ),
)

register_tool(
    name="email.delete_email",
    args_schema=DeleteEmailArgs,
    handler=_delete_email,
    description="Permanently delete a single email by message id. Destructive — requires explicit confirmation.",
    skill_doc=(
        "email.delete_email(message_id, confirm_delete: true). "
        "confirm_delete has no default and only accepts literal true. "
        "Currently fails with error_type=not_implemented — no IMAP/"
        "provider-API client wired up yet."
    ),
)
