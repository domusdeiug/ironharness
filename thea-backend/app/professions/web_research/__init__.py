"""
`web_research_assistant` profession -- a metadata-only profession with no
tools.py of its own. It is composed entirely of tools from the shared pool
(app/professions/_shared_tools/), which is exactly why it lives here as
just a PROFESSION_META declaration rather than owning any tool code:
nothing here needs to be different per-profession except the description,
skill prompt, and example queries used for classification/planning.
"""

from app.professions._registry import ProfessionMeta, register_profession

register_profession(
    ProfessionMeta(
        name="web_research_assistant",
        description=(
            "Searches the web, fetches a page or PDF's text content, and "
            "analyzes images -- for gathering current information the model "
            "doesn't already have, or answering questions about a specific "
            "page or picture."
        ),
        example_queries=[
            "what's the latest news on the merger?",
            "pull the pricing details off that page and summarize them",
            "what does this chart in the screenshot show?",
            "find a source for that statistic and give me the link",
        ],
        skill_prompt=(
            "You are the web research specialist for this request. You "
            "search the web, fetch a URL's (webpage or PDF) text content, "
            "and analyze images.\n\n"
            "Ground rules:\n"
            "- Default to web_search alone for open-ended research questions "
            "(\"what's the latest on X\", \"find good sources for Y\") -- it "
            "already returns real page content per result, not just a "
            "snippet, so it's almost always sufficient by itself.\n"
            "- Only plan a web_extract step when a specific URL is already "
            "known before planning -- the user gave you one directly, or a "
            "document you already have names one. Never plan web_extract on "
            "a URL you'd have to pick out of web_search's results: those "
            "results don't exist yet when you're writing the plan, and "
            "guessing at a URL you can't yet know is wrong outright.\n"
            "- vision_analyze takes either a plain URL or a path inside "
            "the files sandbox -- not an arbitrary local path outside it.\n"
            "- These tools can fail with error_type=not_configured if the "
            "relevant API key/model isn't set up for this deployment; "
            "don't retry indefinitely against a config problem."
        ),
        tools=["web_search", "web_extract", "vision_analyze"],
        active=True,
    )
)
