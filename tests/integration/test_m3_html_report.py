"""spec §13 M3: "HTML report opens from a local file with no network and
prints to A4 cleanly." Proven end to end through the real CLI (`--output
html --html-path`) against real Postgres, not by calling render_html()
directly (that's already covered by tests/unit/test_report_html.py).
"""

from __future__ import annotations

import re

from tablediff.cli.main import main as cli_main

from .conftest import exec_sql


def _seed(pg_database: str) -> None:
    exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
    exec_sql(pg_database, "INSERT INTO a VALUES (1, 'x'), (2, 'y')")
    exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
    exec_sql(pg_database, "INSERT INTO b VALUES (1, 'x'), (2, 'CHANGED')")


class TestHtmlReportViaCli:
    def test_html_report_written_to_file_no_network_assets(self, pg_database, capsys, tmp_path):
        _seed(pg_database)
        html_path = tmp_path / "report.html"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff",
            "--output", "html", "--html-path", str(html_path),
        ]
        code = cli_main(argv)
        assert code == 1, capsys.readouterr().err  # tables differ

        assert html_path.exists()
        text = html_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in text
        assert "DIFFERENT" in text
        assert "<script" not in text
        assert "http://" not in text
        assert "https://" not in text
        assert "@media print" in text
        assert "A4" in text

    def test_html_report_redacts_the_real_password(self, pg_database, capsys, tmp_path):
        _seed(pg_database)
        html_path = tmp_path / "report.html"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff",
            "--output", "html", "--html-path", str(html_path),
        ]
        cli_main(argv)
        text = html_path.read_text(encoding="utf-8")

        # The fixture's password can coincidentally match other legitimate
        # text in the report (e.g. the "postgres" engine name), so check
        # the DSN's own credential shape specifically rather than a bare
        # substring search for the password.
        password_match = re.search(r"://[^:]+:([^@]+)@", pg_database)
        assert password_match, "test DSN fixture must carry a password to make this a real check"
        password = password_match.group(1)
        assert f":{password}@" not in text
        assert ":***@" in text

    def test_html_and_terminal_can_both_be_requested(self, pg_database, capsys, tmp_path):
        _seed(pg_database)
        html_path = tmp_path / "report.html"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff",
            "--output", "terminal", "--output", "html", "--html-path", str(html_path),
        ]
        code = cli_main(argv)
        captured = capsys.readouterr()
        assert code == 1, captured.err
        assert "result   DIFFERENT" in captured.out
        assert html_path.exists()

    def test_html_output_without_html_path_is_a_clean_error(self, pg_database, capsys):
        _seed(pg_database)
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--output", "html",
        ]
        code = cli_main(argv)
        captured = capsys.readouterr()
        assert code == 2
        assert "Traceback" not in captured.err
        assert "--html-path" in captured.err
