import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('check_docs', Path(__file__).with_name('check_docs.py'))
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)


class DocumentationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_english(self):
        path = self.write('a.md', '# Guide\n\nEnglish only.\n')
        self.assertEqual(docs.check_file(self.root, path), [])

    def test_pair(self):
        header = '[English](a.md) | [简体中文](a_zh.md)\n\n'
        path = self.write('a.md', header + '# Guide\n')
        self.write('a_zh.md', header + '# 指南\n')
        self.assertEqual(docs.check_file(self.root, path), [])
        self.assertEqual(docs.check_file(self.root, self.root / 'a_zh.md'), [])

    def test_missing_english(self):
        path = self.write('a_zh.md', '# 指南\n')
        self.assertTrue(any('missing English' in item for item in docs.check_file(self.root, path)))

    def test_missing_selector(self):
        path = self.write('a.md', '# Guide\n')
        self.write('a_zh.md', '# 指南\n')
        self.assertTrue(any('reciprocal' in item for item in docs.check_file(self.root, path)))

    def test_untranslated(self):
        path = self.write('a.md', '# Guide\n未翻译\n')
        self.assertTrue(any('Chinese prose' in item for item in docs.check_file(self.root, path)))

    def test_fixture_and_exact_line_exception(self):
        path = self.write('a.md', '# Guide\n```python\nvalue = "中文测试"\n```\n中文产品名 <!-- docs:allow-han -->\n')
        self.assertEqual(docs.check_file(self.root, path), [])

    def test_links_and_fragments(self):
        self.write('b.md', '# Section\n# Section\n<a id="legacy"></a>\n')
        path = self.write('a.md', '[one](b.md#section)\n[two](b.md#section-1)\n[old](b.md#legacy)\n[reference][ref]\n[ref]: b.md#section\n')
        self.assertEqual(docs.check_file(self.root, path), [])

    def test_invalid_link_and_anchor(self):
        path = self.write('a.md', '# Guide\n[broken](missing.md)\n[bad](#unknown)\n')
        result = docs.check_file(self.root, path)
        self.assertTrue(any('missing link' in item for item in result))
        self.assertTrue(any('missing heading' in item for item in result))

    def test_fenced_examples_not_links(self):
        path = self.write('a.md', '# Guide\n~~~text\n[example](missing.md)\n~~~\n`[inline](missing.md)`\n')
        self.assertEqual(docs.check_file(self.root, path), [])

    def test_external_and_escaped_path(self):
        path = self.write('a.md', '[public](https://example.org/docs)\n[escape](../outside.md)\n')
        result = docs.check_file(self.root, path)
        self.assertEqual(len(result), 1)
        self.assertIn('escapes repository', result[0])

    def test_setext_unicode_and_explicit_anchor(self):
        found = docs.anchors('标题\n====\n# A `code` heading\n<a id="stable-id"></a>\n')
        self.assertTrue({'标题', 'a-code-heading', 'stable-id'}.issubset(found))

    def test_upstream_scope(self):
        path = self.write('LICENSES/upstream.md', '上游文本\n')
        self.assertEqual(docs.check_file(self.root, path), [])


if __name__ == '__main__':
    unittest.main()
