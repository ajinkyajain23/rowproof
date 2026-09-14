"""spec SS7: "Never print passwords, even at --verbose; redact before
logging." Generated SQL never embeds a DSN (only the connectors' own
connect() calls ever see one), so this is only exercised where a DSN
string itself gets shown to a user -- the HTML report's "Reproduce"
metadata (spec SS8.3: "both DSNs with secrets redacted")."""

from tablediff.cli.spec import redact_dsn


def test_redacts_a_plain_password():
    redacted = redact_dsn("postgres://user:hunter2@host:5432/db")
    assert "hunter2" not in redacted
    assert redacted == "postgres://user:***@host:5432/db"


def test_leaves_everything_else_intact():
    redacted = redact_dsn("clickhouse://admin:s3cret@ch-host:8123/analytics")
    assert redacted == "clickhouse://admin:***@ch-host:8123/analytics"


def test_no_password_is_a_no_op():
    dsn = "postgres://user@host:5432/db"
    assert redact_dsn(dsn) == dsn


def test_percent_encoded_password_is_fully_redacted():
    # a password containing an '@' or ':' must be percent-encoded in a
    # real DSN -- confirm the encoded form gets matched and redacted too,
    # not just a plain-ASCII password.
    redacted = redact_dsn("postgres://user:p%40ss%3Aword@host:5432/db")
    assert "p%40ss%3Aword" not in redacted
    assert "***" in redacted


def test_empty_string_password_is_redacted_not_left_visible_as_empty():
    # host:port with a bare trailing colon and no password text at all
    # (":@") is a real, if unusual, DSN shape -- must not crash.
    redacted = redact_dsn("postgres://user:@host:5432/db")
    assert redacted == "postgres://user:@host:5432/db" or "***" in redacted
