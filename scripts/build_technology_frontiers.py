"""Render complete, paragraph-mapped English translations in the GateX PDF template.
Source text stays in the user's archive. Coverage checks reject omissions and
duplicate lines. This builder does not summarize, call a model or add research.
"""
from pathlib import Path
from xml.sax.saxutils import escape
import argparse, hashlib, json, os, shutil
from datetime import datetime, timezone
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('GATEX_PDF_OUTPUT', ROOT / 'output/pdf/technology-frontiers'))
TEMP = ROOT / 'tmp/pdfs/technology-frontiers-full-text'
ARCHIVE = next((path for path in [ROOT.parent / 'wechat-official-account-archive/archive',
    ROOT.parents[2] / 'wechat-official-account-archive/archive'] if path.is_dir()), None)
CONTENT = Path(os.environ.get('GATEX_EDITION_CONTENT', ROOT / 'content/technology-frontiers'))
if os.environ.get('GATEX_SOURCE_ARCHIVE'):
    ARCHIVE = Path(os.environ['GATEX_SOURCE_ARCHIVE'])
REVISION = '20260912-full-edition'
W, H = 595.276, 841.89
NAVY, BLUE, INK, MUTED, CYAN, LINE = map(HexColor,
    ['#081D38', '#1267A3', '#182E43', '#5A6E80', '#43BDD7', '#D3E0EA'])
def register_fonts():
    roots = [Path(os.environ['GATEX_FONT_DIR'])] if os.environ.get('GATEX_FONT_DIR') else []
    roots += [Path('/System/Library/Fonts/Supplemental'),
        Path('/usr/share/fonts/truetype/liberation2'), Path('/usr/share/fonts/truetype/liberation')]
    for name, candidates in [('GX', ['Arial.ttf', 'LiberationSans-Regular.ttf']),
        ('GXB', ['Arial Bold.ttf', 'LiberationSans-Bold.ttf']),
        ('GXS', ['Times New Roman.ttf', 'LiberationSerif-Regular.ttf'])]:
        font = next((root / file for root in roots for file in candidates if (root / file).is_file()), None)
        if font is None: raise RuntimeError('Install Liberation fonts or set GATEX_FONT_DIR')
        pdfmetrics.registerFont(TTFont(name, str(font)))

register_fonts()
STYLES = {
    'paragraph': ParagraphStyle('Body', fontName='GXS', fontSize=11.5, leading=16.6,
        textColor=INK, spaceAfter=11, allowWidows=0, allowOrphans=0),
    'heading': ParagraphStyle('Heading', fontName='GXB', fontSize=15, leading=20,
        textColor=NAVY, spaceBefore=17, spaceAfter=10, keepWithNext=True),
    'subheading': ParagraphStyle('Subheading', fontName='GXB', fontSize=11.5, leading=16,
        textColor=BLUE, spaceBefore=10, spaceAfter=8, keepWithNext=True),
    'bullet': ParagraphStyle('Bullet', fontName='GXS', fontSize=11.5, leading=16.6,
        textColor=INK, leftIndent=13, firstLineIndent=-10, spaceAfter=7,
        allowWidows=0, allowOrphans=0),
    'disclaimer': ParagraphStyle('Disclaimer', fontName='GX', fontSize=9.2, leading=13.2,
        textColor=INK, spaceAfter=13, allowWidows=0, allowOrphans=0),
    'note': ParagraphStyle('Note', fontName='GX', fontSize=9, leading=13,
        textColor=MUTED, spaceAfter=17),
}

def plain(text):
    return text.replace('\u2011', '-').replace('\u2013', '-').replace('\u2014', ' - ')

def draw_paragraph(c, text, x, y, width, size, leading, color, font='GX'):
    p = Paragraph(escape(plain(text)).replace('\n', '<br/>'), ParagraphStyle(
        'Cover', fontName=font, fontSize=size, leading=leading, textColor=color))
    _, height = p.wrap(width, H); p.drawOn(c, x, y - height)
    return y - height

def page_art(c, doc, edition, total=None):
    c.saveState()
    if doc.page == 1:
        c.setFillColor(NAVY); c.rect(0, 0, W, H, fill=1, stroke=0)
        art = ImageReader(str(CONTENT / 'covers' / (edition['id'] + '.jpg')))
        aw, ah = art.getSize(); scale = max(W / aw, H / ah)
        c.drawImage(art, (W - aw * scale) / 2, (H - ah * scale) / 2,
            width=aw * scale, height=ah * scale)
        c.setFillColor(white); c.setFont('GXB', 23); c.drawString(48, H - 70, 'GATEX')
        c.setFont('GX', 9); c.drawString(48, H - 91, 'INTELLIGENCE')
        c.setStrokeColor(CYAN); c.line(48, H - 141, W - 48, H - 141)
        draw_paragraph(c, 'TECHNOLOGY FRONTIERS', 48, H - 178, W - 96, 10, 14, CYAN, 'GXB')
        size = 39 if len(edition['title']) < 45 else 34
        y = draw_paragraph(c, edition['title'], 48, H - 216, W - 102, size, size + 5, white, 'GXS')
        if y < 290: raise ValueError('Cover title overlaps edition date')
        draw_paragraph(c, edition['sourceDate'], 48, 78, W - 96, 10, 15, HexColor('#C7DFEF'))
        c.setFillColor(white); c.setFont('GX', 8); c.drawString(48, 38, 'GATEX.FUND')
    else:
        c.setFillColor(NAVY); c.rect(0, H - 8, W, 8, fill=1, stroke=0)
        c.setFont('GXB', 11); c.drawString(48, H - 38, 'GATEX')
        c.setFillColor(MUTED); c.setFont('GX', 8)
        c.drawRightString(W - 48, H - 37, 'TECHNOLOGY FRONTIERS')
        c.setStrokeColor(LINE); c.setLineWidth(.6); c.line(48, H - 51, W - 48, H - 51)
        c.setFont('GX', 8); c.drawString(48, 32, 'GATEX INTELLIGENCE')
        c.drawRightString(W - 48, 32, f'{doc.page:02d} / {total:02d}' if total else str(doc.page))
    c.restoreState()

DISCLAIMER_PARAGRAPHS = [
    ('Purpose and scope', 'This publication is provided by GateX for general information and discussion. It is not personal investment, financial, legal, tax, accounting or other professional advice, an investment recommendation, or an offer, solicitation or commitment to buy, sell or underwrite any security, financial instrument, product or service. It does not take account of any reader\'s objectives, financial position, experience, jurisdiction or particular circumstances. Readers should obtain independent professional advice and conduct their own assessment before making decisions.'),
    ('Information, interpretation and timeliness', 'The material reflects the information, opinions and circumstances available to its original author at the stated publication date. Facts, quotations, estimates, descriptions of businesses and market data may be incomplete, disputed, subsequently corrected or out of date. GateX has not independently audited every source, statement or calculation. Reasonable care in translation and presentation does not constitute a warranty of accuracy, completeness, fitness for a particular purpose or continued availability. Differences in language and context may affect interpretation; the original source should be consulted where precision is material. GateX has no general obligation to update this edition or notify readers of later developments.'),
    ('Views, forecasts and illustrations', 'Views expressed belong to the identified authors and quoted speakers and do not necessarily represent GateX, its affiliates, staff or partners. Publication does not imply endorsement of any issuer, product, technology or opinion. Forward-looking statements, forecasts, scenarios, examples and estimates involve assumptions and uncertainty; actual outcomes may differ materially. Past performance, historical relationships and modelled or hypothetical results do not predict future results. References to returns, prices or growth do not constitute a guarantee. Investments may lose value, including the full amount invested; liquidity, currency, leverage, counterparty, market, operational and regulatory conditions can change.'),
    ('Sources, rights and third-party material', 'This edition contains authorized translated source material and is presented with source and editorial notes in the article. Third-party names, marks, quotations, links and data remain attributable to their respective owners. Links are provided for reference and do not imply control over or approval of an external site. Illustrations are editorial visualizations and should not be treated as evidence, actual facilities or representations of a named organization. Permission for this edition does not grant readers a license to reproduce, redistribute, resell, train models on or commercially exploit protected material beyond applicable law or express permission from the relevant rights holder.'),
    ('Use and responsibility', 'Readers remain responsible for how they interpret and use this publication and for compliance with laws and restrictions applicable to them. To the extent permitted by applicable law, GateX and its affiliates do not accept liability for losses arising from reliance on the material or from errors, omissions, service interruptions or third-party content. Nothing in this notice excludes or limits duties or liabilities that cannot lawfully be excluded or limited, and no statement should be read as restricting a reader\'s mandatory statutory rights. Questions, correction requests and rights inquiries may be sent to info@gatex.fund.'),
]

def story(edition):
    items = [Spacer(1, 1), PageBreak()]
    note_at = max(0, len(edition['blocks']) // 2 - 1)
    for index, block in enumerate(edition['blocks']):
        if block['type'] == 'divider':
            items.extend([Spacer(1, 6), Paragraph('* * *', STYLES['note'])])
        else:
            text = escape(plain(block['text'])).replace('\n', '<br/>')
            if block['type'] == 'bullet': text = '- ' + text
            items.append(Paragraph(text, STYLES[block['type']]))
        if index == note_at:
            source_note = (f"Source and edition note: {edition['sourceName']}, {edition['sourceDate']}. "
                'GateX provides the authorized English translation and editorial presentation. '
                'The original argument, examples and qualifications are preserved; the views remain those of the source author.')
            if 'agentic' in edition['id']:
                source_note += ' The publisher\'s commentary frames a reproduced essay by Junyang Lin; these are distinct authorial contributions.'
            if edition.get('sourceUrl'):
                source_note += ' Original publication: ' + edition['sourceUrl']
            items.extend([Spacer(1, 5), Paragraph(escape(plain(source_note)), STYLES['note'])])
    items.extend([PageBreak(), Paragraph('DISCLAIMER & IMPORTANT INFORMATION', STYLES['heading'])])
    for title, body in DISCLAIMER_PARAGRAPHS:
        items.append(Paragraph('<b>' + escape(title) + '.</b> ' + escape(plain(body)), STYLES['disclaimer']))
    return items

def validate_source(edition):
    assert ARCHIVE is not None, 'The preserved WeChat archive was not found'
    source = ARCHIVE / edition['archive']; lines = source.read_text().splitlines(); coverage = []
    for block in edition['blocks']:
        assert block['type'] in (*STYLES, 'divider')
        assert block['type'] == 'divider' or block['text'].strip()
        start, end = block['sourceLines']; assert 1 <= start <= end <= len(lines)
        coverage.extend(range(start, end + 1))
    assert len(coverage) == len(set(coverage)), 'Duplicated source-line coverage'
    assert set(coverage) == set(range(1, len(lines) + 1)), 'Incomplete source-line coverage'
    assert coverage == sorted(coverage), 'Source order changed'
    return source, lines

def build(edition):
    source, lines = validate_source(edition)
    OUT.mkdir(parents=True, exist_ok=True); TEMP.mkdir(parents=True, exist_ok=True)
    path = OUT / (edition['id'] + '.pdf'); draft = TEMP / path.name
    if path.exists():
        old_text = '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
        if 'original English GateX research brief' in old_text or 'UNABRIDGED ENGLISH EDITION' in old_text:
            archived = OUT / ('superseded-briefs' if 'original English GateX research brief' in old_text else 'previous-full-text')
            archived.mkdir(exist_ok=True)
            for suffix in ('.pdf', '.json'):
                old = path.with_suffix(suffix)
                if old.exists(): shutil.copy2(old, archived / old.name)
    pages = None
    for target in (draft, path):
        doc = SimpleDocTemplate(str(target), pagesize=(W, H), rightMargin=48, leftMargin=48,
            topMargin=72, bottomMargin=57, title=edition['title'], author=edition['sourceName'],
            subject='Unabridged English translation - GateX Technology Frontiers')
        callback = lambda c, d: page_art(c, d, edition, pages)
        doc.build(story(edition), onFirstPage=callback, onLaterPages=callback)
        actual = len(PdfReader(target).pages)
        if pages is not None: assert actual == pages
        pages = actual
    reader = PdfReader(path); extracted = '\n'.join(p.extract_text() or '' for p in reader.pages)
    assert not any('\u4e00' <= char <= '\u9fff' for char in extracted)
    assert all(len((p.extract_text() or '').strip()) > 60 for p in reader.pages)
    cover = reader.pages[0].extract_text() or ''
    assert edition['sourceName'] not in cover and 'translation' not in cover.lower()
    final_text = reader.pages[-1].extract_text() or ''
    assert 'DISCLAIMER & IMPORTANT INFORMATION' in final_text, 'Disclaimer must occupy one separate final page'
    assert all(title in final_text for title, _ in DISCLAIMER_PARAGRAPHS), 'Disclaimer is incomplete'
    assert 'DISCLAIMER & IMPORTANT INFORMATION' not in (reader.pages[-2].extract_text() or '')
    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    record = {
        'id': edition['id'], 'section': 'technology-frontiers', 'kind': 'pdf',
        'title': edition['title'], 'summary': edition['listingDescription'],
        'publisher': 'GateX Intelligence', 'sourceName': edition['sourceName'], 'sourceUrl': edition.get('sourceUrl', ''),
        'publishedAt': edition['publishedAt'], 'language': 'English', 'pageCount': pages,
        'coverImageUrl': '/images/technology-frontiers/' + edition['id'] + '-illustrated.jpg',
        'status': 'published', 'order': edition.get('order', 0),
        'fileName': path.name, 'objectKey': 'intelligence-content/files/' + edition['id'] + '/' + REVISION + '.pdf',
        'byteSize': path.stat().st_size, 'createdAt': edition.get('createdAt', stamp), 'updatedAt': stamp,
    }
    path.with_suffix('.json').write_text(json.dumps(record, indent=2) + '\n')
    return {'id': edition['id'], 'pdf': str(path), 'pages': pages, 'bytes': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'source_lines': len(lines), 'covered_source_lines': len(lines),
        'translation_blocks': len(edition['blocks']), 'translated_words': len(' '.join(b['text'] for b in edition['blocks']).split())}

if __name__ == '__main__':
    editions = [json.loads(path.read_text()) for path in sorted(CONTENT.glob('*.json'))]
    assert editions, 'No translated editions are available'
    art_manifest = json.loads((CONTENT / 'covers/manifest.json').read_text())
    art_by_id = {art['id']: art for art in art_manifest}
    for edition in editions:
        art = art_by_id[edition['id']]
        assert art['provider'] == 'APIMart' and art['taskId'], 'Cover must be independently generated'
        assert hashlib.sha256((CONTENT / 'covers' / (edition['id'] + '.jpg')).read_bytes()).hexdigest() == art['sha256']
    assert len({art_by_id[e['id']]['sha256'] for e in editions}) == len(editions), 'Do not reuse article artwork'
    assert len({art_by_id[e['id']]['taskId'] for e in editions}) == len(editions), 'Each article needs its own image task'
    results = [build(edition) for edition in editions]
    (OUT / 'manifest.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
