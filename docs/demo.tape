# vhs tape script for the README's 60-second demo GIF.
# Render with: vhs docs/demo.tape   (needs ttyd + ffmpeg on PATH)
# Requires the docker-compose Postgres/ClickHouse services running first
# (docker compose up -d) with a seeded `orders` table on each side, and
# `rowproof` importable by whatever Python resolves on PATH (pip install
# rowproof, or a dev checkout's venv activated first).

Output docs/demo.gif

Set Shell "bash"
Set FontSize 20
Set Width 1200
Set Height 500
Set Padding 20
Set TypingSpeed 40ms

Hide
Type "alias rowproof='/c/dev/tablediff/.venv/Scripts/python.exe -m rowproof.cli.main'"
Enter
Type "clear"
Enter
Show

Type "rowproof diff postgres://postgres:postgres@127.0.0.1:5432/postgres/public.orders \"
Enter
Type "  clickhouse://default:clickhouse@127.0.0.1:8123/default/orders --key id"
Sleep 500ms
Enter
Sleep 4s
