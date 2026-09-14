"""Unit tests for report/html.py's render_html() — spec §8.3: "One
self-contained file. Sections: summary banner (MATCH/DIFFERENT/
INCOMPLETE), per-table cards, sample of differing rows (capped, with rule
citations), warnings, run metadata (who, when, versions, both DSNs with
secrets redacted), and a 'Reproduce' section with the SQL used ...
No external fonts, no JavaScript required to read it."

No database needed -- built entirely from DiffResult, same as the
terminal/JSON renderers' own unit tests.
"""

from __future__ import annotations

from tablediff.core.models import Algorithm, DiffResult, NormalisationRule, RowDiff, TableRef, Warning
from tablediff.report.html import render_html


def _match_result() -> DiffResult:
    return DiffResult(
        source=TableRef(engine="postgres", database="db", table="orders"),
        target=TableRef(engine="clickhouse", database="db", table="orders"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=1000,
        target_count=1000,
        segments_examined=4,
        queries_per_side=8,
        elapsed_seconds=1.23,
    )


def _different_result() -> DiffResult:
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="orders"),
        target=TableRef(engine="clickhouse", database="db", table="orders"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=1000,
        target_count=999,
        missing_in_target=1,
        changed=1,
    )
    result.row_diffs.append(
        RowDiff(key=(42,), kind="changed", changes={"amount": ("1.50", "1.5", NormalisationRule.DEC_1)})
    )
    result.warnings.append(Warning(message="TS-3: naive timestamp assumed UTC", rule=NormalisationRule.TS_3))
    return result


def test_match_shows_match_banner():
    html = render_html(_match_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "MATCH" in html
    assert "DIFFERENT" not in html


def test_different_shows_different_banner():
    html = render_html(_different_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "DIFFERENT" in html


def test_is_self_contained_no_external_assets_or_js():
    html = render_html(_match_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html


def test_has_print_a4_media_rule():
    html = render_html(_match_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "@media print" in html
    assert "A4" in html


def test_dsns_are_redacted_in_output():
    html = render_html(
        _match_result(),
        source_dsn="postgres://user:hunter2@host:5432/db",
        target_dsn="clickhouse://admin:s3cret@ch-host:8123/db",
    )
    assert "hunter2" not in html
    assert "s3cret" not in html


def test_differing_row_shown_with_rule_citation():
    html = render_html(_different_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "amount" in html
    assert "1.50" in html
    assert "DEC-1" in html


def test_warnings_are_shown():
    html = render_html(_different_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "TS-3" in html
    assert "naive timestamp assumed UTC" in html


def test_reproduce_section_shows_generated_sql():
    html = render_html(
        _match_result(),
        source_dsn="postgres://u:p@h/db",
        target_dsn="clickhouse://u:p@h/db",
        sql_statements=["SELECT count(*) FROM orders", "SELECT md5(...) FROM orders"],
    )
    assert "Reproduce" in html
    assert "SELECT count(*) FROM orders" in html


def test_run_metadata_shows_who_when_and_version():
    html = render_html(
        _match_result(),
        source_dsn="postgres://u:p@h/db",
        target_dsn="clickhouse://u:p@h/db",
        generated_by="ci-runner",
    )
    assert "ci-runner" in html
    import tablediff
    assert tablediff.__version__ in html


def test_sample_pct_is_surfaced_when_set():
    result = _match_result()
    result.sample_pct = 5.0
    html = render_html(result, source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert "5" in html
    assert "sample" in html.lower()


def test_html_is_valid_enough_to_have_matching_tags():
    html = render_html(_match_result(), source_dsn="postgres://u:p@h/db", target_dsn="clickhouse://u:p@h/db")
    assert html.strip().startswith("<!DOCTYPE html>") or html.strip().startswith("<html")
    assert "<html" in html
    assert "</html>" in html
    assert "<body" in html
    assert "</body>" in html
