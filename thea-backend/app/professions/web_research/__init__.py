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
            "- Use web_search to find candidate sources, then web_extract "
            "on the specific URL when you need the full content rather "
            "than just a search snippet.\n"
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
