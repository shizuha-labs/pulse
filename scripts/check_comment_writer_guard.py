#!/usr/bin/env python3
"""CI entry point for the PLS-478 direct-comment writer source guard."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tasks.comment_writer_guard import assert_no_direct_comment_writer_drift


if __name__ == '__main__':
    assert_no_direct_comment_writer_drift(ROOT)
    print('Direct comment writer inventory: OK')
