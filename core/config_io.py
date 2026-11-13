# -*- coding: utf-8 -*-
"""配置写盘的**唯一入口** —— 原子写 + 就地改键（保留注释）。

为什么需要这个模块（round 256 实测）
------------------------------------
仓库里有 4 处「读 YAML → 改一段 → 写回」，写法都是：

    with open(config_file, 'w', encoding='utf-8') as f:     # ← 先截断
        yaml.dump(config, f, ...)                          # ← 再序列化

`open(..., 'w')` **先清空文件**，然后才轮到 `yaml.dump`。于是只要序列化抛
异常（值不可序列化、磁盘满、编码错误），配置文件就变成 **0 字节**，而
外层 `except Exception` 只记一条日志 —— 系统继续跑，规则全没了。这是
「报了个错、丢了个库」的典型形态：`配置/system.yaml` 被清空等于丢了端口
与运行模式，`配置/alarms.yaml` 被清空等于所有报警规则消失。

第二宗罪是 `yaml.dump` 整份重写：**注释全被抹掉、键被按字母重排**。
仓库里 `alarms.yaml` 的注释块（sms 未实现、sound 无消费方、升级策略语义）
就是这样反复被抹平又补回来的 —— 同一段说明被写了两遍以上。

本模块提供：
* :func:`atomic_write_text` —— 同目录唯一临时文件 + fsync + `os.replace`，
  失败时原文件分毫不动；
* :func:`dump_yaml` —— 只重写**值真的变了**的键，其余行（含注释、缩进、
  键顺序）逐字保留；合并结果会重新解析比对，对不上就退回整份 dump
  （仍然原子、且 `sort_keys=False`，不再打乱键序）。

行尾风格（CRLF/LF）按原文件保持一致，避免每次写盘产生整文件 diff。
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

__all__ = ['atomic_write_text', 'dump_yaml', 'read_text',
           'ConfigPersistError']


class ConfigPersistError(RuntimeError):
    """配置写盘失败（原文件未被破坏，内存态需由调用方回滚）。

    单独一个异常类型是为了让上层能把它和「入参不合法」区分开：
    前者是 500（服务器问题，改天重试可能就好），后者是 400。
    """

#: 递归合并的最大层级（顶层键 → 子键 → 孙键）。
#: 再深的值直接整块重写那一层 —— 现实配置不会更深，且越深注释越少。
_MAX_DEPTH = 3


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------

def read_text(path: str | os.PathLike) -> tuple[str, str]:
    """读文本，返回 ``(以 \\n 归一的文本, 原文件的换行符)``。

    归一换行是为了让正则按行工作；写回时按 ``eol`` 还原，
    否则 CRLF 文件被写成 LF 会产生「整文件都改了」的假 diff。
    """
    raw = Path(path).read_bytes()
    eol = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf-8', errors='replace')
    if eol == '\r\n':
        text = text.replace('\r\n', '\n')
    return text, eol


def atomic_write_text(path: str | os.PathLike, text: str,
                      encoding: str = 'utf-8', eol: str = '\n') -> None:
    """原子写：临时文件 → fsync → ``os.replace``。

    临时文件与目标**同目录**（`os.replace` 才能保证原子性；跨盘 rename
    不是原子操作），文件名带 pid + 随机后缀（并发写不互相踩）。
    任何异常都会删掉临时文件并**保持原文件不变**。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text if eol == '\n' else text.replace('\n', eol)
    tmp = path.with_name('.%s.%d.%s.tmp' % (path.name, os.getpid(),
                                            uuid.uuid4().hex[:8]))
    try:
        with open(tmp, 'w', encoding=encoding, newline='') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# YAML 就地合并
# ---------------------------------------------------------------------------

def _key_pattern(indent: int) -> re.Pattern:
    return re.compile(r'^%s([A-Za-z_][\w.-]*)[ \t]*:' % (' ' * indent),
                      re.MULTILINE)


def _child_blocks(text: str, indent: int) -> list[tuple[str, int, int]]:
    """列出缩进恰为 ``indent`` 的子键块 ``[(key, start, end)]``。"""
    matches = list(_key_pattern(indent).finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), m.start(), end))
    return out


def _split_trailer(block: str) -> tuple[str, str]:
    """把块尾的「空行 + 注释行」切出来。

    这些行视觉上属于**下一个键**（是它的说明），替换本键时不能顺手删掉。
    """
    lines = block.splitlines(keepends=True)
    idx = len(lines)
    while idx > 0:
        stripped = lines[idx - 1].strip()
        if stripped == '' or stripped.startswith('#'):
            idx -= 1
        else:
            break
    return ''.join(lines[:idx]), ''.join(lines[idx:])


def _dump_block(key: str, value: Any, indent: int) -> str:
    """把单个键序列化并按 ``indent`` 缩进（``sort_keys=False``：不打乱键序）。"""
    text = yaml.dump({key: value}, allow_unicode=True,
                     default_flow_style=False, sort_keys=False, width=1000)
    if indent:
        text = ''.join((' ' * indent) + line if line.strip() else line
                       for line in text.splitlines(keepends=True))
    return text


def _split_key_line(body: str, indent: int) -> tuple[str, str] | None:
    """把块拆成 ``(键行, 子块文本)``；键行带内联值时返回 None（不是映射）。"""
    nl = body.find('\n')
    if nl < 0:
        return None
    head, rest = body[:nl + 1], body[nl + 1:]
    if head.split(':', 1)[1].strip():
        return None          # `key: value` 内联形式，没有子块
    return head, rest


def _dump_list_item(item: dict, indent: int) -> str:
    """把单个列表项序列化成 ``- key: v`` 并按 ``indent`` 缩进。

    `yaml.dump([item])` 已经自带层级：首行 ``- host: …``（0 缩进），
    后续行 ``  id: …``（2 缩进）。所以统一加同一个 ``pad`` 即可 ——
    早先给后续行又补了 2 个空格，结果 ``id`` 被顶到 6 格，
    YAML 直接报 ``mapping values are not allowed here``。
    """
    text = yaml.dump([item], allow_unicode=True, default_flow_style=False,
                     sort_keys=True, width=1000)
    pad = ' ' * indent
    lines = [line for line in text.splitlines() if line.strip()]
    return '\n'.join(pad + line for line in lines) + '\n'


def _split_item_tail(seg: str) -> tuple[str, str]:
    """把列表项的原文切成 ``(条目正文, 尾部注释块)``。

    尾部那串注释/空行其实是**下一项的标题**（很常见的写法）：

        - id: boiler_01
          name: 一号锅炉
        # ---- 2 号锅炉 ----
        - id: boiler_02

    按 ``^  - `` 切分时，``# ---- 2 号锅炉 ----`` 落在 boiler_01 这段的末尾。
    删 boiler_01 时若把整段丢掉，boiler_02 的标题就跟着没了。
    所以把尾部注释剥出来、当成下一项的"前缀"。
    """
    lines = seg.splitlines(keepends=True)
    k = len(lines)
    while k > 0:
        t = lines[k - 1].strip()
        if t == '' or t.startswith('#'):
            k -= 1
            continue
        break
    return ''.join(lines[:k]), ''.join(lines[k:])


def _parse_item(content: str, pad_len: int) -> dict | None:
    """解析单个列表项正文（带缩进）。

    注意：去缩进后文本以 ``- `` 开头，`yaml.safe_load` 得到的是**列表**，
    不是 dict —— 必须取第一个元素。这一点踩过：直接当 dict 用会一路返回
    None，最后悄悄退回整份重写，注释照样丢。
    """
    dedented = '\n'.join(
        (line[pad_len:] if line.startswith(' ' * pad_len) else line)
        for line in content.splitlines())
    try:
        val = yaml.safe_load(dedented)
    except yaml.YAMLError:
        return None
    if isinstance(val, list) and len(val) == 1 and isinstance(val[0], dict):
        val = val[0]
    if not isinstance(val, dict) or not val.get('id'):
        return None
    return val


def _merge_list_by_id(body: str, indent: int, new_items: list) -> str | None:
    """就地合并 ``key:`` 下的**字典列表**，按 ``id`` 匹配，保留未变项的原文。

    ## 为什么需要它

    `_merge_block` 只处理映射。设备清单是 ``devices: [ {...}, ... ]`` ——
    只要增删一台设备，整个列表就被 `_dump_block` 重写。实测代价：
    `devices_simulated.yaml` 16025 行里 127 行注释、键序与排版一次全丢
    （"由 gen_factory 生成""现场可改这里""${VAR} 占位"这些唯一说明都没了）。
    数据没丢，但下一个人拿到这个文件会不知道它是生成的。

    ## 做法

    把每个条目拆成**三部分**分别保存：前缀注释、正文、尾部注释。
      · 前缀注释 = 上一项的尾部注释（或文首注释）
      · 尾部注释 = 下一项的标题
    重组时按新列表顺序输出 ``前缀 + 正文``，未变的条目正文**逐字保留**。
    这样删掉某一项时，它的正文消失、但它的尾部注释（下一项的标题）
    仍作为下一项的前缀输出 —— 标题不会跟着陪葬。

    只对「元素都是带 id 的字典」生效；其他形态返回 None，调用方整份重写。
    """
    nl = body.find('\n')
    if nl < 0:
        return None
    head, rest = body[:nl + 1], body[nl + 1:]

    item_pat = re.compile(r'^%s- ' % (' ' * (indent + 2)), re.MULTILINE)
    matches = list(item_pat.finditer(rest))
    if not matches:
        return None

    pad_len = indent + 2
    raw_segs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        raw_segs.append(rest[m.start():end])

    # [(id, 前缀, 正文)]，按旧顺序
    parsed: list[tuple[str, str, str]] = []
    carry = rest[:matches[0].start()]        # 首项前缀 = 文首注释
    last_tail = ''
    for seg in raw_segs:
        content, tail = _split_item_tail(seg)
        if not content.strip():
            carry += content + tail
            continue
        val = _parse_item(content, pad_len)
        if val is None:
            return None
        parsed.append((str(val['id']), carry, content))
        carry = tail
        last_tail = tail

    by_id = {pid: (pre, c) for pid, pre, c in parsed}

    out = [head]
    for item in new_items:
        if not isinstance(item, dict) or not item.get('id'):
            return None
        hit = by_id.get(str(item['id']))
        if hit is not None:
            pre, content = hit
            old_val = _parse_item(content, pad_len)
            if old_val == item:
                out.append(pre + content)     # 没变 → 逐字保留
            else:
                out.append(pre + _dump_list_item(item, indent + 2))
        else:
            out.append(_dump_list_item(item, indent + 2))
    out.append(last_tail)                     # 最后一项之后的注释/空行
    return ''.join(out)


def _merge_block(text: str, indent: int, data: dict, depth: int) -> str | None:
    """就地合并一个映射的文本；结构对不上时返回 None（调用方退回整份 dump）。"""
    blocks = _child_blocks(text, indent)
    if not blocks:
        return None

    out = [text[:blocks[0][1]]]      # 首个键之前的注释/空行，原样保留
    order = [b[0] for b in blocks]

    for key, start, end in blocks:
        block = text[start:end]
        body, trailer = _split_trailer(block)

        if key not in data:          # 键被删除：删内容、留尾注释
            out.append(trailer)
            continue

        try:
            old = yaml.safe_load(body)
        except yaml.YAMLError:
            old = None

        if isinstance(old, dict) and old.get(key) == data[key]:
            out.append(body + trailer)   # 没变 → 逐字保留（含内部注释）
            continue

        merged = None
        if (depth > 0 and isinstance(data[key], dict)
                and isinstance(old, dict) and isinstance(old.get(key), dict)):
            parts = _split_key_line(body, indent)
            if parts is not None:
                head, inner = parts
                inner_merged = _merge_block(inner, indent + 2, data[key],
                                            depth - 1)
                if inner_merged is not None:
                    merged = head + inner_merged
        elif isinstance(data[key], list):
            # 字典列表（设备清单、报警规则…）：按 id 就地增删改，
            # 未变的条目连注释一起原样留下来。详见 _merge_list_by_id。
            merged = _merge_list_by_id(body, indent, data[key])

        out.append((merged if merged is not None
                    else _dump_block(key, data[key], indent)) + trailer)

    for key, value in data.items():      # 新增键 → 追加到末尾
        if key not in order:
            out.append(_dump_block(key, value, indent))

    return ''.join(out)


def dump_yaml(path: str | os.PathLike, data: dict, *, depth: int = _MAX_DEPTH,
              sort_keys: bool = False) -> bool:
    """把 ``data`` 写进 ``path``：能就地合并就合并，否则整份重写。

    Returns:
        bool: True = 写成功；False = 写失败（**原文件未被破坏**）。
    """
    path = Path(path)
    text, eol = None, '\n'
    if path.exists():
        try:
            text, eol = read_text(path)
        except OSError as e:
            logger.error("读取原配置失败，改为整份重写 %s: %s", path, e)
            text = None

    new_text = None
    if text is not None:
        try:
            merged = _merge_block(text, 0, data, depth)
            if merged is not None:
                # 合并结果必须与原 dict 完全等价，否则退回整份重写 ——
                # 「保留注释」绝不能以「写错内容」为代价。
                if yaml.safe_load(merged) == data:
                    new_text = merged
                else:
                    logger.debug("就地合并结果与原配置不等价，退回整份重写: %s", path)
        except Exception as e:      # noqa: BLE001 —— 合并是优化，失败就整份重写
            logger.debug("就地合并失败（%s），退回整份重写: %s", e, path)

    if new_text is None:
        try:
            new_text = yaml.dump(data, allow_unicode=True,
                                 default_flow_style=False, sort_keys=sort_keys,
                                 width=1000)
        except Exception as e:      # noqa: BLE001 —— 不可序列化的值
            # 关键：此时**还没碰过原文件**，直接失败退出，文件保持原样。
            logger.error("配置序列化失败，原文件保持不变 %s: %s", path, e)
            return False

    try:
        atomic_write_text(path, new_text, eol=eol)
        return True
    except OSError as e:
        logger.error("配置写入失败（原文件保持不变）%s: %s", path, e)
        return False
