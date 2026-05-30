"""OpenAPI/Swagger API文档自动生成"""
from flask_restx import Api, Namespace, fields, Resource
from flask import Blueprint

# 创建API文档蓝图
swagger_bp = Blueprint('swagger', __name__)

# 创建API实例
api = Api(
    swagger_bp,
    version='3.0.0',
    title='SCADA系统API',
    description='工业级SCADA监控与数据采集系统API文档 - 符合GB/T 36416-2018',
    doc='/docs',  # Swagger UI路径
    prefix='/api/v1'
)

# 定义命名空间
auth_ns = Namespace('auth', description='认证与授权')
device_ns = Namespace('devices', description='设备管理')
alarm_ns = Namespace('alarms', description='告警管理')
data_ns = Namespace('data', description='数据采集')
system_ns = Namespace('system', description='系统管理')
control_ns = Namespace('control', description='设备控制')
industry_ns = Namespace('industry40', description='工业4.0')
metrics_ns = Namespace('metrics', description='Prometheus指标')

api.add_namespace(auth_ns, path='/api/auth')
api.add_namespace(device_ns, path='/api/devices')
api.add_namespace(alarm_ns, path='/api/alarms')
api.add_namespace(data_ns, path='/api/data')
api.add_namespace(system_ns, path='/api/system')
api.add_namespace(control_ns, path='/api/control')
api.add_namespace(industry_ns, path='/api/industry40')
api.add_namespace(metrics_ns, path='/api')

# 定义数据模型
login_model = api.model('Login', {
    'username': fields.String(required=True, description='用户名'),
    'password': fields.String(required=True, description='密码')
})

token_model = api.model('Token', {
    'token': fields.String(description='JWT Token'),
    'user': fields.Raw(description='用户信息')
})

device_model = api.model('Device', {
    'id': fields.String(description='设备ID'),
    'name': fields.String(description='设备名称'),
    'protocol': fields.String(description='协议类型', enum=['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 's7', 'iec104']),
    'host': fields.String(description='设备地址'),
    'port': fields.Integer(description='端口号'),
    'status': fields.String(description='状态'),
    'connected': fields.Boolean(description='是否连接')
})

alarm_model = api.model('Alarm', {
    'id': fields.String(description='告警ID'),
    'device_id': fields.String(description='设备ID'),
    'level': fields.String(description='告警级别', enum=['critical', 'high', 'medium', 'low', 'info']),
    'message': fields.String(description='告警信息'),
    'timestamp': fields.Float(description='时间戳'),
    'acknowledged': fields.Boolean(description='是否确认')
})

health_model = api.model('Health', {
    'status': fields.String(description='健康状态'),
    'modules': fields.Raw(description='模块状态'),
    'uptime': fields.Float(description='运行时间')
})

bypass_request_model = api.model('BypassRequest', {
    'interlock_id': fields.String(required=True, description='联锁ID'),
    'reason': fields.String(required=True, description='旁路原因'),
    'timeout_minutes': fields.Integer(description='超时时间(分钟)', default=30)
})

# 示例端点文档
@auth_ns.route('/login')
class LoginResource(Resource):
    @auth_ns.expect(login_model)
    @auth_ns.response(200, '登录成功', token_model)
    @auth_ns.response(401, '认证失败')
    def post(self):
        """用户登录"""
        pass

@device_ns.route('/')
class DeviceListResource(Resource):
    @auth_ns.doc(security='jwt')
    @device_ns.marshal_list_with(device_model)
    def get(self):
        """获取设备列表"""
        pass

@alarm_ns.route('/')
class AlarmListResource(Resource):
    @auth_ns.doc(security='jwt')
    @alarm_ns.marshal_list_with(alarm_model)
    @alarm_ns.param('level', '告警级别过滤')
    @alarm_ns.param('device_id', '设备ID过滤')
    def get(self):
        """获取告警列表"""
        pass

@system_ns.route('/health')
class HealthResource(Resource):
    @system_ns.marshal_with(health_model)
    def get(self):
        """系统健康检查"""
        pass
