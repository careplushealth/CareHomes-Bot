import logging
import requests
import json
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class GeminiClient:
    """
    Gemini API Integration for fallback website discovery and intelligent contact parsing
    from official care home website HTML/text.
    """

    def __init__(self, api_key: str = "", model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def discover_website_fallback(self, home_name: str, address: str, postcode: str) -> Optional[Dict[str, Any]]:
        """
        Uses Gemini API to discover official website domain for tricky care homes.
        """
        if not self.is_configured():
            return None

        prompt = f"""
You are an expert UK business domain verifier.
Identify the OFFICIAL live website domain for this exact UK care home:
- Name: "{home_name}"
- Address: "{address}"
- Postcode: "{postcode}"

Return ONLY a raw JSON object with the following schema:
{{
  "official_website": "https://www.realdomain.co.uk",
  "confidence": 0.85,
  "reason": "Brief explanation of match"
}}

STRICT RULES:
1. Do NOT fabricate, guess, or construct fake .co.uk / .com domain names by appending .co.uk to the care home's name!
2. Do NOT return third-party directory sites (e.g., carehome.co.uk, facebook.com, cqc.org.uk, yell.com, nhs.uk, linkedin.com, housingcare.org).
3. If you are NOT 100% certain of the real, operating official site domain, set "official_website": null and "confidence": 0.0.
"""
        try:
            url = f"{self.endpoint}?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            content_text = data["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(content_text)
            return result
        except Exception as e:
            logger.warning(f"Gemini API discovery error for '{home_name}': {e}")
            return None

    def parse_contact_info_from_html(self, page_text: str, source_url: str) -> Optional[Dict[str, Any]]:
        """
        Uses Gemini API to intelligently extract contact details directly from a home's OWN official website page text.
        """
        if not self.is_configured() or not page_text:
            return None

        # Truncate text to stay well within limits
        sample_text = page_text[:4000]

        prompt = f"""
Extract contact information ONLY from this official care home website text ({source_url}).

Text content:
\"\"\"
{sample_text}
\"\"\"

Return ONLY a raw JSON object with the following schema:
{{
  "general_email": "info@carehome.co.uk",
  "contact_form_url": "https://carehome.co.uk/contact",
  "manager_name": "Jane Doe",
  "manager_email": "jane.doe@carehome.co.uk"
}}

Rules:
- Extract published emails, contact forms, or manager names ONLY if openly present in the text above.
- Return null for any field not found.
"""
        try:
            url = f"{self.endpoint}?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            content_text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(content_text)
        except Exception as e:
            logger.warning(f"Gemini API contact parsing error for '{source_url}': {e}")
            return None

    def generate_personalized_email(
        self,
        care_home_name: str,
        address: str,
        postcode: str,
        salutation: str,
        sender_org: str,
        sender_name: str,
        reply_to: str,
        unsubscribe_url: str
    ) -> Optional[Dict[str, str]]:
        """
        Uses Gemini API to craft a warm, professional, highly tailored outreach email
        introducing an NHS-partnered Covid testing service.
        """
        if not self.is_configured():
            return None

        prompt = f"""
You are a professional B2B healthcare communications specialist.
Write a personalized, clear, PECR-compliant outreach email introducing an NHS-partnered Covid-19 testing service for UK care homes.

Recipient Details:
- Care Home Name: "{care_home_name}"
- Address/Location: "{address}, {postcode}"
- Addressed To: "{salutation}"

Sender Details:
- Sender Organisation: "{sender_org}"
- Sender Contact Name: "{sender_name}"
- Reply-To Email: "{reply_to}"
- Unsubscribe URL: "{unsubscribe_url}"

Return ONLY a raw JSON object with the following schema:
{{
  "subject": "Subject line here",
  "body_text": "Complete email body here"
}}

Requirements:
1. Insert "{care_home_name}" naturally into the message.
2. Accurately describe the offering as an "NHS-partnered resident Covid testing service" (do NOT state you ARE the NHS itself, accurately state the partnership).
3. Professional, empathetic tone appropriate for UK care home management.
4. Include clear reply-to contact info ({reply_to}).
5. End with the mandatory PECR Opt-Out notice at the bottom including {unsubscribe_url} and reply "UNSUBSCRIBE" option.
"""
        try:
            url = f"{self.endpoint}?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            content_text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(content_text)
        except Exception as e:
            logger.warning(f"Gemini API email generation error for '{care_home_name}': {e}")
            return None
