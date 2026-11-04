"""The Release body budget belongs exclusively to complete human notes."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import publication_body


class PublicationSize(unittest.TestCase):
    def test_limit_rejects_without_truncation(self):
        text = 'x' * publication_body.MAX_CHARACTERS
        self.assertEqual(publication_body.checked(text), text)
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            publication_body.checked(text + 'x')

    def test_unicode_prose_roundtrips_without_machine_payload(self):
        text = '### Component changes\n完整说明与链接。  \n' * 1000
        payload = json.dumps({'body': publication_body.checked(text)}, ensure_ascii=True)
        self.assertEqual(json.loads(payload)['body'], text)
        self.assertFalse(publication_body.has_machine_records(text))

    def test_unknown_html_comments_are_not_removed(self):
        text = 'Human notes\n<!-- editorial TODO retained -->\n'
        self.assertEqual(publication_body.checked(text), text)


if __name__ == '__main__': unittest.main()
