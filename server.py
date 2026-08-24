from __future__ import annotations
import os
import sys
import logging
import secrets
import hashlib
import base64
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Depends, status, Request, Response, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import AppConfig
from src.db import DatabaseManager
from src.models import StageStatus
from src.stages.stage0_import import Stage0Import
from src.stages.stage1_discovery import Stage1Discovery
from src.stages.stage2_extraction import Stage2Extraction
from src.stages.stage3_personalise import Stage3Personalise
from src.stages.stage4_sending import Stage4Sending
from src.review import ReviewQueueManager

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

SECRET_KEY = os.getenv("SECRET_KEY", "carehomes_super_secret_session_key_2026")


def get_expected_token() -> str:
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")
    return hashlib.sha256(f"{admin_user}:{admin_password}:{SECRET_KEY}".encode()).hexdigest()


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get("auth_token")
    if token and secrets.compare_digest(token, get_expected_token()):
        return True

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode()
            user, pwd = decoded.split(":", 1)
            expected_user = os.getenv("ADMIN_USER", "admin")
            expected_pass = os.getenv("ADMIN_PASSWORD", "admin")
            if secrets.compare_digest(user, expected_user) and secrets.compare_digest(pwd, expected_pass):
                return True
        except Exception:
            pass
    return False


def verify_session(request: Request):
    if request.url.path in ("/login", "/logout", "/favicon.ico"):
        return True

    if not is_authenticated(request):
        if request.url.path.startswith("/api"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        else:
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login"}
            )
    return True


# Pydantic models for REST requests
class WebsiteApprovalRequest(BaseModel):
    override_url: Optional[str] = None


class DraftApprovalRequest(BaseModel):
    approved: bool


class SuppressionRequest(BaseModel):
    email: str
    reason: str = "manual entry"


class ConfigUpdateRequest(BaseModel):
    daily_processing_cap: Optional[int] = None
    search_provider: Optional[str] = None
    google_api_key: Optional[str] = None
    google_cse_id: Optional[str] = None
    bing_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    confidence_threshold: Optional[float] = None
    org_name: Optional[str] = None
    sender_name: Optional[str] = None
    reply_to: Optional[str] = None
    unsubscribe_base_url: Optional[str] = None


class PipelineRunRequest(BaseModel):
    stage: str = "all"
    limit: Optional[int] = None


WebsiteApprovalRequest.model_rebuild()
DraftApprovalRequest.model_rebuild()
SuppressionRequest.model_rebuild()
ConfigUpdateRequest.model_rebuild()
PipelineRunRequest.model_rebuild()


# Global background task status tracker
pipeline_task_status = {
    "is_running": False,
    "current_stage": None,
    "last_result": None,
    "error": None
}


def get_config_and_db():
    cfg = AppConfig("config.yaml")
    db = DatabaseManager(cfg.database_path)
    return cfg, db


app = FastAPI(
    title="UK Care Homes Outreach Pipeline Dashboard",
    dependencies=[Depends(verify_session)]
)


@app.on_event("startup")
def auto_seed_database_on_boot():
    cfg, db = get_config_and_db()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM homes")
        total_homes = cursor.fetchone()["cnt"]
        if total_homes == 0:
            seed_path = "data/cqc_carehomes_seed.csv"
            if not os.path.exists(seed_path) and os.path.exists("Carehomes CQC list.csv"):
                seed_path = "Carehomes CQC list.csv"
            if os.path.exists(seed_path):
                logger.info(f"Empty database detected on boot. Auto-seeding care homes dataset from {seed_path}...")
                stage0 = Stage0Import(cfg, db)
                stage0.run_import(seed_path)


@app.get("/api/stats")
def get_stats():
    cfg, db = get_config_and_db()
    with db.get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as cnt FROM homes")
        total_homes = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM homes WHERE stage_status = ?", (StageStatus.PENDING_DISCOVERY,))
        pending_discovery = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM homes WHERE website_status = 'ACCEPTED' OR website_status = 'MANUAL_APPROVED'")
        websites_accepted = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM homes WHERE website_status = 'NEEDS_MANUAL_REVIEW' OR stage_status = 'MANUAL_REVIEW_NEEDED'")
        websites_review_needed = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM homes WHERE stage_status = ?", (StageStatus.PENDING_EXTRACTION,))
        pending_extraction = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM contacts")
        contacts_extracted = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM email_drafts WHERE approved = 0 AND status = 'DRAFT'")
        pending_drafts = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM email_drafts WHERE approved = 1 AND status = 'QUEUED'")
        approved_drafts = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM email_drafts WHERE status = 'SENT'")
        sent_emails = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM suppression_list")
        suppressed_emails = cursor.fetchone()["cnt"]

        today_s1 = db.get_daily_count("Stage1_Discovery")
        today_s2 = db.get_daily_count("Stage2_Extraction")
        today_s3 = db.get_daily_count("Stage3_Personalise")

    return {
        "total_homes": total_homes,
        "pending_discovery": pending_discovery,
        "websites_accepted": websites_accepted,
        "websites_review_needed": websites_review_needed,
        "pending_extraction": pending_extraction,
        "contacts_extracted": contacts_extracted,
        "pending_drafts": pending_drafts,
        "approved_drafts": approved_drafts,
        "sent_emails": sent_emails,
        "suppressed_emails": suppressed_emails,
        "daily_cap": cfg.pipeline.daily_processing_cap,
        "daily_processed_today": {
            "stage1": today_s1,
            "stage2": today_s2,
            "stage3": today_s3
        },
        "pipeline_running": pipeline_task_status["is_running"]
    }


@app.get("/api/homes")
def get_homes(
    page: int = 1,
    limit: int = 20,
    search: Optional[str] = None,
    status: Optional[str] = None
):
    cfg, db = get_config_and_db()
    offset = (page - 1) * limit
    with db.get_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM homes WHERE 1=1"
        params = []

        if search:
            query += " AND (name LIKE ? OR postcode LIKE ? OR address LIKE ?)"
            s_term = f"%{search}%"
            params.extend([s_term, s_term, s_term])

        if status:
            query += " AND stage_status = ?"
            params.append(status)

        # Count total matching
        count_query = f"SELECT COUNT(*) as cnt FROM ({query})"
        cursor.execute(count_query, params)
        total_count = cursor.fetchone()["cnt"]

        query += " ORDER BY id ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]

    return {
        "page": page,
        "limit": limit,
        "total": total_count,
        "homes": rows
    }


@app.get("/api/reviews/websites")
def get_website_reviews():
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    items = reviewer.get_websites_needing_review()
    return {"items": items}


@app.post("/api/reviews/websites/{home_id}/approve")
def approve_website(home_id: int, req: WebsiteApprovalRequest):
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    try:
        reviewer.approve_website(home_id, req.override_url)
        return {"status": "success", "message": f"Website approved for Home #{home_id}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reviews/websites/{home_id}/reject")
def reject_website(home_id: int):
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    try:
        reviewer.reject_website(home_id)
        return {"status": "success", "message": f"Website rejected for Home #{home_id}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/drafts")
def get_drafts(status: Optional[str] = "DRAFT"):
    cfg, db = get_config_and_db()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT d.*, h.name as home_name, h.postcode, h.address
            FROM email_drafts d
            JOIN homes h ON d.home_id = h.id
        """
        params = []
        if status:
            query += " WHERE d.status = ?"
            params.append(status)
        query += " ORDER BY d.id DESC LIMIT 100"

        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]
    return {"drafts": rows}


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: int):
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    reviewer.approve_draft(draft_id)
    return {"status": "success", "message": f"Draft #{draft_id} approved"}


@app.post("/api/drafts/approve-all")
def approve_all_drafts():
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    count = reviewer.approve_all_drafts()
    return {"status": "success", "approved_count": count}


@app.post("/api/drafts/{draft_id}/reject")
def reject_draft(draft_id: int):
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    reviewer.reject_draft(draft_id)
    return {"status": "success", "message": f"Draft #{draft_id} rejected"}


@app.get("/api/suppression")
def get_suppression_list():
    cfg, db = get_config_and_db()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM suppression_list ORDER BY added_at DESC")
        rows = [dict(r) for r in cursor.fetchall()]
    return {"suppression": rows}


@app.post("/api/suppression")
def add_suppression(req: SuppressionRequest):
    cfg, db = get_config_and_db()
    db.add_suppression(req.email, req.reason)
    return {"status": "success", "message": f"Added {req.email} to suppression list"}


@app.get("/api/logs")
def get_audit_logs(limit: int = 50):
    cfg, db = get_config_and_db()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.*, h.name as home_name
            FROM audit_logs l
            LEFT JOIN homes h ON l.home_id = h.id
            ORDER BY l.id DESC LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cursor.fetchall()]
    return {"logs": rows}


@app.get("/api/config")
def get_config():
    cfg = AppConfig("config.yaml")
    return {
        "daily_processing_cap": cfg.pipeline.daily_processing_cap,
        "domain_request_delay_min": cfg.pipeline.domain_request_delay_min,
        "domain_request_delay_max": cfg.pipeline.domain_request_delay_max,
        "user_agent": cfg.pipeline.user_agent,
        "search_provider": cfg.search_api.provider,
        "google_api_key_set": bool(cfg.search_api.google_api_key),
        "google_cse_id_set": bool(cfg.search_api.google_cse_id),
        "bing_api_key_set": bool(cfg.search_api.bing_api_key),
        "gemini_api_key_set": bool(cfg.search_api.gemini_api_key),
        "confidence_threshold": cfg.search_api.confidence_threshold,
        "org_name": cfg.sender_info.org_name,
        "sender_name": cfg.sender_info.sender_name,
        "reply_to": cfg.sender_info.reply_to,
        "unsubscribe_base_url": cfg.sender_info.unsubscribe_base_url,
        "email_subject": cfg.email_template.subject,
        "email_body_template": cfg.email_template.body_template
    }


def run_pipeline_task(stage: str, limit: Optional[int]):
    global pipeline_task_status
    pipeline_task_status["is_running"] = True
    pipeline_task_status["current_stage"] = stage
    pipeline_task_status["error"] = None

    try:
        cfg = AppConfig("config.yaml")
        db = DatabaseManager(cfg.database_path)

        results = {}

        if stage in ("1", "crawler", "all"):
            c_limit = limit or cfg.pipeline.crawling_daily_cap
            s1 = Stage1Discovery(cfg, db)
            results["stage1"] = s1.run(max_items=c_limit)

        if stage in ("2", "crawler", "all"):
            c_limit = limit or cfg.pipeline.crawling_daily_cap
            s2 = Stage2Extraction(cfg, db)
            results["stage2"] = s2.run(max_items=c_limit)

        if stage in ("3", "outreach", "all"):
            s_limit = limit or cfg.pipeline.sending_daily_cap
            s3 = Stage3Personalise(cfg, db)
            results["stage3"] = s3.run(max_items=s_limit)

        if stage in ("4", "outreach"):
            s_limit = limit or cfg.pipeline.sending_daily_cap
            s4 = Stage4Sending(cfg, db, transport_mode="dry_run")
            results["stage4"] = s4.run(max_items=s_limit)

        pipeline_task_status["last_result"] = results
        logger.info(f"Background pipeline run finished: {results}")

    except Exception as e:
        logger.error(f"Error running pipeline stage {stage}: {e}")
        pipeline_task_status["error"] = str(e)
    finally:
        pipeline_task_status["is_running"] = False


@app.post("/api/pipeline/run")
def trigger_pipeline_run(req: PipelineRunRequest, background_tasks: BackgroundTasks):
    global pipeline_task_status
    if pipeline_task_status["is_running"]:
        raise HTTPException(status_code=400, detail="Pipeline is already running.")

    background_tasks.add_task(run_pipeline_task, req.stage, req.limit)
    return {"status": "started", "message": f"Triggered pipeline stage '{req.stage}' run in background."}


@app.post("/api/pipeline/reset-discovery")
def reset_discovery():
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    count = reviewer.reset_discovered_websites()
    return {"status": "success", "message": f"Reset {count} discovered care home websites back to pending status for clean re-discovery."}


@app.get("/api/reviews/unfound")
def get_unfound_reviews():
    cfg, db = get_config_and_db()
    unfound_list = db.get_unfound_carehomes()
    return {"unfound": unfound_list}


@app.get("/api/export/drafts")
def export_drafts():
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    output_path = "data/review_queue_export.csv"
    reviewer.export_drafts_to_csv(output_path)
    return FileResponse(output_path, media_type="text/csv", filename="email_drafts_review_queue.csv")


@app.get("/api/export/unfound")
def export_unfound():
    cfg, db = get_config_and_db()
    reviewer = ReviewQueueManager(db)
    output_path = "data/unfound_carehomes_review.csv"
    reviewer.export_unfound_to_csv(output_path)
    return FileResponse(output_path, media_type="text/csv", filename="unfound_carehomes_review.csv")


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(content=render_login_page(), status_code=200)


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    expected_user = os.getenv("ADMIN_USER", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD", "admin")

    if secrets.compare_digest(username.strip(), expected_user) and secrets.compare_digest(password.strip(), expected_pass):
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key="auth_token",
            value=get_expected_token(),
            max_age=86400,
            httponly=True,
            samesite="lax"
        )
        return response
    else:
        return HTMLResponse(content=render_login_page(error_message="Invalid username or password. Please try again."), status_code=401)


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="auth_token")
    return response


@app.get("/", response_class=HTMLResponse)
def index_html(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return HTML_CONTENT


LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - UK Care Homes Outreach</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #f3f4f6; }
        .glass-card { background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.1); }
        .accent-gradient { background: linear-gradient(135deg, #6366f1 0%, #06b6d4 100%); }
        .accent-text { background: linear-gradient(135deg, #818cf8 0%, #22d3ee 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
    <!-- Animated background glow elements -->
    <div class="absolute -top-32 -left-32 w-96 h-96 bg-indigo-600/20 rounded-full blur-3xl"></div>
    <div class="absolute -bottom-32 -right-32 w-96 h-96 bg-cyan-600/20 rounded-full blur-3xl"></div>

    <div class="max-w-md w-full glass-card p-8 rounded-3xl shadow-2xl space-y-8 relative z-10">
        <!-- Logo & Header -->
        <div class="text-center space-y-3">
            <div class="w-16 h-16 rounded-2xl accent-gradient flex items-center justify-center font-extrabold text-white text-2xl mx-auto shadow-lg shadow-indigo-500/30">
                NHS
            </div>
            <h1 class="text-2xl font-extrabold tracking-tight accent-text">UK Care Homes Outreach</h1>
            <p class="text-xs text-slate-400">Enter admin credentials to access pipeline dashboard</p>
        </div>

        <!-- Login Form -->
        <form action="/login" method="POST" class="space-y-5">
            {error_html}

            <div class="space-y-1">
                <label class="text-xs font-semibold text-slate-300">Username</label>
                <div class="relative">
                    <input type="text" name="username" placeholder="admin" required class="w-full bg-slate-900/80 border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-indigo-500 transition pl-10">
                    <span class="absolute left-3.5 top-3.5 text-slate-400">👤</span>
                </div>
            </div>

            <div class="space-y-1">
                <label class="text-xs font-semibold text-slate-300">Password</label>
                <div class="relative">
                    <input type="password" id="password-input" name="password" placeholder="••••••••" required class="w-full bg-slate-900/80 border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-indigo-500 transition pl-10 pr-10">
                    <span class="absolute left-3.5 top-3.5 text-slate-400">🔒</span>
                    <button type="button" onclick="togglePasswordVisibility()" class="absolute right-3.5 top-3.5 text-slate-400 hover:text-white text-xs">👁️</button>
                </div>
            </div>

            <button type="submit" class="w-full py-3.5 px-4 text-sm font-bold text-white accent-gradient rounded-xl shadow-lg shadow-indigo-500/25 hover:opacity-95 transition transform active:scale-95">
                Sign In to Dashboard
            </button>
        </form>

        <div class="text-center border-t border-slate-800 pt-4">
            <p class="text-[11px] text-slate-500">PECR Compliant B2B Outreach Engine • HealthTest Solutions UK</p>
        </div>
    </div>

    <script>
        function togglePasswordVisibility() {
            const pwd = document.getElementById('password-input');
            pwd.type = pwd.type === 'password' ? 'text' : 'password';
        }
    </script>
</body>
</html>
"""


def render_login_page(error_message: Optional[str] = None) -> str:
    if error_message:
        err_html = f'''
        <div class="p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs font-medium flex items-center space-x-2">
            <span>⚠️</span>
            <span>{error_message}</span>
        </div>
        '''
    else:
        err_html = ""
    return LOGIN_HTML_TEMPLATE.replace("{error_html}", err_html)


# HTML Dashboard UI Code
HTML_CONTENT = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UK Care Homes Outreach Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #f3f4f6; }
        .glass-panel { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .accent-gradient { background: linear-gradient(135deg, #6366f1 0%, #06b6d4 100%); }
        .accent-text { background: linear-gradient(135deg, #818cf8 0%, #22d3ee 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    </style>
</head>
<body class="min-h-screen flex flex-col">

    <!-- Top Navigation Bar -->
    <header class="glass-panel border-b sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-xl accent-gradient flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
                    NHS
                </div>
                <div>
                    <h1 class="text-xl font-bold tracking-tight accent-text">UK Care Homes Outreach</h1>
                    <p class="text-xs text-slate-400">NHS-Partnered Covid Testing Outreach Pipeline</p>
                </div>
            </div>

            <!-- Tab Navigation Links -->
            <nav class="flex items-center space-x-2 bg-slate-900/60 p-1.5 rounded-xl border border-slate-800">
                <button onclick="switchTab('overview')" id="tab-overview" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg bg-indigo-600 text-white shadow-sm transition">
                    📊 Overview
                </button>
                <button onclick="switchTab('website-reviews')" id="tab-website-reviews" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg text-slate-300 hover:text-white transition">
                    🔍 Website Reviews <span id="badge-websites-review" class="ml-1 px-2 py-0.5 text-xs bg-amber-500/20 text-amber-300 rounded-full font-semibold">0</span>
                </button>
                <button onclick="switchTab('drafts')" id="tab-drafts" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg text-slate-300 hover:text-white transition">
                    ✉️ Email Drafts <span id="badge-drafts-pending" class="ml-1 px-2 py-0.5 text-xs bg-indigo-500/20 text-indigo-300 rounded-full font-semibold">0</span>
                </button>
                <button onclick="switchTab('unfound')" id="tab-unfound" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg text-slate-300 hover:text-white transition">
                    📂 Unfound List <span id="badge-unfound-count" class="ml-1 px-2 py-0.5 text-xs bg-rose-500/20 text-rose-300 rounded-full font-semibold">0</span>
                </button>
                <button onclick="switchTab('suppression')" id="tab-suppression" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg text-slate-300 hover:text-white transition">
                    🛑 Opt-Out / Suppression
                </button>
                <button onclick="switchTab('logs')" id="tab-logs" class="tab-btn px-4 py-2 text-sm font-medium rounded-lg text-slate-300 hover:text-white transition">
                    📜 Audit Logs
                </button>
            </nav>

            <div class="flex items-center space-x-2">
                <button onclick="resetDiscovery()" title="Reset all discovered websites back to pending for clean re-discovery with strict HTTP verification" class="px-3 py-2 text-xs font-semibold text-amber-300 hover:text-white bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30 rounded-xl transition flex items-center space-x-1">
                    <span>🔄 Reset Discovery</span>
                </button>
                <button onclick="triggerRunPipeline('crawler')" id="btn-run-crawler" class="px-4 py-2 text-xs font-semibold text-white bg-gradient-to-r from-indigo-600 to-cyan-600 hover:from-indigo-500 hover:to-cyan-500 rounded-xl shadow-lg shadow-indigo-500/20 transition flex items-center space-x-2">
                    <span>🕷️ Run Data Crawler (500/day)</span>
                </button>
                <button onclick="triggerRunPipeline('outreach')" id="btn-run-outreach" class="px-4 py-2 text-xs font-semibold text-white bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 rounded-xl shadow-lg shadow-emerald-500/20 transition flex items-center space-x-2">
                    <span>✉️ Send Outreach Emails (30/day)</span>
                </button>
                <a href="/logout" class="px-3 py-2 text-xs font-semibold text-rose-300 hover:text-white bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 rounded-xl transition flex items-center space-x-1">
                    <span>🚪 Logout</span>
                </a>
            </div>
        </div>
    </header>

    <!-- Main Content Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto px-6 py-8">

        <!-- TAB 1: OVERVIEW & METRICS -->
        <div id="section-overview" class="tab-content space-y-8">
            <!-- Key Metric Cards Grid -->
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                <!-- Total Care Homes Card -->
                <div class="glass-panel p-6 rounded-2xl shadow-xl">
                    <div class="flex items-center justify-between">
                        <span class="text-sm font-medium text-slate-400">Total Imported Care Homes</span>
                        <span class="p-2 rounded-xl bg-indigo-500/10 text-indigo-400">🏥</span>
                    </div>
                    <div class="mt-4 flex items-baseline justify-between">
                        <span id="stat-total-homes" class="text-3xl font-extrabold text-white">0</span>
                        <span class="text-xs text-slate-400">CQC Dataset</span>
                    </div>
                </div>

                <!-- Discovered Websites Card -->
                <div class="glass-panel p-6 rounded-2xl shadow-xl">
                    <div class="flex items-center justify-between">
                        <span class="text-sm font-medium text-slate-400">Official Websites Verified</span>
                        <span class="p-2 rounded-xl bg-cyan-500/10 text-cyan-400">🌐</span>
                    </div>
                    <div class="mt-4 flex items-baseline justify-between">
                        <span id="stat-websites-accepted" class="text-3xl font-extrabold text-white">0</span>
                        <span id="stat-websites-pending" class="text-xs text-amber-400 font-medium">0 needing review</span>
                    </div>
                </div>

                <!-- Email Drafts Queue Card -->
                <div class="glass-panel p-6 rounded-2xl shadow-xl">
                    <div class="flex items-center justify-between">
                        <span class="text-sm font-medium text-slate-400">Email Review Queue</span>
                        <span class="p-2 rounded-xl bg-purple-500/10 text-purple-400">✉️</span>
                    </div>
                    <div class="mt-4 flex items-baseline justify-between">
                        <span id="stat-pending-drafts" class="text-3xl font-extrabold text-white">0</span>
                        <span id="stat-approved-drafts" class="text-xs text-emerald-400 font-medium">0 approved</span>
                    </div>
                </div>

                <!-- Daily Cap Usage Card -->
                <div class="glass-panel p-6 rounded-2xl shadow-xl">
                    <div class="flex items-center justify-between">
                        <span class="text-sm font-medium text-slate-400">Daily Processing Cap</span>
                        <span class="p-2 rounded-xl bg-emerald-500/10 text-emerald-400">⚡</span>
                    </div>
                    <div class="mt-4 flex items-baseline justify-between">
                        <span id="stat-daily-progress" class="text-3xl font-extrabold text-white">0 / 45</span>
                        <span class="text-xs text-slate-400">Polite Limit</span>
                    </div>
                </div>
            </div>

            <!-- Pipeline Controls & Progress Panel -->
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-6">
                <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-lg font-bold text-white">Pipeline Execution Controls</h2>
                        <p class="text-xs text-slate-400">Execute pipeline stages with rate-limiting, robots.txt compliance & checkpointing</p>
                    </div>
                    <div id="pipeline-status-indicator" class="flex items-center space-x-2 text-sm font-medium text-slate-400">
                        <span class="w-2.5 h-2.5 rounded-full bg-slate-500"></span>
                        <span>Idle</span>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <!-- Button 1: Data Crawler -->
                    <button onclick="triggerRunPipeline('crawler')" class="p-6 rounded-2xl bg-slate-900/80 border border-indigo-500/30 hover:border-indigo-500 text-left transition space-y-3 shadow-xl group">
                        <div class="flex items-center justify-between">
                            <span class="text-lg font-bold text-white group-hover:text-indigo-400 transition flex items-center space-x-2">
                                <span>🕷️ 1. Run Data Crawler</span>
                            </span>
                            <span class="text-xs px-2.5 py-1 rounded-full bg-indigo-500/20 text-indigo-300 font-semibold border border-indigo-500/30">Scrape 500/day</span>
                        </div>
                        <p class="text-xs text-slate-300">Executes Stage 1 Web Discovery & Stage 2 Contact Extraction. Discovers websites via DuckDuckGo/Gemini and extracts published contact emails from care home sites.</p>
                        <div class="pt-2 text-xs font-semibold text-indigo-400 group-hover:underline flex items-center space-x-1">
                            <span>Start Data Scraping Run &rarr;</span>
                        </div>
                    </button>

                    <!-- Button 2: Outreach Sender -->
                    <button onclick="triggerRunPipeline('outreach')" class="p-6 rounded-2xl bg-slate-900/80 border border-emerald-500/30 hover:border-emerald-500 text-left transition space-y-3 shadow-xl group">
                        <div class="flex items-center justify-between">
                            <span class="text-lg font-bold text-white group-hover:text-emerald-400 transition flex items-center space-x-2">
                                <span>✉️ 2. Review & Send Outreach</span>
                            </span>
                            <span class="text-xs px-2.5 py-1 rounded-full bg-emerald-500/20 text-emerald-300 font-semibold border border-emerald-500/30">Send 30/day</span>
                        </div>
                        <p class="text-xs text-slate-300">Executes Stage 3 AI Email Personalization & Stage 4 Controlled Dispatch. Generates custom PECR emails with Gemini AI and dispatches 30 emails per day from your approved queue.</p>
                        <div class="pt-2 text-xs font-semibold text-emerald-400 group-hover:underline flex items-center space-x-1">
                            <span>Start Email Personalize & Send Run &rarr;</span>
                        </div>
                    </button>
                </div>
            </div>

            <!-- Recent Activity Audit Feed -->
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4">
                <div class="flex items-center justify-between">
                    <h3 class="text-md font-bold text-white">Live Pipeline Audit Feed</h3>
                    <button onclick="loadLogs()" class="text-xs text-indigo-400 hover:text-indigo-300 font-medium">Refresh Logs</button>
                </div>
                <div id="live-audit-feed" class="space-y-3 max-h-60 overflow-y-auto pr-2 font-mono text-xs">
                    <p class="text-slate-500">Loading logs...</p>
                </div>
            </div>
        </div>

        <!-- TAB 2: WEBSITE MANUAL REVIEWS -->
        <div id="section-website-reviews" class="tab-content hidden space-y-6">
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4">
                <div>
                    <h2 class="text-lg font-bold text-white">Stage 1 Discovered Websites Needing Manual Review</h2>
                    <p class="text-xs text-slate-400">Candidates with confidence scores below threshold (&lt; 0.65) or third-party directory flags</p>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-300">
                        <thead class="bg-slate-900/80 text-xs uppercase text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="p-3">ID</th>
                                <th class="p-3">Care Home Name</th>
                                <th class="p-3">Postcode</th>
                                <th class="p-3">Discovered Website Candidate</th>
                                <th class="p-3">Confidence Score</th>
                                <th class="p-3">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="website-review-table-body" class="divide-y divide-slate-800/50">
                            <tr><td colspan="6" class="p-4 text-center text-slate-500">Loading website reviews...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 3: EMAIL DRAFTS REVIEW QUEUE -->
        <div id="section-drafts" class="tab-content hidden space-y-6">
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4">
                <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-lg font-bold text-white">Stage 3 Email Outreach Review Queue</h2>
                        <p class="text-xs text-slate-400">PECR-compliant drafts generated for outreach (held for manual approval before sending)</p>
                    </div>
                    <div class="flex items-center space-x-3">
                        <a href="/api/export/drafts" target="_blank" class="px-3 py-1.5 text-xs font-semibold rounded-lg bg-slate-800 text-slate-200 border border-slate-700 hover:bg-slate-700 transition">
                            📥 Export CSV
                        </a>
                        <button onclick="approveAllDrafts()" class="px-4 py-1.5 text-xs font-semibold rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-sm transition">
                            ✅ Approve All Pending Drafts
                        </button>
                    </div>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-300">
                        <thead class="bg-slate-900/80 text-xs uppercase text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="p-3">Draft ID</th>
                                <th class="p-3">Care Home</th>
                                <th class="p-3">Recipient Email</th>
                                <th class="p-3">Subject Line</th>
                                <th class="p-3">Approval Status</th>
                                <th class="p-3">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="drafts-table-body" class="divide-y divide-slate-800/50">
                            <tr><td colspan="6" class="p-4 text-center text-slate-500">Loading email drafts...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 4: SUPPRESSION & PECR OPT-OUT -->
        <div id="section-suppression" class="tab-content hidden space-y-6">
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-6">
                <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-lg font-bold text-white">PECR Opt-Out & Suppression List</h2>
                        <p class="text-xs text-slate-400">Emails in this list are automatically blocked from receiving outreach drafts or emails</p>
                    </div>
                </div>

                <!-- Add Suppression Form -->
                <form onsubmit="handleAddSuppression(event)" class="flex items-center space-x-3 bg-slate-900/60 p-4 rounded-xl border border-slate-800">
                    <input type="email" id="suppress-email-input" placeholder="enter email address to suppress..." required class="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-4 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                    <input type="text" id="suppress-reason-input" placeholder="reason (e.g. opt-out request)" class="w-64 bg-slate-800 border border-slate-700 rounded-lg px-4 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                    <button type="submit" class="px-4 py-2 text-sm font-semibold bg-red-600 hover:bg-red-500 text-white rounded-lg transition">
                        Add to Suppression List
                    </button>
                </form>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-300">
                        <thead class="bg-slate-900/80 text-xs uppercase text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="p-3">ID</th>
                                <th class="p-3">Suppressed Email</th>
                                <th class="p-3">Reason</th>
                                <th class="p-3">Added Date</th>
                            </tr>
                        </thead>
                        <tbody id="suppression-table-body" class="divide-y divide-slate-800/50">
                            <tr><td colspan="4" class="p-4 text-center text-slate-500">Loading suppression list...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB: UNFOUND CARE HOMES FALLBACK LIST -->
        <div id="section-unfound" class="tab-content hidden space-y-6">
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4">
                <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-lg font-bold text-white">Unfound Care Homes Review Queue</h2>
                        <p class="text-xs text-slate-400">Care homes where website discovery or contact extraction failed, kept aside for manual review so zero care homes are missed</p>
                    </div>
                    <div>
                        <a href="/api/export/unfound" target="_blank" class="px-4 py-2 text-xs font-semibold rounded-xl bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-500/20 transition flex items-center space-x-2">
                            <span>📥 Export Unfound List CSV</span>
                        </a>
                    </div>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-300">
                        <thead class="bg-slate-900/80 text-xs uppercase text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="p-3">CQC ID</th>
                                <th class="p-3">Care Home Name</th>
                                <th class="p-3">Postcode</th>
                                <th class="p-3">Address</th>
                                <th class="p-3">Discovered URL / Status</th>
                                <th class="p-3">Reason / Missing Data</th>
                                <th class="p-3">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="unfound-table-body" class="divide-y divide-slate-800/50">
                            <tr><td colspan="7" class="p-4 text-center text-slate-500">Loading unfound care homes...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 5: AUDIT LOGS -->
        <div id="section-logs" class="tab-content hidden space-y-6">
            <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4">
                <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-lg font-bold text-white">Full Pipeline Audit Log History</h2>
                        <p class="text-xs text-slate-400">Complete log of all discovery, crawl, rate limit, draft & suppression decisions</p>
                    </div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs font-mono text-slate-300">
                        <thead class="bg-slate-900/80 uppercase text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="p-3">Time</th>
                                <th class="p-3">Stage</th>
                                <th class="p-3">Action</th>
                                <th class="p-3">Care Home</th>
                                <th class="p-3">Details / Message</th>
                            </tr>
                        </thead>
                        <tbody id="full-logs-table-body" class="divide-y divide-slate-800/50">
                            <tr><td colspan="5" class="p-4 text-center text-slate-500">Loading logs...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

    </main>

    <!-- Modal for Previewing Full Email Draft -->
    <div id="email-preview-modal" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 z-50 hidden">
        <div class="glass-panel max-w-2xl w-full rounded-2xl p-6 space-y-4 border border-slate-700 shadow-2xl">
            <div class="flex items-center justify-between border-b border-slate-800 pb-3">
                <h3 class="text-lg font-bold text-white" id="modal-subject">Email Subject</h3>
                <button onclick="closeModal()" class="text-slate-400 hover:text-white font-bold text-lg">&times;</button>
            </div>
            <div class="text-xs text-slate-400 space-y-1">
                <p><strong>To:</strong> <span id="modal-recipient" class="text-indigo-300"></span></p>
                <p><strong>Care Home:</strong> <span id="modal-home-name" class="text-white"></span></p>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 font-sans text-sm text-slate-200 whitespace-pre-wrap max-h-96 overflow-y-auto" id="modal-body">
            </div>
            <div class="flex justify-end space-x-3 pt-2">
                <button onclick="closeModal()" class="px-4 py-2 text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg">Close</button>
            </div>
        </div>
    </div>

    <!-- Frontend Script Logic -->
    <script>
        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.tab-btn').forEach(el => {
                el.classList.remove('bg-indigo-600', 'text-white');
                el.classList.add('text-slate-300');
            });

            document.getElementById(`section-${tabId}`).classList.remove('hidden');
            const activeBtn = document.getElementById(`tab-${tabId}`);
            if (activeBtn) {
                activeBtn.classList.add('bg-indigo-600', 'text-white');
                activeBtn.classList.remove('text-slate-300');
            }

            if (tabId === 'overview') loadStats();
            if (tabId === 'website-reviews') loadWebsiteReviews();
            if (tabId === 'drafts') loadDrafts();
            if (tabId === 'unfound') loadUnfound();
            if (tabId === 'suppression') loadSuppression();
            if (tabId === 'logs') loadFullLogs();
        }

        async function loadStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();

                document.getElementById('stat-total-homes').innerText = data.total_homes.toLocaleString();
                document.getElementById('stat-websites-accepted').innerText = data.websites_accepted.toLocaleString();
                document.getElementById('stat-websites-pending').innerText = `${data.websites_review_needed} needing review`;
                document.getElementById('stat-pending-drafts').innerText = data.pending_drafts.toLocaleString();
                document.getElementById('stat-approved-drafts').innerText = `${data.approved_drafts} approved`;
                
                const totalToday = (data.daily_processed_today.stage1 || 0) + (data.daily_processed_today.stage2 || 0);
                document.getElementById('stat-daily-progress').innerText = `${totalToday} / ${data.daily_cap}`;

                document.getElementById('badge-websites-review').innerText = data.websites_review_needed;
                document.getElementById('badge-drafts-pending').innerText = data.pending_drafts;
                loadUnfound();

                const indicator = document.getElementById('pipeline-status-indicator');
                if (data.pipeline_running) {
                    indicator.innerHTML = `<span class="w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping"></span><span class="text-amber-400 font-semibold">Running...</span>`;
                } else {
                    indicator.innerHTML = `<span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span><span class="text-slate-400">Idle</span>`;
                }
            } catch (err) {
                console.error("Error loading stats:", err);
            }
        }

        async function loadLogs() {
            try {
                const res = await fetch('/api/logs?limit=15');
                const data = await res.json();
                const container = document.getElementById('live-audit-feed');
                if (!data.logs.length) {
                    container.innerHTML = '<p class="text-slate-500">No logs recorded yet.</p>';
                    return;
                }
                container.innerHTML = data.logs.map(log => `
                    <div class="p-2 rounded bg-slate-900/70 border border-slate-800/80 flex items-start space-x-2">
                        <span class="text-slate-500 shrink-0">${log.timestamp.substring(11, 19)}</span>
                        <span class="px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300 text-[10px] font-semibold">${log.stage}</span>
                        <span class="text-slate-200 flex-1">${log.message}</span>
                    </div>
                `).join('');
            } catch (err) {
                console.error("Error loading logs:", err);
            }
        }

        async function loadWebsiteReviews() {
            try {
                const res = await fetch('/api/reviews/websites');
                const data = await res.json();
                const tbody = document.getElementById('website-review-table-body');

                if (!data.items.length) {
                    tbody.innerHTML = '<tr><td colspan="6" class="p-6 text-center text-slate-400">🎉 No websites currently requiring manual review! All high-confidence matches auto-accepted.</td></tr>';
                    return;
                }

                tbody.innerHTML = data.items.map(item => `
                    <tr class="hover:bg-slate-900/40">
                        <td class="p-3 text-slate-400">#${item.id}</td>
                        <td class="p-3 font-semibold text-white">${item.name}</td>
                        <td class="p-3 text-slate-400">${item.postcode}</td>
                        <td class="p-3">
                            <a href="${item.discovered_website}" target="_blank" class="text-cyan-400 underline hover:text-cyan-300 font-mono text-xs">${item.discovered_website || 'None'}</a>
                        </td>
                        <td class="p-3">
                            <span class="px-2 py-1 rounded text-xs font-semibold ${item.website_confidence >= 0.5 ? 'bg-amber-500/20 text-amber-300' : 'bg-red-500/20 text-red-300'}">
                                ${(item.website_confidence * 100).toFixed(0)}% Match
                            </span>
                        </td>
                        <td class="p-3 space-x-2">
                            <button onclick="approveWebsite(${item.id})" class="px-3 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold shadow transition">
                                Approve
                            </button>
                            <button onclick="rejectWebsite(${item.id})" class="px-3 py-1 bg-red-600 hover:bg-red-500 text-white rounded text-xs font-semibold shadow transition">
                                Reject
                            </button>
                        </td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        async function approveWebsite(id) {
            await fetch(`/api/reviews/websites/${id}/approve`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) });
            loadWebsiteReviews();
            loadStats();
        }

        async function rejectWebsite(id) {
            await fetch(`/api/reviews/websites/${id}/reject`, { method: 'POST' });
            loadWebsiteReviews();
            loadStats();
        }

        async function loadDrafts() {
            try {
                const res = await fetch('/api/drafts?status=DRAFT');
                const data = await res.json();
                const tbody = document.getElementById('drafts-table-body');

                if (!data.drafts.length) {
                    tbody.innerHTML = '<tr><td colspan="6" class="p-6 text-center text-slate-400">No pending email drafts in queue. Run Stage 3 to generate drafts.</td></tr>';
                    return;
                }

                window.draftsData = {};
                data.drafts.forEach(d => window.draftsData[d.id] = d);

                tbody.innerHTML = data.drafts.map(d => `
                    <tr class="hover:bg-slate-900/40">
                        <td class="p-3 text-slate-400">#${d.id}</td>
                        <td class="p-3 font-semibold text-white">${d.home_name} <span class="text-xs text-slate-400">(${d.postcode})</span></td>
                        <td class="p-3 text-indigo-300 font-mono text-xs">${d.recipient_email}</td>
                        <td class="p-3 text-slate-200">${d.subject}</td>
                        <td class="p-3">
                            <span class="px-2 py-1 rounded text-xs font-semibold ${d.approved === 1 ? 'bg-emerald-500/20 text-emerald-300' : 'bg-indigo-500/20 text-indigo-300'}">
                                ${d.approved === 1 ? 'Approved' : 'Pending Review'}
                            </span>
                        </td>
                        <td class="p-3 space-x-2">
                            <button onclick="previewDraft(${d.id})" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded text-xs font-medium">Preview</button>
                            <button onclick="approveDraft(${d.id})" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold">Approve</button>
                            <button onclick="rejectDraft(${d.id})" class="px-2.5 py-1 bg-red-600 hover:bg-red-500 text-white rounded text-xs font-semibold">Reject</button>
                        </td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        function previewDraft(id) {
            const d = window.draftsData[id];
            if (!d) return;
            document.getElementById('modal-subject').innerText = d.subject;
            document.getElementById('modal-recipient').innerText = `${d.recipient_name} <${d.recipient_email}>`;
            document.getElementById('modal-home-name').innerText = `${d.home_name} (${d.postcode})`;
            document.getElementById('modal-body').innerText = d.body_text;
            document.getElementById('email-preview-modal').classList.remove('hidden');
        }

        function closeModal() {
            document.getElementById('email-preview-modal').classList.add('hidden');
        }

        async function approveDraft(id) {
            await fetch(`/api/drafts/${id}/approve`, { method: 'POST' });
            loadDrafts();
            loadStats();
        }

        async function approveAllDrafts() {
            if (!confirm('Approve all pending drafts for sending queue?')) return;
            await fetch('/api/drafts/approve-all', { method: 'POST' });
            loadDrafts();
            loadStats();
        }

        async function rejectDraft(id) {
            await fetch(`/api/drafts/${id}/reject`, { method: 'POST' });
            loadDrafts();
            loadStats();
        }

        async function loadUnfound() {
            try {
                const res = await fetch('/api/reviews/unfound');
                const data = await res.json();
                const tbody = document.getElementById('unfound-table-body');

                document.getElementById('badge-unfound-count').innerText = data.unfound.length;

                if (!data.unfound.length) {
                    tbody.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-500">🎉 No unfound care homes! All care homes have valid official websites and contact details.</td></tr>';
                    return;
                }

                tbody.innerHTML = data.unfound.map(item => `
                    <tr class="hover:bg-slate-900/40">
                        <td class="p-3 text-slate-400 font-mono text-xs">${item.cqc_location_id || item.id}</td>
                        <td class="p-3 font-semibold text-white">${item.name}</td>
                        <td class="p-3 text-slate-400 font-mono text-xs">${item.postcode}</td>
                        <td class="p-3 text-slate-400 text-xs">${item.address || '-'}</td>
                        <td class="p-3 text-xs">
                            ${item.discovered_website ? `<a href="${item.discovered_website}" target="_blank" class="text-cyan-400 underline font-mono">${item.discovered_website}</a>` : '<span class="text-rose-400 font-medium">No Website Found</span>'}
                        </td>
                        <td class="p-3 text-xs text-amber-300 font-medium">
                            ${item.website_status === 'NEEDS_MANUAL_REVIEW' ? 'Low confidence website match' : (item.website_status === 'NO_RESULT' ? 'No website returned by search' : 'Website found but no email address published')}
                        </td>
                        <td class="p-3 space-x-2">
                            <button onclick="approveWebsite(${item.id})" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold">Approve</button>
                        </td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        async function loadSuppression() {
            try {
                const res = await fetch('/api/suppression');
                const data = await res.json();
                const tbody = document.getElementById('suppression-table-body');
                if (!data.suppression.length) {
                    tbody.innerHTML = '<tr><td colspan="4" class="p-6 text-center text-slate-500">No emails on suppression list.</td></tr>';
                    return;
                }
                tbody.innerHTML = data.suppression.map(s => `
                    <tr>
                        <td class="p-3 text-slate-400">#${s.id}</td>
                        <td class="p-3 text-red-300 font-mono text-xs">${s.email}</td>
                        <td class="p-3 text-slate-300">${s.reason}</td>
                        <td class="p-3 text-slate-400 text-xs">${s.added_at.substring(0, 10)}</td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        async function handleAddSuppression(e) {
            e.preventDefault();
            const email = document.getElementById('suppress-email-input').value;
            const reason = document.getElementById('suppress-reason-input').value || 'manual entry';
            await fetch('/api/suppression', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ email, reason })
            });
            document.getElementById('suppress-email-input').value = '';
            loadSuppression();
        }

        async function loadFullLogs() {
            try {
                const res = await fetch('/api/logs?limit=100');
                const data = await res.json();
                const tbody = document.getElementById('full-logs-table-body');
                if (!data.logs.length) {
                    tbody.innerHTML = '<tr><td colspan="5" class="p-4 text-center text-slate-500">No logs recorded.</td></tr>';
                    return;
                }
                tbody.innerHTML = data.logs.map(l => `
                    <tr class="hover:bg-slate-900/40">
                        <td class="p-2 text-slate-500">${l.timestamp.substring(0, 19).replace('T', ' ')}</td>
                        <td class="p-2"><span class="px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-semibold">${l.stage}</span></td>
                        <td class="p-2 text-slate-300 font-semibold">${l.action}</td>
                        <td class="p-2 text-slate-400">${l.home_name || '-'}</td>
                        <td class="p-2 text-slate-200">${l.message}</td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        async function resetDiscovery() {
            if (!confirm('Are you sure you want to reset all discovered websites back to pending status for clean re-discovery?')) return;
            try {
                const res = await fetch('/api/pipeline/reset-discovery', { method: 'POST' });
                const data = await res.json();
                alert(data.message || 'Discovery status reset.');
                loadStats();
                loadLogs();
            } catch (err) {
                alert('Error resetting discovery: ' + err.message);
            }
        }

        async function triggerRunPipeline(stage) {
            try {
                const res = await fetch('/api/pipeline/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ stage: stage })
                });
                if (!res.ok) {
                    const text = await res.text();
                    let errMsg = text;
                    try {
                        const errJson = JSON.parse(text);
                        if (errJson.detail) errMsg = errJson.detail;
                    } catch(e) {}
                    alert(`Pipeline info: ${errMsg}`);
                    return;
                }
                const data = await res.json();
                alert(`🚀 Pipeline run started successfully! Stage '${stage}' is processing in background.`);
                loadStats();
                loadLogs();
            } catch (err) {
                alert('Error starting pipeline: ' + err.message);
            }
        }

        // Init
        loadStats();
        loadLogs();
        setInterval(loadStats, 5000);
        setInterval(loadLogs, 5000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
