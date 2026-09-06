"""Training Academy — progress, scoring and certification (§74-80).

§77: nothing in this module writes to leads, companies, opportunities,
tasks or any other production table.  Practice answers are checked here
against the content definition, so there is no training record that could
reach a dashboard, a report or My Work — and therefore no filter anyone
can forget to apply.
"""
import hashlib
from datetime import datetime

from app import db
from app.models.training import (TrainingProgress, TrainingStatus,
                                 Certificate)
from app.training.content import LEVELS, BY_KEY, PASS_MARK, CONTENT_VERSION


def progress_for(emp_code):
    rows = {p.level_key: p for p in
            TrainingProgress.query.filter_by(emp_code=emp_code).all()}
    out = []
    for level in LEVELS:
        row = rows.get(level['key'])
        out.append({
            'key': level['key'], 'level': level['level'],
            'title': level['title'], 'why': level['why'],
            'status': row.status if row else TrainingStatus.NOT_STARTED,
            'practice_score': row.practice_score if row else None,
            'quiz_score': row.quiz_score if row else None,
            'attempts': (row.attempts or 0) if row else 0,
            'completed_at': (str(row.completed_at)[:10]
                             if row and row.completed_at else ''),
            'stale': bool(row and row.content_version != CONTENT_VERSION),
        })
    return out


def summary_for(emp_code):
    rows = progress_for(emp_code)
    done = [r for r in rows if r['status'] == TrainingStatus.COMPLETED]
    scores = [r['quiz_score'] for r in done if r['quiz_score'] is not None]
    cert = Certificate.query.filter_by(emp_code=emp_code,
                                       revoked_at=None).first()
    return {
        'levels': len(rows),
        'completed': len(done),
        'percent': round(len(done) / len(rows) * 100) if rows else 0,
        'average_score': round(sum(scores) / len(scores)) if scores else None,
        'certified': cert is not None,
        'certificate': cert.to_dict() if cert else None,
    }


def _row(emp_code, level_key):
    row = TrainingProgress.query.filter_by(emp_code=emp_code,
                                           level_key=level_key).first()
    if row is None:
        row = TrainingProgress(emp_code=emp_code, level_key=level_key,
                               status=TrainingStatus.IN_PROGRESS,
                               content_version=CONTENT_VERSION)
        db.session.add(row)
    return row


def mark_learned(emp_code, level_key):
    if level_key not in BY_KEY:
        raise ValueError('No such level')
    row = _row(emp_code, level_key)
    row.learned_at = row.learned_at or datetime.utcnow()
    row.content_version = CONTENT_VERSION
    if row.status == TrainingStatus.NOT_STARTED:
        row.status = TrainingStatus.IN_PROGRESS
    db.session.commit()
    return row


def check_practice(emp_code, level_key, answer):
    """§76 — the learner does the exercise and it is validated.

    Checked against the content definition, never against production data,
    so a wrong answer costs nothing but a retry.
    """
    level = BY_KEY.get(level_key)
    if level is None:
        raise ValueError('No such level')
    practice = level['practice']

    correct = False
    feedback = ''
    if practice['kind'] == 'number':
        try:
            int(str(answer).strip())
            correct = True
        except (TypeError, ValueError):
            feedback = 'That is not a number — type the figure you can see.'
    else:
        try:
            correct = int(answer) == practice['answer']
        except (TypeError, ValueError):
            correct = False
        if not correct:
            feedback = practice.get('hint', '')

    row = _row(emp_code, level_key)
    row.attempts = (row.attempts or 0) + 1
    if correct:
        row.practised_at = datetime.utcnow()
        row.practice_score = 100
    db.session.commit()
    return correct, feedback


def check_quiz(emp_code, level_key, answers):
    """§74 VALIDATE — the assessment, and the gate on completing a level."""
    level = BY_KEY.get(level_key)
    if level is None:
        raise ValueError('No such level')
    questions = level['quiz']

    right = 0
    detail = []
    for i, (text, options, correct_index) in enumerate(questions):
        given = None
        try:
            given = int(answers[i])
        except (IndexError, TypeError, ValueError, KeyError):
            given = None
        ok = (given == correct_index)
        right += 1 if ok else 0
        detail.append({'question': text, 'correct': ok,
                       'answer': options[correct_index]})

    score = round(right / len(questions) * 100) if questions else 0
    row = _row(emp_code, level_key)
    row.attempts = (row.attempts or 0) + 1
    row.quiz_score = max(row.quiz_score or 0, score)
    row.content_version = CONTENT_VERSION

    passed = score >= PASS_MARK and row.practice_score
    if passed:
        row.status = TrainingStatus.COMPLETED
        row.completed_at = row.completed_at or datetime.utcnow()
    db.session.commit()

    return {
        'score': score, 'passed': bool(passed), 'pass_mark': PASS_MARK,
        'detail': detail,
        'needs_practice': not row.practice_score,
    }


def issue_certificate(emp_code, emp_name):
    """§79 — awarded only when every level is complete."""
    rows = progress_for(emp_code)
    done = [r for r in rows if r['status'] == TrainingStatus.COMPLETED]
    if len(done) < len(rows):
        return None, (f'{len(rows) - len(done)} level(s) still to '
                      f'complete')

    existing = Certificate.query.filter_by(emp_code=emp_code,
                                           revoked_at=None).first()
    if existing is not None:
        return existing, None

    scores = [r['quiz_score'] for r in done if r['quiz_score'] is not None]
    average = round(sum(scores) / len(scores)) if scores else 0

    # Deterministic and verifiable: the same person and version always
    # yields the same id, so a printed certificate can be checked.
    digest = hashlib.sha256(
        f'{emp_code}|{CONTENT_VERSION}|{len(done)}'.encode()).hexdigest()
    cert = Certificate(
        certificate_id=f'PCM-{digest[:8].upper()}-{len(done):02d}',
        emp_code=emp_code, emp_name=emp_name, levels_passed=len(done),
        average_score=average, content_version=CONTENT_VERSION)
    db.session.add(cert)
    db.session.commit()
    return cert, None


def management_view():
    """§80 — where the whole team stands."""
    from app import Employee

    people = Employee.query.filter_by(is_active=True)\
                           .order_by(Employee.name).all()
    certs = {c.emp_code: c for c in
             Certificate.query.filter_by(revoked_at=None).all()}

    rows = []
    for emp in people:
        s = summary_for(emp.emp_code)
        rows.append({
            'emp_code': emp.emp_code, 'name': emp.name,
            'vertical': emp.vertical or '', 'completed': s['completed'],
            'levels': s['levels'], 'percent': s['percent'],
            'average_score': s['average_score'],
            'certified': emp.emp_code in certs,
            'started': s['completed'] > 0 or TrainingProgress.query.filter_by(
                emp_code=emp.emp_code).count() > 0,
        })

    return {
        'people': rows,
        'total': len(rows),
        'started': sum(1 for r in rows if r['started']),
        'certified': sum(1 for r in rows if r['certified']),
        'not_started': sum(1 for r in rows if not r['started']),
    }
