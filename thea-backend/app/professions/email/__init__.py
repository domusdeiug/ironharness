from app.professions._registry import ProfessionMeta, register_profession

register_profession(
    ProfessionMeta(
        name="email_assistant",
        description=(
            "Searches, drafts, sends, and deletes email. Sending and "
            "deleting are treated as real, consequential actions -- drafts "
            "are used whenever intent to send immediately isn't explicit."
        ),
        example_queries=[
            "email the team the numbers from the spreadsheet",
            "draft a reply to Sarah's message about the budget",
            "find the email from the vendor about the invoice",
            "delete that spam message from this morning",
        ],
        skill_prompt=(
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
        tools=["email.search_inbox", "email.draft_email", "email.send_email", "email.delete_email"],
        active=True,
    )
)
