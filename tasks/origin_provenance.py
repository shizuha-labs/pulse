"""Structured Shizuha Origin evidence attached to Pulse items.

``affects_versions`` models product/release versions.  CI failures and
regressions instead need an immutable observation of the exact repository,
commit, ref and workflow run where the symptom was seen.  Keep that evidence in
``Item.metadata.origin_observations`` so the contract is deployable without a
schema migration and remains readable by older Pulse builds.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlencode, urlsplit


# Deployment-owned trust anchors. Never derive them from a caller URL.
# orig18-v3 (PLS-1409 reconciliation, 6bb84ca6): the provenance trust anchor is
# the deployment-provided ORIGIN_PUBLIC_URL, validated fail-loud at import
# (https-only, no credentials, no path/query) — NOT a hardcoded first-party
# host. A hardcoded first-party pin here would make third-party deployments
# treat origin.shizuha.com as a trust anchor, violating the configify
# fail-closed contract (test_first_party_hosts_fail_closed_under_third_party_topology).
ORIGIN_PUBLIC_URL = os.environ.get('ORIGIN_PUBLIC_URL', 'https://forgejo.example.invalid').rstrip('/')
_origin_public = urlsplit(ORIGIN_PUBLIC_URL)
if (_origin_public.scheme != 'https' or not _origin_public.hostname
        or _origin_public.username or _origin_public.password
        or _origin_public.path or _origin_public.query or _origin_public.fragment):
    raise ValueError('ORIGIN_PUBLIC_URL must be an HTTPS origin without credentials or a path')
_PUBLIC_ORIGIN_HOSTS = {_origin_public.hostname} | {
    host.strip().lower() for host in os.environ.get('ORIGIN_PUBLIC_HOST_ALIASES', '').split(',') if host.strip()
}
_INTERNAL_ORIGIN_HOSTS = {
    host.strip().lower() for host in os.environ.get('ORIGIN_INTERNAL_HOST_ALIASES', '').split(',') if host.strip()
}
_ORIGIN_HOST_PATTERN = '(?:' + '|'.join(re.escape(host) for host in sorted(
    _PUBLIC_ORIGIN_HOSTS | _INTERNAL_ORIGIN_HOSTS
)) + ')'
ORIGIN_RUN_URL_RE = re.compile(
    # Absolute URLs must name Origin. Relative URLs must begin at a genuine
    # token boundary; without this guard, the ``owner/repo/...`` path inside
    # ``https://github.com/owner/repo/actions/runs/1`` was misclassified as a
    # relative Origin URL.
    r"(?P<url>"
    rf"(?:https?://{_ORIGIN_HOST_PATTERN}/|(?<![A-Za-z0-9_./:-])/?)"
    r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/actions/runs/(?P<run_number>\d+)"
    r"(?:/jobs/(?P<job_id>\d+))?)"
)
_ORIGIN_PR_URL_RE = re.compile(
    rf"^{re.escape(ORIGIN_PUBLIC_URL)}/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/pulls/"
    r"(?P<number>[1-9][0-9]*)/?$"
)
# PLS-834 / PLAT-1296 v9: historical writers sometimes stored the GitHub-shaped
# singular ``/pull/N`` path on the Origin host. That representation is NEVER a
# trusted ingress or API-construction key (``parse_origin_pr_reference`` stays
# plural-only). It exists solely so already-persisted legacy rows can be
# identity-matched and provider-confirmed for supersession/refresh.
_ORIGIN_PR_LEGACY_SINGULAR_URL_RE = re.compile(
    rf"^{re.escape(ORIGIN_PUBLIC_URL)}/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/pull/"
    r"(?P<number>[1-9][0-9]*)/?$"
)
_ORIGIN_PR_SHORT_REFERENCE_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)#"
    r"(?P<number>[1-9][0-9]*)$"
)
# PLS-834 residual (live instance PLAT-7725): the Forgejo instance has also
# been reachable under the ``git.shizuha.com`` host, and historical writers
# persisted linked-PR rows under that host (both plural and singular paths).
# Like the Origin singular ``/pull/N`` rows, these are NEVER trusted ingress
# or API-construction keys — identity/repair targets only.
_FORGEJO_LEGACY_HOSTS = {
    host.strip().lower() for host in os.environ.get('ORIGIN_LEGACY_HOST_ALIASES', '').split(',') if host.strip()
}
_LEGACY_HOST_PATTERN = '|'.join(re.escape(host) for host in sorted(_FORGEJO_LEGACY_HOSTS)) or r'(?!)'
_FORGEJO_LEGACY_HOST_PR_URL_RE = re.compile(
    rf"^https://(?:{_LEGACY_HOST_PATTERN})/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/pulls?/"
    r"(?P<number>[1-9][0-9]*)/?$"
)
_GITHUB_PR_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/pull/"
    r"(?P<number>[1-9][0-9]*)/?$",
    re.IGNORECASE,
)
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REPOSITORY_LINE_RE = re.compile(
    r"(?:^|\n)\s*-?\s*Repository:\s*`(?P<repo>[^`\s]+/[^`\s]+)`",
    re.IGNORECASE,
)
_REF_SHA_LINE_RES = (
    re.compile(
        r"(?:^|\n)\s*-?\s*Ref/SHA:\s*`(?P<ref>[^`]+)`\s*/\s*`(?P<sha>[0-9a-f]{7,64})`",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\n)\s*-?\s*Last scan:\s*`(?P<ref>[^`]+)`\s*/\s*`(?P<sha>[0-9a-f]{7,64})`",
        re.IGNORECASE,
    ),
)
_ORIGIN_PROVENANCE_SOURCES = {
    "forgejo",
    "origin-ci",
    "origin-forgejo",
    "security-ci",
}
_ALLOWED_FIELDS = {
    "provider",
    "repo",
    "ref",
    "commit_sha",
    "workflow",
    "check",
    "run_id",
    "run_number",
    "job_id",
    "status",
    "run_url",
    "logs_url",
    "image",
    "image_digest",
    "observed_at",
}


def canonical_origin_run_url(value: str) -> str:
    """Return a canonical public Origin run/job URL, or ``value`` unchanged."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return f"{ORIGIN_PUBLIC_URL}{raw}"
    if not raw.startswith(("http://", "https://")):
        return f"{ORIGIN_PUBLIC_URL}/{raw.lstrip('/')}"
    parsed = urlsplit(raw)
    if parsed.hostname in _INTERNAL_ORIGIN_HOSTS:
        # CI writers run inside the cluster and historically recorded their
        # service URL.  Never leak an unreachable service hostname to agents or
        # browsers: retain the exact run path but publish its public Origin URL.
        return f"{ORIGIN_PUBLIC_URL}{parsed.path}"
    return raw


def parse_origin_run_url(value: str) -> dict[str, str]:
    """Parse an Origin Actions run URL into stable repository/run fields."""
    raw = canonical_origin_run_url(value)
    if not raw:
        return {}
    parsed = urlsplit(raw)
    if parsed.hostname not in _PUBLIC_ORIGIN_HOSTS:
        return {}
    match = ORIGIN_RUN_URL_RE.search(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    if not match:
        return {}
    result = {
        "provider": "origin-forgejo",
        "repo": f"{match.group('owner')}/{match.group('repo')}",
        "run_number": match.group("run_number"),
        "run_url": raw,
    }
    if match.group("job_id"):
        result["job_id"] = match.group("job_id")
    return result


def parse_origin_pr_url(value: str) -> tuple[str, str, int] | None:
    """Return canonical Origin PR coordinates, or ``None``.

    This is the shared trust boundary for token-bearing linked-PR consumers.
    Only the public HTTPS Origin host and exact ``owner/repo/pulls/number`` path
    are accepted; arbitrary hosts, HTTP downgrade, userinfo/ports,
    query/fragment smuggling, encoded separators, and malformed paths fail
    closed before any authenticated request can be built.
    """
    match = _ORIGIN_PR_URL_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_forgejo_legacy_host_pr_url(value: str) -> tuple[str, str, int] | None:
    """Return coordinates for a legacy ``git.shizuha.com`` PR URL only.

    PLS-834 residual: the same Forgejo instance served PR URLs under the
    ``git.shizuha.com`` host, and historical writers persisted rows with that
    host verbatim. This is an identity/repair helper for rows that already
    exist in task metadata — it must never be used as the sole trust boundary
    for building authenticated requests from untrusted ingress, and it must
    never make ``git.shizuha.com`` URLs acceptable as new ``pulse_link_pr``
    input (ingress fails closed on them with the canonical-form error).
    """
    match = _FORGEJO_LEGACY_HOST_PR_URL_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_origin_pr_legacy_singular_url(value: str) -> tuple[str, str, int] | None:
    """Return coordinates for a legacy Origin singular ``/pull/N`` URL only.

    PLS-834 / PLAT-1296 v9: this is an identity/repair helper for rows that
    already exist in task metadata. It must never be used as the sole trust
    boundary for building authenticated requests from untrusted ingress, and it
    must never make singular URLs acceptable as new ``pulse_link_pr`` input.
    Host, scheme, and path constraints match the canonical plural parser.
    """
    match = _ORIGIN_PR_LEGACY_SINGULAR_URL_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


_ORIGIN_PR_PATH_COORDS_RE = re.compile(
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"pulls?/"
    r"(?P<number>[1-9][0-9]*)",
    re.IGNORECASE,
)


def origin_pr_link_ingress_error(value: str) -> str | None:
    """Fail closed on non-canonical Origin PR URL shapes before persistence.

    PLS-834 / Standard PLAT-1296 rule 13: new linked-PR ingress may accept only
    the exact public HTTPS plural form (or a non-Origin URL / short key handled
    elsewhere). Singular ``/pull/N``, query/fragment smuggling, userinfo, port,
    HTTP downgrade, internal service hosts, and other Origin-host lookalikes
    must never become new metadata rows — even when the caller also supplies
    ``repo`` / ``pr_number``.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    # Canonical plural URL or strict short key — allowed.
    if parse_origin_pr_reference(raw) is not None:
        return None
    # ORIG-18: github.com is a read-only mirror. Internal development, and
    # the Origin merge of an open-source contribution, never use a GitHub
    # pull request as Pulse evidence. Reject it before any fetch or row write
    # so a 404 cannot become an open obligation.
    if _GITHUB_PR_URL_RE.fullmatch(raw):
        return (
            "Error: GitHub is a read-only mirror and is not a Pulse code "
            "pull request. Link the Origin pull request that carries this "
            f"change ({ORIGIN_PUBLIC_URL}/<owner>/<repo>/pulls/<n>). "
            "A github.com pull request is never merge or assignment evidence, "
            "including an open-source contribution — that change is merged "
            "on Origin first."
        )

    looks_origin = False
    path = ""
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if host in _PUBLIC_ORIGIN_HOSTS or host in _INTERNAL_ORIGIN_HOSTS:
            looks_origin = True
            path = parsed.path or ""
        elif host in _FORGEJO_LEGACY_HOSTS:
            # PLS-834 residual: git.shizuha.com is the same Forgejo instance
            # under a legacy host name. New links under it must fail closed
            # with the canonical-form error instead of slipping through as a
            # "non-Origin URL" and persisting without provenance.
            looks_origin = True
            path = parsed.path or ""
        elif any(host in raw.lower() for host in _PUBLIC_ORIGIN_HOSTS | _INTERNAL_ORIGIN_HOSTS) and re.search(
            r"/pulls?/", raw, re.IGNORECASE
        ):
            looks_origin = True
            path = parsed.path or raw
    except Exception:
        if any(host in raw.lower() for host in _PUBLIC_ORIGIN_HOSTS | _INTERNAL_ORIGIN_HOSTS) and re.search(
            r"/pulls?/", raw, re.IGNORECASE
        ):
            looks_origin = True
            path = raw

    if not looks_origin:
        # PLS-1226: any URL-shaped value that is not a trusted provider shape
        # is hostile — fail closed BEFORE persistence. The historical verbatim
        # persist path let arbitrary-host URLs (e.g.
        # https://evil.example.com/shizuha-labs/deploy/pulls/999888) become
        # open no-provenance rows: a phishing surface in task sidebars and a
        # self-recreating PLAT-1296/E re-entry wedge (no head_sha is ever
        # fetchable for them). Post-ORIG-18 the only trusted providers are
        # canonical Origin and canonical GitHub; short keys (owner/repo#N) and
        # the no-pr sentinel are not URLs and are handled elsewhere.
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', raw):
            host = ""
            try:
                host = (urlsplit(raw).hostname or "").lower()
            except Exception:
                pass
            return (
                "Error: untrusted PR URL host "
                f"'{host or raw[:80]}'. Only a canonical Origin pull request "
                f"({ORIGIN_PUBLIC_URL}/<owner>/<repo>/pulls/<n>) is "
                "accepted. GitHub is a read-only mirror and is not a Pulse "
                "code pull request."
            )
        return None

    coords = _ORIGIN_PR_PATH_COORDS_RE.search(path or raw)
    if coords:
        canonical = canonical_origin_pr_url(
            coords.group("owner"),
            coords.group("repo"),
            int(coords.group("number")),
        )
        return (
            "Error: malformed Origin PR URL (non-canonical path, query, "
            "fragment, host, or scheme). "
            f"Link the canonical form instead: {canonical}"
        )
    return (
        "Error: malformed Origin PR URL. "
        "Use the canonical form: "
        f"{ORIGIN_PUBLIC_URL}/<owner>/<repo>/pulls/<n>"
    )


def canonical_origin_pr_url(owner: str, repo: str, number: int) -> str:
    """Return the canonical public Origin PR URL for exact coordinates."""
    return f"{ORIGIN_PUBLIC_URL}/{owner}/{repo}/pulls/{int(number)}"


def parse_origin_pr_reference(value: str) -> tuple[str, str, int] | None:
    """Return coordinates for a trusted Origin PR URL or strict short key.

    Pulse persists linked Origin PRs in either canonical public-URL form or its
    first-class ``owner/repo#N`` rendering.  This shared boundary gives both
    representations identical coordinates without treating arbitrary relative
    paths, host-bearing values, or malformed lookalikes as Origin references.
    Consumers still construct authenticated requests from their configured
    Origin base; the short key never supplies a network destination.
    """
    raw = str(value or "").strip()
    canonical = parse_origin_pr_url(raw)
    if canonical:
        return canonical
    match = _ORIGIN_PR_SHORT_REFERENCE_RE.fullmatch(raw)
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_linked_pr_identity(value: str) -> tuple[str, str, int] | None:
    """Return provider-neutral coordinates for a canonical linked PR.

    Origin imported pre-cutover GitHub PRs without rewriting historical bounce
    map keys.  The two URLs therefore name one logical obligation when their
    owner, repository and number match.  This helper is deliberately limited to
    the trusted Origin representations (canonical plural URL, strict short key,
    and already-persisted legacy singular Origin ``/pull/N`` rows, plus
    already-persisted legacy ``git.shizuha.com``-host rows) plus
    canonical GitHub PR URLs; it is an identity matcher only and never supplies
    an authenticated request target.
    """
    origin = parse_origin_pr_reference(value)
    if origin:
        return origin[0].lower(), origin[1].lower(), origin[2]
    legacy = parse_origin_pr_legacy_singular_url(value)
    if legacy:
        return legacy[0].lower(), legacy[1].lower(), legacy[2]
    forgejo_legacy = parse_forgejo_legacy_host_pr_url(value)
    if forgejo_legacy:
        return (
            forgejo_legacy[0].lower(),
            forgejo_legacy[1].lower(),
            forgejo_legacy[2],
        )
    match = _GITHUB_PR_URL_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return (
        match.group("owner").lower(),
        match.group("repo").lower(),
        int(match.group("number")),
    )


# Optional deployment-owned retirement map. A renamed first-party repository
# must not become a permanent global policy for every external installation.
_frozen_repos = json.loads(os.environ.get('PULSE_FROZEN_DEVELOPMENT_REPOS', '{}'))
if not isinstance(_frozen_repos, dict) or any(
    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*', key)
    or not isinstance(reason, str) or not reason.strip()
    for key, reason in _frozen_repos.items()
):
    raise ValueError('PULSE_FROZEN_DEVELOPMENT_REPOS must map owner/repository to a reason')
_FROZEN_DEVELOPMENT_REPOS = {
    tuple(key.lower().split('/')): reason for key, reason in _frozen_repos.items()
}


def development_repo_block_reason(owner: str | None, repo: str | None) -> str | None:
    """Return a reject message if (owner, repo) is a frozen development line."""
    if not owner or not repo:
        return None
    return _FROZEN_DEVELOPMENT_REPOS.get(
        (str(owner).strip().lower(), str(repo).strip().lower())
    )


def linked_pr_development_block_reason(
    url: str = "",
    repo: str | None = None,
) -> str | None:
    """Reject a linked-PR URL or owner/repo that names a frozen development line."""
    ident = parse_linked_pr_identity(url)
    if ident:
        return development_repo_block_reason(ident[0], ident[1])
    if repo and "/" in str(repo):
        owner, name = str(repo).strip().split("/", 1)
        return development_repo_block_reason(owner, name)
    return None


def find_origin_run_url(text: str) -> str:
    """Extract the first absolute or relative Origin Actions run URL in text."""
    match = ORIGIN_RUN_URL_RE.search(str(text or ""))
    return canonical_origin_run_url(match.group("url")) if match else ""


def infer_origin_observation(
    *,
    source_url: str = "",
    description: str = "",
    source: str = "",
) -> dict[str, str]:
    """Recover structured Origin evidence from legacy CI writer prose.

    Security CI has historically included the exact repository, ref, SHA and
    Origin run URL in its description while omitting a machine-readable field.
    Inferring that narrow, labelled format at the Pulse boundary makes old
    vendored writers immediately safe and lets existing tasks become precise
    without a data migration.  Free-form tasks are never guessed: inference
    requires either a valid Origin run URL or a known Origin writer source.
    """
    text = str(description or "")
    run_url = find_origin_run_url(source_url) or find_origin_run_url(text)
    source_name = str(source or "").strip().lower()
    if not run_url and source_name not in _ORIGIN_PROVENANCE_SOURCES:
        return {}

    result = parse_origin_run_url(run_url) if run_url else {
        "provider": "origin-forgejo",
    }
    repository = _REPOSITORY_LINE_RE.search(text)
    if repository and not result.get("repo"):
        result["repo"] = repository.group("repo")
    for pattern in _REF_SHA_LINE_RES:
        match = pattern.search(text)
        if match:
            result["ref"] = match.group("ref").strip()
            result["commit_sha"] = match.group("sha").lower()
            break
    if source_name:
        result["check"] = source_name
    try:
        return normalize_origin_observation(result)
    except ValueError:
        # A malformed legacy description remains readable and cannot smuggle
        # unvalidated data into the first-class provenance contract.
        return {}


def origin_observations_with_inference(
    metadata: Any,
    *,
    source_url: str = "",
    description: str = "",
    source: str = "",
    payload: Any = None,
    labels: Any = None,
) -> list[dict[str, str]]:
    """Return explicit observations plus safe legacy-prose inference."""
    raw_metadata = metadata if isinstance(metadata, dict) else {}
    try:
        explicit = normalize_origin_observations(raw_metadata.get("origin_observations"))
    except ValueError:
        explicit = []
    inferred = infer_origin_observation(
        source_url=source_url,
        description=description,
        source=source,
    )
    if source == "origin-ci" and inferred:
        inferred = _origin_ci_payload_observation(inferred, payload, labels)
    return normalize_origin_observations([*explicit, *([inferred] if inferred else [])])


def origin_ci_logs_url(observation: dict) -> str:
    """Build the existing read-only log route from repository-scoped identity.

    Forgejo's API run ID is global; this service deliberately takes the UI's
    per-repository run number. Never substitute one for a missing other.
    """
    repo = str(observation.get("repo") or "")
    number = str(observation.get("run_number") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", repo):
        return ""
    if not re.fullmatch(r"[1-9][0-9]*", number):
        return ""
    if observation.get("run_url"):
        parsed = parse_origin_run_url(observation["run_url"])
        if parsed.get("repo") != repo or parsed.get("run_number") != number:
            return ""
    logs_url = os.environ.get('ORIGIN_CI_LOGS_URL', '').strip()
    parsed_logs = urlsplit(logs_url)
    if (parsed_logs.scheme not in {'http', 'https'} or not parsed_logs.hostname
            or parsed_logs.username or parsed_logs.password or parsed_logs.query or parsed_logs.fragment):
        return ''
    return logs_url + '?' + urlencode({
        "repo": repo, "run": number,
    })


def _origin_ci_payload_observation(inferred: dict, payload: Any, labels: Any) -> dict:
    """Retain the notifier's typed receipt without parsing its display prose.

    The URL binds repository/run number; conflicting labels or malformed typed
    identity cannot attach a different run's global ID/SHA to that URL.
    """
    if not isinstance(payload, dict) or not isinstance(labels, dict):
        return inferred
    if labels.get("repository") not in (None, "", inferred.get("repo")):
        return inferred
    values = {
        "run_id": payload.get("run_id"),
        "commit_sha": payload.get("commit_sha"),
        "status": payload.get("status"),
        "workflow": labels.get("workflow"),
        "ref": labels.get("ref"),
    }
    if values["run_id"] not in (None, "") and not re.fullmatch(r"[1-9][0-9]*", str(values["run_id"])):
        return inferred
    if values["commit_sha"] not in (None, "") and not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(values["commit_sha"])):
        return inferred
    if (payload.get("run_number") not in (None, "")
            and str(payload["run_number"]) != inferred.get("run_number")):
        return inferred
    values = {key: value for key, value in values.items() if value not in (None, "")}
    return normalize_origin_observation({**inferred, **values})


def normalize_origin_observation(value: Any) -> dict[str, str]:
    """Validate and canonicalize one observation for safe API/tool rendering."""
    if not isinstance(value, dict):
        raise ValueError("each origin observation must be a JSON object")

    raw = {str(k): v for k, v in value.items() if k in _ALLOWED_FIELDS and v not in (None, "")}
    run_url = canonical_origin_run_url(raw.get("run_url", ""))
    parsed = parse_origin_run_url(run_url)
    if run_url and not parsed:
        raise ValueError("run_url must be a Shizuha Origin Actions run/job URL")
    result: dict[str, str] = {**parsed}

    for key, val in raw.items():
        text = str(val).strip()
        if text:
            result[key] = text

    result["provider"] = (result.get("provider") or "origin-forgejo").lower()
    if result["provider"] != "origin-forgejo":
        raise ValueError("provider must be origin-forgejo")
    if run_url:
        result["run_url"] = run_url

    repo = (result.get("repo") or "").strip(" /")
    if repo and (repo.count("/") != 1 or any(part in {"", ".", ".."} for part in repo.split("/"))):
        raise ValueError("repo must be owner/name")
    if repo:
        result["repo"] = repo

    commit_sha = (result.get("commit_sha") or "").lower()
    if commit_sha and not _COMMIT_RE.fullmatch(commit_sha):
        raise ValueError("commit_sha must be a 7-64 character hexadecimal revision")
    if commit_sha:
        result["commit_sha"] = commit_sha

    if not any(result.get(k) for k in ("repo", "commit_sha", "run_url", "image", "image_digest")):
        raise ValueError("origin observation needs repo, commit_sha, run_url, image, or image_digest")
    return {key: result[key] for key in _ALLOWED_FIELDS if result.get(key)}


def normalize_origin_observations(value: Any) -> list[dict[str, str]]:
    """Normalize a list (or one object) and de-duplicate retry-equivalent rows.

    When one writer knows only the run URL and a later writer enriches that same
    run with the full SHA/workflow, retain one row containing the union rather
    than two apparently different observations.
    """
    if value in (None, ""):
        return []
    rows = value if isinstance(value, list) else [value]
    normalized: list[dict[str, str]] = []
    seen: dict[tuple[str, ...], int] = {}
    for row in rows:
        item = normalize_origin_observation(row)
        # A global ID and an unrelated repository number can have the same
        # integer value. An enriched row supplies an explicit mapping between
        # namespaces; either identity can then join a compatible partial row.
        identities = [
            (
                kind,
                item.get("provider", ""),
                item.get("repo", ""),
                item[kind],
                item.get("job_id", ""),
            )
            for kind in ("run_number", "run_id") if item.get(kind)
        ]
        if not identities:
            identities = [(
                "revision",
                item.get("provider", ""),
                item.get("repo", ""),
                item.get("commit_sha", ""),
                item.get("image_digest") or item.get("image", ""),
            )]
        matches = {seen[identity] for identity in identities if identity in seen}
        if len(matches) == 1:
            index = next(iter(matches))
            immutable = ("run_id", "run_number", "commit_sha", "workflow", "ref")
            if any(item.get(key) and normalized[index].get(key)
                   and item[key] != normalized[index][key] for key in immutable):
                # Retain contradictory receipts separately. Filling missing
                # fields across them would invent an apparently complete run.
                normalized.append(item)
                continue
            # The first row retains mutable status/timing; a compatible later
            # receipt can fill missing identity without changing its meaning.
            normalized[index] = {**item, **normalized[index]}
            for identity in identities:
                seen.setdefault(identity, index)
            continue
        for identity in identities:
            seen.setdefault(identity, len(normalized))
        normalized.append(item)
    return normalized


def merge_origin_observations(existing: Any, additions: Any) -> list[dict[str, str]]:
    """Append immutable observations, newest first, without losing enrichment."""
    old_rows = existing if isinstance(existing, list) else ([] if existing in (None, "") else [existing])
    new_rows = additions if isinstance(additions, list) else ([] if additions in (None, "") else [additions])
    return normalize_origin_observations([*new_rows, *old_rows])
