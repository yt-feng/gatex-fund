"""Derive source entries without changing the preserved source wording."""
from __future__ import annotations

import re
from html.parser import HTMLParser


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.ignored += 1
        if not self.ignored and tag in {'p', 'div', 'section', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'br', 'tr'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in {'p', 'div', 'section', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'tr'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def source_lines(text: str, rich_html: str = '') -> list[str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Source body is empty')
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    lines = text.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) != 1:
        return lines
    if rich_html:
        parser = _Paragraphs()
        parser.feed(rich_html)
        paragraphs = [part.strip() for part in ''.join(parser.parts).splitlines() if part.strip()]
        positions = [i for i, char in enumerate(text) if not char.isspace()]
        original = ''.join(text[i] for i in positions)
        if len(paragraphs) > 1 and ''.join(re.sub(r'\s', '', part) for part in paragraphs) == original:
            # Use HTML only to locate boundaries in the ORIGINAL text. This
            # retains spaces within words/phrases and never substitutes HTML
            # text or a shortened provider summary for the full source.
            result, count, start = [], 0, 0
            for paragraph in paragraphs[:-1]:
                count += len(re.sub(r'\s', '', paragraph))
                end = positions[count]
                result.append(text[start:end].strip())
                start = end
            result.append(text[start:].strip())
            return result
    if len(text) > 1000:
        return [part for part in re.split(r'(?<=[\u3002\uff01\uff1f])(?=[^\s\u201d\u2019])', text) if part]
    return lines
