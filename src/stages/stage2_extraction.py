import re
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from typing import Dict, Any, Optional, List, Set, Tuple
from src.stages.base import BaseStage
from src.models import CareHome, ContactDetails, StageStatus
from src.utils.robots import DomainPolicyManager
from src.utils.validation import is_valid_email, clean_text
from src.utils.gemini import GeminiClient

logger = logging.getLogger(__name__)

# Common internal paths for contact & staff pages on care home sites
TARGET_PATH_KEYWORDS = [
    "contact", "contact-us", "about", "about-us", "team", "staff",
    "management", "our-team", "meet-the-team", "get-in-touch"
]

# Patterns for identifying general emails vs manager emails
GENERAL_EMAIL_PREFIXES = (
    "info@", "enquiries@", "admin@", "office@", "reception@",
    "manager@", "care@", "contact@", "help@", "support@"
)


class Stage2Extraction(BaseStage):
    """
    STAGE 2: Contact Extraction from each home's OWN official website only
    """

    def __init__(self, config, db):
        super().__init__(config, db, stage_name="Stage2_Extraction")
        p_cfg = config.pipeline
        self.policy_manager = DomainPolicyManager(
            user_agent=p_cfg.user_agent,
            min_delay=p_cfg.domain_request_delay_min,
            max_delay=p_cfg.domain_request_delay_max,
            request_timeout=p_cfg.request_timeout_seconds
        )

    def run(self, max_items: Optional[int] = None) -> Dict[str, Any]:
        logger.info("Starting Stage 2: Contact Extraction from official websites...")

        pending_homes = self.db.get_homes_for_stage(StageStatus.PENDING_EXTRACTION, limit=max_items)
        logger.info(f"Found {len(pending_homes)} homes pending contact extraction.")

        processed = 0
        emails_found = 0
        forms_found = 0
        managers_found = 0
        crawls_failed = 0

        for home in pending_homes:
            if not self.can_process_today(custom_cap=self.config.pipeline.crawling_daily_cap):
                logger.info(f"Daily crawling cap reached ({self.config.pipeline.crawling_daily_cap}). Pausing Stage 2.")
                break

            target_url = home.active_website
            if not target_url:
                logger.warning(f"Home #{home.id} ({home.name}) has no valid active website URL. Skipping.")
                self.db.update_home_stage(home.id, StageStatus.MANUAL_REVIEW_NEEDED)
                continue

            logger.info(f"Extracting contact details for Home #{home.id} ({home.name}) from OWN site: {target_url}...")
            self.increment_daily_progress()
            processed += 1

            contact_details, success = self._extract_from_domain(home.id, target_url)

            if success and contact_details:
                self.db.save_contact_details(contact_details)
                if contact_details.general_email or contact_details.manager_email:
                    emails_found += 1
                if contact_details.contact_form_url:
                    forms_found += 1
                if contact_details.manager_name or contact_details.manager_email:
                    managers_found += 1

                # Advance home to Stage 3 Personalisation
                self.db.update_home_stage(home.id, StageStatus.PENDING_PERSONALISATION)
                audit_msg = (
                    f"Extracted details from {target_url}: General Email={contact_details.general_email}, "
                    f"Form={contact_details.contact_form_url}, Manager={contact_details.manager_name} ({contact_details.manager_email})"
                )
                self.db.log_audit("Stage2_Extraction", "EXTRACTION_SUCCESS", audit_msg, home_id=home.id)
            else:
                crawls_failed += 1
                self.db.update_home_stage(home.id, StageStatus.MANUAL_REVIEW_NEEDED)
                audit_msg = f"Failed to extract contact information from {target_url}"
                self.db.log_audit("Stage2_Extraction", "EXTRACTION_FAILED", audit_msg, home_id=home.id)

        summary = {
            "processed": processed,
            "emails_found": emails_found,
            "forms_found": forms_found,
            "managers_found": managers_found,
            "failed_crawls": crawls_failed
        }
        logger.info(f"Stage 2 Extraction complete summary: {summary}")
        return summary

    def _extract_from_domain(self, home_id: int, base_url: str) -> Tuple[Optional[ContactDetails], bool]:
        domain = self.policy_manager.get_domain(base_url)

        # 1. Check robots.txt for domain homepage
        if not self.policy_manager.is_allowed(base_url):
            logger.warning(f"Robots.txt disallows crawling for domain: {domain}")
            return None, False

        visited_urls: Set[str] = set()
        urls_to_visit = [base_url]

        found_emails: Set[str] = set()
        contact_form_url: Optional[str] = None
        manager_name: Optional[str] = None
        manager_email: Optional[str] = None
        source_url: Optional[str] = base_url

        try:
            # Crawl up to 5 pages on the SAME domain (homepage + contact/about/team links)
            max_pages = 5
            pages_crawled = 0

            while urls_to_visit and pages_crawled < max_pages:
                current_url = urls_to_visit.pop(0)
                if current_url in visited_urls:
                    continue
                visited_urls.add(current_url)

                # Ensure strict same-domain constraint
                if self.policy_manager.get_domain(current_url) != domain:
                    continue

                if not self.policy_manager.is_allowed(current_url):
                    continue

                # Rate limiting delay before fetching
                self.policy_manager.enforce_rate_limit(current_url)

                logger.debug(f"Fetching page: {current_url}")
                headers = {"User-Agent": self.config.pipeline.user_agent}
                resp = requests.get(
                    current_url,
                    headers=headers,
                    timeout=self.config.pipeline.request_timeout_seconds,
                    allow_redirects=True
                )
                pages_crawled += 1

                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # Extract emails from text & mailto links
                emails_on_page = self._extract_emails_from_soup(soup)
                for email in emails_on_page:
                    found_emails.add(email)
                    source_url = current_url

                # Extract contact form URL if not already found
                if not contact_form_url:
                    form_url = self._extract_contact_form(soup, current_url)
                    if form_url:
                        contact_form_url = form_url

                # Check if current page is team/staff page and extract manager info
                if any(k in current_url.lower() for k in ["team", "staff", "about", "management"]):
                    name, m_email = self._extract_manager_info(soup)
                    if name:
                        manager_name = name
                    if m_email:
                        manager_email = m_email

                # Fallback to Gemini AI text parser if no email or manager found on page
                gemini_client = GeminiClient(api_key=self.config.search_api.gemini_api_key)
                if (not found_emails or not manager_name) and gemini_client.is_configured():
                    gemini_info = gemini_client.parse_contact_info_from_html(soup.get_text(separator=" "), current_url)
                    if gemini_info:
                        if gemini_info.get("general_email") and is_valid_email(gemini_info["general_email"]):
                            found_emails.add(gemini_info["general_email"].lower())
                        if gemini_info.get("manager_email") and is_valid_email(gemini_info["manager_email"]):
                            manager_email = gemini_info["manager_email"].lower()
                        if gemini_info.get("manager_name"):
                            manager_name = gemini_info["manager_name"]
                        if gemini_info.get("contact_form_url") and not contact_form_url:
                            contact_form_url = gemini_info["contact_form_url"]

                # Discover relevant internal contact/team links to visit next
                internal_links = self._find_relevant_internal_links(soup, current_url, domain)
                for link in internal_links:
                    if link not in visited_urls and link not in urls_to_visit:
                        urls_to_visit.append(link)

            # Categorize general email vs manager email
            general_email: Optional[str] = None

            if manager_email:
                # If manager email is found, select general email from remaining emails
                general_candidates = [e for e in found_emails if e.lower() != manager_email.lower()]
                general_email = self._select_best_general_email(general_candidates)
            else:
                general_email = self._select_best_general_email(list(found_emails))

            return ContactDetails(
                home_id=home_id,
                general_email=general_email,
                contact_form_url=contact_form_url,
                manager_name=manager_name,
                manager_email=manager_email,
                source_page_url=source_url
            ), True

        except Exception as e:
            logger.error(f"Error crawling {base_url}: {e}")
            return None, False

    def _extract_emails_from_soup(self, soup: BeautifulSoup) -> List[str]:
        emails = set()
        # 1. Mailto links
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                clean_email = href.split(":")[1].split("?")[0].strip()
                if is_valid_email(clean_email):
                    emails.add(clean_email.lower())

        # 2. Text regex matching
        text = soup.get_text(separator=" ")
        regex_emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
        for e in regex_emails:
            if is_valid_email(e):
                emails.add(e.lower())

        return list(emails)

    def _extract_contact_form(self, soup: BeautifulSoup, current_url: str) -> Optional[str]:
        # Check <form> elements
        forms = soup.find_all("form")
        for f in forms:
            action = f.get("action", "")
            full_form_url = urljoin(current_url, action) if action else current_url
            return full_form_url

        # Check if page URL looks like contact form
        if "contact" in current_url.lower():
            return current_url

        return None

    def _extract_manager_info(self, soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
        """
        Extracts named manager email ONLY if published directly on home's own staff/team page.
        """
        text = soup.get_text(separator="\n")
        lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]

        manager_name: Optional[str] = None
        manager_email: Optional[str] = None

        for idx, line in enumerate(lines):
            # Look for lines mentioning Manager / Home Manager / Registered Manager
            if re.search(r"\b(home manager|registered manager|care manager|general manager)\b", line, re.IGNORECASE):
                # Look in neighboring lines for name and email
                window = lines[max(0, idx-2): min(len(lines), idx+3)]
                for w in window:
                    # Match name patterns e.g. "Jane Doe" or "Manager: John Smith"
                    name_match = re.search(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b", w)
                    if name_match and name_match.group(1) not in ("Home Manager", "Registered Manager", "Care Home"):
                        manager_name = name_match.group(1)
                    # Match manager specific email
                    em_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", w)
                    if em_match and is_valid_email(em_match.group(1)):
                        manager_email = em_match.group(1).lower()

        return manager_name, manager_email

    def _find_relevant_internal_links(self, soup: BeautifulSoup, current_url: str, domain: str) -> List[str]:
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full_url = urljoin(current_url, href)
            parsed = urlparse(full_url)
            # Must be same domain
            if parsed.netloc.lower() == domain:
                path = parsed.path.lower()
                if any(kw in path for kw in TARGET_PATH_KEYWORDS):
                    links.append(full_url)
        return links

    def _select_best_general_email(self, emails: List[str]) -> Optional[str]:
        if not emails:
            return None
        # Prioritize standard general prefixes
        for prefix in GENERAL_EMAIL_PREFIXES:
            for e in emails:
                if e.startswith(prefix):
                    return e
        # Otherwise return the first valid email found
        return emails[0]
