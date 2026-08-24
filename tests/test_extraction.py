import unittest
from bs4 import BeautifulSoup
from src.utils.validation import is_valid_email
from src.stages.stage2_extraction import Stage2Extraction
from src.config import AppConfig


class TestStage2Extraction(unittest.TestCase):
    def test_email_validation(self):
        self.assertTrue(is_valid_email("info@oakwoodcare.co.uk"))
        self.assertTrue(is_valid_email("sarah.smith@oakwoodcare.co.uk"))
        self.assertFalse(is_valid_email("not_an_email"))
        self.assertFalse(is_valid_email("logo.png@2x"))

    def test_email_extraction_from_html(self):
        html = """
        <html>
            <body>
                <p>Welcome to Meadow View Care Home.</p>
                <p>Contact us at <a href="mailto:enquiries@meadowview.co.uk">enquiries@meadowview.co.uk</a></p>
                <p>Or call our manager Sarah Jenkins at sarah.jenkins@meadowview.co.uk</p>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        stage2 = Stage2Extraction(AppConfig(), None)
        emails = stage2._extract_emails_from_soup(soup)

        self.assertIn("enquiries@meadowview.co.uk", emails)
        self.assertIn("sarah.jenkins@meadowview.co.uk", emails)


if __name__ == "__main__":
    unittest.main()
