#!/usr/bin/env python3
"""
测试API响应格式
"""

import requests
import json

# 登录获取token
login_url = "http://localhost:5000/api/auth/login"
login_data = {"username": "admin", "password": "admin123"}

try:
    login_resp = requests.post(login_url, json=login_data)
    login_result = login_resp.json()
    
    if not login_result.get('success'):
        print(f"登录失败: {login_result}")
        exit(1)
    
    token = login_result['token']
    print(f"登录成功，获取token: {token[:20]}...")
    
    # 测试API
    api_url = "http://localhost:5000/api/industry40/overview"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    api_resp = requests.get(api_url, headers=headers)
    api_data = api_resp.json()
    
    print("\n=== API响应数据 ===")
    print(f"predictive_maintenance.status: {api_data['predictive_maintenance']['status']}")
    print(f"predictive_maintenance.avg_health_score: {api_data['predictive_maintenance']['avg_health_score']}")
    print(f"oee.status: {api_data['oee']['status']}")
    print(f"oee.avg_oee_percent: {api_data['oee']['avg_oee_percent']}")
    print(f"energy.status: {api_data['energy']['status']}")
    print(f"energy.total_power_kw: {api_data['energy']['total_power_kw']}")
    print(f"energy.carbon_emission_kg: {api_data['energy']['carbon_emission_kg']}")
    
    # 检查数据格式
    print("\n=== 数据格式检查 ===")
    print(f"predictive_maintenance.avg_health_score 类型: {type(api_data['predictive_maintenance']['avg_health_score'])}")
    print(f"oee.avg_oee_percent 类型: {type(api_data['oee']['avg_oee_percent'])}")
    print(f"energy.total_power_kw 类型: {type(api_data['energy']['total_power_kw'])}")
    print(f"energy.carbon_emission_kg 类型: {type(api_data['energy']['carbon_emission_kg'])}")
    
    # 检查前端期望的格式
    print("\n=== 前端期望格式检查 ===")
    health_score = api_data['predictive_maintenance']['avg_health_score']
    oee_percent = api_data['oee']['avg_oee_percent']
    power_kw = api_data['energy']['total_power_kw']
    carbon_kg = api_data['energy']['carbon_emission_kg']
    
    print(f"健康评分 > 0: {health_score > 0}")
    print(f"OEE > 0: {oee_percent > 0}")
    print(f"功率 > 0: {power_kw > 0}")
    print(f"碳排放 > 0: {carbon_kg > 0}")
    
    # 模拟前端显示逻辑
    print("\n=== 前端显示逻辑模拟 ===")
    print(f"健康评分显示: {health_score if health_score > 0 else '--'}")
    print(f"OEE显示: {oee_percent if oee_percent > 0 else '--'}%")
    print(f"功率显示: {power_kw if power_kw > 0 else '--'}")
    print(f"碳排放显示: {carbon_kg if carbon_kg > 0 else '--'}")
    
except Exception as e:
    print(f"测试失败: {e}")
    import traceback
    traceback.print_exc()
