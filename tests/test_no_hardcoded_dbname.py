"""守卫草案（round 213 待落地）：禁止在**参与逻辑的字符串字面量**里写死库文件名。

为什么这一类可以干净地禁，而「相对路径字面量」不行
------------------------------------------------
* `paths.DB_PATHS` 是**唯一**该出现库文件名的地方；
  解析入口 `paths.get_db_path(mode)` / `paths.get_active_db_path()` 收的是
  **模式名**或**运行时登记的路径**，**不是文件名**。
  → 所以任何地方出现 `'scada*.db'` 字面量，**必然是错的**（没有例外可言）。
* 对比「相对路径字面量」（`配置/x.yaml` / `data/x.db`，实测 41 处 / 19 文件）：
  **大多数是正确的** —— 它们被传给了 `paths.resolve()`。
  要判它得做**数据流分析**（这个字面量有没有经过解析器），
  所以那一类**先不纳入本守卫**，只在本文件末尾记为后续工作。

判据（三层，逐层收紧）
--------------------
1. **AST**，不是 grep —— 注释/docstring 里引用库名不算
   （本轮已因「文本匹配把注释当代码」栽过三次）。
2. 只判**参与逻辑的字符串字面量**（排除 docstring）。
3. **豁免表逐条写理由**，并配**反向守卫**：豁免的文件一旦不再命中，必须从表里删掉
   （否则豁免表会变成垃圾桶 —— 这是本仓库既有守卫用过的模式）。

当前实测（2026-10-11）
---------------------
A 类命中 **21 处 / 10 文件**，其中：
* **20 处 / 9 文件**是真缺陷（`tools/` 下的诊断/部署/迁移/规划工具）；
* **1 处**（`config.py` 的 `except ImportError:` 兜底分支）**可辩护**，进豁免表。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 生产代码扫描范围（排除测试 / 构建产物 / 虚拟环境 / 历史遗留）
SCAN_DIRS = ("core", "采集层", "存储层", "报警层", "展示层", "智能层", "用户层",
             "timeseries", "tools")
SCAN_FILES = ("run.py", "config.py", "paths.py")
EXCLUDE_PARTS = (".venv", "node_modules", "dist", "build", ".pytest_tmp",
                 "__pycache__", ".git", "release", "legacy")

#: 库文件名形态（含 SQLite 的 -wal / -shm 旁路文件）
DB_NAME_RE = re.compile(r"^scada[a-z_]*\.db(-wal|-shm)?$")

#: ★ 豁免表：`(相对路径, 语句文本) -> 理由`。
#:
#: 规矩（与仓库既有 `ALLOWED_HITS` 同款）：
#:   ① 每条**必须写清理由**；
#:   ② **修好了必须删掉** —— 下面的 `test_allowlist_has_no_stale_entries` 会强制这一点。
#:
#: ⭐ **键为什么是「语句文本」而不是「行号」（round 214 根治）**
#: 行号会因**任何无关改动**漂移：本仓实测两次 —— round 209 改 config.py 导致
#: `config.py:106 → 120`；round 214 又在同文件顶部加了 3 行弱值 → `120 → 122`。
#: 两次都让守卫**假红**（内容一个字没改，豁免却失效），而假红的守卫早晚被关掉。
#: 语句文本只在**这行真的被改**时才变 —— 那正是我们**想**让豁免失效的时刻
#: （改了就要重新审一遍理由）。归一化：去首尾空白 + 连续空白压成单空格。
#: ⚠️ 已知代价：若给同一行改格式（如加括号、拆行），键也会失效 —— 但那同样
#: 值得人工看一眼，比"改三行别的代码就假红"划算得多。
ALLOWED_HARDCODED: dict[tuple[str, str], str] = {
    ("config.py", "_DEFAULT_DB_PATH = _DATA_DIR / 'scada.db'"):
        "`except ImportError:` 兜底分支 —— paths 模块不可用时的最后手段；"
        "正常路径读的是 paths.DB_PATHS['default']（单一真源），不是第二套真源",
    ("tools/deploy.py", "legacy = latest_backup / 'data' / 'scada.db'"):
        "rollback 的**旧格式备份兼容**：早年 deploy 备份库时写死文件名 "
        "'scada.db'，已存在的备份目录里就是这个名字。新备份已改为跟随真源库名"
        "（`db_path.name`），但读旧备份时必须能认这个名字 —— 这是兼容入口，"
        "不是第二套真源（旁边 legacy 分支只在主名不存在时启用）。",
}


def _iter_production_py() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        root = BACKEND_ROOT / d
        if root.is_dir():
            out += [p for p in root.rglob("*.py")
                    if not any(x in p.parts for x in EXCLUDE_PARTS)]
    for n in SCAN_FILES:
        p = BACKEND_ROOT / n
        if p.is_file():
            out.append(p)
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    """所有 docstring 常量的 id（要排除 —— 注释里引用库名不算缺陷）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _db_paths_exempt_lines(p: pathlib.Path) -> set[int]:
    """`p` 里 `DB_PATHS = {...}` 那张表**内部**的行号 —— **唯一真源**，必须豁免。

    ⚠️ 用**行号**而不是 `id(node)` 做豁免键：`id()` 只在**同一次解析**内有效，
    元守卫里换一次解析就对不上了 —— 我第一版就是这么写的，
    结果被自己的元守卫抓出来（**夹具 bug，不是守卫 bug**）。
    行号跨解析稳定，而且更可读（豁免表里能直接写"哪一行、为什么"）。

    不豁免就会给出 4 条假阳性（`paths.py:135-138`）——
    而**假阳性和假绿一样有害**：假红的守卫早晚被人关掉。
    """
    if not p.is_file():
        return set()
    tree = ast.parse(p.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DB_PATHS" for t in targets):
            continue
        if value is None:
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                lines.add(sub.lineno)
    return lines


def _const_stmt_map(tree: ast.AST) -> dict[int, ast.stmt]:
    """`{常量节点 id: 它所属的**最内层**语句}`。

    用于把豁免键从「行号」升级为「语句文本」—— 行号会因**任何无关改动**漂移
    （本仓实测两次：round 209 改 config.py 导致 106→120；round 214 又 +2 行 → 122），
    而语句文本只在**这行真的被改**时才变，那正是我们想让豁免失效的时刻。
    """
    out: dict[int, ast.stmt] = {}
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.stmt):
            continue
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Constant):
                out[id(sub)] = stmt        # 后访问的更内层 → 覆盖外层
    return out


def _stmt_key(source: str, stmt: ast.stmt) -> str:
    """语句文本归一化（去首尾空白、把连续空白压成单空格），用作稳定豁免键。"""
    seg = ast.get_source_segment(source, stmt) or ""
    return " ".join(seg.split())


def _collect_from_files(files: list[pathlib.Path],
                        exempt_lines: dict[str, set[int]] | None = None
                        ) -> list[tuple[str, str, int, str]]:
    """在给定文件列表里找写死的库名。

    Returns:
        `[(文件, 语句文本, 行号, 字面量)]` —— 语句文本是**稳定豁免键**（抗行号漂移）。

    Args:
        files: 要扫的文件。
        exempt_lines: `{文件名: {豁免行号}}` —— 用于豁免单一真源表（按行号，因为
            那张表本身就是「行号内的一块字面量」，行号语义更自然）。
    """
    hits: list[tuple[str, str, int, str]] = []
    exempt_lines = exempt_lines or {}
    for p in files:
        key = _key_of(p)
        skip = exempt_lines.get(key, set())
        source = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as e:            # pragma: no cover
            pytest.fail(f"{key} 语法错误: {e}")
        ds = _docstring_ids(tree)
        stmt_of = _const_stmt_map(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in ds and node.lineno not in skip
                    and DB_NAME_RE.match(node.value)):
                stmt = stmt_of.get(id(node))
                skey = _stmt_key(source, stmt) if stmt is not None else ""
                hits.append((key, skey, node.lineno, node.value))
    return hits


def _key_of(p: pathlib.Path) -> str:
    """扫描结果里用的文件名：仓库内用相对路径，其余用文件名（元守卫的夹具）。"""
    try:
        return p.relative_to(BACKEND_ROOT).as_posix()
    except ValueError:
        return p.name


def _collect_hardcoded_dbnames() -> list[tuple[str, str, int, str]]:
    """返回 `[(相对路径, 语句文本, 行号, 字面量)]`（已排除 docstring 与单一真源表）。"""
    files = _iter_production_py()
    exempt = {_key_of(p): _db_paths_exempt_lines(p) for p in files}
    return _collect_from_files(files, exempt)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_scan_covers_real_files():
    """元守卫：先证明扫描器真的在扫东西（防 exclude 写错导致"扫了 0 个"的假绿）。"""
    files = _iter_production_py()
    assert len(files) > 100, f"只扫到 {len(files)} 个文件 —— 跳过集可能写错了"
    rels = {p.relative_to(BACKEND_ROOT).as_posix() for p in files}
    for must in ("run.py", "paths.py", "采集层/data_collector.py", "存储层/database.py"):
        assert must in rels, f"{must} 没有被扫到"


def test_no_hardcoded_db_filename_in_production_code():
    """★ 核心：生产代码里不得写死库文件名（必须走 paths 的解析）。"""
    bad = [
        f"{rel}:{ln}  {val!r}   （语句: {skey!r}）"
        for rel, skey, ln, val in _collect_hardcoded_dbnames()
        if (rel, skey) not in ALLOWED_HARDCODED
    ]
    assert not bad, (
        "生产代码里出现了**写死的库文件名** —— 库名只能出现在 `paths.DB_PATHS` 那一张表里。\n"
        "  解析入口是 `paths.get_db_path(mode)` / `paths.get_active_db_path()`，\n"
        "  它们收的是**模式名**或**运行时登记的路径**，不是文件名。\n"
        "  ⚠️ 后果是**静默的**：写死 `scada.db` 的工具会指向那份没人用的旧库，\n"
        "     而且结果看起来完全正常（真文件、真表、真行数）——\n"
        "     诊断会报「正常」、迁移会「成功」、备份会备下错的库。\n"
        "  修法：改用 `paths.get_active_db_path()`；工具类没有 app 上下文时\n"
        "        用 `paths.get_db_path(mode)` 并**显式传 mode**（不许默认值悄悄选库）。\n  "
        + "\n  ".join(sorted(bad))
    )


def test_allowlist_entries_are_not_stale():
    """★ 反向守卫：豁免的文件一旦不再命中，必须从豁免表里删掉。

    没有这条，豁免表会慢慢变成垃圾桶 —— 修好的东西留在里面，
    下次真回归时守卫是绿的（因为它被豁免了）。
    """
    live = {(rel, skey) for rel, skey, _ln, _v in _collect_hardcoded_dbnames()}
    stale = [f"{rel} :: {skey!r}" for (rel, skey) in ALLOWED_HARDCODED
             if (rel, skey) not in live]
    assert not stale, (
        "豁免表里有**已经不再命中**的条目 —— 说明那处已经修好了，请把它们删掉：\n  "
        + "\n  ".join(sorted(stale))
    )


def test_allowlist_is_not_empty():
    """元守卫：豁免表**不得被清空**。

    没有这条，把表清空就能让下面两条防腐检查（stale / 理由）一起空转 ——
    守卫自己绿了，而豁免宏磁化全部失效（20 处缺陷重新变成"未豁免"虽然会让
    主守卫报警，但那是主守卫的功劳，不是登记表的防腐能力）。
    """
    assert ALLOWED_HARDCODED, (
        "豁免表是空的 —— 「不得腐烂」「必须写理由」两条元守卫会随之空转")


def test_allowlist_entries_have_reasons():
    """豁免必须逐条写理由（防空理由蒙混）。"""
    for key, reason in ALLOWED_HARDCODED.items():
        assert reason and len(reason.strip()) >= 10, f"豁免 {key} 没写理由"


# ---------------------------------------------------------------------------
# 元守卫：让这道守卫**自己可验证**（防假绿 + 防假红）
# ---------------------------------------------------------------------------

class TestMetaGuards:
    """按 skill `static-guard-tests` 的纪律：守卫自己也要有正向/反向对照。"""

    def test_flags_a_real_hardcoded_literal(self, tmp_path):
        """★ 防假绿：夹具里放一个**真写死**的模块 → 必须报出来。"""
        f = tmp_path / "synthetic_bad.py"
        f.write_text(
            "from pathlib import Path\n"
            "DB = Path('data') / 'scada.db'\n"
            "WAL = 'scada_simulated.db-wal'\n",
            encoding="utf-8")
        hits = _collect_from_files([f])
        vals = {v for _r, _s, _l, v in hits}
        assert "scada.db" in vals, f"没报出写死的 'scada.db'：{hits}"
        assert "scada_simulated.db-wal" in vals, f"没报出 -wal 变体：{hits}"

    def test_ignores_docstring_and_comment_mentions(self, tmp_path):
        """★ 防假红：只出现在 **docstring / 注释** 里的库名**不算缺陷**。

        这条正是本轮反复踩的坑（代码指针守卫 / 防回退守卫 / 变异台都栽过）：
        **文本匹配会把注释当代码**。
        """
        f = tmp_path / "synthetic_doc.py"
        f.write_text(
            '"""说明：以前这里写死了 data/scada.db，现已改走 paths。"""\n'
            "import paths\n"
            "def f():\n"
            '    """docstring 里也提一下 scada.db。"""\n'
            "    # 注释里再提一次 scada_simulated.db\n"
            "    return paths.get_active_db_path()\n",
            encoding="utf-8")
        hits = _collect_from_files([f])
        assert hits == [], f"注释/docstring 里的库名被误报成缺陷：{hits}"

    def test_ignores_the_single_source_table(self, tmp_path):
        """★ 防假红：单一真源表（`DB_PATHS`）里的字面量必须豁免。"""
        f = tmp_path / "synthetic_paths.py"
        f.write_text(
            "from pathlib import Path\n"
            "DB_PATHS = {\n"
            "    'simulated': Path('scada_simulated.db'),\n"
            "    'default': Path('scada.db'),\n"
            "}\n",
            encoding="utf-8")
        exempt = {_key_of(f): _db_paths_exempt_lines(f)}
        hits = _collect_from_files([f], exempt)
        assert hits == [], f"单一真源表里的字面量被误报：{hits}"
        # 反向：**同一份文件**里表外的写死必须仍被报出来（防豁免开得太大）
        f2 = tmp_path / "synthetic_paths_outside.py"
        f2.write_text(
            "from pathlib import Path\n"
            "DB_PATHS = {'default': Path('scada.db')}\n"
            "LEAK = Path('data') / 'scada.db'\n",
            encoding="utf-8")
        exempt2 = {_key_of(f2): _db_paths_exempt_lines(f2)}
        hits2 = _collect_from_files([f2], exempt2)
        assert [v for _r, _s, _l, v in hits2] == ["scada.db"], (
            f"豁免开得太大 —— 表外的写死也被放过了：{hits2}")


    def test_does_not_flag_the_correct_patterns(self, tmp_path):
        """★ 防假红：**正确写法**必须一条都不报（这证明"修好之后会变绿"）。

        三种正确写法（round 212/213 定的口径）：
          1. 运行时单一真源：`paths.get_active_db_path()`
          2. 按模式解析：`paths.get_db_path('simulated')` —— 注意传的是**模式名**，不是文件名
          3. 读单一真源表：`paths.DB_PATHS['default']`
        """
        f = tmp_path / "synthetic_good.py"
        f.write_text(
            "import paths\n"
            "from pathlib import Path\n"
            "def f():\n"
            "    a = paths.get_active_db_path()\n"
            "    b = paths.get_db_path('simulated')\n"
            "    c = paths.DB_PATHS['default']\n"
            "    return a, b, c\n",
            encoding="utf-8")
        hits = _collect_from_files([f])
        assert hits == [], f"正确写法被误报：{hits}"


# ---------------------------------------------------------------------------
# 后续工作（本守卫**刻意未覆盖**，因为需要数据流分析，先记下来）
# ---------------------------------------------------------------------------
#
# 「相对路径字面量」（`配置/x.yaml` / `data/x.db`）实测 **41 处 / 19 文件**，
# 但**大多数是正确的** —— 它们被传给了 `paths.resolve()`。
# 要判它必须回答：「这个字面量有没有经过解析器？」
#   正例：`paths.resolve('配置/system.yaml')`        → OK
#   反例：`Path('data/scada.db')` / `open('配置/x')` → 缺陷
# 这需要跟踪「字面量 → 变量 → 调用参数」的数据流（甚至跨函数），
# 属另一个量级的工作，**不要在守卫里用"出现即违规"糊过去**（会满屏假阳性，
# 假红的守卫早晚被人关掉）。
def test_broader_relative_path_class_is_documented():
    """占位：把"更大那一类还没覆盖"这件事**写在测试里**，免得被人误以为已覆盖。"""
    assert True, (
        "「相对路径字面量」那一类（41 处 / 19 文件）尚未纳入守卫，"
        "原因是需要数据流分析（是否经过 paths.resolve）。"
    )


# ---------------------------------------------------------------------------
# round 213 新增的单一真源助手：本身也要有测试（否则它自己成了无守卫的新代码）
# ---------------------------------------------------------------------------

class TestCliDbModeResolver:
    """`paths.resolve_cli_db_mode` 的优先级链 —— 20 处修复都依赖它。"""

    def test_explicit_mode_wins(self, monkeypatch):
        import paths
        monkeypatch.setenv('SCADA_DB_MODE', 'real')
        assert paths.resolve_cli_db_mode('simulated') == 'simulated', (
            '显式传入的 mode 必须优先于 env')

    def test_env_used_when_no_explicit(self, monkeypatch):
        import paths
        monkeypatch.setenv('SCADA_DB_MODE', 'real')
        assert paths.resolve_cli_db_mode(None) == 'real'

    def test_fallback_is_simulated(self, monkeypatch):
        import paths
        monkeypatch.delenv('SCADA_DB_MODE', raising=False)
        assert paths.resolve_cli_db_mode(None) == 'simulated', (
            '回退值必须与 run.py 无参默认（模拟模式）一致')

    def test_empty_env_string_falls_back(self, monkeypatch):
        """空串 env 视同未设置（`os.environ.get` 返回 '' 也是 falsy）。"""
        import paths
        monkeypatch.setenv('SCADA_DB_MODE', '')
        assert paths.resolve_cli_db_mode(None) == 'simulated'

    def test_describe_shows_mode_and_path(self, monkeypatch):
        import paths
        monkeypatch.delenv('SCADA_DB_MODE', raising=False)
        desc = paths.describe_db_choice('real')
        assert 'real' in desc and 'scada_real.db' in desc, desc
