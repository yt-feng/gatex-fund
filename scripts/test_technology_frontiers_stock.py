"""Offline stock/native publication tests; no model calls or Worker writes."""
import hashlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import technology_frontiers_daily as daily
import stock_photo_assets as stock
import build_technology_frontiers as renderer
from reportlab.platypus import Paragraph


def photo(identifier,keywords,**kwargs):
    return {'id':identifier,'src':f'/images/stock/{identifier}.jpg','title':identifier,
        'alt':{'en':identifier,'zh':'\u7167\u7247'},'keywords':keywords,'sourcePage':'https://commons.wikimedia.org/wiki/File:Example.jpg',
        'imageUrl':'https://upload.wikimedia.org/unused.jpg','author':'Original photographer','license':'CC BY 3.0',
        'licenseUrl':'https://creativecommons.org/licenses/by/3.0/','width':1600,'height':1000,**kwargs}


CATALOG=[photo('servers',['data center','AI infrastructure','computing']),photo('finance',['capital markets','finance','investment']),
    photo('solar',['solar','photovoltaic','renewable energy','\u5149\u4f0f']),
    photo('port',['shipping','logistics','Singapore'],locations=['Singapore','\u65b0\u52a0\u5761']),
    photo('wafer',['semiconductor','\u82af\u7247']),photo('hong-kong',['Hong Kong','\u9999\u6e2f'],locations=['Hong Kong','\u9999\u6e2f'])]


def photo_blob():
    image=Image.effect_noise((1600,1000),65).convert('RGB');out=io.BytesIO()
    image.save(out,format='JPEG',quality=90);return out.getvalue()


class StockPublicationTests(unittest.TestCase):
    def test_primary_title_relevance_and_specific_topic_priority(self):
        self.assertIsNone(stock.select_stock_photo('Saudi Arabia crude oil outlook',CATALOG,summary='Solar capacity is a minor comparison'))
        self.assertIsNone(stock.select_stock_photo('Unrelated subject',CATALOG,summary='capital markets'))
        self.assertEqual(stock.select_stock_photo('Data center investment',CATALOG)['id'],'servers')
        self.assertIsNone(stock.select_stock_photo('Data center investment',CATALOG,exclude_ids={'servers'}))
        self.assertEqual(stock.select_stock_photo('Semiconductor finance investment',CATALOG)['id'],'wafer')

    def test_geography_cannot_imply_named_local_facility(self):
        for place in ('Saudi Arabia','China','Dubai','Riyadh','Abu Dhabi','Shanghai','Beijing','Shenzhen','GCC','MENA','\u8fea\u62dc','\u4e0a\u6d77','\u6d77\u5408\u4f1a'):
            with self.subTest(place=place): self.assertIsNone(stock.select_stock_photo(place+' shipping',CATALOG))
        self.assertEqual(stock.select_stock_photo('Singapore shipping',CATALOG)['id'],'port')
        self.assertEqual(stock.select_stock_photo('Shipping',CATALOG)['id'],'port')
        self.assertIsNone(stock.select_stock_photo('Hong Kong medical research',CATALOG))

    def test_remote_catalog_and_local_site_assets_need_no_supplier_key(self):
        self.assertFalse((Path(stock.__file__).resolve().parents[1]/'public/images/stock/catalog.json').exists())
        with patch.object(stock,'_read',return_value=json.dumps({'version':1,'photos':CATALOG}).encode()) as read:
            self.assertEqual(stock.load_catalog(),CATALOG)
        read.assert_called_once_with('https://gatex.fund/images/stock/catalog.json',512*1024)
        with patch.object(stock,'_read',return_value=b'image') as read:
            self.assertEqual(stock.photo_bytes(CATALOG[0]),b'image')
        read.assert_called_once_with('https://gatex.fund/images/stock/servers.jpg',16*1024*1024)
        for src in ('https://supplier.example/image.jpg','/images/stock/../../secret.jpg'):
            with self.assertRaises(ValueError): stock.photo_bytes({**CATALOG[0],'src':src})

    def test_stock_receipt_binds_canonical_provenance_and_actual_cover(self):
        source={'id':'fixture','progress':{'coverTask':{'taskId':'old-paid-task','provider':'APIMart'}}}
        with tempfile.TemporaryDirectory() as directory,patch.object(stock,'load_catalog',return_value=CATALOG),\
            patch.object(stock,'photo_bytes',return_value=photo_blob()),patch.object(daily,'api') as api,\
            patch.object(daily,'request_json',side_effect=AssertionError('no image provider')):
            art=daily.generated_art(source,{'title':'Data center investment','listingDescription':'Summary'},Path(directory))
            cover=Path(directory)/'fixture.jpg'
            self.assertEqual(art['provider'],'stock');self.assertEqual(art['model'],'licensed-stock-v1')
            self.assertEqual(art['taskId'],'stock1:servers:2:3')
            self.assertEqual(art['source'],stock.stock_photo_source(CATALOG[0]))
            self.assertEqual(art['sha256'],hashlib.sha256(cover.read_bytes()).hexdigest())
            self.assertEqual((art['width'],art['height']),(1200,1800));self.assertGreater(art['bytes'],8000)
            saved=api.call_args.args[1];self.assertTrue(saved['replaceCoverTask'])
            self.assertEqual(saved['coverTask']['source'],art['source'])
            self.assertNotIn('sourceImageSha256',art['source'])

    def test_unmatched_and_unavailable_catalog_use_native_without_ai(self):
        for catalogue in ([],OSError('unavailable')):
            with self.subTest(catalogue=type(catalogue).__name__),tempfile.TemporaryDirectory() as directory,\
                patch.object(stock,'load_catalog',side_effect=catalogue if isinstance(catalogue,Exception) else None,return_value=catalogue),\
                patch.object(daily,'api'),patch.object(daily,'request_json',side_effect=AssertionError('no image provider')):
                art=daily.generated_art({'id':'fixture'},{'title':'Unmatched topic','listingDescription':''},Path(directory))
                self.assertEqual(art['provider'],'native');self.assertEqual(art['model'],'native-cover-v1')
                self.assertEqual(art['taskId'],'native1:fixture');self.assertIsNone(art['source'])
                self.assertGreater(art['bytes'],8000)

    def test_valid_cached_artwork_is_reused_without_new_supplier_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            cover=Path(directory)/'fixture.jpg';Image.effect_noise((1200,1800),60).convert('RGB').save(cover,format='JPEG')
            task={'taskId':'old-task','provider':'APIMart','model':'gpt-image-2','sha256':hashlib.sha256(cover.read_bytes()).hexdigest()}
            with patch.object(stock,'load_catalog',side_effect=AssertionError('no download')),patch.object(daily,'api',side_effect=AssertionError('no write')),\
                patch.object(daily,'request_json',side_effect=AssertionError('no provider')):
                art=daily.generated_art({'id':'fixture','progress':{'coverTask':task}},{'title':'Original title'},Path(directory))
            self.assertEqual(art['taskId'],'old-task');self.assertEqual(art['sha256'],task['sha256'])

    def test_rebuild_selects_stock_or_native_without_resubmitting_old_ai_task(self):
        with tempfile.TemporaryDirectory() as directory:
            cover=Path(directory)/'fixture.jpg';Image.effect_noise((1200,1800),60).convert('RGB').save(cover,format='JPEG')
            with patch.object(stock,'load_catalog',return_value=[]),patch.object(daily,'api') as api,patch.object(daily,'request_json',side_effect=AssertionError('no image provider')):
                art=daily.generated_art({'id':'fixture','rebuild':True,'progress':{'coverTask':{'taskId':'old-task','provider':'APIMart'}}},{'title':'New title'},Path(directory))
            self.assertEqual(art['provider'],'native');self.assertTrue(api.call_args.args[1]['replaceCoverTask'])

    def test_pdf_keeps_source_blocks_and_full_photo_credit(self):
        original=[{'type':'paragraph','sourceLines':[1,1],'text':'Original findings, with all 65% qualifiers retained.'}]
        edition={'id':'fixture','blocks':original,'sourceName':'Unsolved Problems','sourceDate':'15 October 2026',
            'coverGeneration':{'provider':'stock','source':stock.stock_photo_source(CATALOG[0])}}
        texts=[item.getPlainText() for item in renderer.story(edition) if isinstance(item,Paragraph)]
        self.assertIn(original[0]['text'],texts)
        credit=next(value for value in texts if value.startswith('Illustrative photograph:'))
        self.assertIn(CATALOG[0]['author'],credit);self.assertIn(CATALOG[0]['licenseUrl'],credit);self.assertIn(CATALOG[0]['sourcePage'],credit)
        self.assertIn('Resized/cropped',credit)
        self.assertEqual(edition['blocks'],original)

    def test_daily_workflow_schedules_text_model_flags_and_credentials_are_preserved(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/technology-frontiers-daily.yml').read_text()
        self.assertIn('cron: "15 4 * * *"',text)
        self.assertIn('vars.ENABLE_TECHNOLOGY_FRONTIERS_PUBLICATION',text)
        self.assertIn("vars.GATEX_TRANSLATION_MODEL || 'gpt-4o-mini'",text)
        self.assertIn('secrets.GATEX_TECHNOLOGY_PUBLICATION_SECRET',text)
        self.assertIn('workflows: [scheduled-snapshot, technology-frontiers-historical-backfill]',text)
        code=Path(daily.__file__).read_text()
        self.assertNotIn('/v1/images/generations',code)
        self.assertNotIn('/v1/tasks/',code)
        self.assertTrue('/chat/completions' in code)


if __name__=='__main__':unittest.main()
