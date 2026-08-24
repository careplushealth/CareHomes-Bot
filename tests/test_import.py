import unittest
import os
import tempfile
import pandas as pd
from src.config import AppConfig
from src.db import DatabaseManager
from src.stages.stage0_import import Stage0Import
from src.utils.validation import compute_dedupe_hash, normalize_postcode, normalize_care_home_name


class TestStage0Import(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pipeline.db")
        self.csv_path = os.path.join(self.temp_dir.name, "test_homes.csv")

        self.config = AppConfig()
        self.config.database_path = self.db_path
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dedupe_hash_computation(self):
        h1 = compute_dedupe_hash("Oakwood House Care Home Ltd", "SW1A 1AA")
        h2 = compute_dedupe_hash("oakwood house care home", "sw1a1aa")
        self.assertEqual(h1, h2)

    def test_import_and_deduplication(self):
        data = [
            {"Location Name": "St Jude Care Home", "Location Postal Code": "E1 6AN", "Location Web Address": "https://stjude.co.uk"},
            {"Location Name": "St Jude Care Home Ltd", "Location Postal Code": "E1 6AN", "Location Web Address": "https://stjude.co.uk"},  # Duplicate
            {"Location Name": "Sunshine Manor", "Location Postal Code": "N1 9AA", "Location Web Address": ""}
        ]
        df = pd.DataFrame(data)
        df.to_csv(self.csv_path, index=False)

        stage0 = Stage0Import(self.config, self.db)
        summary = stage0.run_import(self.csv_path)

        self.assertEqual(summary["new_homes_inserted"], 2)
        self.assertEqual(summary["duplicate_homes_skipped"], 1)
        self.assertEqual(summary["with_website"], 2)
        self.assertEqual(summary["without_website"], 1)


if __name__ == "__main__":
    unittest.main()
