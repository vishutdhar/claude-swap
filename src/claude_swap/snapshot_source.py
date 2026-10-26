"""Snapshot source — the supported read path for dashboards and GUI shells.

Pacing is store-governed: the usage store's persisted poll plans plus its
freshness/backoff/claim gates (decided atomically in ``UsageStore.reserve``)
cap every surface at the same per-account cadence, so a dashboard repainting
every few seconds and a one-shot ``cswap list`` produce identical network
behavior. This class therefore just runs the same on-demand pass as ``cswap
list`` (``fetch=None``) each take — the store decides which accounts, if
any, may actually be fetched — and offers ``store_only`` for shells that
host an auto engine (which already collects on its own schedule).

``take()`` is blocking (file locks, keychain subprocesses, network): call it
from a background thread, never a UI event loop.
"""

from __future__ import annotations

import threading
from dataclasses import replace

from claude_swap.json_output import DEAD_LOGIN_SENTINELS, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


class SnapshotSource:
    """Takes one coherent snapshot per call; the store paces the network.

    ``full=True`` is accepted for API stability but is no faster than a
    normal pass: it is capped by the store's serve TTL and poll plans.
    ``fresh=True`` (the menu bar's Refresh now) re-measures every candidate
    past the serve TTL and the poll plans, exactly as ``cswap list --fresh``
    does; the store still refuses quarantined, backed-off, claimed and
    recently-429'd slots. ``store_only=True`` reads the store without any
    network eligibility.
    """

    def __init__(self, switcher: ClaudeAccountSwitcher) -> None:
        self.switcher = switcher
        self._last: AccountsSnapshot | None = None
        self._lock = threading.Lock()

    def take(
        self, *, full: bool = False, store_only: bool = False, fresh: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        fetch: set[str] | None = set() if store_only else None
        # `fresh` rides along only when asked for, as `cswap list --fresh`
        # does, so every other take stays the call it always was.
        snap = self.switcher.accounts_snapshot(
            fetch=fetch, **({"fresh": True} if fresh else {})
        )
        with self._lock:
            snap = self._reconcile(snap)
            self._last = snap
            return snap

    def _reconcile(self, snap: AccountsSnapshot) -> AccountsSnapshot:
        if self._last is None:
            return snap
        previous = {acc.number: acc for acc in self._last.accounts}
        accounts = tuple(
            self._reconcile_account(acc, previous.get(acc.number), snap.taken_at)
            for acc in snap.accounts
        )
        return replace(snap, accounts=accounts)

    def _reconcile_account(
        self,
        acc: AccountSnapshot,
        prev: AccountSnapshot | None,
        taken_at: float,
    ) -> AccountSnapshot:
        if prev is None or account_identity(acc) != account_identity(prev):
            return acc

        prev_fetched = prev.usage.fetched_at
        fetched = acc.usage.fetched_at
        if prev_fetched is not None and (fetched is None or fetched < prev_fetched):
            # The read is behind the one already shown (a late worker, or a
            # read with no reading at all): keep showing the newer reading.
            carried = _with_current_age(prev.usage, taken_at)
            if (
                prev.usage.sentinel in DEAD_LOGIN_SENTINELS
                and acc.usage.sentinel != prev.usage.sentinel
            ):
                # Except a dead login the current read no longer reports.
                # Only a change of the slot's credential ends one, and a
                # held verdict tells the user to log in again, so it is
                # never carried. A read with an older reading of its own
                # keeps the newer numbers under its own verdict. A read with
                # no reading keeps nothing of the dead entry: the slot shows
                # it has no reading yet, and such a row is due on the next
                # pass.
                if fetched is None:
                    return acc
                carried = replace(carried, sentinel=acc.usage.sentinel)
            return replace(acc, usage=carried)
        if acc.usage.sentinel is not None:
            return acc

        if (
            prev.usage.sentinel == USAGE_TOKEN_EXPIRED
            and fetched == prev_fetched
        ):
            return replace(acc, usage=replace(acc.usage, sentinel=USAGE_TOKEN_EXPIRED))
        return acc


def account_identity(acc: AccountSnapshot) -> tuple[str, str, str]:
    """The identity discriminator for a slot across snapshots.

    The single definition of "same account": snapshot reconciliation here and
    the TUI's snapshot merge must agree, or an identity change (e.g. a slot
    re-used by another login) is detected by one and missed by the other.
    """
    return (acc.email, acc.org_uuid, acc.kind)


def _with_current_age(usage: UsageEntry, taken_at: float) -> UsageEntry:
    fetched = usage.fetched_at
    if fetched is None:
        return usage
    return replace(usage, age_s=max(0.0, taken_at - fetched))
