"""Deterministic, local-only email thread reconstruction (Communications
Phase Step 4).

Pure grouping logic only -- no database access, no ORM, no I/O -- so it
can be unit tested directly against plain data (see
tests/test_communications_threading.py). `app/core/communications/thread_rebuild.py`
is the thin DB-facing layer that feeds `Communication` rows into
`group_messages()` and reconciles the result against `communication_threads`.

Algorithm, in the approved precedence order:

1. `Message-ID` -- a message's own Message-ID is never a *source* of a
   link (it doesn't reference anything by having one), but it is the
   *target* every other message's In-Reply-To/References are checked
   against. Two messages with the *same* Message-ID would be a dedup
   collision, prevented upstream (plan §7); this module never merges on
   Message-ID equality between two different rows.
2. `In-Reply-To` -- if present, its value is looked up against every
   other message's Message-ID; a match links the two.
3. `References` -- likewise, each id in the (possibly multi-valued)
   References chain is looked up the same way. In-Reply-To and
   References are both objective, RFC 5322-defined evidence of a real
   reply relationship (a compliant client's References is a superset
   that includes whatever it put in In-Reply-To), so both are used
   together to build one linkage graph rather than treated as mutually
   exclusive alternatives -- this is strictly more complete (see the
   "reply imported before its parent" case: In-Reply-To may name a
   message not yet imported while an *earlier* ancestor in References
   already is) and can never produce a link that In-Reply-To-only
   matching wouldn't already justify, since both are still exact
   Message-ID matches, not fuzzy signals.
4. Fallback (normalized subject + participants + date proximity) is
   used *only* for a message that has none of the three headers above
   at all (see `_is_headerless`). A message that carries any real
   threading header is never grouped by the fuzzy fallback, even if its
   header doesn't happen to resolve to a match -- if there had been a
   real reply relationship, the header would show it, so guessing one
   in its place would risk a false positive the plan explicitly forbids
   ("prefer false negatives ... leave messages unthreaded rather than
   incorrectly joining unrelated emails").

A connected component of size 1 (no real linkage to anything) is left
unthreaded -- `communication_threads` rows only ever represent an actual
multi-message conversation, matching the UI requirement that a lone
message must not show a broken/empty thread link.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

# How close two headerless messages' dates must be to be considered part
# of the same fallback-matched conversation. Deliberately generous enough
# to span a slow-moving school email exchange, but bounded -- unbounded
# proximity would eventually match unrelated messages that happen to
# share a subject and one participant.
FALLBACK_DATE_PROXIMITY = timedelta(days=30)

_REPLY_FORWARD_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd|aw)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    """Strip conservative, repeated reply/forward prefixes (`Re:`,
    `Fwd:`, `Fw:`, `Aw:`, optionally `Re[2]:`-style) and collapse
    whitespace, for grouping/display purposes only.

    Never used to alter `communications.subject` -- the stored original
    is untouched regardless of what this returns.
    """
    if not subject:
        return ""
    text = subject
    while True:
        match = _REPLY_FORWARD_PREFIX_RE.match(text)
        if not match:
            break
        text = text[match.end() :]
    return " ".join(text.split())


def _participant_set(
    from_address: str | None, to_addresses: Sequence[str], cc_addresses: Sequence[str]
) -> frozenset[str]:
    values = list(to_addresses) + list(cc_addresses)
    if from_address:
        values.append(from_address)
    return frozenset(v.strip().lower() for v in values if v and v.strip())


@dataclass(frozen=True)
class ThreadableMessage:
    """The subset of a `Communication` row's fields threading needs.

    Deliberately plain data, not the ORM model -- keeps this module's
    logic testable with zero database setup.
    """

    communication_id: int
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    subject: str | None
    from_address: str | None
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    message_date: datetime | None
    case_id: int | None


@dataclass(frozen=True)
class ThreadGroup:
    """One reconstructed conversation -- always 2+ member messages."""

    communication_ids: tuple[int, ...]
    subject_normalized: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    participants: tuple[str, ...]
    case_id: int | None


class _UnionFind:
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1


def _is_headerless(message: ThreadableMessage) -> bool:
    return not message.message_id and not message.in_reply_to and not message.references


def _linked_ids(message: ThreadableMessage) -> list[str]:
    ids: list[str] = []
    if message.in_reply_to:
        ids.append(message.in_reply_to)
    for ref in message.references:
        if ref and ref not in ids:
            ids.append(ref)
    return ids


def group_messages(messages: Sequence[ThreadableMessage]) -> list[ThreadGroup]:
    """Partition `messages` into reconstructed conversations.

    Deterministic and idempotent: the same input always yields the same
    partition (grouping depends only on message content, never on import
    order, prior thread assignment, or any other mutable state), which is
    what lets the DB-facing rebuild reconcile safely no matter how many
    times it runs or in what order messages were imported.
    """
    n = len(messages)
    union_find = _UnionFind(n)

    by_message_id: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.message_id and message.message_id not in by_message_id:
            by_message_id[message.message_id] = index

    for index, message in enumerate(messages):
        for ref_id in _linked_ids(message):
            target = by_message_id.get(ref_id)
            if target is not None and target != index:
                union_find.union(index, target)

    # Sort dateless messages after dated ones without ever comparing a
    # placeholder against a real date directly: tuple comparison only
    # inspects the second element when the first (the "has no date" flag)
    # is equal, so a naive placeholder here is never `<`-compared against
    # a caller-supplied date of unknown (naive or aware) awareness.
    headerless_indices = [i for i, m in enumerate(messages) if _is_headerless(m)]
    headerless_indices.sort(
        key=lambda i: (
            messages[i].message_date is None,
            messages[i].message_date or datetime.min,
            messages[i].communication_id,
        )
    )

    open_groups: list[dict] = []
    for index in headerless_indices:
        message = messages[index]
        subject_key = normalize_subject(message.subject).casefold()
        participants = _participant_set(message.from_address, message.to_addresses, message.cc_addresses)
        date = message.message_date

        matched = None
        if subject_key and participants and date is not None:
            for group in open_groups:
                if group["subject_key"] != subject_key:
                    continue
                if not (group["participants"] & participants):
                    continue
                if group["last_date"] is None:
                    # The group's most recent member has no date of its
                    # own -- proximity can't be verified, so don't guess.
                    continue
                if abs(date - group["last_date"]) > FALLBACK_DATE_PROXIMITY:
                    continue
                matched = group
                break

        if matched is not None:
            union_find.union(index, matched["representative"])
            matched["participants"] = matched["participants"] | participants
            matched["last_date"] = date
        else:
            open_groups.append(
                {
                    "subject_key": subject_key,
                    "participants": participants,
                    "last_date": date,
                    "representative": index,
                }
            )

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(n):
        components[union_find.find(index)].append(index)

    groups: list[ThreadGroup] = []
    for indices in components.values():
        if len(indices) < 2:
            continue
        members = [messages[i] for i in indices]
        dated = [m for m in members if m.message_date is not None]
        first_at = min((m.message_date for m in dated), default=None)
        last_at = max((m.message_date for m in dated), default=None)
        earliest = min(dated, key=lambda m: m.message_date) if dated else min(members, key=lambda m: m.communication_id)
        participants: set[str] = set()
        for m in members:
            participants |= _participant_set(m.from_address, m.to_addresses, m.cc_addresses)
        case_ids = {m.case_id for m in members if m.case_id is not None}
        case_id = next(iter(case_ids)) if len(case_ids) == 1 else None

        groups.append(
            ThreadGroup(
                communication_ids=tuple(sorted(m.communication_id for m in members)),
                subject_normalized=normalize_subject(earliest.subject) or None,
                first_message_at=first_at,
                last_message_at=last_at,
                participants=tuple(sorted(participants)),
                case_id=case_id,
            )
        )

    groups.sort(key=lambda g: g.communication_ids[0])
    return groups
