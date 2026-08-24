import unittest
import os
import tempfile
from src.config import AppConfig
from src.db import DatabaseManager
from src.stages.stage1_discovery import Stage1Discovery, SearchAPIClient
from src.utils.scoring import calculate_website_confidence, is_aggregator_domain


class TestStage1Discovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pipeline.db")
        self.config = AppConfig()
        self.config.database_path = self.db_path
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_aggregator_domain_rejection(self):
        self.assertTrue(is_aggregator_domain("www.carehome.co.uk"))
        self.assertTrue(is_aggregator_domain("cqc.org.uk"))
        self.assertTrue(is_aggregator_domain("facebook.com"))
        self.assertFalse(is_aggregator_domain("oakwoodcare.co.uk"))

    def test_confidence_scoring(self):
        # Strong match
        score1, reason1 = calculate_website_confidence(
            url="https://www.oakwoodcare.co.uk",
            care_home_name="Oakwood Care Home",
            postcode="SW1A 1AA",
            page_title="Oakwood Care Home - Official Site",
            snippet="Residential care in London SW1A 1AA"
        )
        self.assertGreaterEqual(score1, 0.65)

        # Rejected directory
        score2, reason2 = calculate_website_confidence(
            url="https://www.carehome.co.uk/carehome.cfm/searchfind/site/123",
            care_home_name="Oakwood Care Home",
            postcode="SW1A 1AA"
        )
        self.assertEqual(score2, 0.0)

    def test_mock_search_client(self):
        client = SearchAPIClient(provider="mock", google_key="", google_cse_id="", bing_key="")
        results = client.search('"Oakwood House" "SW1A 1AA"')
        self.assertGreater(len(results), 0)
        self.assertIn("oakwood", results[0]["url"].lower())


if __name__ == "__main__":
    unittest.main()
