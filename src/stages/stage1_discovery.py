import logging
import requests
import json
from typing import Dict, Any, Optional, Tuple, List
from src.stages.base import BaseStage
from src.models import CareHome, StageStatus
from src.utils.scoring import calculate_website_confidence, extract_domain
from src.utils.validation import normalize_url
from src.utils.gemini import GeminiClient

logger = logging.getLogger(__name__)


class SearchAPIClient:
    """Official Search API Client supporting Google Custom Search JSON API, Bing Web Search API, and Mock API."""

    def __init__(self, provider: str, google_key: str, google_cse_id: str, bing_key: str):
        self.provider = provider.lower()
        self.google_key = google_key
        self.google_cse_id = google_cse_id
        self.bing_key = bing_key

    def search(self, query: str) -> List[Dict[str, str]]:
        """
        Executes web search query via official or free API.
        Returns list of dicts: [{'url': ..., 'title': ..., 'snippet': ...}]
        """
        if self.provider in ("ddg", "duckduckgo"):
            return self._search_duckduckgo(query)
        elif self.provider == "google":
            return self._search_google(query)
        elif self.provider == "bing":
            return self._search_bing(query)
        else:
            return self._search_mock(query)

    def _search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """100% Free web search requiring NO API keys and NO domain restrictions."""
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS

            ddgs = DDGS()
            raw_results = list(ddgs.text(query, max_results=3))
            results = []
            for item in raw_results:
                results.append({
                    "url": item.get("href", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("body", "")
                })
            return results
        except Exception as e:
            logger.error(f"DuckDuckGo search error for '{query}': {e}")
            return []

    def _search_google(self, query: str) -> List[Dict[str, str]]:
        if not self.google_key or not self.google_cse_id:
            logger.warning("Google API Key or CSE ID missing.")
            return []

        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.google_key,
            "cx": self.google_cse_id,
            "q": query,
            "num": 3
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("items", []):
                results.append({
                    "url": item.get("link", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", "")
                })
            return results
        except Exception as e:
            logger.error(f"Google CSE API search error for '{query}': {e}")
            return []

    def _search_bing(self, query: str) -> List[Dict[str, str]]:
        if not self.bing_key:
            logger.warning("Bing API Key missing.")
            return []

        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": self.bing_key}
        params = {"q": query, "count": 3}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("webPages", {}).get("value", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("name", ""),
                    "snippet": item.get("snippet", "")
                })
            return results
        except Exception as e:
            logger.error(f"Bing Web Search API error for '{query}': {e}")
            return []

    def _search_mock(self, query: str) -> List[Dict[str, str]]:
        """
        Mock search response for testing & demonstration without consuming paid API quota.
        Derives realistic simulated website URL from query.
        """
        # Extract home name from query: e.g. '"Oakwood House" "SW1A 1AA"' -> oakwoodhousecare.co.uk
        words = [w.strip('"').lower() for w in query.split() if not w.startswith('"SW') and not w.startswith('"E') and not w.startswith('"W') and not w.startswith('"N')]
        clean_words = [w for w in words if w not in ("care", "home", "nursing", "residential", "house", "ltd")]
        brand = "".join(clean_words[:2]) if clean_words else "carehome"

        # Check if query simulates low confidence test case
        if "unknown" in query.lower() or "generic" in query.lower():
            mock_url = f"https://www.carehome.co.uk/carehome.cfm/searchfind/site/{brand}"
            mock_title = "CareHome UK Directory Listing"
            mock_snippet = f"Directory listing for {query}"
        else:
            mock_url = f"https://www.{brand}care.co.uk"
            mock_title = f"Official Website for {query}"
            mock_snippet = f"Welcome to our care home in {query}. Providing compassionate residential care."

        return [{
            "url": mock_url,
            "title": mock_title,
            "snippet": mock_snippet
        }]


class Stage1Discovery(BaseStage):
    """
    STAGE 1: Official API Website Discovery & Confidence Scoring for Blank-Website Care Homes
    """

    def __init__(self, config, db):
        super().__init__(config, db, stage_name="Stage1_Discovery")
        s_cfg = config.search_api
        self.search_client = SearchAPIClient(
            provider=s_cfg.provider,
            google_key=s_cfg.google_api_key,
            google_cse_id=s_cfg.google_cse_id,
            bing_key=s_cfg.bing_api_key
        )
        self.threshold = s_cfg.confidence_threshold

    def _is_url_reachable(self, url: str, timeout: int = 5) -> bool:
        if not url or not url.startswith(("http://", "https://")):
            return False
        headers = {"User-Agent": self.config.pipeline.user_agent}
        try:
            resp = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code in (200, 301, 302, 307, 308):
                return True
        except Exception:
            pass

        try:
            resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
            if resp.status_code in (200, 301, 302, 307, 308):
                return True
        except Exception:
            pass

        return False

    def run(self, max_items: Optional[int] = None) -> Dict[str, Any]:
        logger.info("Starting Stage 1: Website Discovery...")

        pending_homes = self.db.get_homes_for_stage(StageStatus.PENDING_DISCOVERY, limit=max_items)
        logger.info(f"Found {len(pending_homes)} homes pending website discovery.")

        processed = 0
        discovered_high_confidence = 0
        flagged_for_review = 0
        no_result_found = 0

        for home in pending_homes:
            if not self.can_process_today(custom_cap=self.config.pipeline.crawling_daily_cap):
                logger.info(f"Daily crawling cap reached ({self.config.pipeline.crawling_daily_cap}). Pausing Stage 1.")
                break

            # Construct query: Name + Postcode
            query = f'"{home.name}" "{home.postcode}"'
            logger.info(f"Discovering website for Home #{home.id}: {home.name} ({home.postcode})...")

            search_results = self.search_client.search(query)
            processed += 1
            self.increment_daily_progress()

            if not search_results:
                no_result_found += 1
                msg = f"No search results returned for query: {query}"
                logger.warning(f"Home #{home.id}: {msg}")
                self.db.update_home_website(
                    home_id=home.id,
                    discovered_url=None,
                    confidence=0.0,
                    website_status="NO_RESULT",
                    next_stage=StageStatus.MANUAL_REVIEW_NEEDED
                )
                self.db.log_audit("Stage1_Discovery", "NO_RESULTS", msg, home_id=home.id)
                continue

            top_result = search_results[0]
            cand_url = normalize_url(top_result["url"])
            title = top_result.get("title", "")
            snippet = top_result.get("snippet", "")

            # Sanity & confidence check
            confidence, reason = calculate_website_confidence(
                url=cand_url,
                care_home_name=home.name,
                postcode=home.postcode,
                page_title=title,
                snippet=snippet
            )

            logger.info(
                f"Home #{home.id} -> Found candidate URL: {cand_url} | "
                f"Confidence Score: {confidence:.2f} | Reason: {reason}"
            )

            # Verify URL with live HTTP ping test
            is_reachable = self._is_url_reachable(cand_url)

            if confidence >= self.threshold and is_reachable:
                # Auto-accept and advance to Stage 2
                discovered_high_confidence += 1
                self.db.update_home_website(
                    home_id=home.id,
                    discovered_url=cand_url,
                    confidence=confidence,
                    website_status="ACCEPTED",
                    next_stage=StageStatus.PENDING_EXTRACTION
                )
                audit_msg = f"Accepted verified website {cand_url} with confidence {confidence:.2f} (>= {self.threshold:.2f}). Reason: {reason}"
                self.db.log_audit("Stage1_Discovery", "WEBSITE_ACCEPTED", audit_msg, home_id=home.id)
            else:
                # Fallback check using Gemini API if configured
                gemini_client = GeminiClient(api_key=self.config.search_api.gemini_api_key)
                gemini_res = gemini_client.discover_website_fallback(home.name, home.address, home.postcode) if gemini_client.is_configured() else None

                gemini_url = normalize_url(gemini_res["official_website"]) if (gemini_res and gemini_res.get("official_website")) else None
                gemini_reachable = self._is_url_reachable(gemini_url) if gemini_url else False

                if gemini_url and gemini_reachable and gemini_res.get("confidence", 0.0) >= self.threshold:
                    discovered_high_confidence += 1
                    self.db.update_home_website(
                        home_id=home.id,
                        discovered_url=gemini_url,
                        confidence=gemini_res.get("confidence", 0.90),
                        website_status="ACCEPTED",
                        next_stage=StageStatus.PENDING_EXTRACTION
                    )
                    audit_msg = f"Accepted verified website via Gemini API Fallback: {gemini_url} (Confidence: {gemini_res.get('confidence'):.2f}). Reason: {gemini_res.get('reason')}"
                    self.db.log_audit("Stage1_Discovery", "GEMINI_WEBSITE_ACCEPTED", audit_msg, home_id=home.id)
                else:
                    # Flag for manual review / unfound list
                    flagged_for_review += 1
                    status_reason = f"Confidence {confidence:.2f} (< {self.threshold:.2f}). Reason: {reason}"
                    if not is_reachable:
                        status_reason += " [Domain not reachable via HTTP]"
                    self.db.update_home_website(
                        home_id=home.id,
                        discovered_url=cand_url if is_reachable else None,
                        confidence=confidence if is_reachable else 0.0,
                        website_status="NEEDS_MANUAL_REVIEW",
                        next_stage=StageStatus.MANUAL_REVIEW_NEEDED
                    )
                    audit_msg = f"Flagged care home website for manual review. {status_reason}"
                    self.db.log_audit("Stage1_Discovery", "FLAGGED_FOR_REVIEW", audit_msg, home_id=home.id)

        summary = {
            "processed": processed,
            "accepted_high_confidence": discovered_high_confidence,
            "flagged_for_manual_review": flagged_for_review,
            "no_results": no_result_found
        }
        logger.info(f"Stage 1 Discovery complete summary: {summary}")
        return summary
