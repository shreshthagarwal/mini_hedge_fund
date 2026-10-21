"""
env.py — load KEY=VALUE pairs from a .env file into os.environ.

Values already set in the real environment win, so a server can override
anything in the file. Lines starting with # and blank lines are ignored.
"""

import os


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
