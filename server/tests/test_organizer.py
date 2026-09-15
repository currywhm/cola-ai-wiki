import unittest

from app.services.organizer import local_organize


class OrganizerTests(unittest.TestCase):
    def test_local_organize_is_source_preserving(self):
        result = local_organize('项目说明.md', '项目预算为一百万元。交付时间是六月。')
        self.assertEqual(result['title'], '项目说明')
        self.assertIn('一百万元', result['summary'])
        self.assertEqual(result['method'], 'local')
        self.assertGreaterEqual(len(result['key_points']), 2)


if __name__ == '__main__':
    unittest.main()
