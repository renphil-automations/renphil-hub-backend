"""Export each Airtable user's name, department and programs to Excel.

Reads every row of Users (base appSh6OwO3ZMAkuVE, table tbl537cgZO1xCBOQs).
Programs come from two fields, which disagree in some rows, so both are kept:
  - "Program"       free text, comma-separated when a user is on several programs
  - "Program Names" lookup of the linked "Programs" records (Funds and Programs)

Usage (from renphil-hub-backend/):
    .venv/Scripts/python.exe scripts/export_airtable_user_programs.py [output.xlsx]

The Airtable token is read from AIRTABLE_API_KEY in renphil-hub-backend/.env.
"""

import sys
from pathlib import Path

import xlsxwriter
from dotenv import dotenv_values
from pyairtable import Api

BASE_ID = "appSh6OwO3ZMAkuVE"
TABLE_ID = "tbl537cgZO1xCBOQs"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "user_programs.xlsx"


def user_name(fields):
    name = (fields.get("Name") or "").strip()
    if name:
        return name
    return " ".join(
        part.strip() for part in (fields.get("First Name"), fields.get("Last Name")) if part and part.strip()
    )


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    api_key = dotenv_values(ENV_PATH).get("AIRTABLE_API_KEY")
    if not api_key:
        sys.exit(f"AIRTABLE_API_KEY not found in {ENV_PATH}")

    rows = Api(api_key).table(BASE_ID, TABLE_ID).all(
        fields=["Name", "First Name", "Last Name", "Department", "Program", "Program Names"]
    )

    users = []
    for row in rows:
        fields = row["fields"]
        users.append([
            user_name(fields),
            fields.get("Department") or "",
            (fields.get("Program") or "").strip(),
            ", ".join(str(n).strip() for n in fields.get("Program Names") or [] if str(n).strip()),
        ])
    users.sort(key=lambda u: u[0].casefold())

    workbook = xlsxwriter.Workbook(str(output))
    sheet = workbook.add_worksheet("Users")
    header = workbook.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
    headers = ["Name", "Department", "Program (text)", "Programs (linked)"]
    sheet.write_row(0, 0, headers, header)
    for i, user in enumerate(users, start=1):
        sheet.write_row(i, 0, user)

    sheet.set_column(0, 0, 30)
    sheet.set_column(1, 1, 18)
    sheet.set_column(2, 3, 55)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(users), len(headers) - 1)
    workbook.close()

    print(f"{len(users)} users -> {output}")


if __name__ == "__main__":
    main()
