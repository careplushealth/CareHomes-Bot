from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


class StageStatus:
    PENDING_DISCOVERY = "PENDING_DISCOVERY"
    PENDING_EXTRACTION = "PENDING_EXTRACTION"
    PENDING_PERSONALISATION = "PENDING_PERSONALISATION"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FAILED = "FAILED"
    MANUAL_REVIEW_NEEDED = "MANUAL_REVIEW_NEEDED"
    SKIPPED = "SKIPPED"


def get_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CareHome:
    id: Optional[int] = None
    cqc_location_id: Optional[str] = None
    name: str = ""
    address: str = ""
    postcode: str = ""
    original_website: Optional[str] = None
    discovered_website: Optional[str] = None
    website_confidence: float = 0.0
    website_status: str = "UNCHECKED"  # "UNCHECKED", "ACCEPTED", "NEEDS_MANUAL_REVIEW", "REJECTED"
    stage_status: str = StageStatus.PENDING_DISCOVERY
    dedupe_hash: str = ""
    created_at: str = field(default_factory=get_utc_now)
    updated_at: str = field(default_factory=get_utc_now)

    @property
    def active_website(self) -> Optional[str]:
        if self.original_website and self.original_website.strip():
            return self.original_website.strip()
        if self.discovered_website and self.website_status in ("ACCEPTED", "MANUAL_APPROVED"):
            return self.discovered_website.strip()
        return None


@dataclass
class ContactDetails:
    id: Optional[int] = None
    home_id: int = 0
    general_email: Optional[str] = None
    contact_form_url: Optional[str] = None
    manager_name: Optional[str] = None
    manager_email: Optional[str] = None
    source_page_url: Optional[str] = None
    created_at: str = field(default_factory=get_utc_now)


@dataclass
class EmailDraft:
    id: Optional[int] = None
    home_id: int = 0
    recipient_email: str = ""
    recipient_name: str = "Care Home Manager"
    subject: str = ""
    body_text: str = ""
    approved: int = 0  # 0 = pending review, 1 = approved, -1 = rejected
    status: str = "DRAFT"  # "DRAFT", "QUEUED", "SENT", "FAILED", "SUPPRESSED"
    sent_at: Optional[str] = None
    created_at: str = field(default_factory=get_utc_now)
