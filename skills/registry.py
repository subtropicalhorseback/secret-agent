"""
registry.py - Chip Skill Registry
Skills self-register using @register("intent_name", help="description")
Handler signature: async def handler(session, sender, clean_text, rag_context, history)
Handler must return: (answer: str, force_file: bool)
"""

SKILL_REGISTRY = {}
SKILL_HELP     = {}  # intent -> help text for /help
cancel_flags   = {}  # sender -> True if cancel requested


def register(intent, help=None):
    def decorator(fn):
        SKILL_REGISTRY[intent] = fn
        if help:
            SKILL_HELP[intent] = help
        return fn
    return decorator


def get_full_help(base_commands):
    """Merge base COMMANDS dict with registered skill help text."""
    combined = dict(base_commands)
    for intent, text in SKILL_HELP.items():
        if intent not in combined:
            combined[f"!{intent}"] = text
    return combined


def hot_load(skill_name):
    """Dynamically load or reload a skill without restarting coordinator."""
    import importlib
    try:
        mod = importlib.import_module(f"skills.{skill_name}")
        importlib.reload(mod)
        return skill_name in SKILL_REGISTRY
    except Exception as e:
        print(f"Hot load failed for {skill_name}: {e}")
        return False
