"""Focused display tests; source blocks and existing report PDFs stay untouched."""
from copy import deepcopy
from io import BytesIO
import importlib.util
from pathlib import Path
import unittest
from pypdf import PdfReader
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table

spec = importlib.util.spec_from_file_location('renderer', Path(__file__).with_name('build_technology_frontiers.py'))
renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)

def paragraph(text, first, last=None, kind='paragraph'):
    return {'type': kind, 'text': text, 'sourceLines': [first, last or first]}

def edition(blocks):
    return {'id': 'renderer-fixture', 'sourceName': 'Unsolved Problems', 'sourceDate': '12 September 2026',
        'sourceUrl': 'https://mp.weixin.qq.com/s?__biz=private&mid=123&idx=1', 'blocks': blocks}

class RendererTests(unittest.TestCase):
    def comparison(self):
        return [paragraph('Expanded into the following table:', 2),
            paragraph('Can be changed / Cannot be changed', 3, 4),
            paragraph("Consensus is wrong: Contrarian—this is a strategy that can make big money / Value trap: Even though you are right, you don't get paid.", 5, 7),
            paragraph('Consensus is right: Go with the consensus, but only if the wind stops (e.g., policy risk) / Go with the consensus, but what you earn is a discount from "underestimated persistence."', 8, 10)]

    def test_comparison_preserves_every_cell_and_does_not_mutate_source_blocks(self):
        blocks = self.comparison(); before = deepcopy(blocks)
        expected = [['', 'Consensus is wrong', 'Consensus is right'],
            ['Can be changed', 'Contrarian—this is a strategy that can make big money', 'Go with the consensus, but only if the wind stops (e.g., policy risk)'],
            ['Cannot be changed', "Value trap: Even though you are right, you don't get paid.", 'Go with the consensus, but what you earn is a discount from "underestimated persistence."']]
        self.assertEqual(renderer.comparison_table_cells(blocks, 1), expected)
        self.assertTrue(expected[2][1].startswith('Value trap'))
        self.assertIn('underestimated persistence', expected[2][2])
        items = renderer.story(edition(blocks))
        tables = [item for item in items if isinstance(item, Table)]
        self.assertEqual(len(tables), 1)
        self.assertEqual([[cell.getPlainText() for cell in row] for row in tables[0]._cellvalues],
            [[renderer.plain(cell) for cell in row] for row in expected])
        self.assertEqual(blocks, before)
        width, height = tables[0].wrap(renderer.W - 96, renderer.H - 129)
        self.assertAlmostEqual(width, renderer.W - 96)
        self.assertLess(height, renderer.H - 129)
        unrelated = self.comparison()
        unrelated[1]['text'] = 'Lower intensity / Higher intensity'
        self.assertEqual(renderer.comparison_table_cells(unrelated, 1)[0], ['', 'Lower intensity', 'Higher intensity'])
        concise = self.comparison()
        concise[1]['text'] = 'Can change / Cannot change'
        concise[2]['text'] = concise[2]['text'].replace('Consensus is wrong:', 'Consensus wrong:')
        concise[3]['text'] = concise[3]['text'].replace('Consensus is right:', 'Consensus right:')
        self.assertEqual(renderer.comparison_table_cells(concise, 1)[0], ['', 'Consensus wrong', 'Consensus right'])

    def test_native_table_pdf_retains_all_cell_text(self):
        cells = renderer.comparison_table_cells(self.comparison(), 1)
        result = BytesIO()
        doc = SimpleDocTemplate(result, pagesize=(renderer.W, renderer.H), leftMargin=48, rightMargin=48)
        doc.build([renderer.comparison_table(cells)])
        pages = PdfReader(result).pages
        self.assertEqual(len(pages), 1)
        extracted = ' '.join((pages[0].extract_text() or '').split())
        for row in cells:
            for cell in row:
                self.assertIn(renderer.plain(cell), extracted)

    def test_ambiguous_comparisons_remain_original_paragraphs(self):
        variants = []
        for index, patch in [(0, {'text': 'Ordinary prose without a table introduction.'}),
            (1, {'sourceLines': [3, 3]}), (2, {'text': 'First category: one / two / three'}),
            (3, {'sourceLines': [9, 11]}), (3, {'text': 'Missing row label delimiter / another cell'})]:
            blocks = self.comparison(); blocks[index].update(patch); variants.append(blocks)
        for blocks in variants:
            with self.subTest(blocks=blocks):
                self.assertIsNone(renderer.comparison_table_cells(blocks, 1))
                items = renderer.story(edition(blocks))
                self.assertFalse(any(isinstance(item, Table) for item in items))
                texts = [item.getPlainText() for item in items if isinstance(item, Paragraph)]
                for block in blocks:
                    self.assertIn(renderer.plain(block['text']), texts)

    def test_source_is_removed_from_body_and_kept_briefly_in_disclaimer(self):
        long_text = 'This substantive paragraph develops the original argument, preserving its evidence, context and qualifications for the reader.'
        blocks = [paragraph(long_text, 1), paragraph('A section heading', 2, kind='heading'),
            paragraph(long_text + ' The discussion continues.', 3), paragraph('Three key points:', 4),
            paragraph('First point.', 5, kind='bullet'), paragraph('Second point.', 6, kind='bullet'),
            paragraph('Third point.', 7, kind='bullet'), paragraph(long_text + ' A concluding observation.', 8)]
        texts = [item.getPlainText() for item in renderer.story(edition(blocks)) if isinstance(item, Paragraph)]
        self.assertFalse(any(value.startswith('Source and edition note:') for value in texts))
        self.assertTrue(any('This edition is an authorized English translation of material published by Unsolved Problems on 12 September 2026, prepared and presented by GateX.' in value for value in texts))
        self.assertNotIn('https://mp.weixin.qq.com', ' '.join(texts))
        intro_index = texts.index('Three key points:')
        self.assertEqual(texts[intro_index + 1:intro_index + 4], ['- First point.', '- Second point.', '- Third point.'])

    def test_raw_source_urls_are_omitted_from_body_layout(self):
        blocks = [paragraph('Original essay: https://example.com/article', 1),
            paragraph('A substantive paragraph follows the reference.', 2)]
        texts = [item.getPlainText() for item in renderer.story(edition(blocks)) if isinstance(item, Paragraph)]
        self.assertIn('Original essay:', texts)
        self.assertNotIn('https://example.com/article', ' '.join(texts))

    def test_short_article_keeps_disclaimer_after_body_without_splitting_table(self):
        blocks = self.comparison()
        items = renderer.story(edition(blocks))
        table_index = next(index for index, value in enumerate(items) if isinstance(value, Table))
        disclaimer_index = next(index for index, value in enumerate(items) if isinstance(value, Paragraph)
            and value.getPlainText() == 'DISCLAIMER & IMPORTANT INFORMATION')
        self.assertGreater(disclaimer_index, table_index)
        self.assertFalse(any(isinstance(value, Paragraph) and value.getPlainText().startswith('Source and edition note:') for value in items))

if __name__ == '__main__': unittest.main()
