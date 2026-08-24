import unittest
import os
import tempfile
from src.config import AppConfig
from src.db import DatabaseManager
from src.models import CareHome, ContactDetails, StageStatus
from src.stages.stage3_personalise import Stage3Personalise


class TestStage3Personalise(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pipeline.db")
        self.config = AppConfig()
        self.config.database_path = self.db_path
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_draft_creation_and_pecr_optout(self):
        # Insert care home and contact
        home = CareHome(
            name="Oakwood Care Home",
            postcode="SW1A 1AA",
            stage_status=StageStatus.PENDING_PERSONALISATION,
            dedupe_hash="hash1"
        )
        self.db.insert_care_homes([home])

        # Retrieve inserted ID
        homes = self.db.get_homes_for_stage(StageStatus.PENDING_PERSONALISATION)
        home_id = homes[0].id

        contact = ContactDetails(
            home_id=home_id,
            general_email="info@oakwoodcare.co.uk",
            manager_name="Sarah Smith"
        )
        self.db.save_contact_details(contact)

        stage3 = Stage3Personalise(self.config, self.db)
        summary = stage3.run()

        self.assertEqual(summary["drafts_created"], 1)

        drafts = self.db.get_pending_drafts()
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]

        # Check PECR opt-out inclusion
        self.assertIn("opt-out", draft["body_text"].lower())
        self.assertIn("unsubscribe", draft["body_text"].lower())
        self.assertIn("Sarah Smith", draft["body_text"])
        self.assertEqual(draft["approved"], 0)  # Gated review queue default


if __name__ == "__main__":
    unittest.main()
