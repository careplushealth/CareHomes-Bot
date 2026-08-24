import argparse
import sys
import os
import logging
import pandas as pd
from typing import List

from src.config import AppConfig
from src.db import DatabaseManager
from src.stages.stage0_import import Stage0Import
from src.stages.stage1_discovery import Stage1Discovery
from src.stages.stage2_extraction import Stage2Extraction
from src.stages.stage3_personalise import Stage3Personalise
from src.stages.stage4_sending import Stage4Sending
from src.review import ReviewQueueManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("main")


def generate_sample_cqc_csv(output_path: str = "data/sample_care_homes.csv"):
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    sample_data = [
        {
            "Location ID": "1-10001",
            "Location Name": "Oakwood House Care Home",
            "Location Address": "12 High Street, Kensington",
            "Location Postal Code": "SW1A 1AA",
            "Location Web Address": "https://www.oakwoodhousecare.co.uk"
        },
        {
            "Location ID": "1-10002",
            "Location Name": "Meadow View Residential Care",
            "Location Address": "45 Green Lane, Richmond",
            "Location Postal Code": "TW9 2AB",
            "Location Web Address": ""  # Missing website -> test Stage 1 discovery
        },
        {
            "Location ID": "1-10003",
            "Location Name": "Pine Tree Lodge Nursing Home",
            "Location Address": "88 Station Road, Croydon",
            "Location Postal Code": "CR0 1XX",
            "Location Web Address": ""  # Missing website -> test Stage 1 discovery
        },
        {
            "Location ID": "1-10004",
            "Location Name": "Unknown Generic Home",
            "Location Address": "1 Unknown Way",
            "Location Postal Code": "E1 6AN",
            "Location Web Address": ""  # Missing website -> test low-confidence scoring flag
        },
        {
            "Location ID": "1-10001",  # Duplicate row -> test deduplication
            "Location Name": "Oakwood House Care Home",
            "Location Address": "12 High Street, Kensington",
            "Location Postal Code": "SW1A 1AA",
            "Location Web Address": "https://www.oakwoodhousecare.co.uk"
        }
    ]
    df = pd.DataFrame(sample_data)
    df.to_csv(output_path, index=False)
    logger.info(f"Generated sample CQC care home dataset at: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="UK Care Homes Outreach Pipeline (CQC Data Processing & PECR-Compliant Outreach)"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml file")

    subparsers = parser.add_subparsers(dest="command", help="Sub-command to run")

    # init-db
    subparsers.add_parser("init-db", help="Initialize SQLite database schema")

    # generate-sample-csv
    gen_p = subparsers.add_parser("generate-sample-csv", help="Generate a synthetic CQC CSV file for testing")
    gen_p.add_argument("--output", default="data/sample_care_homes.csv", help="Output path for sample CSV")

    # import-csv
    imp_p = subparsers.add_parser("import-csv", help="Import & deduplicate CQC care home dataset CSV")
    imp_p.add_argument("csv_path", help="Path to input CSV file")

    # run-pipeline
    run_p = subparsers.add_parser("run-pipeline", help="Execute pipeline stages")
    run_p.add_argument("--stage", choices=["1", "2", "3", "4", "all"], default="all",
                       help="Specific stage to execute (1: Discovery, 2: Extraction, 3: Personalise, 4: Send)")
    run_p.add_argument("--limit", type=int, default=None, help="Max items to process in this run")

    # review
    rev_p = subparsers.add_parser("review", help="Inspect and manage review queue")
    rev_p.add_argument("--action", choices=["list-websites", "list-drafts", "approve-all-drafts", "approve-website"], default="list-drafts")
    rev_p.add_argument("--id", type=int, help="Target ID for single website/draft approval")

    # export-queue
    exp_p = subparsers.add_parser("export-queue", help="Export review queue email drafts to CSV")
    exp_p.add_argument("--output", default="data/review_queue_export.csv", help="Output CSV filepath")

    # export-unfound
    exp_u = subparsers.add_parser("export-unfound", help="Export list of care homes needing manual fallback review to CSV")
    exp_u.add_argument("--output", default="data/unfound_carehomes_review.csv", help="Output CSV filepath")

    # reset-discovery
    subparsers.add_parser("reset-discovery", help="Reset all discovered websites back to PENDING_DISCOVERY")

    # send
    send_p = subparsers.add_parser("send", help="Execute Stage 4 email dispatch (GATED)")
    send_p.add_argument("--mode", choices=["resend", "smtp", "dry_run", "mock"], default="resend", help="Sending transport mode")
    send_p.add_argument("--approved-only", action="store_true", default=True, help="Only send approved drafts")
    send_p.add_argument("--limit", type=int, default=None, help="Max emails to send")

    # unsubscribe
    unsub_p = subparsers.add_parser("unsubscribe", help="Add an email address to PECR suppression list")
    unsub_p.add_argument("email", help="Email address to suppress")
    unsub_p.add_argument("--reason", default="unsubscribe request", help="Reason for suppression")

    # server
    srv_p = subparsers.add_parser("server", help="Launch interactive Web Dashboard UI")
    srv_p.add_argument("--host", default="0.0.0.0", help="Host address to bind")
    srv_p.add_argument("--port", type=int, default=8000, help="Port to listen on")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cfg = AppConfig(args.config)
    db = DatabaseManager(cfg.database_path)
    reviewer = ReviewQueueManager(db)

    if args.command == "init-db":
        db.init_db()
        logger.info(f"Initialized SQLite database at {cfg.database_path}")

    elif args.command == "generate-sample-csv":
        generate_sample_cqc_csv(args.output)

    elif args.command == "import-csv":
        stage0 = Stage0Import(cfg, db)
        summary = stage0.run_import(args.csv_path)
        print("\n--- CSV IMPORT SUMMARY ---")
        for k, v in summary.items():
            print(f"  {k}: {v}")

    elif args.command == "run-pipeline":
        limit = args.limit
        if args.stage in ("1", "all"):
            print("\n==========================================")
            print("RUNNING STAGE 1: WEBSITE DISCOVERY")
            print("==========================================")
            stage1 = Stage1Discovery(cfg, db)
            res1 = stage1.run(max_items=limit)
            print("Stage 1 Summary:", res1)

        if args.stage in ("2", "all"):
            print("\n==========================================")
            print("RUNNING STAGE 2: CONTACT EXTRACTION")
            print("==========================================")
            stage2 = Stage2Extraction(cfg, db)
            res2 = stage2.run(max_items=limit)
            print("Stage 2 Summary:", res2)

        if args.stage in ("3", "all"):
            print("\n==========================================")
            print("RUNNING STAGE 3: EMAIL PERSONALISATION")
            print("==========================================")
            stage3 = Stage3Personalise(cfg, db)
            res3 = stage3.run(max_items=limit)
            print("Stage 3 Summary:", res3)

        if args.stage == "4":
            print("\n==========================================")
            print("RUNNING STAGE 4: GATED SENDING")
            print("==========================================")
            stage4 = Stage4Sending(cfg, db, transport_mode="dry_run")
            res4 = stage4.run(max_items=limit)
            print("Stage 4 Summary:", res4)

    elif args.command == "review":
        if args.action == "list-websites":
            items = reviewer.get_websites_needing_review()
            print(f"\n--- WEBSITES NEEDING MANUAL REVIEW ({len(items)}) ---")
            for item in items:
                print(f"  ID #{item['id']}: {item['name']} ({item['postcode']}) -> Found: {item['discovered_website']} (Confidence: {item['website_confidence']:.2f})")

        elif args.action == "list-drafts":
            drafts = reviewer.list_pending_drafts()
            print(f"\n--- PENDING EMAIL DRAFTS IN REVIEW QUEUE ({len(drafts)}) ---")
            for d in drafts:
                print(f"  Draft #{d['id']} [Home #{d['home_id']} - {d['home_name']}]: Recipient='{d['recipient_email']}' | Subject='{d['subject']}'")

        elif args.action == "approve-all-drafts":
            count = reviewer.approve_all_drafts()
            print(f"\nSuccessfully approved {count} email drafts for sending queue.")

        elif args.action == "approve-website":
            if not args.id:
                print("Error: --id is required for approve-website command.")
                sys.exit(1)
            reviewer.approve_website(args.id)
            print(f"Approved website for Home #{args.id}")

    elif args.command == "export-queue":
        count = reviewer.export_drafts_to_csv(args.output)
        print(f"\nExported {count} drafts to {args.output}")

    elif args.command == "export-unfound":
        count = reviewer.export_unfound_to_csv(args.output)
        print(f"\nExported {count} unfound care homes for manual review to {args.output}")

    elif args.command == "reset-discovery":
        count = reviewer.reset_discovered_websites()
        print(f"\nReset {count} discovered care home websites back to PENDING_DISCOVERY.")

    elif args.command == "send":
        stage4 = Stage4Sending(cfg, db, transport_mode=args.mode)
        res = stage4.run(max_items=args.limit, approved_only=args.approved_only)
        print("\n--- STAGE 4 DISPATCH SUMMARY ---")
        for k, v in res.items():
            print(f"  {k}: {v}")

    elif args.command == "unsubscribe":
        db.add_suppression(args.email, args.reason)
        print(f"Added '{args.email}' to PECR suppression list.")

    elif args.command == "server":
        import uvicorn
        logger.info(f"Starting Web Dashboard Server at http://{args.host}:{args.port}")
        uvicorn.run("server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
