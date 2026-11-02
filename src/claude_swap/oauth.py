"""OAuth token management and usage API for Claude Code accounts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.codex_usage import MAX_RESET_CREDITS, finite_float
from claude_swap.printer import warning as print_warning

OAUTH_BETA_HEADER = "oauth-2025-04-20"
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

_logger = logging.getLogger("claude-swap")


def extract_access_token(credentials: str) -> str | None:
    """Extract the OAuth access token from a credentials JSON string."""
    try:
        data = json.loads(credentials)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_oauth_data(credentials: str) -> dict | None:
    """Extract the Claude AI OAuth payload from a credentials JSON string."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def credential_fingerprint(credentials: str) -> str | None:
    """Stable identity fingerprint for a stored credential.

    Refresh-token hash when one exists (survives access-token rotation, so two
    generations of the same OAuth lineage compare equal); full-content hash
    otherwise (API keys and setup-tokens rotate never, so content identity is
    lineage identity). None only for empty input — a caller comparing "did the
    credential change?" must never get None for real bytes, or every
    comparison against it would degenerate to "changed".
    """
    if not credentials:
        return None
    data = extract_oauth_data(credentials)
    token = data.get("refreshToken") if data else None
    if isinstance(token, str) and token:
        return "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    return "sha256-full:" + hashlib.sha256(credentials.encode()).hexdigest()


def is_oauth_token_expired(expires_at: object) -> bool:
    """Return whether an OAuth token is expired or about to expire."""
    if not isinstance(expires_at, (int, float)):
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a refresh-token grant attempt.

    ``credentials`` is the full rotated credentials JSON on success, else None.
    ``error`` classifies failures so callers can distinguish a dead refresh-token
    lineage (permanent: quarantine, stop retrying) from a network blip
    (transient: retry later):

    - ``None`` — success (``credentials`` is set)
    - ``"invalid_grant"`` — the token endpoint rejected the grant; this refresh
      token is dead and re-login is required
    - ``"no_refresh_token"`` — the stored credential carries no usable refresh
      token (also permanent for retry purposes)
    - ``"transient"`` — network/server error; the token may still be valid

    ``token_account`` is the account identity the token endpoint optionally
    includes alongside a successful grant (``{"uuid", "email",
    "organizationUuid"}``, fields possibly None) — a zero-request identity
    source for the credential that was just refreshed. None when the server
    omitted it or on failure; callers must treat it as opportunistic.
    """

    credentials: str | None
    error: str | None
    token_account: dict | None = None
    # Fingerprint of the generation actually consumed (POSTed). Set by the
    # consume gate, which may substitute a fresher re-read or a session
    # profile for the caller's snapshot — strike binding must follow the
    # POSTed bytes, not the snapshot.
    consumed_fp: str | None = None
    # Did the consumed successor actually reach the stash? Only meaningful on
    # a demoted (`transient` WITH credentials) outcome from the consume gate.
    # False there means the `consume-gate-unpersisted` corner: BOTH the
    # persist and the stash write failed, so the successor survives only in
    # `credentials` and retrying POSTs the spent predecessor. Callers that
    # tell the user what to do next must not promise a stash that never
    # happened.
    stashed: bool = False


def try_refresh_oauth_credentials(
    credentials: str, timeout_s: float = 10.0
) -> RefreshOutcome:
    """Refresh an OAuth access token via direct token endpoint POST.

    ``timeout_s`` bounds the network exchange. Callers that hold locks other
    processes contend for should pass a budget comfortably inside the
    contenders' acquire timeout (see ``_fetch_active_usage``).
    """
    # ``no_refresh_token`` is a PERMANENT verdict (it strikes at
    # AUTH_DEAD_STRIKES=1), so it demands a structurally complete OAuth dict
    # genuinely missing the field. An unparseable or non-dict blob is more
    # likely a torn/partial read than a real credential shape — transient:
    # the next pass re-reads and either succeeds or sees the true shape.
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return RefreshOutcome(None, "transient")
    if not isinstance(data, dict):
        return RefreshOutcome(None, "transient")
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return RefreshOutcome(None, "no_refresh_token")

    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": OAUTH_CLIENT_ID,
        }).encode()

        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "claude-swap/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_data = json.loads(resp.read().decode())

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        oauth["accessToken"] = resp_data["access_token"]
        oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
        if resp_data.get("refresh_token"):
            oauth["refreshToken"] = resp_data["refresh_token"]
        if resp_data.get("scope"):
            oauth["scopes"] = resp_data["scope"].split()

        data["claudeAiOauth"] = oauth
        return RefreshOutcome(
            json.dumps(data), None, _parse_token_account(resp_data)
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        _logger.debug("OAuth refresh failed: %r, body: %s", e, body[:500])
        # Permanent only when the server itself rejected the grant: a 4xx AND
        # an explicit marker in the body. Anything ambiguous stays transient —
        # a misclassified transient costs one retry, a misclassified permanent
        # would wrongly quarantine a live token.
        if e.code in (400, 401, 403):
            # RFC 6749 §5.2: the verdict is the top-level ``error`` member of
            # the JSON body. A substring scan misclassifies — the marker can
            # appear inside another envelope's detail text, and a dead-token
            # verdict at AUTH_DEAD_STRIKES=1 quarantines the slot on the
            # spot. Unparseable bodies stay transient (a misclassified
            # transient costs one retry; a misclassified permanent wrongly
            # quarantines a live token).
            try:
                err = json.loads(body).get("error")
            except (ValueError, AttributeError):
                err = None
            # invalid_grant: this slot's refresh lineage is dead.
            # invalid_client: OUR client credential was rejected — systemic
            # (client_id rotated/blocked), no evidence about any slot, so it
            # keeps its own kind and lands no strike.
            if err in ("invalid_grant", "invalid_client"):
                return RefreshOutcome(None, err)
        return RefreshOutcome(None, "transient")
    except Exception as e:
        _logger.debug("OAuth refresh failed: %r", e)
        return RefreshOutcome(None, "transient")


def _parse_token_account(resp_data: dict) -> dict | None:
    """Extract the optional account identity from a token-endpoint response.

    The refresh grant's response body may carry ``account`` / ``organization``
    objects naming who the rotated token belongs to (Claude Code surfaces the
    same fields as ``tokenAccount``). Absent in some responses — never rely on
    it, but never discard it either. Same strict boundary as
    ``fetch_oauth_profile``: usable identity requires a non-empty string
    ``account.uuid`` (consumers backfill and compare by uuid), optional
    fields are normalized to str-or-None, and anything malformed is None —
    this identity is opportunistic and must never break the refresh that
    carried it.
    """
    account = resp_data.get("account")
    if not isinstance(account, dict):
        return None
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    email = account.get("email_address")
    organization = resp_data.get("organization")
    org_uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return {
        "uuid": uuid.strip(),
        "email": email if isinstance(email, str) else None,
        "organizationUuid": org_uuid if isinstance(org_uuid, str) else None,
    }


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh an OAuth access token; None on any failure (see RefreshOutcome)."""
    return try_refresh_oauth_credentials(credentials).credentials


def fetch_oauth_profile(access_token: str) -> dict | None:
    """Resolve an OAuth access token to its account identity, or None.

    ``GET /api/oauth/profile`` answers the one question the credential bytes
    can't: *whose* token is this. Returns ``{"uuid", "email",
    "organizationUuid"}`` or None on any failure — callers treat None as
    "unresolvable", never as an error. The identity oracle is strictly
    advisory (a switch proceeds pre-fix on None), so the boundary is strict
    the other way: a response counts as resolved only when it carries a
    non-empty string ``account.uuid`` — a response without a usable uuid,
    including a schema change that renames it, is None, keeping that drift
    on the fail-open path rather than silently degrading switches.
    ``email``/``organizationUuid`` are optional (str-or-None); a uuid-only
    response *does* resolve, and classification decides whether such partial
    evidence is sufficient for each decision. Must not be called while any
    credential/config lock is held (network under locks is forbidden).
    """
    url = "https://api.anthropic.com/api/oauth/profile"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Evidence, not proof: the live access token can't authenticate.
            # A freshly rotated own-credential would carry a fresh token, but
            # this also fires benignly (family rotated, then the access token
            # expired on an idle machine). Log-file only — the caller falls
            # back to pre-fix behavior and the user sees nothing.
            _logger.warning(
                "OAuth profile returned 401 while resolving credential "
                "ownership; proceeding without identity (pre-fix behavior)."
            )
        else:
            _logger.debug("OAuth profile fetch failed: %r", e)
        return None
    except Exception as e:
        _logger.debug("OAuth profile fetch failed: %r", e)
        return None
    account = data.get("account") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        _logger.debug("OAuth profile response missing account object")
        return None
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        _logger.debug("OAuth profile response missing account.uuid")
        return None
    email = account.get("email")
    organization = data.get("organization")
    org_uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return {
        "uuid": uuid.strip(),
        "email": email if isinstance(email, str) else None,
        "organizationUuid": org_uuid if isinstance(org_uuid, str) else None,
    }



def build_token_status(credentials: str) -> str | None:
    """Return a short debug summary of stored OAuth token state."""
    oauth = extract_oauth_data(credentials)
    if not oauth:
        return None

    has_refresh_token = bool(oauth.get("refreshToken"))
    expires_at = oauth.get("expiresAt")
    refresh_str = "yes" if has_refresh_token else "no"

    if not isinstance(expires_at, (int, float)):
        return f"oauth: unknown expiry, refresh token {refresh_str}"

    expires_utc = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
    state = "expired" if is_oauth_token_expired(expires_at) else "fresh"
    countdown, clock = format_reset(expires_utc.isoformat())
    return f"oauth: {state}, refresh token {refresh_str}, expires {clock} in {countdown}"


def format_reset(resets_at: str) -> tuple[str, str]:
    """Return (countdown, clock) for a reset time in local time."""
    reset_utc = datetime.fromisoformat(resets_at)
    now = datetime.now(timezone.utc)
    remaining = reset_utc - now
    total_seconds = max(0, int(remaining.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        countdown = f"{days}d {hours}h"
    elif hours > 0:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"

    return countdown, reset_clock_string(reset_utc, now)


def reset_clock_string(reset_utc: datetime, now_utc: datetime) -> str:
    """Absolute reset time in local time: "20:39" same-day, else "Jul 5 08:59"."""
    reset_local = reset_utc.astimezone()
    now_local = now_utc.astimezone()
    if reset_local.date() == now_local.date():
        return reset_local.strftime("%H:%M")
    day = str(reset_local.day)
    return reset_local.strftime(f"%b {day} %H:%M")


def fresh_reset_strings(window: dict) -> tuple[str, str] | None:
    """``(countdown, clock)`` for one usage window, or None when unknown.

    Recomputed from ``resets_at`` at render time: the strings cached at fetch
    time drift as the measurement ages (a countdown frozen 2h ago overstates
    the remaining wait by those 2h, and a same-day "15:30" clock silently
    starts meaning yesterday). Entries persisted without ``resets_at`` fall
    back to the fetch-time strings — stale beats blank.
    """
    resets_at = window.get("resets_at")
    if resets_at:
        try:
            return format_reset(resets_at)
        except (ValueError, TypeError):
            pass  # unparseable cached value — fall back below
    if "clock" in window:
        return window.get("countdown", "?"), window["clock"]
    return None


def request_usage_data(access_token: str) -> dict:
    """Request raw utilization data from the Anthropic usage API."""
    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


# ---- limit resets ----------------------------------------------------------
#
# Some accounts hold a banked "limit reset" (claude.ai Settings > Usage, "Limit
# resets"). The usage endpoint reports them in a ``cedar_ember`` object, but
# only when asked (``?cedar_ember=1``) and only to Claude Code's own client:
# with cswap's User-Agent it answers ``ineligible_reason: "surface"`` and no
# grants. So this one read mirrors the User-Agent of the Claude Code install
# the token was issued to. It is a separate, rare call (paced per account by
# the usage store), never a change to the paced usage poll above, which
# keeps its own User-Agent and URL: the endpoint budgets polling
# per surface, and this is Claude Code's surface. Nothing here ever uses a
# reset; the read only displays.

LIMIT_RESETS_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
# The Claude Code version named when none can be read from the install: the
# one the cedar_ember answer was verified against on 2026-11-02.
CLAUDE_CODE_VERSION_FALLBACK = "2.1.316"
CLAUDE_CODE_VERSIONS_DIR = Path(".local") / "share" / "claude" / "versions"  # under the home folder
CLAUDE_VERSION_TIMEOUT_S = 5.0
# A version as Claude Code names its installs ("2.1.316"). Nothing else may
# reach the header: a name with a space or a line break would forge one.
_VERSION_RE = re.compile(r"\d{1,4}(?:\.\d{1,6}){1,3}")

_CLAUDE_CODE_UA: str | None = None
_CLAUDE_CODE_UA_LOCK = threading.Lock()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _installed(entry: os.DirEntry) -> bool:
    """Whether a versions-folder entry is an install: a file (as Claude Code
    keeps them) or a folder, its links followed. A link whose target is
    gone, or is anything else, is not."""
    try:
        return entry.is_file() or entry.is_dir()
    except OSError:
        return False


def _run_claude_version(binary: str | None = None) -> str | None:
    """What ``claude --version`` prints, or None when it cannot be run.

    The menu bar's launch agent may not have ``~/.local/bin`` on its PATH,
    so the usual install place is tried after the PATH lookup.
    """
    if binary is None:
        binary = shutil.which("claude")
        if binary is None:
            fallback = Path.home() / ".local" / "bin" / "claude"
            binary = str(fallback) if fallback.is_file() else None
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLAUDE_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout if done.returncode == 0 else None


def claude_code_version(
    versions_dir: Path | None = None, run: Callable[[], str | None] | None = None
) -> str:
    """The installed Claude Code's version, for the resets read's User-Agent.

    The newest version-named install under ``~/.local/share/claude/versions``
    (compared by number: 2.1.316 is newer than 2.1.99; a dangling link is
    skipped), else the first word of ``claude --version``, else
    ``CLAUDE_CODE_VERSION_FALLBACK``. Only a plain dotted number is ever
    returned.
    """
    folder = versions_dir if versions_dir is not None else Path.home() / CLAUDE_CODE_VERSIONS_DIR
    try:
        with os.scandir(folder) as entries:
            names = [
                entry.name
                for entry in entries
                if _VERSION_RE.fullmatch(entry.name) and _installed(entry)
            ]
    except OSError:
        names = []
    if names:
        return max(names, key=_version_key)
    try:
        answer = (run or _run_claude_version)()
    except Exception:
        answer = None
    words = answer.split() if isinstance(answer, str) else []
    if words and _VERSION_RE.fullmatch(words[0]):
        return words[0]
    return CLAUDE_CODE_VERSION_FALLBACK


def claude_code_user_agent() -> str:
    """Claude Code's own User-Agent (``claude-cli/2.1.316 (external, cli)``),
    the version discovered once per process."""
    global _CLAUDE_CODE_UA
    with _CLAUDE_CODE_UA_LOCK:
        if _CLAUDE_CODE_UA is None:
            _CLAUDE_CODE_UA = f"claude-cli/{claude_code_version()} (external, cli)"
        return _CLAUDE_CODE_UA


def request_limit_resets(access_token: str) -> dict | None:
    """The account's ``cedar_ember`` object, or None when nothing was learned.

    One GET of the usage endpoint with ``cedar_ember=1&skip_spend=1`` and
    Claude Code's User-Agent. None on any failure (network, HTTP status, a
    body that is not JSON, no ``cedar_ember`` object), and also for a
    ``"surface"`` refusal: then the server did not judge this client's
    grants at all, which says nothing about whether the account has one.
    Failures are logged at debug level, without the token. Never raises.
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "User-Agent": claude_code_user_agent(),
        }
        req = urllib.request.Request(LIMIT_RESETS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        kind, _retry_after = _classify_usage_error(e)
        _logger.debug("Limit resets read failed: %s", kind)
        return None
    cedar = data.get("cedar_ember") if isinstance(data, dict) else None
    if not isinstance(cedar, dict):
        _logger.debug("Limit resets read: the answer carried no cedar_ember object")
        return None
    if cedar.get("ineligible_reason") == "surface":
        _logger.debug("Limit resets read: refused for this client (surface)")
        return None
    return cedar


def _grant_count(value) -> int | None:
    """A grant's reset count: a whole number in 0..MAX_RESET_CREDITS, else None."""
    number = finite_float(value)
    if number is None or not number.is_integer() or not 0 <= number <= MAX_RESET_CREDITS:
        return None
    return int(number)


def iso_epoch(value) -> float | None:
    """An ISO 8601 time as an epoch; one without a zone is UTC. None if unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def parse_limit_resets(cedar_ember, now: float) -> dict | None:
    """The ``limit_resets`` entry kept beside an account's usage windows.

    ``{"grants": [...], "eligible": bool}``: every grant whose ``ends_at``
    is ahead of ``now``, as ``{"id", "label", "left", "total", "ends_at"
    (ISO, UTC), "usable_now", "paused"}``, kept one by one so the menu can
    sum them up when it draws (``summarize_limit_resets``) and a grant that
    ends between reads drops out on its own. None when the answer is not an
    object or has no live grant (``no_grant``, ``surface``, every grant
    ended). A grant without a readable ``ends_at`` or ``resets_left`` is
    left out: a reset that cannot be dated is not promised. A missing
    ``resets_total`` counts what is left. Flags count only when they are
    ``true`` itself.
    """
    if not isinstance(cedar_ember, dict):
        return None
    raw = cedar_ember.get("grants")
    if not isinstance(raw, list):
        return None
    grants = []
    for grant in raw:
        if not isinstance(grant, dict):
            continue
        ends = iso_epoch(grant.get("ends_at"))
        left = _grant_count(grant.get("resets_left"))
        if ends is None or ends <= now or left is None:
            continue
        total = _grant_count(grant.get("resets_total"))
        grant_id, label = grant.get("id"), grant.get("label")
        grants.append({
            "id": grant_id if isinstance(grant_id, str) else None,
            "label": label if isinstance(label, str) else None,
            "left": left,
            "total": left if total is None else total,
            "ends_at": _utc_iso(ends),
            "usable_now": grant.get("usable_now") is True,
            "paused": grant.get("paused") is True,
        })
    if not grants:
        return None
    return {"grants": grants, "eligible": cedar_ember.get("eligible") is True}


def summarize_limit_resets(entry, now: float) -> dict | None:
    """What the menu shows for a stored ``limit_resets`` entry at ``now``.

    Summed over the stored grants whose ``ends_at`` is still ahead of
    ``now`` and that are not paused: ``left`` and ``total``, ``usable_now``
    (any of them), ``label`` (the first one's) and ``ends_at``, the soonest
    end among those that still have resets left (among all of them when
    none has: a used reset still shows until it ends). When every live grant
    is paused, those are summed instead and ``paused`` is true: a paused
    reset is not a used one. ``eligible`` is the answer's own flag. None
    when no stored grant is live and readable, and for an entry in any
    other shape (the summed one written before grants were kept).
    """
    if not isinstance(entry, dict):
        return None
    grants = entry.get("grants")
    if not isinstance(grants, list):
        return None
    live = []  # (grant, ends epoch, left, total)
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        ends = iso_epoch(grant.get("ends_at"))
        left = _grant_count(grant.get("left"))
        if ends is None or ends <= now or left is None:
            continue
        total = _grant_count(grant.get("total"))
        live.append((grant, ends, left, left if total is None else total))
    if not live:
        return None
    counted = [item for item in live if item[0].get("paused") is not True]
    paused = not counted
    if paused:
        counted = live
    dated = [item for item in counted if item[2] > 0] or counted
    label = counted[0][0].get("label")
    return {
        "left": sum(left for _grant, _ends, left, _total in counted),
        "total": sum(total for _grant, _ends, _left, total in counted),
        "ends_at": _utc_iso(min(ends for _grant, ends, _left, _total in dated)),
        "label": label if isinstance(label, str) else None,
        "usable_now": any(grant.get("usable_now") is True for grant, *_rest in counted),
        "eligible": entry.get("eligible") is True,
        "paused": paused,
    }


@dataclass(frozen=True)
class LimitResetsRead:
    """One limit-resets read made beside a usage read.

    ``cedar_ember`` is what ``request_limit_resets`` answered: the object,
    or None when the read failed and nothing was learned. The usage store
    parses it with its own clock and keeps the previous entry on a failure.
    """

    cedar_ember: dict | None


def _read_limit_resets(
    access_token: str, gate: Callable[[], bool]
) -> LimitResetsRead | None:
    """``request_limit_resets`` behind its gate, unable to fail the usage read
    it rides on. None when the gate refuses (or fails): no read was made."""
    try:
        allowed = gate() is True
    except Exception as e:
        _logger.debug("Limit resets reservation failed: %r", e)
        return None
    if not allowed:
        return None
    try:
        return LimitResetsRead(request_limit_resets(access_token))
    except Exception as e:  # the contract says never; a bug must not cost the usage
        _logger.debug("Limit resets read raised: %r", e)
        return LimitResetsRead(None)


def _classify_usage_error(e: Exception) -> tuple[str, float | None]:
    """Map a usage-fetch exception to ``(kind, retry_after_s)``.

    ``kind`` is a short stable token for logs and backoff decisions
    (``"http-429"``, ``"timeout"``, ``"network"``, ``"bad-response"``, or the
    exception type name as a fallback). ``retry_after_s`` is the parsed
    ``Retry-After`` header when the server sent one (seconds form only — the
    HTTP-date form is rare enough to ignore).
    """
    if isinstance(e, urllib.error.HTTPError):
        retry_after = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                retry_after = max(0.0, float(raw.strip()))
            except ValueError:
                pass
        return f"http-{e.code}", retry_after
    if isinstance(e, TimeoutError):  # socket.timeout is an alias since 3.10
        return "timeout", None
    if isinstance(e, urllib.error.URLError):
        if isinstance(e.reason, TimeoutError):
            return "timeout", None
        return "network", None
    if isinstance(e, json.JSONDecodeError):
        return "bad-response", None
    return type(e).__name__, None


def _log_usage_failure(
    context: str, e: Exception, kind: str, retry_after_s: float | None = None
) -> None:
    """One WARNING line with the cause so it lands in the default log file
    (issue #85 was undiagnosable with failures swallowed at DEBUG); the full
    exception repr stays at DEBUG. The line is what users paste into public
    issues, so ``context`` must not carry the email, and the server's
    Retry-After rides along when present (it answers the backoff-tuning
    question without a second ask)."""
    where = f" {context}" if context else ""
    cause = kind if retry_after_s is None else f"{kind}, retry-after {retry_after_s:.0f}s"
    if kind == "http-429":
        # Whether the budget counts per access token or per account depends
        # on the org's 429 regime (both measured; see poll_policy), so the
        # message stays scope-neutral. Under the account-scoped regime
        # re-authenticating does not clear a block, and two machines holding
        # different tokens for one account still compete for one budget —
        # cumulative polling across surfaces and machines can saturate it,
        # and backoff plus the adaptive cadence are the recovery.
        cause += " (usage-endpoint budget reached; backing off)"
    _logger.warning("Usage fetch failed%s: %s", where, cause)
    _logger.debug("Usage fetch failure detail%s: %r", where, e)



def build_usage_result(data: dict) -> dict | None:
    """Normalize raw usage API data into the structure used by the CLI."""
    _logger.debug("Usage API response: %s", json.dumps(data, indent=2))

    result = {}

    h5 = data.get("five_hour")
    if h5:
        h5_entry = {"pct": h5["utilization"]}
        if h5.get("resets_at"):
            h5_entry["resets_at"] = h5["resets_at"]
            h5_entry["countdown"], h5_entry["clock"] = format_reset(h5["resets_at"])
        result["five_hour"] = h5_entry

    d7 = data.get("seven_day")
    if d7:
        d7_entry = {"pct": d7["utilization"]}
        if d7.get("resets_at"):
            d7_entry["resets_at"] = d7["resets_at"]
            d7_entry["countdown"], d7_entry["clock"] = format_reset(d7["resets_at"])
        result["seven_day"] = d7_entry

    eu = data.get("extra_usage")
    if eu and eu.get("is_enabled"):
        # Claude Code returns nullable used_credits, monthly_limit, and utilization
        # (monthly_limit=None = unlimited). All three are needed to render the spend
        # line, so when any is null skip just the spend entry; five_hour/seven_day
        # go through unchanged.
        used_credits = eu.get("used_credits")
        monthly_limit = eu.get("monthly_limit")
        utilization = eu.get("utilization")
        if used_credits is not None and monthly_limit is not None and utilization is not None:
            try:
                spend_entry: dict = {
                    "used": float(used_credits) / 100,
                    "limit": float(monthly_limit) / 100,
                    "pct": float(utilization),
                    "currency": eu.get("currency", "USD"),
                }
                if eu.get("resets_at"):
                    spend_entry["resets_at"] = eu["resets_at"]
                    spend_entry["countdown"], spend_entry["clock"] = format_reset(eu["resets_at"])
                result["spend"] = spend_entry
            except (TypeError, ValueError) as e:
                _logger.debug("extra_usage parse failed: %r", e)

    # Per-model weekly limits live in the newer ``limits`` array as
    # ``weekly_scoped`` entries carrying a ``scope.model.display_name`` (e.g.
    # "Fable"). The legacy five_hour/seven_day keys above never expose these, so
    # surface each scoped window separately. Absent/older responses (no
    # ``limits``) simply yield no ``scoped`` key.
    limits = data.get("limits")
    if isinstance(limits, list):
        scoped: list[dict] = []
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            scope = lim.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            pct = lim.get("percent")
            if not name or not isinstance(pct, (int, float)):
                continue
            scoped_entry: dict = {"name": name, "pct": float(pct)}
            if lim.get("resets_at"):
                scoped_entry["resets_at"] = lim["resets_at"]
                scoped_entry["countdown"], scoped_entry["clock"] = format_reset(lim["resets_at"])
            scoped.append(scoped_entry)
        if scoped:
            result["scoped"] = scoped

    return result if result else None


def relevant_windows(
    usage: dict | None, models: Sequence[str] = ()
) -> list[tuple[str, float, str | None]]:
    """Every ``(label, pct, resets_at)`` window that gates this account.

    Always the 5-hour ("5h") and 7-day ("7d") windows. When ``models`` is
    non-empty, each named per-model weekly ``scoped`` window is included too
    (matched case-insensitively on display name, e.g. "Fable"; the sentinel
    ``all`` matches every scoped window the account reports). The single
    canonical window source for decisions, scheduling, and reset math — so a
    window that binds a decision can never be invisible to the scheduler.
    ``spend`` (pay-as-you-go extra-usage credits) is a separate axis and is
    deliberately excluded. ``resets_at`` is the ISO string as fetched, or
    ``None`` when the API sent none.
    """
    if not isinstance(usage, dict):
        return []
    windows: list[tuple[str, float, str | None]] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            windows.append((label, float(window["pct"]), window.get("resets_at")))
    if models:
        wanted = {m.lower() for m in models}
        match_all = "all" in wanted
        scoped = usage.get("scoped")
        if isinstance(scoped, list):
            for s in scoped:
                if (
                    isinstance(s, dict)
                    and isinstance(s.get("pct"), (int, float))
                    and isinstance(s.get("name"), str)
                    and (match_all or s["name"].lower() in wanted)
                ):
                    windows.append((s["name"], float(s["pct"]), s.get("resets_at")))
    return windows


def account_headroom(
    usage: dict | None, models: Sequence[str] = ()
) -> float | None:
    """Remaining percentage before this account hits a rate-limit window.

    Considers the 5-hour and 7-day utilization windows — the two that always
    gate requests. When ``models`` is non-empty, each named per-model weekly
    ``scoped`` window (see :func:`relevant_windows`) is folded in too: a model
    maxed at 100% blocks that model's work even with 5h/7d headroom, so for
    someone pinned to that model it binds just as hard. Returns the headroom
    of the *binding* window (``100 - max(pct)``), so ``<= 0`` means the
    account is at or over a limit. Returns ``None`` when usage is unavailable
    or carries no window data, which callers treat as "unknown" (never
    auto-skipped).
    """
    pcts = [pct for _, pct, _ in relevant_windows(usage, models)]
    if not pcts:
        return None
    return 100.0 - max(pcts)


@dataclass(frozen=True)
class UsageOutcome:
    """Result of a usage-API fetch attempt.

    ``usage`` is the normalized usage dict on success (it can also be ``None``
    on a successful round trip whose response carried no window data).
    ``error`` is ``None`` on success, else a ``_classify_usage_error`` kind
    (plus ``"no-access-token"`` / ``"refresh-failed"`` for pre-request
    failures). ``retry_after_s`` carries the server's Retry-After when sent.
    """

    usage: dict | None
    error: str | None = None
    retry_after_s: float | None = None
    # Fingerprint of the credential whose rt was POSTed when error is a
    # permanent auth kind — lets the store bind the strike to that
    # generation (see usage_store.FetchRecord.struck_fp).
    struck_fp: str | None = None
    # The limit-resets read made right after a successful usage read, when
    # one was asked for; None when none was made.
    limit_resets: LimitResetsRead | None = None


def fetch_usage(access_token: str) -> dict | None:
    """Fetch 5-hour and 7-day utilization from the Anthropic usage API."""
    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except Exception as e:
        kind, _ = _classify_usage_error(e)
        _log_usage_failure("", e, kind)
        return None


# Refresh failures that will not resolve by retrying THIS pass, so the caller
# must not fall through to the usage endpoint with the known-expired token.
# `consume-busy` belongs here for a reason the other two make obvious only in
# hindsight: the retry re-enters the same gate, finds it still held, and the
# distinct kind arrives as generic "refresh-failed" — hiding it, and spending a
# guaranteed 401 per pass to learn nothing.
_DETERMINISTIC_REFRESH_ERRORS = (
    "store-unmirrored", "invalid_client", "consume-busy", "stash-unreadable",
)


def try_fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
    refresh_via: Callable[[str, str, str], RefreshOutcome] | None = None,
    limit_resets: Callable[[], bool] | None = None,
) -> UsageOutcome:
    """Fetch usage for an account, refreshing expired tokens for inactive accounts only.

    Active accounts are never refreshed — Claude Code owns those credentials.
    ``refresh_via(account_num, email, snapshot)`` supersedes the direct POST
    when given: the switcher passes its consume gate, which re-reads the
    freshest copy under the slot lock, persists via fingerprint CAS, and
    never consumes a superseded snapshot. ``persist_credentials`` is then
    unused for the refresh (the gate persists internally).

    ``limit_resets`` asks for the rare limit-resets read as well. It is the
    read's gate (``UsageStore.reserve_limit_resets``), asked right before
    the request and only after a usage read that succeeded with window
    data; the read is sent with the token that read used only when the gate
    answers True, and is carried in ``UsageOutcome.limit_resets``. Neither
    the gate nor the read can fail the usage read.
    """
    context = f"for account {account_num}"  # no email: paste-safe for public issues
    oauth = extract_oauth_data(credentials)
    access_token = oauth.get("accessToken") if oauth else None
    if not access_token:
        return UsageOutcome(None, error="no-access-token")

    working_credentials = credentials

    if (
        not is_active
        and oauth.get("refreshToken")
        and is_oauth_token_expired(oauth.get("expiresAt"))
    ):
        if refresh_via is not None:
            refresh = refresh_via(account_num, email, working_credentials)
        else:
            refresh = try_refresh_oauth_credentials(working_credentials)
        if refresh.credentials:
            working_credentials = refresh.credentials
            if refresh_via is None:
                _persist(persist_credentials, account_num, email, working_credentials)
            oauth = extract_oauth_data(working_credentials) or oauth
            access_token = oauth.get("accessToken") or access_token
        elif refresh.error in ("invalid_grant", "no_refresh_token"):
            # The refresh-token lineage is server-rejected (or structurally
            # absent) — permanently dead. Don't hit the usage endpoint with
            # a token we know is expired (that just adds a 401/429 to a lost
            # cause): report the permanent failure distinctly so the store
            # can quarantine the account. The strike binds to the bytes the
            # gate actually POSTed (it may have substituted a fresher
            # re-read for our snapshot) — fall back to the snapshot's
            # fingerprint only for the direct-POST path.
            return UsageOutcome(
                None, error=refresh.error,
                struck_fp=(
                    refresh.consumed_fp
                    or credential_fingerprint(working_credentials)
                ),
            )
        elif refresh.error in _DETERMINISTIC_REFRESH_ERRORS:
            # Deterministic refusals (M4 parity guard; a systemic client_id
            # rejection; another process holding the consume gate): hitting the
            # usage endpoint with the known-expired token would 401 every pass.
            # Surface the distinct kind instead — ERROR_NOTES renders the
            # remedy for each.
            return UsageOutcome(None, error=refresh.error)
        # A transient refresh failure falls through to try the (expired) token;
        # the 401 path below retries the refresh.

    def succeeded(data: dict, token: str) -> UsageOutcome:
        usage = build_usage_result(data)
        if limit_resets is None or usage is None:
            return UsageOutcome(usage)
        return UsageOutcome(usage, limit_resets=_read_limit_resets(token, limit_resets))

    try:
        data = request_usage_data(access_token)
        return succeeded(data, access_token)
    except urllib.error.HTTPError as e:
        kind, retry_after = _classify_usage_error(e)
        if (
            e.code != 401
            or is_active
            or not oauth
            or not oauth.get("refreshToken")
        ):
            _log_usage_failure(context, e, kind, retry_after)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)

        # Retry once after refreshing on 401 (inactive accounts only). A
        # server-rejected grant (invalid_grant) means this refresh-token lineage
        # is permanently dead — surface it distinctly (not the generic
        # "refresh-failed") so the store can quarantine instead of retrying a
        # dead token forever.
        if refresh_via is not None:
            refresh = refresh_via(account_num, email, working_credentials)
        else:
            refresh = try_refresh_oauth_credentials(working_credentials)
        if not refresh.credentials:
            _log_usage_failure(context, e, kind)
            dead = refresh.error in ("invalid_grant", "no_refresh_token")
            # Deterministic kinds keep their identity here too — collapsing
            # them to "refresh-failed" would hide the ERROR_NOTES remedy
            # exactly on the 401 path (a not-yet-locally-expired token the
            # server already rotated past).
            distinct = dead or refresh.error in _DETERMINISTIC_REFRESH_ERRORS
            return UsageOutcome(
                None,
                error=refresh.error if distinct else "refresh-failed",
                struck_fp=(
                    (refresh.consumed_fp
                     or credential_fingerprint(working_credentials))
                    if dead else None
                ),
            )

        working_credentials = refresh.credentials
        if refresh_via is None:
            _persist(persist_credentials, account_num, email, working_credentials)
        refreshed_oauth = extract_oauth_data(working_credentials)
        new_token = refreshed_oauth.get("accessToken") if refreshed_oauth else None
        if not new_token:
            return UsageOutcome(None, error="refresh-failed")

        try:
            data = request_usage_data(new_token)
            return succeeded(data, new_token)
        except Exception as retry_error:
            kind, retry_after = _classify_usage_error(retry_error)
            _log_usage_failure(context + " after refresh", retry_error, kind, retry_after)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)
    except Exception as e:
        kind, retry_after = _classify_usage_error(e)
        _log_usage_failure(context, e, kind, retry_after)
        return UsageOutcome(None, error=kind, retry_after_s=retry_after)


def fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
) -> dict | None:
    """Usage dict or None (see try_fetch_usage_for_account for the cause)."""
    return try_fetch_usage_for_account(
        account_num, email, credentials, is_active, persist_credentials
    ).usage


def _persist(
    callback: Callable[[str, str, str], None] | None,
    account_num: str,
    email: str,
    credentials: str,
) -> None:
    """Call the persist callback, warning loudly on failure."""
    if not callback:
        return
    try:
        callback(account_num, email, credentials)
    except Exception as e:
        _logger.warning(
            "Refreshed OAuth token for account %s (%s) but failed to persist it: %r. "
            "The refresh token on disk may now be stale; if the next refresh fails "
            "with invalid_grant, re-run `cswap --add-account` after logging in.",
            account_num,
            email,
            e,
        )
        print_warning(
            f"Warning: failed to save refreshed token for account {account_num} ({email}). "
            f"If the next refresh fails, re-run `cswap --add-account` after logging in."
        )
