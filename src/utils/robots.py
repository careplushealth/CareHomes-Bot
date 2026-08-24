import time
import random
import logging
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
import requests
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class DomainPolicyManager:
    """
    Manages robots.txt caching and per-domain polite rate limiting (3-8 seconds delay).
    """

    def __init__(
        self,
        user_agent: str = "UKCareHomeOutreachBot/1.0",
        min_delay: float = 3.0,
        max_delay: float = 8.0,
        request_timeout: int = 10
    ):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.request_timeout = request_timeout
        self._robots_cache: Dict[str, RobotFileParser] = {}
        self._last_request_time: Dict[str, float] = {}

    def get_domain(self, url: str) -> str:
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "http://" + url
        parsed = urlparse(url)
        return parsed.netloc.lower()

    def fetch_robots_txt(self, url: str) -> RobotFileParser:
        domain = self.get_domain(url)
        if domain in self._robots_cache:
            return self._robots_cache[domain]

        parsed = urlparse(url if url.startswith("http") else "http://" + url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        rfp = RobotFileParser()
        rfp.set_url(robots_url)

        try:
            headers = {"User-Agent": self.user_agent}
            response = requests.get(robots_url, headers=headers, timeout=self.request_timeout)
            if response.status_code == 200:
                rfp.parse(response.text.splitlines())
                logger.info(f"Successfully fetched and parsed robots.txt for {domain}")
            else:
                # If robots.txt doesn't exist (404), crawling is allowed by default
                rfp.allow_all = True
                logger.info(f"No robots.txt found for {domain} (HTTP {response.status_code}); defaulting to allowed")
        except Exception as e:
            logger.warning(f"Could not fetch robots.txt for {domain}: {e}; defaulting to allowed")
            rfp.allow_all = True

        self._robots_cache[domain] = rfp
        return rfp

    def is_allowed(self, url: str) -> bool:
        """Returns True if crawling the given URL is allowed by robots.txt."""
        try:
            rfp = self.fetch_robots_txt(url)
            allowed = rfp.can_fetch(self.user_agent, url)
            return allowed
        except Exception as e:
            logger.warning(f"Error checking robots.txt for {url}: {e}; assuming allowed")
            return True

    def enforce_rate_limit(self, url: str):
        """Enforces a polite randomized delay (3-8 seconds) between requests to the SAME domain."""
        domain = self.get_domain(url)
        now = time.time()
        if domain in self._last_request_time:
            elapsed = now - self._last_request_time[domain]
            required_delay = random.uniform(self.min_delay, self.max_delay)
            if elapsed < required_delay:
                sleep_time = required_delay - elapsed
                logger.debug(f"Rate limiting domain {domain}: sleeping for {sleep_time:.2f}s")
                time.sleep(sleep_time)

        self._last_request_time[domain] = time.time()
