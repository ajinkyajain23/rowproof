"""HTML sign-off report (spec §8.3): "One self-contained file. Sections:
summary banner (MATCH/DIFFERENT/INCOMPLETE), per-table cards, sample of
differing rows (capped, with rule citations), warnings, run metadata (who,
when, versions, both DSNs with secrets redacted), and a 'Reproduce'
section with the SQL used. Print-friendly ... No external fonts, no
JavaScript required to read it."

Inline CSS, no external assets, no <script> -- everything the spec's
"opens from a file share with no network" requirement needs lives in this
one returned string.
"""

from __future__ import annotations

import getpass
import platform
from datetime import datetime, timezone

from jinja2 import Template

import tablediff
from tablediff.cli.spec import redact_dsn
from tablediff.core.models import DiffResult

_TEMPLATE = Template(
    """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>tablediff report -- {{ result.source.qualified_name }}</title>
<style>
  body { font-family: Georgia, 'Times New Roman', serif; margin: 2rem auto; max-width: 900px;
         color: #1a1a1a; background: #fff; line-height: 1.45; }
  h1, h2 { font-family: Arial, Helvetica, sans-serif; }
  .banner { padding: 1rem 1.5rem; border-radius: 4px; font-family: Arial, Helvetica, sans-serif;
            font-size: 1.4rem; font-weight: bold; margin-bottom: 1.5rem; }
  .banner.match { background: #e6f4ea; color: #1e7e34; border: 1px solid #1e7e34; }
  .banner.different { background: #fdecea; color: #a52a2a; border: 1px solid #a52a2a; }
  .banner.incomplete { background: #fff8e1; color: #8a6d00; border: 1px solid #8a6d00; }
  .card { border: 1px solid #ccc; border-radius: 4px; padding: 1rem 1.5rem; margin-bottom: 1.5rem;
          page-break-inside: avoid; }
  table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
  th, td { border: 1px solid #ccc; padding: 0.3rem 0.6rem; text-align: left; font-size: 0.9rem; }
  th { background: #f5f5f5; }
  .meta { font-size: 0.85rem; color: #444; }
  .rule { color: #666; font-style: italic; }
  .sql { background: #f5f5f5; padding: 0.75rem; font-family: 'Courier New', monospace;
         font-size: 0.85rem; white-space: pre-wrap; word-break: break-word; }
  .warning { color: #8a6d00; }
  @media print {
    @page { size: A4; margin: 1.5cm; }
    body { max-width: none; }
    .card { page-break-inside: avoid; }
  }
</style>
</head>
<body>

<div class="banner {{ status.lower() }}">{{ status }}</div>

<div class="card">
  <h2>{{ result.source.qualified_name }}</h2>
  <table>
    <tr><th>source</th><td>{{ result.source }}</td></tr>
    <tr><th>target</th><td>{{ result.target }}</td></tr>
    <tr><th>key columns</th><td>{{ result.key_columns | join(', ') }}</td></tr>
    <tr><th>algorithm</th><td>{{ result.algorithm.value }}</td></tr>
    {% if result.sample_pct is not none %}
    <tr><th>sample</th><td>{{ result.sample_pct }}% of rows -- counts below are of the sample only</td></tr>
    {% endif %}
    <tr><th>row count</th><td>source {{ result.source_count }} / target {{ result.target_count }}</td></tr>
    <tr><th>missing in target</th><td>{{ result.missing_in_target }}</td></tr>
    <tr><th>extra in target</th><td>{{ result.extra_in_target }}</td></tr>
    <tr><th>changed</th><td>{{ result.changed }}</td></tr>
    <tr><th>segments / queries per side</th><td>{{ result.segments_examined }} / {{ result.queries_per_side }}</td></tr>
    <tr><th>elapsed</th><td>{{ "%.1f"|format(result.elapsed_seconds) }}s</td></tr>
  </table>

  {% if result.row_diffs %}
  <h3>Differing rows{% if result.truncated %} (truncated at {{ result.row_diffs|length }} rows -- counts above are exact){% endif %}</h3>
  <table>
    <tr><th>key</th><th>kind</th><th>column</th><th>source</th><th>target</th><th>rule</th></tr>
    {% for rd in result.row_diffs %}
      {% if rd.kind == "changed" %}
        {% for col, change in rd.changes.items() %}
        <tr>
          <td>{{ rd.key | join(',') }}</td>
          <td>{{ rd.kind }}</td>
          <td>{{ col }}</td>
          <td>{{ change[0] }}</td>
          <td>{{ change[1] }}</td>
          <td class="rule">{{ change[2].value if change[2] else '' }}</td>
        </tr>
        {% endfor %}
      {% else %}
        <tr><td>{{ rd.key | join(',') }}</td><td>{{ rd.kind }}</td><td colspan="4"></td></tr>
      {% endif %}
    {% endfor %}
  </table>
  {% endif %}

  {% if result.warnings %}
  <h3>Warnings</h3>
  <ul>
    {% for w in result.warnings %}
    <li class="warning">{{ w.message }}{% if w.rule %} ({{ w.rule.value }}){% endif %}</li>
    {% endfor %}
  </ul>
  {% endif %}
</div>

<div class="card meta">
  <h2>Run metadata</h2>
  <table>
    <tr><th>who</th><td>{{ generated_by }}</td></tr>
    <tr><th>when</th><td>{{ generated_at }}</td></tr>
    <tr><th>tablediff version</th><td>{{ tablediff_version }}</td></tr>
    <tr><th>python version</th><td>{{ python_version }}</td></tr>
    <tr><th>source DSN</th><td>{{ source_dsn_redacted }}</td></tr>
    <tr><th>target DSN</th><td>{{ target_dsn_redacted }}</td></tr>
  </table>
</div>

<div class="card">
  <h2>Reproduce</h2>
  {% if sql_statements %}
  <div class="sql">{{ sql_statements | join('\\n\\n') }}</div>
  {% else %}
  <p class="meta">No SQL statements were captured for this run.</p>
  {% endif %}
</div>

</body>
</html>
"""
)


def render_html(
    result: DiffResult,
    *,
    source_dsn: str,
    target_dsn: str,
    sql_statements: list[str] | None = None,
    generated_at: datetime | None = None,
    generated_by: str | None = None,
) -> str:
    """spec §8.3's HTML sign-off report, built from the same DiffResult
    the terminal/JSON renderers use. Self-contained (inline CSS, no
    external assets/fonts/JS) so it opens from a plain file share with no
    network, per spec §3's connectivity requirement for this format.
    """
    status = "MATCH" if result.is_match else "DIFFERENT"
    when = generated_at or datetime.now(timezone.utc)
    who = generated_by or getpass.getuser()
    return _TEMPLATE.render(
        result=result,
        status=status,
        generated_at=when.isoformat(timespec="seconds"),
        generated_by=who,
        tablediff_version=tablediff.__version__,
        python_version=platform.python_version(),
        source_dsn_redacted=redact_dsn(source_dsn),
        target_dsn_redacted=redact_dsn(target_dsn),
        sql_statements=sql_statements or [],
    )
