from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from intelligence_sources.contract import (
    MAX_EVIDENCE_EXCERPT_CHARS,
    MAX_PRIVATE_DOCUMENT_BYTES,
    MAX_SOURCE_EXCERPT_CHARS,
    PRIVATE_DOCUMENT_MIME_TYPE,
    IntakeContractError,
    build_envelope,
    envelopes_from_batch,
    write_jsonl,
)
from intelligence_sources.delivery import (
    IntakeDeliveryError,
    RejectRedirects,
    build_direct_opener,
    deliver_envelopes,
    deliver_file,
)
from intelligence_sources.tikhub_backfill import (
    DETAIL_ENDPOINT,
    PROFILE_ENDPOINT,
    BackfillCandidate,
    BackfillError,
    candidates_from_page,
    envelope_from_detail,
    fetch_page,
    run_backfill_page,
    verify_profile,
)


SOURCE_URL = (
    "http://mp.weixin.qq.com/s?__biz=synthetic-biz&mid=100&idx=1&sn=synthetic-sn"
    "&scene=1#rd"
)
ROOT = Path(__file__).resolve().parents[1]


def intake_config() -> dict:
    return {
        "enabled": True,
        "verification_status": "verified",
        "channel_key": "synthetic-channel",
        "publisher": "Synthetic Publisher",
        "author": "Synthetic Author",
        "wechat_alias": "synthetic_alias",
        "tikhub_username": "gh_synthetic123",
        "language": "zh",
        "industry": "technology",
        "access_scope": "member",
    }


class ContractTests(unittest.TestCase):
    def test_published_schema_matches_contract_limits_and_enums(self):
        schema = json.loads(
            (ROOT / "schemas/gatex-intelligence-intake.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            "gatex-intelligence-intake/v1",
        )
        self.assertEqual(
            schema["properties"]["topic"]["properties"]["provenanceType"]["const"],
            "source_channel",
        )
        self.assertEqual(
            schema["properties"]["topic"]["properties"]["language"]["enum"],
            ["zh", "en"],
        )
        self.assertEqual(
            schema["properties"]["topic"]["properties"]["accessScope"]["enum"],
            ["public", "member", "advanced", "staff"],
        )
        source = schema["properties"]["sources"]["items"]["properties"]
        self.assertEqual(source["kind"]["const"], "social")
        self.assertEqual(source["status"]["enum"], ["accepted", "withdrawn"])
        self.assertEqual(source["excerpt"]["maxLength"], MAX_SOURCE_EXCERPT_CHARS)
        deletion = source["metadata"]["properties"]["deletionStatus"]["enum"]
        self.assertEqual(deletion, ["active", "removed", "unknown"])
        private_document = schema["properties"]["privateDocument"]
        self.assertEqual(
            private_document["properties"]["mimeType"]["const"],
            PRIVATE_DOCUMENT_MIME_TYPE,
        )
        self.assertEqual(
            private_document["properties"]["content"]["maxLength"],
            MAX_PRIVATE_DOCUMENT_BYTES,
        )
        self.assertFalse(schema["additionalProperties"])

    def test_envelope_has_short_public_fields_and_a_private_generation_document(self):
        content = ("Synthetic source material. " * 40) + "END-MARKER"
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content=content,
        )
        source = envelope["sources"][0]
        self.assertTrue(envelope["triggerDraft"])
        self.assertEqual(envelope["topic"]["provenanceType"], "source_channel")
        self.assertFalse(envelope["metadata"]["autoPublish"])
        self.assertEqual(source["metadata"]["author"], "Synthetic Author")
        self.assertEqual(source["metadata"]["deletionStatus"], "active")
        self.assertEqual(source["kind"], "social")
        self.assertEqual(source["status"], "accepted")
        self.assertEqual(envelope["topic"]["language"], "zh")
        self.assertEqual(envelope["topic"]["accessScope"], "member")
        self.assertLessEqual(len(source["excerpt"]), MAX_SOURCE_EXCERPT_CHARS)
        self.assertLessEqual(
            len(envelope["evidence"][0]["excerpt"]), MAX_EVIDENCE_EXCERPT_CHARS
        )
        self.assertNotIn("END-MARKER", json.dumps(envelope["sources"]))
        self.assertNotIn("END-MARKER", json.dumps(envelope["evidence"]))
        self.assertIn("END-MARKER", envelope["privateDocument"]["content"])
        self.assertEqual(
            envelope["privateDocument"]["sha256"], source["contentHash"]
        )
        self.assertEqual(source["url"], "https://mp.weixin.qq.com/s?__biz=synthetic-biz&mid=100&idx=1&sn=synthetic-sn")

    def test_batch_export_rejects_escape_and_wrong_publisher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(
                json.dumps({"articles": [{"article_directory": "../escape"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntakeContractError, "unsafe"):
                envelopes_from_batch(batch_root=root, intake_config=intake_config())

    def test_private_generation_document_enforces_utf8_byte_limit(self):
        with self.assertRaisesRegex(IntakeContractError, "private document limit"):
            build_envelope(
                channel_key="synthetic-channel",
                title="Oversized source",
                publisher="Synthetic Publisher",
                author="Synthetic Author",
                source_url=SOURCE_URL,
                published_at="2026-08-29T01:02:03Z",
                content="x" * (MAX_PRIVATE_DOCUMENT_BYTES + 1),
            )

    def test_removed_source_is_withdrawn_and_does_not_trigger_a_draft(self):
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Removed source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic removed source excerpt.",
            deletion_status="removed",
        )
        self.assertFalse(envelope["triggerDraft"])
        self.assertEqual(envelope["metadata"]["publicationState"], "source_removed")
        self.assertEqual(envelope["sources"][0]["status"], "withdrawn")
        self.assertEqual(envelope["evidence"][0]["status"], "withdrawn")
        self.assertNotIn("privateDocument", envelope)

    def test_same_article_from_both_collectors_has_one_external_identity(self):
        sogou = build_envelope(
            channel_key="synthetic-channel",
            title="Collector overlap",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Sogou rendered text.",
            collection_method="sogou_incremental",
        )
        tikhub = build_envelope(
            channel_key="synthetic-channel",
            title="Collector overlap",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="TikHub rendered text.",
            collection_method="tikhub_backfill",
        )
        self.assertEqual(sogou["externalId"], tikhub["externalId"])
        self.assertEqual(sogou["sources"][0]["url"], tikhub["sources"][0]["url"])
        self.assertEqual(
            sogou["sources"][0]["metadata"]["sourceIdentityHash"],
            tikhub["sources"][0]["metadata"]["sourceIdentityHash"],
        )
        self.assertNotEqual(sogou["idempotencyKey"], tikhub["idempotencyKey"])

    def test_batch_export_uses_sealed_author_and_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "articles" / "synthetic"
            article.mkdir(parents=True)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "articles": [
                            {
                                "article_directory": "articles/synthetic",
                                "article_html_sha256": "b" * 64,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (article / "metadata.json").write_text(
                json.dumps(
                    {
                        "source": "Synthetic Publisher",
                        "title": "Synthetic article",
                        "url": SOURCE_URL,
                        "published_at": "2026-08-29T01:02:03Z",
                        "article_html_sha256": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )
            (article / "content.txt").write_text(
                "Synthetic content for an original GateX scan.", encoding="utf-8"
            )
            envelopes = envelopes_from_batch(
                batch_root=root,
                intake_config=intake_config(),
            )
            self.assertEqual(len(envelopes), 1)
            expected_hash = hashlib.sha256(
                b"Synthetic content for an original GateX scan."
            ).hexdigest()
            self.assertEqual(envelopes[0]["sources"][0]["contentHash"], expected_hash)
            self.assertEqual(envelopes[0]["privateDocument"]["sha256"], expected_hash)
            self.assertEqual(
                envelopes[0]["metadata"]["sourceAuthor"], "Synthetic Author"
            )


class DeliveryTests(unittest.TestCase):
    def test_gatex_delivery_ignores_collector_proxy_environment(self):
        proxy_url = "http://collector-proxy.invalid:8080"
        token = "synthetic-token-value-1234567890"
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "https_proxy": proxy_url,
                "http_proxy": proxy_url,
                "all_proxy": proxy_url,
            },
            clear=False,
        ):
            with patch(
                "urllib.request.getproxies",
                return_value={"https": proxy_url},
            ) as proxy_discovery:
                opener = build_direct_opener()
            proxy_discovery.assert_not_called()

        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxy_handlers, [])
        self.assertNotIn(proxy_url, repr(opener.handlers))
        self.assertNotIn(token, repr(opener.handlers))

    def test_dry_run_never_opens_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "intake.jsonl"
            envelope = build_envelope(
                channel_key="synthetic-channel",
                title="Synthetic source title",
                publisher="Synthetic Publisher",
                author="Synthetic Author",
                source_url=SOURCE_URL,
                published_at="2026-08-29T01:02:03Z",
                content="Synthetic content.",
            )
            write_jsonl(output, [envelope])
            self.assertEqual(deliver_file(output, mode="dry-run"), 1)

    def test_post_sets_idempotency_and_bearer_headers(self):
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic content.",
        )
        captured = []

        class Response:
            status = 202

        def opener(request, timeout):
            captured.append((request, timeout))
            return Response()

        count = deliver_envelopes(
            [envelope],
            endpoint="https://gatex.fund/api/integrations/intelligence/intake",
            token="synthetic-token-value-1234567890",
            opener=opener,
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            captured[0][0].get_header("Idempotency-key"),
            envelope["idempotencyKey"],
        )
        self.assertTrue(
            captured[0][0].get_header("Authorization").startswith("Bearer ")
        )

    def test_dry_run_rejects_worker_contract_mismatches_and_extra_content(self):
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic content.",
        )
        mutations = {
            "language": lambda value: value["topic"].update(language="zh-CN"),
            "access-scope": lambda value: value["topic"].update(accessScope="internal"),
            "provenance": lambda value: value["topic"].update(provenanceType="curated_source"),
            "source-kind": lambda value: value["sources"][0].update(kind="wechat_official_account"),
            "source-status": lambda value: value["sources"][0].update(status="withdrawn"),
            "auto-publish": lambda value: value["metadata"].update(autoPublish=True),
            "full-content": lambda value: value["sources"][0].update(content="not allowed"),
            "missing-private-document": lambda value: value.pop("privateDocument"),
            "private-document-hash": lambda value: value["privateDocument"].update(
                sha256="0" * 64
            ),
            "private-document-mime": lambda value: value["privateDocument"].update(
                mimeType="text/html"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "intake.jsonl"
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    invalid = json.loads(json.dumps(envelope))
                    mutate(invalid)
                    output.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
                    with self.assertRaises(IntakeContractError):
                        deliver_file(output, mode="dry-run")

    def test_post_endpoint_rejects_www_alias(self):
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic content.",
        )
        with self.assertRaisesRegex(IntakeDeliveryError, "not allowed"):
            deliver_envelopes(
                [envelope],
                endpoint="https://www.gatex.fund/api/integrations/intelligence/intake",
                token="synthetic-token-value-1234567890",
            )

    def test_zero_new_post_still_rejects_missing_delivery_configuration(self):
        with self.assertRaisesRegex(IntakeDeliveryError, "credential"):
            deliver_envelopes(
                [],
                endpoint="https://gatex.fund/api/integrations/intelligence/intake",
                token="",
            )
        with self.assertRaisesRegex(IntakeDeliveryError, "not allowed"):
            deliver_envelopes(
                [],
                endpoint="",
                token="synthetic-token-value-1234567890",
            )

    def test_delivery_refuses_redirects(self):
        handler = RejectRedirects()
        redirected = handler.redirect_request(
            object(),
            None,
            302,
            "Found",
            {},
            "https://example.invalid/collector",
        )
        self.assertIsNone(redirected)

        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic content.",
        )

        class RedirectResponse:
            status = 302

        with self.assertRaisesRegex(IntakeDeliveryError, "HTTP 302"):
            deliver_envelopes(
                [envelope],
                endpoint="https://gatex.fund/api/integrations/intelligence/intake",
                token="synthetic-token-value-1234567890",
                opener=lambda request, timeout: RedirectResponse(),
            )

    def test_retry_failure_exposes_only_sanitized_status_and_error_class(self):
        envelope = build_envelope(
            channel_key="synthetic-channel",
            title="Synthetic source title",
            publisher="Synthetic Publisher",
            author="Synthetic Author",
            source_url=SOURCE_URL,
            published_at="2026-08-29T01:02:03Z",
            content="Synthetic content.",
        )
        secret = "synthetic-token-value-1234567890"

        class Response:
            status = 503
            body = f"upstream failure with {secret}"

        def opener(request, timeout):
            return Response()

        with self.assertRaises(IntakeDeliveryError) as raised:
            deliver_envelopes(
                [envelope],
                endpoint="https://gatex.fund/api/integrations/intelligence/intake",
                token=secret,
                attempts=1,
                opener=opener,
            )
        diagnostic = " ".join(raised.exception.diagnostic_fields())
        self.assertEqual(
            diagnostic,
            "category=http-retryable http_status=503 cause_type=HTTPResponse",
        )
        self.assertNotIn(secret, diagnostic)
        self.assertNotIn("upstream failure", diagnostic)

    def test_cli_failure_log_never_prints_secret_or_response_body(self):
        from intelligence_sources import cli

        secret = "synthetic-token-value-1234567890"
        error = IntakeDeliveryError(
            f"upstream response contained {secret}",
            category="http-retryable",
            http_status=503,
            cause_type="HTTPError",
        )
        stderr = io.StringIO()
        with patch("intelligence_sources.cli.deliver_file", side_effect=error):
            with contextlib.redirect_stderr(stderr):
                status = cli.main(
                    [
                        "deliver",
                        "--input",
                        "/does/not/matter.jsonl",
                        "--mode",
                        "post",
                        "--endpoint",
                        "https://gatex.fund/api/integrations/intelligence/intake",
                    ]
                )
        output = stderr.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("error_type=IntakeDeliveryError", output)
        self.assertIn("category=http-retryable", output)
        self.assertIn("http_status=503", output)
        self.assertIn("cause_type=HTTPError", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("upstream response", output)


class TikHubBackfillTests(unittest.TestCase):
    def test_profile_requires_exact_username_and_display_name(self):
        requests = []

        class Transport:
            def post(self, endpoint, body):
                requests.append((endpoint, body))
                return {
                    "code": 200,
                    "data": {
                        "user_name": "gh_synthetic123",
                        "nick_name": "Different Publisher",
                    },
                }

        with self.assertRaisesRegex(BackfillError, "identity"):
            verify_profile(Transport(), "gh_synthetic123", "Synthetic Publisher")
        self.assertFalse(requests[0][1]["raw"])

    def test_article_page_uses_tikhub_minimum_page_size(self):
        requests = []

        class Transport:
            def post(self, endpoint, body):
                requests.append((endpoint, body))
                return {
                    "code": 200,
                    "data": {
                        "biz_username": "gh_synthetic123",
                        "articles": [],
                        "next_offset": "cursor-2",
                        "is_end": True,
                    },
                }

        candidates, next_offset, is_end = fetch_page(
            Transport(),
            username="gh_synthetic123",
            offset="cursor-1",
            page_size=1,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(next_offset, "cursor-2")
        self.assertTrue(is_end)
        self.assertEqual(requests[0][1]["page_size"], 10)

    def test_page_and_detail_become_original_scan_envelope(self):
        page = {
            "articles": [
                {
                    "appMsg": {
                        "baseInfo": {"createTime": 1780000000},
                        "detailInfo": [
                            {
                                "title": "Synthetic history item",
                                "contentUrl": SOURCE_URL,
                                "digest": "Synthetic digest.",
                                "sendTime": 1780000001,
                            }
                        ],
                    }
                }
            ]
        }
        candidates = candidates_from_page(page)
        self.assertEqual(len(candidates), 1)
        detail = {
            "data": {
                "content": {
                    "user_name": "gh_synthetic123",
                    "nick_name": "Synthetic Publisher",
                    "alias": "synthetic_alias",
                    "author": "Synthetic Author",
                    "title": "Synthetic history item",
                    "content_text": "A source passage used only as a short excerpt.",
                    "content_noencode": "<p>Synthetic source passage.</p>",
                    "create_timestamp": 1780000001,
                    "del_reason_id": 0,
                }
            }
        }
        envelope = envelope_from_detail(
            detail,
            intake_config=intake_config(),
            username="gh_synthetic123",
            candidate=candidates[0],
        )
        self.assertTrue(envelope["triggerDraft"])
        self.assertEqual(
            envelope["sources"][0]["metadata"]["collectionMethod"],
            "tikhub_backfill",
        )
        self.assertEqual(
            envelope["privateDocument"]["sha256"],
            envelope["sources"][0]["contentHash"],
        )

    def test_unverified_profile_fails_before_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = intake_config()
            config["tikhub_username"] = ""
            (root / "config.json").write_text(
                json.dumps({"intelligence_intake": config}), encoding="utf-8"
            )
            (root / "state.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BackfillError, "username"):
                run_backfill_page(
                    config_path=root / "config.json",
                    state_path=root / "state.json",
                    state_out=root / "state.next.json",
                    output_path=root / "intake.jsonl",
                    token="synthetic-token-value-1234567890",
                    maximum_items=10,
                )

    def test_removed_tikhub_item_needs_no_full_document(self):
        candidate = BackfillCandidate(
            title="Removed history item",
            source_url=SOURCE_URL,
            digest="Last available source digest.",
            published_at=1780000001,
        )
        detail = {
            "data": {
                "content": {
                    "user_name": "gh_synthetic123",
                    "nick_name": "Synthetic Publisher",
                    "alias": "synthetic_alias",
                    "author": "Synthetic Author",
                    "title": "Removed history item",
                    "create_timestamp": 1780000001,
                    "del_reason_id": 1,
                }
            }
        }
        envelope = envelope_from_detail(
            detail,
            intake_config=intake_config(),
            username="gh_synthetic123",
            candidate=candidate,
        )
        self.assertFalse(envelope["triggerDraft"])
        self.assertEqual(envelope["sources"][0]["status"], "withdrawn")
        self.assertNotIn("privateDocument", envelope)

    def test_disabled_candidate_profile_fails_before_transport(self):
        class Transport:
            def post(self, endpoint, body):
                raise AssertionError("disabled profile opened a network transport")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = intake_config()
            config["enabled"] = False
            config["verification_status"] = "pending_tikhub_profile"
            config["tikhub_username"] = "gh_candidate123"
            (root / "config.json").write_text(
                json.dumps({"intelligence_intake": config}), encoding="utf-8"
            )
            (root / "state.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BackfillError, "disabled"):
                run_backfill_page(
                    config_path=root / "config.json",
                    state_path=root / "state.json",
                    state_out=root / "state.next.json",
                    output_path=root / "intake.jsonl",
                    token="synthetic-token-value-1234567890",
                    maximum_items=10,
                    transport=Transport(),
                )

    def test_pending_page_preserves_next_cursor_across_resumed_runs(self):
        second_url = SOURCE_URL.replace("mid=100", "mid=101")
        candidates = [
            BackfillCandidate(
                title="Synthetic history item one",
                source_url=SOURCE_URL,
                digest="First digest.",
                published_at=1780000001,
            ),
            BackfillCandidate(
                title="Synthetic history item two",
                source_url=second_url,
                digest="Second digest.",
                published_at=1780000002,
            ),
        ]

        class Transport:
            def post(self, endpoint, body):
                if endpoint == PROFILE_ENDPOINT:
                    return {
                        "code": 200,
                        "data": {
                            "user_name": "gh_synthetic123",
                            "nick_name": "Synthetic Publisher",
                        },
                    }
                if endpoint != DETAIL_ENDPOINT:
                    raise AssertionError(f"unexpected endpoint: {endpoint}")
                candidate_url = body["url"]
                candidate = next(item for item in candidates if item.source_url == candidate_url)
                return {
                    "code": 200,
                    "data": {
                        "content": {
                            "user_name": "gh_synthetic123",
                            "nick_name": "Synthetic Publisher",
                            "alias": "synthetic_alias",
                            "author": "Synthetic Author",
                            "title": candidate.title,
                            "content_text": f"Source excerpt for {candidate.title}.",
                            "create_timestamp": candidate.published_at,
                            "del_reason_id": 0,
                        }
                    },
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text(
                json.dumps({"intelligence_intake": intake_config()}), encoding="utf-8"
            )
            (root / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "offset": "cursor-1",
                        "is_end": False,
                        "pending": [item.as_dict() for item in candidates],
                        "pending_next_offset": "cursor-2",
                        "pending_is_end": True,
                        "seen": [],
                    }
                ),
                encoding="utf-8",
            )
            first_count = run_backfill_page(
                config_path=root / "config.json",
                state_path=root / "state.json",
                state_out=root / "state.next.json",
                output_path=root / "intake-one.jsonl",
                token="synthetic-token-value-1234567890",
                maximum_items=1,
                transport=Transport(),
            )
            first_state = json.loads((root / "state.next.json").read_text(encoding="utf-8"))
            self.assertEqual(first_count, 1)
            self.assertEqual(first_state["offset"], "cursor-1")
            self.assertEqual(first_state["pending_next_offset"], "cursor-2")
            self.assertEqual(len(first_state["pending"]), 1)

            second_count = run_backfill_page(
                config_path=root / "config.json",
                state_path=root / "state.next.json",
                state_out=root / "state.done.json",
                output_path=root / "intake-two.jsonl",
                token="synthetic-token-value-1234567890",
                maximum_items=1,
                transport=Transport(),
            )
            final_state = json.loads((root / "state.done.json").read_text(encoding="utf-8"))
            self.assertEqual(second_count, 1)
            self.assertEqual(final_state["offset"], "cursor-2")
            self.assertTrue(final_state["is_end"])
            self.assertEqual(final_state["pending"], [])


if __name__ == "__main__":
    unittest.main()
