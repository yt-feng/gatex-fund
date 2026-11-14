from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from intelligence_sources.source_text import source_lines
from intelligence_sources import tikhub_backfill as api


class CanonicalSourceTests(unittest.TestCase):
    def test_url_removes_tracking_and_http_without_inventing_signature(self):
        url='http://mp.weixin.qq.com/s?__biz=fixture%3D%3D&amp;mid=100&amp;idx=2&amp;sn=signature&amp;sessionid=old&amp;scene=1#rd'
        normalized=api.canonical_article_url(url)
        self.assertTrue(normalized.startswith('https://mp.weixin.qq.com/s?'))
        self.assertEqual(parse_qs(urlsplit(normalized).query),{'__biz':['fixture=='],'mid':['100'],'idx':['2'],'sn':['signature']})
        self.assertNotIn('sn=',api.canonical_article_url('https://mp.weixin.qq.com/s?__biz=fixture&mid=100&idx=1'))
        self.assertEqual(api.canonical_article_url('http://mp.weixin.qq.com/s/abc_123?scene=1#rd'),'https://mp.weixin.qq.com/s/abc_123')

    def test_ambiguous_or_untrusted_article_identity_is_rejected(self):
        for url in ('https://example.com/s?mid=1','https://mp.weixin.qq.com/s?__biz=x&mid=1&mid=2&idx=1',
                'https://user@mp.weixin.qq.com/s/abc','https://mp.weixin.qq.com/s?__biz=x&mid=no&idx=1'):
            with self.subTest(url=url),self.assertRaises(api.BackfillError): api.canonical_article_url(url)

    def test_html_boundaries_keep_original_words_and_spaces(self):
        text='An AI Agent is here. The last complete paragraph.'
        result=source_lines(text,'<p>An <span>AI</span> Agent is here.</p><p>The last complete paragraph.</p>')
        self.assertEqual(result,['An AI Agent is here.','The last complete paragraph.'])
        self.assertEqual(''.join(''.join(result).split()),''.join(text.split()))

    def test_shorter_html_cannot_replace_full_text(self):
        self.assertEqual(source_lines('Entire first and last passage.','<p>Entire first.</p>'),['Entire first and last passage.'])
        self.assertEqual(source_lines('first\n\nlast\n\n'),['first','','last'])

    def test_long_flat_text_can_be_split_without_omitting_characters(self):
        text=('word '*250)+'\u3002'+('other '*250)+'\u3002'
        lines=source_lines(text)
        self.assertGreater(len(lines),1)
        self.assertEqual(''.join(lines),text)


class BackfillQueueTests(unittest.TestCase):
    def test_unavailable_candidate_does_not_pin_page_or_discard_later_record(self):
        class Transport:
            calls=0
            max_calls=100
            sleeper=staticmethod(lambda seconds:None)
            failed=True
            details=[]
            def post(self,endpoint,body):
                self.calls+=1
                if endpoint==api.PROFILE_ENDPOINT:
                    return {'code':200,'data':{'user_name':'gh_fixture123','nick_name':'Fixture'}}
                if endpoint==api.ARTICLES_ENDPOINT:
                    mids=[100,200] if body['offset']=='' else [300]
                    return {'code':200,'data':{'biz_username':'gh_fixture123','next_offset':'next' if body['offset']=='' else '',
                        'is_end':body['offset']!='','articles':[{'appMsg':{'detailInfo':[
                            {'title':'Fixture','sendTime':1780000000,'contentUrl':f'http://mp.weixin.qq.com/s?__biz=fixture&mid={mid}&idx=1&sn=s&sessionid=x'} for mid in mids]}}]}}
                mid=parse_qs(urlsplit(body['url']).query)['mid'][0]
                self.details.append(mid)
                if mid=='100' and self.failed: raise api.TikHubHTTPError(400)
                return {'code':200,'data':{'content':{'user_name':'gh_fixture123','nick_name':'Fixture','alias':'fixture',
                    'mid':mid,'idx':1,'bizuin':'fixture','title':'Fixture','create_timestamp':1780000000,
                    'content_text':'First. Last.','content_noencode':'<p>First.</p><p>Last.</p>','link':body['url']}}}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            config={'intelligence_intake':{'enabled':True,'verification_status':'verified','tikhub_username':'gh_fixture123','publisher':'Fixture','wechat_alias':'fixture'},'provider':{'expected_biz':'fixture'}}
            (root/'config').write_text(json.dumps(config));(root/'state').write_text('{}')
            def run(transport):
                count=api.run_technology_backfill_page(config_path=root/'config',state_path=root/'state',state_out=root/'next',output_path=root/'records',token='unused',maximum_items=2,transport=transport)
                state=json.loads((root/'next').read_text());(root/'state').write_text(json.dumps(state))
                return count,state
            first=Transport();count,state=run(first)
            self.assertEqual(count,1);self.assertEqual(state['offset'],'next')
            self.assertEqual(len(state['pending']),1);self.assertEqual(state['last_run']['deferred_count'],1)
            second=Transport();second.details=[];count,state=run(second)
            self.assertEqual(count,1);self.assertNotIn('100',second.details)
            self.assertTrue(state['is_end']);self.assertEqual(len(state['pending']),1)
            class Future(datetime):
                @classmethod
                def now(cls,tz=None):return datetime.now(tz)+timedelta(hours=1)
            third=Transport();third.failed=False;third.details=[]
            with patch.object(api,'datetime',Future):count,state=run(third)
            self.assertEqual(count,1);self.assertEqual(third.details,['100'])
            self.assertEqual(state['pending'],[]);self.assertEqual(state['retry_state'],{})
            self.assertEqual(len(state['seen_documents']),3)
