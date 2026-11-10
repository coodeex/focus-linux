#!/usr/bin/env python3
"""Release the tested Focus2 worktree while carrying over the live timer."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

DEV = Path(__file__).resolve().parents[1]
PROD = DEV.with_name("focus-linux")
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCKET = RUNTIME / "focus2-control.sock"
RESTORE = RUNTIME / "focus2-restore.json"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def command(message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect(str(SOCKET))
        client.sendall(message.encode())
        return client.recv(4096).decode().strip()


def snapshot():
    response = command("snapshot")
    try:
        data = json.loads(response)
        if data["mode"] in ("focus", "break"):
            return data
    except (ValueError, KeyError, TypeError):
        pass
    # The first release upgrades a timer that only understands human status.
    response = command("status")
    match = re.search(r"Status: (Focusing|Break) — (\d+):(\d+) remaining", response)
    if not match:
        raise RuntimeError(f"Cannot capture live timer: {response}")
    mode = "focus" if match[1] == "Focusing" else "break"
    remaining = int(match[2]) * 60 + int(match[3])
    data = {"version": 1, "mode": mode, "remaining_seconds": remaining,
            "captured_at": time.time()}
    if mode == "focus":
        data["cycle_minutes"] = 30
        data["elapsed_seconds"] = max(0, 30 * 60 - remaining)
    else:
        data["elapsed_seconds"] = max(0, 5 * 60 - remaining)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tested", action="store_true", help="release tested, committed changes")
    parser.add_argument("--dry-run", action="store_true", help="check release readiness")
    args = parser.parse_args()
    if not args.tested and not args.dry_run:
        parser.error("pass --tested after testing, or --dry-run")
    if git(DEV, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Development worktree has uncommitted tracked changes")
    if git(PROD, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Production worktree has uncommitted tracked changes")
    if not SOCKET.exists():
        raise RuntimeError("Production timer socket is absent")
    before = snapshot()
    print("Live timer:", before["mode"], round(before["remaining_seconds"]), "seconds")
    print("Development commit:", git(DEV, "rev-parse", "--short", "HEAD"))
    if args.dry_run:
        return
    subprocess.run(["git", "-C", str(DEV), "push", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(PROD), "fetch", "origin", "main"], check=True)
    target = git(PROD, "rev-parse", "origin/main")
    current = git(PROD, "rev-parse", "HEAD")
    if target == current:
        print("Production already matches the release commit.")
        return
    if git(PROD, "merge-base", current, target) != current:
        raise RuntimeError("Production cannot fast-forward to main")
    before = snapshot()  # Capture immediately before the file swap.
    temporary = RESTORE.with_suffix(".tmp")
    temporary.write_text(json.dumps(before))
    temporary.replace(RESTORE)
    try:
        subprocess.run(["git", "-C", str(PROD), "merge", "--ff-only", "origin/main"], check=True)
    except Exception:
        RESTORE.unlink(missing_ok=True)
        raise
    deadline = time.monotonic() + 12
    after = None
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            after = json.loads(command("snapshot"))
            if after.get("mode") == before["mode"] and not RESTORE.exists():
                break
        except (OSError, ValueError):
            pass
    else:
        raise RuntimeError("Release applied, but timer restoration could not be verified")
    expected = max(0, before["remaining_seconds"] - (time.time() - before["captured_at"]))
    if abs(after["remaining_seconds"] - expected) > 5:
        raise RuntimeError(f"Timer changed unexpectedly: expected {expected:.1f}s, got {after['remaining_seconds']:.1f}s")
    print("Restored:", after["mode"], round(after["remaining_seconds"]), "seconds")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        sys.exit(1)
