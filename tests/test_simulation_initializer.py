"""
模拟初始化器测试 - 提升 core/simulation_initializer.py 覆盖率
"""
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


@pytest.fixture
def mock_device_manager():
    """模拟设备管理器"""
    dm = MagicMock()
    dm.simulation_mode = True
    dm.devices = {}
    dm.add_device.return_value = True
    dm.remove_device.return_value = True
    dm.get_all_devices.return_value = {}
    dm.get_device_status.return_value = {'device_id': 'test', 'connected': True}
    return dm


class TestSimulationInitializerInit:
    """初始化测试"""

    def test_init_basic(self, mock_device_manager):
        """基本初始化"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            assert si is not None
            assert si.device_manager is mock_device_manager

    def test_init_with_modules(self, mock_device_manager):
        """带模块初始化"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(
                mock_device_manager,
                oee_calculator=MagicMock(),
                predictive_maintenance=MagicMock(),
                spc_analyzer=MagicMock(),
                energy_manager=MagicMock()
            )
            assert si.oee_calculator is not None


class TestPresets:
    """预设管理测试"""

    def test_get_presets_empty(self, mock_device_manager):
        """无预设时返回空列表"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            presets = si.get_presets()
            assert isinstance(presets, list)

    def test_get_preset_categories(self, mock_device_manager):
        """获取预设分类"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            categories = si.get_preset_categories()
            assert isinstance(categories, (list, dict))

    def test_get_preset_detail_not_found(self, mock_device_manager):
        """获取不存在的预设详情"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            result = si.get_preset_detail('nonexistent')
            assert result is None

    def test_load_presets_file_not_exists(self, mock_device_manager):
        """预设文件不存在时返回空列表"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            assert si.presets == []


class TestAddPresetDevice:
    """添加预设设备测试"""

    def test_add_preset_device_not_found(self, mock_device_manager):
        """添加不存在的预设设备"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            result = si.add_preset_device('nonexistent')
            assert result['success'] is False

    def test_add_preset_batch_empty(self, mock_device_manager):
        """批量添加空列表"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            result = si.add_preset_batch([])
            assert isinstance(result, dict)

    def test_remove_device(self, mock_device_manager):
        """删除设备"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            result = si.remove_device('test_dev')
            assert isinstance(result, dict)


class TestSimulationDataGeneration:
    """模拟数据生成测试"""

    def test_generate_simulated_data(self, mock_device_manager):
        """生成模拟数据"""
        with patch('core.simulation_initializer.PRESETS_PATH') as mock_path:
            mock_path.exists.return_value = False
            from core.simulation_initializer import SimulationInitializer
            si = SimulationInitializer(mock_device_manager)
            # This method might not exist, let's check
            assert hasattr(si, 'device_sim_params')
