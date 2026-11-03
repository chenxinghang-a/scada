"""界面多语言（i18n）。

## 现状与边界（重要，别当成「全站已翻译」）

模板里约有 **3000 处中文文案**，JS 里约 **900 处**。一次性全量迁移不现实，
也没必要：现场 90% 的界面文字是设备名、点位名、报警内容 —— 那些是**数据**，
不该翻译；真正需要翻译的是**界面骨架**（导航、按钮、表头、提示语）。

所以这里只做「骨架」：

* 词条按**语义 key** 组织（`nav.dashboard` / `btn.save`），不是把中文当 key；
* 词条文件在 `配置/i18n/<lang>.json`，可增量补；
* 前端 `data-i18n="nav.dashboard"` 标记要翻译的元素，`i18n.js` 统一替换；
* **没有词条的元素保持原文**（中文），不会变成空白 —— 这比「翻了 90% 剩下的
  变成 undefined」安全得多。

## 语言从哪来（优先级从高到低）

1. URL 参数 `?lang=en-US`（临时预览，会写进 localStorage）
2. 浏览器 localStorage（用户上次在设置里选的）
3. `配置/system.yaml` 的 `ui.language`（部署默认值）
4. 浏览器 `Accept-Language`
5. `zh-CN`

## 支持的语言

见 `SUPPORTED`。新增语言只需放一份 `<lang>.json` 并加进这个列表。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 支持的语言。第一个是兜底（词条最全的那个）。
SUPPORTED: tuple[str, ...] = ('zh-CN', 'en-US')
DEFAULT_LANG = 'zh-CN'

#: 界面语言的显示名（给设置页的下拉框用，用各语言自己的写法）
LANG_LABELS: dict[str, str] = {
    'zh-CN': '简体中文',
    'en-US': 'English',
}

_I18N_DIR = Path(__file__).resolve().parent.parent / '配置' / 'i18n'
_cache: dict[str, dict[str, str]] = {}
_cache_lock = threading.Lock()


def normalize(lang: str | None) -> str:
    """把各种写法归一化到 `SUPPORTED` 里的一个。

    `en` / `en-us` / `en_US` / `EN` 都认；认不出就回默认。
    现场常见的是浏览器发 `en-US,en;q=0.9` 这种带 q 值的串。
    """
    if not lang:
        return DEFAULT_LANG
    raw = str(lang).split(',')[0].split(';')[0].strip().replace('_', '-').lower()
    if not raw:
        return DEFAULT_LANG
    for supported in SUPPORTED:
        if raw == supported.lower():
            return supported
    # `en` → `en-US`；`zh` / `zh-hans` → `zh-CN`
    primary = raw.split('-')[0]
    for supported in SUPPORTED:
        if supported.lower().split('-')[0] == primary:
            return supported
    return DEFAULT_LANG


def load(lang: str) -> dict[str, str]:
    """加载某语言的词条（带缓存）。文件缺失返回空 dict，不抛异常。"""
    lang = normalize(lang)
    with _cache_lock:
        if lang in _cache:
            return _cache[lang]

    path = _I18N_DIR / f'{lang}.json'
    entries: dict[str, str] = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            entries = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        else:
            logger.error('i18n 词条文件顶层不是对象: %s', path)
    except FileNotFoundError:
        logger.warning('i18n 词条文件不存在（该语言将全部回退原文）: %s', path)
    except Exception as e:      # noqa: BLE001
        logger.error('i18n 词条文件读取失败: %s — %s', path, e)

    with _cache_lock:
        _cache[lang] = entries
    return entries


def translate(key: str, lang: str = DEFAULT_LANG, default: str | None = None) -> str:
    """取词条。**缺失时返回 `default`（调用方传原文）或 key 本身**。

    为什么不用 gettext：项目里没有 `.po` 工具链，运维也不会跑 `msgfmt`。
    JSON 词条可以直接用记事本改，现场应急时能改。
    """
    entries = load(lang)
    if key in entries and entries[key]:
        return entries[key]
    if default is not None:
        return default
    # 回退到默认语言（中文词条最全）
    if lang != DEFAULT_LANG:
        fallback = load(DEFAULT_LANG)
        if key in fallback and fallback[key]:
            return fallback[key]
    return key


def bundle(lang: str) -> dict[str, Any]:
    """返回给前端的一整包：当前语言 + 可选语言列表 + 词条。"""
    lang = normalize(lang)
    return {
        'language': lang,
        'supported': [{'code': c, 'label': LANG_LABELS.get(c, c)} for c in SUPPORTED],
        'messages': load(lang),
    }


def clear_cache() -> None:
    """清缓存（词条文件改了之后调，或测试用）。"""
    with _cache_lock:
        _cache.clear()


def coverage(lang: str) -> tuple[int, int]:
    """返回 (该语言词条数, 默认语言词条数) —— 给体检工具报告翻译完整度。"""
    return len(load(lang)), len(load(DEFAULT_LANG))
