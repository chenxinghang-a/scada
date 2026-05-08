"""Git push helper - commit and push to GitHub"""
from typing import Any
import subprocess
import sys
import os

os.chdir(r"c:\Users\cxx\WorkBuddy\Claw\industrial_scada")

GIT = r"C:\Users\cxx\WorkBuddy\Claw\tools\mingit\cmd\git.exe"

def run(args):
    result = subprocess.run([GIT] + args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode

# Status
print("=== Git Status ===")
run(["status"])

# Diff summary
print("\n=== Changed Files ===")
run(["diff", "--stat"])
run(["diff", "--cached", "--stat"])

# Add all
print("\n=== Staging All ===")
run(["add", "-A"])

# Commit
print("\n=== Committing ===")
msg = sys.argv[1] if len(sys.argv) > 1 else "fix: alarm_output - buzzer pulse, manual toggle, flash thread safety"
rc = run(["commit", "-m", msg])

if rc != 0:
    print("Nothing to commit or commit failed")
    sys.exit(0)

# Push
print("\n=== Pushing ===")
run(["push", "origin", "main"])
print("\nDone!")
