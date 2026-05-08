"""
通过GitHub REST API直接推送代码（绕过git CLI限制）
读取本地文件 → 比对远程 → 创建blob → 创建tree → 创建commit → 更新ref
"""
import os
import sys
import json
import hashlib
import base64
import requests
from pathlib import Path
from datetime import datetime

PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
REPO = "chenxinghang-a/scada"
BRANCH = "main"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

if not TOKEN:
    # 尝试从gh config读
    gh_config = Path.home() / ".config" / "gh" / "hosts.yml"
    if not gh_config.exists():
        gh_config = Path.home() / ".config" / "gh" / "config.yml"
    
    # 尝试从git credential读
    git_cred = Path.home() / ".git-credentials"
    if git_cred.exists():
        content = git_cred.read_text()
        for line in content.splitlines():
            # 使用更安全的URL解析
            if line.startswith("https://oauth2:") and "github.com" in line:
                # 提取token：https://oauth2:TOKEN@github.com/...
                try:
                    token_part = line.split("https://oauth2:")[1]
                    if "@" in token_part:
                        TOKEN = token_part.split("@")[0]
                        break
                except (IndexError, ValueError):
                    continue

if not TOKEN:
    print("ERROR: 没找到GitHub Token，设置环境变量 GITHUB_TOKEN 或配置 gh auth")
    sys.exit(1)

print(f"Token found: {TOKEN[:8]}...")

BASE = "https://api.github.com"
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

# .gitignore 排除规则
IGNORE_DIRS = {"__pycache__", "logs", "data", "venv", ".venv", "backup", ".git", "exports", ".workbuddy"}
IGNORE_FILES = {".env"}
IGNORE_EXTS = {".pyc", ".pyo", ".db", ".sqlite3", ".log", ".bak"}
IGNORE_PATTERNS = ["stderr", "stdout"]  # 开头匹配

PROJECT_DIR = Path(r"c:\Users\cxx\WorkBuddy\Claw\industrial_scada")


def should_ignore(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    name = Path(rel_path).name
    
    # 检查目录
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    
    # 检查文件名
    if name in IGNORE_FILES:
        return True
    
    # 检查扩展名
    for ext in IGNORE_EXTS:
        if name.endswith(ext):
            return True
    
    # 检查前缀模式
    for pat in IGNORE_PATTERNS:
        if name.startswith(pat):
            return True
    
    # 排除临时文件
    if name.startswith("_git_") or name == "_push.bat":
        return True
    
    return False


def get_remote_ref():
    """获取远程main分支的最新commit SHA"""
    url = f"{BASE}/repos/{REPO}/git/refs/heads/{BRANCH}"
    r = requests.get(url, headers=HEADERS, proxies=PROXY, timeout=15)
    if r.status_code == 200:
        sha = r.json()["object"]["sha"]
        print(f"Remote HEAD: {sha[:12]}")
        return sha
    else:
        print(f"获取远程ref失败: {r.status_code} {r.text[:200]}")
        return None


def get_commit(sha):
    """获取commit详情"""
    url = f"{BASE}/repos/{REPO}/git/commits/{sha}"
    r = requests.get(url, headers=HEADERS, proxies=PROXY, timeout=15)
    return r.json()


def get_tree(sha):
    """获取tree详情（递归）"""
    url = f"{BASE}/repos/{REPO}/git/trees/{sha}?recursive=1"
    r = requests.get(url, headers=HEADERS, proxies=PROXY, timeout=15)
    return r.json()


def create_blob(file_path: Path):
    """创建blob（上传文件内容）"""
    try:
        # 尝试UTF-8读取
        content = file_path.read_text(encoding='utf-8')
        encoding = 'utf-8'
    except UnicodeDecodeError:
        # 二进制文件用base64
        content = base64.b64encode(file_path.read_bytes()).decode('ascii')
        encoding = 'base64'
    
    url = f"{BASE}/repos/{REPO}/git/blobs"
    data = {"content": content, "encoding": encoding}
    r = requests.post(url, headers=HEADERS, json=data, proxies=PROXY, timeout=30)
    if r.status_code == 201:
        return r.json()["sha"]
    else:
        print(f"  创建blob失败: {file_path.name} -> {r.status_code}")
        return None


def create_tree(base_tree_sha, tree_entries):
    """创建新tree"""
    url = f"{BASE}/repos/{REPO}/git/trees"
    data = {"base_tree": base_tree_sha, "tree": tree_entries}
    r = requests.post(url, headers=HEADERS, json=data, proxies=PROXY, timeout=30)
    if r.status_code == 201:
        return r.json()["sha"]
    else:
        print(f"创建tree失败: {r.status_code} {r.text[:300]}")
        return None


def create_commit(tree_sha, parent_sha, message):
    """创建commit"""
    url = f"{BASE}/repos/{REPO}/git/commits"
    data = {
        "message": message,
        "tree": tree_sha,
        "parents": [parent_sha]
    }
    r = requests.post(url, headers=HEADERS, json=data, proxies=PROXY, timeout=15)
    if r.status_code == 201:
        return r.json()["sha"]
    else:
        print(f"创建commit失败: {r.status_code} {r.text[:300]}")
        return None


def update_ref(sha):
    """更新分支指向"""
    url = f"{BASE}/repos/{REPO}/git/refs/heads/{BRANCH}"
    data = {"sha": sha, "force": False}
    r = requests.patch(url, headers=HEADERS, json=data, proxies=PROXY, timeout=15)
    if r.status_code == 200:
        return True
    else:
        print(f"更新ref失败: {r.status_code} {r.text[:300]}")
        return False


def collect_local_files():
    """收集本地所有需要推送的文件"""
    files = {}
    for f in PROJECT_DIR.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(PROJECT_DIR)).replace("\\", "/")
            if not should_ignore(rel):
                files[rel] = f
    return files


def main():
    print("=== 通过GitHub API推送 ===\n")
    
    # 1. 获取远程HEAD
    remote_sha = get_remote_ref()
    if not remote_sha:
        sys.exit(1)
    
    # 2. 获取远程tree
    remote_commit = get_commit(remote_sha)
    remote_tree_sha = remote_commit["tree"]["sha"]
    remote_tree = get_tree(remote_tree_sha)
    
    # 构建远程文件hash映射 {path: sha}
    remote_files = {}
    if "tree" in remote_tree:
        for item in remote_tree["tree"]:
            if item["type"] == "blob":
                remote_files[item["path"]] = item["sha"]
    
    print(f"远程文件数: {len(remote_files)}")
    
    # 3. 收集本地文件
    local_files = collect_local_files()
    print(f"本地文件数: {len(local_files)}")
    
    # 4. 找出需要更新/新增的文件
    tree_entries = []
    changed = []
    
    for rel_path, local_path in local_files.items():
        # 计算本地文件的git blob hash（SHA-1 of "blob {size}\0{content}"）
        try:
            content = local_path.read_bytes()
        except Exception as e:
            print(f"  跳过 {rel_path}: {e}")
            continue
        
        # git blob hash
        git_header = f"blob {len(content)}\0".encode()
        local_hash = hashlib.sha1(git_header + content).hexdigest()
        
        remote_hash = remote_files.get(rel_path)
        
        if remote_hash != local_hash:
            # 需要上传
            blob_sha = create_blob(local_path)
            if blob_sha:
                tree_entries.append({
                    "path": rel_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha
                })
                action = "更新" if remote_hash else "新增"
                changed.append(f"  {action}: {rel_path}")
        
        # 从远程映射中删除已处理的
        remote_files.pop(rel_path, None)
    
    # 5. 标记需要删除的文件（远程有但本地没有）
    for deleted_path in remote_files:
        tree_entries.append({
            "path": deleted_path,
            "mode": "100644",
            "type": "blob",
            "sha": None  # None = 删除
        })
        changed.append(f"  删除: {deleted_path}")
    
    if not tree_entries:
        print("\n没有变更，无需推送")
        return
    
    print(f"\n变更文件 {len(tree_entries)} 个:")
    for c in changed:
        print(c)
    
    # 6. 创建tree
    print("\n创建tree...")
    new_tree_sha = create_tree(remote_tree_sha, tree_entries)
    if not new_tree_sha:
        sys.exit(1)
    print(f"新tree: {new_tree_sha[:12]}")
    
    # 7. 创建commit
    commit_msg = f"fix: alarm_output - buzzer pulse, manual toggle, flash thread safety\n\n{len(changed)} files changed"
    print("创建commit...")
    new_commit_sha = create_commit(new_tree_sha, remote_sha, commit_msg)
    if not new_commit_sha:
        sys.exit(1)
    print(f"新commit: {new_commit_sha[:12]}")
    
    # 8. 更新ref
    print("更新分支...")
    if update_ref(new_commit_sha):
        print(f"\n✅ 推送成功！ https://github.com/{REPO}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
