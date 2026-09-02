import os, sys, glob, json, time, subprocess

USER_HOME = os.path.expanduser("~")
CODEX_DIR = os.path.join(USER_HOME, ".codex")
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA", "")
REPORT = r"C:\Users\ferhat\Desktop\CodexTools\diagnose_report.txt"

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore")

def snapshot(label, paths):
    lines = [f"\n=== SNAPSHOT: {label} ==="]
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        mtime = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(fp)))
                        lines.append(f"  FILE [{mtime}] {sz:>8} B  {fp}")
                    except:
                        pass
        elif os.path.isfile(p):
            try:
                sz = os.path.getsize(p)
                mtime = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(p)))
                lines.append(f"  FILE [{mtime}] {sz:>8} B  {p}")
            except:
                pass
        else:
            lines.append(f"  MISSING: {p}")
    return "\n".join(lines)

def find_roaming():
    pat = os.path.join(LOCAL_APP_DATA, "Packages", "OpenAI.Codex_*", "LocalCache", "Roaming", "Codex")
    dirs = glob.glob(pat)
    if not dirs:
        return []
    rd = dirs[0]
    return [rd, os.path.join(rd,"web","Codex"), os.path.join(rd,"web","Codex","codex-browser-app"), os.path.join(rd,"web","Codex","Default")]

def kill_codex():
    run(["powershell","-NoProfile","-Command","Get-Process -Name 'Codex','ChatGPT','node_repl' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"])
    time.sleep(3)

def start_codex():
    subprocess.Popen(["explorer.exe","shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"],close_fds=True)

def read_auth():
    p=os.path.join(CODEX_DIR,"auth.json")
    if os.path.exists(p):
        try:
            return open(p,"r",encoding="utf-8").read(2000)
        except Exception as e:
            return f"ERROR: {e}"
    return "auth.json YOK"

def read_state():
    p=os.path.join(CODEX_DIR,".codex-global-state.json")
    if os.path.exists(p):
        try:
            return open(p,"r",encoding="utf-8").read(2000)
        except:
            return "ERROR"
    return ".codex-global-state.json YOK"

report=[]
report.append("="*60)
report.append("CODEX OTURUM TANISI - " + time.strftime("%Y-%m-%d %H:%M:%S"))
report.append("="*60)

watch_dirs=[CODEX_DIR]+find_roaming()
report.append("Izlenen:\n"+"\n".join(f"  {d}" for d in watch_dirs))

report.append(snapshot("BASLANGIC",watch_dirs))

print("Codex kapatiliyor...")
kill_codex()
report.append(snapshot("CODEX KAPATILDI",watch_dirs))

print("Codex baslatiliyor, 15 sn bekliyorum...")
start_codex()
time.sleep(15)

report.append(snapshot("CODEX ACIK (15sn)",watch_dirs))
report.append("\n=== AUTH.JSON (CODEX ACIK) ===")
report.append(read_auth())
report.append("\n=== GLOBAL-STATE (CODEX ACIK) ===")
report.append(read_state())

print("Codex kapatiliyor, son okuma...")
kill_codex()
time.sleep(3)
report.append(snapshot("SON DURUM",watch_dirs))
report.append("\n=== AUTH.JSON (KAPALI) ===")
report.append(read_auth())
report.append("\n=== CMDKEY LIST ===")
report.append(run(["cmdkey","/list"]).stdout)

with open(REPORT,"w",encoding="utf-8") as f:
    f.write("\n".join(report))

print(f"Rapor: {REPORT}")
