"""Reviewed GateX stock catalogue and local assets; no AI or supplier fallback.

Topic selection mirrors GateX lib/stock-photos.ts. A secondary description can
rank eligible photos, but cannot make an unrelated photo eligible.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from PIL import Image, ImageOps

CATALOG_URL = 'https://gatex.fund/images/stock/catalog.json'
STOCK_MODEL = 'licensed-stock-v1'
TOPICS = (
    ('semiconductor','chip','GPU','artificial intelligence','AI','robot','\u534a\u5bfc\u4f53','\u82af\u7247','\u4eba\u5de5\u667a\u80fd','\u673a\u5668\u4eba','data center','data centre','computing','cloud infrastructure','AI infrastructure','\u6570\u636e\u4e2d\u5fc3','\u7b97\u529b','\u4e91\u8ba1\u7b97'),
    ('healthcare','medical','hospital','pharmaceutical','\u533b\u7597','\u533b\u9662','\u533b\u836f'),
    ('solar','photovoltaic','\u592a\u9633\u80fd','\u5149\u4f0f'),
    ('oil','gas','petroleum','crude','\u77f3\u6cb9','\u539f\u6cb9','\u5929\u7136\u6c14'),
    ('energy','renewable energy','energy transition','wind power','\u80fd\u6e90','\u53ef\u518d\u751f\u80fd\u6e90','\u80fd\u6e90\u8f6c\u578b','\u98ce\u7535'),
    ('logistics','shipping','container','supply chain','\u7269\u6d41','\u822a\u8fd0','\u96c6\u88c5\u7bb1','\u4f9b\u5e94\u94fe'),
    ('airport','aviation','airline','\u673a\u573a','\u822a\u7a7a'),
    ('finance','investment','bank','capital markets','stock exchange','equity','\u91d1\u878d','\u6295\u8d44','\u94f6\u884c','\u8d44\u672c\u5e02\u573a','\u8bc1\u5238\u4ea4\u6613\u6240','\u80a1\u6743'),
)
GEOGRAPHIES = ('Singapore','\u65b0\u52a0\u5761','Hong Kong','\u9999\u6e2f','Frankfurt','\u6cd5\u5170\u514b\u798f','United Arab Emirates','UAE','\u963f\u8054\u914b','Saudi Arabia','Saudi','\u6c99\u7279','China','\u4e2d\u56fd','United States','USA','\u7f8e\u56fd','United Kingdom','UK','\u82f1\u56fd','Germany','\u5fb7\u56fd','Japan','\u65e5\u672c','India','\u5370\u5ea6','Qatar','\u5361\u5854\u5c14','Bahrain','\u5df4\u6797','Kuwait','\u79d1\u5a01\u7279','Oman','\u963f\u66fc','Australia','\u6fb3\u5927\u5229\u4e9a','Canada','\u52a0\u62ff\u5927','Malaysia','\u9a6c\u6765\u897f\u4e9a','Indonesia','\u5370\u5ea6\u5c3c\u897f\u4e9a','Thailand','\u6cf0\u56fd','Vietnam','\u8d8a\u5357','Gulf','\u6d77\u6e7e','Middle East','\u4e2d\u4e1c','Dubai','\u8fea\u62dc','Riyadh','\u5229\u96c5\u5f97','GCC','\u6d77\u5408\u4f1a','MENA','\u4e2d\u4e1c\u5317\u975e','Abu Dhabi','\u963f\u5e03\u624e\u6bd4','Shanghai','\u4e0a\u6d77','Beijing','\u5317\u4eac','Shenzhen','\u6df1\u5733')


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).lower().split())


def matches(text, keyword):
    key = normalized(keyword)
    if not key: return False
    if re.search(r'[\u3400-\u9fff]', key): return key in text
    return bool(re.search(r'(^|[^a-z0-9_])' + re.escape(key) + r'(?:s|es)?(?=$|[^a-z0-9_])', text))


def _specificity(keyword):
    key = normalized(keyword)
    if re.search(r'[\u3400-\u9fff]', key): return 2 if len(key) >= 4 else 1
    words = len(key.split(' '))
    return words + 1 if words > 1 else 1


def _signature(value):
    result = 2166136261
    for char in value:
        # JS for-of uses each Unicode scalar, charCodeAt(0) its first UTF-16 unit.
        unit = int.from_bytes(char.encode('utf-16-le')[:2], 'little')
        result = ((result ^ unit) * 16777619) & 0xffffffff
    return result


def select_stock_photo(title, catalog, *, industry='', summary='', seed=None, exclude_ids=()):
    original_title = str(title or '')
    title, industry, summary = map(normalized, (title, industry, summary))
    primary = title + ' ' + industry
    topics = [words for words in TOPICS if any(matches(primary, word) for word in words)]
    energy = [words for words in topics if words in (TOPICS[2], TOPICS[3])]
    specific = [words for words in topics if words != TOPICS[-1]]
    required = energy or specific or topics
    explicit_location = any(matches(primary, location) for location in GEOGRAPHIES)
    ranked = []
    for photo in catalog:
        if photo['id'] in exclude_ids: continue
        keywords = photo.get('keywords') or []
        if not any(matches(title, word) or matches(industry, word) for word in keywords): continue
        if photo.get('locations') and explicit_location and not any(matches(primary, place) for place in photo['locations']): continue
        if required and not any(any(matches(normalized(keyword), word) for word in words for keyword in keywords) for words in required): continue
        score = sum(_specificity(word) * ((5 if matches(title, word) else 0) + (7 if matches(industry, word) else 0) + (1 if matches(summary, word) else 0)) for word in keywords)
        if score: ranked.append((-score, _signature(f'{seed or original_title}:{photo["id"]}'), photo['id'], photo))
    ranked.sort(key=lambda item: item[:3])
    return ranked[0][3] if ranked else None


def stock_photo_source(photo):
    host = urlsplit(photo['sourcePage']).hostname or ''
    origin = ('wikimedia-commons' if host.endswith('wikimedia.org') else 'pexels' if host.endswith('pexels.com') else 'unsplash' if host.endswith('unsplash.com') else host or 'licensed-library')
    source = dict(id=photo['id'], provider='stock', origin=origin, title=photo['title'], author=photo['author'], license=photo['license'], licenseUrl=photo['licenseUrl'], sourceUrl=photo['sourcePage'], imageUrl=photo['imageUrl'], width=photo['width'], height=photo['height'], credit=f'{photo["title"]} \u2014 {photo["author"]} ({photo["license"]})')
    if photo.get('objectPosition'): source['objectPosition'] = photo['objectPosition']
    return source


def _read(url, limit):
    if urlsplit(url).scheme != 'https': raise ValueError('Stock assets require HTTPS')
    with urlopen(Request(url, headers={'User-Agent':'GateX-Stock-Publisher/1.0','Accept':'application/json,image/*'}), timeout=35) as response:
        if urlsplit(response.geturl()).netloc != urlsplit(url).netloc: raise ValueError('Stock asset changed origin')
        blob = response.read(limit + 1)
    if len(blob) > limit: raise ValueError('Stock response exceeds limit')
    return blob


def catalog_url():
    return os.getenv('GATEX_STOCK_CATALOG_URL', CATALOG_URL).strip() or CATALOG_URL


def load_catalog(url=None):
    local = Path(__file__).resolve().parents[1] / 'public/images/stock/catalog.json'
    value = json.loads(local.read_text()) if url is None and local.is_file() else json.loads(_read(url or catalog_url(), 512 * 1024))
    if value.get('version') != 1 or not isinstance(value.get('photos'), list): raise ValueError('Unsupported stock catalogue')
    for photo in value['photos']:
        if not re.fullmatch(r'/images/stock/[a-z0-9-]+\.(?:jpg|jpeg|png)', photo.get('src','')): raise ValueError('Invalid stock local asset')
        if not all(photo.get(key) for key in ('id','title','author','license','licenseUrl','sourcePage','imageUrl','width','height')): raise ValueError('Stock provenance missing')
    return value['photos']


def photo_bytes(photo, url=None):
    src = photo.get('src','')
    if not re.fullmatch(r'/images/stock/[a-z0-9-]+\.(?:jpg|jpeg|png)', src): raise ValueError('Invalid stock local asset')
    local = Path(__file__).resolve().parents[1] / 'public' / src.lstrip('/')
    if url is None and local.is_file():
        if local.stat().st_size > 16 * 1024 * 1024: raise ValueError('Stock response exceeds limit')
        return local.read_bytes()
    return _read(urljoin(url or catalog_url(), src), 16 * 1024 * 1024)


def save_stock_photo(photo, target, *, size=(1536,1024), portrait_cover=False, blob=None):
    """Crop reviewed raster locally. Preserve canonical origin separately from output SHA."""
    data = photo_bytes(photo) if blob is None else blob
    with Image.open(io.BytesIO(data)) as original:
        original.load()
        image = ImageOps.exif_transpose(original).convert('RGB')
        if image.width < 600 or image.height < 450: raise ValueError('Stock resolution is too small')
        position = re.fullmatch(r'(\d+(?:\.\d+)?)% (\d+(?:\.\d+)?)%', photo.get('objectPosition','50% 50%'))
        centering = tuple(min(1.,max(0.,float(value)/100)) for value in position.groups()) if position else (.5,.5)
        if portrait_cover:
            width,height=size
            canvas=Image.new('RGB', size, '#081D38')
            lower=int(height*.45)
            canvas.paste(ImageOps.fit(image,(width,lower),Image.Resampling.LANCZOS,centering=centering),(0,height-lower))
            image=canvas
        else: image=ImageOps.fit(image,size,Image.Resampling.LANCZOS,centering=centering)
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.tmp')
    image.save(temporary,format='PNG' if target.suffix.lower()=='.png' else 'JPEG',quality=93,optimize=True)
    temporary.replace(target)
    return {'source':stock_photo_source(photo),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size,'width':image.width,'height':image.height,'sourceImageSha256':hashlib.sha256(data).hexdigest(),'cropApplied':True}


def prepare_stock_photo(title, target, *, industry='', summary='', seed=None, exclude_ids=(), size=(1536,1024), portrait_cover=False, catalog=None):
    photo=select_stock_photo(title, load_catalog() if catalog is None else catalog, industry=industry,summary=summary,seed=seed,exclude_ids=exclude_ids)
    if not photo:return None
    return save_stock_photo(photo,target,size=size,portrait_cover=portrait_cover)


def photo_caption(source):
    """Human-readable attribution retained in PDF and web image captions."""
    if not source: return ''
    caption = (f"Illustrative photograph: {source['credit']}. "
               f"Source: {source['sourceUrl']}. Licence: {source['licenseUrl']}. "
               "Resized/cropped for this layout.")
    if source['license'].startswith('CC BY-SA'):
        caption += f" The resized photograph and its display crops remain under {source['license']}."
    return caption


def save_native_cover(title, target, *, size=(1200, 1800)):
    """Legible native typography, not a stock or generated-photography claim."""
    from PIL import ImageDraw, ImageFont
    width, height = size
    image = Image.new('RGB', size, '#081D38')
    draw = ImageDraw.Draw(image)
    brand = ImageFont.load_default(size=max(26, width // 23))
    font = ImageFont.load_default(size=max(30, width // 19))
    small = ImageFont.load_default(size=max(16, width // 48))
    left = int(width * .08); right = width - left
    draw.text((left, int(height * .08)), 'GATEX', font=brand, fill='white')
    draw.text((left, int(height * .14)), 'TECHNOLOGY FRONTIERS', font=small, fill='#43BDD7')
    draw.line((left, int(height * .20), right, int(height * .20)), fill='#43BDD7', width=3)
    words = str(title).split(); lines = []; line = ''
    for word in words:
        trial = (line + ' ' + word).strip()
        if line and draw.textlength(trial, font=font) > right - left:
            lines.append(line); line = word
        else: line = trial
    if line: lines.append(line)
    if len(lines) > 14: raise ValueError('Native cover title is too long')
    draw.multiline_text((left, int(height * .26)), '\n'.join(lines), font=font,
                        fill='white', spacing=max(10, width // 75))
    draw.text((left, height - int(height * .08)), 'GATEX.FUND', font=small, fill='#C7DFEF')
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format='JPEG', quality=93, optimize=True)
    if target.stat().st_size < 8000: raise ValueError('Native cover is below the publication byte limit')
    return {'source': None, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'bytes': target.stat().st_size, 'width': width, 'height': height}
