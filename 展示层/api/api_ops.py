"""
运维工具API
提供运行时配置、数据库维护、系统诊断、数据清理等运维接口
"""

import logging
from flask import Blueprint, request

from core.ops_tools import (
    ops_audit, runtime_config_manager, db_maintainer,
    data_cleaner, diagnostic_exporter,
)
from core.service_response import success_response, error_response
from 用户层.auth import jwt_required

logger = logging.getLogger(__name__)

ops_bp = Blueprint('api_ops', __name__, url_prefix='/api/ops')


# ================================================================
# 运行时配置 API
# ================================================================

@ops_bp.route('/config', methods=['GET'])
@jwt_required
def get_runtime_config():
    """获取运行时配置"""
    try:
        return success_response(runtime_config_manager.get_all())
    except Exception as e:
        logger.error(f"获取运行时配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config', methods=['PUT'])
@jwt_required
def update_runtime_config():
    """更新运行时配置"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        operator = data.get('operator', 'api_user')
        updates = data.get('config', {})
        if not updates:
            return error_response("config 字段不能为空", 400)

        changed = runtime_config_manager.update_batch(updates, operator)

        # 记录审计
        ops_audit.log_operation(
            'config_update', operator=operator,
            details={'keys': list(updates.keys()), 'changed_count': changed},
        )

        return success_response({
            'changed_count': changed,
            'config': runtime_config_manager.get_all(),
        })
    except Exception as e:
        logger.error(f"更新运行时配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/<key>', methods=['GET'])
@jwt_required
def get_config_key(key):
    """获取单个配置值"""
    try:
        value = runtime_config_manager.get(key)
        if value is None:
            return error_response(f"配置项 {key} 不存在", 404)
        return success_response({'key': key, 'value': value})
    except Exception as e:
        logger.error(f"获取配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/<key>', methods=['PUT'])
@jwt_required
def set_config_key(key):
    """设置单个配置值"""
    try:
        data = request.get_json()
        if not data or 'value' not in data:
            return error_response("value 字段必填", 400)

        operator = data.get('operator', 'api_user')
        value = data['value']
        changed = runtime_config_manager.set(key, value, operator)

        ops_audit.log_operation(
            'config_set', operator=operator, target=key,
            details={'value': value, 'changed': changed},
        )

        return success_response({'key': key, 'value': value, 'changed': changed})
    except Exception as e:
        logger.error(f"设置配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/history', methods=['GET'])
@jwt_required
def get_config_history():
    """获取配置变更历史"""
    try:
        limit = request.args.get('limit', 50, type=int)
        return success_response(runtime_config_manager.get_history(limit))
    except Exception as e:
        logger.error(f"获取配置历史失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 数据库维护 API
# ================================================================

@ops_bp.route('/db/vacuum', methods=['POST'])
@jwt_required
def db_vacuum():
    """执行 VACUUM"""
    try:
        result = db_maintainer.vacuum()
        ops_audit.log_operation('db_vacuum', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"VACUUM 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/reindex', methods=['POST'])
@jwt_required
def db_reindex():
    """执行 REINDEX"""
    try:
        data = request.get_json() or {}
        table = data.get('table')
        result = db_maintainer.reindex(table)
        ops_audit.log_operation('db_reindex', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"REINDEX 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/analyze', methods=['POST'])
@jwt_required
def db_analyze():
    """执行 ANALYZE"""
    try:
        result = db_maintainer.analyze()
        ops_audit.log_operation('db_analyze', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"ANALYZE 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/integrity', methods=['GET'])
@jwt_required
def db_integrity_check():
    """数据库完整性检查"""
    try:
        result = db_maintainer.integrity_check()
        return success_response(result)
    except Exception as e:
        logger.error(f"完整性检查失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/tables', methods=['GET'])
@jwt_required
def db_table_stats():
    """获取数据库表统计"""
    try:
        result = db_maintainer.get_table_stats()
        return success_response(result)
    except Exception as e:
        logger.error(f"获取表统计失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 数据清理 API
# ================================================================

@ops_bp.route('/cleanup/history', methods=['POST'])
@jwt_required
def cleanup_history():
    """清理过期历史数据"""
    try:
        data = request.get_json() or {}
        days = data.get('retention_days', 90)
        result = data_cleaner.clean_history_data(days)
        ops_audit.log_operation('cleanup_history', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"清理历史数据失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/cleanup/backups', methods=['POST'])
@jwt_required
def cleanup_backups():
    """清理旧备份"""
    try:
        data = request.get_json() or {}
        keep = data.get('keep_count', 5)
        result = data_cleaner.clean_old_backups(keep_count=keep)
        ops_audit.log_operation('cleanup_backups', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"清理备份失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/cleanup/logs', methods=['POST'])
@jwt_required
def cleanup_logs():
    """清理过期日志"""
    try:
        data = request.get_json() or {}
        days = data.get('retention_days', 30)
        result = data_cleaner.clean_log_files(retention_days=days)
        ops_audit.log_operation('cleanup_logs', details=result)
        return success_response(result)
    except Exception as e:
        logger.error(f"清理日志失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 系统诊断 API
# ================================================================

@ops_bp.route('/diagnostics/export', methods=['POST'])
@jwt_required
def export_diagnostics():
    """一键导出系统诊断信息"""
    try:
        data = request.get_json() or {}
        result = diagnostic_exporter.export_diagnostics(
            include_logs=data.get('include_logs', True),
            include_config=data.get('include_config', True),
            include_db_stats=data.get('include_db_stats', True),
            include_system_state=data.get('include_system_state', True),
        )
        ops_audit.log_operation('export_diagnostics', details={'status': result.get('status')})
        return success_response(result)
    except Exception as e:
        logger.error(f"导出诊断信息失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 审计日志 API
# ================================================================

@ops_bp.route('/audit', methods=['GET'])
@jwt_required
def get_ops_audit():
    """获取运维操作审计记录"""
    try:
        limit = request.args.get('limit', 50, type=int)
        return success_response(ops_audit.get_recent(limit))
    except Exception as e:
        logger.error(f"获取审计记录失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
