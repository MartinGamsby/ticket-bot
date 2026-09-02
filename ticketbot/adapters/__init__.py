"""ticketbot.adapters: the swappable edges — sources, sinks, runtimes and repos.

Everything under here is reached only through `core.registry`, by a `type:` name
from config, so adding a new adapter never requires touching the engine.
"""
