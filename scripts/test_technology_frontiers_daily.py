import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile, json

spec = importlib.util.spec_from_file_location('daily', Path(__file__).with_name('technology_frontiers_daily.py'))
daily = importlib.util.module_from_spec(spec); spec.loader.exec_module(daily)

class DailyEditionTests(unittest.TestCase):
    def test_document_identity_does_not_invent_signature(self):
        source = daily.source_from_metadata({'title':'Sample','source':'Example','published_at':'2026-09-12',
            'document_identity':{'__biz':'sample-biz','mid':'123','idx':'1'}}, 'first\n\nlast')
        self.assertEqual(source['lines'], ['first','','last'])
        self.assertEqual(source['sourceUrl'], '')
        self.assertNotIn('sn', source['documentIdentity'])

    def test_wrong_verified_source_is_skipped(self):
        with patch.dict(daily.os.environ, {'GATEX_TECHNOLOGY_SOURCE_BIZ_SHA256':daily.digest('approved')}):
            source = daily.source_from_metadata({'document_identity':{'__biz':'other','mid':'123','idx':'1'}},'body')
        self.assertIsNone(source)

    def test_complete_ordered_coverage_including_blank_line(self):
        blocks = [{'type':'paragraph','sourceLines':[1,1],'text':'First'},
            {'type':'divider','sourceLines':[2,2],'text':''}, {'type':'paragraph','sourceLines':[3,3],'text':'Last'}]
        self.assertEqual(daily.validate_blocks(blocks,3),blocks)
        for broken in [blocks[:2], [blocks[0],blocks[0],blocks[2]],list(reversed(blocks))]:
            with self.assertRaises(ValueError): daily.validate_blocks(broken,3)

    def test_truncated_response_rejected(self):
        with self.assertRaises(ValueError): daily.parse_model_json({'choices':[{'finish_reason':'length','message':{'content':'{}'}}]})

    def test_existing_translation_reused_without_model_or_writes(self):
        saved = {'title':'Title','blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Body'}]}
        with patch.object(daily,'model_call',side_effect=AssertionError('must not call')), patch.object(daily,'api',side_effect=AssertionError('must not write')):
            self.assertEqual(daily.translate({'lines':['source'], 'progress':{'translation':saved}}),saved)

    def test_partial_translation_resumes_after_last_saved_line(self):
        saved=[{'type':'paragraph','sourceLines':[1,1],'text':'First'}]
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'type':'paragraph','sourceLines':[2,2],'text':'Second'}]},
            {'title':'A title','listingDescription':'Description.','artDirection':'A visual'}]) as model, patch.object(daily,'api',return_value={'ok':True}) as api:
            result=daily.translate({'id':'sample','title':'Title','lines':['first','second'],'progress':{'translationBlocks':saved}})
        self.assertEqual(model.call_args_list[0].args[1]['lines'],[{'line':2,'text':'second'}])
        self.assertEqual(len(result['blocks']),2)
        self.assertEqual(api.call_count,2)

    def test_model_heading_whitespace_is_normalized_before_paid_cover(self):
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Body'}]},
            {'title':'  A title\n','listingDescription':' Description. ','artDirection':' A visual\n  '}]), patch.object(daily,'api',return_value={'ok':True}):
            result=daily.translate({'id':'sample','title':'Title','lines':['source']})
        self.assertEqual(result['title'],'A title')
        self.assertEqual(result['listingDescription'],'Description.')
        self.assertEqual(result['artDirection'],'A visual')

    def test_new_source_after_failed_lexical_page_is_not_starved(self):
        pages = [{'sources':[{'id':'aaa-old-failed','publishedAt':'2026-08-01'}], 'cursor':'opaque/next'},
                 {'sources':[{'id':'zzz-new','publishedAt':'2026-09-12'}]}]
        with tempfile.TemporaryDirectory() as temp, patch.object(daily,'api',side_effect=pages) as api, patch.object(daily,'produce') as produce:
            self.assertEqual(daily.publish_pending(1, Path(temp)), 1)
        self.assertEqual(api.call_count, 2)
        self.assertIn('cursor=opaque%2Fnext', api.call_args_list[1].args[0])
        self.assertEqual(produce.call_args.args[0]['id'],'zzz-new')

    def test_failure_does_not_prevent_later_success(self):
        rows=[{'id':'new-failed','publishedAt':'2026-09-12'}, {'id':'old-valid','publishedAt':'2026-09-11'}]
        with tempfile.TemporaryDirectory() as temp, patch.object(daily,'api',return_value={'sources':rows}), patch.object(daily,'produce',side_effect=[RuntimeError('test'), {}]) as produce:
            with self.assertRaises(RuntimeError): daily.publish_pending(2,Path(temp))
        self.assertEqual(produce.call_count,2)

    def test_http_failure_logging_exposes_only_controlled_status_and_scope(self):
        import io
        auth=daily.HTTPError('https://example.test',403,'Denied',{'content-type':'application/json'},io.BytesIO(b'{"error":"Intelligence intake credentials are not valid."}'))
        edge=daily.HTTPError('https://example.test',403,'Denied',{'content-type':'text/html'},io.BytesIO(b'<html>private diagnostic</html>'))
        self.assertEqual(daily.failure_status(daily.service_failure(auth)),' http_status=403 failure_scope=queue-auth')
        self.assertEqual(daily.failure_status(daily.service_failure(edge)),' http_status=403 failure_scope=edge')

    def test_dedicated_credential_takes_precedence_over_legacy_intake(self):
        with patch.dict(daily.os.environ, {'GATEX_TECHNOLOGY_PUBLICATION_SECRET':' dedicated-fixture ', 'GATEX_INTELLIGENCE_INTAKE_SECRET':'legacy-fixture'}), patch.object(daily,'request_json',return_value={'ok':True}) as request:
            daily.api('/pending')
        self.assertEqual(request.call_args.args[1], 'dedicated-fixture')

    def test_batch_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'manifest.json').write_text(json.dumps({'articles':[{'article_directory':'../escape'}]}))
            with self.assertRaises(ValueError): daily.enqueue_batch(root)

if __name__=='__main__': unittest.main()
