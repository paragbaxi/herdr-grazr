from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")

# Once every account is below the thresholds, another one must be this much
# better off before it is worth a swap, or two low accounts would trade places
# on every message.
FALLBACK_MARGIN = 10

# Going back for a week that ends sooner needs this much above the threshold,
# or it is not worth two swaps. The default; a caller can pass its own
# `return_margin`. It never applies to the fallback above.
RETURN_MARGIN = 10

# A weekly window with at least this much left has just reset.
FRESH = 95

# How long before its reset an account's leftover is worth going back for.
LAST_DAY_SECONDS = 24 * 60 * 60

# A handover is only worth it when the other week ends at least this much
# sooner.
SOONER_BY_SECONDS = 60 * 60

# A weekly threshold over 100 keeps an account off altogether, as `at_reserve` in
# grazr's reserve file sets it, or a `keep` of all of it: no reading clears it,
# and an account with none is not taken as fit either, nor kept while active.
HELD_OFF = 101


def merged(previous, current, now):
    """A window already on record keeps the lowest headroom it has shown. Idle
    panes repeat old figures, and a repeat must not put headroom back. A newer
    window is taken as it comes. An older one comes from a pane that has not
    caught up, and the window on record stays. So does one the payload leaves
    out, since a window can be absent on its own. One that has run out is not
    taken at all, so a repeat cannot bring it back once it was dropped."""
    if not isinstance(previous, list):
        return _live(current, now)
    lowest = {(entry.group, entry.resets_at): entry.remaining for entry in previous}
    newest = {
        (entry.kind, entry.group): entry
        for entry in sorted((entry for entry in previous if entry.resets_at), key=lambda entry: entry.resets_at)
    }
    readings = []
    for entry in current:
        recorded = newest.get((entry.kind, entry.group))
        if recorded and entry.resets_at and entry.resets_at < recorded.resets_at:
            readings.append(recorded)
            continue
        readings.append(
            entry._replace(
                remaining=min(entry.remaining, lowest.get((entry.group, entry.resets_at), entry.remaining))
            )
        )
    covered = {(entry.kind, entry.group) for entry in current}
    readings += [entry for entry in previous if (entry.kind, entry.group) not in covered]
    return _live(readings, now)


# Within its week a session's own weekly figure only falls, as each comes from its
# latest request. One that rises this much was put back early: Anthropic gives an
# account a limit reset now and then (Settings > Usage, "Limit resets"), which puts
# the week back to full and leaves its reset time where it was.
RISE = 10


def _weekly(limits):
    return {
        (entry.group, entry.resets_at): entry.remaining
        for entry in limits
        if entry.group == "weekly" and entry.resets_at
    }


def seen(mine, current, now):
    """A session's weekly windows, (group, resets_at) to (remaining, changed),
    with this payload's: `changed` is when its figure last changed, so an idle
    session that repeats its figure keeps an old time. Ended windows go."""
    out = {key: value for key, value in mine.items() if key[1] > now}
    for key, remaining in _weekly(current).items():
        if key not in out or out[key][0] != remaining:
            out[key] = (remaining, now)
    return out


def rises(mine, current, back):
    """The weekly windows this session saw put back: its own figure rose by
    RISE or more since `mine`, what it showed before. A window `back` says was
    put back since that figure is the same reset, seen late by a session idle
    since: the record stands, and the session's figure counts from now."""
    return {
        key
        for key, remaining in _weekly(current).items()
        if key in mine and remaining >= mine[key][0] + RISE
        and (key not in back or mine[key][1] >= back[key])
    }


def fell(before, mine, key, since):
    """Whether a session's figure of window `key` fell from one it showed before
    `since`, when the week was put back: then it is still the old week's. A
    session stopped at its usage limit and sent on after the reset first shows
    the old week's last figure, spent, and only its next request reads the week
    put back. `before` and `mine` as `seen` keeps them, before and after this
    payload."""
    return key in before and key in mine and before[key][1] < since and mine[key][0] < before[key][0]


def put_back(previous, current, mine, back, rose, before=None):
    """`previous` and `current` as `merged` takes them once a week was put back
    early. `back` maps each such window, (group, resets_at), to when; `rose` is
    those this payload puts back, whose record no longer counts. A figure of a
    window put back counts only if the session's figure changed since, in
    `mine` as `seen` keeps it, and did not fall from one it showed before (see
    `fell`, with `before` the session's figures before this payload): a session
    idle since repeats the old week, and the record stands. A payload no
    session is known for counts, as before."""
    before = before or {}
    if isinstance(previous, list):
        previous = [entry for entry in previous if (entry.group, entry.resets_at) not in rose]
    kept = []
    for entry in current:
        key = (entry.group, entry.resets_at)
        if key in back and key not in rose and key in mine and (
            mine[key][1] < back[key] or fell(before, mine, key, back[key])
        ):
            continue
        kept.append(entry)
    return previous, kept


def moved(previous, current):
    """Whether `current` shows spending the record does not: a window lowered
    or a new one opened. A payload that merely lacks a window, or repeats one,
    shows none."""
    recorded = {
        (entry.kind, entry.group, entry.resets_at): entry.remaining
        for entry in (previous if isinstance(previous, list) else [])
    }
    return any(
        (entry.kind, entry.group, entry.resets_at) not in recorded
        or entry.remaining < recorded[(entry.kind, entry.group, entry.resets_at)]
        for entry in current
    )


def _live(limits, now):
    return [entry for entry in limits if entry.resets_at is None or entry.resets_at > now]


def replaced(previous, current, now):
    """The windows on record that have run out, by the clock or because
    `current` carries a newer one. Their last reading is what expired unused.
    The clock has to count, since a payload drops a window the moment its
    reset passes, and the newer window arrives only with the next request."""
    if not isinstance(previous, list):
        return []
    newest = {(entry.kind, entry.group): entry.resets_at for entry in current if entry.resets_at}
    return [
        entry
        for entry in previous
        if entry.resets_at
        and (entry.resets_at <= now or entry.resets_at < newest.get((entry.kind, entry.group), entry.resets_at))
    ]


def thresholds_of(account_id, thresholds, per_account=None):
    """The thresholds one account is held to: the global ones, with any that
    account sets for itself in `per_account`, which is keyed by account id.
    Every judgement about an account goes through here with that account's
    id, so a reserve kept on one account holds whichever account is active."""
    return dict(thresholds, **(per_account or {}).get(account_id, {}))


def needs_rotation(limits, now, thresholds):
    return held_off(thresholds) or not _has_headroom(limits, now, thresholds)


def held_off(thresholds):
    """Whether these thresholds keep their account off whatever it shows."""
    return thresholds.get("weekly", 0) > 100


def decide(limits, active, accounts, now, thresholds, per_account=None, return_margin=RETURN_MARGIN):
    if not needs_rotation(limits, now, thresholds_of(active, thresholds, per_account)):
        sooner = expiring_sooner(limits, active, accounts, now, thresholds, per_account, return_margin)
        return ("rotate", sooner) if sooner else "stay"
    chosen = next_account(active, accounts, now, thresholds, per_account)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    others = [entry for entry in accounts if entry.id != active]
    if not others:
        return "unenrolled"
    # Thresholds an account sets for itself are a floor. Every other account is
    # below its thresholds by now, so one with a floor of its own is never
    # moved into, and an active one below its floor is left whatever the
    # margin: an account without a reserve is drained before one with it.
    drainable = [entry for entry in others if not _keeps_floor(entry.id, per_account)]
    if not drainable:
        return "exhausted"
    best = max(drainable, key=lambda entry: _worst_window(entry.snapshot, now, thresholds))
    if _keeps_floor(active, per_account):
        return "rotate", best.id
    if _worst_window(best.snapshot, now, thresholds) >= _worst_window(limits, now, thresholds) + FALLBACK_MARGIN:
        return "rotate", best.id
    return "exhausted"


def _keeps_floor(account_id, per_account):
    return bool((per_account or {}).get(account_id))


def _worst_window(limits, now, thresholds):
    """How close to the wall an account is: the least left on any live window
    that has a threshold."""
    return min(
        limit.remaining
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    )


def expiring_sooner(limits, active, accounts, now, thresholds, per_account=None, return_margin=RETURN_MARGIN):
    """The first account fit to take over whose week ends before the active
    account's, or None. What it has left is lost at its reset unless spent
    first. So that this does not undo every session swap five hours later, it
    fires only when the active week has just reset or the other account's
    week is in its last day, and only when enough is left there to be worth
    two swaps. A lightly used week reads as fresh for days, so the floor
    applies to both. A `return_margin` of None never goes back: another tool
    places the account, and grazr moves only off one below its thresholds."""
    if return_margin is None:
        return None
    weeks = [limit for limit in limits if limit.group == "weekly" and limit.resets_at and limit.resets_at > now]
    if not weeks:
        return None
    ends = min(limit.resets_at for limit in weeks)
    fresh = any(limit.remaining >= FRESH for limit in weeks)
    for candidate in accounts:
        if candidate.id == active or not isinstance(candidate.snapshot, list):
            continue
        own = thresholds_of(candidate.id, thresholds, per_account)
        if held_off(own) or not _has_headroom(candidate.snapshot, now, own):
            continue
        for limit in candidate.snapshot:
            if limit.group != "weekly" or not limit.resets_at or not now < limit.resets_at:
                continue
            if (ends - limit.resets_at).total_seconds() < SOONER_BY_SECONDS:
                continue
            worth_it = limit.remaining >= own.get("weekly", 0) + return_margin
            last_day = (limit.resets_at - now).total_seconds() <= LAST_DAY_SECONDS
            if worth_it and (fresh or last_day):
                return candidate.id
    return None


def next_account(active, accounts, now, thresholds, per_account=None):
    """The first account in preference order fit to take over, or None. Says
    nothing about whether the active one needs replacing. Each candidate is
    held to its own thresholds, never to the active account's."""
    for candidate in accounts:
        if candidate.id == active:
            continue
        own = thresholds_of(candidate.id, thresholds, per_account)
        if held_off(own):
            continue
        if candidate.snapshot is None or _has_headroom(candidate.snapshot, now, own):
            return candidate.id
    return None


def spent(limits, now, thresholds):
    """The live windows below their thresholds: what keeps an account out.
    Every judgement of spent goes through here, the decision's and the toast's
    alike, so the two never disagree on an account."""
    return [
        limit
        for limit in limits
        if limit.group in thresholds
        and limit.remaining < thresholds[limit.group]
        and (limit.resets_at is None or limit.resets_at > now)
    ]


def shortfall(limits, now, thresholds):
    """The first live window below its threshold, or None."""
    return next(iter(spent(limits, now, thresholds)), None)


def _has_headroom(limits, now, thresholds):
    return shortfall(limits, now, thresholds) is None
