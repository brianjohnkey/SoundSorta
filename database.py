import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "sample_archive.db")

SUPPORTED_EXTENSIONS = ('.wav', '.flac', '.mp3', '.ogg', '.aif', '.aiff')


def get_connection():
    """Every caller (UI thread, scan thread) opens its own short-lived connection."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")  # safe with WAL, much faster commits
    return conn


def _ensure_tag_color_column(cursor):
    """Older databases may lack the 'color' column on tags."""
    cursor.execute("PRAGMA table_info(tags)")
    columns = [col[1] for col in cursor.fetchall()]
    if columns and "color" not in columns:
        try:
            cursor.execute("ALTER TABLE tags ADD COLUMN color TEXT DEFAULT '#255d8f'")
        except sqlite3.OperationalError:
            pass


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # WAL is stored in the database file, so this only needs to succeed once,
    # but it is harmless to re-assert on every start.
    cursor.execute("PRAGMA journal_mode = WAL;")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE,
            file_name TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            color TEXT DEFAULT '#255d8f'
        )
    """)
    _ensure_tag_color_column(cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sample_tags (
            sample_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY (sample_id, tag_id),
            FOREIGN KEY (sample_id) REFERENCES samples(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)
    # The primary key covers lookups by sample; this covers lookups/counts by tag.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sample_tags_tag ON sample_tags(tag_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS library_roots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE
        )
    """)

    conn.commit()
    conn.close()


def find_audio_files(directory_path, should_cancel=None):
    """Walks a directory tree and returns normalized paths of supported audio files."""
    found = []
    for root, _, files in os.walk(directory_path):
        if should_cancel and should_cancel():
            break
        for file in files:
            if file.lower().endswith(SUPPORTED_EXTENSIONS):
                found.append(os.path.normpath(os.path.join(root, file)))
    return found


def index_files(paths, progress_callback=None, should_cancel=None, batch_size=500):
    """
    Inserts file paths into the samples table in batched transactions.
    Returns the number of files processed (existing rows are left untouched).
    """
    conn = get_connection()
    cursor = conn.cursor()
    processed = 0
    batch = []

    try:
        for path in paths:
            if should_cancel and should_cancel():
                break
            batch.append((path, os.path.basename(path)))
            processed += 1

            if len(batch) >= batch_size:
                cursor.executemany(
                    "INSERT OR IGNORE INTO samples (file_path, file_name) VALUES (?, ?)", batch
                )
                conn.commit()
                batch = []

            if progress_callback and processed % 25 == 0:
                progress_callback(processed, path)

        if batch:
            cursor.executemany(
                "INSERT OR IGNORE INTO samples (file_path, file_name) VALUES (?, ?)", batch
            )
        conn.commit()
    finally:
        conn.close()

    return processed


def scan_directory(directory_path, progress_callback=None):
    """Convenience wrapper: walk a folder and index everything found."""
    return index_files(find_audio_files(directory_path), progress_callback=progress_callback)


def delete_sample_from_db(file_path):
    delete_samples_from_db([file_path])


def delete_samples_from_db(file_paths):
    """Removes many sample records in a single transaction."""
    paths = [(os.path.normpath(p),) for p in file_paths]
    if not paths:
        return
    conn = get_connection()
    try:
        conn.executemany("DELETE FROM samples WHERE file_path = ?", paths)
        conn.commit()
    finally:
        conn.close()


def reset_database():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sample_tags")
    cursor.execute("DELETE FROM samples")
    cursor.execute("DELETE FROM tags")
    cursor.execute("DELETE FROM library_roots")
    conn.commit()

    # VACUUM requires autocommit mode (no active transaction)
    isolation = conn.isolation_level
    conn.isolation_level = None
    cursor.execute("VACUUM")
    conn.isolation_level = isolation

    conn.close()


def add_library_root(path):
    conn = get_connection()
    cursor = conn.cursor()
    norm_path = os.path.normpath(path)
    cursor.execute("INSERT OR IGNORE INTO library_roots (path) VALUES (?)", (norm_path,))
    conn.commit()
    conn.close()


def get_library_roots():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT path FROM library_roots ORDER BY path ASC")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def remove_library_root(path):
    conn = get_connection()
    cursor = conn.cursor()
    norm_path = os.path.normpath(path)
    cursor.execute("DELETE FROM library_roots WHERE path = ?", (norm_path,))
    conn.commit()
    conn.close()