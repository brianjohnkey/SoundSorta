import os
from database import get_connection


def create_tag(tag_name, color="#255d8f"):
    if not tag_name:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)", (tag_name, color))
    conn.commit()
    conn.close()


def update_tag_color(tag_name, color):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tags SET color = ? WHERE name = ?", (color, tag_name))
    conn.commit()
    conn.close()


def delete_tag(tag_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tags WHERE name = ?", (tag_name,))
    conn.commit()
    conn.close()


def get_all_tags():
    """
    Returns list of tuples: (tag_name, sample_count, color)
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.name, COUNT(st.sample_id) as count, COALESCE(t.color, '#255d8f')
        FROM tags t
        LEFT JOIN sample_tags st ON t.id = st.tag_id
        GROUP BY t.id, t.name
        ORDER BY t.name ASC
    """)
    results = cursor.fetchall()
    conn.close()
    return results


def _ensure_samples_exist(cursor, norm_paths):
    """Inserts any missing sample rows for the given normalized paths, in one batch."""
    rows = [(p, os.path.basename(p)) for p in norm_paths]
    cursor.executemany(
        "INSERT OR IGNORE INTO samples (file_path, file_name) VALUES (?, ?)", rows
    )


def _ensure_tags_exist(cursor, tag_names):
    """Inserts any missing tag rows for the given names, in one batch."""
    rows = [(name,) for name in tag_names]
    cursor.executemany("INSERT OR IGNORE INTO tags (name) VALUES (?)", rows)


def apply_tags_to_samples(file_paths, tag_names):
    """
    Applies every tag in tag_names to every sample in file_paths.
    Runs as a single transaction regardless of how many files/tags are involved —
    this replaces the old one-connection-per-file loop.
    """
    if not file_paths or not tag_names:
        return

    norm_paths = [os.path.normpath(p) for p in file_paths]

    conn = get_connection()
    cursor = conn.cursor()
    try:
        _ensure_samples_exist(cursor, norm_paths)
        _ensure_tags_exist(cursor, tag_names)

        path_placeholders = ','.join(['?'] * len(norm_paths))
        cursor.execute(
            f"SELECT id FROM samples WHERE file_path IN ({path_placeholders})", norm_paths
        )
        sample_ids = [r[0] for r in cursor.fetchall()]

        tag_placeholders = ','.join(['?'] * len(tag_names))
        cursor.execute(
            f"SELECT id FROM tags WHERE name IN ({tag_placeholders})", list(tag_names)
        )
        tag_ids = [r[0] for r in cursor.fetchall()]

        links = [(sid, tid) for sid in sample_ids for tid in tag_ids]
        cursor.executemany(
            "INSERT OR IGNORE INTO sample_tags (sample_id, tag_id) VALUES (?, ?)", links
        )
        conn.commit()
    finally:
        conn.close()


def remove_tags_from_samples(file_paths, tag_names):
    """
    Removes every tag in tag_names from every sample in file_paths, in one transaction.
    Counterpart to apply_tags_to_samples for the staged apply/remove workflow.
    """
    if not file_paths or not tag_names:
        return

    norm_paths = [os.path.normpath(p) for p in file_paths]

    conn = get_connection()
    cursor = conn.cursor()
    try:
        path_placeholders = ','.join(['?'] * len(norm_paths))
        cursor.execute(
            f"SELECT id FROM samples WHERE file_path IN ({path_placeholders})", norm_paths
        )
        sample_ids = [r[0] for r in cursor.fetchall()]
        if not sample_ids:
            return

        tag_placeholders = ','.join(['?'] * len(tag_names))
        cursor.execute(
            f"SELECT id FROM tags WHERE name IN ({tag_placeholders})", list(tag_names)
        )
        tag_ids = [r[0] for r in cursor.fetchall()]
        if not tag_ids:
            return

        sample_placeholders = ','.join(['?'] * len(sample_ids))
        tag_id_placeholders = ','.join(['?'] * len(tag_ids))
        cursor.execute(
            f"""
            DELETE FROM sample_tags
            WHERE sample_id IN ({sample_placeholders})
            AND tag_id IN ({tag_id_placeholders})
            """,
            sample_ids + tag_ids,
        )
        conn.commit()
    finally:
        conn.close()


def remove_tag_from_sample(file_path, tag_name):
    """Single-file, single-tag removal — kept for the tag chip 'x' on one sample."""
    remove_tags_from_samples([file_path], [tag_name])


def get_tag_state_for_samples(file_paths):
    """
    For the given samples (checked or selected), returns {tag_name: 'all' | 'some'}
    describing whether every one of those samples carries the tag, or only some do.
    A tag with no entry here is carried by none of them — that is the tri-state
    checkbox's third state, and callers should treat a missing key as 'none'.
    """
    if not file_paths:
        return {}

    norm_paths = [os.path.normpath(p) for p in file_paths]

    conn = get_connection()
    cursor = conn.cursor()
    try:
        path_placeholders = ','.join(['?'] * len(norm_paths))
        cursor.execute(
            f"SELECT id FROM samples WHERE file_path IN ({path_placeholders})", norm_paths
        )
        sample_ids = [r[0] for r in cursor.fetchall()]
        total = len(sample_ids)
        if total == 0:
            return {}

        id_placeholders = ','.join(['?'] * len(sample_ids))
        cursor.execute(
            f"""
            SELECT t.name, COUNT(DISTINCT st.sample_id)
            FROM tags t
            JOIN sample_tags st ON st.tag_id = t.id
            WHERE st.sample_id IN ({id_placeholders})
            GROUP BY t.id, t.name
            """,
            sample_ids,
        )
        counts = cursor.fetchall()
    finally:
        conn.close()

    return {name: ('all' if count == total else 'some') for name, count in counts}


def get_samples_by_tags(filter_tags=None):
    """
    Returns every matching sample (no row cap) with its tags and tag colors attached.
    Fetches all tag/color data for the result set in one query instead of one
    query per sample.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if not filter_tags:
            cursor.execute("SELECT id, file_path, file_name FROM samples ORDER BY file_name ASC")
            samples = cursor.fetchall()
        else:
            placeholders = ','.join(['?'] * len(filter_tags))
            query = f"""
                SELECT s.id, s.file_path, s.file_name
                FROM samples s
                JOIN sample_tags st ON s.id = st.sample_id
                JOIN tags t ON st.tag_id = t.id
                WHERE t.name IN ({placeholders})
                GROUP BY s.id
                HAVING COUNT(DISTINCT t.name) = ?
                ORDER BY s.file_name ASC
            """
            cursor.execute(query, (*filter_tags, len(filter_tags)))
            samples = cursor.fetchall()

        if not samples:
            return []

        sample_ids = [s[0] for s in samples]
        id_placeholders = ','.join(['?'] * len(sample_ids))
        cursor.execute(
            f"""
            SELECT st.sample_id, t.name, COALESCE(t.color, '#255d8f')
            FROM sample_tags st
            JOIN tags t ON t.id = st.tag_id
            WHERE st.sample_id IN ({id_placeholders})
            """,
            sample_ids,
        )
        tags_by_sample = {}
        for sample_id, tag_name, color in cursor.fetchall():
            tags_by_sample.setdefault(sample_id, []).append((tag_name, color))
    finally:
        conn.close()

    results = []
    for sample_id, file_path, file_name in samples:
        tag_tuples = tags_by_sample.get(sample_id, [])
        results.append({
            'id': sample_id,
            'file_path': file_path,
            'file_name': file_name,
            'tags': [t[0] for t in tag_tuples],
            'tag_colors': {t[0]: t[1] for t in tag_tuples},
        })

    return results