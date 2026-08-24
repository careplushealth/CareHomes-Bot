import logging
import urllib.parse
from typing import Dict, Any, Optional, List
from src.stages.base import BaseStage
from src.models import CareHome, ContactDetails, EmailDraft, StageStatus
from src.utils.gemini import GeminiClient

logger = logging.getLogger(__name__)


class Stage3Personalise(BaseStage):
    """
    STAGE 3: Email Personalisation & Review Queue Generation
    """

    def __init__(self, config, db):
        super().__init__(config, db, stage_name="Stage3_Personalise")
        self.sender_cfg = config.sender_info
        self.tmpl_cfg = config.email_template

    def run(self, max_items: Optional[int] = None) -> Dict[str, Any]:
        logger.info("Starting Stage 3: Email Personalisation...")

        pending_homes = self.db.get_homes_for_stage(StageStatus.PENDING_PERSONALISATION, limit=max_items)
        logger.info(f"Found {len(pending_homes)} homes pending email personalisation.")

        processed = 0
        drafts_created = 0
        suppressed_skipped = 0
        no_recipient_skipped = 0

        for home in pending_homes:
            contact = self.db.get_contact_for_home(home.id)

            if not contact:
                logger.warning(f"Home #{home.id} ({home.name}) has no contact record in DB. Skipping.")
                self.db.update_home_stage(home.id, StageStatus.MANUAL_REVIEW_NEEDED)
                continue

            # Determine recipient email
            recipient_email = contact.manager_email or contact.general_email

            if not recipient_email:
                msg = f"Home #{home.id} ({home.name}) has no valid email address found (only contact form: {contact.contact_form_url}). Skipping draft creation."
                logger.info(msg)
                no_recipient_skipped += 1
                self.db.update_home_stage(home.id, StageStatus.MANUAL_REVIEW_NEEDED)
                self.db.log_audit("Stage3_Personalise", "NO_RECIPIENT_EMAIL", msg, home_id=home.id)
                continue

            # Check PECR Suppression List
            if self.db.is_email_suppressed(recipient_email):
                msg = f"Recipient email {recipient_email} is on PECR suppression list. Skipping draft creation."
                logger.warning(f"Home #{home.id}: {msg}")
                suppressed_skipped += 1
                self.db.update_home_stage(home.id, StageStatus.SKIPPED)
                self.db.log_audit("Stage3_Personalise", "EMAIL_SUPPRESSED", msg, home_id=home.id)
                continue

            # Determine salutation
            if contact.manager_name:
                salutation = f"{contact.manager_name} (Care Home Manager)"
                recipient_name = contact.manager_name
            else:
                salutation = f"Care Home Manager ({home.name})"
                recipient_name = "Care Home Manager"

            # Construct unsubscribe / opt-out link
            encoded_email = urllib.parse.quote(recipient_email)
            unsubscribe_url = f"{self.sender_cfg.unsubscribe_base_url}?email={encoded_email}&id={home.id}"

            # Try generating with Gemini AI if configured
            gemini_client = GeminiClient(api_key=self.config.search_api.gemini_api_key)
            ai_email = None
            if gemini_client.is_configured():
                ai_email = gemini_client.generate_personalized_email(
                    care_home_name=home.name,
                    address=home.address,
                    postcode=home.postcode,
                    salutation=salutation,
                    sender_org=self.sender_cfg.org_name,
                    sender_name=self.sender_cfg.sender_name,
                    reply_to=self.sender_cfg.reply_to,
                    unsubscribe_url=unsubscribe_url
                )

            if ai_email and ai_email.get("subject") and ai_email.get("body_text"):
                subject = ai_email["subject"]
                body = ai_email["body_text"]
                logger.info(f"Generated custom AI email via Gemini for Home #{home.id} ({home.name})")
            else:
                # Render standard template variables
                subject = self.tmpl_cfg.subject.format(care_home_name=home.name)
                body = self.tmpl_cfg.body_template.format(
                    salutation=salutation,
                    care_home_name=home.name,
                    sender_org=self.sender_cfg.org_name,
                    sender_name=self.sender_cfg.sender_name,
                    reply_to=self.sender_cfg.reply_to,
                    unsubscribe_url=unsubscribe_url
                )

            # Create Email Draft in SQLite Review Queue (approved = 0 -> DO NOT AUTO SEND)
            draft = EmailDraft(
                home_id=home.id,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                subject=subject,
                body_text=body,
                approved=0,  # Requires manual approval or explicit dry-run sending
                status="DRAFT"
            )

            draft_id = self.db.save_email_draft(draft)
            drafts_created += 1
            processed += 1

            # Update home status to READY_FOR_REVIEW
            self.db.update_home_stage(home.id, StageStatus.READY_FOR_REVIEW)
            audit_msg = f"Created email draft #{draft_id} for recipient {recipient_email}. Placed in review queue."
            self.db.log_audit("Stage3_Personalise", "DRAFT_CREATED", audit_msg, home_id=home.id)

        summary = {
            "processed": processed,
            "drafts_created": drafts_created,
            "suppressed_skipped": suppressed_skipped,
            "no_recipient_skipped": no_recipient_skipped
        }
        logger.info(f"Stage 3 Personalisation complete summary: {summary}")
        return summary
