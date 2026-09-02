"""ticketbot.models: provider-neutral message/tool types plus the swappable
`ModelProvider` implementations (`anthropic`, `openai_compat`, `fake`).

The engine only ever imports `ticketbot.models.base` and `ticketbot.core.registry`
— it never imports a concrete provider module directly, so "any AI" stays true.
"""
