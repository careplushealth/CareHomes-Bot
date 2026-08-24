import sqlite3
import os
import logging
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, date, timezone
from src.models import CareHome, ContactDetails, EmailDraft, StageStatus

logger = logging.getLogger(__name__)

# Check for Neon / Cloud PostgreSQL database connection string
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")


def sanitize_db_url(url: Optional[str]) -> str:
    if not url or not url.strip():
        return ""
    url = url.strip()
    if url.startswith("psql "):
        url = url[5:].strip()
    url = url.strip("'\"")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "&channel_binding=" in url:
        url = url.split("&channel_binding=")[0]
    elif "?channel_binding=" in url:
        url = url.split("?channel_binding=")[0]
    return url


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.clean_url = sanitize_db_url(DATABASE_URL)
        self.is_postgres = bool(self.clean_url)
        if self.is_postgres:
            logger.info("Connecting to Neon PostgreSQL cloud database...")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            logger.info(f"Connecting to local SQLite database at {db_path}...")
        self.init_db()

    def get_connection(self):
        if self.is_postgres:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(self.clean_url, cursor_factory=psycopg2.extras.RealDictCursor)
            return conn
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

    def execute_sql(self, cursor, query: str, params=()):
        if self.is_postgres:
            query = query.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            query = query.replace("REAL", "DOUBLE PRECISION")
            query = query.replace("?", "%s")
            query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            query = query.replace("INSERT OR REPLACE INTO", "INSERT INTO")
            if "INSERT INTO homes" in query and "ON CONFLICT" not in query:
                query = query.rstrip() + " ON CONFLICT (dedupe_hash) DO NOTHING"
            elif "INSERT INTO suppression_list" in query and "ON CONFLICT" not in query:
                query = query.rstrip() + " ON CONFLICT (email) DO NOTHING"
            elif "INSERT INTO contacts" in query and "ON CONFLICT" in query:
                query = query.replace("ON CONFLICT(home_id)", "ON CONFLICT (home_id)")
        cursor.execute(query, params)
        return cursor

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Homes table
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS homes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cqc_location_id TEXT,
                    name TEXT NOT NULL,
                    address TEXT,
                    postcode TEXT NOT NULL,
                    original_website TEXT,
                    discovered_website TEXT,
                    website_confidence REAL DEFAULT 0.0,
                    website_status TEXT DEFAULT 'UNCHECKED',
                    stage_status TEXT DEFAULT 'PENDING_DISCOVERY',
                    dedupe_hash TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Contacts table
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    home_id INTEGER UNIQUE NOT NULL,
                    general_email TEXT,
                    contact_form_url TEXT,
                    manager_name TEXT,
                    manager_email TEXT,
                    source_page_url TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (home_id) REFERENCES homes(id) ON DELETE CASCADE
                )
            """)

            # Email Drafts table
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS email_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    home_id INTEGER NOT NULL,
                    recipient_email TEXT NOT NULL,
                    recipient_name TEXT DEFAULT 'Care Home Manager',
                    subject TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    approved INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'DRAFT',
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (home_id) REFERENCES homes(id) ON DELETE CASCADE
                )
            """)

            # Suppression List table
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS suppression_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    reason TEXT DEFAULT 'unsubscribe',
                    added_at TEXT NOT NULL
                )
            """)

            # Audit Logs table
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    home_id INTEGER,
                    stage TEXT NOT NULL,
                    action TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)

            # Daily usage counter table for rate-limiting checkpointing
            self.execute_sql(cursor, """
                CREATE TABLE IF NOT EXISTS daily_processing (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_date TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    UNIQUE(process_date, stage)
                )
            """)

            conn.commit()

    def log_audit(self, stage: str, action: str, message: str, home_id: Optional[int] = None):
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "INSERT INTO audit_logs (home_id, stage, action, message, timestamp) VALUES (?, ?, ?, ?, ?)",
                (home_id, stage, action, message, timestamp)
            )
            conn.commit()

    def increment_daily_count(self, stage: str) -> int:
        today_str = date.today().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "INSERT INTO daily_processing (process_date, stage, count) VALUES (?, ?, 1) "
                "ON CONFLICT(process_date, stage) DO UPDATE SET count = count + 1",
                (today_str, stage)
            )
            conn.commit()
            self.execute_sql(cursor, 
                "SELECT count FROM daily_processing WHERE process_date = ? AND stage = ?",
                (today_str, stage)
            )
            row = cursor.fetchone()
            if not row:
                return 1
            return row["count"] if isinstance(row, dict) else row[0]

    def get_crawler_daily_count(self) -> int:
        """
        Returns total cumulative items processed today across Stage1_Discovery and Stage2_Extraction combined.
        """
        today = date.today().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "SELECT SUM(processed_count) as total FROM daily_audit WHERE date = ? AND stage_name IN ('Stage1_Discovery', 'Stage2_Extraction')",
                (today,)
            )
            row = cursor.fetchone()
            if not row:
                return 0
            val = row["total"] if isinstance(row, dict) else row[0]
            return val or 0

    def get_daily_count(self, stage: str) -> int:
        today_str = date.today().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "SELECT count FROM daily_processing WHERE process_date = ? AND stage = ?",
                (today_str, stage)
            )
            row = cursor.fetchone()
            if not row:
                return 0
            return row["count"] if isinstance(row, dict) else row[0]

    def insert_care_homes(self, homes: List[CareHome]) -> Tuple[int, int]:
        """Inserts care homes, ignoring duplicates based on dedupe_hash. Returns (inserted, skipped)."""
        inserted = 0
        skipped = 0
        now = datetime.now(timezone.utc).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for h in homes:
                try:
                    self.execute_sql(cursor, """
                        INSERT INTO homes (
                            cqc_location_id, name, address, postcode, original_website,
                            discovered_website, website_confidence, website_status,
                            stage_status, dedupe_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        h.cqc_location_id, h.name, h.address, h.postcode, h.original_website,
                        h.discovered_website, h.website_confidence, h.website_status,
                        h.stage_status, h.dedupe_hash, now, now
                    ))
                    inserted += 1
                except Exception as e:
                    if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                        skipped += 1
                    else:
                        skipped += 1
            conn.commit()
        return inserted, skipped

    def get_homes_for_stage(self, stage_status: str, limit: Optional[int] = None) -> List[CareHome]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM homes WHERE stage_status = ? ORDER BY id ASC"
            params = [stage_status]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            self.execute_sql(cursor, query, params)
            rows = cursor.fetchall()
            return [self._row_to_care_home(r) for r in rows]

    def update_home_website(self, home_id: int, discovered_url: Optional[str], confidence: float,
                            website_status: str, next_stage: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, """
                UPDATE homes SET
                    discovered_website = ?,
                    website_confidence = ?,
                    website_status = ?,
                    stage_status = ?,
                    updated_at = ?
                WHERE id = ?
            """, (discovered_url, confidence, website_status, next_stage, now, home_id))
            conn.commit()

    def update_home_stage(self, home_id: int, next_stage: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "UPDATE homes SET stage_status = ?, updated_at = ? WHERE id = ?",
                (next_stage, now, home_id)
            )
            conn.commit()

    def save_contact_details(self, contact: ContactDetails):
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, """
                INSERT INTO contacts (
                    home_id, general_email, contact_form_url, manager_name, manager_email, source_page_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(home_id) DO UPDATE SET
                    general_email = excluded.general_email,
                    contact_form_url = excluded.contact_form_url,
                    manager_name = excluded.manager_name,
                    manager_email = excluded.manager_email,
                    source_page_url = excluded.source_page_url,
                    created_at = excluded.created_at
            """, (
                contact.home_id, contact.general_email, contact.contact_form_url,
                contact.manager_name, contact.manager_email, contact.source_page_url, now
            ))
            conn.commit()

    def get_contact_for_home(self, home_id: int) -> Optional[ContactDetails]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, "SELECT * FROM contacts WHERE home_id = ?", (home_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return ContactDetails(
                id=row["id"],
                home_id=row["home_id"],
                general_email=row["general_email"],
                contact_form_url=row["contact_form_url"],
                manager_name=row["manager_name"],
                manager_email=row["manager_email"],
                source_page_url=row["source_page_url"],
                created_at=row["created_at"]
            )

    def save_email_draft(self, draft: EmailDraft) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, """
                INSERT INTO email_drafts (
                    home_id, recipient_email, recipient_name, subject, body_text, approved, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                draft.home_id, draft.recipient_email, draft.recipient_name, draft.subject,
                draft.body_text, draft.approved, draft.status, now
            ))
            conn.commit()
            return getattr(cursor, "lastrowid", 1)

    def is_email_suppressed(self, email: str) -> bool:
        if not email:
            return False
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, "SELECT 1 FROM suppression_list WHERE LOWER(email) = LOWER(?)", (email.strip(),))
            return cursor.fetchone() is not None

    def reset_discovered_websites(self) -> int:
        """
        Resets discovered websites and stage status for homes that had blank original websites,
        allowing clean re-discovery with strict live HTTP verification.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, """
                UPDATE homes
                SET discovered_website = NULL,
                    website_confidence = 0.0,
                    website_status = 'PENDING',
                    stage_status = 'PENDING_DISCOVERY'
                WHERE original_website IS NULL OR original_website = ''
            """)
            count = cursor.rowcount
            conn.commit()
            return count

    def add_suppression(self, email: str, reason: str = "unsubscribe"):
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, 
                "INSERT OR IGNORE INTO suppression_list (email, reason, added_at) VALUES (?, ?, ?)",
                (email.strip().lower(), reason, now)
            )
            conn.commit()

    def get_pending_drafts(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT d.*, h.name as home_name, h.postcode
                FROM email_drafts d
                JOIN homes h ON d.home_id = h.id
                WHERE d.approved = 0 AND d.status = 'DRAFT'
                ORDER BY d.id ASC
            """
            params = []
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            self.execute_sql(cursor, query, params)
            return [dict(r) for r in cursor.fetchall()]

    def approve_draft(self, draft_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, "UPDATE email_drafts SET approved = 1, status = 'QUEUED' WHERE id = ?", (draft_id,))
            conn.commit()

    def reject_draft(self, draft_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, "UPDATE email_drafts SET approved = -1, status = 'REJECTED' WHERE id = ?", (draft_id,))
            conn.commit()

    def mark_draft_sent(self, draft_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self.execute_sql(cursor, "SELECT home_id FROM email_drafts WHERE id = ?", (draft_id,))
            row = cursor.fetchone()
            self.execute_sql(cursor, "UPDATE email_drafts SET status = 'SENT', sent_at = ? WHERE id = ?", (now, draft_id))
            if row:
                self.execute_sql(cursor, "UPDATE homes SET stage_status = 'SENT', updated_at = ? WHERE id = ?", (now, row["home_id"]))
            conn.commit()

    def get_directory_carehomes(
        self,
        query_text: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Returns paginated care homes with full details (name, address, postcode, website, phone, email, status)
        and matching total count for directory search and filtering.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            sql_base = """
                FROM homes h
                LEFT JOIN contacts c ON h.id = c.home_id
            """
            conditions = []
            params = []

            if query_text and query_text.strip():
                q = f"%{query_text.strip().lower()}%"
                conditions.append("(LOWER(h.name) LIKE ? OR LOWER(h.postcode) LIKE ? OR LOWER(h.address) LIKE ?)")
                params.extend([q, q, q])

            if status_filter and status_filter.lower() != "all":
                sf = status_filter.upper()
                if sf == "ACCEPTED_WEBSITES":
                    conditions.append("h.website_status = 'ACCEPTED'")
                elif sf == "CONTACTS_EXTRACTED":
                    conditions.append("(c.general_email IS NOT NULL OR c.manager_email IS NOT NULL)")
                elif sf == "NEEDS_REVIEW":
                    conditions.append("h.website_status = 'NEEDS_MANUAL_REVIEW'")
                elif sf == "UNFOUND":
                    conditions.append("h.website_status IN ('NEEDS_MANUAL_REVIEW', 'REJECTED', 'NO_RESULT')")
                else:
                    conditions.append("h.stage_status = ?")
                    params.append(sf)

            where_clause = ""
            if conditions:
                where_clause = " WHERE " + " AND ".join(conditions)

            # Count total matching rows
            count_sql = "SELECT COUNT(*) as total " + sql_base + where_clause
            self.execute_sql(cursor, count_sql, params)
            row = cursor.fetchone()
            total_count = row["total"] if isinstance(row, dict) else row[0]

            # Fetch paginated rows
            data_sql = """
                SELECT h.id, h.cqc_location_id, h.name, h.address, h.postcode,
                       COALESCE(h.discovered_website, h.original_website) as website,
                       h.website_confidence, h.website_status, h.stage_status,
                       c.general_email, c.manager_name, c.manager_email, c.contact_form_url,
                       COALESCE(c.manager_email, c.general_email) as primary_email
                """ + sql_base + where_clause + " ORDER BY h.id ASC LIMIT ? OFFSET ?"
            
            data_params = list(params) + [limit, offset]
            self.execute_sql(cursor, data_sql, data_params)
            rows = [dict(r) for r in cursor.fetchall()]
            return rows, total_count

    def get_unfound_carehomes(self) -> List[Dict[str, Any]]:
        """
        Returns all care homes where website discovery or contact extraction failed,
        or where details were low confidence / missing for manual fallback review.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT h.id, h.cqc_location_id, h.name, h.address, h.postcode,
                       h.original_website, h.discovered_website, h.website_confidence,
                       h.website_status, h.stage_status,
                       c.general_email, c.contact_form_url, c.manager_name, c.manager_email
                FROM homes h
                LEFT JOIN contacts c ON h.id = c.home_id
                WHERE h.website_status IN ('NEEDS_MANUAL_REVIEW', 'REJECTED', 'NO_RESULT')
                   OR h.stage_status = 'MANUAL_REVIEW_NEEDED'
                   OR (h.stage_status = 'PENDING_PERSONALISATION' AND (c.general_email IS NULL AND c.manager_email IS NULL))
                ORDER BY h.id ASC
            """
            self.execute_sql(cursor, query)
            return [dict(r) for r in cursor.fetchall()]

    def _row_to_care_home(self, row: sqlite3.Row) -> CareHome:
        return CareHome(
            id=row["id"],
            cqc_location_id=row["cqc_location_id"],
            name=row["name"],
            address=row["address"],
            postcode=row["postcode"],
            original_website=row["original_website"],
            discovered_website=row["discovered_website"],
            website_confidence=row["website_confidence"],
            website_status=row["website_status"],
            stage_status=row["stage_status"],
            dedupe_hash=row["dedupe_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )
