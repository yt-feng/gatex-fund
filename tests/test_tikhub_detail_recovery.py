from __future__ import annotations

import io
import json
import unittest
import urllib.error
from types import SimpleNamespace

from intelligence_sources.tikhub_backfill import (
    BackfillCandidate, BackfillError, TikHubHTTPError, TikHubTransport,
    DETAIL_ENDPOINT, FALLBACK_DETAIL_ENDPOINT, fetch_detail,
)


class ArticleDetailRecoveryTests(unittest.TestCase):
    candidate = BackfillCandidate('Synthetic article',
        'https://mp.weixin.qq.com/s?__biz=synthetic-biz&mid=100&idx=1&sn=signature',
        'Synthetic excerpt', 1780000000)

    def test_rejected_h5_article_uses_native_detail_once_with_identical_source(self):
        payload = {'code': 200, 'data': {'content': {
            'content_text': 'The complete source text.',
            'content_noencode': '<p>The complete source text.</p>',
        }}}
        for status in (400, 404, 408, 429, 500, 502, 503, 504):
            calls = []
            def opener(request, timeout):
                calls.append((request.full_url, json.loads(request.data)))
                if len(calls) == 1:
                    raise urllib.error.HTTPError(request.full_url, status, 'Rejected', {}, io.BytesIO())
                return SimpleNamespace(status=200, read=lambda maximum: json.dumps(payload).encode())
            with self.subTest(status=status):
                transport = TikHubTransport('synthetic-credential-value-1234567890', opener=opener)
                self.assertEqual(fetch_detail(transport, self.candidate), payload)
                self.assertEqual([url for url, _ in calls],
                    ['https://api.tikhub.io' + DETAIL_ENDPOINT,
                     'https://api.tikhub.io' + FALLBACK_DETAIL_ENDPOINT])
                self.assertEqual(calls[0][1], calls[1][1])
                self.assertEqual(calls[0][1], {'url': self.candidate.source_url, 'raw': True})
                self.assertEqual(transport.calls, 2)

    def test_credentials_balance_and_invalid_request_fail_without_fallback(self):
        for status in (401, 402, 403, 422):
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, 'Rejected', {}, io.BytesIO())
            with self.subTest(status=status):
                transport = TikHubTransport('synthetic-credential-value-1234567890', opener=opener)
                with self.assertRaises(TikHubHTTPError) as raised:
                    fetch_detail(transport, self.candidate)
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(transport.calls, 1)

    def test_second_failure_propagates_after_two_requests(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            return SimpleNamespace(status=400 if len(calls) == 1 else 503, read=lambda maximum: b'{}')
        transport = TikHubTransport('synthetic-credential-value-1234567890', opener=opener)
        with self.assertRaises(TikHubHTTPError) as raised:
            fetch_detail(transport, self.candidate)
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(len(calls), 2)

    def test_fallback_respects_existing_request_budget(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, 'Rejected', {}, io.BytesIO())
        transport = TikHubTransport('synthetic-credential-value-1234567890', max_calls=1, opener=opener)
        with self.assertRaisesRegex(BackfillError, 'call budget is exhausted'):
            fetch_detail(transport, self.candidate)
        self.assertEqual(transport.calls, 1)


if __name__ == '__main__':
    unittest.main()
