import re
import hashlib
from urllib.parse import urlparse
from typing import Optional, Any


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, float):
        import math
        if math.isnan(text):
            return ""
    txt_str = str(text).strip().replace("\xa0", " ")
    if txt_str.lower() in ("nan", "none", "null"):
        return ""
    return re.sub(r"\s+", " ", txt_str)


def normalize_postcode(postcode: Optional[str]) -> str:
    if not postcode:
        return ""
    # Strip spaces and convert to uppercase, e.g. "SW1A 1AA" -> "SW1A1AA"
    cleaned = re.sub(r"[^A-Za-z0-9]", "", postcode).upper()
    return cleaned


def normalize_care_home_name(name: Optional[str]) -> str:
    if not name:
        return ""
    text = name.lower()
    # Remove common UK business suffixes for matching/deduplication
    text = re.sub(r"\b(limited|ltd|care home|care house|residential home|nursing home|care|home|house|uk|the)\b", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    return clean_text(text)


def compute_dedupe_hash(name: str, postcode: str) -> str:
    norm_name = normalize_care_home_name(name)
    norm_pc = normalize_postcode(postcode)
    raw = f"{norm_name}|{norm_pc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_valid_email(email: Optional[str]) -> bool:
    if not email:
        return False
    email = email.strip()
    # Basic standard email pattern
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        return False
    # Exclude common image/font dummy extension traps like .png@, etc.
    invalid_exts = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".webp", ".pdf")
    if any(email.lower().endswith(ext) for ext in invalid_exts):
        return False
    return True


def normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return url
