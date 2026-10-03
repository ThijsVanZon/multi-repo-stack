import unittest
from datetime import date

from mrs import versions
from mrs.versions import Version, VersionError


class VersionTests(unittest.TestCase):
    def test_canonical_versions_parse_and_others_fail(self):
        """Unsupported: malformed versions fail clearly."""
        self.assertEqual(versions.parse("26.1.0"), Version(26, 1, 0))
        self.assertEqual(str(versions.parse("05.12.3")), "05.12.3")
        for text in ("26.01.0", "v26.1.0", "26.1", "26.0.0", "2026.1.0", "26.1.0-rc1", "26.1.00", " 26.1.0"):
            with self.subTest(text=text), self.assertRaises(VersionError):
                versions.parse(text)

    def test_version_file_requires_one_final_newline(self):
        self.assertEqual(versions.parse_file(b"26.1.0\n"), Version(26, 1, 0))
        for data in (b"26.1.0", b"26.1.0\r\n", b"26.1.0\n\n", b"\xef\xbb\xbf26.1.0\n"):
            with self.subTest(data=data), self.assertRaises(VersionError):
                versions.parse_file(data)

    def test_next_line_calendar_rule(self):
        """Time: same year increments RELEASE; a later year opens YY.1.0; an open line keeps its name."""
        self.assertEqual(str(versions.next_line(Version(26, 1, 0), date(2026, 12, 31))), "26.2.0")
        self.assertEqual(str(versions.next_line(Version(26, 2, 0), date(2027, 1, 1))), "27.1.0")
        self.assertEqual(str(versions.next_line(Version(26, 9, 0), date(2026, 5, 1))), "26.10.0")
        with self.assertRaises(VersionError):
            versions.next_line(Version(27, 1, 0), date(2026, 12, 31))  # clock rollback needs explanation
        with self.assertRaises(VersionError):
            versions.next_line(Version(26, 1, 1), date(2026, 6, 1))  # patch releases are unsupported

    def test_numeric_not_lexicographic_ordering(self):
        self.assertLess(versions.parse("26.9.0"), versions.parse("26.10.0"))
        self.assertLess(versions.parse("26.10.0"), versions.parse("27.1.0"))
