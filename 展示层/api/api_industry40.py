"""
工业4.0智能层API
预测性维护/OEE/SPC/能源管理/边缘决策/总览

重写说明：
- 统一使用ServiceResponse标准响应格式
- 所有端点添加try/except错误处理
- 添加详细日志记录
- 安全访问模块实例（防空指针）
"""

import logging
import traceback
from datetime import datetime
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from core.service_response import ServiceResponse, success_response, error_response, module_unavailable_response

logger = logging.getLogger(__name__)

industry40_bp = Blueprint('api_industry40', __name__, url_prefix='/api')

_require_auth = jwt_required


# ==================== 辅助函数 ====================

def _get_module(attr_name: str):
    """
    安全获取Flask app上挂载的模块实例
    
    Args:
        attr_name: 模块属性名（如 'predictive_maintenance'）
        
    Returns:
        模块实例或None
    """
    try:
        module = getattr(current_app, attr_name, None)
        if module is None:
            logger.warning(f"模块 '{attr_name}' 未挂载到Flask app")
        return module
    except Exception as e:
        logger.error(f"获取模块 '{attr_name}' 失败: {e}")
        return None


def _module_check(attr_name: str, display_name: str = None):
    """
    检查模块是否可用，不可用则返回错误响应
    
    Args:
        attr_name: 模块属性名
        display_name: 显示名称（用于错误消息）
        
    Returns:
        (module, error_response) 元组。如果module为None，error_response为错误响应；否则error_response为None
    """
    display_name = display_name or attr_name
    module = _get_module(attr_name)
    if module is None:
        return None, module_unavailable_response(display_name)
    return module, None


# ==================== 预测性维护API ====================

@industry40_bp.route('/industry40/health', methods=['GET'])
@_require_auth
def get_health_scores():
    """获取所有设备健康评分"""
    try:
        pm, err = _module_check('predictive_maintenance', '预测性维护')
        if err:
            return err
        
        scores = pm.get_health_scores()
        logger.debug(f"获取健康评分: {len(scores) if scores else 0} 个设备")
        return success_response(scores if scores else {}, message="获取健康评分成功")
    except Exception as e:
        logger.error(f"获取健康评分失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取健康评分失败: {str(e)}", 500)


@industry40_bp.route('/industry40/health/<device_id>', methods=['GET'])
@_require_auth
def get_device_health_by_id(device_id):
    """获取指定设备健康评分"""
    try:
        pm, err = _module_check('predictive_maintenance', '预测性维护')
        if err:
            return err
        
        result = pm.get_device_health(device_id)
        if result is None:
            return error_response(f"设备 '{device_id}' 无健康数据", 404)
        
        logger.debug(f"获取设备 {device_id} 健康评分成功")
        return success_response(result, message=f"获取设备 {device_id} 健康评分成功")
    except Exception as e:
        logger.error(f"获取设备 {device_id} 健康评分失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取设备健康评分失败: {str(e)}", 500)


@industry40_bp.route('/industry40/maintenance-alerts', methods=['GET'])
@_require_auth
def get_maintenance_alerts():
    """获取维护建议列表"""
    try:
        pm, err = _module_check('predictive_maintenance', '预测性维护')
        if err:
            return err
        
        limit = request.args.get('limit', 50, type=int)
        alerts = pm.get_maintenance_alerts(limit)
        logger.debug(f"获取维护建议: {len(alerts) if alerts else 0} 条")
        return success_response(alerts if alerts else [], message="获取维护建议成功")
    except Exception as e:
        logger.error(f"获取维护建议失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取维护建议失败: {str(e)}", 500)


@industry40_bp.route('/industry40/trend/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_trend_data(device_id, register_name):
    """获取趋势分析数据"""
    try:
        pm, err = _module_check('predictive_maintenance', '预测性维护')
        if err:
            return err
        
        result = pm.get_trend_data(device_id, register_name)
        logger.debug(f"获取趋势数据: {device_id}/{register_name}")
        return success_response(result, message="获取趋势数据成功")
    except Exception as e:
        logger.error(f"获取趋势数据失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取趋势数据失败: {str(e)}", 500)


# ==================== OEE API ====================

@industry40_bp.route('/industry40/oee', methods=['GET'])
@_require_auth
def get_all_oee():
    """获取所有设备OEE"""
    try:
        oee, err = _module_check('oee_calculator', 'OEE计算')
        if err:
            return err
        
        result = oee.get_all_oee()
        logger.debug(f"获取所有OEE: {len(result) if result else 0} 个设备")
        return success_response(result if result else {}, message="获取所有设备OEE成功")
    except Exception as e:
        logger.error(f"获取所有OEE失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取所有OEE失败: {str(e)}", 500)


@industry40_bp.route('/industry40/oee/<device_id>', methods=['GET'])
@_require_auth
def get_device_oee(device_id):
    """获取指定设备OEE"""
    try:
        oee, err = _module_check('oee_calculator', 'OEE计算')
        if err:
            return err
        
        result = oee.get_device_oee(device_id)
        if result is None:
            return error_response(f"设备 '{device_id}' 无OEE数据", 404)
        
        logger.debug(f"获取设备 {device_id} OEE成功")
        return success_response(result, message=f"获取设备 {device_id} OEE成功")
    except Exception as e:
        logger.error(f"获取设备 {device_id} OEE失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取设备OEE失败: {str(e)}", 500)


# ==================== SPC API ====================

@industry40_bp.route('/industry40/spc/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_spc_chart(device_id, register_name):
    """获取SPC控制图数据"""
    try:
        spc, err = _module_check('spc_analyzer', 'SPC分析')
        if err:
            return err
        
        chart = spc.get_control_chart(device_id, register_name)
        capability = spc.get_capability(device_id, register_name)
        
        result = {
            'control_chart': chart,
            'capability': capability,
        }
        logger.debug(f"获取SPC数据: {device_id}/{register_name}")
        return success_response(result, message="获取SPC控制图数据成功")
    except Exception as e:
        logger.error(f"获取SPC数据失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取SPC数据失败: {str(e)}", 500)


@industry40_bp.route('/industry40/spc/violations', methods=['GET'])
@_require_auth
def get_spc_violations():
    """获取SPC判异结果"""
    try:
        spc, err = _module_check('spc_analyzer', 'SPC分析')
        if err:
            return err
        
        device_id = request.args.get('device_id')
        limit = request.args.get('limit', 50, type=int)
        violations = spc.get_violations(device_id, limit)
        logger.debug(f"获取SPC判异结果: {len(violations) if violations else 0} 条")
        return success_response(violations if violations else [], message="获取SPC判异结果成功")
    except Exception as e:
        logger.error(f"获取SPC判异结果失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取SPC判异结果失败: {str(e)}", 500)


# ==================== 能源管理API ====================

@industry40_bp.route('/industry40/energy', methods=['GET'])
@_require_auth
def get_energy_summary():
    """获取能耗汇总"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = em.get_energy_summary()
        logger.debug("获取能耗汇总成功")
        return success_response(result, message="获取能耗汇总成功")
    except Exception as e:
        logger.error(f"获取能耗汇总失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取能耗汇总失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/cost', methods=['GET'])
@_require_auth
def get_energy_cost():
    """获取电费分时明细"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = em.get_energy_cost_breakdown()
        logger.debug("获取电费分时明细成功")
        return success_response(result, message="获取电费分时明细成功")
    except Exception as e:
        logger.error(f"获取电费分时明细失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取电费分时明细失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/carbon', methods=['GET'])
@_require_auth
def get_carbon_emission():
    """获取碳排放数据"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = em.get_carbon_emission()
        logger.debug("获取碳排放数据成功")
        return success_response(result, message="获取碳排放数据成功")
    except Exception as e:
        logger.error(f"获取碳排放数据失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取碳排放数据失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/power', methods=['GET'])
@_require_auth
def get_realtime_power():
    """获取实时功率"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = {
            'total_power_kw': em.get_total_power(),
            'devices': em.get_realtime_power(),
        }
        logger.debug(f"获取实时功率: 总功率={result['total_power_kw']}kW")
        return success_response(result, message="获取实时功率成功")
    except Exception as e:
        logger.error(f"获取实时功率失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取实时功率失败: {str(e)}", 500)


# ==================== 电价配置API ====================

@industry40_bp.route('/industry40/energy/tariff', methods=['GET'])
@_require_auth
def get_energy_tariff():
    """获取当前电价配置（电价、时段、碳排放因子）"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = em.get_tariff_config()
        logger.debug("获取电价配置成功")
        return success_response(result, message="获取电价配置成功")
    except Exception as e:
        logger.error(f"获取电价配置失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取电价配置失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/tariff', methods=['PUT'])
@_require_auth
@role_required('admin', 'engineer')
def update_energy_tariff():
    """
    更新电价配置（实时生效 + 持久化到YAML）
    
    请求体示例:
    {
        "tariff": {"peak": 1.5, "flat": 0.8, "valley": 0.4},
        "tariff_periods": {"peak": [[8,11],[18,23]], "valley": [[0,7],[23,24]]},
        "carbon_factor": 0.6
    }
    所有字段可选，仅传需要修改的部分
    """
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        data = request.get_json()
        if not data:
            return error_response("请提供配置数据", 400)
        
        result = em.update_tariff(
            tariff=data.get('tariff'),
            tariff_periods=data.get('tariff_periods'),
            carbon_factor=data.get('carbon_factor'),
        )
        
        if result.get('success'):
            logger.info(f"电价配置已更新: {data}")
            return success_response(result.get('config'), message=result.get('message', '配置已更新'))
        else:
            return error_response(result.get('message', '更新失败'), 400)
    except Exception as e:
        logger.error(f"更新电价配置失败: {e}\n{traceback.format_exc()}")
        return error_response(f"更新电价配置失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/anomaly-config', methods=['GET'])
@_require_auth
def get_energy_anomaly_config():
    """获取能耗异常检测配置"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        result = em.get_anomaly_config()
        return success_response(result, message="获取异常检测配置成功")
    except Exception as e:
        logger.error(f"获取异常检测配置失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取异常检测配置失败: {str(e)}", 500)


@industry40_bp.route('/industry40/energy/anomaly-config', methods=['PUT'])
@_require_auth
@role_required('admin', 'engineer')
def update_energy_anomaly_config():
    """更新能耗异常检测配置"""
    try:
        em, err = _module_check('energy_manager', '能源管理')
        if err:
            return err
        
        data = request.get_json()
        if not data:
            return error_response("请提供配置数据", 400)
        
        result = em.update_anomaly_config(data)
        
        if result.get('success'):
            return success_response(result.get('config'), message=result.get('message'))
        else:
            return error_response(result.get('message', '更新失败'), 400)
    except Exception as e:
        logger.error(f"更新异常检测配置失败: {e}\n{traceback.format_exc()}")
        return error_response(f"更新异常检测配置失败: {str(e)}", 500)


# ==================== 边缘决策API ====================

@industry40_bp.route('/industry40/edge/status', methods=['GET'])
@_require_auth
def get_edge_status():
    """获取边缘决策引擎状态"""
    try:
        edge, err = _module_check('edge_decision', '边缘决策')
        if err:
            return err
        
        result = edge.get_status()
        logger.debug("获取边缘决策状态成功")
        return success_response(result, message="获取边缘决策状态成功")
    except Exception as e:
        logger.error(f"获取边缘决策状态失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取边缘决策状态失败: {str(e)}", 500)


@industry40_bp.route('/industry40/edge/rules', methods=['GET'])
@_require_auth
def get_edge_rules():
    """获取边缘决策规则"""
    try:
        edge, err = _module_check('edge_decision', '边缘决策')
        if err:
            return err
        
        result = edge.get_rules()
        logger.debug("获取边缘决策规则成功")
        return success_response(result, message="获取边缘决策规则成功")
    except Exception as e:
        logger.error(f"获取边缘决策规则失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取边缘决策规则失败: {str(e)}", 500)


@industry40_bp.route('/industry40/edge/log', methods=['GET'])
@_require_auth
def get_edge_log():
    """获取决策日志"""
    try:
        edge, err = _module_check('edge_decision', '边缘决策')
        if err:
            return err
        
        limit = request.args.get('limit', 50, type=int)
        result = edge.get_decision_log(limit)
        logger.debug(f"获取决策日志: {len(result) if result else 0} 条")
        return success_response(result if result else [], message="获取决策日志成功")
    except Exception as e:
        logger.error(f"获取决策日志失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取决策日志失败: {str(e)}", 500)


# ==================== 设备状态API ====================

@industry40_bp.route('/industry40/devices/status', methods=['GET'])
@_require_auth
def get_devices_status():
    """获取所有设备运行状态（用于工艺流程图）"""
    try:
        oee, err = _module_check('oee_calculator', 'OEE计算')
        if err:
            return err
        
        result = oee.get_all_device_states()
        logger.debug(f"获取设备状态: {len(result) if result else 0} 个设备")
        return success_response(result if result else {}, message="获取设备运行状态成功")
    except Exception as e:
        logger.error(f"获取设备状态失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取设备状态失败: {str(e)}", 500)


# ==================== 工业4.0总览API ====================

@industry40_bp.route('/industry40/overview', methods=['GET'])
@_require_auth
def get_industry40_overview():
    """工业4.0总览数据"""
    try:
        result = {
            'predictive_maintenance': {
                'status': 'unavailable',
                'device_count': 0,
                'avg_health_score': 0,
                'recent_alerts': [],
            },
            'oee': {
                'status': 'unavailable',
                'device_count': 0,
                'avg_oee_percent': 0,
                'devices': {},
            },
            'energy': {
                'status': 'unavailable',
                'total_energy_kwh': 0,
                'total_power_kw': 0,
                'electricity_cost': 0,
                'carbon_emission_kg': 0,
            },
            'edge_decision': {
                'status': 'unavailable',
                'running': False,
                'rules_count': 0,
                'interlocks_count': 0,
                'pid_controllers_count': 0,
            },
        }

        # 预测性维护
        pm = _get_module('predictive_maintenance')
        if pm:
            try:
                scores = pm.get_health_scores()
                if scores:
                    avg_health = sum(
                        s.get('health_score', 0) for s in scores.values()
                    ) / len(scores)
                    alerts = pm.get_maintenance_alerts(5)
                    result['predictive_maintenance'] = {
                        'status': 'available',
                        'device_count': len(scores),
                        'avg_health_score': round(avg_health, 1),
                        'recent_alerts': alerts if alerts else [],
                    }
                    logger.debug(f"总览-预测性维护: {len(scores)} 个设备, 平均健康分={avg_health:.1f}")
            except Exception as e:
                logger.error(f"总览-获取预测性维护数据失败: {e}")

        # OEE
        oee_calc = _get_module('oee_calculator')
        if oee_calc:
            try:
                all_oee = oee_calc.get_all_oee()
                if all_oee:
                    avg_oee = sum(
                        o.get('oee_percent', 0) for o in all_oee.values()
                    ) / len(all_oee)
                    result['oee'] = {
                        'status': 'available',
                        'device_count': len(all_oee),
                        'avg_oee_percent': round(avg_oee, 1),
                        'devices': all_oee,
                    }
                    logger.debug(f"总览-OEE: {len(all_oee)} 个设备, 平均OEE={avg_oee:.1f}%")
            except Exception as e:
                logger.error(f"总览-获取OEE数据失败: {e}")

        # 能源管理
        em = _get_module('energy_manager')
        if em:
            try:
                energy_summary = em.get_energy_summary()
                if energy_summary:
                    energy_summary['status'] = 'available'
                    energy_summary['total_power_kw'] = em.get_total_power()
                    result['energy'] = energy_summary
                    logger.debug(f"总览-能源: 总功率={energy_summary.get('total_power_kw', 0)}kW")
            except Exception as e:
                logger.error(f"总览-获取能源数据失败: {e}")

        # 边缘决策
        edge = _get_module('edge_decision')
        if edge:
            try:
                edge_status = edge.get_status()
                if edge_status:
                    edge_status['status'] = 'available'
                    result['edge_decision'] = edge_status
                    logger.debug(f"总览-边缘决策: 运行状态={edge_status.get('running', False)}")
            except Exception as e:
                logger.error(f"总览-获取边缘决策数据失败: {e}")

        logger.info("工业4.0总览数据获取完成")
        return success_response(result, message="获取工业4.0总览数据成功")
    except Exception as e:
        logger.error(f"获取工业4.0总览数据失败: {e}\n{traceback.format_exc()}")
        return error_response(f"获取工业4.0总览数据失败: {str(e)}", 500)
