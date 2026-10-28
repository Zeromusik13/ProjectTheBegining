from email.message import EmailMessage
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
import csv
from functools import wraps
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import smtplib
import ssl
from datetime import datetime
import time
import uuid
from contextlib import closing
from werkzeug.security import check_password_hash, generate_password_hash

TEMP_DIR = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp')
os.makedirs(TEMP_DIR, exist_ok=True)
LEGACY_RESULTS_FILE = os.path.join(TEMP_DIR, 'english_test_results.csv')
LOCAL_APP_DATA = os.environ.get('LOCALAPPDATA') or os.path.join(
    os.path.expanduser('~'), 'AppData', 'Local'
)
ADMIN_DATA_DIR = os.path.join(LOCAL_APP_DATA, 'EnglishPlacementTest')
os.makedirs(ADMIN_DATA_DIR, exist_ok=True)
ADMIN_CREDENTIALS_FILE = os.path.join(ADMIN_DATA_DIR, 'admin_credentials.json')
ACTIVITY_DB_FILE = os.path.join(ADMIN_DATA_DIR, 'active_students.sqlite3')
APP_SECRET_KEY_FILE = os.path.join(ADMIN_DATA_DIR, 'flask_secret_key')
STUDENT_ACTIVITY_TIMEOUT = 30
PERSONAL_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{1,30}[A-Za-z0-9]$')
PROFILE_SEX_OPTIONS = ('Female', 'Male', 'Prefer not to say')


def get_app_secret_key():
    environment_key = os.environ.get('FLASK_SECRET_KEY')
    if environment_key:
        return environment_key

    try:
        with open(APP_SECRET_KEY_FILE, encoding='utf-8') as key_file:
            secret_key = key_file.read().strip()
    except FileNotFoundError:
        secret_key = secrets.token_hex(32)
        try:
            with open(APP_SECRET_KEY_FILE, 'x', encoding='utf-8') as key_file:
                key_file.write(secret_key)
                key_file.flush()
                os.fsync(key_file.fileno())
        except FileExistsError:
            with open(APP_SECRET_KEY_FILE, encoding='utf-8') as key_file:
                secret_key = key_file.read().strip()

    if not secret_key:
        raise ValueError(f'Flask secret key file is empty: {APP_SECRET_KEY_FILE}')
    return secret_key


app = Flask(__name__)
app.secret_key = get_app_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)


def initialize_activity_store():
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS institutions (
                institution_id TEXT PRIMARY KEY,
                institution_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            INSERT OR IGNORE INTO institutions (
                institution_id, institution_code, name, created_at
            )
            VALUES ('main', 'MAIN', 'Main Institution', ?)
            ''',
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS admin_accounts (
                username TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (institution_id, username)
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS active_students (
                monitor_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL DEFAULT 'main',
                name TEXT NOT NULL,
                book TEXT NOT NULL,
                level TEXT NOT NULL,
                exam TEXT NOT NULL DEFAULT 'Midterm Exam',
                started_at TEXT NOT NULL,
                answered_count INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                tab_switches INTEGER NOT NULL,
                answers_json TEXT NOT NULL,
                last_seen REAL NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS writing_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT NOT NULL DEFAULT 'main',
                name TEXT NOT NULL,
                book TEXT NOT NULL,
                level TEXT NOT NULL,
                responses_json TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS writing_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL UNIQUE,
                grammar_score INTEGER NOT NULL CHECK (grammar_score BETWEEN 1 AND 5),
                vocabulary_score INTEGER NOT NULL CHECK (vocabulary_score BETWEEN 1 AND 5),
                organization_score INTEGER NOT NULL CHECK (organization_score BETWEEN 1 AND 5),
                mechanics_score INTEGER NOT NULL CHECK (mechanics_score BETWEEN 1 AND 5),
                task_score INTEGER NOT NULL CHECK (task_score BETWEEN 1 AND 5),
                teacher_comments TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (submission_id) REFERENCES writing_submissions(id)
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS exam_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT NOT NULL DEFAULT 'main',
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                percentage INTEGER NOT NULL,
                tab_switches INTEGER NOT NULL,
                level TEXT NOT NULL,
                book TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS exam_interruptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id TEXT NOT NULL UNIQUE,
                institution_id TEXT NOT NULL DEFAULT 'main',
                name TEXT NOT NULL,
                book TEXT NOT NULL,
                level TEXT NOT NULL,
                exam TEXT NOT NULL,
                reason TEXT NOT NULL,
                ended_by TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                answered_count INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                tab_switches INTEGER NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS teacher_exam_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT NOT NULL DEFAULT 'main',
                teacher_label TEXT NOT NULL,
                book TEXT NOT NULL,
                level TEXT NOT NULL,
                exam TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                percentage REAL NOT NULL,
                tab_switches INTEGER NOT NULL,
                writing_responses_json TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS student_accounts (
                institution_id TEXT NOT NULL DEFAULT 'main',
                student_id TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                date_of_birth TEXT,
                sex TEXT,
                physical_address TEXT,
                PRIMARY KEY (institution_id, student_id),
                UNIQUE (institution_id, email)
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS student_removals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT NOT NULL DEFAULT 'main',
                student_id TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                reason TEXT NOT NULL,
                removed_by TEXT NOT NULL,
                removed_at TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS teacher_accounts (
                institution_id TEXT NOT NULL DEFAULT 'main',
                username TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                date_of_birth TEXT,
                sex TEXT,
                physical_address TEXT,
                PRIMARY KEY (institution_id, username),
                UNIQUE (institution_id, employee_id),
                UNIQUE (institution_id, email)
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS teacher_username_counters (
                initials TEXT PRIMARY KEY,
                last_number INTEGER NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS employee_id_counters (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_number INTEGER NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS support_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_type TEXT NOT NULL,
                requester_id TEXT NOT NULL DEFAULT '',
                requester_name TEXT NOT NULL,
                requester_email TEXT NOT NULL,
                institution_id TEXT NOT NULL DEFAULT 'main',
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                email_status TEXT NOT NULL DEFAULT 'pending',
                email_error TEXT NOT NULL DEFAULT ''
            )
            '''
        )
        connection.execute(
            'INSERT OR IGNORE INTO employee_id_counters (id, last_number) VALUES (1, 0)'
        )
        teacher_column_info = connection.execute(
            'PRAGMA table_info(teacher_accounts)'
        ).fetchall()
        teacher_columns = {row[1] for row in teacher_column_info}
        if 'employee_id' not in teacher_columns:
            connection.execute(
                'ALTER TABLE teacher_accounts ADD COLUMN employee_id TEXT'
            )
            teacher_column_info = connection.execute(
                'PRAGMA table_info(teacher_accounts)'
            ).fetchall()
            teacher_columns = {row[1] for row in teacher_column_info}
        existing_teachers = connection.execute(
            '''
            SELECT rowid FROM teacher_accounts
            WHERE employee_id IS NULL OR employee_id = ''
            ORDER BY created_at, username
            '''
        ).fetchall()
        employee_counter = connection.execute(
            'SELECT last_number FROM employee_id_counters WHERE id = 1'
        ).fetchone()[0]
        existing_employee_ids = connection.execute(
            '''
            SELECT employee_id FROM teacher_accounts
            WHERE employee_id GLOB 'EMP-[0-9]*'
            '''
        ).fetchall()
        employee_counter = max(
            employee_counter,
            max(
                (
                    int(employee_id[4:])
                    for (employee_id,) in existing_employee_ids
                    if employee_id[4:].isdigit()
                ),
                default=0
            )
        )
        for (teacher_rowid,) in existing_teachers:
            employee_counter += 1
            connection.execute(
                'UPDATE teacher_accounts SET employee_id = ? WHERE rowid = ?',
                (f'EMP-{employee_counter:06d}', teacher_rowid)
            )
        connection.execute(
            'UPDATE employee_id_counters SET last_number = ? WHERE id = 1',
            (employee_counter,)
        )
        student_columns = {
            row[1] for row in connection.execute(
                'PRAGMA table_info(student_accounts)'
            ).fetchall()
        }
        if 'institution_id' not in student_columns:
            student_profile_expressions = ', '.join(
                column if column in student_columns else 'NULL'
                for column in ('date_of_birth', 'sex', 'physical_address')
            )
            connection.execute('ALTER TABLE student_accounts RENAME TO student_accounts_legacy')
            connection.execute(
                '''
                CREATE TABLE student_accounts (
                    institution_id TEXT NOT NULL DEFAULT 'main',
                    student_id TEXT NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    date_of_birth TEXT,
                    sex TEXT,
                    physical_address TEXT,
                    PRIMARY KEY (institution_id, student_id),
                    UNIQUE (institution_id, email)
                )
                '''
            )
            connection.execute(
                '''
                INSERT INTO student_accounts (
                    institution_id, student_id, first_name, last_name, email,
                    password_hash, created_at, updated_at, date_of_birth, sex,
                    physical_address
                )
                SELECT 'main', student_id, first_name, last_name, email,
                       password_hash, created_at, updated_at,
                       {student_profile_expressions}
                FROM student_accounts_legacy
                '''.format(student_profile_expressions=student_profile_expressions)
            )
            connection.execute('DROP TABLE student_accounts_legacy')
        teacher_column_info = connection.execute(
            'PRAGMA table_info(teacher_accounts)'
        ).fetchall()
        teacher_primary_key = frozenset(
            row[1] for row in teacher_column_info if row[5]
        )
        teacher_unique_keys = set()
        for index in connection.execute(
            'PRAGMA index_list(teacher_accounts)'
        ).fetchall():
            if index[2]:
                index_name = index[1].replace('"', '""')
                index_columns = connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
                teacher_unique_keys.add(
                    frozenset(row[2] for row in index_columns)
                )
        if teacher_primary_key:
            teacher_unique_keys.add(teacher_primary_key)
        expected_teacher_keys = {
            frozenset(('institution_id', 'username')),
            frozenset(('institution_id', 'employee_id')),
            frozenset(('institution_id', 'email'))
        }
        teacher_account_needs_migration = (
            'institution_id' not in teacher_columns
            or any(
                not row[3]
                for row in teacher_column_info
                if row[1] == 'employee_id'
            )
            or not expected_teacher_keys.issubset(teacher_unique_keys)
            or any(
                key in teacher_unique_keys
                for key in (
                    frozenset(('username',)),
                    frozenset(('employee_id',)),
                    frozenset(('email',))
                )
            )
        )
        if teacher_account_needs_migration:
            teacher_institution_expression = (
                "COALESCE(NULLIF(institution_id, ''), 'main')"
                if 'institution_id' in teacher_columns else "'main'"
            )
            teacher_profile_expressions = ', '.join(
                column if column in teacher_columns else 'NULL'
                for column in ('date_of_birth', 'sex', 'physical_address')
            )
            connection.execute('ALTER TABLE teacher_accounts RENAME TO teacher_accounts_legacy')
            connection.execute(
                '''
                CREATE TABLE teacher_accounts (
                    institution_id TEXT NOT NULL DEFAULT 'main',
                    username TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    date_of_birth TEXT,
                    sex TEXT,
                    physical_address TEXT,
                    PRIMARY KEY (institution_id, username),
                    UNIQUE (institution_id, employee_id),
                    UNIQUE (institution_id, email)
                )
                '''
            )
            connection.execute(
                '''
                INSERT INTO teacher_accounts (
                    institution_id, username, employee_id, first_name, last_name,
                    email, password_hash, must_change_password, created_at, updated_at,
                    date_of_birth, sex, physical_address
                )
                SELECT {teacher_institution_expression}, username, employee_id,
                       first_name, last_name, email, password_hash,
                       must_change_password, created_at, updated_at,
                       {teacher_profile_expressions}
                FROM teacher_accounts_legacy
                '''.format(
                    teacher_institution_expression=teacher_institution_expression,
                    teacher_profile_expressions=teacher_profile_expressions
                )
            )
            connection.execute('DROP TABLE teacher_accounts_legacy')
        for table in (
            'active_students', 'writing_submissions', 'exam_results',
            'exam_interruptions', 'teacher_exam_attempts', 'student_removals'
        ):
            columns = {
                row[1] for row in connection.execute(
                    f'PRAGMA table_info({table})'
                ).fetchall()
            }
            if 'institution_id' not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN institution_id TEXT NOT NULL DEFAULT 'main'"
                )
        for table in ('active_students', 'writing_submissions', 'exam_results'):
            columns = {
                row[1] for row in connection.execute(
                    f'PRAGMA table_info({table})'
                ).fetchall()
            }
            if 'exam' not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN exam TEXT NOT NULL DEFAULT 'Midterm Exam'"
                )
        active_columns = {
            row[1] for row in connection.execute(
                'PRAGMA table_info(active_students)'
            ).fetchall()
        }
        if 'student_id' not in active_columns:
            connection.execute(
                'ALTER TABLE active_students ADD COLUMN student_id TEXT'
            )
        for table in ('student_accounts', 'teacher_accounts'):
            columns = {
                row[1] for row in connection.execute(
                    f'PRAGMA table_info({table})'
                ).fetchall()
            }
            for column in ('date_of_birth', 'sex', 'physical_address'):
                if column not in columns:
                    connection.execute(
                        f'ALTER TABLE {table} ADD COLUMN {column} TEXT'
                    )
        connection.commit()


def migrate_legacy_results():
    required_fields = {
        'first_name', 'last_name', 'email', 'score',
        'total', 'percentage', 'submitted_at'
    }
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            migrated = connection.execute(
                'SELECT 1 FROM app_metadata WHERE key = ?',
                ('legacy_results_csv_migrated',)
            ).fetchone()
            if migrated:
                return

            if os.path.exists(LEGACY_RESULTS_FILE):
                with open(LEGACY_RESULTS_FILE, newline='', encoding='utf-8-sig') as csv_file:
                    reader = csv.DictReader(csv_file)
                    missing_fields = required_fields.difference(reader.fieldnames or [])
                    if missing_fields:
                        missing = ', '.join(sorted(missing_fields))
                        raise ValueError(
                            f'Legacy results file is missing required columns: {missing}'
                        )

                    for line_number, row in enumerate(reader, start=2):
                        if row is None:
                            raise ValueError(
                                f'Invalid row in legacy results file at line {line_number}'
                            )
                        try:
                            result = (
                                row['first_name'] or '',
                                row['last_name'] or '',
                                row['email'] or '',
                                int(row['score']),
                                int(row['total']),
                                int(row['percentage']),
                                int(row.get('tab_switches') or 0),
                                row.get('level') or 'Beginner',
                                row.get('book') or 'Interchange Intro',
                                row['submitted_at'] or ''
                            )
                        except (TypeError, ValueError) as error:
                            raise ValueError(
                                f'Invalid result data in legacy results file at line {line_number}'
                            ) from error

                        connection.execute(
                            '''
                            INSERT INTO exam_results (
                                first_name, last_name, email, score, total,
                                percentage, tab_switches, level, book, submitted_at, exam
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''',
                            (*result, 'Midterm Exam')
                        )

            connection.execute(
                'INSERT INTO app_metadata (key, value) VALUES (?, ?)',
                ('legacy_results_csv_migrated', '1')
            )


initialize_activity_store()
migrate_legacy_results()

LEVELS = ['Intro', '1', '2', '3', 'Passages']
BOOK_LEVELS = {
    'Interchange Intro': 'Intro',
    'Interchange 1': '1',
    'Interchange 2': '2',
    'Interchange 3': '3',
    'Interchange Passages': 'Passages'
}
LEVEL_BOOKS = {level: book for book, level in BOOK_LEVELS.items()}
LEVEL_ALIASES = {
    'Beginner': 'Intro',
    'Intermediate': '2',
    'Advanced': '3',
    'interchange intro': 'Intro',
    'interchange 1': '1',
    'interchange 2': '2',
    'interchange 3': '3',
    'passages': 'Passages',
    'interchange passages': 'Passages'
}


def normalize_book(value):
    if value is None:
        return 'Interchange Intro'

    normalized = str(value).strip()
    if not normalized:
        return 'Interchange Intro'

    lower = normalized.lower()
    for book_name, level in BOOK_LEVELS.items():
        if lower == book_name.lower():
            return book_name
        if lower == f'interchange {level.lower()}':
            return book_name
        if lower == level.lower():
            return book_name

    if lower.startswith('interchange '):
        key = lower.replace('interchange ', '', 1)
        if key in {'intro', '1', '2', '3', 'passages'}:
            return next(book for book, level in BOOK_LEVELS.items() if level.lower() == key)

    return 'Interchange Intro'


def normalize_level(level):
    if level is None:
        return 'Intro'

    normalized = str(level).strip()
    if not normalized:
        return 'Intro'

    if normalized in LEVELS:
        return normalized

    lowered = normalized.lower()
    if lowered in LEVEL_ALIASES:
        return LEVEL_ALIASES[lowered]

    for key, value in LEVEL_ALIASES.items():
        if normalized.lower() == key.lower():
            return value

    if lowered.startswith('interchange '):
        suffix = lowered.replace('interchange ', '', 1)
        if suffix in {'intro', '1', '2', '3', 'passages'}:
            return BOOK_LEVELS.get(f'Interchange {suffix.title() if suffix not in {"1", "2", "3"} else suffix}', 'Intro')

    return 'Intro'


SECTION_ORDER = [
    'Vocabulary',
    'Spelling',
    'Grammar',
    'Listening',
    'Reading / Comprehension',
    'Writing'
]
SECTION_WEIGHTS = {
    'Grammar': 30,
    'Reading / Comprehension': 25,
    'Listening': 20,
    'Vocabulary': 20,
    'Spelling': 5
}
EXAM_NAMES = ('Midterm Exam', 'Final Exam')
EXAM_DURATION_SECONDS = 60 * 60


def load_questions():
    questions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'questions.json')
    with open(questions_file, encoding='utf-8') as question_file:
        try:
            questions = json.load(question_file)
        except json.JSONDecodeError as error:
            raise ValueError(f'Invalid question data in {questions_file}: {error}') from error

    if not isinstance(questions, list) or not questions:
        raise ValueError(f'Question data must be a non-empty list: {questions_file}')

    question_ids = set()
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            raise ValueError(f'Question {index} must be an object in {questions_file}')

        required_fields = {
            'id', 'book', 'unit', 'topic', 'difficulty', 'section', 'question', 'exam'
        }
        missing_fields = required_fields.difference(question)
        if missing_fields:
            missing = ', '.join(sorted(missing_fields))
            raise ValueError(f'Question {index} is missing {missing} in {questions_file}')

        question_id = question['id']
        if not isinstance(question_id, int) or isinstance(question_id, bool):
            raise ValueError(f'Question {index} has a non-integer id in {questions_file}')
        if question_id in question_ids:
            raise ValueError(f'Duplicate question id {question_id} in {questions_file}')
        question_ids.add(question_id)

        if question['difficulty'] not in LEVELS:
            raise ValueError(f'Question {question_id} has an unknown difficulty in {questions_file}')
        if question['book'] != LEVEL_BOOKS[question['difficulty']]:
            raise ValueError(f'Question {question_id} has a book that does not match its difficulty in {questions_file}')
        if (
            not isinstance(question['unit'], int)
            or isinstance(question['unit'], bool)
            or question['unit'] < 1
        ):
            raise ValueError(f'Question {question_id} must have a positive integer unit in {questions_file}')
        if not isinstance(question['topic'], str) or not question['topic'].strip():
            raise ValueError(f'Question {question_id} must have a non-empty topic in {questions_file}')
        if question['section'] not in SECTION_ORDER:
            raise ValueError(f'Question {question_id} has an unknown section in {questions_file}')
        if question['exam'] not in EXAM_NAMES:
            raise ValueError(f'Question {question_id} has an unknown exam in {questions_file}')
        unit_range = range(1, 9) if question['exam'] == 'Midterm Exam' else range(9, 17)
        if question['unit'] not in unit_range:
            raise ValueError(f'Question {question_id} has a unit outside its exam range in {questions_file}')
        if not isinstance(question['question'], str) or not question['question'].strip():
            raise ValueError(f'Question {question_id} must have question text in {questions_file}')

        if question['section'] == 'Writing':
            if question.get('type') != 'writing':
                raise ValueError(f'Writing question {question_id} must have type "writing" in {questions_file}')
            for field in ('minimum_words', 'maximum_words', 'suggested_time'):
                value = question.get(field)
                if value is not None and (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 1
                ):
                    raise ValueError(
                        f'Writing question {question_id} must have a positive integer {field} '
                        f'in {questions_file}'
                    )
            minimum_words = question.get('minimum_words')
            maximum_words = question.get('maximum_words')
            if (
                minimum_words is not None
                and maximum_words is not None
                and maximum_words < minimum_words
            ):
                raise ValueError(
                    f'Writing question {question_id} has a maximum_words below minimum_words '
                    f'in {questions_file}'
                )
        else:
            options = question.get('options')
            answer = question.get('answer')
            if not isinstance(options, list) or not options or not all(isinstance(option, str) for option in options):
                raise ValueError(f'Question {question_id} must have a non-empty options list in {questions_file}')
            if not isinstance(answer, str) or answer not in options:
                raise ValueError(f'Question {question_id} must have an answer listed in its options in {questions_file}')

    for book in BOOK_LEVELS:
        for exam in EXAM_NAMES:
            exam_questions = [
                question for question in questions
                if question['book'] == book and question['exam'] == exam
            ]
            if len(exam_questions) < 30:
                raise ValueError(
                    f'{book} {exam} must contain at least 30 questions '
                    f'in {questions_file}'
                )
            writing_count = sum(
                question['section'] == 'Writing'
                for question in exam_questions
            )
            if writing_count != 1:
                raise ValueError(
                    f'{book} {exam} must contain exactly 1 writing exercise '
                    f'in {questions_file}'
                )

    return questions


QUESTIONS = load_questions()


def get_questions_for_level(level):
    normalized_level = normalize_level(level)
    return [question for question in QUESTIONS if question.get('difficulty') == normalized_level]


def normalize_exam(value):
    return value if value in EXAM_NAMES else 'Midterm Exam'


def get_questions_for_exam(level, exam):
    normalized_level = normalize_level(level)
    normalized_exam = normalize_exam(exam)
    return [
        question for question in QUESTIONS
        if question.get('difficulty') == normalized_level
        and question.get('exam', 'Midterm Exam') == normalized_exam
    ]


def group_questions_by_section(questions):
    grouped = {section: [] for section in SECTION_ORDER}
    for question in questions:
        section = question.get('section', 'General')
        if section not in grouped:
            grouped[section] = []
        grouped[section].append(question)

    return [(section, grouped[section]) for section in SECTION_ORDER if grouped[section]]


def get_randomized_questions(level='Intro', exam='Midterm Exam'):
    questions = get_questions_for_exam(level, exam)
    order = [question['id'] for question in questions]
    random.shuffle(order)
    return order


def get_session_question_order(level, exam):
    normalized_level = normalize_level(level)
    normalized_exam = normalize_exam(exam)
    questions = get_questions_for_exam(normalized_level, normalized_exam)
    question_ids = [question['id'] for question in questions]
    question_order = session.get('question_order')
    order_is_valid = (
        isinstance(question_order, list)
        and len(question_order) == len(question_ids)
        and all(type(question_id) is int for question_id in question_order)
        and set(question_order) == set(question_ids)
    )
    if (
        session.get('question_order_context')
        != {'level': normalized_level, 'exam': normalized_exam}
        or not order_is_valid
    ):
        question_order = get_randomized_questions(normalized_level, normalized_exam)
        session['question_order'] = question_order
        session['question_order_context'] = {
            'level': normalized_level,
            'exam': normalized_exam
        }
    return question_order


def randomize_question_options(questions):
    randomized_questions = []
    for question in questions:
        randomized_question = question.copy()
        options = question.get('options')
        if options:
            options = options[:]
            random.shuffle(options)
            randomized_question['options'] = options
        randomized_questions.append(randomized_question)
    return randomized_questions


def save_result(first_name, last_name, email, score, total, percentage, tab_switches=0, level='Beginner', book='Interchange Intro', exam='Midterm Exam', institution_id='main'):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            connection.execute(
                '''
                INSERT INTO exam_results (
                    institution_id,
                    first_name, last_name, email, score, total, percentage,
                    tab_switches, level, book, submitted_at, exam
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    institution_id,
                    first_name,
                    last_name,
                    email,
                    score,
                    total,
                    percentage,
                    tab_switches,
                    level,
                    book,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    exam
                )
            )


def save_writing_submission(first_name, last_name, book, level, exam, responses, institution_id='main'):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            connection.execute(
                '''
                INSERT INTO writing_submissions (
                    institution_id, name, book, level, exam, responses_json, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    institution_id,
                    f'{first_name} {last_name}'.strip(),
                    book,
                    level,
                    exam,
                    json.dumps(responses),
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                )
            )


def save_teacher_exam_attempt(teacher_label, book, level, exam, score, total, percentage, tab_switches, writing_responses, institution_id='main'):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            connection.execute(
                '''
                INSERT INTO teacher_exam_attempts (
                    institution_id,
                    teacher_label, book, level, exam, score, total, percentage,
                    tab_switches, writing_responses_json, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    institution_id,
                    teacher_label,
                    book,
                    level,
                    exam,
                    score,
                    total,
                    percentage,
                    tab_switches,
                    json.dumps(writing_responses),
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                )
            )


def load_teacher_exam_attempts(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT teacher_exam_attempts.teacher_label, teacher_exam_attempts.book,
                   teacher_exam_attempts.level, teacher_exam_attempts.exam,
                   teacher_exam_attempts.score, teacher_exam_attempts.total,
                   teacher_exam_attempts.percentage, teacher_exam_attempts.tab_switches,
                   teacher_exam_attempts.writing_responses_json,
                   teacher_exam_attempts.submitted_at,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM teacher_exam_attempts
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR teacher_exam_attempts.institution_id = ?)
            ORDER BY teacher_exam_attempts.id DESC
            ''',
            (institution_id, institution_id)
        ).fetchall()
    return [
        {
            **{
                key: row[key]
                for key in (
                    'teacher_label', 'book', 'level', 'exam', 'score', 'total',
                    'percentage', 'tab_switches', 'submitted_at',
                    'institution_name', 'institution_code'
                )
            },
            'writing_responses': json.loads(row['writing_responses_json'])
        }
        for row in rows
    ]


def load_writing_submissions(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT writing_submissions.name, writing_submissions.book,
                   writing_submissions.level, writing_submissions.exam,
                   writing_submissions.responses_json, writing_submissions.submitted_at,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM writing_submissions
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR writing_submissions.institution_id = ?)
            ORDER BY writing_submissions.id DESC
            ''',
            (institution_id, institution_id)
        ).fetchall()
    return [
        {
            **{
                key: row[key]
                for key in (
                    'name', 'book', 'level', 'exam', 'submitted_at',
                    'institution_name', 'institution_code'
                )
            },
            'responses': json.loads(row['responses_json'])
        }
        for row in rows
    ]


def get_section_scores(questions, form_data):
    section_totals = {}
    section_correct = {}

    for question in questions:
        if question.get('type') == 'writing':
            continue
        section = question.get('section', 'General')
        section_totals[section] = section_totals.get(section, 0) + 1

        selected = form_data.get(f"q{question['id']}")
        if selected == question.get('answer'):
            section_correct[section] = section_correct.get(section, 0) + 1

    scores = []
    for section, weight in SECTION_WEIGHTS.items():
        total = section_totals.get(section, 0)
        correct = section_correct.get(section, 0)
        section_percentage = correct / total * 100 if total else 0
        scores.append({
            'section': section,
            'correct': correct,
            'total': total,
            'weight': weight,
            'percentage': section_percentage,
            'weighted_contribution': section_percentage * weight / 100
        })
    return scores


def get_weighted_percentage(section_scores):
    return round(sum(
        item['weighted_contribution']
        for item in section_scores
    ), 1)


def get_cefr_band_from_percentage(percentage):
    try:
        percentage = float(percentage)
    except (TypeError, ValueError):
        return 'Not available'

    if percentage < 0:
        return 'Not available'
    if percentage >= 80:
        return 'C1-C2'
    if percentage >= 50:
        return 'B1-B2'
    return 'A1-A2'


def load_results(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT exam_results.first_name, exam_results.last_name, exam_results.email,
                   exam_results.score, exam_results.total, exam_results.percentage,
                   exam_results.tab_switches, exam_results.level, exam_results.book,
                   exam_results.exam, exam_results.submitted_at,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM exam_results
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR exam_results.institution_id = ?)
            ORDER BY exam_results.id DESC
            ''',
            (institution_id, institution_id)
        ).fetchall()

    results = [dict(row) for row in rows]
    for result in results:
        result['cefr_band'] = get_cefr_band_from_percentage(result['percentage'])
    return results


def get_dashboard_analytics(institution_id=None):
    today = datetime.now().strftime('%Y-%m-%d')
    placement_distribution = {'A1-A2': 0, 'B1-B2': 0, 'C1-C2': 0}
    percentages = []

    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        students_today = connection.execute(
            '''
            SELECT COUNT(*) FROM exam_results
            WHERE (? IS NULL OR institution_id = ?)
              AND substr(submitted_at, 1, 10) = ?
            ''',
            (institution_id, institution_id, today)
        ).fetchone()[0]
        rows = connection.execute(
            '''
            SELECT percentage FROM exam_results
            WHERE (? IS NULL OR institution_id = ?)
            ''',
            (institution_id, institution_id)
        ).fetchall()

    for (percentage,) in rows:
        band = get_cefr_band_from_percentage(percentage)
        if band in placement_distribution:
            placement_distribution[band] += 1
        try:
            percentages.append(float(percentage))
        except (TypeError, ValueError):
            continue

    average_score = sum(percentages) / len(percentages) if percentages else None
    return {
        'students_today': students_today,
        'average_score': average_score,
        'placement_distribution': placement_distribution
    }


def is_valid_admin_session():
    if session.get('is_admin') is not True:
        return False
    if session.get('is_main_admin') is True:
        return True
    return get_admin_account(
        session.get('admin_username'),
        session.get('institution_id')
    ) is not None


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_valid_admin_session():
            return redirect(url_for('admin_login'))
        return view(*args, **kwargs)

    return wrapped


def main_admin_required(view):
    @wraps(view)
    @admin_required
    def wrapped(*args, **kwargs):
        if session.get('is_main_admin') is not True:
            return 'This action is reserved for the Main Admin.', 403
        return view(*args, **kwargs)

    return wrapped


def current_institution_id():
    return session.get('institution_id', 'main')


def is_main_admin():
    return session.get('is_main_admin') is True


def admin_data_scope():
    return None if is_main_admin() else current_institution_id()


@app.get('/home')
def home():
    if session.get('is_admin') is True:
        if is_valid_admin_session():
            return redirect(url_for('admin_dashboard'))
        session.clear()
        return redirect(url_for('admin_login'))

    if session.get('is_teacher') is True:
        teacher_account = get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if teacher_account is None:
            session.clear()
            return redirect(url_for('teacher_login'))
        if teacher_account['must_change_password']:
            return redirect(url_for('teacher_change_password'))
        if not is_profile_complete(teacher_account):
            return redirect(url_for('profile'))
        return redirect(url_for('teacher_exam_take'))

    student_account = get_student_account(
        session.get('student_id'),
        session.get('institution_id')
    )
    if student_account is not None:
        if not is_profile_complete(student_account):
            return redirect(url_for('profile'))
        exam_access = session.get('student_exam_access')
        if isinstance(exam_access, dict):
            return redirect(url_for(
                'index',
                book=normalize_book(exam_access.get('book')),
                exam=normalize_exam(exam_access.get('exam'))
            ))
        return redirect(url_for('index'))

    return redirect(url_for('student_login'))


def generate_csrf_token():
    token = session.get('_csrf_token')
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.before_request
def validate_csrf_token():
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return None

    expected_token = session.get('_csrf_token')
    provided_token = (
        request.form.get('csrf_token')
        or request.headers.get('X-CSRF-Token')
    )
    if (
        not isinstance(expected_token, str)
        or not isinstance(provided_token, str)
        or not provided_token.isascii()
        or not hmac.compare_digest(expected_token, provided_token)
    ):
        message = 'The CSRF token is missing or invalid.'
        if request.is_json or request.accept_mimetypes.best == 'application/json':
            return jsonify(error=message), 400
        abort(400, description=message)

    return None


@app.before_request
def require_complete_profile():
    if session.get('is_teacher') is not True and not session.get('student_id'):
        return None
    if request.endpoint in {
        'static', 'profile', 'help', 'home', 'student_logout',
        'teacher_logout', 'teacher_change_password'
    }:
        return None
    _, account, _ = get_signed_in_profile()
    if account is not None and not is_profile_complete(account):
        return redirect(url_for('profile'))
    return None


@app.context_processor
def inject_admin_template_helpers():
    return {
        'is_main_admin': is_main_admin,
        'csrf_token': generate_csrf_token,
        'is_signed_in_user': (
            session.get('is_teacher') is True or bool(session.get('student_id'))
        ),
        'today': datetime.now().date().isoformat()
    }


def get_admin_account(username, institution_id):
    if not isinstance(username, str) or not isinstance(institution_id, str):
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT username, institution_id, password_hash
            FROM admin_accounts
            WHERE username = ? AND institution_id = ?
            ''',
            (username, institution_id)
        ).fetchone()
    return dict(row) if row else None


def load_institutions():
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT institution_id, institution_code, name
            FROM institutions
            ORDER BY name COLLATE NOCASE
            '''
        ).fetchall()
    return [dict(row) for row in rows]


def get_institution(institution_id):
    if not isinstance(institution_id, str):
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT institution_id, institution_code, name
            FROM institutions
            WHERE institution_id = ?
            ''',
            (institution_id,)
        ).fetchone()
    return dict(row) if row else None


def get_institution_by_code(institution_code):
    if not isinstance(institution_code, str):
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT institution_id, institution_code, name
            FROM institutions
            WHERE institution_code = ?
            ''',
            (institution_code.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def load_admin_accounts(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        if institution_id is None:
            rows = connection.execute(
                '''
                SELECT admin_accounts.username, admin_accounts.institution_id,
                       institutions.name AS institution_name,
                       institutions.institution_code
                FROM admin_accounts
                JOIN institutions USING (institution_id)
                ORDER BY institutions.name COLLATE NOCASE,
                         admin_accounts.username COLLATE NOCASE
                '''
            ).fetchall()
        else:
            rows = connection.execute(
                '''
                SELECT admin_accounts.username, admin_accounts.institution_id,
                       institutions.name AS institution_name,
                       institutions.institution_code
                FROM admin_accounts
                JOIN institutions USING (institution_id)
                WHERE admin_accounts.institution_id = ?
                ORDER BY admin_accounts.username COLLATE NOCASE
                ''',
                (institution_id,)
            ).fetchall()
    return [dict(row) for row in rows]


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        account = get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if session.get('is_teacher') is not True or account is None:
            session.clear()
            return redirect(url_for('teacher_login'))
        if account['must_change_password']:
            session['pending_teacher_username'] = account['username']
            session.pop('is_teacher', None)
            session.pop('teacher_username', None)
            return redirect(url_for('teacher_change_password'))
        return view(*args, **kwargs)

    return wrapped


def get_admin_password_hash():
    if os.environ.get('FLASK_ADMIN_PASSWORD'):
        return None
    if not os.path.exists(ADMIN_CREDENTIALS_FILE):
        return None

    with open(ADMIN_CREDENTIALS_FILE, encoding='utf-8') as credentials_file:
        credentials = json.load(credentials_file)
    password_hash = credentials.get('password_hash')
    if not isinstance(password_hash, str) or not password_hash:
        raise ValueError(f'Invalid admin credentials file: {ADMIN_CREDENTIALS_FILE}')
    return password_hash


def save_admin_password(password):
    credentials = {'password_hash': generate_password_hash(password)}
    try:
        with open(ADMIN_CREDENTIALS_FILE, 'x', encoding='utf-8') as credentials_file:
            json.dump(credentials, credentials_file)
            credentials_file.flush()
            os.fsync(credentials_file.fileno())
    except FileExistsError:
        return False
    return True


def get_active_students(institution_id=None, include_answers=False):
    cutoff = time.time() - STUDENT_ACTIVITY_TIMEOUT
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                'DELETE FROM active_students WHERE last_seen < ?',
                (cutoff,)
            )
            active_rows = connection.execute(
                '''
                SELECT active_students.monitor_id, active_students.name,
                       active_students.book, active_students.level, active_students.exam,
                       active_students.started_at, active_students.answered_count,
                       active_students.total_questions, active_students.tab_switches,
                       active_students.answers_json, active_students.institution_id,
                       institutions.institution_code
                FROM active_students
                JOIN institutions USING (institution_id)
                WHERE active_students.last_seen >= ?
                  AND (? IS NULL OR active_students.institution_id = ?)
                ORDER BY started_at
                ''',
                (cutoff, institution_id, institution_id)
            ).fetchall()

    students = []
    for row in active_rows:
        student = dict(row)
        answers = json.loads(student.pop('answers_json'))
        if include_answers:
            questions = {
                question['id']: question
                for question in get_questions_for_exam(student['level'], student['exam'])
            }
            answer_details = []
            correct_count = 0
            for raw_question_id, selected in answers.items():
                try:
                    question_id = int(raw_question_id)
                except (TypeError, ValueError):
                    continue
                question = questions.get(question_id)
                if not question:
                    continue
                is_correct = selected == question['answer']
                correct_count += is_correct
                answer_details.append({
                    'question': question['question'],
                    'selected': selected,
                    'correct_answer': question['answer'],
                    'is_correct': is_correct
                })
            student['answers'] = answer_details
            student['score'] = correct_count
        students.append(student)

    return students


def get_exam_interruption(monitor_id, institution_id):
    if not monitor_id:
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT name, book, level, exam, reason, ended_by, ended_at
            FROM exam_interruptions
            WHERE monitor_id = ? AND institution_id = ?
            ''',
            (monitor_id, institution_id)
        ).fetchone()
    return dict(row) if row else None


def load_exam_interruptions(institution_id=None, limit=20):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT exam_interruptions.name, exam_interruptions.book,
                   exam_interruptions.exam, exam_interruptions.reason,
                   exam_interruptions.ended_by, exam_interruptions.ended_at,
                   exam_interruptions.answered_count,
                   exam_interruptions.total_questions, exam_interruptions.tab_switches,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM exam_interruptions
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR exam_interruptions.institution_id = ?)
            ORDER BY exam_interruptions.id DESC
            LIMIT ?
            ''',
            (institution_id, institution_id, limit)
        ).fetchall()
    return [dict(row) for row in rows]


def get_student_account(student_id, institution_id):
    if not isinstance(student_id, str) or not isinstance(institution_id, str):
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT student_id, first_name, last_name, email, password_hash,
                   date_of_birth, sex, physical_address
            FROM student_accounts
            WHERE student_id = ? AND institution_id = ?
            ''',
            (student_id, institution_id)
        ).fetchone()
    return dict(row) if row else None


def is_profile_complete(account):
    return bool(
        account
        and isinstance(account.get('first_name'), str)
        and account['first_name'].strip()
        and isinstance(account.get('last_name'), str)
        and account['last_name'].strip()
        and account.get('date_of_birth')
        and account.get('sex') in PROFILE_SEX_OPTIONS
        and isinstance(account.get('physical_address'), str)
        and account['physical_address'].strip()
    )


def validate_profile_data(form):
    first_name = form.get('first_name', '').strip()
    last_name = form.get('last_name', '').strip()
    date_of_birth = form.get('date_of_birth', '').strip()
    sex = form.get('sex', '').strip()
    physical_address = form.get('physical_address', '').strip()
    if not first_name or len(first_name) > 80 or not last_name or len(last_name) > 80:
        return None, 'Enter a first and last name, each no longer than 80 characters.'
    try:
        birth_date = datetime.strptime(date_of_birth, '%Y-%m-%d').date()
    except ValueError:
        return None, 'Enter a valid date of birth.'
    if birth_date >= datetime.now().date():
        return None, 'Date of birth must be in the past.'
    if sex not in PROFILE_SEX_OPTIONS:
        return None, 'Choose one of the listed sex options.'
    if not physical_address or len(physical_address) > 500:
        return None, 'Enter a physical address up to 500 characters.'
    return {
        'first_name': first_name,
        'last_name': last_name,
        'date_of_birth': date_of_birth,
        'sex': sex,
        'physical_address': physical_address
    }, None


def get_signed_in_profile():
    if session.get('is_teacher') is True:
        account = get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if account:
            return 'teacher', account, {
                'username': account['username'],
                'institution_id': account['institution_id']
            }
    if session.get('student_id'):
        account = get_student_account(
            session.get('student_id'),
            session.get('institution_id')
        )
        if account:
            return 'student', account, {
                'student_id': account['student_id'],
                'institution_id': session.get('institution_id')
            }
    return None, None, None


def is_valid_personal_id(personal_id):
    return isinstance(personal_id, str) and PERSONAL_ID_PATTERN.fullmatch(personal_id) is not None


def get_teacher_account(username, institution_id):
    if not isinstance(username, str) or not isinstance(institution_id, str):
        return None
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            '''
            SELECT username, employee_id, first_name, last_name, email, password_hash,
                   must_change_password, institution_id, date_of_birth, sex,
                   physical_address
            FROM teacher_accounts
            WHERE username = ? AND institution_id = ?
            ''',
            (username, institution_id)
        ).fetchone()
    return dict(row) if row else None


def load_teacher_accounts(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT teacher_accounts.username, teacher_accounts.employee_id,
                   teacher_accounts.first_name, teacher_accounts.last_name,
                   teacher_accounts.email, teacher_accounts.institution_id,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM teacher_accounts
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR teacher_accounts.institution_id = ?)
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
            ''',
            (institution_id, institution_id)
        ).fetchall()
    return [dict(row) for row in rows]


def create_teacher_username(first_name, last_name):
    initials = ''.join(
        character for character in (first_name[0] + last_name[0]).upper()
        if 'A' <= character <= 'Z'
    )
    if len(initials) != 2:
        raise ValueError('Teacher names must start with standard A-Z letters to create a username.')
    prefix = f'{initials}-'
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            'SELECT last_number FROM teacher_username_counters WHERE initials = ?',
            (initials,)
        ).fetchone()
        if row:
            number = row[0] + 1
        else:
            rows = connection.execute(
                'SELECT username FROM teacher_accounts WHERE username LIKE ?',
                (f'{prefix}%',)
            ).fetchall()
            number = max(
                (
                    int(username[len(prefix):])
                    for (username,) in rows
                    if username[len(prefix):].isdigit()
                ),
                default=0
            ) + 1
        if number > 9999:
            connection.rollback()
            raise ValueError(f'No teacher username numbers remain for initials {initials}.')
        connection.execute(
            '''
            INSERT INTO teacher_username_counters (initials, last_number)
            VALUES (?, ?)
            ON CONFLICT(initials) DO UPDATE SET last_number = excluded.last_number
            ''',
            (initials, number)
        )
        connection.commit()
    return f'{prefix}{number:04d}'


def create_employee_id():
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            'SELECT last_number FROM employee_id_counters WHERE id = 1'
        ).fetchone()
        if row is None:
            raise RuntimeError('Employee ID counter is missing.')
        number = row[0] + 1
        connection.execute(
            'UPDATE employee_id_counters SET last_number = ? WHERE id = 1',
            (number,)
        )
        connection.commit()
    return f'EMP-{number:06d}'


def load_student_accounts(institution_id=None):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT student_accounts.student_id, student_accounts.first_name,
                   student_accounts.last_name, student_accounts.email,
                   student_accounts.institution_id,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM student_accounts
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR student_accounts.institution_id = ?)
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
            ''',
            (institution_id, institution_id)
        ).fetchall()
    return [dict(row) for row in rows]


def load_student_removals(institution_id=None, limit=100):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT student_removals.student_id, student_removals.first_name,
                   student_removals.last_name, student_removals.email,
                   student_removals.reason, student_removals.removed_by,
                   student_removals.removed_at, student_removals.institution_id,
                   institutions.name AS institution_name,
                   institutions.institution_code
            FROM student_removals
            JOIN institutions USING (institution_id)
            WHERE (? IS NULL OR student_removals.institution_id = ?)
            ORDER BY student_removals.id DESC
            LIMIT ?
            ''',
            (institution_id, institution_id, limit)
        ).fetchall()
    return [dict(row) for row in rows]


def send_support_notification(support_request):
    recipient = os.environ.get('MAIN_ADMIN_EMAIL', '').strip()
    smtp_host = os.environ.get('SMTP_HOST', '').strip()
    if not recipient or not smtp_host:
        raise RuntimeError(
            'Set MAIN_ADMIN_EMAIL and SMTP_HOST to enable email notifications.'
        )
    try:
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    except ValueError as error:
        raise RuntimeError('SMTP_PORT must be a valid port number.') from error
    username = os.environ.get('SMTP_USERNAME', '').strip()
    password = os.environ.get('SMTP_PASSWORD', '')
    sender = os.environ.get('SMTP_FROM', '').strip() or username or recipient
    use_ssl = os.environ.get('SMTP_USE_SSL', '').strip().lower() in {'1', 'true', 'yes'}
    use_tls = os.environ.get('SMTP_USE_TLS', 'true').strip().lower() not in {
        '0', 'false', 'no'
    }

    message = EmailMessage()
    message['Subject'] = f"App support request #{support_request['id']}: {support_request['subject']}"
    message['From'] = sender
    message['To'] = recipient
    message['Reply-To'] = support_request['requester_email']
    message.set_content(
        f"Support request #{support_request['id']}\n"
        f"From: {support_request['requester_name']} "
        f"<{support_request['requester_email']}>\n"
        f"Role: {support_request['requester_type']}\n"
        f"Submitted: {support_request['submitted_at']}\n\n"
        f"Subject: {support_request['subject']}\n\n"
        f"{support_request['message']}\n"
    )

    if use_ssl:
        with smtplib.SMTP_SSL(
            smtp_host, smtp_port, timeout=15,
            context=ssl.create_default_context()
        ) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if username:
                smtp.login(username, password)
            smtp.send_message(message)


def update_support_email_status(request_id, status, error=''):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            connection.execute(
                '''
                UPDATE support_requests
                SET email_status = ?, email_error = ?
                WHERE id = ?
                ''',
                (status, error[:500], request_id)
            )


def load_all_profiles():
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        students = [
            dict(row) for row in connection.execute(
                '''
                SELECT student_id AS account_id, institution_id, first_name,
                       last_name, email, date_of_birth, sex, physical_address
                FROM student_accounts
                ORDER BY institution_id, last_name COLLATE NOCASE,
                         first_name COLLATE NOCASE
                '''
            ).fetchall()
        ]
        teachers = [
            dict(row) for row in connection.execute(
                '''
                SELECT username AS account_id, institution_id, first_name,
                       last_name, email, employee_id, date_of_birth, sex,
                       physical_address
                FROM teacher_accounts
                ORDER BY institution_id, last_name COLLATE NOCASE,
                         first_name COLLATE NOCASE
                '''
            ).fetchall()
        ]
    institutions = {
        institution['institution_id']: institution
        for institution in load_institutions()
    }
    for account in students + teachers:
        institution = institutions[account['institution_id']]
        account['institution_name'] = institution['name']
        account['institution_code'] = institution['institution_code']
    return students, teachers


@app.route('/help', methods=['GET', 'POST'])
def help():
    user_type, account, identity = get_signed_in_profile()
    requester_name = (
        f"{account['first_name']} {account['last_name']}".strip()
        if account else ''
    )
    requester_email = account['email'] if account else ''
    error = None

    if request.method == 'POST':
        submitted_name = request.form.get('name', '').strip()
        submitted_email = request.form.get('email', '').strip()
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        if not submitted_name or len(submitted_name) > 160:
            error = 'Enter your name (up to 160 characters).'
        elif (
            len(submitted_email) > 254
            or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', submitted_email)
        ):
            error = 'Enter a valid email address so the Main Admin can reply.'
        elif not subject or len(subject) > 120 or any(
            character in subject for character in '\r\n'
        ):
            error = 'Enter a subject up to 120 characters.'
        elif not 10 <= len(message) <= 5000:
            error = 'Describe the issue in 10-5000 characters.'
        else:
            submitted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            requester_type = user_type or (
                'main_admin' if is_main_admin()
                else 'institution_admin' if session.get('is_admin') is True
                else 'visitor'
            )
            requester_id = (
                (identity.get('username') or identity.get('student_id'))
                if identity else session.get('admin_username', '')
            )
            institution_id = (
                identity.get('institution_id') if identity
                else session.get('institution_id', 'main')
            )
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    cursor = connection.execute(
                        '''
                        INSERT INTO support_requests (
                            requester_type, requester_id, requester_name,
                            requester_email, institution_id, subject, message,
                            submitted_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            requester_type,
                            requester_id or '',
                            requester_name or submitted_name,
                            requester_email or submitted_email,
                            institution_id or 'main',
                            subject,
                            message,
                            submitted_at
                        )
                    )
                    support_request_id = cursor.lastrowid
            support_request = {
                'id': support_request_id,
                'requester_type': requester_type,
                'requester_name': requester_name or submitted_name,
                'requester_email': requester_email or submitted_email,
                'subject': subject,
                'message': message,
                'submitted_at': submitted_at
            }
            try:
                send_support_notification(support_request)
            except RuntimeError as send_error:
                logging.warning(
                    'Support request %s saved; email is pending: %s',
                    support_request_id,
                    send_error
                )
                update_support_email_status(
                    support_request_id, 'pending', str(send_error)
                )
                flash(
                    f'Request #{support_request_id} was saved. Email notification '
                    'is pending Main Admin email/SMTP configuration.',
                    'warning'
                )
            except (OSError, smtplib.SMTPException, ValueError) as send_error:
                logging.exception(
                    'Could not email support request %s.',
                    support_request_id
                )
                update_support_email_status(
                    support_request_id, 'failed', str(send_error)
                )
                flash(
                    f'Request #{support_request_id} was saved, but its email '
                    'notification failed. The Main Admin can retry it.',
                    'warning'
                )
            else:
                update_support_email_status(support_request_id, 'sent')
                flash(
                    f'Your request #{support_request_id} was sent to the Main Admin.',
                    'success'
                )
            return redirect(url_for('help'))

        requester_name = submitted_name
        requester_email = submitted_email

    support_requests = []
    if is_main_admin():
        with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            support_requests = [
                dict(row) for row in connection.execute(
                    '''
                    SELECT id, requester_type, requester_id, requester_name,
                           requester_email, institution_id, subject, message,
                           submitted_at, email_status, email_error
                    FROM support_requests
                    ORDER BY id DESC
                    LIMIT 200
                    '''
                ).fetchall()
            ]
    return render_template(
        'help.html',
        requester_name=requester_name,
        requester_email=requester_email,
        error=error,
        support_requests=support_requests
    )


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if session.get('is_admin') is True:
        if is_valid_admin_session():
            return redirect(url_for(
                'admin_profiles' if is_main_admin() else 'admin_dashboard'
            ))
        session.clear()
        return redirect(url_for('admin_login'))
    user_type, account, identity = get_signed_in_profile()
    if account is None:
        destination = 'teacher_login' if session.get('is_teacher') else 'student_login'
        return redirect(url_for(destination))

    error = None
    if request.method == 'POST':
        profile_data, error = validate_profile_data(request.form)
        if profile_data:
            if user_type == 'teacher':
                where_clause = 'username = ? AND institution_id = ?'
                parameters = (
                    profile_data['first_name'], profile_data['last_name'],
                    profile_data['date_of_birth'], profile_data['sex'],
                    profile_data['physical_address'],
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    identity['username'], identity['institution_id']
                )
                table = 'teacher_accounts'
            else:
                where_clause = 'student_id = ? AND institution_id = ?'
                parameters = (
                    profile_data['first_name'], profile_data['last_name'],
                    profile_data['date_of_birth'], profile_data['sex'],
                    profile_data['physical_address'],
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    identity['student_id'], identity['institution_id']
                )
                table = 'student_accounts'
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    updated = connection.execute(
                        f'''
                        UPDATE {table}
                        SET first_name = ?, last_name = ?, date_of_birth = ?,
                            sex = ?, physical_address = ?, updated_at = ?
                        WHERE {where_clause}
                        ''',
                        parameters
                    ).rowcount
            if not updated:
                abort(404, description='Your account could not be found.')
            flash('Your profile was saved.', 'success')
            return redirect(url_for('profile'))

    return render_template(
        'profile.html',
        account=account,
        user_type=user_type,
        sex_options=PROFILE_SEX_OPTIONS,
        error=error,
        profile_required=not is_profile_complete(account)
    )


@app.route('/teacher/login', methods=['GET', 'POST'])
def teacher_login():
    if session.get('is_teacher') is True:
        account = get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if account:
            if account['must_change_password']:
                return redirect(url_for('teacher_change_password'))
            return redirect(url_for('teacher_exam_take'))

    if request.method == 'POST':
        institution = get_institution_by_code(
            request.form.get('institution_code', '')
        )
        username = request.form.get('username', '').strip().upper()
        password = request.form.get('password', '')
        account = get_teacher_account(
            username,
            institution['institution_id'] if institution else None
        )
        if account and check_password_hash(account['password_hash'], password):
            session.clear()
            if account['must_change_password']:
                session['pending_teacher_username'] = username
                session['pending_institution_id'] = account['institution_id']
                return redirect(url_for('teacher_change_password'))
            session['is_teacher'] = True
            session['teacher_username'] = username
            session['institution_id'] = account['institution_id']
            if not is_profile_complete(account):
                return redirect(url_for('profile'))
            return redirect(url_for('teacher_exam_take'))
        flash('The teacher username or password was not accepted.', 'error')

    return render_template('teacher_login.html', institutions=load_institutions())


@app.route('/teacher/change-password', methods=['GET', 'POST'])
def teacher_change_password():
    username = session.get('pending_teacher_username')
    institution_id = session.get('pending_institution_id')
    account = get_teacher_account(username, institution_id)
    if account is None:
        session.clear()
        return redirect(url_for('teacher_login'))
    if not account['must_change_password']:
        session.clear()
        session['is_teacher'] = True
        session['teacher_username'] = username
        session['institution_id'] = institution_id
        account = get_teacher_account(username, institution_id)
        if account and not is_profile_complete(account):
            return redirect(url_for('profile'))
        return redirect(url_for('teacher_exam_take'))

    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        if len(password) < 8:
            error = 'Choose a password with at least 8 characters.'
        elif len(password) > 128:
            error = 'Choose a password with no more than 128 characters.'
        elif password != confirm_password:
            error = 'The passwords do not match.'
        else:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    connection.execute(
                        '''
                        UPDATE teacher_accounts
                        SET password_hash = ?, must_change_password = 0, updated_at = ?
                        WHERE username = ? AND institution_id = ?
                        ''',
                        (generate_password_hash(password), now, username, institution_id)
                    )
            session.clear()
            session['is_teacher'] = True
            session['teacher_username'] = username
            session['institution_id'] = institution_id
            return redirect(url_for('teacher_exam_take'))

    return render_template('teacher_change_password.html', error=error)


@app.post('/teacher/logout')
def teacher_logout():
    monitor_id = session.get('monitor_id')
    if monitor_id:
        with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
            with connection:
                connection.execute(
                    '''
                    DELETE FROM active_students
                    WHERE monitor_id = ? AND institution_id = ?
                    ''',
                    (monitor_id, session.get('institution_id', 'main'))
                )
    session.clear()
    return redirect(url_for('teacher_login'))


@app.route('/student/login', methods=['GET', 'POST'])
def student_login():
    selected_book = normalize_book(
        request.form.get('book') if request.method == 'POST'
        else request.args.get('book')
    )
    selected_exam = normalize_exam(
        request.form.get('exam') if request.method == 'POST'
        else request.args.get('exam')
    )
    if request.method == 'POST':
        institution = get_institution_by_code(
            request.form.get('institution_code', '')
        )
        student_id = request.form.get('student_id', '').strip()
        password = request.form.get('password', '')
        account = get_student_account(
            student_id,
            institution['institution_id'] if institution else None
        )
        if (
            account
            and is_valid_personal_id(student_id)
            and check_password_hash(account['password_hash'], password)
        ):
            session.clear()
            session['student_id'] = student_id
            session['institution_id'] = institution['institution_id']
            session['student_exam_access'] = {
                'book': selected_book,
                'exam': selected_exam
            }
            if not is_profile_complete(account):
                return redirect(url_for('profile'))
            return redirect(url_for(
                'index',
                book=selected_book,
                exam=selected_exam
            ))
        flash('The personal ID or password was not accepted.', 'error')

    return render_template(
        'student_login.html',
        selected_book=selected_book,
        selected_exam=selected_exam,
        institutions=load_institutions()
    )


@app.post('/student/logout')
def student_logout():
    monitor_id = session.get('monitor_id')
    if monitor_id:
        with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
            with connection:
                connection.execute(
                    '''
                    DELETE FROM active_students
                    WHERE monitor_id = ? AND institution_id = ?
                    ''',
                    (monitor_id, session.get('institution_id', 'main'))
                )
    session.pop('student_id', None)
    session.pop('student_exam_access', None)
    session.pop('monitor_id', None)
    session.pop('exam_attempt', None)
    return redirect(url_for('student_login'))


@app.route('/admin/students', methods=['GET', 'POST'])
@admin_required
def admin_students():
    data_scope = admin_data_scope()
    if request.method == 'POST':
        institution_id = (
            request.form.get('institution_id', '').strip()
            if is_main_admin()
            else current_institution_id()
        )
        if get_institution(institution_id) is None:
            flash('Select a valid institution.', 'error')
            return redirect(url_for('admin_students'))
        action = request.form.get('action', '')
        student_id = request.form.get('student_id', '').strip()
        original_id = request.form.get('original_id', '').strip()
        if action == 'delete':
            if not is_valid_personal_id(student_id):
                flash('Select a valid personal ID to delete.', 'error')
            else:
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        deleted = connection.execute(
                            '''
                            DELETE FROM student_accounts
                            WHERE student_id = ? AND institution_id = ?
                            ''',
                            (student_id, institution_id)
                        ).rowcount
                        connection.execute(
                            '''
                            DELETE FROM active_students
                            WHERE student_id = ? AND institution_id = ?
                            ''',
                            (student_id, institution_id)
                        )
                if deleted:
                    flash('Student account deleted.', 'success')
                else:
                    flash('That student account no longer exists.', 'error')
            return redirect(url_for('admin_students'))

        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        if action not in {'create', 'update'}:
            flash('Choose a valid student account action.', 'error')
        elif not is_valid_personal_id(student_id):
            flash('Personal ID must be 3-32 letters or numbers, with optional dashes.', 'error')
        elif not first_name or not last_name or len(first_name) > 80 or len(last_name) > 80:
            flash('Enter a first and last name (up to 80 characters each).', 'error')
        elif len(email) > 254 or '@' not in email:
            flash('Enter a valid email address.', 'error')
        elif action == 'update' and not is_valid_personal_id(original_id):
            flash('The original student account could not be identified.', 'error')
        else:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            try:
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        if action == 'create':
                            connection.execute(
                                '''
                                INSERT INTO student_accounts (
                                    institution_id, student_id, first_name, last_name, email,
                                    password_hash, created_at, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''',
                                (
                                    institution_id,
                                    student_id,
                                    first_name,
                                    last_name,
                                    email,
                                    generate_password_hash(student_id),
                                    now,
                                    now
                                )
                            )
                        else:
                            updated = connection.execute(
                                '''
                                UPDATE student_accounts
                                SET student_id = ?, first_name = ?, last_name = ?,
                                    email = ?, password_hash = ?, updated_at = ?
                                WHERE student_id = ? AND institution_id = ?
                                ''',
                                (
                                    student_id,
                                    first_name,
                                    last_name,
                                    email,
                                    generate_password_hash(student_id),
                                    now,
                                    original_id,
                                    institution_id
                                )
                            ).rowcount
                            if not updated:
                                raise ValueError('That student account no longer exists.')
                            if student_id != original_id:
                                connection.execute(
                                    '''
                                    UPDATE active_students SET student_id = ?
                                    WHERE student_id = ? AND institution_id = ?
                                    ''',
                                    (student_id, original_id, institution_id)
                                )
                flash(
                    'Student account created. The initial password is the personal ID.'
                    if action == 'create'
                    else 'Student account updated. The password is now the personal ID.',
                    'success'
                )
            except sqlite3.IntegrityError:
                flash('That personal ID or email is already assigned to another account.', 'error')
            except ValueError as error:
                flash(str(error), 'error')
        return redirect(url_for('admin_students'))

    return render_template(
        'student_accounts.html',
        accounts=load_student_accounts(data_scope),
        removals=load_student_removals(data_scope),
        institutions=load_institutions() if is_main_admin() else []
    )


@app.route('/teacher/students', methods=['GET', 'POST'])
@teacher_required
def teacher_students():
    institution_id = session['institution_id']
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'create':
            student_id = request.form.get('student_id', '').strip()
            first_name = request.form.get('first_name', '').strip()
            last_name = request.form.get('last_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            if not is_valid_personal_id(student_id):
                flash('Enter a valid personal ID or passport number.', 'error')
            elif not first_name or not last_name or len(first_name) > 80 or len(last_name) > 80:
                flash('Enter a first and last name (up to 80 characters each).', 'error')
            elif len(email) > 254 or '@' not in email:
                flash('Enter a valid email address.', 'error')
            else:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                try:
                    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                        with connection:
                            connection.execute(
                                '''
                                INSERT INTO student_accounts (
                                    institution_id, student_id, first_name, last_name, email,
                                    password_hash, created_at, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''',
                                (
                                    institution_id,
                                    student_id,
                                    first_name,
                                    last_name,
                                    email,
                                    generate_password_hash(student_id),
                                    now,
                                    now
                                )
                            )
                    flash(
                        'Student account created. The student signs in with the Personal ID as both username and initial password.',
                        'success'
                    )
                except sqlite3.IntegrityError:
                    flash('That personal ID or email is already assigned to an account.', 'error')
        elif action == 'delete':
            student_id = request.form.get('student_id', '').strip()
            reason = request.form.get('reason', '').strip()
            if not is_valid_personal_id(student_id):
                flash('Select a valid student account to remove.', 'error')
            elif not 5 <= len(reason) <= 500:
                flash('Enter a removal reason between 5 and 500 characters.', 'error')
            else:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                removed_by = session['teacher_username']
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        student = connection.execute(
                            '''
                            SELECT student_id, first_name, last_name, email
                            FROM student_accounts
                            WHERE student_id = ? AND institution_id = ?
                            ''',
                            (student_id, institution_id)
                        ).fetchone()
                        if student is None:
                            flash('That student account no longer exists.', 'error')
                        else:
                            connection.execute(
                                '''
                                INSERT INTO student_removals (
                                    institution_id, student_id, first_name, last_name, email, reason,
                                    removed_by, removed_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''',
                                (institution_id, *student, reason, removed_by, now)
                            )
                            connection.execute(
                                '''
                                DELETE FROM student_accounts
                                WHERE student_id = ? AND institution_id = ?
                                ''',
                                (student_id, institution_id)
                            )
                            connection.execute(
                                '''
                                DELETE FROM active_students
                                WHERE student_id = ? AND institution_id = ?
                                ''',
                                (student_id, institution_id)
                            )
                            flash('Student account removed and the reason recorded.', 'success')
        else:
            flash('Choose a valid student account action.', 'error')
        return redirect(url_for('teacher_students'))

    return render_template(
        'teacher_student_accounts.html',
        accounts=load_student_accounts(institution_id),
        teacher_account=get_teacher_account(
            session.get('teacher_username'),
            institution_id
        )
    )


@app.route('/admin/teachers', methods=['GET', 'POST'])
@admin_required
def admin_teachers():
    data_scope = admin_data_scope()
    if request.method == 'POST':
        institution_id = (
            request.form.get('institution_id', '').strip()
            if is_main_admin()
            else current_institution_id()
        )
        if get_institution(institution_id) is None:
            flash('Select a valid institution.', 'error')
            return redirect(url_for('admin_teachers'))
        action = request.form.get('action', '')
        if action == 'delete':
            username = request.form.get('username', '').strip().upper()
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    deleted = connection.execute(
                        '''
                        DELETE FROM teacher_accounts
                        WHERE username = ? AND institution_id = ?
                        ''',
                        (username, institution_id)
                    ).rowcount
            flash(
                'Teacher account deleted.' if deleted
                else 'That teacher account no longer exists.',
                'success' if deleted else 'error'
            )
            return redirect(url_for('admin_teachers'))

        if action != 'create':
            flash('Choose a valid teacher account action.', 'error')
            return redirect(url_for('admin_teachers'))

        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        if not first_name or not last_name or len(first_name) > 80 or len(last_name) > 80:
            flash('Enter a first and last name (up to 80 characters each).', 'error')
        elif len(email) > 254 or '@' not in email:
            flash('Enter a valid email address.', 'error')
        else:
            try:
                username = create_teacher_username(first_name, last_name)
                employee_id = create_employee_id()
                temporary_password = secrets.token_urlsafe(16)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        connection.execute(
                            '''
                            INSERT INTO teacher_accounts (
                                institution_id, username, employee_id, first_name, last_name,
                                email, password_hash,
                                must_change_password, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            ''',
                            (
                                institution_id,
                                username,
                                employee_id,
                                first_name,
                                last_name,
                                email,
                                generate_password_hash(temporary_password),
                                now,
                                now
                            )
                        )
                flash(
                    f'Teacher account created. Employee ID: {employee_id}. '
                    f'Username: {username}. '
                    f'Temporary password: {temporary_password}. '
                    'Share it securely; it will be required to change at first sign-in.',
                    'success'
                )
            except sqlite3.IntegrityError:
                flash('That teacher email is already assigned to another account.', 'error')
            except ValueError as error:
                flash(str(error), 'error')
        return redirect(url_for('admin_teachers'))

    return render_template(
        'teacher_accounts.html',
        accounts=load_teacher_accounts(data_scope),
        institutions=load_institutions() if is_main_admin() else []
    )


@app.route('/', methods=['GET', 'POST'])
def index():
    if session.get('is_admin') is True and not is_valid_admin_session():
        session.clear()
        return redirect(url_for('admin_login'))
    selected_book = normalize_book(
        request.form.get('book', 'Interchange Intro')
        if request.method == 'POST'
        else request.args.get('book', 'Interchange Intro')
    )
    selected_level = BOOK_LEVELS.get(selected_book, "Intro")
    selected_exam = normalize_exam(
        request.form.get('exam')
        if request.method == 'POST'
        else request.args.get('exam')
    )
    teacher_access = session.get('teacher_exam_access')
    teacher_account = (
        get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if session.get('is_teacher') is True
        else None
    )
    if session.get('is_teacher') is True and (
        teacher_account is None or teacher_account['must_change_password']
    ):
        session.clear()
        return redirect(url_for('teacher_login'))
    teacher_session = (
        session.get('is_admin') is True
        or (session.get('is_teacher') is True and teacher_account is not None)
    )
    teacher_attempt = (
        teacher_session
        and isinstance(teacher_access, dict)
        and teacher_access.get('book') == selected_book
        and teacher_access.get('exam') == selected_exam
    )
    account = get_student_account(
        session.get('student_id'),
        session.get('institution_id')
    )
    if account is None and not teacher_attempt:
        if teacher_session:
            destination = 'teacher_exam_take' if session.get('is_teacher') is True else 'teacher_exam'
            return redirect(url_for(destination))
        session.pop('student_id', None)
        session.pop('student_exam_access', None)
        return redirect(url_for(
            'student_login',
            book=selected_book,
            exam=selected_exam
        ))
    if request.method == 'POST':
        monitor_id = session.get('monitor_id')
        interruption = get_exam_interruption(monitor_id, current_institution_id())
        if interruption:
            return render_template(
                'form.html',
                submitted=False,
                exam_terminated=True,
                selected_book=interruption['book'],
                selected_exam=interruption['exam'],
                termination_reason=interruption['reason'],
                student_account=account,
                teacher_attempt=teacher_attempt,
                teacher_is_admin=session.get('is_admin') is True,
                teacher_account=teacher_account
            ), 410
        selected_book = normalize_book(request.form.get('book', selected_book))
        selected_level = BOOK_LEVELS.get(selected_book, "Intro")
        selected_exam = normalize_exam(request.form.get('exam'))
        if teacher_session:
            if (
                not isinstance(teacher_access, dict)
                or teacher_access.get('book') != selected_book
                or teacher_access.get('exam') != selected_exam
            ):
                destination = 'teacher_exam_take' if session.get('is_teacher') is True else 'teacher_exam'
                return redirect(url_for(destination))
        else:
            exam_access = session.get('student_exam_access')
            if (
                not isinstance(exam_access, dict)
                or exam_access.get('book') != selected_book
                or exam_access.get('exam') != selected_exam
            ):
                return redirect(url_for(
                    'student_login',
                    book=selected_book,
                    exam=selected_exam
                ))
        exam_attempt = session.get('exam_attempt')
        if (
            not isinstance(exam_attempt, dict)
            or exam_attempt.get('book') != selected_book
            or exam_attempt.get('exam') != selected_exam
            or not isinstance(exam_attempt.get('started_at'), (int, float))
            or time.time() - exam_attempt['started_at'] >= EXAM_DURATION_SECONDS
        ):
            return render_template(
                'form.html',
                submitted=False,
                exam_expired=True,
                selected_book=selected_book,
                selected_exam=selected_exam,
                student_account=account,
                teacher_attempt=teacher_attempt,
                teacher_is_admin=session.get('is_admin') is True,
                teacher_account=teacher_account
            ), 410

        first_name = (
            teacher_account['first_name'] if teacher_account
            else 'Teacher' if teacher_attempt else account['first_name']
        )
        last_name = (
            teacher_account['last_name'] if teacher_account
            else '' if teacher_attempt else account['last_name']
        )
        email = '' if teacher_attempt else account['email']
        tab_switches = int(request.form.get('tab_switches', '0') or 0)
        question_order = get_session_question_order(selected_level, selected_exam)
        questions_for_level = get_questions_for_exam(selected_level, selected_exam)
        question_lookup = {q['id']: q for q in questions_for_level}
        ordered_questions = [question_lookup[qid] for qid in question_order if qid in question_lookup]

        writing_questions = [q for q in ordered_questions if q.get('type') == 'writing']
        graded_questions = [q for q in ordered_questions if q.get('type') != 'writing']
        writing_responses = [
            {
                'question': question['question'],
                'answer': request.form.get(f"q{question['id']}", '').strip()
            }
            for question in writing_questions
        ]
        if any(response['answer'] for response in writing_responses) and not teacher_attempt:
            save_writing_submission(
                first_name,
                last_name,
                selected_book,
                selected_level,
                selected_exam,
                writing_responses,
                current_institution_id()
            )

        score = 0
        results = []

        for q in graded_questions:
            selected = request.form.get(f"q{q['id']}")
            is_correct = selected == q['answer']
            if is_correct:
                score += 1

            results.append({
                'question': q['question'],
                'selected': selected,
                'correct': q['answer'],
                'is_correct': is_correct
            })

        total = len(graded_questions)
        section_scores = get_section_scores(ordered_questions, request.form)
        percentage = get_weighted_percentage(section_scores)
        cefr_band = get_cefr_band_from_percentage(percentage)
        passed = percentage >= 60

        if monitor_id:
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    connection.execute(
                        '''
                        DELETE FROM active_students
                        WHERE monitor_id = ? AND institution_id = ?
                        ''',
                        (monitor_id, current_institution_id())
                    )
        if teacher_attempt:
            save_teacher_exam_attempt(
                f'{first_name} {last_name}'.strip(),
                selected_book,
                selected_level,
                selected_exam,
                score,
                total,
                percentage,
                tab_switches,
                writing_responses,
                current_institution_id()
            )
        else:
            save_result(
                first_name,
                last_name,
                email,
                score,
                total,
                percentage,
                tab_switches,
                selected_level,
                selected_book,
                selected_exam,
                current_institution_id()
            )
        session.pop('exam_attempt', None)
        session.pop('monitor_id', None)
        session.pop('question_order', None)
        session.pop('question_order_context', None)
        if teacher_attempt:
            session.pop('teacher_exam_access', None)
        else:
            session.pop('student_id', None)
            session.pop('student_exam_access', None)

        return render_template(
            'form.html',
            questions=ordered_questions,
            results=results,
            score=score,
            total=total,
            percentage=percentage,
            passed=passed,
            first_name=first_name,
            last_name=last_name,
            email=email,
            tab_switches=tab_switches,
            submitted=True,
            grouped_questions=group_questions_by_section(ordered_questions),
            selected_level=selected_level,
            selected_book=selected_book,
            selected_exam=selected_exam,
            section_scores=section_scores,
            cefr_band=cefr_band,
            writing_responses=writing_responses,
            student_account=account,
            teacher_attempt=teacher_attempt,
            teacher_is_admin=session.get('is_admin') is True,
            teacher_account=teacher_account
        )

    if not teacher_attempt:
        if teacher_session:
            destination = 'teacher_exam_take' if session.get('is_teacher') is True else 'teacher_exam'
            return redirect(url_for(destination))
        exam_access = session.get('student_exam_access')
        if (
            not isinstance(exam_access, dict)
            or exam_access.get('book') != selected_book
            or exam_access.get('exam') != selected_exam
        ):
            return redirect(url_for(
                'student_login',
                book=selected_book,
                exam=selected_exam
            ))
    monitor_id = session.setdefault('monitor_id', uuid.uuid4().hex)
    interruption = get_exam_interruption(monitor_id, current_institution_id())
    if interruption:
        return render_template(
            'form.html',
            submitted=False,
            exam_terminated=True,
            selected_book=interruption['book'],
            selected_exam=interruption['exam'],
            termination_reason=interruption['reason'],
            student_account=account,
            teacher_attempt=teacher_attempt,
            teacher_is_admin=session.get('is_admin') is True,
            teacher_account=teacher_account
        ), 410
    if request.args.get('restart') == '1':
        session['monitor_id'] = uuid.uuid4().hex
        session.pop('exam_attempt', None)
        session.pop('question_order', None)
        session.pop('question_order_context', None)
        monitor_id = session['monitor_id']

    exam_attempt = session.get('exam_attempt')
    if (
        request.args.get('restart') == '1'
        or not isinstance(exam_attempt, dict)
        or exam_attempt.get('book') != selected_book
        or exam_attempt.get('exam') != selected_exam
        or not isinstance(exam_attempt.get('started_at'), (int, float))
    ):
        exam_attempt = {
            'book': selected_book,
            'exam': selected_exam,
            'started_at': None
        }
        session['exam_attempt'] = exam_attempt
    started_at = exam_attempt.get('started_at')
    remaining_seconds = None
    if isinstance(started_at, (int, float)):
        remaining_seconds = max(
            0,
            int(EXAM_DURATION_SECONDS - (time.time() - started_at) - 3)
        )
        if remaining_seconds == 0:
            return render_template(
                'form.html',
                submitted=False,
                exam_expired=True,
                selected_book=selected_book,
                selected_exam=selected_exam,
                student_account=account,
                teacher_attempt=teacher_attempt,
                teacher_is_admin=session.get('is_admin') is True,
                teacher_account=teacher_account
            ), 410

    questions_for_level = get_questions_for_exam(selected_level, selected_exam)
    question_order = get_session_question_order(selected_level, selected_exam)
    question_lookup = {q['id']: q for q in questions_for_level}
    ordered_questions = [question_lookup[qid] for qid in question_order if qid in question_lookup]
    ordered_questions = randomize_question_options(ordered_questions)

    return render_template(
        'form.html',
        questions=ordered_questions,
        submitted=False,
        first_name=(
            teacher_account['first_name'] if teacher_account
            else 'Teacher' if teacher_attempt else account['first_name']
        ),
        last_name=(
            teacher_account['last_name'] if teacher_account
            else '' if teacher_attempt else account['last_name']
        ),
        email='' if teacher_attempt else account['email'],
        tab_switches=0,
        grouped_questions=group_questions_by_section(ordered_questions),
        selected_level=selected_level,
        selected_book=selected_book,
        selected_exam=selected_exam,
        remaining_seconds=remaining_seconds,
        monitor_id=monitor_id,
        student_account=account,
        teacher_attempt=teacher_attempt,
        teacher_is_admin=session.get('is_admin') is True,
        teacher_account=teacher_account
    )


@app.post('/activity')
def update_activity():
    if session.get('is_admin') is True and not is_valid_admin_session():
        session.clear()
        return jsonify(error='Please sign in again.', login_required=True), 401
    activity = request.get_json(silent=True)
    if not isinstance(activity, dict):
        return jsonify(error='A JSON activity update is required.'), 400

    book = normalize_book(activity.get('book'))
    level = BOOK_LEVELS.get(book, 'Intro')
    exam = normalize_exam(activity.get('exam'))
    teacher_account = (
        get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
        if session.get('is_teacher') is True
        else None
    )
    account = get_student_account(
        session.get('student_id'),
        session.get('institution_id')
    )
    teacher_access = session.get('teacher_exam_access')
    teacher_attempt = (
        (
            session.get('is_admin') is True
            or (
                session.get('is_teacher') is True
                and teacher_account is not None
                and not teacher_account['must_change_password']
            )
        )
        and isinstance(teacher_access, dict)
        and teacher_access.get('book') == book
        and teacher_access.get('exam') == exam
    )
    if account is None and not teacher_attempt:
        session.pop('student_id', None)
        return jsonify(error='Please sign in with your personal ID.', login_required=True), 401
    first_name = (
        teacher_account['first_name'] if teacher_account
        else 'Teacher' if teacher_attempt else account['first_name']
    )
    last_name = (
        teacher_account['last_name'] if teacher_account
        else '' if teacher_attempt else account['last_name']
    )
    monitor_id = session.setdefault('monitor_id', uuid.uuid4().hex)
    interruption = get_exam_interruption(monitor_id, current_institution_id())
    if interruption:
        return jsonify(error='This exam was ended by the administrator.', terminated=True), 410

    answered_count = activity.get('answered_count')
    tab_switches = activity.get('tab_switches')
    if (
        isinstance(answered_count, bool)
        or not isinstance(answered_count, int)
        or isinstance(tab_switches, bool)
        or not isinstance(tab_switches, int)
        or answered_count < 0
        or tab_switches < 0
    ):
        return jsonify(error='Activity counts must be non-negative integers.'), 400

    exam_attempt = session.get('exam_attempt')
    if (
        not isinstance(exam_attempt, dict)
        or exam_attempt.get('book') != book
        or exam_attempt.get('exam') != exam
    ):
        exam_attempt = {'book': book, 'exam': exam, 'started_at': None}
    if not isinstance(exam_attempt.get('started_at'), (int, float)):
        exam_attempt['started_at'] = time.time()
    session['exam_attempt'] = exam_attempt
    remaining_seconds = max(
        0,
        int(EXAM_DURATION_SECONDS - (time.time() - exam_attempt['started_at']) - 3)
    )
    if remaining_seconds == 0:
        return jsonify(error='The exam time has expired.', expired=True), 410

    questions = [
        question for question in get_questions_for_exam(level, exam)
        if question.get('type') != 'writing'
    ]
    total_questions = len(questions)
    question_lookup = {question['id']: question for question in questions}
    answers = activity.get('answers', {})
    if not isinstance(answers, dict):
        return jsonify(error='Answers must be sent as a question-to-answer object.'), 400

    validated_answers = {}
    for raw_question_id, selected in answers.items():
        try:
            question_id = int(raw_question_id)
        except (TypeError, ValueError):
            return jsonify(error='An answer contains an invalid question ID.'), 400
        question = question_lookup.get(question_id)
        if (
            question is None
            or not isinstance(selected, str)
            or selected not in question['options']
        ):
            return jsonify(error='An answer does not match a question option.'), 400
        validated_answers[question_id] = selected

    answered_count = len(validated_answers)
    now = time.time()

    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        with connection:
            previous = connection.execute(
                '''
                SELECT started_at, last_seen FROM active_students
                WHERE monitor_id = ? AND institution_id = ?
                ''',
                (monitor_id, current_institution_id())
            ).fetchone()
            started_at = (
                previous[0]
                if previous and previous[1] >= now - STUDENT_ACTIVITY_TIMEOUT
                else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            connection.execute(
                '''
                INSERT INTO active_students (
                    monitor_id, institution_id, name, book, level, exam, started_at, answered_count,
                    total_questions, tab_switches, answers_json, last_seen, student_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(monitor_id) DO UPDATE SET
                    institution_id = excluded.institution_id,
                    name = excluded.name,
                    book = excluded.book,
                    level = excluded.level,
                    exam = excluded.exam,
                    started_at = excluded.started_at,
                    answered_count = excluded.answered_count,
                    total_questions = excluded.total_questions,
                    tab_switches = excluded.tab_switches,
                    answers_json = excluded.answers_json,
                    last_seen = excluded.last_seen,
                    student_id = excluded.student_id
                ''',
                (
                    monitor_id,
                    current_institution_id(),
                    f'{first_name} {last_name}',
                    book,
                    level,
                    exam,
                    started_at,
                    answered_count,
                    total_questions,
                    min(tab_switches, 10000),
                    json.dumps(validated_answers),
                    now,
                    None if teacher_attempt else account['student_id']
                )
            )

    return jsonify(remaining_seconds=remaining_seconds)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('is_admin') is True:
        return redirect(url_for('admin_dashboard'))

    error = None
    setup_required = (
        not os.environ.get('FLASK_ADMIN_PASSWORD')
        and not os.path.exists(ADMIN_CREDENTIALS_FILE)
    )

    if request.method == 'POST' and setup_required:
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        if len(password) < 8:
            error = 'Choose a password with at least 8 characters.'
        elif len(password) > 16:
            error = 'Choose a password with no more than 16 characters.'
        elif password != confirm_password:
            error = 'The passwords do not match.'
        elif save_admin_password(password):
            session.clear()
            session['is_admin'] = True
            session['is_main_admin'] = True
            session['institution_id'] = 'main'
            return redirect(url_for('admin_dashboard'))
        else:
            setup_required = False
            error = 'Admin setup was already completed. Sign in with the saved password.'
    elif request.method == 'POST':
        username = request.form.get('username', '').strip().upper()
        submitted_password = request.form.get('password', '')
        admin_password = os.environ.get('FLASK_ADMIN_PASSWORD')
        password_hash = get_admin_password_hash()
        password_matches = (
            not username
            and (
                hmac.compare_digest(submitted_password, admin_password)
                if admin_password
                else bool(password_hash and check_password_hash(password_hash, submitted_password))
            )
        )
        if password_matches:
            session.clear()
            session['is_admin'] = True
            session['is_main_admin'] = True
            session['institution_id'] = 'main'
            return redirect(url_for('admin_dashboard'))
        institution = get_institution_by_code(
            request.form.get('institution_code', '')
        )
        institution_admin = get_admin_account(
            username,
            institution['institution_id'] if institution else None
        )
        if (
            institution_admin
            and check_password_hash(institution_admin['password_hash'], submitted_password)
        ):
            session.clear()
            session['is_admin'] = True
            session['admin_username'] = institution_admin['username']
            session['institution_id'] = institution_admin['institution_id']
            return redirect(url_for('admin_dashboard'))
        error = 'The password was not accepted.'

    return render_template(
        'admin_login.html',
        setup_required=setup_required,
        error=error,
        institutions=load_institutions()
    )


@app.post('/admin/logout')
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/admins', methods=['GET', 'POST'])
@admin_required
def admin_accounts():
    institution_id = current_institution_id()
    if request.method == 'POST':
        action = request.form.get('action', '')
        username = request.form.get('username', '').strip().upper()
        if action == 'delete':
            target_institution_id = (
                request.form.get('institution_id', '').strip()
                if is_main_admin()
                else institution_id
            )
            if (
                not username
                or (
                    username == session.get('admin_username')
                    and target_institution_id == institution_id
                )
            ):
                flash('You cannot remove your own signed-in admin account.', 'error')
            else:
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        deleted = connection.execute(
                            '''
                            DELETE FROM admin_accounts
                            WHERE username = ? AND institution_id = ?
                            ''',
                            (username, target_institution_id)
                        ).rowcount
                flash(
                    'Admin account deleted.' if deleted
                    else 'That admin account does not exist.',
                    'success' if deleted else 'error'
                )
            return redirect(url_for('admin_accounts'))

        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        target_institution_id = (
            request.form.get('institution_id', '').strip()
            if is_main_admin()
            else institution_id
        )
        if action != 'create':
            flash('Choose a valid admin account action.', 'error')
        elif get_institution(target_institution_id) is None:
            flash('Select a valid institution.', 'error')
        elif not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{2,31}', username):
            flash('Use a 3-32 character username containing letters, numbers, dots, dashes, or underscores.', 'error')
        elif len(password) < 8 or len(password) > 128:
            flash('Choose a password between 8 and 128 characters.', 'error')
        elif password != confirm_password:
            flash('The passwords do not match.', 'error')
        else:
            try:
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    with connection:
                        connection.execute(
                            '''
                            INSERT INTO admin_accounts (
                                username, institution_id, password_hash, created_at
                            )
                            VALUES (?, ?, ?, ?)
                            ''',
                            (
                                username,
                                target_institution_id,
                                generate_password_hash(password),
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            )
                        )
                flash('Admin account created for this institution.', 'success')
            except sqlite3.IntegrityError:
                flash('That admin username is already in use.', 'error')
        return redirect(url_for('admin_accounts'))

    accounts = load_admin_accounts(None if is_main_admin() else institution_id)
    return render_template(
        'admin_accounts.html',
        accounts=accounts,
        institution=None if is_main_admin() else get_institution(institution_id),
        institutions=load_institutions() if is_main_admin() else []
    )


@app.route('/admin/institutions', methods=['GET', 'POST'])
@main_admin_required
def admin_institutions():
    if request.method == 'POST':
        name = request.form.get('institution_name', '').strip()
        username = request.form.get('username', '').strip().upper()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        if not name or len(name) > 120:
            flash('Enter an institution name up to 120 characters.', 'error')
        elif not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{2,31}', username):
            flash('Use a 3-32 character admin username containing letters, numbers, dots, dashes, or underscores.', 'error')
        elif len(password) < 8 or len(password) > 128:
            flash('Choose a password between 8 and 128 characters.', 'error')
        elif password != confirm_password:
            flash('The passwords do not match.', 'error')
        else:
            try:
                with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    latest_code = connection.execute(
                        '''
                        SELECT MAX(CAST(SUBSTR(institution_code, 6) AS INTEGER))
                        FROM institutions
                        WHERE institution_code GLOB 'INST-[0-9]*'
                        '''
                    ).fetchone()[0] or 0
                    institution_code = f'INST-{latest_code + 1:04d}'
                    institution_id = uuid.uuid4().hex
                    with connection:
                        connection.execute(
                            '''
                            INSERT INTO institutions (
                                institution_id, institution_code, name, created_at
                            )
                            VALUES (?, ?, ?, ?)
                            ''',
                            (
                                institution_id,
                                institution_code,
                                name,
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            )
                        )
                        connection.execute(
                            '''
                            INSERT INTO admin_accounts (
                                username, institution_id, password_hash, created_at
                            )
                            VALUES (?, ?, ?, ?)
                            ''',
                            (
                                username,
                                institution_id,
                                generate_password_hash(password),
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            )
                        )
                flash(
                    f'Institution created. Share institution code {institution_code} '
                    f'and admin username {username} with its administrator.',
                    'success'
                )
            except sqlite3.IntegrityError:
                flash('That admin username is already in use.', 'error')
        return redirect(url_for('admin_institutions'))

    return render_template(
        'admin_institutions.html',
        institutions=load_institutions(),
        accounts=load_admin_accounts()
    )


@app.route('/admin/profiles', methods=['GET', 'POST'])
@main_admin_required
def admin_profiles():
    error = None
    if request.method == 'POST':
        account_type = request.form.get('account_type', '')
        profile_data, error = validate_profile_data(request.form)
        if account_type not in {'student', 'teacher'}:
            error = 'Choose a valid account type.'
        if profile_data and error is None:
            institution_id = request.form.get('institution_id', '').strip()
            account_id = request.form.get('account_id', '').strip()
            if account_type == 'student':
                table = 'student_accounts'
                key_column = 'student_id'
            else:
                table = 'teacher_accounts'
                key_column = 'username'
            with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
                with connection:
                    updated = connection.execute(
                        f'''
                        UPDATE {table}
                        SET first_name = ?, last_name = ?, date_of_birth = ?,
                            sex = ?, physical_address = ?, updated_at = ?
                        WHERE {key_column} = ? AND institution_id = ?
                        ''',
                        (
                            profile_data['first_name'],
                            profile_data['last_name'],
                            profile_data['date_of_birth'],
                            profile_data['sex'],
                            profile_data['physical_address'],
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            account_id,
                            institution_id
                        )
                    ).rowcount
            if not updated:
                error = 'That account could not be found.'
            else:
                flash('Profile updated.', 'success')
                return redirect(url_for('admin_profiles'))

    students, teachers = load_all_profiles()
    return render_template(
        'admin_profiles.html',
        students=students,
        teachers=teachers,
        sex_options=PROFILE_SEX_OPTIONS,
        error=error
    )


@app.post('/admin/support/<int:request_id>/retry')
@main_admin_required
def retry_support_notification(request_id):
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            'SELECT * FROM support_requests WHERE id = ?',
            (request_id,)
        ).fetchone()
    if row is None:
        abort(404, description='Support request not found.')
    support_request = dict(row)
    try:
        send_support_notification(support_request)
    except RuntimeError as send_error:
        update_support_email_status(request_id, 'pending', str(send_error))
        flash(
            f'Request #{request_id} remains pending: {send_error}',
            'warning'
        )
    except (OSError, smtplib.SMTPException, ValueError) as send_error:
        logging.exception('Could not email support request %s.', request_id)
        update_support_email_status(request_id, 'failed', str(send_error))
        flash(
            f'Email notification for request #{request_id} failed again.',
            'error'
        )
    else:
        update_support_email_status(request_id, 'sent')
        flash(f'Email notification for request #{request_id} was sent.', 'success')
    return redirect(url_for('help'))


@app.route('/admin/exam', methods=['GET', 'POST'])
@admin_required
def teacher_exam():
    selected_book = normalize_book(
        request.form.get('book') if request.method == 'POST'
        else request.args.get('book')
    )
    selected_exam = normalize_exam(
        request.form.get('exam') if request.method == 'POST'
        else request.args.get('exam')
    )
    if request.method == 'POST':
        session.pop('question_order', None)
        session.pop('question_order_context', None)
        session['exam_attempt'] = {
            'book': selected_book,
            'exam': selected_exam,
            'started_at': time.time()
        }
        session['teacher_exam_access'] = {
            'book': selected_book,
            'exam': selected_exam
        }
        session['monitor_id'] = uuid.uuid4().hex
        return redirect(url_for(
            'index',
            book=selected_book,
            exam=selected_exam
        ))
    return render_template(
        'teacher_exam.html',
        books=list(BOOK_LEVELS),
        exams=EXAM_NAMES,
        selected_book=selected_book,
        selected_exam=selected_exam,
        teacher_user=False
    )


@app.route('/teacher/exam', methods=['GET', 'POST'])
@teacher_required
def teacher_exam_take():
    selected_book = normalize_book(
        request.form.get('book') if request.method == 'POST'
        else request.args.get('book')
    )
    selected_exam = normalize_exam(
        request.form.get('exam') if request.method == 'POST'
        else request.args.get('exam')
    )
    if request.method == 'POST':
        session.pop('question_order', None)
        session.pop('question_order_context', None)
        session['exam_attempt'] = {
            'book': selected_book,
            'exam': selected_exam,
            'started_at': time.time()
        }
        session['teacher_exam_access'] = {
            'book': selected_book,
            'exam': selected_exam
        }
        session['monitor_id'] = uuid.uuid4().hex
        return redirect(url_for(
            'index',
            book=selected_book,
            exam=selected_exam
        ))
    return render_template(
        'teacher_exam.html',
        books=list(BOOK_LEVELS),
        exams=EXAM_NAMES,
        selected_book=selected_book,
        selected_exam=selected_exam,
        teacher_user=True,
        teacher_account=get_teacher_account(
            session.get('teacher_username'),
            session.get('institution_id')
        )
    )


@app.get('/admin')
@admin_required
def admin_dashboard():
    data_scope = admin_data_scope()
    return render_template(
        'admin.html',
        analytics=get_dashboard_analytics(data_scope),
        interruptions=load_exam_interruptions(data_scope),
        institution=None if is_main_admin() else get_institution(current_institution_id())
    )


@app.get('/admin/teacher-attempts')
@admin_required
def teacher_exam_attempts():
    return render_template(
        'teacher_exam_attempts.html',
        attempts=load_teacher_exam_attempts(admin_data_scope())
    )


@app.post('/admin/end-exam')
@admin_required
def admin_end_exam():
    data_scope = admin_data_scope()
    monitor_id = request.form.get('monitor_id', '').strip()
    reason = request.form.get('reason', '').strip()
    if not monitor_id:
        return jsonify(error='Select an active exam to end.'), 400
    if not 5 <= len(reason) <= 500:
        return jsonify(error='Enter a reason between 5 and 500 characters.'), 400

    ended_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with closing(sqlite3.connect(ACTIVITY_DB_FILE, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            student = connection.execute(
                '''
                SELECT monitor_id, institution_id, name, book, level, exam,
                       started_at, answered_count, total_questions, tab_switches
                FROM active_students
                WHERE monitor_id = ?
                  AND (? IS NULL OR institution_id = ?)
                ''',
                (monitor_id, data_scope, data_scope)
            ).fetchone()
            if not student:
                return jsonify(error='That student is no longer in an active exam.'), 404
            connection.execute(
                '''
                INSERT INTO exam_interruptions (
                    monitor_id, institution_id, name, book, level, exam, reason, ended_by,
                    ended_at, started_at, answered_count, total_questions, tab_switches
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    student['monitor_id'],
                    student['institution_id'],
                    student['name'],
                    student['book'],
                    student['level'],
                    student['exam'],
                    reason,
                    'Administrator',
                    ended_at,
                    student['started_at'],
                    student['answered_count'],
                    student['total_questions'],
                    student['tab_switches']
                )
            )
            connection.execute(
                '''
                DELETE FROM active_students
                WHERE monitor_id = ? AND institution_id = ?
                ''',
                (monitor_id, student['institution_id'])
            )

    return jsonify(message=f"Ended {student['name']}'s {student['exam']} attempt.")


@app.get('/admin/writing')
@admin_required
def writing_review():
    return render_template(
        'writing_review.html',
        submissions=load_writing_submissions(admin_data_scope())
    )


@app.get('/admin/activity')
@admin_required
def admin_activity():
    include_answers = request.args.get('include_answers') == 'true'
    return jsonify(students=get_active_students(
        admin_data_scope(),
        include_answers=include_answers
    ))


@app.route('/results')
@admin_required
def results_page():
    rows = load_results(admin_data_scope())
    return render_template('results.html', results=rows)


@app.route('/download-results')
@admin_required
def download_results():
    output = io.StringIO(newline='')
    fieldnames = [
        'first_name', 'last_name', 'email', 'score', 'total', 'percentage',
        'cefr_band', 'tab_switches', 'level', 'book', 'exam', 'submitted_at',
        'institution_name', 'institution_code'
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for result in load_results(admin_data_scope()):
        writer.writerow({field: result.get(field, '') for field in fieldnames})
    csv_file = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    return send_file(
        csv_file,
        mimetype='text/csv',
        as_attachment=True,
        download_name='english_test_results.csv'
    )


if __name__ == '__main__':
    app.run(debug=True)
    