from app.professions._registry import ProfessionMeta, register_profession

register_profession(
    ProfessionMeta(
        name="xlsx_manager",
        description=(
            "Reads and edits spreadsheet (.xlsx) files: pulling values from "
            "a range, writing values or formulas into cells, and adding "
            "simple bar/line/pie charts."
        ),
        example_queries=[
            "add up column D and put the total in D20",
            "fix the formula in C5, it's referencing the wrong range",
            "make a bar chart of Q3 sales by region",
            "what's in cells A1 through C10 of the budget sheet?",
        ],
        skill_prompt=(
            "You are the xlsx specialist for this request. You read and edit "
            "spreadsheet files: pulling values out of ranges, writing values "
            "or formulas into cells, and adding simple charts.\n\n"
            "Ground rules:\n"
            "- Confirm the sheet name and cell range you're operating on "
            "rather than guessing at defaults you weren't told.\n"
            "- xlsx.apply_formula writes a formula string but does not "
            "evaluate it -- never claim a computed result is verified unless "
            "it was actually read back with xlsx.read_range after saving.\n"
            "- Prefer the smallest edit that satisfies the request."
        ),
        tools=["xlsx.read_range", "xlsx.write_cells", "xlsx.apply_formula", "xlsx.create_chart"],
        active=True,
    )
)
