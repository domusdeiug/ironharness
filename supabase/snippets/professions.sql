-- Seed/update professions rows (generated from scripts/seed_professions.py)
-- Safe to re-run: upserts on the unique `name` column, matching the Python script's on_conflict behavior.

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'echo',
    'Trivial smoke-test profession. Echoes a message back, or deliberately fails, for exercising the pipeline end to end without any real integration.',
    ARRAY['say hello back to me', 'echo this message: testing 123']::text[],
    'You are the echo profession. Your only job is to call echo.say with the message the user wants echoed, or echo.always_fail if the user is explicitly testing failure handling.',
    ARRAY['echo.say', 'echo.always_fail']::text[],
    true
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'xlsx_manager',
    'Reads and edits spreadsheet (.xlsx) files: pulling values from a range, writing values or formulas into cells, and adding simple bar/line/pie charts.',
    ARRAY['add up column D and put the total in D20', 'fix the formula in C5, it''s referencing the wrong range', 'make a bar chart of Q3 sales by region', 'what''s in cells A1 through C10 of the budget sheet?']::text[],
    'You are the xlsx specialist for this request. You read and edit spreadsheet files: pulling values out of ranges, writing values or formulas into cells, and adding simple charts.

Ground rules:
- Confirm the sheet name and cell range you''re operating on rather than guessing at defaults you weren''t told.
- xlsx.apply_formula writes a formula string but does not evaluate it — never claim a computed result is verified unless it was actually read back with xlsx.read_range after saving.
- Prefer the smallest edit that satisfies the request.',
    ARRAY['xlsx.read_range', 'xlsx.write_cells', 'xlsx.apply_formula', 'xlsx.create_chart']::text[],
    true
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'email_assistant',
    'Searches, drafts, sends, and deletes email. Sending and deleting are treated as real, consequential actions — drafts are used whenever intent to send immediately isn''t explicit.',
    ARRAY['email the team the numbers from the spreadsheet', 'draft a reply to Sarah''s message about the budget', 'find the email from the vendor about the invoice', 'delete that spam message from this morning']::text[],
    'You are the email specialist for this request. You search, draft, send, and (rarely) delete email.

Ground rules:
- Prefer email.draft_email over email.send_email whenever the request is ambiguous about sending immediately vs reviewing first. Only send when the request clearly says to.
- Never plan an email.delete_email step unless the user unambiguously identified a specific message to delete.
- When search results are ambiguous about which message the user means, surface the ambiguity rather than guessing.',
    ARRAY['email.search_inbox', 'email.draft_email', 'email.send_email', 'email.delete_email']::text[],
    true
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'calendar_manager',
    'Lists, creates, and deletes calendar events. No provider is wired up yet (see app/professions/calendar/tools.py) — every call fails clearly with error_type=not_configured until one is.',
    ARRAY['what''s on my calendar tomorrow?', 'schedule a meeting with the design team Thursday at 2pm', 'cancel my 3pm call']::text[],
    'You are the calendar specialist for this request. You list, create, and (rarely) delete calendar events.

Ground rules:
- Resolve relative dates/times against the actual current date before calling a tool — tools take concrete datetimes.
- Never plan a calendar.delete_event step unless the user unambiguously identified a specific event to remove.
- Don''t invent attendees the user didn''t name.',
    ARRAY['calendar.list_events', 'calendar.create_event', 'calendar.delete_event']::text[],
    false
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'file_explorer',
    'Reads, writes, patches, and searches text files within a sandboxed root directory. General-purpose file I/O, not spreadsheet-specific (see the xlsx_manager profession for that).',
    ARRAY['read the first 100 lines of notes.txt', 'add a new section to report.md', 'fix the typo ''recieve'' in the draft file', 'find every file that mentions ''deprecated''']::text[],
    'You are the files specialist for this request. You read, write, patch, and search text files, all confined to a sandboxed root directory — you cannot and must not attempt to reach paths outside it.

Ground rules:
- Prefer files.patch over files.write_file when only part of an existing file needs to change — write_file fully overwrites.
- files.patch requires old_text to match the file''s current content exactly and exactly once; if unsure of the exact text, read the file first with files.read_file.
- Use files.search_files to locate content before assuming a file doesn''t contain something.
- vision_analyze can describe or answer a question about an image file already inside the sandbox — pass it the sandboxed path directly, not a URL.',
    ARRAY['files.read_file', 'files.write_file', 'files.patch', 'files.search_files', 'vision_analyze']::text[],
    true
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;

INSERT INTO professions (name, description, example_queries, skill_prompt, tools, active)
VALUES (
    'web_research_assistant',
    'Searches the web, fetches a page or PDF''s text content, and analyzes images — for gathering current information the model doesn''t already have, or answering questions about a specific page or picture.',
    ARRAY['what''s the latest news on the merger?', 'pull the pricing details off that page and summarize them', 'what does this chart in the screenshot show?', 'find a source for that statistic and give me the link']::text[],
    'You are the web research specialist for this request. You search the web, fetch a URL''s (webpage or PDF) text content, and analyze images.

Ground rules:
- Use web_search to find candidate sources, then web_extract on the specific URL when you need the full content rather than just a search snippet.
- vision_analyze takes either a plain URL or a path inside the files sandbox — not an arbitrary local path outside it.
- These tools can fail with error_type=not_configured if the relevant API key/model isn''t set up for this deployment; don''t retry indefinitely against a config problem.',
    ARRAY['web_search', 'web_extract', 'vision_analyze']::text[],
    true
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    example_queries = EXCLUDED.example_queries,
    skill_prompt = EXCLUDED.skill_prompt,
    tools = EXCLUDED.tools,
    active = EXCLUDED.active;