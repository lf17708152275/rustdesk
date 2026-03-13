#!/usr/bin/env python3
# coding: utf-8
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).resolve().parent
WF_DIR = REPO_ROOT / ".github" / "workflows"

REQ = [("PUB_KEY", "${{ secrets.PUB_KEY }}"),
       ("SERVERS", "${{ vars.SERVERS }}"),
       ("API_SERVERS", "${{ vars.API_SERVERS }}")]

NEW_SUBMODULE_URL = "https://github.com/lf17708152275/hbb_common.git"

def run_cmd(args, cwd: Path):
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

def parse_gitmodules_for_hbb_common(repo_root: Path) -> str | None:
    gm = repo_root / ".gitmodules"
    if not gm.exists():
        return None
    text = gm.read_text(encoding="utf-8", errors="ignore")
    # 解析 [submodule "..."] 块，提取 path/url，优先匹配包含 hbb_common 的块
    blocks = re.split(r"\n\s*\[submodule\s*\"", text)
    best_path = None
    for b in blocks:
        # 形如: name"]\n  path = libs/hbb_common\n  url = ...
        # 简易抽取
        m_path = re.search(r"^\s*path\s*=\s*(.+)$", b, flags=re.MULTILINE)
        m_url = re.search(r"^\s*url\s*=\s*(.+)$", b, flags=re.MULTILINE)
        path_val = m_path.group(1).strip() if m_path else None
        url_val = m_url.group(1).strip() if m_url else ""
        if not path_val:
            continue
        if "hbb_common" in path_val or "hbb_common" in url_val:
            return path_val
        # 兜底记一个候选
        if best_path is None:
            best_path = path_val
    return best_path

def switch_hbb_common_submodule(repo_root: Path, new_url: str = NEW_SUBMODULE_URL) -> bool:
    # 优先从 .gitmodules 找路径，找不到则按 rustdesk 结构默认值
    sub_path = parse_gitmodules_for_hbb_common(repo_root) or "libs/hbb_common"

    # 1) set-url
    code, out, err = run_cmd(["git", "submodule", "set-url", sub_path, new_url], repo_root)
    if code != 0:
        print(f"git submodule set-url failed for {sub_path}: {err or out}")
        return False

    # 2) sync
    code, out, err = run_cmd(["git", "submodule", "sync", "--recursive"], repo_root)
    if code != 0:
        print(f"git submodule sync failed: {err or out}")
        return False

    # 3) update
    code, out, err = run_cmd(["git", "submodule", "update", "--init", "--recursive"], repo_root)
    if code != 0:
        print(f"git submodule update failed: {err or out}")
        return False

    print(f"Switched submodule {sub_path} to {new_url}")
    return True

def update_and_stage_submodule_pointer(repo_root: Path, sub_path: str) -> bool:
    """将子模块检出到远端默认分支（origin/HEAD -> main/master），并在父仓库暂存指针。

    步骤：
      1) 在子模块目录执行 fetch
      2) 解析 origin/HEAD 指向的分支，若失败则回退到 main/master
      3) checkout 到对应分支最新提交（跟踪 origin/<branch>）
      4) 回到父仓库执行 git add <sub_path> .gitmodules
    """
    sub_dir = repo_root / sub_path
    if not sub_dir.exists():
        print(f"Submodule path not found: {sub_dir}")
        return False

    # 1) fetch 子模块远端
    code, out, err = run_cmd(["git", "fetch", "--prune", "--tags", "origin"], sub_dir)
    if code != 0:
        print(f"git fetch in submodule failed: {err or out}")
        return False

    # 2) 解析 origin/HEAD 指向
    branch = None
    code, head_ref, err = run_cmd(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], sub_dir)
    if code == 0 and head_ref.startswith("origin/"):
        branch = head_ref.split("/", 1)[1]
    else:
        # 回退到 main/master 之一
        for cand in ("main", "master"):
            code2, _, _ = run_cmd(["git", "rev-parse", "--verify", f"origin/{cand}"], sub_dir)
            if code2 == 0:
                branch = cand
                break
    if not branch:
        print("Cannot determine remote default branch for submodule.")
        return False

    # 3) 检出到远端最新提交（创建/重置同名本地分支并跟踪 origin/<branch>）
    code, out, err = run_cmd(["git", "checkout", "-B", branch, f"origin/{branch}"], sub_dir)
    if code != 0:
        print(f"git checkout to origin/{branch} failed: {err or out}")
        return False

    code, sha, err = run_cmd(["git", "rev-parse", "HEAD"], sub_dir)
    if code != 0:
        print(f"git rev-parse in submodule failed: {err or sha}")
        return False

    # 4) 暂存父仓库中的子模块指针和 .gitmodules（如有变更）
    code, out, err = run_cmd(["git", "add", sub_path, ".gitmodules"], repo_root)
    if code != 0:
        print(f"git add submodule pointer failed: {err or out}")
        return False

    print(f"Submodule {sub_path} updated to {sha[:12]} on branch {branch} and staged.")
    return True

def detect_nl(s: str) -> str:
    return "\r\n" if "\r\n" in s else "\n"

def parse_kv(line: str):
    m = re.match(r'^\s*([A-Za-z0-9_][A-Za-z0-9_\-]*)\s*:\s*(.*)$', line)
    return (m.group(1), m.group(2)) if m else (None, None)

def is_top_key(line: str) -> bool:
    # 顶层键：行首无缩进且非注释
    return re.match(r'^[A-Za-z0-9_-]+:\s*$', line) is not None

def env_block_bounds(lines, env_idx: int):
    """
    env 块：从 env: 下一行起，允许：
      - 空行
      - 注释行（# 开头，无论缩进与否）
      - 缩进行（实际的 env 键）
    遇到“非注释且非缩进的顶层键行”才结束。
    """
    i = env_idx + 1
    n = len(lines)
    while i < n:
        l = lines[i]
        if l.startswith(" ") or l.startswith("\t") or l.strip() == "" or l.lstrip().startswith("#"):
            i += 1
            continue
        # 非注释+非缩进：若是顶层键，结束；否则也结束以避免误并入
        break
    return env_idx, i

def normalize_env(lines, nl: str) -> (bool, list):
    changed = False

    # 找顶层 env:（行首）
    env_idx = None
    for i, l in enumerate(lines):
        if l.startswith("env:"):
            env_idx = i
            break

    if env_idx is None:
        # 无 env:，插在 name: 后或文件头
        insert_pos = 0
        for i, l in enumerate(lines):
            if l.startswith("name:"):
                insert_pos = i + 1
                break
        block = ["env:"] + [f"  {k}: {v}" for k, v in REQ]
        return True, lines[:insert_pos] + block + lines[insert_pos:]

    # 先无条件把单行 env 拆为块
    m_inline = re.match(r'^env:\s+(.*)$', lines[env_idx])
    if m_inline:
        rest = m_inline.group(1).strip()
        lines[env_idx] = "env:"
        # 如果 rest 本身是 "KEY: VAL" 形式，则作为一行加入块；否则保持原样缩进加入
        k0, v0 = parse_kv(rest)
        if k0:
            lines.insert(env_idx + 1, f"  {k0}: {v0}")
        else:
            lines.insert(env_idx + 1, f"  {rest}")
        changed = True

    # 重新计算块范围（允许注释行/空行）
    start, end = env_block_bounds(lines, env_idx)

    # 解析现有 env 键（保序，跳过注释和空行）
    order = []
    kv = {}
    for j in range(env_idx + 1, end):
        if lines[j].lstrip().startswith("#") or lines[j].strip() == "":
            continue
        k, v = parse_kv(lines[j])
        if k and k not in kv:
            order.append(k)
            kv[k] = v

    # 顶部插入必需键（幂等）
    new_order = []
    for k, v in REQ:
        if k not in kv:
            kv[k] = v
            changed = True
        new_order.append(k)
    for k in order:
        if k not in new_order:
            new_order.append(k)

    # 收集块内原注释/空行，保留到 rebuilt 尾部（不丢注释）
    comments = []
    for j in range(env_idx + 1, end):
        if lines[j].lstrip().startswith("#") or lines[j].strip() == "":
            comments.append(lines[j])

    # 重建 env 块
    rebuilt = ["env:"] + [f"  {k}: {kv[k]}" for k in new_order]
    rebuilt.extend(comments)

    new_lines = lines[:env_idx] + rebuilt + lines[end:]
    if new_lines != lines:
        changed = True
    return changed, new_lines

def process_one(p: Path) -> bool:
    src = p.read_text(encoding="utf-8")
    nl = detect_nl(src)
    lines = src.split(nl)
    changed, out_lines = normalize_env(lines, nl)
    if not changed:
        return False
    p.with_suffix(p.suffix + ".bak").write_text(src, encoding="utf-8")
    p.write_text(nl.join(out_lines), encoding="utf-8")
    return True

def update_common_rs_admin_url(repo_root: Path) -> bool:
    """在 src/common.rs 中将硬编码的 https://admin.rustdesk.com 改为读取环境变量。

    替换：
      "https://admin.rustdesk.com".to_owned()
    为：
      match option_env!("API_SERVERS") { Some(s) => s.to_owned(), None => "https://admin.rustdesk.com".to_owned(), }

    若已替换过（检测到 option_env!("API_SERVERS")），则不做修改。
    """
    target = repo_root / "src" / "common.rs"
    if not target.exists():
        print(f"skip: {target} not found")
        return False
    text = target.read_text(encoding="utf-8")
    if 'option_env!("API_SERVERS")' in text:
        return False
    old = '"https://admin.rustdesk.com".to_owned()'
    new = 'match option_env!("API_SERVERS") { Some(s) => s.to_owned(), None => "https://admin.rustdesk.com".to_owned(), }'
    if old not in text:
        # 兼容无 .to_owned() 形式
        old2 = '"https://admin.rustdesk.com".into()'
        if old2 in text:
            text_new = text.replace(old2, new)
        else:
            # 未找到目标字面量
            print("No hardcoded admin url found to replace in src/common.rs")
            return False
    else:
        text_new = text.replace(old, new)
    target.with_suffix(target.suffix + ".bak").write_text(text, encoding="utf-8")
    target.write_text(text_new, encoding="utf-8")
    return True

def main():
    if not WF_DIR.exists():
        print(f"skip: {WF_DIR} not found")
        return
    changed = []
    files = sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))
    for f in files:
        if process_one(f):
            changed.append(f.relative_to(REPO_ROOT).as_posix())
    if changed:
        print("Updated workflows:")
        for f in changed:
            print(" -", f)
    else:
        print("No workflow changes needed.")
    print(f"Total updated: {len(changed)}")

    # 更新 src/common.rs 中的 admin 站点配置为可由环境变量覆盖
    if update_common_rs_admin_url(REPO_ROOT):
        code, out, err = run_cmd(["git", "add", "src/common.rs"], REPO_ROOT)
        if code == 0:
            print("Staged change: src/common.rs")
        else:
            print(f"git add src/common.rs failed: {err or out}")

    # 一键切换 git 子模块到新地址
    switched = switch_hbb_common_submodule(REPO_ROOT, NEW_SUBMODULE_URL)
    if not switched:
        print("Submodule switch was not completed. Please check errors above.")
    else:
        print("Submodule switched successfully.")

    # 自动更新并暂存父仓库中的子模块指针与 .gitmodules
    sub_path = parse_gitmodules_for_hbb_common(REPO_ROOT) or "libs/hbb_common"
    if update_and_stage_submodule_pointer(REPO_ROOT, sub_path):
        print(f"Staged submodule pointer and .gitmodules for {sub_path}.")
    else:
        print(f"Failed to update/stage submodule pointer for {sub_path}.")

def sync_master():
    """
    自动同步master分支
    """
    sync_master_file = WF_DIR / "sync_master.yml"
    if not sync_master_file.exists():
        print(f"new file: {sync_master_file}")
        with open(sync_master_file, "w", encoding="utf-8") as f:
            content = """name: Sync branches with master

on:
  push:
    branches:
      - master

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v3
        with:
          fetch-depth: 0

      - name: Sync cloud branch
        run: |
          git checkout cloud
          git merge master --no-edit
          git push origin cloud

      - name: Sync home branch
        run: |
          git checkout home
          git merge master --no-edit
          git push origin home
"""
            f.write(content)
        


if __name__ == "__main__":
    main()
    sync_master()