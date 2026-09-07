"""How much of a conversation one turn may read, and how far back.

These are internal defaults, not settings. Every number here is a bound on
work the person never asked for by name — pages fetched, model calls made
inside the retrieval sub-agent, characters handed back to the parent — and a
deployment that needed to tune them would be tuning the wrong thing: the
retrieval loop stops when it has enough evidence, not when it runs out of
budget. The budget exists so that a pathological conversation (a channel with
40,000 messages, a thread with 900 replies) cannot turn one question into an
unbounded read.

Nothing here reaches the environment, so there is no new configuration to
carry, and no way for a misconfigured deployment to make retrieval unbounded.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscussionPolicy:
    """Bounds for reading the conversation around a message.

    Attributes:
        first_page: Messages read by the first, un-parameterised look at a
            channel — the "last ten messages" a person means by "above".
        page_size: Messages per page for every later read.
        max_page_size: Ceiling on a page size the model asks for.
        thread_priming: Thread messages read automatically the first time the
            bot is drawn into an existing thread.
        max_records_per_turn: Total messages one turn may take back from
            retrieval, across every call and every page.
        max_steps: Model calls the retrieval sub-agent may make before it must
            report what it has.
        max_runs_per_turn: How many times the parent may run retrieval in one
            turn, so a follow-up question is possible and a loop is not.
        excerpt_chars: Longest single message body handed to the sub-agent;
            longer ones are cut with a marker.
        quote_chars: Longest verbatim quote carried into the digest.
        max_digest_chars: Ceiling on the whole digest the parent receives.
        search_scan_pages: Pages of channel history one search may scan.
        max_sources: Sources cited back to the parent.
    """

    first_page: int = 10
    page_size: int = 20
    max_page_size: int = 50
    thread_priming: int = 10
    max_records_per_turn: int = 120
    max_steps: int = 4
    max_runs_per_turn: int = 2
    excerpt_chars: int = 700
    quote_chars: int = 400
    max_digest_chars: int = 6000
    search_scan_pages: int = 6
    max_sources: int = 12


POLICY = DiscussionPolicy()

__all__ = ["POLICY", "DiscussionPolicy"]
