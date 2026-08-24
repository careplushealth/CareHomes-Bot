import os
import csv
import gc
import logging
from typing import Dict, Any, Optional, List
from src.stages.base import BaseStage
from src.models import CareHome, StageStatus
from src.utils.validation import (
    clean_text, normalize_postcode, compute_dedupe_hash, normalize_url
)

logger = logging.getLogger(__name__)


class Stage0Import(BaseStage):
    def __init__(self, config, db):
        super().__init__(config, db, stage_name="Stage0_Import")

    def run_import(self, csv_filepath: str) -> Dict[str, Any]:
        if not os.path.exists(csv_filepath):
            raise FileNotFoundError(f"CSV file not found at: {csv_filepath}")

        logger.info(f"Loading CSV dataset from {csv_filepath}...")

        # Detect header row index
        header_row = 0
        with open(csv_filepath, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                line_lower = line.lower()
                if "name" in line_lower and ("postcode" in line_lower or "postal code" in line_lower or "address" in line_lower):
                    header_row = idx
                    break

        with open(csv_filepath, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(header_row):
                next(f, None)

            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            col_map = self._map_columns(headers)
            logger.info(f"Mapped CSV columns from header line {header_row}: {col_map}")

            care_homes_batch: List[CareHome] = []
            total_rows_read = 0
            invalid_rows = 0
            non_carehome_skipped = 0
            inserted_total = 0
            skipped_total = 0
            with_website = 0
            without_website = 0

            for idx, row in enumerate(reader):
                total_rows_read += 1
                name = clean_text(row.get(col_map["name"])) if col_map.get("name") else ""
                postcode = clean_text(row.get(col_map["postcode"])) if col_map.get("postcode") else ""
                address = clean_text(row.get(col_map["address"])) if col_map.get("address") else ""
                cqc_id = clean_text(row.get(col_map["cqc_id"])) if col_map.get("cqc_id") else f"ROW_{idx}"
                website_raw = clean_text(row.get(col_map["website"])) if col_map.get("website") else ""
                service_type = clean_text(row.get(col_map["service_types"])).lower() if col_map.get("service_types") else ""

                if not name or not postcode:
                    invalid_rows += 1
                    continue

                # Strict Filter: Keep ONLY Care Homes & Nursing Homes
                is_care_home = False
                if service_type:
                    if "residential home" in service_type or "nursing home" in service_type or "care home" in service_type:
                        is_care_home = True
                else:
                    name_lower = name.lower()
                    if any(kw in name_lower for kw in ["care home", "nursing home", "residential home", "care house", "care centre", "nursing & residential"]):
                        is_care_home = True

                if not is_care_home and col_map.get("service_types"):
                    non_carehome_skipped += 1
                    continue

                dedupe_hash = compute_dedupe_hash(name, postcode)
                website_url = normalize_url(website_raw)

                if website_url:
                    stage_status = StageStatus.PENDING_EXTRACTION
                    website_status = "ACCEPTED"
                    with_website += 1
                else:
                    stage_status = StageStatus.PENDING_DISCOVERY
                    website_status = "UNCHECKED"
                    without_website += 1

                care_homes_batch.append(CareHome(
                    cqc_location_id=cqc_id,
                    name=name,
                    address=address,
                    postcode=postcode,
                    original_website=website_url,
                    website_status=website_status,
                    stage_status=stage_status,
                    dedupe_hash=dedupe_hash
                ))

                # Batch insert every 1,000 items to keep RAM usage minimal
                if len(care_homes_batch) >= 1000:
                    ins, skp = self.db.insert_care_homes(care_homes_batch)
                    inserted_total += ins
                    skipped_total += skp
                    care_homes_batch.clear()
                    gc.collect()

            if care_homes_batch:
                ins, skp = self.db.insert_care_homes(care_homes_batch)
                inserted_total += ins
                skipped_total += skp
                care_homes_batch.clear()
                gc.collect()

        summary = {
            "total_rows_read": total_rows_read,
            "invalid_rows_skipped": invalid_rows,
            "non_carehome_rows_filtered": non_carehome_skipped,
            "new_homes_inserted": inserted_total,
            "duplicate_homes_skipped": skipped_total,
            "with_website": with_website,
            "without_website": without_website
        }

        msg = (
            f"Stage 0 Import completed. Total rows read: {total_rows_read}. "
            f"Care Homes inserted: {inserted_total} (Skipped {skipped_total} dupes, {non_carehome_skipped} non-carehomes). "
            f"With website: {with_website}, Without website: {without_website}."
        )
        logger.info(msg)
        self.db.log_audit("Stage0_Import", "IMPORT_COMPLETED", msg)
        return summary

    def run(self, max_items: Optional[int] = None) -> Dict[str, Any]:
        # Stage 0 is called explicitly via CLI import command with file path
        return {"status": "Use run_import(csv_filepath) to import dataset"}

    def _map_columns(self, columns: List[str]) -> Dict[str, str]:
        cols_lower = {c.strip().lower(): c for c in columns}
        mapping = {}

        # Name mapping
        for name_key in ["location name", "care home name", "name", "provider name"]:
            if name_key in cols_lower:
                mapping["name"] = cols_lower[name_key]
                break

        # Postcode mapping
        for pc_key in ["location postal code", "postcode", "postal code", "post_code"]:
            if pc_key in cols_lower:
                mapping["postcode"] = cols_lower[pc_key]
                break

        # Address mapping
        for addr_key in ["location primary address line 1", "address", "location address", "address1"]:
            if addr_key in cols_lower:
                mapping["address"] = cols_lower[addr_key]
                break

        # Website mapping
        for web_key in ["service's website (if available)", "service's website", "location web address", "website", "web_address", "url", "site"]:
            if web_key in cols_lower:
                mapping["website"] = cols_lower[web_key]
                break

        # Service Types mapping
        for st_key in ["service types", "service_types", "service type", "type"]:
            if st_key in cols_lower:
                mapping["service_types"] = cols_lower[st_key]
                break

        # CQC ID mapping
        for id_key in ["cqc location id (for office use only)", "location id", "cqc_id", "id", "cqc location id"]:
            if id_key in cols_lower:
                mapping["cqc_id"] = cols_lower[id_key]
                break

        if "name" not in mapping or "postcode" not in mapping:
            raise ValueError(
                f"CSV must contain at least 'name' and 'postcode' columns. Available: {columns}"
            )

        return mapping
