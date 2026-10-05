"""Unread projection stays exact without a historical Session/event product."""
def test_unread_counts_handle_sparse_numbers_other_projects_and_future_cursors(service, project, tmp_path):
    sessions = [service.join_room(project['id'], name=f'Worker {i}', client='test', model='test') for i in range(4)]
    other = tmp_path / 'other'
    other.mkdir()
    other_project = service.create_project(root_path=str(other), name='Other')
    service.join_room(other_project['id'], name='Other Worker', client='test', model='test')
    with service.database.connect(write=True) as db:
        # Imported event histories can have gaps; keep every event row.
        db.execute('UPDATE events SET project_seq = project_seq * 10 WHERE project_id = ?', (project['id'],))
        for session, cursor in zip(sessions, (0, 10, 25, 10000)):
            db.execute('UPDATE agent_sessions SET last_read_cursor = ? WHERE id = ?', (cursor, session['agent']['id']))
    with service.database.connect() as db:
        rows, total = service._session_activity_rows(db, project['id'], limit=3)
        assert total == 4
        assert len(rows) == 3
        for row in rows:
            expected = db.execute('SELECT COUNT(*) AS c FROM events WHERE project_id = ? AND project_seq > ?',
                                  (project['id'], row['last_read_cursor'])).fetchone()['c']
            assert row['unread_count'] == expected


def test_unread_query_work_is_bounded_by_events_and_returned_sessions(service, project):
    for i in range(60):
        service.join_room(project['id'], name=f'Worker {i}', client='test', model='test')
    with service.database.connect(write=True) as db:
        latest = db.execute('SELECT MAX(project_seq) AS n FROM events WHERE project_id = ?', (project['id'],)).fetchone()['n']
        db.executemany(
            'INSERT INTO events(project_id,event_type,payload_json,project_seq,created_at) VALUES (?, ?, ?, ?, ?)',
            [(project['id'], 'message.message', '{}', latest + i + 1, '2026-01-01T00:00:00Z') for i in range(2000)],
        )
        db.execute('UPDATE agent_sessions SET last_read_cursor = 0 WHERE project_id = ?', (project['id'],))
    ticks = []
    with service.database.connect() as db:
        db.set_progress_handler(lambda: ticks.append(1) or 0, 1000)
        rows, total = service._session_activity_rows(db, project['id'], limit=10)
        db.set_progress_handler(None, 0)
    assert total == 60 and len(rows) == 10
    assert all(row['unread_count'] == latest + 2000 for row in rows)
    # Bound SQLite VM work, not wall-clock time. The former range join crosses
    # 120,000 unread pairs even though only ten Sessions are returned.
    assert len(ticks) < 100
