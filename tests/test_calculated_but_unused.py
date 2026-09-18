# -*- coding: utf-8 -*-
"""三处「算了却不用」的修复回归（round 168i）。

共同病征：**变量算出来了，但从未被使用** —— pyflakes 的
`local variable ... is assigned to but never used` 能直接扫出来。
这类代码通常意味着「原本打算用它，写到一半漏了」，而漏掉的往往是
控制限、损失值、校验值这类**直接影响结论正确性**的东西。

1. `智能层/spc_analyzer.py` —— `d3_table` 定义了却不用，R 图下限硬编码为 0
2. `智能层/oee_calculator.py` —— `availability_loss` 算出来却没放进返回值
3. `gateway/dnp3_gateway.py` —— `crc` 解析出来却既不校验也不返回
"""

from unittest.mock import MagicMock

import pytest


# ============================================================ SPC R 图下限

class TestRChartLowerLimit:

    def _chart(self, subgroup_size: int, values_per_group: list[float]):
        from 智能层.spc_analyzer import SPCAnalyzer

        spc = SPCAnalyzer(MagicMock(), config={'subgroup_size': subgroup_size})
        for v in values_per_group:
            spc.feed_data('dev1', 'temp', float(v))
        return spc.calculate_xbar_r_chart('dev1', 'temp')

    def test_large_subgroup_gets_positive_lower_limit(self):
        """**核心回归**：n=7 时 d3=0.076>0，R 图下限必须是正数。

        修复前 d3_table 从未被使用、`r_lcl = 0` 硬编码 —— 于是「极差过小」
        这类异常（数据被抹平、测量分辨率不足、采样被平滑）永远检不出来。
        """
        # 两组，每组 7 个点，组内极差 = 6
        group = [1, 2, 3, 4, 5, 6, 7]
        chart = self._chart(7, group + group)

        assert chart is not None, '数据足够却没算出控制图'
        lcl = chart['r_chart']['lcl']
        assert lcl > 0, f'n=7 时 R 图下限应为 d3*r_bar>0，实际 {lcl}（硬编码 0 的老毛病）'
        # d3(7) = 0.076, r_bar = 6
        assert abs(lcl - 0.076 * 6) < 0.01, f'下限数值不对: {lcl}'

    def test_small_subgroup_keeps_zero_lower_limit(self):
        """n<=6 时 d3=0，下限保持 0（标准做法，别改坏）。"""
        group = [1, 2, 3, 4, 5]      # 极差 4
        chart = self._chart(5, group + group)

        assert chart is not None
        assert chart['r_chart']['lcl'] == 0.0, \
            f'n=5 时 d3=0，下限应为 0，实际 {chart["r_chart"]["lcl"]}'

    def test_upper_limit_still_uses_d4(self):
        """上限逻辑不能被这次改动影响。"""
        group = [1, 2, 3, 4, 5, 6, 7]
        chart = self._chart(7, group + group)
        ucl = chart['r_chart']['ucl']
        # d4(7) = 1.924, r_bar = 6
        assert abs(ucl - 1.924 * 6) < 0.01, f'上限数值不对: {ucl}'


# ============================================================ OEE 可用率损失

class TestOEELosses:

    def test_availability_loss_is_returned(self):
        """**核心回归**：按可用率推算的损失必须出现在返回值里。

        修复前这个值算出来就被丢掉，调用方只能看到「实际停机损失」，
        两者对不上时无从发现（计划时间算错、实际运行时间被高估）。
        """
        from 智能层.oee_calculator import OEECalculator

        calc = OEECalculator(MagicMock())
        sd = {
            'planned_production_time': 1000.0,
            'actual_run_time': 800.0,
            'downtime': 200.0,
            'total_count': 100,
            'good_count': 95,
            'ideal_cycle_time': 5.0,
        }
        losses = calc._calculate_losses('dev1', sd, 0.8, 0.9, 0.95)

        assert '可用率损失_秒' in losses, \
            f'返回值缺少「可用率损失_秒」，实际键: {sorted(losses)}'
        # (1 - 0.8) * 1000 = 200
        assert abs(losses['可用率损失_秒'] - 200.0) < 0.1, losses['可用率损失_秒']

    def test_availability_loss_matches_percentage(self):
        """秒数与百分比必须自洽（都来自同一个 availability）。"""
        from 智能层.oee_calculator import OEECalculator

        calc = OEECalculator(MagicMock())
        sd = {
            'planned_production_time': 500.0,
            'actual_run_time': 300.0,
            'downtime': 200.0,
            'total_count': 50,
            'good_count': 50,
            'ideal_cycle_time': 1.0,
        }
        losses = calc._calculate_losses('dev1', sd, 0.6, 1.0, 1.0)

        # availability=0.6 → 秒数 (1-0.6)*500 = 200，百分比 40.0
        assert abs(losses['可用率损失_秒'] - 200.0) < 0.1
        assert abs(losses['可用率损失占比_百分比'] - 40.0) < 0.1


# ============================================================ DNP3 帧头 CRC

class TestDNP3FrameCrc:

    def test_parse_frame_exposes_crc(self):
        """**核心回归**：帧头 CRC 必须透出来，且明确标注未校验。

        修复前 `crc` 解析完就丢了 —— 等于帧头校验形同虚设：
        线路噪声造成的位翻转不会被发现，损坏的帧带着 valid=True 一路往上走。
        """
        from gateway.dnp3_gateway import DNP3Parser

        frame = DNP3Parser.build_read_request(source=1, destination=2, object_group=1)
        result = DNP3Parser.parse_frame(frame)

        assert result['valid'] is True, result
        assert 'crc' in result, f'返回值没有 crc 字段，实际键: {sorted(result)}'
        assert isinstance(result['crc'], int)
        assert result['crc_verified'] is False, \
            '必须明确标注 CRC 未校验 —— 不能让调用方以为已经验过了'

    def test_crc_value_matches_frame_bytes(self):
        """透出的 crc 必须真的来自帧的第 8~10 字节。"""
        import struct

        from gateway.dnp3_gateway import DNP3Parser

        frame = DNP3Parser.build_read_request(source=7, destination=9, object_group=1)
        result = DNP3Parser.parse_frame(frame)

        assert result['crc'] == struct.unpack('<H', frame[8:10])[0]

    def test_short_frame_still_rejected(self):
        """帧太短 / 起始字节错，仍要拒绝（原有行为别破坏）。"""
        from gateway.dnp3_gateway import DNP3Parser

        assert DNP3Parser.parse_frame(b'\x01\x02\x03')['valid'] is False
        bad = b'\x00\x00' + b'\x00' * 20
        assert DNP3Parser.parse_frame(bad)['valid'] is False
