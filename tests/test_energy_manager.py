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
        """首次累积读数只建立基线，不累加。

        `energy_kwh` 是电表的**累积读数**（单调递增），不是本次增量。
        首次读数无法得知此前用了多少电，因此只能建基线。
        （原测试断言「读数 10.0 → 累积 10.0」，把累积量当增量，
          会让能耗/电费/碳排线性虚增 —— 这是 2026-09 审计修掉的缺陷。）
        """
        em.feed_power_data('dev1', power_kw=0, energy_kwh=10.0)
        assert em.energy_accumulated['dev1']['energy_kwh'] == 0.0, \
            '首次累积读数应只建立基线，不累加'

    def test_feed_power_data_accumulates(self, em):
        """连续读数应换算成增量累加（相邻两次读数做差）"""
        em.feed_power_data('dev1', power_kw=0, energy_kwh=5.0)    # 建基线
        em.feed_power_data('dev1', power_kw=0, energy_kwh=8.0)    # 增量 3
        assert em.energy_accumulated['dev1']['energy_kwh'] == 3.0, \
            '应累加增量 3.0，而不是把两次读数相加'

    def test_feed_power_data_reading_rollback_is_safe(self, em):
        """读数回退（换表 / 计量回绕）只重置基线，不产生负增量、也不虚增"""
        em.feed_power_data('dev1', power_kw=0, energy_kwh=100.0)
        em.feed_power_data('dev1', power_kw=0, energy_kwh=5.0)    # 回退
        assert em.energy_accumulated['dev1']['energy_kwh'] == 0.0, \
            '读数回退不应产生负增量，也不该把 5.0 当增量加上去'
        # 回退后基线已重置为 5.0，后续正常递增应继续累加
        em.feed_power_data('dev1', power_kw=0, energy_kwh=9.0)
        assert em.energy_accumulated['dev1']['energy_kwh'] == 4.0

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


# ============================================================
# 费率单一来源（2026-09 修复：两套默认费率 → 同一能耗两种费用）
# ============================================================

class TestRateSingleSource:
    """EnergyManager 与 EnergyOptimizer/EnergyAnalyzer 必须共用同一套费率与碳因子。"""

    def test_default_rates_match_energy_manager(self):
        """EnergyAnalyzer 默认电价/碳因子必须等于 DEFAULT_CONFIG（不再自带第二套）"""
        from 智能层.energy_optimizer import EnergyAnalyzer
        analyzer = EnergyAnalyzer()
        assert analyzer.tariff == DEFAULT_CONFIG['tariff']
        assert analyzer.carbon_factor == DEFAULT_CONFIG['carbon_factor']

    def test_peak_valley_hours_derived_from_tariff_periods(self):
        """峰/谷时段由 tariff_periods 推导，避免出现两套时段口径"""
        from 智能层.energy_optimizer import EnergyAnalyzer
        analyzer = EnergyAnalyzer()

        expected_peak = set()
        for start, end in DEFAULT_CONFIG['tariff_periods']['peak']:
            expected_peak.update(range(start, end))
        expected_valley = set()
        for start, end in DEFAULT_CONFIG['tariff_periods']['valley']:
            expected_valley.update(range(start, end))

        assert set(analyzer.peak_hours) == expected_peak
        assert set(analyzer.valley_hours) == expected_valley
        assert set(analyzer.peak_hours).isdisjoint(set(analyzer.valley_hours))

    def test_explicit_override_still_honored(self):
        """显式配置仍可覆盖（只是默认值不再各写一套）"""
        from 智能层.energy_optimizer import EnergyAnalyzer
        analyzer = EnergyAnalyzer({'peak_price': 3.0, 'carbon_factor': 0.9})
        assert analyzer.tariff['peak'] == 3.0
        assert analyzer.tariff['flat'] == DEFAULT_CONFIG['tariff']['flat']
        assert analyzer.carbon_factor == 0.9

    def test_same_energy_same_cost_across_modules(self, tmp_path):
        """同一份能耗（1kWh @ 每个小时）在两个模块必须算出相同电费与碳排"""
        from 智能层.energy_optimizer import EnergyAnalyzer

        for hour in range(24):
            ts = datetime(2026, 3, 1, hour, 0, 0)

            em = EnergyManager(database=MagicMock(), config_path=tmp_path / 'energy.yaml')
            em.feed_power_data('dev1', power_kw=0, energy_kwh=0.0, timestamp=ts)  # 建基线
            em.feed_power_data('dev1', power_kw=0, energy_kwh=1.0, timestamp=ts)  # 增量 1kWh
            summary = em.get_energy_summary()
            assert summary['total_energy_kwh'] == 1.0

            analyzer = EnergyAnalyzer()
            analyzer.add_record('dev1', ts.timestamp(), 1.0)
            consumption = analyzer.get_device_consumption('dev1')

            assert summary['electricity_cost'] == consumption['total_cost_yuan'], \
                f'{hour}时电价口径不一致: EnergyManager={summary["electricity_cost"]}, ' \
                f'EnergyAnalyzer={consumption["total_cost_yuan"]}'
            assert abs(summary['carbon_emission_kg'] - consumption['carbon_kg']) < 0.01

    def test_hour_bucket_classification_matches(self, tmp_path):
        """小时 → 峰/平/谷 的归类必须在两个模块中一致"""
        from 智能层.energy_optimizer import EnergyAnalyzer

        for hour in range(24):
            ts = datetime(2026, 3, 1, hour, 0, 0)

            em = EnergyManager(database=MagicMock(), config_path=tmp_path / 'energy.yaml')
            em.feed_power_data('dev1', power_kw=0, energy_kwh=0.0, timestamp=ts)
            em.feed_power_data('dev1', power_kw=0, energy_kwh=1.0, timestamp=ts)
            summary = em.get_energy_summary()
            em_bucket = max(
                ('peak', 'flat', 'valley'),
                key=lambda b: summary[f'{b}_kwh'],
            )

            analyzer = EnergyAnalyzer()
            analyzer.add_record('dev1', ts.timestamp(), 1.0)
            c = analyzer.get_device_consumption('dev1')
            an_bucket = max(
                (('peak', c['peak_energy_kwh']), ('flat', c['flat_energy_kwh']),
                 ('valley', c['valley_energy_kwh'])),
                key=lambda kv: kv[1],
            )[0]

            assert em_bucket == an_bucket, f'{hour}时的峰谷平归类不一致: {em_bucket} vs {an_bucket}'


# ============================================================
# dt<=0 边界保护（2026-09 修复：水/气路径缺保护 → 可能负累加）
# ============================================================

class TestNonPositiveDtGuard:

    def test_water_backwards_timestamp_does_not_subtract(self, em):
        """水表时间戳回拨时不得负累加（把已统计用量抹掉）"""
        from datetime import timedelta
        t0 = datetime(2026, 3, 1, 10, 0, 0)

        em.feed_water_data('dev1', 10.0, timestamp=t0)                    # 建基线
        em.feed_water_data('dev1', 10.0, timestamp=t0 + timedelta(hours=1))
        assert em.energy_accumulated['dev1']['water_m3'] == 10.0

        em.feed_water_data('dev1', 10.0, timestamp=t0)                    # 时间回拨
        assert em.energy_accumulated['dev1']['water_m3'] == 10.0, '时间回拨导致负累加'

    def test_water_same_timestamp_does_not_accumulate(self, em):
        """Δt=0 不应累加任何用量"""
        t0 = datetime(2026, 3, 1, 10, 0, 0)
        em.feed_water_data('dev1', 10.0, timestamp=t0)
        em.feed_water_data('dev1', 10.0, timestamp=t0)
        assert em.energy_accumulated['dev1']['water_m3'] == 0.0

    def test_gas_backwards_timestamp_does_not_subtract(self, em):
        """气表时间戳回拨时不得负累加"""
        from datetime import timedelta
        t0 = datetime(2026, 3, 1, 10, 0, 0)

        em.feed_gas_data('dev1', 20.0, timestamp=t0)
        em.feed_gas_data('dev1', 20.0, timestamp=t0 + timedelta(hours=1))
        assert em.energy_accumulated['dev1']['gas_m3'] == 20.0

        em.feed_gas_data('dev1', 20.0, timestamp=t0)
        assert em.energy_accumulated['dev1']['gas_m3'] == 20.0, '时间回拨导致负累加'

    def test_power_backwards_timestamp_does_not_subtract(self, em):
        """电力路径同样不得因时间回拨产生负增量（既有一致性）"""
        from datetime import timedelta
        t0 = datetime(2026, 3, 1, 10, 0, 0)
        em.feed_power_data('dev1', power_kw=10.0, timestamp=t0)
        em.feed_power_data('dev1', power_kw=10.0, timestamp=t0 + timedelta(hours=1))
        before = em.energy_accumulated['dev1']['energy_kwh']
        assert before > 0

        em.feed_power_data('dev1', power_kw=10.0, timestamp=t0)
        assert em.energy_accumulated['dev1']['energy_kwh'] == before
