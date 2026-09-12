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
    return {'id': 'renderer-fixture', 'sourceName': 'Unsolved Problems', 'sourceDate': '12 September 2026', 'blocks': blocks}

class RendererTests(unittest.TestCase):
    def comparison(self):
        return [paragraph('The distinction is set out in the following table:', 1),
            paragraph('Can change / Cannot change', 2, 3),
            paragraph('Consensus wrong: Reassessment & its condition / Value trap with a qualification', 4, 6),
            paragraph('Consensus right: Change below 20% / Persistence above 30%', 7, 9)]

    def test_comparison_preserves_every_cell_and_does_not_mutate_source_blocks(self):
        blocks = self.comparison(); before = deepcopy(blocks)
        expected = [['', 'Consensus wrong', 'Consensus right'],
            ['Can change', 'Reassessment & its condition', 'Change below 20%'],
            ['Cannot change', 'Value trap with a qualification', 'Persistence above 30%']]
        self.assertEqual(renderer.comparison_table_cells(blocks, 1), expected)
        self.assertTrue(expected[2][1].startswith('Value trap'))
        self.assertTrue(expected[2][2].startswith('Persistence'))
        items = renderer.story(edition(blocks))
        tables = [item for item in items if isinstance(item, Table)]
        self.assertEqual(len(tables), 1)
        self.assertEqual([[cell.getPlainText() for cell in row] for row in tables[0]._cellvalues], expected)
        self.assertEqual(blocks, before)
        width, height = tables[0].wrap(renderer.W - 96, renderer.H - 129)
        self.assertAlmostEqual(width, renderer.W - 96)
        self.assertLess(height, renderer.H - 129)
        unrelated = self.comparison()
        unrelated[1]['text'] = 'Lower intensity / Higher intensity'
        self.assertEqual(renderer.comparison_table_cells(unrelated, 1)[0], ['', 'Lower intensity', 'Higher intensity'])

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
                self.assertIn(cell, extracted)

    def test_ambiguous_comparisons_remain_original_paragraphs(self):
        variants = []
        for index, patch in [(0, {'text': 'Ordinary prose without a table introduction.'}),
            (1, {'sourceLines': [2, 2]}), (2, {'text': 'First category: one / two / three'}),
            (3, {'sourceLines': [8, 10]}), (3, {'text': 'Missing row label delimiter / another cell'})]:
            blocks = self.comparison(); blocks[index].update(patch); variants.append(blocks)
        for blocks in variants:
            with self.subTest(blocks=blocks):
                self.assertIsNone(renderer.comparison_table_cells(blocks, 1))
                items = renderer.story(edition(blocks))
                self.assertFalse(any(isinstance(item, Table) for item in items))
                texts = [item.getPlainText() for item in items if isinstance(item, Paragraph)]
                for block in blocks:
                    self.assertIn(block['text'], texts)

    def test_source_note_does_not_interrupt_an_intro_and_its_list(self):
        long_text = 'This substantive paragraph develops the original argument, preserving its evidence, context and qualifications for the reader.'
        blocks = [paragraph(long_text, 1), paragraph('A section heading', 2, kind='heading'),
            paragraph(long_text + ' The discussion continues.', 3), paragraph('Three key points:', 4),
            paragraph('First point.', 5, kind='bullet'), paragraph('Second point.', 6, kind='bullet'),
            paragraph('Third point.', 7, kind='bullet'), paragraph(long_text + ' A concluding observation.', 8)]
        texts = [item.getPlainText() for item in renderer.story(edition(blocks)) if isinstance(item, Paragraph)]
        note_index = next(index for index, value in enumerate(texts) if value.startswith('Source and edition note:'))
        intro_index = texts.index('Three key points:')
        self.assertEqual(note_index + 1, intro_index)
        self.assertEqual(texts[intro_index + 1:intro_index + 4], ['- First point.', '- Second point.', '- Third point.'])

    def test_short_article_places_note_after_body_without_splitting_table(self):
        blocks = self.comparison()
        items = renderer.story(edition(blocks))
        table_index = next(index for index, value in enumerate(items) if isinstance(value, Table))
        note_index = next(index for index, value in enumerate(items) if isinstance(value, Paragraph)
            and value.getPlainText().startswith('Source and edition note:'))
        self.assertGreater(note_index, table_index)
        self.assertEqual(sum(isinstance(value, Paragraph) and value.getPlainText().startswith('Source and edition note:') for value in items), 1)

if __name__ == '__main__': unittest.main()
