from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree as ET

from coordinator.bundle import timestamp
from coordinator.store import ROOT, Store

TASK_NAME = "Moomoo-Coordinator-DryRun"
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_xml(*, python, database, env_file, user_sid, expires_at):
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", user_sid):
        raise ValueError("invalid Windows user SID")
    task = ET.Element("Task", version="1.2", xmlns=NAMESPACE)
    registration = ET.SubElement(task, "RegistrationInfo")
    ET.SubElement(registration, "Description").text = "24-hour local dry-run coordinator; no trading, Guard messages or renewal."
    trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "LogonTrigger")
    ET.SubElement(trigger, "EndBoundary").text = timestamp(expires_at).isoformat()
    ET.SubElement(trigger, "Enabled").text = "true"
    ET.SubElement(trigger, "UserId").text = user_sid
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="Operator")
    ET.SubElement(principal, "UserId").text = user_sid
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                        ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"),
                        ("StartWhenAvailable", "false"), ("Enabled", "true"),
                        ("WakeToRun", "false"), ("ExecutionTimeLimit", "PT25H")):
        ET.SubElement(settings, name).text = value
    action = ET.SubElement(ET.SubElement(task, "Actions", Context="Operator"), "Exec")
    ET.SubElement(action, "Command").text = str(python)
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline([
        "-u", "-m", "coordinator", "--db", str(database), "run", "--env-file", str(env_file),
        "--interval", "120", "--log-file", str(Path(database).parent / "worker.log")])
    ET.SubElement(action, "WorkingDirectory").text = str(ROOT)
    return ET.tostring(task, encoding="unicode")


def install(database: Path, env_file: Path):
    status = Store(database).status()
    now = datetime.now(timezone.utc)
    if (status["mode"] != "DRY_RUN" or status["trading_enabled"] or not status["guard_halted"]
            or status["revoked_at"] or not timestamp(status["starts_at"]) <= now < timestamp(status["expires_at"])):
        raise ValueError("rehearsal is inactive, revoked or unsafe; no task changed")
    if sys.platform != "win32":
        raise ValueError("Windows Task Scheduler is required")
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file() or not env_file.is_file():
        raise ValueError("Python windowless executable or explicit server env file is missing")
    identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                              check=True, capture_output=True, text=True, timeout=20)
    user_sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
    xml = task_xml(python=python, database=database, env_file=env_file,
                   user_sid=user_sid, expires_at=status["expires_at"])
    with NamedTemporaryFile(mode="w", encoding="utf-16", suffix=".xml", prefix="coordinator-task-",
                            dir=database.parent, delete=False) as handle:
        handle.write('<?xml version="1.0" encoding="UTF-16"?>\n' + xml)
        path = Path(handle.name)
    try:
        created = subprocess.run(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(path)],
                                 input="", capture_output=True, text=True, timeout=30)
        if created.returncode:
            print(created.stdout.strip())
            print(created.stderr.strip())
            return created.returncode
    finally:
        path.unlink()
    started = subprocess.run(["schtasks.exe", "/Run", "/TN", TASK_NAME],
                             capture_output=True, text=True, timeout=30)
    print(json.dumps({"task_name": TASK_NAME, "registered": True, "start_requested": started.returncode == 0,
                      "expires_at": status["expires_at"], "trading_enabled": False, "guard_halted": True,
                      "log_file": str(database.parent / "worker.log")}))
    if started.returncode:
        print(started.stderr.strip())
    return started.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install a current-user Windows task for an existing dry-run ledger")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args(argv)
    return install(args.db.resolve(), args.env_file.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
