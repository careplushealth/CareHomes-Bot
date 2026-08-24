import logging
import pandas as pd
from typing import List, Dict, Any, Optional
from src.db import DatabaseManager
from src.models import StageStatus

logger = logging.getLogger(__name__)


class ReviewQueueManager:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def reset_discovered_websites(self) -> int:
        count = self.db.reset_discovered_websites()
        msg = f"Reset {count} care home website discoveries back to PENDING_DISCOVERY."
        logger.info(msg)
        self.db.log_audit("Review", "DISCOVERY_RESET", msg)
        return count

    def get_websites_needing_review(self) -> List[Dict[str, Any]]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, postcode, address, discovered_website, website_confidence, website_status
                FROM homes
                WHERE website_status = 'NEEDS_MANUAL_REVIEW' OR stage_status = 'MANUAL_REVIEW_NEEDED'
                ORDER BY id ASC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def approve_website(self, home_id: int, override_url: Optional[str] = None):
        home = self.db.get_homes_for_stage(StageStatus.MANUAL_REVIEW_NEEDED)
        # Find home
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM homes WHERE id = ?", (home_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Care Home #{home_id} not found.")

            target_url = override_url if override_url else row["discovered_website"]
            if not target_url:
                raise ValueError(f"Care Home #{home_id} has no website specified to approve.")

            self.db.update_home_website(
                home_id=home_id,
                discovered_url=target_url,
                confidence=1.0,
                website_status="MANUAL_APPROVED",
                next_stage=StageStatus.PENDING_EXTRACTION
            )
            msg = f"Manually approved website {target_url} for Care Home #{home_id}"
            logger.info(msg)
            self.db.log_audit("Review", "WEBSITE_MANUAL_APPROVED", msg, home_id=home_id)

    def reject_website(self, home_id: int):
        with self.db.get_connection() as conn:
            conn.execute("""
                UPDATE homes SET website_status = 'REJECTED', stage_status = 'SKIPPED' WHERE id = ?
            """, (home_id,))
            conn.commit()
            msg = f"Rejected website discovery for Care Home #{home_id}"
            logger.info(msg)
            self.db.log_audit("Review", "WEBSITE_REJECTED", msg, home_id=home_id)

    def list_pending_drafts(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.db.get_pending_drafts(limit=limit)

    def approve_draft(self, draft_id: int):
        self.db.approve_draft(draft_id)
        msg = f"Approved email draft #{draft_id} for sending queue."
        logger.info(msg)
        self.db.log_audit("Review", "DRAFT_APPROVED", msg)

    def approve_all_drafts(self) -> int:
        pending = self.db.get_pending_drafts()
        for d in pending:
            self.db.approve_draft(d["id"])
        msg = f"Batch-approved {len(pending)} pending email drafts."
        logger.info(msg)
        self.db.log_audit("Review", "BATCH_DRAFTS_APPROVED", msg)
        return len(pending)

    def reject_draft(self, draft_id: int):
        self.db.reject_draft(draft_id)
        msg = f"Rejected email draft #{draft_id}."
        logger.info(msg)
        self.db.log_audit("Review", "DRAFT_REJECTED", msg)

    def export_drafts_to_csv(self, output_csv_path: str) -> int:
        with self.db.get_connection() as conn:
            query = """
                SELECT d.id as draft_id, h.id as home_id, h.name as care_home_name, h.postcode,
                       d.recipient_email, d.recipient_name, d.subject, d.body_text,
                       d.approved, d.status, d.created_at
                FROM email_drafts d
                JOIN homes h ON d.home_id = h.id
                ORDER BY d.id ASC
            """
            df = pd.read_sql_query(query, conn)
            df.to_csv(output_csv_path, index=False)
            logger.info(f"Exported {len(df)} email drafts to {output_csv_path}")
            return len(df)

    def export_unfound_to_csv(self, output_csv_path: str) -> int:
        unfound_list = self.db.get_unfound_carehomes()
        df = pd.DataFrame(unfound_list)
        df.to_csv(output_csv_path, index=False)
        logger.info(f"Exported {len(df)} unfound care homes to {output_csv_path}")
        return len(df)
