#!/usr/bin/env bash
# 제출용 소스만 result/에 압축합니다. 기존 DB와 인증 파일은 보존하고 제외합니다.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
"$PYTHON" - <<'PYCODE'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
root = Path.cwd()
output = root / "result" / "task-1" / "G105_runbook_source.zip"
output.parent.mkdir(parents=True, exist_ok=True)
files = [root / name for name in (
    "README.md", ".gitignore", ".env.example", "requirements.txt", "server.py",
    "mcp_server.py", "mcp_server_docs.py", "web_chat.html", "web_demo_server.py",
    "eval/results/.gitkeep")]
for folder in ("agent", "tests", "data", "eval", "sh"):
    files.extend(path for path in (root / folder).rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts
                 and not path.relative_to(root).as_posix().startswith("eval/results/")
                 and path.suffix in {".py", ".md", ".json", ".sh", ".cjs"})
files.append(root / "doc" / "RAG_GUIDE.md")
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for path in sorted(files):
        if path.is_symlink():
            raise ValueError(f"Symbolic link is not allowed: {path.name}")
        archive.write(path, Path("runbook-agent") / path.relative_to(root))
with ZipFile(output) as archive:
    assert archive.testzip() is None
    for name in archive.namelist():
        assert not name.endswith((".db", ".pyc", ".log"))
        assert "/.env" not in name or name.endswith("/.env.example")
print(output.relative_to(root))
PYCODE
