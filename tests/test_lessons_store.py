"""Pin invariants: salient.lessons file-store semantics.

The lessons store is the source of truth for cross-engagement
operator-curated notes. Bugs here surface as "I added a lesson but
the agent never sees it" or "appending corrupted yesterday's
header" — both costly. These pins lock the day-header merge rules,
atomic-write behavior, name-validation refusals, and summary
aggregation.

Each test uses SALIENT_LESSONS_DIR via a temp-dir fixture so the
real ~/.salient/lessons stays untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from salient_core.memory import lessons


class _TmpDirFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="salient-lessons-")
        self._prev = os.environ.get("SALIENT_LESSONS_DIR")
        os.environ["SALIENT_LESSONS_DIR"] = self._tmp.name
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("SALIENT_LESSONS_DIR", None)
        else:
            os.environ["SALIENT_LESSONS_DIR"] = self._prev
        self._tmp.cleanup()


class ReadWriteClear(_TmpDirFixture):
    def test_read_missing_returns_empty(self):
        self.assertEqual(lessons.read("nobody"), "")

    def test_write_then_read_round_trip(self):
        lessons.write("scanner", "# 2026-05-28\n- hello\n")
        self.assertEqual(lessons.read("scanner"), "# 2026-05-28\n- hello\n")

    def test_clear_removes_file_and_reports_outcome(self):
        lessons.write("scanner", "x")
        self.assertTrue(lessons.clear("scanner"))
        self.assertFalse(lessons.clear("scanner"))
        self.assertEqual(lessons.read("scanner"), "")

    def test_write_creates_parent_dir(self):
        # Point to a subdir that doesn't exist yet.
        os.environ["SALIENT_LESSONS_DIR"] = str(self.dir / "nested" / "deep")
        lessons.write("scanner", "content")
        self.assertEqual(lessons.read("scanner"), "content")


class AppendDayHeaderRules(_TmpDirFixture):
    def test_first_append_creates_header_and_bullet(self):
        lessons.append("scanner", "first lesson", today="2026-05-28")
        self.assertEqual(
            lessons.read("scanner"),
            "# 2026-05-28\n- first lesson\n",
        )

    def test_same_day_append_slots_under_existing_header(self):
        lessons.append("scanner", "first", today="2026-05-28")
        lessons.append("scanner", "second", today="2026-05-28")
        self.assertEqual(
            lessons.read("scanner"),
            "# 2026-05-28\n- first\n- second\n",
        )

    def test_new_day_emits_fresh_header_with_separator(self):
        lessons.append("scanner", "old", today="2026-05-28")
        lessons.append("scanner", "new", today="2026-05-29")
        self.assertEqual(
            lessons.read("scanner"),
            "# 2026-05-28\n- old\n\n# 2026-05-29\n- new\n",
        )

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            lessons.append("scanner", "   ")

    def test_whitespace_only_around_text_stripped(self):
        lessons.append("scanner", "  hello world  ", today="2026-05-28")
        body = lessons.read("scanner")
        self.assertIn("- hello world\n", body)
        self.assertNotIn("-   hello", body)


class NameValidation(_TmpDirFixture):
    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            lessons.read("")

    def test_path_separators_rejected(self):
        for bad in ("foo/bar", "foo\\bar", "../foo", "./foo"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                lessons.read(bad)


class AtomicWrite(_TmpDirFixture):
    def test_no_tmp_artifacts_after_write(self):
        lessons.write("scanner", "body")
        # `.scanner.<random>.tmp` files should not survive a successful
        # write — atomic rename consumes them.
        artifacts = list(self.dir.glob(".scanner.*.tmp"))
        self.assertEqual(artifacts, [])

    def test_overwrite_preserves_no_partial_content(self):
        lessons.write("scanner", "old content")
        lessons.write("scanner", "new content")
        self.assertEqual(lessons.read("scanner"), "new content")


class Summary(_TmpDirFixture):
    def test_returns_empty_when_dir_missing(self):
        # Point at a dir that doesn't exist.
        os.environ["SALIENT_LESSONS_DIR"] = str(self.dir / "never")
        self.assertEqual(lessons.summary(), [])

    def test_excludes_empty_files(self):
        # An empty file shouldn't show up as a lessons entry.
        empty = self.dir / "ghost.md"
        empty.write_text("")
        self.assertEqual(lessons.summary(), [])

    def test_counts_bullets_not_headers(self):
        lessons.append("scanner", "a", today="2026-05-28")
        lessons.append("scanner", "b", today="2026-05-29")
        lessons.append("manager", "x", today="2026-05-28")
        sums = lessons.summary()
        by_agent = {s.agent: s for s in sums}
        self.assertEqual(by_agent["scanner"].line_count, 2)
        self.assertEqual(by_agent["manager"].line_count, 1)
        self.assertGreater(by_agent["scanner"].size_bytes, 0)

    def test_sorted_by_agent_name(self):
        for n in ("zeta", "alpha", "manager"):
            lessons.append(n, "x", today="2026-05-28")
        names = [s.agent for s in lessons.summary()]
        self.assertEqual(names, sorted(names))


class OversizeSignal(_TmpDirFixture):
    def test_under_threshold_not_oversized(self):
        lessons.write("scanner", "x" * 10)
        s = lessons.summary()[0]
        self.assertFalse(lessons.is_oversized(s))

    def test_over_threshold_flagged(self):
        lessons.write("scanner", "x" * (lessons.SIZE_WARN_BYTES + 1))
        s = lessons.summary()[0]
        self.assertTrue(lessons.is_oversized(s))


if __name__ == "__main__":
    unittest.main()
