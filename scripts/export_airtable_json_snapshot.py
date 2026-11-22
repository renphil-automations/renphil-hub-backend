"""Export the Airtable feedback table as an unreviewed JSON snapshot.

Set AIRTABLE_API_KEY, RENPHIL_HUB_BASE_ID, and FEEDBACKS_TABLE in the
environment (or provide --env-file). The destination must not already exist.
This script only reads Airtable and never connects to or writes to Neon.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New private JSON file to create")
    parser.add_argument("--env-file", type=Path, help="Optional private environment file")
    args = parser.parse_args()

    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file() or not load_dotenv(args.env_file, override=False):
            parser.error("Could not load the specified environment file")

    required = ("AIRTABLE_API_KEY", "RENPHIL_HUB_BASE_ID", "FEEDBACKS_TABLE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Missing configuration: " + ", ".join(missing))

    from pyairtable import Api
    try:
        records = Api(os.environ["AIRTABLE_API_KEY"]).table(
            os.environ["RENPHIL_HUB_BASE_ID"], os.environ["FEEDBACKS_TABLE"]
        ).all()
        # Exclusive creation prevents replacing an existing reviewed snapshot.
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump({"records": records}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        parser.exit(1, "Output already exists; choose a new filename.\n")
    except Exception as exc:
        # Avoid printing credentials, records, or third-party response bodies.
        parser.exit(1, f"Export failed ({type(exc).__name__}).\n")
    print(f"Exported {len(records)} records to the private output file. Review before import.")


if __name__ == "__main__":
    main()