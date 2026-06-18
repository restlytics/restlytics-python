"""SQL normalization tests -- pure stdlib, no third-party deps.

Mirrors the Laravel reference SDK's cases so every language produces the same
``db.query.summary`` grouping key.
"""

import os
import sys
import unittest

# Make ``restlytics`` importable when running ``python -m unittest discover``
# from the SDK root, without installing the package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restlytics.sql import normalize, operation_of  # noqa: E402


class SqlNormalizeTest(unittest.TestCase):
    def test_strips_numeric_literals(self):
        self.assertEqual(
            "select * from users where id = ?",
            normalize("SELECT * FROM users WHERE id = 1"),
        )

    def test_spec_example(self):
        # The canonical SPEC example.
        self.assertEqual(
            "select * from users where id = ?",
            normalize("SELECT * FROM users WHERE id = 42"),
        )

    def test_strips_string_literals(self):
        self.assertEqual(
            "select * from users where email = ?",
            normalize("SELECT * FROM users WHERE email = 'alice@example.com'"),
        )

    def test_two_different_literals_produce_the_same_template(self):
        a = normalize("SELECT * FROM users WHERE id = 1")
        b = normalize("SELECT * FROM users WHERE id = 2")
        self.assertEqual(a, b)

    def test_collapses_in_lists_to_single_placeholder(self):
        self.assertEqual(
            "select * from users where id in (?)",
            normalize("SELECT * FROM users WHERE id IN (1, 2, 3, 4, 5)"),
        )
        short = normalize("SELECT * FROM users WHERE id IN (1, 2)")
        long = normalize("SELECT * FROM users WHERE id IN (1, 2, 3, 4)")
        self.assertEqual(short, long)

    def test_collapses_existing_placeholders_and_in_lists(self):
        self.assertEqual(
            "select * from t where id in (?)",
            normalize("SELECT * FROM t WHERE id IN (?, ?, ?)"),
        )

    def test_squashes_whitespace_and_newlines(self):
        self.assertEqual(
            "select id from users where active = ?",
            normalize("SELECT   id\n  FROM users\n\tWHERE active   =   1"),
        )

    def test_collapses_values_tuples(self):
        self.assertEqual(
            "insert into t (a, b) values (?)",
            normalize("INSERT INTO t (a, b) VALUES (1, 2), (3, 4), (5, 6)"),
        )

    def test_handles_named_and_positional_bindings(self):
        self.assertEqual(
            "select * from users where id = ? and name = ?",
            normalize("SELECT * FROM users WHERE id = :id AND name = $1"),
        )

    def test_does_not_mangle_identifiers_with_trailing_digits(self):
        out = normalize("SELECT column2 FROM table1 WHERE column2 = 5")
        self.assertIn("column2", out)
        self.assertIn("= ?", out)

    def test_strips_decimal_and_hex_literals(self):
        self.assertEqual(
            "select * from t where price > ? and flag = ?",
            normalize("SELECT * FROM t WHERE price > 19.99 AND flag = 0xFF"),
        )

    def test_strips_double_quoted_string_literals(self):
        # Double-quoted literals are stripped too (MySQL-style).
        out = normalize('SELECT * FROM t WHERE name = "alice"')
        self.assertEqual("select * from t where name = ?", out)

    def test_empty_and_none_are_safe(self):
        self.assertEqual("", normalize(""))
        self.assertEqual("", normalize(None))

    def test_lowercases_keywords(self):
        self.assertEqual(
            "select id from users",
            normalize("SELECT id FROM USERS"),
        )

    def test_operation_of(self):
        self.assertEqual("select", operation_of("SELECT * FROM users"))
        self.assertEqual("insert", operation_of("  INSERT INTO t VALUES (1)"))
        self.assertEqual("update", operation_of("UPDATE t SET x = 1"))
        self.assertEqual("", operation_of(""))


if __name__ == "__main__":
    unittest.main()
