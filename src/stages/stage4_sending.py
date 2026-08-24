import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, Optional, List
from src.stages.base import BaseStage
from src.models import StageStatus

import requests

logger = logging.getLogger(__name__)


class EmailTransport(object):
    """Abstraction for email sending backend (Resend, SMTP, SendGrid, AWS SES, or DryRun/Mock)."""

    def __init__(self, mode: str = "dry_run", smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = "", resend_api_key: str = "",
                 resend_from_email: str = "onboarding@resend.dev"):
        self.mode = mode.lower()
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.resend_api_key = resend_api_key
        self.resend_from_email = resend_from_email

    def send_email(self, recipient: str, subject: str, body_text: str, reply_to: str) -> bool:
        if self.mode == "dry_run" or self.mode == "mock":
            logger.info(f"[DRY RUN / MOCK SEND] Would send email to: {recipient} | Subject: '{subject}'")
            return True
        elif self.mode == "resend":
            return self._send_resend(recipient, subject, body_text, reply_to)
        elif self.mode == "smtp":
            return self._send_smtp(recipient, subject, body_text, reply_to)
        else:
            logger.warning(f"Unknown transport mode '{self.mode}'. Defaulting to dry run.")
            return True

    def _send_resend(self, recipient: str, subject: str, body_text: str, reply_to: str) -> bool:
        if not self.resend_api_key:
            logger.error("Resend API key is missing.")
            return False

        url = "https://api.resend.com/emails"
        headers = {
            "Authorization": f"Bearer {self.resend_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "from": self.resend_from_email,
            "to": [recipient],
            "subject": subject,
            "text": body_text,
            "reply_to": reply_to
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Successfully sent email via Resend API to {recipient} (ID: {data.get('id')})")
            return True
        except Exception as e:
            logger.error(f"Failed to send email via Resend API to {recipient}: {e}")
            return False

    def _send_smtp(self, recipient: str, subject: str, body_text: str, reply_to: str) -> bool:
        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user or reply_to
            msg["To"] = recipient
            msg["Subject"] = subject
            msg["Reply-To"] = reply_to
            msg.attach(MIMEText(body_text, "plain", "utf-8"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                if self.smtp_user and self.smtp_pass:
                    server.login(self.smtp_user, self.smtp_pass)
                server.send_message(msg)

            logger.info(f"Successfully sent SMTP email to {recipient}")
            return True
        except Exception as e:
            logger.error(f"Failed to send SMTP email to {recipient}: {e}")
            return False


class Stage4Sending(BaseStage):
    """
    STAGE 4: Optional, Gated Email Dispatcher
    Requires manual approval (approved == 1) and explicit dry-run / send invocation.
    """

    def __init__(self, config, db, transport_mode: Optional[str] = None):
        super().__init__(config, db, stage_name="Stage4_Sending")
        mode = transport_mode or config.smtp.mode
        self.transport = EmailTransport(
            mode=mode,
            smtp_host=config.smtp.host,
            smtp_port=config.smtp.port,
            smtp_user=config.smtp.user,
            smtp_pass=config.smtp.password,
            resend_api_key=config.smtp.resend_api_key,
            resend_from_email=config.smtp.resend_from_email
        )
        self.sender_cfg = config.sender_info

    def run(self, max_items: Optional[int] = None, approved_only: bool = True) -> Dict[str, Any]:
        logger.info(f"Starting Stage 4: Email Dispatch (Mode: {self.transport.mode})...")

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT d.*, h.name as home_name FROM email_drafts d JOIN homes h ON d.home_id = h.id WHERE d.status = 'QUEUED'"
            if approved_only:
                query += " AND d.approved = 1"
            query += " ORDER BY d.id ASC"
            if max_items:
                query += f" LIMIT {int(max_items)}"
            cursor.execute(query)
            queued_drafts = [dict(r) for r in cursor.fetchall()]

        logger.info(f"Found {len(queued_drafts)} approved email drafts ready for sending.")

        sent_count = 0
        failed_count = 0
        suppressed_count = 0

        for draft in queued_drafts:
            if not self.can_process_today(custom_cap=self.config.pipeline.sending_daily_cap):
                logger.info(f"Daily sending cap reached ({self.config.pipeline.sending_daily_cap}). Pausing Stage 4.")
                break

            recipient = draft["recipient_email"]

            # Final check against PECR suppression list before dispatch
            if self.db.is_email_suppressed(recipient):
                logger.warning(f"Draft #{draft['id']} recipient {recipient} is on suppression list. Suppressing.")
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE email_drafts SET status = 'SUPPRESSED' WHERE id = ?", (draft["id"],))
                    conn.commit()
                suppressed_count += 1
                continue

            success = self.transport.send_email(
                recipient=recipient,
                subject=draft["subject"],
                body_text=draft["body_text"],
                reply_to=self.sender_cfg.reply_to
            )

            if success:
                sent_count += 1
                self.db.mark_draft_sent(draft["id"])
                audit_msg = f"Sent email draft #{draft['id']} to {recipient} (Transport: {self.transport.mode})"
                self.db.log_audit("Stage4_Sending", "EMAIL_SENT", audit_msg, home_id=draft["home_id"])
            else:
                failed_count += 1
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE email_drafts SET status = 'FAILED' WHERE id = ?", (draft["id"],))
                    conn.commit()
                audit_msg = f"Failed to send email draft #{draft['id']} to {recipient}"
                self.db.log_audit("Stage4_Sending", "SEND_FAILED", audit_msg, home_id=draft["home_id"])

        summary = {
            "processed_queued": len(queued_drafts),
            "sent": sent_count,
            "failed": failed_count,
            "suppressed": suppressed_count,
            "transport_mode": self.transport.mode
        }
        logger.info(f"Stage 4 Sending complete summary: {summary}")
        return summary
