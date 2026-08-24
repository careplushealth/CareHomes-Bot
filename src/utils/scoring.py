import re
from urllib.parse import urlparse
from typing import Tuple
from src.utils.validation import normalize_care_home_name, normalize_postcode

# Domains that should never be accepted as a care home's official website
AGGREGATOR_DOMAINS = {
    "carehome.co.uk", "cqc.org.uk", "facebook.com", "instagram.com", "twitter.com",
    "linkedin.com", "yell.com", "wikipedia.org", "google.com", "bing.com",
    "directory.co.uk", "nhs.uk", "gov.uk", "companieshouse.gov.uk", "tripadvisor.co.uk",
    "glassdoor.co.uk", "indeed.com", "housingcare.org"
}


def extract_domain(url: str) -> str:
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def is_aggregator_domain(domain: str) -> bool:
    domain_clean = domain.lower()
    for agg in AGGREGATOR_DOMAINS:
        if domain_clean == agg or domain_clean.endswith("." + agg):
            return True
    return False


def calculate_website_confidence(
    url: str,
    care_home_name: str,
    postcode: str,
    page_title: str = "",
    snippet: str = ""
) -> Tuple[float, str]:
    """
    Calculates a confidence score between 0.0 and 1.0 for a discovered website URL.
    Returns (confidence_score, reason).
    """
    domain = extract_domain(url)
    if not domain:
        return 0.0, "Invalid domain"

    if is_aggregator_domain(domain):
        return 0.0, f"Rejected third-party directory/aggregator domain ({domain})"

    # Tokenize care home name
    raw_name_norm = normalize_care_home_name(care_home_name)
    name_tokens = [t for t in raw_name_norm.split() if len(t) > 2]

    if not name_tokens:
        name_tokens = [t for t in care_home_name.lower().split() if len(t) > 2]

    domain_letters_only = re.sub(r"[^a-z0-9]", "", domain)

    # 1. Domain match score
    matched_domain_tokens = 0
    for token in name_tokens:
        if token in domain_letters_only:
            matched_domain_tokens += 1

    domain_match_ratio = (matched_domain_tokens / len(name_tokens)) if name_tokens else 0.0

    # 2. Title & Snippet match score
    combined_text = f"{page_title} {snippet}".lower()
    matched_text_tokens = sum(1 for token in name_tokens if token in combined_text)
    text_match_ratio = (matched_text_tokens / len(name_tokens)) if name_tokens else 0.0

    # 3. Postcode / location check in snippet
    norm_pc = normalize_postcode(postcode)
    postcode_matched = False
    if norm_pc and (norm_pc.lower() in combined_text.replace(" ", "")):
        postcode_matched = True

    # Weighting algorithm
    # If domain match is strong (e.g. 50%+ of name tokens in domain):
    if domain_match_ratio >= 0.5:
        score = 0.65 + (0.25 * domain_match_ratio) + (0.10 * text_match_ratio)
        reason = f"Strong domain match ({matched_domain_tokens}/{len(name_tokens)} tokens in {domain})"
    elif domain_match_ratio > 0:
        score = 0.45 + (0.30 * text_match_ratio) + (0.15 if postcode_matched else 0.0)
        reason = f"Partial domain match ({matched_domain_tokens}/{len(name_tokens)} tokens in {domain})"
    else:
        # Domain didn't match directly (e.g. brand group domain like "countrycourtcare.com")
        if text_match_ratio >= 0.6:
            score = 0.40 + (0.25 * text_match_ratio) + (0.15 if postcode_matched else 0.0)
            reason = f"Weak domain match but strong page title/text match in {domain}"
        else:
            score = 0.20
            reason = f"Low relevance match for domain {domain}"

    score = min(1.0, max(0.0, round(score, 2)))
    return score, reason
