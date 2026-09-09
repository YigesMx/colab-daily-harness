#!/usr/bin/env python3
"""Fail closed if the publishable tree contains local secrets or private identities."""
from pathlib import Path
import os
import re
import sys

from dotenv import dotenv_values

EXCLUDED = {".local", ".git", ".venv", "state", "working_tmp", "crawl_tmp", "node_modules", "dist", "__pycache__"}
EXCLUDED_FILES = {".env", ".repo_private_key", ".repo_private_key.pub", "refact.jsonl"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".sh", ".mjs", ".mts", ".ts", ".vue", ".css", ".html", ".txt", ".example", ".prompt"}


def publishable_files(root):
    for path in root.rglob("*"):
        if any(part in EXCLUDED for part in path.relative_to(root).parts) or path.name in EXCLUDED_FILES:
            continue
        if path.is_file() and (path.suffix.lower() in TEXT_SUFFIXES or path.name in {"AGENTS.md", ".env.example"}):
            yield path


def private_values(root, environ=None):
    values = []
    private_words = ("TOKEN", "SECRET", "PASSWORD", "WEBHOOK", "API_KEY", "PRIVATE", "EMAIL", "OWNER", "DOMAIN", "URL", "ID")
    environment = dict(os.environ if environ is None else environ)
    configuration = {**dotenv_values(root / ".env"), **environment}
    for key, value in configuration.items():
        if isinstance(value, str) and len(value) >= 8 and any(word in key.upper() for word in private_words):
            values.append(value)
    return values


def text_finding(text, secrets):
    if any(secret in text for secret in secrets):
        return "private configuration value"
    if re.search(r"/(?:home|Users)/[A-Za-z0-9._-]+/", text):
        return "absolute personal home path"
    if re.search(r"(?i)authorization\s*:\s*(?:bearer\s+)?[A-Za-z0-9._~+/-]{16,}", text):
        return "credential material"
    if re.search(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----", text):
        return "private key material"
    return None


def scan_file_map(root, files, environ=None):
    """Scan an exact prospective public write set without printing matched values."""
    secrets = private_values(root, environ)
    findings = []
    for name, data in files.items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        kind = text_finding(text, secrets)
        if kind:
            findings.append((Path(name), kind))
    return findings


def scan(root, environ=None):
    secrets = private_values(root, environ)
    findings = []
    for path in publishable_files(root):
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        kind = text_finding(text, secrets)
        if kind:
            findings.append((path, kind))
    return findings


def main(argv=None):
    root = Path((argv or sys.argv[1:] or [os.environ.get("PROJECT_ROOT", ".")])[0]).resolve()
    findings = scan(root)
    if findings:
        for path, kind in findings:
            print(f"{path.relative_to(root)}: {kind}", file=sys.stderr)
        return 1
    print("Public tree scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
