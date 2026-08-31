from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


INTAKE_SCHEMA = "gatex-intelligence-intake/v1"
MAX_SOURCE_EXCERPT_CHARS = 180
MAX_EVIDENCE_EXCERPT_CHARS = 180
MAX_PRIVATE_DOCUMENT_BYTES = 2_000_000
PRIVATE_DOCUMENT_MIME_TYPE = "text/plain; charset=utf-8"
OFFICIAL_REPORT_CHANNEL_KEY = "gatex-e2e-official-source-v3"
OFFICIAL_REPORT_URL = "https://www.iea.org/reports/energy-and-ai"
OFFICIAL_REPORT_TITLE = "Energy and AI"
OFFICIAL_REPORT_PUBLISHED_AT = "2025-04-10T00:00:00Z"
OFFICIAL_REPORT_PUBLISHER = "International Energy Agency"
OFFICIAL_REPORT_AUTHOR = "International Energy Agency"
OFFICIAL_REPORT_COLLECTION_METHOD = "manual_e2e_official_source"
OFFICIAL_REPORT_NOTE = (
    "The IEA Energy and AI report estimates data-centre electricity consumption at about "
    "415 TWh in 2024 and projects about 945 TWh by 2030 in its base case. GateX should "
    "independently verify the figures and assess grid, supply-chain and investment implications "
    "for China and Gulf markets."
)
OFFICIAL_REPORT_TOPIC_BRIEF = (
    "Create an original GateX decision-intelligence edition from this official public source "
    "signal. Independently verify all material facts, explain the implications for China and "
    "Gulf markets, cite primary sources, use only short quotations, and do not reproduce the "
    "source report."
)
OFFICIAL_REPORT_IDENTITY_HASH = hashlib.sha256(
    OFFICIAL_REPORT_URL.encode("utf-8")
).hexdigest()
OFFICIAL_REPORT_EXTERNAL_ID = f"official:{OFFICIAL_REPORT_IDENTITY_HASH}"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_WHITESPACE = re.compile(r"\s+")
_ALLOWED_SOURCE_HOSTS = {"mp.weixin.qq.com"}
_IDENTITY_QUERY_KEYS = ("__biz", "mid", "idx", "sn")
_TOP_LEVEL_FIELDS = {
    "schema",
    "channelKey",
    "externalId",
    "idempotencyKey",
    "topic",
    "triggerDraft",
    "sources",
    "evidence",
    "metadata",
    "privateDocument",
}


class IntakeContractError(ValueError):
    pass


def _required_string(value: Any, label: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise IntakeContractError(f"{label} must be a string")
    text = _WHITESPACE.sub(" ", html.unescape(value)).strip()
    if not text or len(text) > maximum:
        raise IntakeContractError(f"{label} is missing or too long")
    return text


def _optional_string(value: Any, *, maximum: int = 500) -> str:
    if value is None:
        return ""
    return _required_string(value, "optional value", maximum=maximum)


def _excerpt(value: str, maximum: int) -> str:
    text = _WHITESPACE.sub(" ", html.unescape(value)).strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "\u2026"


def _private_text(value: Any) -> str:
    if not isinstance(value, str):
        raise IntakeContractError("content must be a string")
    normalized = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text or len(text.encode("utf-8")) > MAX_PRIVATE_DOCUMENT_BYTES:
        raise IntakeContractError("content is missing or exceeds the private document limit")
    return text


def _published_at(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    text = _required_string(value, "published_at", maximum=80)
    if text.isdigit() and len(text) == 10:
        return datetime.fromtimestamp(int(text), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise IntakeContractError("published_at must be ISO-8601 or an epoch") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_url(value: Any) -> str:
    raw = _required_string(value, "source_url", maximum=2048)
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _ALLOWED_SOURCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or parsed.path not in {"/s", "/s/"}
    ):
        raise IntakeContractError("source_url is not an allowed article URL")
    query = parse_qs(parsed.query, keep_blank_values=False)
    identity = {key: query[key][0] for key in _IDENTITY_QUERY_KEYS if query.get(key)}
    if len(identity) != len(_IDENTITY_QUERY_KEYS):
        raise IntakeContractError("source_url has no stable article identity")
    clean_query = urlencode([(key, identity[key]) for key in _IDENTITY_QUERY_KEYS])
    return urlunparse(("https", parsed.hostname, "/s", "", clean_query, ""))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _identity_hash(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _idempotency_key(channel_key: str, source_url: str, content_hash: str) -> str:
    payload = "\n".join((INTAKE_SCHEMA, channel_key, source_url, content_hash))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeContractError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if set(value) - allowed:
        raise IntakeContractError(f"{label} contains fields outside the collector contract")


def _bounded_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntakeContractError(f"{label} must be a number")
    number = float(value)
    if not 0 <= number <= 1:
        raise IntakeContractError(f"{label} must be between zero and one")
    return number


def build_envelope(
    *,
    channel_key: str,
    title: str,
    publisher: str,
    author: str,
    source_url: str,
    published_at: str | int | float,
    content: str,
    language: str = "zh",
    industry: str = "technology",
    access_scope: str = "member",
    quality_score: float = 0.82,
    relevance_score: float = 0.86,
    collection_method: str = "sogou_incremental",
    deletion_status: str = "active",
) -> dict[str, Any]:
    clean_channel = _required_string(channel_key, "channel_key", maximum=120)
    clean_title = _required_string(title, "title", maximum=300)
    clean_publisher = _required_string(publisher, "publisher", maximum=160)
    clean_author = _required_string(author, "author", maximum=160)
    clean_url = _source_url(source_url)
    clean_published_at = _published_at(published_at)
    clean_content = _private_text(content)
    clean_hash = _content_hash(clean_content)
    clean_language = _required_string(language, "language", maximum=32)
    clean_industry = _required_string(industry, "industry", maximum=80)
    clean_scope = _required_string(access_scope, "access_scope", maximum=32)
    if clean_language not in {"zh", "en"}:
        raise IntakeContractError("language is invalid")
    if clean_scope not in {"public", "member", "advanced", "staff"}:
        raise IntakeContractError("access_scope is invalid")
    clean_method = _required_string(collection_method, "collection_method", maximum=80)
    clean_deletion_status = _required_string(
        deletion_status, "deletion_status", maximum=32
    )
    if clean_deletion_status not in {"active", "removed", "unknown"}:
        raise IntakeContractError("deletion_status is invalid")
    source_excerpt = _excerpt(clean_content, MAX_SOURCE_EXCERPT_CHARS)
    evidence_excerpt = _excerpt(clean_content, MAX_EVIDENCE_EXCERPT_CHARS)
    identity_hash = _identity_hash(clean_url)
    idempotency_key = _idempotency_key(clean_channel, clean_url, clean_hash)
    external_id = f"wechat:{identity_hash}"
    source_is_active = clean_deletion_status == "active"
    envelope = {
        "schema": INTAKE_SCHEMA,
        "channelKey": clean_channel,
        "externalId": external_id,
        "idempotencyKey": idempotency_key,
        "topic": {
            "title": f"GateX Perspective: {clean_title}",
            "brief": (
                "Create an original GateX Intelligence edition from the source signal below. "
                "Use the GateX report format and visual identity throughout, attribute the author "
                "and publisher, verify material claims with independent public sources, use only "
                "short quotations, and never reproduce the article in full."
            ),
            "industry": clean_industry,
            "language": clean_language,
            "accessScope": clean_scope,
            "priority": "normal",
            "provenanceType": "source_channel",
        },
        "triggerDraft": source_is_active,
        "sources": [
            {
                "kind": "social",
                "url": clean_url,
                "title": clean_title,
                "publisher": clean_publisher,
                "publishedAt": clean_published_at,
                "excerpt": source_excerpt,
                "contentHash": clean_hash,
                "status": "accepted" if source_is_active else "withdrawn",
                "qualityScore": min(1.0, max(0.0, float(quality_score))),
                "relevanceScore": min(1.0, max(0.0, float(relevance_score))),
                "metadata": {
                    "author": clean_author,
                    "accountDisplayName": clean_publisher,
                    "sourceIdentityHash": identity_hash,
                    "collectionMethod": clean_method,
                    "attributionRequired": True,
                    "factCheckRequired": True,
                    "maxQuoteCharacters": MAX_EVIDENCE_EXCERPT_CHARS,
                    "reusePolicy": "original_summary_only",
                    "deletionStatus": clean_deletion_status,
                    "rightsReviewStatus": "policy_cleared",
                },
            }
        ],
        "evidence": [
            {
                "sourceUrl": clean_url,
                "claimId": f"source-excerpt:{identity_hash[:16]}",
                "excerpt": evidence_excerpt,
                "confidence": 0.55,
                "status": "accepted" if source_is_active else "withdrawn",
                "metadata": {
                    "quotation": True,
                    "generationMustVerifyIndependently": True,
                },
            }
        ],
        "metadata": {
            "pipeline": "gatex_curated_source_v1",
            "productionMethod": "curated_source",
            "brand": "GateX",
            "presentationFormat": "gatex_report_v1",
            "publicationState": "auto_publish_pending" if source_is_active else "source_removed",
            "humanApprovalRequired": False,
            "autoPublish": source_is_active,
            "autoPublishAfterQuality": source_is_active,
            "machineQualityGateRequired": True,
            "sourceAuthor": clean_author,
            "sourcePublisher": clean_publisher,
            "sourcePublishedAt": clean_published_at,
            "sourceContentHash": clean_hash,
            "sourceDeletionStatus": clean_deletion_status,
        },
    }
    if source_is_active:
        envelope["privateDocument"] = {
            "mimeType": PRIVATE_DOCUMENT_MIME_TYPE,
            "content": clean_content,
            "sha256": clean_hash,
        }
    validate_envelope(envelope)
    return envelope


def build_official_report_e2e_envelope() -> dict[str, Any]:
    clean_content = _private_text(OFFICIAL_REPORT_NOTE)
    content_hash = _content_hash(clean_content)
    idempotency_key = _idempotency_key(
        OFFICIAL_REPORT_CHANNEL_KEY,
        OFFICIAL_REPORT_URL,
        content_hash,
    )
    excerpt = _excerpt(clean_content, MAX_SOURCE_EXCERPT_CHARS)
    envelope = {
        "schema": INTAKE_SCHEMA,
        "channelKey": OFFICIAL_REPORT_CHANNEL_KEY,
        "externalId": OFFICIAL_REPORT_EXTERNAL_ID,
        "idempotencyKey": idempotency_key,
        "topic": {
            "title": f"GateX Perspective: {OFFICIAL_REPORT_TITLE}",
            "brief": OFFICIAL_REPORT_TOPIC_BRIEF,
            "industry": "AI infrastructure",
            "language": "en",
            "accessScope": "public",
            "priority": "normal",
            "provenanceType": "source_channel",
        },
        "triggerDraft": True,
        "sources": [
            {
                "kind": "report",
                "url": OFFICIAL_REPORT_URL,
                "title": OFFICIAL_REPORT_TITLE,
                "publisher": OFFICIAL_REPORT_PUBLISHER,
                "publishedAt": OFFICIAL_REPORT_PUBLISHED_AT,
                "excerpt": excerpt,
                "contentHash": content_hash,
                "status": "accepted",
                "qualityScore": 0.9,
                "relevanceScore": 0.9,
                "metadata": {
                    "author": OFFICIAL_REPORT_AUTHOR,
                    "accountDisplayName": OFFICIAL_REPORT_PUBLISHER,
                    "sourceIdentityHash": OFFICIAL_REPORT_IDENTITY_HASH,
                    "collectionMethod": OFFICIAL_REPORT_COLLECTION_METHOD,
                    "attributionRequired": True,
                    "factCheckRequired": True,
                    "maxQuoteCharacters": MAX_EVIDENCE_EXCERPT_CHARS,
                    "reusePolicy": "original_summary_only",
                    "deletionStatus": "active",
                    "rightsReviewStatus": "policy_cleared",
                },
            }
        ],
        "evidence": [
            {
                "sourceUrl": OFFICIAL_REPORT_URL,
                "claimId": f"official-source-note:{OFFICIAL_REPORT_IDENTITY_HASH[:16]}",
                "excerpt": excerpt,
                "confidence": 0.8,
                "status": "accepted",
                "metadata": {
                    "quotation": False,
                    "generationMustVerifyIndependently": True,
                },
            }
        ],
        "metadata": {
            "pipeline": "gatex_curated_source_v1",
            "productionMethod": "curated_source",
            "brand": "GateX",
            "presentationFormat": "gatex_report_v1",
            "publicationState": "auto_publish_pending",
            "humanApprovalRequired": False,
            "autoPublish": True,
            "autoPublishAfterQuality": True,
            "machineQualityGateRequired": True,
            "sourceAuthor": OFFICIAL_REPORT_AUTHOR,
            "sourcePublisher": OFFICIAL_REPORT_PUBLISHER,
            "sourcePublishedAt": OFFICIAL_REPORT_PUBLISHED_AT,
            "sourceContentHash": content_hash,
            "sourceDeletionStatus": "active",
        },
        "privateDocument": {
            "mimeType": PRIVATE_DOCUMENT_MIME_TYPE,
            "content": clean_content,
            "sha256": content_hash,
        },
    }
    validate_envelope(envelope)
    return envelope


def validate_envelope(value: Mapping[str, Any]) -> None:
    envelope = _object(value, "intake envelope")
    _exact_fields(envelope, _TOP_LEVEL_FIELDS, "intake envelope")
    if envelope.get("schema") != INTAKE_SCHEMA:
        raise IntakeContractError("intake envelope schema is invalid")
    channel_key = _required_string(envelope.get("channelKey"), "channelKey", maximum=120)

    topic = _object(envelope.get("topic"), "topic")
    _exact_fields(
        topic,
        {
            "title",
            "brief",
            "industry",
            "language",
            "accessScope",
            "priority",
            "provenanceType",
        },
        "topic",
    )
    _required_string(topic.get("title"), "topic.title", maximum=340)
    _required_string(topic.get("brief"), "topic.brief", maximum=1_000)
    _required_string(topic.get("industry"), "topic.industry", maximum=80)
    if topic.get("language") not in {"zh", "en"}:
        raise IntakeContractError("topic.language is invalid")
    if topic.get("accessScope") not in {"public", "member", "advanced", "staff"}:
        raise IntakeContractError("topic.accessScope is invalid")
    if topic.get("priority") not in {"low", "normal", "high", "urgent"}:
        raise IntakeContractError("topic.priority is invalid")
    if topic.get("provenanceType") != "source_channel":
        raise IntakeContractError("topic.provenanceType is invalid")

    sources = envelope.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        raise IntakeContractError("intake envelope must contain one source")
    source = _object(sources[0], "source")
    _exact_fields(
        source,
        {
            "kind",
            "url",
            "title",
            "publisher",
            "publishedAt",
            "excerpt",
            "contentHash",
            "status",
            "qualityScore",
            "relevanceScore",
            "metadata",
        },
        "source",
    )
    source_kind = source.get("kind")
    if source_kind not in {"social", "report"}:
        raise IntakeContractError("source.kind is invalid")
    official_report = source_kind == "report"
    if official_report:
        source_url = _required_string(source.get("url"), "source.url", maximum=2048)
        if source.get("url") != OFFICIAL_REPORT_URL:
            raise IntakeContractError("official report URL is invalid")
    else:
        source_url = _source_url(source.get("url"))
        if source_url != source.get("url"):
            raise IntakeContractError("source.url is not canonical")
    _required_string(source.get("title"), "source.title", maximum=300)
    publisher = _required_string(source.get("publisher"), "source.publisher", maximum=160)
    published_at = _published_at(source.get("publishedAt"))
    if published_at != source.get("publishedAt"):
        raise IntakeContractError("source.publishedAt is not canonical")
    if official_report and (
        envelope.get("channelKey") != OFFICIAL_REPORT_CHANNEL_KEY
        or source.get("title") != OFFICIAL_REPORT_TITLE
        or source.get("publisher") != OFFICIAL_REPORT_PUBLISHER
        or published_at != OFFICIAL_REPORT_PUBLISHED_AT
        or topic.get("title") != f"GateX Perspective: {OFFICIAL_REPORT_TITLE}"
        or topic.get("brief") != OFFICIAL_REPORT_TOPIC_BRIEF
        or topic.get("industry") != "AI infrastructure"
        or topic.get("language") != "en"
        or topic.get("accessScope") != "public"
        or topic.get("priority") != "normal"
    ):
        raise IntakeContractError("official report identity is invalid")
    excerpt = source.get("excerpt")
    if not isinstance(excerpt, str) or len(excerpt) > MAX_SOURCE_EXCERPT_CHARS:
        raise IntakeContractError("source.excerpt is invalid")
    if official_report and excerpt != _excerpt(
        OFFICIAL_REPORT_NOTE,
        MAX_SOURCE_EXCERPT_CHARS,
    ):
        raise IntakeContractError("official report excerpt is invalid")
    content_hash = str(source.get("contentHash") or "")
    if not _SHA256.fullmatch(content_hash):
        raise IntakeContractError("source.contentHash is invalid")
    quality_score = _bounded_number(source.get("qualityScore"), "source.qualityScore")
    relevance_score = _bounded_number(source.get("relevanceScore"), "source.relevanceScore")
    if official_report and (quality_score != 0.9 or relevance_score != 0.9):
        raise IntakeContractError("official report source scores are invalid")

    source_metadata = _object(source.get("metadata"), "source.metadata")
    _exact_fields(
        source_metadata,
        {
            "author",
            "accountDisplayName",
            "sourceIdentityHash",
            "collectionMethod",
            "attributionRequired",
            "factCheckRequired",
            "maxQuoteCharacters",
            "reusePolicy",
            "deletionStatus",
            "rightsReviewStatus",
        },
        "source.metadata",
    )
    author = _required_string(source_metadata.get("author"), "source.metadata.author", maximum=160)
    if source_metadata.get("accountDisplayName") != publisher:
        raise IntakeContractError("source account identity does not match its publisher")
    identity_hash = _identity_hash(source_url)
    if source_metadata.get("sourceIdentityHash") != identity_hash:
        raise IntakeContractError("source identity hash is invalid")
    if official_report:
        if (
            source_metadata.get("author") != OFFICIAL_REPORT_AUTHOR
            or identity_hash != OFFICIAL_REPORT_IDENTITY_HASH
            or source_metadata.get("collectionMethod")
            != OFFICIAL_REPORT_COLLECTION_METHOD
        ):
            raise IntakeContractError("official report source metadata is invalid")
    elif source_metadata.get("collectionMethod") not in {
        "sogou_incremental",
        "tikhub_backfill",
    }:
        raise IntakeContractError("source collection method is invalid")
    if source_metadata.get("attributionRequired") is not True:
        raise IntakeContractError("source attribution must be required")
    if source_metadata.get("factCheckRequired") is not True:
        raise IntakeContractError("source fact checking must be required")
    if source_metadata.get("maxQuoteCharacters") != MAX_EVIDENCE_EXCERPT_CHARS:
        raise IntakeContractError("source quotation limit is invalid")
    if source_metadata.get("reusePolicy") != "original_summary_only":
        raise IntakeContractError("source reuse policy is invalid")
    deletion_status = source_metadata.get("deletionStatus")
    if deletion_status not in {"active", "removed", "unknown"}:
        raise IntakeContractError("source deletion status is invalid")
    if source_metadata.get("rightsReviewStatus") != "policy_cleared":
        raise IntakeContractError("source rights review status is invalid")

    if official_report and deletion_status != "active":
        raise IntakeContractError("official report must remain active")
    source_is_active = deletion_status == "active"
    expected_source_status = "accepted" if source_is_active else "withdrawn"
    if source.get("status") != expected_source_status:
        raise IntakeContractError("source status is inconsistent with deletion status")
    if envelope.get("triggerDraft") is not source_is_active:
        raise IntakeContractError("triggerDraft is inconsistent with source status")

    if source_is_active:
        private_document = _object(envelope.get("privateDocument"), "privateDocument")
        _exact_fields(
            private_document,
            {"mimeType", "content", "sha256"},
            "privateDocument",
        )
        if private_document.get("mimeType") != PRIVATE_DOCUMENT_MIME_TYPE:
            raise IntakeContractError("privateDocument.mimeType is invalid")
        private_content = _private_text(private_document.get("content"))
        if private_content != private_document.get("content"):
            raise IntakeContractError("privateDocument.content is not canonical")
        if official_report and private_content != OFFICIAL_REPORT_NOTE:
            raise IntakeContractError("official report source note is invalid")
        private_hash = _content_hash(private_content)
        if private_document.get("sha256") != private_hash or content_hash != private_hash:
            raise IntakeContractError("privateDocument hash does not match source content")
    elif "privateDocument" in envelope:
        raise IntakeContractError("withdrawn sources cannot include a private document")

    evidence_rows = envelope.get("evidence")
    if not isinstance(evidence_rows, list) or len(evidence_rows) != 1:
        raise IntakeContractError("intake envelope must contain one evidence row")
    evidence = _object(evidence_rows[0], "evidence")
    _exact_fields(
        evidence,
        {"sourceUrl", "claimId", "excerpt", "confidence", "status", "metadata"},
        "evidence",
    )
    if evidence.get("sourceUrl") != source_url:
        raise IntakeContractError("evidence source URL does not match")
    _required_string(evidence.get("claimId"), "evidence.claimId", maximum=200)
    evidence_excerpt = evidence.get("excerpt")
    if not isinstance(evidence_excerpt, str) or len(evidence_excerpt) > MAX_EVIDENCE_EXCERPT_CHARS:
        raise IntakeContractError("evidence excerpt is invalid")
    confidence = _bounded_number(evidence.get("confidence"), "evidence.confidence")
    if official_report and (
        evidence.get("claimId")
        != f"official-source-note:{OFFICIAL_REPORT_IDENTITY_HASH[:16]}"
        or evidence_excerpt
        != _excerpt(OFFICIAL_REPORT_NOTE, MAX_EVIDENCE_EXCERPT_CHARS)
        or confidence != 0.8
    ):
        raise IntakeContractError("official report evidence is invalid")
    expected_evidence_status = "accepted" if source_is_active else "withdrawn"
    if evidence.get("status") != expected_evidence_status:
        raise IntakeContractError("evidence status is inconsistent with source status")
    evidence_metadata = _object(evidence.get("metadata"), "evidence.metadata")
    _exact_fields(
        evidence_metadata,
        {"quotation", "generationMustVerifyIndependently"},
        "evidence.metadata",
    )
    expected_quotation = not official_report
    if evidence_metadata.get("quotation") is not expected_quotation:
        raise IntakeContractError("evidence quotation flag is invalid")
    if evidence_metadata.get("generationMustVerifyIndependently") is not True:
        raise IntakeContractError("evidence verification flag is invalid")

    metadata = _object(envelope.get("metadata"), "metadata")
    _exact_fields(
        metadata,
        {
            "pipeline",
            "productionMethod",
            "brand",
            "presentationFormat",
            "publicationState",
            "humanApprovalRequired",
            "autoPublish",
            "autoPublishAfterQuality",
            "machineQualityGateRequired",
            "sourceAuthor",
            "sourcePublisher",
            "sourcePublishedAt",
            "sourceContentHash",
            "sourceDeletionStatus",
        },
        "metadata",
    )
    if metadata.get("pipeline") != "gatex_curated_source_v1":
        raise IntakeContractError("metadata.pipeline is invalid")
    if metadata.get("productionMethod") != "curated_source":
        raise IntakeContractError("metadata.productionMethod is invalid")
    if metadata.get("brand") != "GateX" or metadata.get("presentationFormat") != "gatex_report_v1":
        raise IntakeContractError("GateX presentation metadata is invalid")
    expected_publication_state = "auto_publish_pending" if source_is_active else "source_removed"
    if metadata.get("publicationState") != expected_publication_state:
        raise IntakeContractError("metadata.publicationState is invalid")
    if metadata.get("humanApprovalRequired") is not False:
        raise IntakeContractError("publication governance is invalid")
    if metadata.get("autoPublish") is not source_is_active:
        raise IntakeContractError("automatic publication state is inconsistent with source status")
    if metadata.get("autoPublishAfterQuality") is not source_is_active:
        raise IntakeContractError("quality-gated publication state is inconsistent with source status")
    if metadata.get("machineQualityGateRequired") is not True:
        raise IntakeContractError("machine quality gate must be required")
    if metadata.get("sourceAuthor") != author or metadata.get("sourcePublisher") != publisher:
        raise IntakeContractError("source attribution metadata does not match")
    if metadata.get("sourcePublishedAt") != published_at:
        raise IntakeContractError("source publication metadata does not match")
    if metadata.get("sourceContentHash") != content_hash:
        raise IntakeContractError("source content hash metadata does not match")
    if metadata.get("sourceDeletionStatus") != deletion_status:
        raise IntakeContractError("source deletion metadata does not match")

    external_id = (
        OFFICIAL_REPORT_EXTERNAL_ID
        if official_report
        else f"wechat:{identity_hash}"
    )
    if envelope.get("externalId") != external_id:
        raise IntakeContractError("externalId is invalid")
    expected_idempotency = _idempotency_key(channel_key, source_url, content_hash)
    if envelope.get("idempotencyKey") != expected_idempotency:
        raise IntakeContractError("idempotencyKey is invalid")


def _safe_article_directory(batch_root: Path, value: Any) -> Path:
    relative = Path(_required_string(value, "article_directory", maximum=500))
    if relative.is_absolute() or ".." in relative.parts:
        raise IntakeContractError("article_directory is unsafe")
    root = batch_root.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise IntakeContractError("article_directory escaped its batch")
    return candidate


def envelopes_from_batch(
    *,
    batch_root: Path,
    intake_config: Mapping[str, Any],
    collection_method: str = "sogou_incremental",
) -> list[dict[str, Any]]:
    root = batch_root.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntakeContractError("batch manifest is unavailable") from error
    records = manifest.get("articles") if isinstance(manifest, dict) else None
    if not isinstance(records, list):
        raise IntakeContractError("batch manifest has no article list")

    channel_key = _required_string(intake_config.get("channel_key"), "channel_key", maximum=120)
    expected_publisher = _required_string(
        intake_config.get("publisher"), "publisher", maximum=160
    )
    author = _required_string(intake_config.get("author"), "author", maximum=160)
    language = _optional_string(intake_config.get("language"), maximum=32) or "zh"
    industry = _optional_string(intake_config.get("industry"), maximum=80) or "technology"
    access_scope = _optional_string(intake_config.get("access_scope"), maximum=32) or "member"

    envelopes: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise IntakeContractError("batch article record is invalid")
        article_dir = _safe_article_directory(root, record.get("article_directory"))
        try:
            metadata = json.loads((article_dir / "metadata.json").read_text(encoding="utf-8"))
            content = (article_dir / "content.txt").read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as error:
            raise IntakeContractError("batch article files are incomplete") from error
        if not isinstance(metadata, dict):
            raise IntakeContractError("batch article metadata is invalid")
        publisher = _required_string(metadata.get("source"), "publisher", maximum=160)
        if publisher.casefold() != expected_publisher.casefold():
            raise IntakeContractError("batch article publisher does not match the sealed profile")
        envelopes.append(
            build_envelope(
                channel_key=channel_key,
                title=metadata.get("title"),
                publisher=publisher,
                author=author,
                source_url=metadata.get("url") or metadata.get("resolved_url"),
                published_at=metadata.get("published_at"),
                content=content,
                language=language,
                industry=industry,
                access_scope=access_scope,
                collection_method=collection_method,
            )
        )
    return envelopes


def write_jsonl(path: Path, envelopes: Iterable[Mapping[str, Any]]) -> int:
    values = list(envelopes)
    for value in values:
        validate_envelope(value)
    payload = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return len(values)
