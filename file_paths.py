"""Native Windows extended paths for file IO, not for display/project identity."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path


def extended_windows_path(value):
    text = os.fspath(value).replace('/', '\\')
    if text.startswith('\\\\?\\'):
        return text
    text = ntpath.abspath(text)
    if text.startswith('\\\\'):
        return '\\\\?\\UNC\\' + text[2:]
    return '\\\\?\\' + text


def io_path(value):
    return Path(extended_windows_path(value)) if os.name == 'nt' else Path(value)
