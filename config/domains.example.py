"""
config/domains.example.py — vault domains and analysis style (exemplar)
=======================================================================
Copy to config/domains.py (gitignored) and describe your own household. If
config/domains.py exists it is loaded instead of this file. Every name below
is read by the code; keep them all defined.
"""

# name -> one-line description used by the nightly document classifier.
# Keep "general" as the catch-all.
VAULT_DOMAINS = {
    "home":         "house, garden, repairs, appliances, vehicles",
    "health":       "medical, dental, fitness, pets",
    "learning":     "coursework, study notes, reading",
    "research":     "background research, reports, reference material",
    "personal":     "personal financial, records, employment, legal",
    "professional": "work deliverables, career, professional correspondence",
    "general":      "everything else",
}

# Domains accepted as a "domain:" prefix on uploads and vault commands.
PREFIX_DOMAINS = ["home", "health", "learning", "research", "general", "private"]

# Domains never flagged sensitive by keyword, and domains always treated as sensitive.
SENSITIVITY_IMMUNE = {"learning", "research"}
ALWAYS_SENSITIVE   = {"personal"}

# Domains whose scanned documents get OCR first when re-chunking.
OCR_PRIORITY_DOMAINS = {"personal", "private", "health"}

# Extra regexes appended to the generic sensitivity patterns that ship in code.
EXTRA_SENSITIVITY_PATTERNS = []

# Synced folder name -> (domain, sensitive 0/1). None = never ingest that folder.
FOLDER_MAP = {
    "home":         ("home",         0),
    "health":       ("health",       1),
    "learning":     ("learning",     0),
    "research":     ("research",     0),
    "private":      ("private",      1),
    "general":      ("general",      0),
    "unclassified": ("unclassified", 0),
    "intake":       ("unclassified", 0),
}

# Example domains offered to the per-message taxonomy tagger.
TAXONOMY_EXAMPLES = "home, health, learning, research, professional, personal, media, finance"

# House style for confidence language in the analysis pipelines.
ANALYSIS_LANGUAGE = "calibrated probabilistic language (almost certain, likely, roughly even, unlikely, remote)"
