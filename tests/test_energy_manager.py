"""
Tests for 智能层.energy_manager: EnergyManager tariff, power data, carbon, anomaly
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock
from pathlib import Path

from 智能层.energy_manager import EnergyManager, DEFAULT_CONFIG


@pytest.fixture
def em(tmp_path):
    """Create EnergyManager with mocked database and temp config path"""
    return EnergyManager(
        database=MagicMock(),
        config_path=tmp_path / 'energy.yaml'
    )


# ============================================================
# Initialization Tests
# ============================================================

class TestInit:

    def test_default_tariff(self, em):
        """Default tariff values are loaded"""
        assert em.tariff['peak'] == DEFAULT_CONFIG['tariff']['peak']
        assert em.tariff['flat'] == DEFAULT_CONFIG['tariff']['flat']
        assert em.tariff['valley'] == DEFAULT_CONFIG['tariff']['valley']

    def test_default_carbon_factor(self, em):
        """Default carbon factor is loaded"""
        assert em.carbon_factor == DEFAULT_CONFIG['carbon_factor']

    def test_default_anomaly_config(self, em):
        """Default anomaly config is loaded"""
        assert 'threshold_multiplier' in em.anomaly_config
        assert 'warning_multiplier' in em.anomaly_config

    def test_custom_config_override(self, tmp_path):
        """Custom config overrides defaults"""
        em = EnergyManager(
            database=MagicMock(),
            config={'tariff': {'peak': 2.0}, 'carbon_factor': 1.0},
            config_path=tmp_path / 'energy.yaml'
        )
        assert em.tariff['peak'] == 2.0
        assert em.carbon_factor == 1.0
        # Non-overridden values should remain default
        assert em.tariff['flat'] == DEFAULT_CONFIG['tariff']['flat']


# ============================================================
# Tariff Config Tests
# ============================================================

class TestTariffConfig:

    def test_get_tariff_config(self, em):
        """get_tariff_config returns current config"""
        config = em.get_tariff_config()
        assert 'tariff' in config
        assert 'tariff_periods' in config
        assert 'carbon_factor' in config

    def test_get_tariff_config_returns_copy(self, em):
        """get_tariff_config returns a copy (not internal reference)"""
        config = em.get_tariff_config()
        config['tariff']['peak'] = 999
        assert em.tariff['peak'] != 999

    def test_update_tariff_success(self, em, tmp_path):
        """update_tariff updates tariff values"""
        result = em.update_tariff(tariff={'peak': 2.0})
        assert result['success'] is True
        assert em.tariff['peak'] == 2.0

    def test_update_tariff_negative_rejected(self, em):
        """update_tariff rejects negative tariff"""
        result = em.update_tariff(tariff={'peak': -1.0})
        assert result['success'] is False
        assert '非负数' in result['message']

    def test_update_tariff_periods_success(self, em):
        """update_tariff updates time periods"""
        result = em.update_tariff(tariff_periods={'peak': [[9, 12]]})
        assert result['success'] is True

    def test_update_tariff_periods_invalid_type(self, em):
        """update_tariff rejects invalid period type"""
        result = em.update_tariff(tariff_periods={'invalid': [[9, 12]]})
        assert result['success'] is False

    def test_update_tariff_periods_invalid_range(self, em):
        """update_tariff rejects invalid time range"""
        result = em.update_tariff(tariff_periods={'peak': [[25, 30]]})
        assert result['success'] is False

    def test_update_tariff_periods_bad_format(self, em):
        """update_tariff rejects bad period format"""
        result = em.update_tariff(tariff_periods={'peak': [9, 12]})  # not a list of lists
        assert result['success'] is False

    def test_update_carbon_factor(self, em):
        """update_tariff updates carbon factor"""
        result = em.update_tariff(carbon_factor=0.8)
        assert result['success'] is True
        assert em.carbon_factor == 0.8

    def test_update_carbon_factor_negative(self, em):
        """update_tariff rejects negative carbon factor"""
        result = em.update_tariff(carbon_factor=-1.0)
        assert result['success'] is False


# ============================================================
# Power Data Tests
# ============================================================

class TestPowerData:

    def test_feed_power_data(self, em):
        """feed_power_data stores realtime power"""
        em.feed_power_data('dev1', power_kw=5.0)
        assert 'dev1' in em.realtime_power
        assert em.realtime_power['dev1']['power_kw'] == 5.0

    def test_feed_power_data_with_energy(self, em):
        """feed_power_data accumulates energy"""
        em.feed_power_data('dev1', power_kw=0, energy_kwh=10.0)
        assert em.energy_accumulated['dev1']['energy_kwh'] == 10.0

    def test_feed_power_data_accumulates(self, em):
        """Multiple calls accumulate energy"""
        em.feed_power_data('dev1', power_kw=0, energy_kwh=5.0)
        em.feed_power_data('dev1', power_kw=0, energy_kwh=3.0)
        assert em.energy_accumulated['dev1']['energy_kwh'] == 8.0

    def test_get_realtime_power(self, em):
        """get_realtime_power returns current power data"""
        em.feed_power_data('dev1', power_kw=5.0)
        power = em.get_realtime_power()
        assert 'dev1' in power

    def test_get_realtime_power_empty(self, em):
        """get_realtime_power returns empty dict initially"""
        assert em.get_realtime_power() == {}


# ============================================================
# Anomaly Config Tests
# ============================================================

class TestAnomalyConfig:

    def test_get_anomaly_config(self, em):
        """get_anomaly_config returns current config"""
        config = em.get_anomaly_config()
        assert 'threshold_multiplier' in config
        assert 'warning_multiplier' in config

    def test_update_anomaly_config_success(self, em):
        """update_anomaly_config updates threshold multiplier"""
        result = em.update_anomaly_config({'threshold_multiplier': 3.0})
        assert result['success'] is True
        assert em.anomaly_config['threshold_multiplier'] == 3.0

    def test_update_anomaly_config_invalid(self, em):
        """update_anomaly_config rejects non-positive multiplier"""
        result = em.update_anomaly_config({'threshold_multiplier': -1.0})
        assert result['success'] is False

    def test_update_anomaly_config_warning(self, em):
        """update_anomaly_config updates warning multiplier"""
        result = em.update_anomaly_config({'warning_multiplier': 2.0})
        assert result['success'] is True


# ============================================================
# Start/Stop Tests
# ============================================================

class TestStartStop:

    def test_start(self, em):
        """start() sets running flag"""
        em.start()
        assert em._running is True
        em.stop()

    def test_double_start_no_op(self, em):
        """Calling start() twice doesn't create duplicate threads"""
        em.start()
        t1 = em._thread
        em.start()
        assert em._thread is t1
        em.stop()

    def test_stop(self, em):
        """stop() clears running flag"""
        em.start()
        em.stop()
        assert em._running is False


# ============================================================
# Energy Summary Tests
# ============================================================

class TestEnergySummary:

    def test_get_energy_summary_empty(self, em):
        """get_energy_summary returns summary for empty state"""
        summary = em.get_energy_summary()
        assert isinstance(summary, dict)

    def test_get_energy_summary_with_data(self, em):
        """get_energy_summary includes accumulated data"""
        em.feed_power_data('dev1', power_kw=5.0, energy_kwh=100.0)
        summary = em.get_energy_summary()
        assert isinstance(summary, dict)


# ============================================================
# Carbon Emission Tests
# ============================================================

class TestCarbonEmission:

    def test_calculate_carbon_emission(self, em):
        """calculate_carbon_emission computes CO2 from energy"""
        # carbon_factor default is 0.581 kgCO2/kWh
        emission = em.calculate_carbon_emission(100.0)
        expected = 100.0 * em.carbon_factor
        assert abs(emission - expected) < 0.01

    def test_calculate_carbon_emission_zero(self, em):
        """Zero energy gives zero emission"""
        assert em.calculate_carbon_emission(0) == 0


# ============================================================
# Save/Load Config Tests
# ============================================================

class TestConfigPersistence:

    def test_save_config_creates_file(self, em):
        """_save_config creates YAML file"""
        em._save_config()
        assert em.config_path.exists()

    def test_save_and_reload_config(self, tmp_path):
        """Saved config can be reloaded"""
        cfg_path = tmp_path / 'energy.yaml'
        em1 = EnergyManager(database=MagicMock(), config_path=cfg_path)
        em1.tariff['peak'] = 5.0
        em1._save_config()

        em2 = EnergyManager(database=MagicMock(), config_path=cfg_path)
        assert em2.tariff['peak'] == 5.0
