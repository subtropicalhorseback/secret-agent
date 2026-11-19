"""
config/routing_constants.example.py — generic router vocabulary (exemplar)
==========================================================================
quick_route() in coordinator.py uses these constants to route short and
keyword-obvious messages without an LLM call. This file ships with the repo as
a neutral starting point; copy it to config/routing_constants.py (gitignored)
and replace the vocabulary with the language your own household actually uses.
If config/routing_constants.py exists it is loaded instead of this file.

Every name below is read by the coordinator. Keep them all defined.
"""

# Messages at or under this word count skip the router and RAG entirely.
FAST_PATH_WORDS = 7

# Exact short replies that skip everything (acks, greetings, filler).
FAST_PATH_EXACT = {
    "yeah", "yep", "nope", "ok", "okay", "k", "noted", "thanks", "ty",
    "got it", "sounds good", "makes sense", "sure", "fine", "no", "yes",
    "correct", "exactly", "right", "agreed", "confirmed", "hmm",
    "interesting", "fair", "understood", "one sec", "never mind",
    "lol", "nice", "good", "great", "perfect", "done",
}

# Any message containing one of these phrases routes to a vault search.
VAULT_PHRASES = [
    "search my files", "look in my documents", "what do my notes say",
    "find the document", "check the vault", "pull up the file",
]

# domain -> keywords. A keyword hit tags the message with that domain so the
# right context sections and RAG filters are applied. Domain names should
# match the domains your vault and config/context/*.md files use.
DOMAIN_KEYWORDS = {
    "home": [
        "garden", "lawn", "garage", "repair", "plumbing", "appliance",
        "pantry", "recipe", "groceries", "maintenance schedule",
    ],
    "health": [
        "doctor", "appointment", "prescription", "symptom", "dentist",
        "checkup", "vaccine", "allergy", "medication",
    ],
    "learning": [
        "homework", "syllabus", "lecture", "assignment", "exam", "study",
        "reading list", "course", "professor",
    ],
    "research": [
        "investigate", "sources", "citation", "literature", "dataset",
        "methodology", "primary source", "background on",
    ],
    "personal": [
        "birthday", "anniversary", "vacation", "gift", "family", "weekend",
    ],
    "professional": [
        "resume", "interview", "career", "meeting", "deadline", "project plan",
        "quarterly", "stakeholder",
    ],
}

# Openers that signal a conversational pivot rather than a lookup.
CONVERSATIONAL_STARTS = [
    "btw", "fyi", "also", "one more thing", "quick question",
    "while we're here", "for context", "actually", "wait,", "hold on",
    "i meant", "i mean", "correction:", "scratch that",
]

# Phrases that signal the message wants real analysis (heavier model).
ANALYTICAL_TRIGGERS = [
    "analyze", "compare", "synthesize", "evaluate", "assess",
    "difference between", "pros and cons", "implications", "break down",
    "walk me through", "explain why", "explain how",
]

# Per-household vocabulary hooks. ADMIN_VOCABULARY holds shorthand that should
# bias routing toward the admin's domains; PARTNER_TOPICS holds topics that
# belong to another user and must be EXCLUDED from the admin's domain signals.
ADMIN_VOCABULARY = set()
PARTNER_TOPICS = set()
