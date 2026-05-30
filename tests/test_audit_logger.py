"""
Tests for 用户层.audit_logger: AuditLogger logging, query, export, integrity
"""

import pytest
import os
import tempfile
from datetime import datetime, timedelta

from 用户层.audit_logger import AuditLogger


@pytest.fixture
def audit(tmp_path):
    """Create AuditLogger with temp database"""
    db_path = str(tmp_path / 'test_audit.db')
    return AuditLogger(db_path=db_path)


# ============================================================
# Initialization Tests
# ============================================================

class TestInit:

    def test_creates_database_file(self, audit):
        """AuditLogger creates database file"""
        assert os.path.exists(audit.db_path)

    def test_table_exists(self, audit):
        """Audit table is created"""
        import sqlite3
        conn = sqlite3.connect(audit.db_path)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'")
        assert cursor.fetchone() is not None
        conn.close()


# ============================================================
# Log Operation Tests
# ============================================================

class TestLogOperation:

    def test_log_operation_basic(self, audit):
        """log_operation writes a record"""
        audit.log_operation(user='testuser', action='set_value', target='dev1/temp', value=50.0, result='success')
        records = audit.query()
        assert len(records) == 1
        assert records[0]['user_name'] == 'testuser'
        assert records[0]['action'] == 'set_value'

    def test_log_operation_with_all_fields(self, audit):
        """log_operation stores all fields"""
        audit.log_operation(
            user='admin', action='start_device', target='dev1',
            value={'speed': 100}, reason='Maintenance test',
            result='success', detail='Started successfully',
            role='admin', ip_address='192.168.1.1'
        )
        records = audit.query()
        assert len(records) == 1
        assert records[0]['user_role'] == 'admin'
        assert records[0]['ip_address'] == '192.168.1.1'

    def test_log_operation_dict_value(self, audit):
        """log_operation serializes dict values as JSON"""
        audit.log_operation(user='u', action='test', target='t', value={'key': 'val'})
        records = audit.query()
        assert 'key' in records[0]['value']

    def test_log_operation_none_value(self, audit):
        """log_operation handles None value"""
        audit.log_operation(user='u', action='test', target='t', value=None)
        records = audit.query()
        assert records[0]['value'] == ''

    def test_log_operation_checksum(self, audit):
        """Each record has a SHA-256 checksum"""
        audit.log_operation(user='u', action='test', target='t', result='success')
        records = audit.query()
        assert len(records[0]['checksum']) == 64  # SHA-256 hex

    def test_log_operation_multiple_records(self, audit):
        """Multiple log entries are all stored"""
        for i in range(10):
            audit.log_operation(user='u', action=f'action_{i}', target='t', result='success')
        records = audit.query()
        assert len(records) == 10


# ============================================================
# Query Tests
# ============================================================

class TestQuery:

    def _seed_data(self, audit):
        audit.log_operation(user='alice', action='set_value', target='dev1/temp', result='success')
        audit.log_operation(user='bob', action='start_device', target='dev2', result='success')
        audit.log_operation(user='alice', action='set_value', target='dev1/pressure', result='failed')

    def test_query_all(self, audit):
        """query() returns all records"""
        self._seed_data(audit)
        assert len(audit.query()) == 3

    def test_query_by_user(self, audit):
        """query filters by user"""
        self._seed_data(audit)
        records = audit.query(user='alice')
        assert len(records) == 2
        assert all(r['user_name'] == 'alice' for r in records)

    def test_query_by_action(self, audit):
        """query filters by action"""
        self._seed_data(audit)
        records = audit.query(action='set_value')
        assert len(records) == 2

    def test_query_by_target(self, audit):
        """query filters by target (LIKE match)"""
        self._seed_data(audit)
        records = audit.query(target='dev1')
        assert len(records) == 2

    def test_query_by_result(self, audit):
        """query filters by result"""
        self._seed_data(audit)
        records = audit.query(result='failed')
        assert len(records) == 1

    def test_query_by_time_range(self, audit):
        """query filters by time range"""
        self._seed_data(audit)
        now = datetime.now()
        records = audit.query(start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1))
        assert len(records) == 3

    def test_query_limit(self, audit):
        """query respects limit"""
        for i in range(20):
            audit.log_operation(user='u', action='a', target='t', result='success')
        records = audit.query(limit=5)
        assert len(records) == 5

    def test_query_empty(self, audit):
        """query on empty DB returns empty list"""
        assert audit.query() == []

    def test_query_combined_filters(self, audit):
        """query with multiple filters"""
        self._seed_data(audit)
        records = audit.query(user='alice', result='failed')
        assert len(records) == 1


# ============================================================
# Stats Tests
# ============================================================

class TestStats:

    def test_get_operation_stats(self, audit):
        """get_operation_stats returns statistics"""
        audit.log_operation(user='u1', action='set_value', target='t', result='success')
        audit.log_operation(user='u1', action='start_device', target='t', result='success')
        audit.log_operation(user='u2', action='set_value', target='t', result='failed')
        stats = audit.get_operation_stats()
        assert 'total_records' in stats
        assert stats['total_records'] == 3
        assert 'today_by_action' in stats
        assert 'today_by_user' in stats
        assert 'today_by_result' in stats

    def test_get_operation_stats_empty(self, audit):
        """get_operation_stats on empty DB"""
        stats = audit.get_operation_stats()
        assert stats['total_records'] == 0


# ============================================================
# Export Tests
# ============================================================

class TestExport:

    def test_export_csv(self, audit, tmp_path):
        """export_csv creates CSV file"""
        audit.log_operation(user='u', action='test', target='t', result='success')
        output = str(tmp_path / 'export.csv')
        count = audit.export_csv(output)
        assert count == 1
        assert os.path.exists(output)

    def test_export_csv_empty(self, audit, tmp_path):
        """export_csv returns 0 for empty DB"""
        output = str(tmp_path / 'export_empty.csv')
        count = audit.export_csv(output)
        assert count == 0

    def test_export_csv_content(self, audit, tmp_path):
        """export_csv contains header and data"""
        audit.log_operation(user='u', action='test', target='t', result='success')
        output = str(tmp_path / 'export.csv')
        audit.export_csv(output)
        with open(output, 'r', encoding='utf-8-sig') as f:
            lines = f.readlines()
        assert len(lines) >= 2  # header + data
        assert 'user_name' in lines[0]


# ============================================================
# Integrity Verification Tests
# ============================================================

class TestIntegrity:

    def test_verify_integrity_empty(self, audit):
        """verify_integrity on empty DB returns OK"""
        result = audit.verify_integrity()
        assert result['integrity'] == 'OK'
        assert result['total_records'] == 0

    def test_verify_integrity_valid(self, audit):
        """verify_integrity returns OK for untampered records"""
        for i in range(5):
            audit.log_operation(user='u', action='test', target=f't{i}', result='success')
        result = audit.verify_integrity()
        assert result['integrity'] == 'OK'
        assert result['valid'] == 5
        assert result['invalid'] == 0

    def test_verify_integrity_detects_tamper(self, audit):
        """verify_integrity detects tampered checksum"""
        audit.log_operation(user='u', action='test', target='t', result='success')
        # Tamper with the checksum
        import sqlite3
        conn = sqlite3.connect(audit.db_path)
        conn.execute("UPDATE audit_log SET checksum = 'tampered' WHERE id = 1")
        conn.commit()
        conn.close()
        result = audit.verify_integrity()
        assert result['integrity'] == 'COMPROMISED'
        assert result['invalid'] == 1
        assert 1 in result['invalid_ids']
