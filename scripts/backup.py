#!/usr/bin/env python3
"""Consistent SQLite backup, including transactions currently in WAL."""

import argparse
import os
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
if args.source.resolve() == args.destination.resolve():
    parser.error("Source and destination must differ")
if not args.source.is_file():
    parser.error("Source database does not exist")
os.umask(0o077)
# Refuse to overwrite an existing backup.
fd = os.open(args.destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect(f"file:{args.source}?mode=ro", uri=True) as source:
    with sqlite3.connect(args.destination) as destination:
        source.backup(destination)
print(f"Backup saved to {args.destination}")
