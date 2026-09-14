"""spec §13 M3: "--sample 1% selects the same rows on both sides (verify
by diffing a sample against itself → MATCH)." Run against real Postgres,
through the actual CLI (not calling core.hashdiff.diff() directly) so the
argparse wiring, the JSON `sample_pct` field, and the terminal "SAMPLED"
notice are all proven end to end, not just the algorithm.
"""

from __future__ import annotations

import json

import pytest

from tablediff.cli.main import main as cli_main

from .conftest import exec_sql


def _seed_identical_tables(pg_database: str, n: int = 20_000) -> None:
    exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, amount numeric(10,2))")
    exec_sql(
        pg_database,
        f"INSERT INTO a SELECT g, 'row-' || g, (g % 1000)::numeric / 10 FROM generate_series(1, {n}) g",
    )
    exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
    exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")


class TestSamplePctSelectsSameRowsAndMatches:
    def test_sample_pct_via_cli_matches_for_identical_data(self, pg_database, capsys, tmp_path):
        _seed_identical_tables(pg_database)
        json_path = tmp_path / "result.json"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--sample", "5%",
            "--output", "json", "--json-path", str(json_path),
        ]
        code = cli_main(argv)
        assert code == 0, capsys.readouterr().err

        payload = json.loads(json_path.read_text())
        assert payload["is_match"] is True
        assert payload["sample_pct"] == 5.0
        # Meaningfully narrower than the full 20,000-row table -- proves
        # sampling actually reduced scope, not the whole table in disguise.
        assert 0 < payload["source_count"] < 4000
        assert payload["source_count"] == payload["target_count"]

    def test_sample_is_deterministic_same_row_count_both_runs(self, pg_database, capsys, tmp_path):
        _seed_identical_tables(pg_database)

        counts = []
        for i in range(2):
            json_path = tmp_path / f"result{i}.json"
            argv = [
                "diff", f"{pg_database}/a", f"{pg_database}/b",
                "--key", "id", "--algorithm", "hashdiff", "--sample", "5%",
                "--output", "json", "--json-path", str(json_path),
            ]
            code = cli_main(argv)
            assert code == 0, capsys.readouterr().err
            counts.append(json.loads(json_path.read_text())["source_count"])

        assert counts[0] == counts[1]

    def test_sample_still_catches_a_real_difference(self, pg_database, capsys, tmp_path):
        """Narrowing scope must never weaken what gets reported for the
        rows that ARE examined — a genuine difference inside the sample
        is still a genuine difference."""
        _seed_identical_tables(pg_database)
        exec_sql(pg_database, "UPDATE b SET name = name || '-CHANGED'")

        json_path = tmp_path / "result.json"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--sample", "10%",
            "--output", "json", "--json-path", str(json_path),
        ]
        code = cli_main(argv)
        assert code == 1, capsys.readouterr().err

        payload = json.loads(json_path.read_text())
        assert payload["is_match"] is False
        assert payload["changed"] > 0
        assert payload["changed"] == payload["source_count"]

    def test_terminal_output_shows_sampled_notice(self, pg_database, capsys):
        _seed_identical_tables(pg_database, n=1000)
        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id", "--algorithm", "hashdiff", "--sample", "20%"]
        code = cli_main(argv)
        captured = capsys.readouterr()
        assert code == 0, captured.err
        # spec §4.3: "Report results as 'of the sampled rows' with the
        # sample size stated. Never silently sample."
        assert "SAMPLED" in captured.out
        assert "20%" in captured.out

    def test_no_sample_flag_leaves_terminal_output_unchanged(self, pg_database, capsys):
        _seed_identical_tables(pg_database, n=500)
        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id", "--algorithm", "hashdiff"]
        code = cli_main(argv)
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "SAMPLED" not in captured.out

    def test_sample_rows_flag_via_cli(self, pg_database, capsys, tmp_path):
        _seed_identical_tables(pg_database, n=20_000)
        json_path = tmp_path / "result.json"
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--sample-rows", "1000",
            "--output", "json", "--json-path", str(json_path),
        ]
        code = cli_main(argv)
        assert code == 0, capsys.readouterr().err
        payload = json.loads(json_path.read_text())
        assert payload["sample_pct"] is not None
        assert 0 < payload["source_count"] < 20_000

    def test_sample_with_joindiff_forced_is_a_clean_error(self, pg_database, capsys):
        _seed_identical_tables(pg_database, n=100)
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "joindiff", "--sample", "10%",
        ]
        code = cli_main(argv)
        captured = capsys.readouterr()
        assert code == 2
        assert "Traceback" not in captured.err
        assert "--sample" in captured.err

    def test_sample_and_sample_rows_together_rejected_by_argparse(self, pg_database):
        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--sample", "10%", "--sample-rows", "100",
        ]
        with pytest.raises(SystemExit) as exc_info:
            cli_main(argv)
        assert exc_info.value.code == 2
