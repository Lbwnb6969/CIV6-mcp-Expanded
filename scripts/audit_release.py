"""Audit the tracked public source and built archives, without printing secrets."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import tarfile
import zipfile

root = Path(__file__).resolve().parent.parent
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True).split("\0")
patterns = {
    "github_token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_secret": re.compile(rb"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{25,}"),
    "private_local_path": re.compile(rb"[CD]:[/\\]Users[/\\]Administrator[/\\]", re.I),
}


def check(name, raw):
    lower = name.lower()
    if any(s in lower for s in (".git/", ".venv/", "native-evidence/", "__pycache__/")) or lower.endswith((".civ6save", ".civ6map", ".log", ".env")):
        raise ValueError("Forbidden release member: " + name)
    for label, pattern in patterns.items():
        if pattern.search(raw):
            raise ValueError("Sensitive content category " + label + " in " + name)


count = 0
for name in filter(None, tracked):
    check(name, (root / name).read_bytes())
    count += 1
artifacts = []
for path in sorted((root / "dist").glob("*")):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                check(name, archive.read(name))
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    check(member.name, archive.extractfile(member).read())
    else:
        continue
    artifacts.append({"name": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
print(json.dumps({"tracked_files": count, "sensitive_categories": list(patterns), "status": "PASS", "artifacts": artifacts}, indent=2))
