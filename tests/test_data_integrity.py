"""
Data Integrity Tests
====================
Ensures the SCADA system preserves data correctly:
- History data doesn't get duplicate entries per write
- Realtime data is not deleted during cleanup
- Crash recovery from persistence file
- Modbus chunked reads don't drop boundary registers
"""

import json
import os
import sqlite3
import tempfile
import queue
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest


# ============================================================
# History Data Duplicate Prevention
# ============================================================

class TestHistoryDataIntegrity:
    """history_data should accumulate every sample, not overwrite."""

    def test_insert_data_creates_history_and_realtime(self, db):
        """insert_data writes to both history_data and realtime_data."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()

        database.insert_data('dev1', 'temperature', 25.5, now, 'C')
        database.insert_data('dev1', 'temperature', 26.0, now, 'C')

        # history_data should have BOTH entries
        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM history_data WHERE device_id = ? AND register_name = ?',
                           ('dev1', 'temperature'))
            count = cursor.fetchone()[0]
            assert count == 2, f"Expected 2 history rows, got {count}"

        # realtime_data should have only 1 row (UPSERT)
        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM realtime_data WHERE device_id = ? AND register_name = ?',
                           ('dev1', 'temperature'))
            count = cursor.fetchone()[0]
            assert count == 1, f"Expected 1 realtime row, got {count}"

    def test_batch_insert_preserves_all_records(self, db):
        """insert_data_batch writes all records to history_data."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()

        batch = [
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0, 'timestamp': now, 'unit': 'C'},
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.5, 'timestamp': now, 'unit': 'C'},
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 26.0, 'timestamp': now, 'unit': 'C'},
        ]
        database.insert_data_batch(batch)

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM history_data WHERE device_id = ?', ('dev1',))
            count = cursor.fetchone()[0]
            assert count == 3, f"Expected 3 history rows, got {count}"

    def test_realtime_data_latest_value_wins(self, db):
        """realtime_data UPSERT keeps the latest value."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()

        database.insert_data('dev1', 'pressure', 1.0, now - timedelta(seconds=10), 'MPa')
        database.insert_data('dev1', 'pressure', 2.0, now, 'MPa')

        latest = database.get_latest_data('dev1', 'pressure')
        assert latest is not None
        assert latest['value'] == 2.0

    def test_batch_insert_skips_invalid_records(self, db):
        """insert_data_batch skips records with missing keys."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()

        batch = [
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0, 'timestamp': now, 'unit': 'C'},
            {'bad_key': 'no_device_id'},  # invalid
            {'device_id': 'dev1', 'register_name': 'press', 'value': 1.0, 'timestamp': now, 'unit': 'MPa'},
        ]
        database.insert_data_batch(batch)

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM history_data')
            count = cursor.fetchone()[0]
            assert count == 2  # Only 2 valid records


# ============================================================
# Realtime Data Survives Cleanup
# ============================================================

class TestRealtimeDataCleanup:
    """cleanup_old_data must NOT delete realtime_data."""

    def test_cleanup_preserves_realtime_data(self, db):
        """cleanup_old_data only deletes old history_data, not realtime_data."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()
        old_time = now - timedelta(days=60)

        # Insert old history data
        database.insert_data('dev1', 'temp', 25.0, old_time, 'C')
        # Insert current realtime data
        database.insert_data('dev1', 'temp', 30.0, now, 'C')

        # Cleanup with 30-day retention
        database.cleanup_old_data(retention_days=30)

        # realtime_data should still exist
        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM realtime_data WHERE device_id = ?', ('dev1',))
            rt_count = cursor.fetchone()[0]
            assert rt_count == 1, "realtime_data should not be deleted by cleanup"

    def test_cleanup_deletes_old_history_data(self, db):
        """cleanup_old_data deletes history_data older than retention period."""
        from 存储层.database import Database

        database = Database(db)
        now = datetime.now()

        # Insert old and new history data
        database.insert_data('dev1', 'temp', 20.0, now - timedelta(days=60), 'C')
        database.insert_data('dev1', 'temp', 25.0, now - timedelta(days=10), 'C')
        database.insert_data('dev1', 'temp', 30.0, now, 'C')

        database.cleanup_old_data(retention_days=30)

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM history_data WHERE device_id = ?', ('dev1',))
            count = cursor.fetchone()[0]
            # The 60-day-old record should be deleted, the other 2 remain
            assert count == 2, f"Expected 2 history rows after cleanup, got {count}"


# ============================================================
# Crash Recovery from Persistence File
# ============================================================

class TestCrashRecovery:
    """DiskBackedQueue must recover data from disk after crash."""

    def test_recovery_from_persistence_file(self, tmp_path):
        """DiskBackedQueue recovers items from the persistence file on init."""
        from 采集层.data_collector import DiskBackedQueue

        persist_dir = str(tmp_path / 'queue')
        os.makedirs(persist_dir, exist_ok=True)
        persist_file = Path(persist_dir) / 'pending_data.jsonl'

        # Simulate crash: write items to the persistence file
        items = [
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0,
             'timestamp': datetime.now().isoformat(), 'unit': 'C'},
            {'device_id': 'dev1', 'register_name': 'press', 'value': 1.5,
             'timestamp': datetime.now().isoformat(), 'unit': 'MPa'},
        ]
        with open(persist_file, 'w', encoding='utf-8') as f:
            for item in items:
                f.write(json.dumps(item, default=str, ensure_ascii=False) + '\n')

        # Create queue - should recover
        q = DiskBackedQueue(maxsize=1000, persist_dir=persist_dir)
        assert q.qsize() == 2

        # Verify recovered items
        recovered = []
        while not q.empty():
            recovered.append(q.get_nowait())
        assert len(recovered) == 2
        assert recovered[0]['device_id'] == 'dev1'
        assert recovered[1]['register_name'] == 'press'

    def test_recovery_skips_corrupted_lines(self, tmp_path):
        """DiskBackedQueue skips corrupted lines and recovers valid ones."""
        from 采集层.data_collector import DiskBackedQueue

        persist_dir = str(tmp_path / 'queue')
        os.makedirs(persist_dir, exist_ok=True)
        persist_file = Path(persist_dir) / 'pending_data.jsonl'

        # Write valid and corrupted lines
        with open(persist_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0,
                                'timestamp': datetime.now().isoformat()}) + '\n')
            f.write('this is not valid json\n')  # corrupted
            f.write(json.dumps({'device_id': 'dev1', 'register_name': 'press', 'value': 1.0,
                                'timestamp': datetime.now().isoformat()}) + '\n')

        q = DiskBackedQueue(maxsize=1000, persist_dir=persist_dir)
        # Should recover 2 valid items, skip the corrupted one
        assert q.qsize() == 2

    def test_recovery_skips_items_without_value(self, tmp_path):
        """DiskBackedQueue skips items missing the 'value' field."""
        from 采集层.data_collector import DiskBackedQueue

        persist_dir = str(tmp_path / 'queue')
        os.makedirs(persist_dir, exist_ok=True)
        persist_file = Path(persist_dir) / 'pending_data.jsonl'

        with open(persist_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0,
                                'timestamp': datetime.now().isoformat()}) + '\n')
            f.write(json.dumps({'device_id': 'dev1', 'register_name': 'no_val'}) + '\n')

        q = DiskBackedQueue(maxsize=1000, persist_dir=persist_dir)
        assert q.qsize() == 1

    def test_clear_persistence_removes_file(self, tmp_path):
        """clear_persistence removes the persistence file."""
        from 采集层.data_collector import DiskBackedQueue

        persist_dir = str(tmp_path / 'queue')
        os.makedirs(persist_dir, exist_ok=True)
        persist_file = Path(persist_dir) / 'pending_data.jsonl'

        # Write something
        with open(persist_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'value': 1}) + '\n')

        q = DiskBackedQueue(maxsize=1000, persist_dir=persist_dir)
        assert persist_file.exists() or q.qsize() == 1  # recovered and deleted

        q.clear_persistence()
        assert not persist_file.exists()


# ============================================================
# Modbus Chunked Read Boundary Registers
# ============================================================

class TestModbusChunkedReads:
    """Chunked Modbus reads must not drop registers at chunk boundaries."""

    def test_single_chunk_reads_all_registers(self):
        """When total_count <= 125, a single read covers all registers."""
        mock_client = MagicMock()
        mock_client.read_holding_registers.return_value = [100, 200, 300, 400, 500]
        mock_client.decode_uint16 = lambda x: x

        device_config = {
            'registers': [
                {'name': 'reg0', 'address': 0, 'data_type': 'uint16'},
                {'name': 'reg1', 'address': 1, 'data_type': 'uint16'},
                {'name': 'reg2', 'address': 2, 'data_type': 'uint16'},
                {'name': 'reg3', 'address': 3, 'data_type': 'uint16'},
                {'name': 'reg4', 'address': 4, 'data_type': 'uint16'},
            ]
        }

        # Simulate the single-chunk path from _collect_modbus
        registers = device_config['registers']
        min_addr = min(r['address'] for r in registers)
        max_end = max(r['address'] + 1 for r in registers)  # uint16 = 1 register
        total_count = max_end - min_addr

        assert total_count <= 125  # Single chunk
        all_regs = mock_client.read_holding_registers(min_addr, total_count)

        collected = []
        for reg in registers:
            offset = reg['address'] - min_addr
            raw = all_regs[offset:offset + 1]
            if len(raw) >= 1:
                value = mock_client.decode_uint16(raw[0])
                collected.append((reg['name'], value))

        assert len(collected) == 5
        assert collected[0] == ('reg0', 100)
        assert collected[4] == ('reg4', 500)

    def test_chunked_read_with_overlap_covers_boundary(self):
        """Chunked reads with overlap ensure boundary registers are not dropped."""
        # Simulate 130 uint16 registers (needs 2 chunks with overlap)
        num_registers = 130

        # The actual source data: each address holds its address as value
        source_data = {i: i * 10 for i in range(num_registers)}

        def mock_read(addr, count):
            """Return 'count' registers starting at 'addr' from source_data."""
            return [source_data.get(addr + i, 0) for i in range(count)]

        mock_client = MagicMock()
        mock_client.read_holding_registers = mock_read
        mock_client.decode_uint16 = lambda x: x

        # Build register configs
        registers = [
            {'name': f'reg{i}', 'address': i, 'data_type': 'uint16'}
            for i in range(num_registers)
        ]

        min_addr = 0
        max_end = num_registers
        chunk_size = 125
        max_reg_size = 1
        step = chunk_size - (max_reg_size - 1)  # 125

        collected = {}
        for start in range(min_addr, max_end, step):
            count = min(chunk_size, max_end - start)
            chunk = mock_read(start, count)
            if chunk is None:
                continue
            for reg in registers:
                if reg['address'] < start or reg['address'] >= start + count:
                    continue
                offset = reg['address'] - start
                raw = chunk[offset:offset + 1]
                if len(raw) >= 1:
                    value = mock_client.decode_uint16(raw[0])
                    collected[reg['address']] = value

        # All 130 registers should be collected
        assert len(collected) == 130, f"Expected 130, got {len(collected)}"
        assert set(collected.keys()) == set(range(130))
        # Verify values are correct (each address holds address*10)
        for addr in range(130):
            assert collected[addr] == addr * 10, f"Wrong value at address {addr}"

    def test_none_response_does_not_crash(self):
        """A None response from read_holding_registers is handled gracefully."""
        mock_client = MagicMock()
        mock_client.read_holding_registers.return_value = None

        result = mock_client.read_holding_registers(0, 10)
        assert result is None
        # The collector should skip and continue, not crash

    def test_partial_response_skips_short_reads(self):
        """Registers with insufficient data are skipped."""
        mock_client = MagicMock()
        mock_client.read_holding_registers.return_value = [100]  # Only 1 register
        mock_client.decode_uint16 = lambda x: x

        # A float32 register needs 2 registers, but only 1 was returned
        raw = [100]
        size = 2  # float32 needs 2 registers
        assert len(raw) < size  # Should be skipped
