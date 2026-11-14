from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from snapshot_pipeline.io import atomic_write_json, load_json

from .contract import IntakeContractError, build_envelope, write_jsonl
from .source_text import source_lines


PROFILE_ENDPOINT = "/api/v1/wechat_mp/v2/fetch_account_profile"
ARTICLES_ENDPOINT = "/api/v1/wechat_mp/v2/fetch_account_articles"
DETAIL_ENDPOINT = "/api/v1/wechat_mp/v2/fetch_article_detail_h5"
FALLBACK_DETAIL_ENDPOINT = "/api/v1/wechat_mp/v2/fetch_article_detail"
_ENDPOINTS = {PROFILE_ENDPOINT, ARTICLES_ENDPOINT, DETAIL_ENDPOINT, FALLBACK_DETAIL_ENDPOINT}
_API_HOSTS = {"api.tikhub.io", "api.tikhub.dev"}
_USERNAME = re.compile(r"^gh_[A-Za-z0-9_]{3,61}$")
_WHITESPACE = re.compile(r"\s+")


class BackfillError(RuntimeError):
    pass


class TikHubHTTPError(BackfillError):
    def __init__(self, status: int) -> None:
        self.http_status = status
        super().__init__(f"TikHub returned HTTP {status}")


class TikHubTransportError(BackfillError):
    status = "network"


class TikHubBudgetExhausted(BackfillError):
    pass


def canonical_article_url(value: str) -> str:
    """Keep the immutable document/signature; never forward discovery sessions."""
    parsed = urlparse(html.unescape(value.strip()))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "mp.weixin.qq.com" or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise BackfillError("article navigation URL is invalid")
    if parsed.path.rstrip('/') == '/s':
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        values = {}
        for key, item in pairs:
            if key in {'__biz', 'mid', 'idx', 'sn'}:
                if key in values and values[key] != item:
                    raise BackfillError("article navigation identity is ambiguous")
                values[key] = item
        if not all(values.get(key) for key in ('__biz', 'mid', 'idx')) or not values['mid'].isdigit() or not values['idx'].isdigit():
            raise BackfillError("article navigation identity is unavailable")
        return urlunparse(('https', 'mp.weixin.qq.com', '/s', '', urlencode([(key, values[key]) for key in ('__biz', 'mid', 'idx', 'sn') if key in values]), ''))
    if not re.fullmatch(r'/s/[A-Za-z0-9_-]+', parsed.path):
        raise BackfillError("article navigation URL is invalid")
    return urlunparse(('https', 'mp.weixin.qq.com', parsed.path, '', '', ''))


def recoverable_detail_error(error: BaseException) -> bool:
    return isinstance(error, TikHubTransportError) or isinstance(error, TikHubHTTPError) and error.http_status in {400, 404, 408, 429, 500, 502, 503, 504}


class RejectTikHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(RejectTikHubRedirects())


@dataclass(frozen=True)
class BackfillCandidate:
    title: str
    source_url: str
    digest: str
    published_at: int

    @property
    def identity_hash(self) -> str:
        return hashlib.sha256(self.source_url.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_url": self.source_url,
            "digest": self.digest,
            "published_at": self.published_at,
        }


class TikHubTransport:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.tikhub.io",
        timeout: float = 60.0,
        max_calls: int = 100,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        maximum_seconds: float = 480,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _API_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise BackfillError("TikHub base URL is invalid")
        bearer = token.strip()
        if len(bearer) < 24 or any(character.isspace() for character in bearer):
            raise BackfillError("TikHub credential is unavailable")
        self.base_url = f"https://{parsed.hostname}"
        self.token = bearer
        self.timeout = timeout
        self.max_calls = max(1, max_calls)
        self.calls = 0
        self.sleeper, self.clock = sleeper, clock
        self.deadline = clock() + maximum_seconds
        self.opener = opener or (
            lambda request, timeout: _NO_REDIRECT_OPENER.open(request, timeout=timeout)
        )

    def post(self, endpoint: str, body: Mapping[str, Any]) -> dict[str, Any]:
        for attempt in range(3 if endpoint in {PROFILE_ENDPOINT, ARTICLES_ENDPOINT} else 1):
            try:
                return self._post_once(endpoint, body)
            except (TikHubHTTPError, TikHubTransportError) as error:
                if not recoverable_detail_error(error) or attempt == 2 or endpoint not in {PROFILE_ENDPOINT, ARTICLES_ENDPOINT}:
                    raise
                self.sleeper((1, 3)[attempt])
        raise AssertionError("unreachable API attempt")

    def _post_once(self, endpoint: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if endpoint not in _ENDPOINTS:
            raise BackfillError("TikHub endpoint is not allowed")
        if self.calls >= self.max_calls:
            raise TikHubBudgetExhausted("TikHub call budget is exhausted")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TikHubBudgetExhausted("TikHub time budget is exhausted")
        self.calls += 1
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "gatex-intelligence-source-runner/1",
            },
        )
        response = None
        try:
            response = self.opener(request, min(self.timeout, remaining))
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            raw = response.read(20_000_001)
        except urllib.error.HTTPError as error:
            error.close()
            raise TikHubHTTPError(error.code) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TikHubTransportError("TikHub request failed") from error
        finally:
            close = getattr(response, 'close', None)
            if callable(close): close()
        if status != 200:
            raise TikHubHTTPError(status)
        if len(raw) > 20_000_000:
            raise BackfillError("TikHub response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackfillError("TikHub returned an invalid response") from error
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise BackfillError("TikHub rejected the request")
        return payload


def _required_text(value: Any, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise BackfillError(f"{label} is invalid")
    text = _WHITESPACE.sub(" ", html.unescape(value)).strip()
    if not text or len(text) > maximum:
        raise BackfillError(f"{label} is invalid")
    return text


def verify_profile(
    transport: TikHubTransport,
    username: str,
    expected_publisher: str,
) -> None:
    if not _USERNAME.fullmatch(username):
        raise BackfillError("verified TikHub username is unavailable")
    payload = transport.post(PROFILE_ENDPOINT, {"username": username, "raw": False})
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BackfillError("TikHub profile identity did not match the sealed profile")
    returned_username = str(data.get("user_name") or "").strip()
    returned_publisher = _required_text(data.get("nick_name"), "profile publisher", 160)
    if (
        returned_username != username
        or returned_publisher.casefold() != expected_publisher.casefold()
    ):
        raise BackfillError("TikHub profile identity did not match the sealed profile")


def _candidate_groups(data: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    articles = data.get("articles")
    if not isinstance(articles, list):
        raise BackfillError("TikHub article page is invalid")
    for group in articles:
        if isinstance(group, dict):
            yield group


def candidates_from_page(data: Mapping[str, Any]) -> list[BackfillCandidate]:
    candidates: list[BackfillCandidate] = []
    seen: set[str] = set()
    for group in _candidate_groups(data):
        app_message = group.get("appMsg")
        app_message = app_message if isinstance(app_message, dict) else {}
        base = app_message.get("baseInfo")
        base = base if isinstance(base, dict) else {}
        details = app_message.get("detailInfo")
        if not isinstance(details, list):
            continue
        default_time = int(base.get("createTime") or base.get("updateTime") or 0)
        for detail in details:
            if not isinstance(detail, dict):
                continue
            url = str(detail.get("contentUrl") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = _required_text(detail.get("title"), "article title", 300)
            digest = _WHITESPACE.sub(" ", html.unescape(str(detail.get("digest") or ""))).strip()
            published_at = int(detail.get("sendTime") or detail.get("createTime") or default_time)
            if published_at <= 0:
                raise BackfillError("article publication time is invalid")
            candidates.append(
                BackfillCandidate(
                    title=title,
                    source_url=url,
                    digest=digest[:1000],
                    published_at=published_at,
                )
            )
    return candidates


def fetch_page(
    transport: TikHubTransport,
    *,
    username: str,
    offset: str,
    page_size: int,
) -> tuple[list[BackfillCandidate], str, bool]:
    payload = transport.post(
        ARTICLES_ENDPOINT,
        {
            "username": username,
            "page_size": max(10, min(page_size, 20)),
            "offset": offset,
            "item_show_type": 0,
            "raw": True,
        },
    )
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("biz_username") != username:
        raise BackfillError("TikHub article page identity did not match the sealed profile")
    next_offset = str(data.get("next_offset") or "")
    is_end = bool(data.get("is_end"))
    return candidates_from_page(data), next_offset, is_end


def _detail_content(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        raise BackfillError("TikHub article detail is invalid")
    return content


def envelope_from_detail(
    payload: Mapping[str, Any],
    *,
    intake_config: Mapping[str, Any],
    username: str,
    candidate: BackfillCandidate,
) -> dict[str, Any]:
    content = _detail_content(payload)
    if content.get("user_name") != username:
        raise BackfillError("TikHub article identity did not match the sealed profile")
    expected_publisher = _required_text(intake_config.get("publisher"), "publisher", 160)
    publisher = _required_text(content.get("nick_name"), "article publisher", 160)
    if publisher.casefold() != expected_publisher.casefold():
        raise BackfillError("TikHub article publisher did not match the sealed profile")
    expected_alias = str(intake_config.get("wechat_alias") or "").strip()
    if expected_alias and str(content.get("alias") or "").strip() != expected_alias:
        raise BackfillError("TikHub article alias did not match the sealed profile")
    deletion_status = "removed" if int(content.get("del_reason_id") or 0) else "active"
    article_text = content.get("content_text")
    if not isinstance(article_text, str) or not article_text.strip():
        if deletion_status == "active":
            raise BackfillError("article content is invalid")
        article_text = candidate.digest or candidate.title
    title = _required_text(content.get("title") or candidate.title, "article title", 300)
    author = _required_text(
        content.get("author") or intake_config.get("author"), "article author", 160
    )
    published = content.get("create_timestamp") or content.get("ori_create_time")
    if not published:
        published = content.get("create_time") or candidate.published_at
    if isinstance(published, str) and not published.isdigit():
        try:
            published = datetime.fromisoformat(published).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            published = candidate.published_at
    return build_envelope(
        channel_key=intake_config.get("channel_key"),
        title=title,
        publisher=publisher,
        author=author,
        source_url=candidate.source_url,
        published_at=published,
        content=article_text,
        language=str(intake_config.get("language") or "zh"),
        industry=str(intake_config.get("industry") or "technology"),
        access_scope=str(intake_config.get("access_scope") or "member"),
        collection_method="tikhub_backfill",
        deletion_status=deletion_status,
    )


def fetch_detail(
    transport: TikHubTransport,
    candidate: BackfillCandidate,
) -> dict[str, Any]:
    body = {"url": canonical_article_url(candidate.source_url), "raw": True}
    last_error = None
    for attempt, endpoint in enumerate((DETAIL_ENDPOINT, FALLBACK_DETAIL_ENDPOINT, DETAIL_ENDPOINT, FALLBACK_DETAIL_ENDPOINT)):
        try:
            return transport.post(endpoint, body)
        except (TikHubHTTPError, TikHubTransportError) as error:
            if not recoverable_detail_error(error):
                raise
            last_error = error
            if attempt < 3:
                getattr(transport, 'sleeper', time.sleep)((0, 1, 3)[attempt])
    raise last_error


def _state_candidates(value: Any) -> list[BackfillCandidate]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BackfillError("backfill pending state is invalid")
    result: list[BackfillCandidate] = []
    for item in value:
        if not isinstance(item, dict):
            raise BackfillError("backfill pending state is invalid")
        result.append(
            BackfillCandidate(
                title=_required_text(item.get("title"), "pending title", 300),
                source_url=_required_text(item.get("source_url"), "pending URL", 2048),
                digest=str(item.get("digest") or "")[:1000],
                published_at=int(item.get("published_at") or 0),
            )
        )
    return result


def candidate_key(candidate: BackfillCandidate) -> str:
    parsed = urlparse(canonical_article_url(candidate.source_url))
    values = dict(parse_qsl(parsed.query))
    identity = ':'.join(values.get(key, '') for key in ('__biz', 'mid', 'idx'))
    return hashlib.sha256((identity if all(values.get(key) for key in ('__biz','mid','idx')) else parsed.path).encode()).hexdigest()


def retry_due(ledger: Mapping[str, Any], key: str, stamp: float) -> bool:
    entry = ledger.get(key, {})
    return not entry or float(entry.get('next_retry_at', 0)) <= stamp


def record_retry(ledger: dict, key: str, error: BaseException, stamp: float) -> None:
    attempts = int(ledger.get(key, {}).get('attempts', 0)) + 1
    ledger[key] = {'attempts': attempts, 'last_failure_at': stamp,
        'next_retry_at': stamp + min(300 * 2 ** min(attempts - 1, 7), 21600),
        'error_type': type(error).__name__, 'http_status': getattr(error, 'http_status', None)}


def detail_budget_available(transport: Any, reserve: int = 4) -> bool:
    return (getattr(transport, 'calls', 0) + reserve <= getattr(transport, 'max_calls', 10000)
        and getattr(transport, 'clock', time.monotonic)() + 1 < getattr(transport, 'deadline', float('inf')))


def _prepare_backfill_page(state, active, username, limit, state_out, output_path, transform, status):
    # Page scanning and retry work are independent. One unavailable article
    # cannot pin the historical cursor to the same page indefinitely.
    pending = _state_candidates(state.get('pending'))
    offset = str(state.get('pending_next_offset') or state.get('offset') or '')
    legacy_page = bool(pending and (state.get('pending_next_offset') or state.get('pending_is_end')))
    ended = bool(state.get('pending_is_end')) if legacy_page else bool(state.get('is_end'))
    seen_raw, documents_raw = state.get('seen') or [], state.get('seen_documents') or []
    if not all(isinstance(value, list) and all(isinstance(item, str) for item in value) for value in (seen_raw, documents_raw)):
        raise BackfillError('backfill seen state is invalid')
    seen, documents = set(seen_raw), set(documents_raw)
    retries = dict(state.get('retry_state') or {})
    stamp = datetime.now(timezone.utc).timestamp()
    due = [item for item in pending if item.identity_hash not in seen and candidate_key(item) not in documents and retry_due(retries, candidate_key(item), stamp)]
    if not ended and len(due) < limit:
        page, offset, ended = fetch_page(active, username=username, offset=offset, page_size=min(limit,20))
        # Prefer fresh discovery URLs for the same document over saved sessions.
        queue = {candidate_key(item):item for item in pending}
        queue.update({candidate_key(item):item for item in page})
        pending = list(queue.values())
    records, failed = [], 0
    for candidate in pending:
        key = candidate_key(candidate)
        if candidate.identity_hash in seen or key in documents or not retry_due(retries,key,stamp):
            continue
        if len(records) >= limit or not detail_budget_available(active):
            break
        try:
            payload = fetch_detail(active, candidate)
        except TikHubBudgetExhausted:
            break
        except (TikHubHTTPError, TikHubTransportError) as error:
            if not recoverable_detail_error(error): raise
            record_retry(retries,key,error,stamp); failed += 1
            continue
        records.append(transform(candidate,payload))
        seen.add(candidate.identity_hash); documents.add(key); retries.pop(key,None)
    remaining = [item for item in pending if item.identity_hash not in seen and candidate_key(item) not in documents]
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with output_path.open('w',encoding='utf-8') as handle:
        for record in records:
            handle.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n')
    next_state = dict(state)
    deferred = sum(candidate_key(item) in retries for item in remaining)
    next_state.update(version=1, offset=offset, is_end=ended,
        pending=[item.as_dict() for item in remaining], pending_next_offset='',pending_is_end=False,
        seen=sorted(seen), seen_documents=sorted(documents),retry_state=retries,
        last_run={'status': 'partial' if deferred else status, 'prepared_count':len(records),
            'deferred_count':deferred,'attempted_failures':failed,'pending_count':len(remaining),
            'at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')})
    atomic_write_json(state_out,next_state)
    return len(records)


def run_backfill_page(
    *,
    config_path: Path,
    state_path: Path,
    state_out: Path,
    output_path: Path,
    token: str,
    maximum_items: int,
    base_url: str = "https://api.tikhub.io",
    transport: TikHubTransport | None = None,
) -> int:
    config = load_json(config_path)
    state = load_json(state_path)
    intake = config.get("intelligence_intake") if isinstance(config, dict) else None
    if not isinstance(intake, dict) or intake.get("enabled") is not True:
        raise BackfillError("sealed source profile is disabled")
    username = str(intake.get("tikhub_username") or "").strip()
    if not _USERNAME.fullmatch(username):
        raise BackfillError("verified TikHub username is unavailable")
    if intake.get("verification_status") != "verified":
        raise BackfillError("source identity is not verified")
    expected_publisher = _required_text(intake.get("publisher"), "publisher", 160)
    if not isinstance(state, dict):
        raise BackfillError("backfill state is invalid")
    limit = max(1, min(int(maximum_items), 50))
    active = transport or TikHubTransport(token, base_url=base_url, max_calls=limit * 4 + 6)
    verify_profile(active, username, expected_publisher)

    return _prepare_backfill_page(state, active, username, limit, state_out, output_path,
        lambda candidate, payload: envelope_from_detail(payload, intake_config=intake,
            username=username, candidate=candidate), 'ready')


def _first_digit(value: Any, maximum: int) -> str:
    """Return a bounded numeric identity value without exposing provider data."""
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > maximum:
        return ""
    return text


def _technology_identity(
    content: Mapping[str, Any], candidate: BackfillCandidate, approved_biz: str
) -> dict[str, str]:
    """Build the stable WeChat identity required by the private edition queue.

    TikHub can return either a signed article URL or a detail payload with the
    numeric message identity.  The approved publisher identity is sealed in
    the GateX Worker, so the collector uses that public allowlisted biz value
    and only accepts numeric mid/idx values from the preserved article.
    """
    urls = [
        str(content.get(key) or "").strip()
        for key in ("content_url", "url", "source_url", "link")
    ] + [candidate.source_url]
    query_values: dict[str, str] = {}
    for raw in urls:
        try:
            parsed = urlparse(raw)
        except ValueError:
            continue
        for key in ("__biz", "mid", "idx"):
            value = parsed.query and next(
                (part.split("=", 1)[1] for part in parsed.query.split("&") if part.startswith(key + "=")),
                "",
            )
            if value and key not in query_values:
                query_values[key] = value
    mid = ""
    for key in ("mid", "appmsgid", "app_msg_id", "msgid", "message_id"):
        mid = _first_digit(content.get(key), 20)
        if mid:
            break
    mid = mid or _first_digit(query_values.get("mid"), 20)
    idx = ""
    for key in ("idx", "item_idx", "item_index", "appmsg_index"):
        idx = _first_digit(content.get(key), 3)
        if idx:
            break
    idx = idx or _first_digit(query_values.get("idx"), 3)
    if not mid or not idx or not (1 <= int(idx) <= 999):
        raise BackfillError("article document identity is unavailable")
    if content.get('bizuin') and str(content['bizuin']) != approved_biz:
        raise BackfillError('article document source identity changed')
    expected={'__biz':approved_biz,'mid':mid,'idx':idx}
    for raw in urls:
        parsed=urlparse(raw)
        if parsed.hostname != 'mp.weixin.qq.com': continue
        query=dict(parse_qsl(parsed.query))
        if any(query.get(key) and query[key] != expected[key] for key in expected):
            raise BackfillError('article navigation identity changed')
    return expected


def _technology_source_from_detail(
    payload: Mapping[str, Any],
    *,
    intake_config: Mapping[str, Any],
    username: str,
    candidate: BackfillCandidate,
    approved_biz: str,
) -> dict[str, Any]:
    content = _detail_content(payload)
    if content.get("user_name") != username:
        raise BackfillError("TikHub article identity did not match the sealed profile")
    expected_publisher = _required_text(intake_config.get("publisher"), "publisher", 160)
    publisher = _required_text(content.get("nick_name"), "article publisher", 160)
    if publisher.casefold() != expected_publisher.casefold():
        raise BackfillError("TikHub article publisher did not match the sealed profile")
    expected_alias = str(intake_config.get("wechat_alias") or "").strip()
    if expected_alias and str(content.get("alias") or "").strip() != expected_alias:
        raise BackfillError("TikHub article alias did not match the sealed profile")
    article_text = content.get("content_text")
    if not isinstance(article_text, str) or not article_text.strip():
        raise BackfillError("article content is invalid")
    article_text = article_text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = source_lines(article_text, str(content.get('content_noencode') or ''))
    # TikHub often terminates article text with a newline. The source
    # contract carries meaningful article lines only, so discard trailing
    # blank lines before translation coverage is checked.
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(line.strip() for line in lines) or len(lines) > 2000:
        raise BackfillError("article content is invalid")
    if any(len(line) > 100000 for line in lines):
        raise BackfillError("article content is invalid")
    title = _required_text(content.get("title") or candidate.title, "article title", 500)
    published = content.get("create_timestamp") or content.get("ori_create_time")
    if not published:
        published = content.get("create_time") or candidate.published_at
    if isinstance(published, str) and not published.isdigit():
        try:
            published = datetime.fromisoformat(published).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            published = candidate.published_at
    if isinstance(published, (int, float)) or (isinstance(published, str) and published.isdigit()):
        stamp = int(published)
        if stamp > 100_000_000_000:
            stamp //= 1000
        published = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    published_date = str(published)[:10]
    try:
        datetime.fromisoformat(published_date)
    except ValueError as error:
        raise BackfillError("article publication time is invalid") from error
    source_url = next(
        (str(content.get(key) or "").strip() for key in ("content_url", "url", "link", "source_url") if str(content.get(key) or "").strip() and urlparse(str(content[key])).hostname == "mp.weixin.qq.com"),
        candidate.source_url,
    )
    return {
        "schema": "gatex-technology-source/v1",
        "documentIdentity": _technology_identity(content, candidate, approved_biz),
        "sourceName": "Unsolved Problems",
        "sourceUrl": source_url,
        "title": title,
        "publishedAt": published_date,
        "lines": lines,
    }


def run_technology_backfill_page(
    *,
    config_path: Path,
    state_path: Path,
    state_out: Path,
    output_path: Path,
    token: str,
    maximum_items: int,
    base_url: str = "https://api.tikhub.io",
    transport: TikHubTransport | None = None,
) -> int:
    """Prepare a resumable page of complete Unsolved Problems source records."""
    config = load_json(config_path)
    state = load_json(state_path)
    intake = config.get("intelligence_intake") if isinstance(config, dict) else None
    if not isinstance(intake, dict) or intake.get("enabled") is not True:
        raise BackfillError("sealed source profile is disabled")
    username = str(intake.get("tikhub_username") or "").strip()
    if not _USERNAME.fullmatch(username):
        raise BackfillError("verified TikHub username is unavailable")
    if intake.get("verification_status") != "verified":
        raise BackfillError("source identity is not verified")
    expected_publisher = _required_text(intake.get("publisher"), "publisher", 160)
    provider = config.get("provider") if isinstance(config, dict) else None
    approved_biz = str(provider.get("expected_biz") or "").strip() if isinstance(provider, dict) else ""
    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", approved_biz):
        raise BackfillError("approved source identity is unavailable")
    if not isinstance(state, dict):
        raise BackfillError("backfill state is invalid")
    limit = max(1, min(int(maximum_items), 50))
    active = transport or TikHubTransport(token, base_url=base_url, max_calls=limit * 4 + 6)
    verify_profile(active, username, expected_publisher)

    return _prepare_backfill_page(state, active, username, limit, state_out, output_path,
        lambda candidate, payload: _technology_source_from_detail(payload, intake_config=intake,
            username=username, candidate=candidate, approved_biz=approved_biz), 'technology-ready')
