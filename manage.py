#!/usr/bin/env python
"""Utility da riga di comando di Django."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django non è installato. Attiva l'ambiente virtuale e lancia "
            "'pip install -r requirements.txt'."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
