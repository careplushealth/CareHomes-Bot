import unittest
import os
import tempfile
from src.config import AppConfig
from src.db import DatabaseManager
from src.models import CareHome, EmailDraft, StageStatus
from src.stages.stage4_sending import Stage4Sending


class TestStage4Sending(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pipeline.db")
        self.config = AppConfig()
        self.config.database_path = self.db_path
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_gated_sending_and_suppression_list(self):
        home = CareHome(name="Test Home", postcode="N1 1AA", stage_status=StageStatus.READY_FOR_REVIEW, dedupe_hash="hash2")
        self.db.insert_care_homes([home])
        homes = self.db.get_homes_for_stage(StageStatus.READY_FOR_REVIEW)
        home_id = homes[0].id

        # Insert approved draft
        draft1 = EmailDraft(
            home_id=home_id,
            recipient_email="manager@testhome.co.uk",
            subject="Test Subject",
            body_text="Test Body",
            approved=1,
            status="QUEUED"
        )
        d1_id = self.db.save_email_draft(draft1)

        # Insert suppressed email draft
        draft2 = EmailDraft(
            home_id=home_id,
            recipient_email="optout@suppressed.co.uk",
            subject="Test Subject 2",
            body_text="Test Body 2",
            approved=1,
            status="QUEUED"
        )
        d2_id = self.db.save_email_draft(draft2)
        self.db.add_suppression("optout@suppressed.co.uk", "user opted out")

        stage4 = Stage4Sending(self.config, self.db, transport_mode="dry_run")
        summary = stage4.run(approved_only=True)

        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["suppressed"], 1)


if __name__ == "__main__":
    unittest.main()
