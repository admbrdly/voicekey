import unittest

from voicekey.delivery import UnsafeText, prepare


class DeliveryTextTests(unittest.TestCase):
    def test_normal_text_and_unicode_are_unchanged(self):
        text = "Hello, 世界! 👩‍💻 — café"
        self.assertEqual(prepare(text), text)

    def test_all_line_breaks_and_tabs_are_flattened(self):
        for separator in ('\n', '\r', '\r\n', '\v', '\f', '\x85', '\u2028', '\u2029', '\t', '\n\n\t'):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(prepare('first' + separator + 'second'), 'first second')
                self.assertEqual(prepare('first' + separator), 'first ')

    def test_formatted_text_keeps_paragraphs_and_indentation(self):
        self.assertEqual(prepare('first\r\n\r\n\tsecond\u2028third', formatting=True),
                         'first\n\n\tsecond\nthird')

    def test_flattening_absorbs_adjacent_spaces_without_changing_plain_spacing(self):
        for separator in (' \n', '\n ', '  \r\n\n  \t  ', ' \t '):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(prepare('first' + separator + 'second'), 'first second')
        text = 'first  second' + ' ' * 10000
        self.assertEqual(prepare(text), text)
        self.assertEqual(prepare('first \n  second', formatting=True), 'first \n  second')

    def test_other_c0_del_and_c1_controls_refuse_the_whole_text(self):
        formatting_codes = {9, 10, 11, 12, 13, 0x85}
        for code in (*range(32), *range(0x7f, 0xa0)):
            if code in formatting_codes:
                continue
            for formatting in (False, True):
                with self.subTest(code=code, formatting=formatting):
                    with self.assertRaisesRegex(UnsafeText, f'U\\+{code:04X}'):
                        prepare('before' + chr(code) + 'after', formatting=formatting)
