import os
import shutil
import stat
import glob

USER_HOME = os.path.expanduser("~")
CODEX_DIR = os.path.join(USER_HOME, ".codex")

def rmtree_readonly(path):
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
            except Exception:
                pass
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IWRITE)
            except Exception:
                pass
    shutil.rmtree(path)

for item in os.listdir(CODEX_DIR):
    if item in ("config.toml", "plugins", "cache", ".tmp", "process_manager"):
        continue
    p = os.path.join(CODEX_DIR, item)
    if os.path.isdir(p):
        rmtree_readonly(p)
    else:
        try:
            os.remove(p)
        except:
            pass

hesap2_dir = os.path.join(USER_HOME, ".codex_hesap2")
if os.path.exists(hesap2_dir):
    for item in os.listdir(hesap2_dir):
        if item.startswith("roaming_session"):
            continue
        src = os.path.join(hesap2_dir, item)
        dst = os.path.join(CODEX_DIR, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    print("Recovered files from .codex_hesap2 to .codex")

import re
cfg = os.path.join(CODEX_DIR, "config.toml")
if os.path.exists(cfg):
    with open(cfg, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace(".codex_hesap2", ".codex")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(content)
    print("Fixed config.toml")

print("Done!")
