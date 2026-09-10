from app.professions._registry import ProfessionMeta, register_profession

register_profession(
    ProfessionMeta(
        name="calendar_manager",
        description=(
            "Lists, creates, and deletes calendar events. No provider is "
            "wired up yet (see app/professions/calendar/tools.py) -- every "
            "call fails clearly with error_type=not_configured until one is."
        ),
        example_queries=[
            "what's on my calendar tomorrow?",
            "schedule a meeting with the design team Thursday at 2pm",
            "cancel my 3pm call",
        ],
        skill_prompt=(
            "You are the calendar specialist for this request. You list, "
            "create, and (rarely) delete calendar events.\n\n"
            "Ground rules:\n"
            "- Resolve relative dates/times against the actual current date "
            "before calling a tool -- tools take concrete datetimes.\n"
            "- Never plan a calendar.delete_event step unless the user "
            "unambiguously identified a specific event to remove.\n"
            "- Don't invent attendees the user didn't name."
        ),
        tools=["calendar.list_events", "calendar.create_event", "calendar.delete_event"],
        # Deliberately inactive: no calendar provider is implemented yet
        # (see UnconfiguredCalendarClient), so every call would fail.
        # Flip to True once a real provider is wired up in
        # app/professions/calendar/tools.py's _get_client().
        active=False,
    )
)
