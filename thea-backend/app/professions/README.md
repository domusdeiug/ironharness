Think of a "profession" as a specialist the system can call on — like hiring a new expert. Here's the whole recipe, in plain terms, using a made-up example: let's say you want to add a **"slack_manager"** profession that can post messages to Slack.

**Step 1: Make a folder**
Inside `thea-backend/app/professions/`, create a new folder called `slack`.

**Step 2: Write the actual tools (the "what it can do")**
Inside `slack/`, make a file called `tools.py`. This is where you write the real code — the functions that actually send a Slack message, read a channel, etc. Each one gets registered with a name like `slack.send_message`. (You'd copy the shape of an existing one, like `email/tools.py`, since they all follow the same pattern.)

**Step 3: Write the "ID card" for the profession**
In that same folder, make (or edit) `__init__.py` and describe the profession in plain language:
- its **name** (`"slack_manager"`)
- a short **description** of what it does
- a few **example queries** a user might type ("post an update in #general")
- a **skill_prompt** — instructions telling the model how to behave when it's wearing this "hat" (be careful about who it messages, etc.)
- the **list of tools** it owns (the ones you wrote in step 2)
- whether it's **active** (turned on) or not

**Step 4: That's it — don't touch anything else**
No other file in the whole codebase needs to change. You don't edit a master list anywhere.

**Step 5: Let it reach the database**
The system needs one more thing: to tell the actual database "hey, there's a new profession now." That happens automatically — either:
- restart the worker (it does this on startup), or
- run the one manual command if you don't want to wait: `python -m scripts.seed_professions`

That's the whole process: **new folder → tools.py (what it can do) → `__init__.py` (its ID card) → restart or run the sync command.** Nothing else in the repo needs editing.
