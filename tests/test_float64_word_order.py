# -*- coding: utf-8 -*-
"""float64 解码：用「字序 + 字内字节序」两个正交维度，替代有歧义的 ABCD/BADC。

同一个缩写 `BADC` 在 32 位指「字内字节交换」，在本项目的 64 位约定里指「字交换」，
现场按 32 位的理解配 64 位就会读出乱码。这里用 1.0 的 IEEE-754 位型
(0x3FF0000000000000) 作为唯一判据，把所有排列钉死。
"""

import pytest

from 采集层.base_client import decode_float64, decode_float64_registers

ONE = [0x3FF0, 0x0000, 0x0000, 0x0000]          # 1.0 的四个 16 位寄存器


class TestWordOrder:

    def test_big(self):
        assert decode_float64(ONE, word_order='big') == 1.0

    def test_little(self):
        """三菱低字在前：整个 4 字序列反转。"""
        assert decode_float64([0, 0, 0, 0x3FF0], word_order='little') == 1.0

    def test_pair_swap(self):
        """相邻字两两互换（旧约定里的 64 位 BADC）。"""
        assert decode_float64([0, 0x3FF0, 0, 0], word_order='pair_swap') == 1.0

    def test_dword_swap(self):
        """前后双字互换（旧约定里的 64 位 CDAB）。"""
        assert decode_float64([0, 0, 0x3FF0, 0], word_order='dword_swap') == 1.0

    def test_byte_swap_in_word(self):
        """真 BADC：每个 16 位字内部字节交换。0x3FF0 → 0x803F? 不，是 0xF03F。"""
        assert decode_float64([0xF03F, 0, 0, 0], byte_swap_in_word=True) == 1.0

    def test_byte_swap_combines_with_word_order(self):
        """两个维度可叠加：字序 pair_swap + 字内字节交换。"""
        regs = [0x0000, 0xF03F, 0x0000, 0x0000]
        assert decode_float64(regs, word_order='pair_swap',
                              byte_swap_in_word=True) == 1.0


class TestCompatibilityWithLegacy:

    def test_legacy_badc_equals_pair_swap(self):
        """旧字符串 BADC 的解释必须和 pair_swap 完全一致，不能悄悄改语义。"""
        for regs in ([0x1234, 0x5678, 0x9ABC, 0xDEF0],
                     [0x3FF0, 0, 0, 0],
                     [0xFFFF, 0x0001, 0x8000, 0x7FFF]):
            assert decode_float64_registers(regs, 'BADC') == \
                decode_float64(regs, word_order='pair_swap')

    def test_legacy_cdab_equals_dword_swap(self):
        for regs in ([0x1234, 0x5678, 0x9ABC, 0xDEF0],
                     [0x3FF0, 0, 0, 0]):
            assert decode_float64_registers(regs, 'CDAB') == \
                decode_float64(regs, word_order='dword_swap')

    def test_legacy_abcd_equals_big(self):
        for regs in ([0x1234, 0x5678, 0x9ABC, 0xDEF0], ONE):
            assert decode_float64_registers(regs, 'ABCD') == \
                decode_float64(regs, word_order='big')


class TestBadInput:

    def test_unknown_word_order_rejected(self):
        with pytest.raises(ValueError, match='word_order'):
            decode_float64(ONE, word_order='BADC')   # 旧缩写不再被接受

    def test_too_few_registers_rejected(self):
        with pytest.raises(ValueError):
            decode_float64([0x3FF0, 0], word_order='big')
