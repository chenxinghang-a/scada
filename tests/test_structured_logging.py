"""
Tests for structured logging and API common utilities - covers core/structured_logging.py (0%) and 展示层/api/_common.py (42%)
"""
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


class TestStructuredLogging:
    @patch('loguru.logger')
    def test_setup_logging_json(self, mock_logger):
        from core.structured_logging import setup_logging

        tmp = tempfile.mkdtemp()
        try:
            result = setup_logging(log_dir=tmp, log_level='DEBUG', json_format=True)
            assert result is not None
            mock_logger.remove.assert_called_once()
            assert mock_logger.add.call_count >= 3  # console + app log + audit + error
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    @patch('loguru.logger')
    def test_setup_logging_human_readable(self, mock_logger):
        from core.structured_logging import setup_logging

        tmp = tempfile.mkdtemp()
        try:
            result = setup_logging(log_dir=tmp, log_level='INFO', json_format=False)
            assert result is not None
            mock_logger.remove.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    @patch('loguru.logger')
    def test_setup_logging_creates_dir(self, mock_logger):
        from core.structured_logging import setup_logging

        tmp = tempfile.mkdtemp()
        log_dir = os.path.join(tmp, 'new_logs')
        try:
            setup_logging(log_dir=log_dir)
            assert os.path.exists(log_dir)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_logger(self):
        from core.structured_logging import get_logger

        logger = get_logger('test_module')
        assert logger is not None

    def test_get_logger_audit(self):
        from core.structured_logging import get_logger

        logger = get_logger('audit_module', audit=True)
        assert logger is not None


class TestAPICommon:
    def test_load_yaml_config_existing_file(self):
        import yaml
        from 展示层.api._common import load_yaml_config

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump({'key': 'value', 'nested': {'a': 1}}, tmp)
        tmp.close()

        try:
            result = load_yaml_config(tmp.name)
            assert result['key'] == 'value'
            assert result['nested']['a'] == 1
        finally:
            os.unlink(tmp.name)

    def test_load_yaml_config_nonexistent(self):
        from 展示层.api._common import load_yaml_config

        result = load_yaml_config('/nonexistent/path/config.yaml')
        assert result == {}

    def test_save_yaml_config_success(self):
        import yaml
        from 展示层.api._common import save_yaml_config

        tmp = tempfile.mkdtemp()
        filepath = os.path.join(tmp, 'test_config.yaml')

        try:
            result = save_yaml_config(filepath, {'key': 'value', 'number': 42})
            assert result is True
            assert os.path.exists(filepath)

            with open(filepath, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
            assert loaded['key'] == 'value'
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_yaml_config_creates_dirs(self):
        from 展示层.api._common import save_yaml_config

        tmp = tempfile.mkdtemp()
        filepath = os.path.join(tmp, 'subdir', 'nested', 'config.yaml')

        try:
            result = save_yaml_config(filepath, {'a': 1})
            assert result is True
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_yaml_config_error(self):
        from 展示层.api._common import save_yaml_config

        result = save_yaml_config('/invalid/\x00/path', {'a': 1})
        assert result is False

    def test_get_auth_manager(self):
        from 展示层.api._common import get_auth_manager
        from flask import Flask

        app = Flask(__name__)
        mock_auth = MagicMock()
        app.auth_manager = mock_auth

        with app.app_context():
            result = get_auth_manager()
            assert result is mock_auth
