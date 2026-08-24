import os
import yaml
from dataclasses import dataclass
from typing import Dict, Any, List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PipelineConfig:
    daily_processing_cap: int = 500
    crawling_daily_cap: int = 500
    sending_daily_cap: int = 30
    domain_request_delay_min: float = 3.0
    domain_request_delay_max: float = 8.0
    request_timeout_seconds: int = 10
    user_agent: str = "UKCareHomeOutreachBot/1.0 (+https://example.co.uk/bot-info)"


@dataclass
class SearchAPIConfig:
    provider: str = "duckduckgo"  # "duckduckgo", "google", "bing", or "mock"
    google_api_key: str = ""
    google_cse_id: str = ""
    bing_api_key: str = ""
    gemini_api_key: str = ""
    confidence_threshold: float = 0.65


@dataclass
class SenderInfoConfig:
    org_name: str = "HealthTest Solutions UK"
    sender_name: str = "Outreach Operations"
    reply_to: str = "support@example.co.uk"
    unsubscribe_base_url: str = "https://example.co.uk/optout"


@dataclass
class SMTPConfig:
    mode: str = "resend"  # "resend", "smtp", "dry_run", "mock"
    resend_api_key: str = ""
    resend_from_email: str = "onboarding@resend.dev"
    host: str = "smtp.gmail.com"
    port: int = 587
    user: str = ""
    password: str = ""


@dataclass
class EmailTemplateConfig:
    subject: str = "NHS-Partnered Resident Covid Testing Support for {care_home_name}"
    body_template: str = ""


class AppConfig:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.database_path: str = "data/carehomes_pipeline.db"
        self.pipeline = PipelineConfig()
        self.search_api = SearchAPIConfig()
        self.sender_info = SenderInfoConfig()
        self.smtp = SMTPConfig()
        self.email_template = EmailTemplateConfig()

        if os.path.exists(config_path):
            self.load_from_yaml(config_path)

        self._override_from_env()

    def load_from_yaml(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "database_path" in data:
            self.database_path = data["database_path"]

        if "pipeline" in data:
            p = data["pipeline"]
            self.pipeline.daily_processing_cap = int(p.get("daily_processing_cap", self.pipeline.daily_processing_cap))
            self.pipeline.crawling_daily_cap = int(p.get("crawling_daily_cap", self.pipeline.crawling_daily_cap))
            self.pipeline.sending_daily_cap = int(p.get("sending_daily_cap", self.pipeline.sending_daily_cap))
            self.pipeline.domain_request_delay_min = float(p.get("domain_request_delay_min", self.pipeline.domain_request_delay_min))
            self.pipeline.domain_request_delay_max = float(p.get("domain_request_delay_max", self.pipeline.domain_request_delay_max))
            self.pipeline.request_timeout_seconds = int(p.get("request_timeout_seconds", self.pipeline.request_timeout_seconds))
            self.pipeline.user_agent = p.get("user_agent", self.pipeline.user_agent)

        if "search_api" in data:
            s = data["search_api"]
            self.search_api.provider = s.get("provider", self.search_api.provider)
            self.search_api.google_api_key = s.get("google_api_key", self.search_api.google_api_key)
            self.search_api.google_cse_id = s.get("google_cse_id", self.search_api.google_cse_id)
            self.search_api.bing_api_key = s.get("bing_api_key", self.search_api.bing_api_key)
            self.search_api.gemini_api_key = s.get("gemini_api_key", self.search_api.gemini_api_key)
            self.search_api.confidence_threshold = float(s.get("confidence_threshold", self.search_api.confidence_threshold))

        if "sender_info" in data:
            snd = data["sender_info"]
            self.sender_info.org_name = snd.get("org_name", self.sender_info.org_name)
            self.sender_info.sender_name = snd.get("sender_name", self.sender_info.sender_name)
            self.sender_info.reply_to = snd.get("reply_to", self.sender_info.reply_to)
            self.sender_info.unsubscribe_base_url = snd.get("unsubscribe_base_url", self.sender_info.unsubscribe_base_url)

        if "smtp_settings" in data:
            st = data["smtp_settings"]
            self.smtp.mode = st.get("mode", self.smtp.mode)
            self.smtp.resend_api_key = st.get("resend_api_key", self.smtp.resend_api_key)
            self.smtp.resend_from_email = st.get("resend_from_email", self.smtp.resend_from_email)
            self.smtp.host = st.get("host", self.smtp.host)
            self.smtp.port = int(st.get("port", self.smtp.port))
            self.smtp.user = st.get("user", self.smtp.user)
            self.smtp.password = st.get("password", self.smtp.password)

        if "email_template" in data:
            tmpl = data["email_template"]
            self.email_template.subject = tmpl.get("subject", self.email_template.subject)
            self.email_template.body_template = tmpl.get("body_template", self.email_template.body_template)

    def _override_from_env(self):
        if os.getenv("SEARCH_API_PROVIDER"):
            self.search_api.provider = os.getenv("SEARCH_API_PROVIDER")
        if os.getenv("GOOGLE_API_KEY"):
            self.search_api.google_api_key = os.getenv("GOOGLE_API_KEY")
        if os.getenv("GOOGLE_CSE_ID"):
            self.search_api.google_cse_id = os.getenv("GOOGLE_CSE_ID")
        if os.getenv("BING_API_KEY"):
            self.search_api.bing_api_key = os.getenv("BING_API_KEY")
        if "GEMINI_API_KEY" in os.environ:
            self.search_api.gemini_api_key = os.environ["GEMINI_API_KEY"]
        if "RESEND_API_KEY" in os.environ:
            self.smtp.resend_api_key = os.environ["RESEND_API_KEY"]
        if "RESEND_FROM_EMAIL" in os.environ:
            self.smtp.resend_from_email = os.environ["RESEND_FROM_EMAIL"]
        if os.getenv("SMTP_MODE"):
            self.smtp.mode = os.getenv("SMTP_MODE")
        if os.getenv("SMTP_HOST"):
            self.smtp.host = os.getenv("SMTP_HOST")
        if os.getenv("SMTP_PORT"):
            self.smtp.port = int(os.getenv("SMTP_PORT"))
        if os.getenv("SMTP_USER"):
            self.smtp.user = os.getenv("SMTP_USER")
        if os.getenv("SMTP_PASS"):
            self.smtp.password = os.getenv("SMTP_PASS")
        if os.getenv("DAILY_CAP"):
            self.pipeline.daily_processing_cap = int(os.getenv("DAILY_CAP"))
