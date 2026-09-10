from app.professions._registry import ProfessionMeta, register_profession

register_profession(
    ProfessionMeta(
        name="file_explorer",
        description=(
            "Reads, writes, patches, and searches text files within a "
            "sandboxed root directory. General-purpose file I/O, not "
            "spreadsheet-specific (see the xlsx_manager profession for that)."
        ),
        example_queries=[
            "read the first 100 lines of notes.txt",
            "add a new section to report.md",
            "fix the typo 'recieve' in the draft file",
            "find every file that mentions 'deprecated'",
        ],
        skill_prompt=(
            "You are the files specialist for this request. You read, write, "
            "patch, and search text files, all confined to a sandboxed root "
            "directory -- you cannot and must not attempt to reach paths "
            "outside it.\n\n"
            "Ground rules:\n"
            "- Prefer files.patch over files.write_file when only part of an "
            "existing file needs to change -- write_file fully overwrites.\n"
            "- files.patch requires old_text to match the file's current "
            "content exactly and exactly once; if unsure of the exact text, "
            "read the file first with files.read_file.\n"
            "- Use files.search_files to locate content before assuming a "
            "file doesn't contain something.\n"
            "- vision_analyze can describe or answer a question about an "
            "image file already inside the sandbox -- pass it the sandboxed "
            "path directly, not a URL."
        ),
        tools=["files.read_file", "files.write_file", "files.patch", "files.search_files", "vision_analyze"],
        active=True,
    )
)
