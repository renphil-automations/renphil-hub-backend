"""Export every unique program name in the Airtable base to Excel.

The list is the union of three sources (base appSh6OwO3ZMAkuVE):
  - Funds and Programs (tblfMzEL91XjADdrN) "Name"
  - Users (tbl537cgZO1xCBOQs) "Program": free text, comma-separated when a user
    is on several programs
  - Users "Program Names": lookup of the linked "Programs" records

Names are matched case-insensitively. A program in Funds and Programs is shown
with that table's spelling. Users-only names (including "Studio") are kept
as they appear in Airtable.

Usage (from renphil-hub-backend/):
    .venv/Scripts/python.exe scripts/export_airtable_program_names.py [output.xlsx]

The Airtable token is read from AIRTABLE_API_KEY in renphil-hub-backend/.env.
"""

import sys
from pathlib import Path

import xlsxwriter
from dotenv import dotenv_values
from pyairtable import Api

BASE_ID = "appSh6OwO3ZMAkuVE"
USERS_TABLE_ID = "tbl537cgZO1xCBOQs"
PROGRAMS_TABLE_ID = "tblfMzEL91XjADdrN"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "program_names.xlsx"


def split_free_text(value):
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    api_key = dotenv_values(ENV_PATH).get("AIRTABLE_API_KEY")
    if not api_key:
        sys.exit(f"AIRTABLE_API_KEY not found in {ENV_PATH}")

    api = Api(api_key)
    program_rows = api.table(BASE_ID, PROGRAMS_TABLE_ID).all(fields=["Name"])
    user_rows = api.table(BASE_ID, USERS_TABLE_ID).all(fields=["Program", "Program Names"])

    # casefolded name -> {"name", "listed", "text": set(user ids), "linked": set(user ids)}
    programs = {}

    def entry(name):
        key = name.casefold()
        if key not in programs:
            programs[key] = {"name": name, "listed": False, "text": set(), "linked": set()}
        return programs[key]

    for row in program_rows:
        name = str(row["fields"].get("Name") or "").strip()
        if name:
            program = entry(name)
            program["name"] = name
            program["listed"] = True

    for row in user_rows:
        fields = row["fields"]
        for name in split_free_text(fields.get("Program")):
            entry(name)["text"].add(row["id"])
        for name in fields.get("Program Names") or []:
            if str(name).strip():
                entry(str(name).strip())["linked"].add(row["id"])

    workbook = xlsxwriter.Workbook(str(output))
    sheet = workbook.add_worksheet("Programs")
    header = workbook.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
    headers = [
        "Program Name",
        "Users",
        "In Funds and Programs",
        "In Users 'Program' (text)",
        "In Users 'Programs' (linked)",
    ]
    sheet.write_row(0, 0, headers, header)

    ordered = sorted(programs.values(), key=lambda p: p["name"].casefold())
    for i, program in enumerate(ordered, start=1):
        sheet.write_row(i, 0, [
            program["name"],
            len(program["text"] | program["linked"]),
            "Yes" if program["listed"] else "",
            "Yes" if program["text"] else "",
            "Yes" if program["linked"] else "",
        ])

    sheet.set_column(0, 0, 50)
    sheet.set_column(1, 1, 10)
    sheet.set_column(2, 4, 28)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(ordered), len(headers) - 1)
    workbook.close()

    listed = sum(p["listed"] for p in ordered)
    print(f"{len(ordered)} unique programs ({listed} in Funds and Programs, "
          f"{len(ordered) - listed} only in Users) -> {output}")


if __name__ == "__main__":
    main()
