import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile, json

spec = importlib.util.spec_from_file_location('daily', Path(__file__).with_name('technology_frontiers_daily.py'))
daily = importlib.util.module_from_spec(spec); spec.loader.exec_module(daily)

class DailyEditionTests(unittest.TestCase):
    def test_cover_prompt_prefers_metaphoric_objects_without_people(self):
        prompt = daily.cover_prompt({'artDirection': 'A path through changing systems'})
        self.assertIn('No people, faces, heads, hands, human silhouettes, portraits, or human figures.', prompt)
        self.assertIn('Prefer objects, architecture, landscapes, instruments, machines', prompt)
        self.assertIn('No text, typography, letters, numbers, logos', prompt)

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

    def test_malformed_model_envelope_is_a_validation_failure(self):
        responses=[None, [], {'choices':None}, {'choices':{}}, {'choices':[]}]
        responses += [{'choices':[choice]} for choice in (None, [], 'choice', 1)]
        responses += [{'choices':[{'message':message}]} for message in (None, [], {'content':{}}, {'content':'[]'})]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                daily.parse_model_json(response)

    def test_malformed_translated_blocks_are_validation_failures(self):
        valid={'type':'paragraph','sourceLines':[1,1],'text':'Complete text.'}
        malformed=[None, {}, 'blocks', [None], [[]], ['block']]
        malformed += [[{**valid,'type':value}] for value in (None, [], {})]
        malformed += [[{**valid,'text':value}] for value in (None, [], {}, 1, True)]
        malformed += [[{**valid,'sourceLines':value}] for value in (None, '1,1', [1], [True,1], [1,1.0], [0,1], [1,2])]
        malformed += [[{'type':'divider','sourceLines':[1,1],'text':None}]]
        for blocks in malformed:
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                daily.validate_blocks(blocks,1)

    def test_existing_translation_reused_without_model_or_writes(self):
        saved = {'title':'Title','reviewVersion':'faithful-v1','blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Body'}]}
        with patch.object(daily,'model_call',side_effect=AssertionError('must not call')), patch.object(daily,'api',side_effect=AssertionError('must not write')):
            self.assertEqual(daily.translate({'lines':['source'], 'progress':{'translation':saved}}),saved)

    def test_partial_translation_resumes_after_last_saved_line(self):
        saved=[{'type':'paragraph','sourceLines':[1,1],'text':'First'}]
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'type':'paragraph','sourceLines':[2,2],'text':'Second'}]},
            {'title':'A title','listingDescription':'Description.','artDirection':'A visual'}]) as model, patch.object(daily,'api',return_value={'ok':True}) as api, patch.object(daily,'review_translation',side_effect=lambda source,draft:draft):
            result=daily.translate({'id':'sample','title':'Title','lines':['first','second'],'progress':{'translationBlocks':saved}})
        self.assertEqual(model.call_args_list[0].args[1]['lines'],[{'line':2,'text':'second'}])
        self.assertEqual(len(result['blocks']),2)
        self.assertEqual(api.call_count,1)

    def test_model_heading_whitespace_is_normalized_before_paid_cover(self):
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Body'}]},
            {'title':'  A title\n','listingDescription':' Description. ','artDirection':' A visual\n  '}]), patch.object(daily,'api',return_value={'ok':True}), patch.object(daily,'review_translation',side_effect=lambda source,draft:draft):
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

    def test_machine_requests_identify_the_publisher_and_accept_json(self):
        from unittest.mock import MagicMock
        response=MagicMock(); response.__enter__.return_value.read.return_value=b'{"ok":true}'
        with patch.object(daily,'urlopen',return_value=response) as request:
            self.assertTrue(daily.request_json('https://example.test','fixture')['ok'])
        headers=request.call_args.args[0]
        self.assertEqual(headers.get_header('User-agent'),daily.CLIENT_NAME)
        self.assertEqual(headers.get_header('Accept'),'application/json')

    def test_plain_cloudflare_code_is_classified_without_body_disclosure(self):
        import io
        error=daily.HTTPError('https://example.test',403,'Denied',{'content-type':'text/plain','server':'cloudflare'},io.BytesIO(b'error code: 1010'))
        self.assertEqual(daily.failure_status(daily.service_failure(error)),' http_status=403 failure_scope=edge edge_code=1010')

    def test_model_explicitly_requests_nonstreaming_json(self):
        with patch.dict(daily.os.environ, {'GATEX_MODEL_CREDENTIAL':'fixture'}), patch.object(daily,'request_json',return_value={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}) as request:
            self.assertEqual(daily.model_call('Return JSON',{'example':True}),{'ok':True})
        self.assertIs(request.call_args.args[2]['stream'],False)

    def test_publisher_label_is_canonical_only_after_approved_identity_match(self):
        with patch.dict(daily.os.environ, {'GATEX_TECHNOLOGY_SOURCE_BIZ_SHA256':daily.digest('approved')}):
            source=daily.source_from_metadata({'title':'Example','source':'source-a','published_at':'2026-09-12','document_identity':{'__biz':'approved','mid':'123','idx':'1'}},'body')
        self.assertEqual(source['sourceName'],'Unsolved Problems')

    def test_documented_data_wrapper_is_supported(self):
        self.assertEqual(daily.parse_model_json({'data':{'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}}),{'ok':True})

    def test_ornamental_separator_is_preserved_but_text_cannot_become_divider(self):
        block={'type':'divider','sourceLines':[1,1],'text':''}
        self.assertEqual(daily.validate_blocks([block],1,source_lines=['\u2014\u2014'])[0]['text'],'--')
        self.assertEqual(block['type'],'note')
        with self.assertRaises(ValueError): daily.validate_blocks([{'type':'divider','sourceLines':[1,1],'text':''}],1,source_lines=['Meaningful source text'])

    def test_invalid_structure_retries_identical_source_lines(self):
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'type':'quote','sourceLines':[1,1],'text':'Body'}]},
            {'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Body'}]},
            {'title':'Title','listingDescription':'Description.','artDirection':'Visual'}]) as model, patch.object(daily,'api',return_value={'ok':True}), patch.object(daily,'review_translation',side_effect=lambda source,draft:draft):
            result=daily.translate({'id':'sample','title':'Title','lines':['source']})
        self.assertEqual(model.call_args_list[0].args[1],model.call_args_list[1].args[1])
        self.assertEqual(len(result['blocks']),1)

    def test_translation_recovers_malformed_blocks_before_checkpointing(self):
        valid={'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Complete translation.'}]}
        invalid_responses=[{'blocks':None}, {'blocks':{}}, {'blocks':[None]},
            {'blocks':[{'type':[],'sourceLines':[1,1],'text':'Body'}]},
            {'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':None}]}]
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid), patch.object(daily,'model_call',side_effect=[invalid,valid,
                {'title':'Title','listingDescription':'Description.','artDirection':'Visual'}]) as model, \
                patch.object(daily,'api',return_value={'ok':True}) as api, \
                patch.object(daily,'review_translation',side_effect=lambda source,draft:draft):
                result=daily.translate({'id':'sample','title':'Source','lines':['source']})
            self.assertEqual(model.call_count,3)
            self.assertEqual(model.call_args_list[0].args[1],model.call_args_list[1].args[1])
            self.assertEqual(result['blocks'],valid['blocks'])
            api.assert_called_once_with('/sources/sample/progress',{'translationBlocks':valid['blocks']})

    def test_translation_heading_recovers_without_retranslating_saved_body(self):
        blocks=[{'type':'paragraph','sourceLines':[1,1],'text':'Complete translation.'}]
        valid={'title':'  Clear title  ','listingDescription':'  Factual description.  ','artDirection':'  Existing concept  '}
        invalid_responses=[ValueError('Model content is not valid JSON'), {},
            {**valid,'artDirection':' '}, {**valid,'title':'T'*201},
            {**valid,'listingDescription':'D'*181}, {**valid,'title':'\u4e2d\u6587'},
            {**valid,'listingDescription':'Untranslated \u4e2d\u6587'}]
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid), patch.object(daily,'model_call',side_effect=[invalid,valid]) as model, \
                patch.object(daily,'api') as api, patch.object(daily,'review_translation',side_effect=lambda source,draft:draft) as review:
                result=daily.translate({'id':'sample','title':'Source','lines':['source'],
                    'progress':{'translationBlocks':blocks}})
            self.assertEqual(model.call_count,2)
            self.assertEqual(model.call_args_list[0].args[1],model.call_args_list[1].args[1])
            self.assertEqual(result,{'title':'Clear title','listingDescription':'Factual description.',
                'artDirection':'Existing concept','blocks':blocks})
            api.assert_not_called()
            review.assert_called_once()

    def test_review_heading_recovers_without_repeating_review_or_changing_art(self):
        blocks=[{'type':'paragraph','sourceLines':[1,1],'text':'Original translation.'}]
        source={'id':'sample','title':'Source','lines':['source']}
        draft={'title':'Draft','listingDescription':'Draft summary.','artDirection':'Existing concept','blocks':blocks}
        edits={'blocks':[{'blockId':1,'text':'Corrected complete translation.'}]}
        valid={'title':'  Clear title  ','listingDescription':'  Factual description.  '}
        invalid_responses=[ValueError('Model content is not valid JSON'), {}, {**valid,'title':None},
            {**valid,'title':'T'*201}, {**valid,'listingDescription':'D'*181},
            {**valid,'title':'\u4e2d\u6587'}, {**valid,'listingDescription':'Untranslated \u4e2d\u6587'}]
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid), patch.object(daily,'model_call',side_effect=[edits,invalid,valid]) as model, \
                patch.object(daily,'api',return_value={'ok':True}) as api:
                result=daily.review_translation(source,draft)
            self.assertEqual(model.call_count,3)
            self.assertEqual(model.call_args_list[1].args[1],model.call_args_list[2].args[1])
            self.assertEqual(result['blocks'][0]['text'],'Corrected complete translation.')
            self.assertEqual(result['title'],'Clear title')
            self.assertEqual(result['listingDescription'],'Factual description.')
            self.assertEqual(result['artDirection'],'Existing concept')
            self.assertEqual(result['reviewVersion'],'faithful-v1')
            self.assertEqual(blocks[0]['text'],'Original translation.')
            api.assert_called_once_with('/sources/sample/progress',{'translation':result})

    def test_heading_retry_exhaustion_preserves_progress_without_accepting_invalid_output(self):
        source={'id':'sample','title':'Source','lines':['source']}
        blocks=[{'type':'paragraph','sourceLines':[1,1],'text':'Complete translation.'}]
        draft={'title':'Draft','listingDescription':'Draft summary.','artDirection':'Existing concept','blocks':blocks}
        with patch.object(daily,'model_call',side_effect=[{'blocks':blocks},{},{},{}]) as model, \
            patch.object(daily,'api',return_value={'ok':True}) as api, patch.object(daily,'review_translation') as review:
            with self.assertRaisesRegex(ValueError,'Missing edition heading'): daily.translate(source)
        self.assertEqual(model.call_count,4)
        self.assertTrue(all('translatedArticle' in call.args[1] for call in model.call_args_list[1:]))
        api.assert_called_once_with('/sources/sample/progress',{'translationBlocks':blocks})
        review.assert_not_called()
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'blockId':1,'text':'Reviewed translation.'}]},{},{},{}]) as model, \
            patch.object(daily,'api') as api:
            with self.assertRaisesRegex(ValueError,'Missing edition heading'): daily.review_translation(source,draft)
        self.assertEqual(model.call_count,4)
        self.assertTrue(all('draftTitle' in call.args[1] for call in model.call_args_list[1:]))
        api.assert_not_called()
        self.assertNotIn('reviewVersion',draft)
        self.assertEqual(draft['blocks'],blocks)
        self.assertEqual(draft['blocks'][0]['text'],'Complete translation.')

    def test_model_content_retries_do_not_repeat_service_or_programming_failures(self):
        errors=[RuntimeError('Credential unavailable'), daily.ServiceFailure(403,'service'),
            TypeError('Programming failure'), AttributeError('Programming failure'),
            ValueError('Unexpected validator failure'), json.JSONDecodeError('Service JSON failure','{',0)]
        for error in errors:
            for failure_source in ('model','validator'):
                with self.subTest(error=type(error).__name__,failure_source=failure_source), \
                    patch.object(daily,'model_call',side_effect=error if failure_source=='model' else None,return_value={}) as model:
                    def validator(value):
                        if failure_source=='validator': raise error
                        return value
                    with self.assertRaises(type(error)) as raised:
                        daily.validated_model_call('Return JSON',{'source':'unchanged'},validator,'test-validation')
                self.assertIs(raised.exception,error)
                model.assert_called_once()
        source={'id':'sample','title':'Source','lines':['first','second']}
        draft={'title':'Draft','listingDescription':'Summary.','artDirection':'Existing concept','blocks':[
            {'type':'paragraph','sourceLines':[1,1],'text':'First translation.'},
            {'type':'paragraph','sourceLines':[2,2],'text':'Second translation.'}]}
        for error in errors[-2:]:
            for phase,run in [('translation',lambda:daily.translate(source)),
                ('review',lambda:daily.review_translation(source,draft))]:
                with self.subTest(error=type(error).__name__,phase=phase), \
                    patch.object(daily,'model_call',side_effect=error) as model, patch.object(daily,'api') as api:
                    with self.assertRaises(type(error)) as raised: run()
                self.assertIs(raised.exception,error)
                model.assert_called_once()
                api.assert_not_called()

    def test_review_recovers_model_envelope_and_json_failures_on_identical_blocks(self):
        source={'id':'sample','title':'Source','lines':['source']}
        draft={'title':'Draft','listingDescription':'Summary.','artDirection':'Existing concept',
            'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Original translation.'}]}
        def envelope(content): return {'choices':[{'message':{'content':content},'finish_reason':'stop'}]}
        responses=[{'choices':[None]}, envelope('{broken JSON'),
            envelope(json.dumps({'blocks':[{'blockId':1,'text':'Corrected translation.'}]})),
            envelope(json.dumps({'title':'Clear title','listingDescription':'Factual description.'}))]
        with patch.dict(daily.os.environ,{'APIMART_API_KEY':'fixture'}), \
            patch.object(daily,'request_json',side_effect=responses) as request, patch.object(daily,'api',return_value={'ok':True}) as api:
            result=daily.review_translation(source,draft)
        self.assertEqual(request.call_count,4)
        first_payload=request.call_args_list[0].args[2]['messages'][1]['content']
        self.assertTrue(all(call.args[2]['messages'][1]['content']==first_payload for call in request.call_args_list[:3]))
        self.assertEqual(result['blocks'][0]['text'],'Corrected translation.')
        api.assert_called_once_with('/sources/sample/progress',{'translation':result})

    def test_source_coverage_failure_splits_chunk_before_giving_up(self):
        invalid = {'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'Only first'}]}
        first = {'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'First'}]}
        second = {'blocks':[{'type':'paragraph','sourceLines':[2,2],'text':'Second'}]}
        with patch.object(daily,'model_call',side_effect=[invalid, invalid, invalid, first, second,
            {'title':'Title','listingDescription':'Description.','artDirection':'Visual'}]) as model, \
            patch.object(daily,'api',return_value={'ok':True}), \
            patch.object(daily,'review_translation',side_effect=lambda source,draft:draft):
            result=daily.translate({'id':'sample','title':'Title','lines':['source one','source two']})
        self.assertEqual([b['sourceLines'] for b in result['blocks']], [[1,1],[2,2]])
        self.assertEqual(model.call_args_list[3].args[1]['lines'],[{'line':1,'text':'source one'}])

    def test_unreviewed_saved_translation_is_corrected_without_retranslation_or_art_changes(self):
        original=[{'type':'paragraph','sourceLines':[1,1],'text':'A persistent discount.'}]
        corrected=[{'type':'paragraph','sourceLines':[1,1],'text':'A discount for underestimated persistence.'}]
        draft={'title':'Old title','listingDescription':'Old summary.','artDirection':'Existing concept','blocks':original}
        source={'id':'sample','title':'Source title','lines':['source'], 'progress':{'translation':draft,'coverTask':{'taskId':'saved-task'}}}
        with patch.object(daily,'model_call',side_effect=[{'blocks':[{'blockId':1,'text':corrected[0]['text']}]},{'title':' Clear title ','listingDescription':'Faithful description.'}]) as model, patch.object(daily,'api',return_value={'ok':True}) as api:
            result=daily.translate(source)
        self.assertEqual(model.call_count,2)
        self.assertEqual(result['blocks'],corrected)
        self.assertEqual(result['reviewVersion'],'faithful-v1')
        self.assertEqual(result['artDirection'],'Existing concept')
        self.assertEqual(source['progress']['coverTask']['taskId'],'saved-task')
        self.assertEqual(api.call_args.args[1]['translation'],result)

    def test_review_cannot_repartition_or_drop_source_blocks(self):
        original=[{'type':'paragraph','sourceLines':[1,1],'text':'First.'},{'type':'paragraph','sourceLines':[2,2],'text':'Second.'}]
        merged={'blocks':[{'type':'paragraph','sourceLines':[1,2],'text':'First. Second.'}]}
        source={'id':'sample','title':'Source','lines':['first','second']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual','blocks':original}
        with patch.object(daily,'model_call',return_value=merged) as model, patch.object(daily,'api') as api:
            with self.assertRaisesRegex(ValueError,'Review changed the source block map'): daily.review_translation(source,draft)
        self.assertEqual(model.call_count,6)
        api.assert_not_called()

    def test_review_preserves_multiline_blocks_and_blank_dividers(self):
        original=[{'type':'heading','sourceLines':[1,2],'text':'Original heading.'},
            {'type':'divider','sourceLines':[3,3],'text':''},
            {'type':'note','sourceLines':[4,4],'text':'Original closing note.'}]
        source={'id':'sample','title':'Source','lines':['first heading line','second heading line','','closing note']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual','blocks':original}
        edits={'blocks':[{'blockId':1,'type':'divider','sourceLines':[1,4],'text':'Complete corrected heading.'},
            {'blockId':2,'text':''},{'blockId':3,'text':'Complete corrected closing note.'}]}
        heading={'title':'Title','listingDescription':'Description.'}
        with patch.object(daily,'model_call',side_effect=[edits,heading]) as model, patch.object(daily,'api',return_value={'ok':True}):
            result=daily.review_translation(source,draft)
        self.assertEqual([(b['type'],b['sourceLines']) for b in result['blocks']],
            [('heading',[1,2]),('divider',[3,3]),('note',[4,4])])
        self.assertEqual([b['text'] for b in result['blocks']], [b['text'] for b in edits['blocks']])
        self.assertEqual(model.call_args_list[0].args[1]['draftBlocks'],
            [{**block,'blockId':index+1} for index,block in enumerate(original)])
        self.assertEqual(model.call_args_list[0].args[1]['sourceLines'],
            [{'line':index+1,'text':line} for index,line in enumerate(source['lines'])])
        self.assertEqual(original[0]['text'],'Original heading.')
        self.assertEqual(original[2]['text'],'Original closing note.')

    def test_review_retries_missing_duplicate_reordered_and_foreign_block_ids(self):
        original=[{'type':'paragraph','sourceLines':[1,1],'text':'First.'},
            {'type':'paragraph','sourceLines':[2,2],'text':'Second.'}]
        source={'id':'sample','title':'Source','lines':['first','second']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual','blocks':original}
        valid={'blocks':[{'blockId':1,'text':'First corrected.'},{'blockId':2,'text':'Second corrected.'}]}
        invalid_responses=[
            {'blocks':[{'blockId':1,'text':'First corrected.'}]},
            {'blocks':[{'text':'First corrected.'},{'blockId':2,'text':'Second corrected.'}]},
            {'blocks':[{'blockId':1,'text':'First corrected.'},{'blockId':1,'text':'Second corrected.'}]},
            {'blocks':[{'blockId':2,'text':'Second corrected.'},{'blockId':1,'text':'First corrected.'}]},
            {'blocks':[{'blockId':1,'text':'First corrected.'},{'blockId':3,'text':'Foreign.'}]},
            {'blocks':[{'blockId':True,'text':'First corrected.'},{'blockId':2,'text':'Second corrected.'}]},
        ]
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid), patch.object(daily,'model_call',side_effect=[invalid,valid,
                {'title':'Title','listingDescription':'Description.'}]) as model, patch.object(daily,'api',return_value={'ok':True}) as api:
                result=daily.review_translation(source,draft)
            self.assertEqual(model.call_count,3)
            self.assertEqual(model.call_args_list[0].args[1],model.call_args_list[1].args[1])
            self.assertEqual([b['text'] for b in result['blocks']],['First corrected.','Second corrected.'])
            self.assertEqual(api.call_count,1)

    def test_review_structure_failure_splits_without_losing_global_block_ids(self):
        original=[{'type':'paragraph','sourceLines':[1,2],'text':'First complete block.'},
            {'type':'note','sourceLines':[3,3],'text':'Closing block.'}]
        source={'id':'sample','title':'Source','lines':['first','continued','closing']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual','blocks':original}
        invalid={'blocks':[{'blockId':1,'text':'Only one block.'}]}
        responses=[invalid,invalid,invalid,{'blocks':[{'blockId':1,'text':'First corrected block.'}]},
            {'blocks':[{'blockId':2,'text':'Corrected closing block.'}]},
            {'title':'Title','listingDescription':'Description.'}]
        with patch.object(daily,'model_call',side_effect=responses) as model, patch.object(daily,'api',return_value={'ok':True}) as api:
            result=daily.review_translation(source,draft)
        self.assertEqual(model.call_count,6)
        self.assertEqual([b['blockId'] for b in model.call_args_list[3].args[1]['draftBlocks']],[1])
        self.assertEqual([b['blockId'] for b in model.call_args_list[4].args[1]['draftBlocks']],[2])
        self.assertEqual(model.call_args_list[3].args[1]['sourceLines'],
            [{'line':1,'text':'first'},{'line':2,'text':'continued'}])
        self.assertEqual(model.call_args_list[4].args[1]['sourceLines'],[{'line':3,'text':'closing'}])
        self.assertEqual([b['sourceLines'] for b in result['blocks']],[[1,2],[3,3]])
        self.assertEqual([b['text'] for b in result['blocks']],['First corrected block.','Corrected closing block.'])
        self.assertEqual(api.call_count,1)

    def test_review_single_block_failure_never_saves_or_requests_heading(self):
        original=[{'type':'paragraph','sourceLines':[1,1],'text':'Original complete translation.'}]
        source={'id':'sample','title':'Source','lines':['meaningful source']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual','blocks':original}
        cases=[
            ({'blocks':[{'blockId':2,'text':'Foreign block.'}]},'Review changed the source block map'),
            ({'blocks':[{'blockId':1,'text':None}]},'Invalid reviewed block text'),
            ({'blocks':[{'blockId':1,'text':''}]},'Translation is empty'),
            ({'blocks':[{'blockId':1,'text':'Untranslated \u4e2d\u6587'}]},'Untranslated body text remains'),
        ]
        for response,message in cases:
            with self.subTest(message=message), patch.object(daily,'model_call',return_value=response) as model, patch.object(daily,'api') as api:
                with self.assertRaisesRegex(ValueError,message): daily.review_translation(source,draft)
            self.assertEqual(model.call_count,3)
            self.assertTrue(all('draftBlocks' in call.args[1] for call in model.call_args_list))
            api.assert_not_called()
            self.assertEqual(original[0]['text'],'Original complete translation.')
            self.assertNotIn('reviewVersion',draft)

    def test_review_rejects_incomplete_original_draft_before_model_or_save(self):
        source={'id':'sample','title':'Source','lines':['first','second']}
        draft={'title':'Title','listingDescription':'Summary','artDirection':'Visual',
            'blocks':[{'type':'paragraph','sourceLines':[1,1],'text':'First only.'}]}
        with patch.object(daily,'model_call') as model, patch.object(daily,'api') as api:
            with self.assertRaisesRegex(ValueError,'Source coverage is incomplete'): daily.review_translation(source,draft)
        model.assert_not_called()
        api.assert_not_called()

    def test_batch_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'manifest.json').write_text(json.dumps({'articles':[{'article_directory':'../escape'}]}))
            with self.assertRaises(ValueError): daily.enqueue_batch(root)

if __name__=='__main__': unittest.main()
