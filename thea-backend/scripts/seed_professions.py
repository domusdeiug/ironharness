"""
Seed/update `professions` rows. Per the handoff: a new profession built
entirely from already-registered tools can be added as a single row here —
no code change, no deploy, beyond running this script (or doing the
equivalent upsert directly against Supabase).

Usage:
    python -m scripts.seed_professions
"""

from __future__ import annotations

from app.db import get_service_client
from app.tools import load_all_tools
from app.tools.registry import all_tools

PROFESSIONS: list[dict] = [
    {
        "name": "echo",
        "description": (
            "Trivial smoke-test profession. Echoes a message back, or "
            "deliberately fails, for exercising the pipeline end to end "
            "without any real integration."
        ),
        "example_queries": [
            "say hello back to me",
            "echo this message: testing 123",
        ],
        "skill_prompt": (
            "You are the echo profession. Your only job is to call echo.say "
            "with the message the user wants echoed, or echo.always_fail if "
            "the user is explicitly testing failure handling."
        ),
        "tools": ["echo.say", "echo.always_fail"],
        "active": True,
    },
    {
        "name": "xlsx_manager",
        "description": (
            "Reads and edits spreadsheet (.xlsx) files: pulling values from "
            "a range, writing values or formulas into cells, and adding "
            "simple bar/line/pie charts."
        ),
        "example_queries": [
            "add up column D and put the total in D20",
            "fix the formula in C5, it's referencing the wrong range",
            "make a bar chart of Q3 sales by region",
            "what's in cells A1 through C10 of the budget sheet?",
        ],
        "skill_prompt": (
            "You are the xlsx specialist for this request. You read and edit "
            "spreadsheet files: pulling values out of ranges, writing values "
            "or formulas into cells, and adding simple charts.\n\n"
            "Ground rules:\n"
            "- Confirm the sheet name and cell range you're operating on "
            "rather than guessing at defaults you weren't told.\n"
            "- xlsx.apply_formula writes a formula string but does not "
            "evaluate it — never claim a computed result is verified unless "
            "it was actually read back with xlsx.read_range after saving.\n"
            "- Prefer the smallest edit that satisfies the request."
        ),
        "tools": ["xlsx.read_range", "xlsx.write_cells", "xlsx.apply_formula", "xlsx.create_chart"],
        "active": True,
    },
    {
        "name": "email_assistant",
        "description": (
            "Searches, drafts, sends, and deletes email. Sending and "
            "deleting are treated as real, consequential actions — drafts "
            "are used whenever intent to send immediately isn't explicit."
        ),
        "example_queries": [
            "email the team the numbers from the spreadsheet",
            "draft a reply to Sarah's message about the budget",
            "find the email from the vendor about the invoice",
            "delete that spam message from this morning",
        ],
        "skill_prompt": (
            "You are the email specialist for this request. You search, "
            "draft, send, and (rarely) delete email.\n\n"
            "Ground rules:\n"
            "- Prefer email.draft_email over email.send_email whenever the "
            "request is ambiguous about sending immediately vs reviewing "
            "first. Only send when the request clearly says to.\n"
            "- Never plan an email.delete_email step unless the user "
            "unambiguously identified a specific message to delete.\n"
            "- When search results are ambiguous about which message the "
            "user means, surface the ambiguity rather than guessing."
        ),
        "tools": ["email.search_inbox", "email.draft_email", "email.send_email", "email.delete_email"],
        "active": True,
    },
    {
        "name": "calendar_manager",
        "description": (
            "Lists, creates, and deletes calendar events. No provider is "
            "wired up yet (see app/professions/calendar/tools.py) — every "
            "call fails clearly with error_type=not_configured until one is."
        ),
        "example_queries": [
            "what's on my calendar tomorrow?",
            "schedule a meeting with the design team Thursday at 2pm",
            "cancel my 3pm call",
        ],
        "skill_prompt": (
            "You are the calendar specialist for this request. You list, "
            "create, and (rarely) delete calendar events.\n\n"
            "Ground rules:\n"
            "- Resolve relative dates/times against the actual current date "
            "before calling a tool — tools take concrete datetimes.\n"
            "- Never plan a calendar.delete_event step unless the user "
            "unambiguously identified a specific event to remove.\n"
            "- Don't invent attendees the user didn't name."
        ),
        "tools": ["calendar.list_events", "calendar.create_event", "calendar.delete_event"],
        # Deliberately inactive: no calendar provider is implemented yet
        # (see UnconfiguredCalendarClient), so every call would fail.
        # Flip to True once a real provider is wired up in
        # app/professions/calendar/tools.py's _get_client().
        "active": False,
    },
    {
        "name": "file_explorer",
        "description": (
            "Reads, writes, patches, and searches text files within a "
            "sandboxed root directory. General-purpose file I/O, not "
            "spreadsheet-specific (see the xlsx_manager profession for that)."
        ),
        "example_queries": [
            "read the first 100 lines of notes.txt",
            "add a new section to report.md",
            "fix the typo 'recieve' in the draft file",
            "find every file that mentions 'deprecated'",
        ],
        "skill_prompt": (
            "You are the files specialist for this request. You read, write, "
            "patch, and search text files, all confined to a sandboxed root "
            "directory — you cannot and must not attempt to reach paths "
            "outside it.\n\n"
            "Ground rules:\n"
            "- Prefer files.patch over files.write_file when only part of an "
            "existing file needs to change — write_file fully overwrites.\n"
            "- files.patch requires old_text to match the file's current "
            "content exactly and exactly once; if unsure of the exact text, "
            "read the file first with files.read_file.\n"
            "- Use files.search_files to locate content before assuming a "
            "file doesn't contain something.\n"
            "- vision_analyze can describe or answer a question about an "
            "image file already inside the sandbox — pass it the sandboxed "
            "path directly, not a URL."
        ),
        "tools": ["files.read_file", "files.write_file", "files.patch", "files.search_files", "vision_analyze"],
        "active": True,
    },
    {
        "name": "web_research_assistant",
        "description": (
            "Searches the web, fetches a page or PDF's text content, and "
            "analyzes images — for gathering current information the model "
            "doesn't already have, or answering questions about a specific "
            "page or picture."
        ),
        "example_queries": [
            "what's the latest news on the merger?",
            "pull the pricing details off that page and summarize them",
            "what does this chart in the screenshot show?",
            "find a source for that statistic and give me the link",
        ],
        "skill_prompt": (
            "You are the web research specialist for this request. You "
            "search the web, fetch a URL's (webpage or PDF) text content, "
            "and analyze images.\n\n"
            "Ground rules:\n"
            "- Use web_search to find candidate sources, then web_extract "
            "on the specific URL when you need the full content rather "
            "than just a search snippet.\n"
            "- vision_analyze takes either a plain URL or a path inside "
            "the files sandbox — not an arbitrary local path outside it.\n"
            "- These tools can fail with error_type=not_configured if the "
            "relevant API key/model isn't set up for this deployment; "
            "don't retry indefinitely against a config problem."
        ),
        "tools": ["web_search", "web_extract", "vision_analyze"],
        "active": True,
    },
]


def main() -> None:
    load_all_tools()
    registered = all_tools()

    client = get_service_client()

    for prof in PROFESSIONS:
        missing = [t for t in prof["tools"] if t not in registered]
        if missing:
            raise SystemExit(
                f"Profession '{prof['name']}' references unregistered tools {missing}. "
                "Register the tool(s) in code first — per the handoff, only professions "
                "built from *already hardened* tools can be added as a pure data row."
            )

        client.table("professions").upsert(prof, on_conflict="name").execute()
        print(f"Upserted profession: {prof['name']}")


if __name__ == "__main__":
    main()
