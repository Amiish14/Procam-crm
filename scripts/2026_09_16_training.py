#!/usr/bin/env python3
"""
Training Academy tables — §73-81.

    python scripts/2026_09_16_training.py --check
    python scripts/2026_09_16_training.py

Creates training_progress and training_certificates.  No production table
is touched, and none ever will be: §77 requires training to be invisible
to dashboards, reports and My Work, and the safest guarantee is that the
Academy writes nowhere else.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
app, db = _main.app, _main.db

from app.models.training import TrainingProgress, Certificate  # noqa: E402
from app.training.content import LEVELS                        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    dry = args.check

    with app.app_context():
        names = set(db.inspect(db.engine).get_table_names())
        for model in (TrainingProgress, Certificate):
            table = model.__tablename__
            if table in names:
                print(f'{table} — already present')
                continue
            print(f'CREATE TABLE {table}' + ('   (dry run)' if dry else ''))
            if not dry:
                model.__table__.create(db.engine)

        print(f'\nLevels available: {len(LEVELS)}')
        for level in LEVELS:
            print(f'  {level["level"]:>2}. {level["title"]}')

        if not dry:
            print(f'\nEnrolled so far: '
                  f'{TrainingProgress.query.count()} progress row(s), '
                  f'{Certificate.query.count()} certificate(s)')
        print('\n' + ('Dry run — nothing written.' if dry else '✓ applied'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
