# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.support import ROOT

SERIAL_READER = r'''import base64,json,os,re,select,sys,termios,time,tty
device,baud_text,pattern_text,timeout_text=sys.argv[1:]
baud=int(baud_text); timeout=float(timeout_text)
def emit(kind,**fields):
 print(json.dumps({'type':kind,**fields},separators=(',',':')),flush=True)
fd=None
try:
 fd=os.open(device,os.O_RDONLY|os.O_NOCTTY|os.O_NONBLOCK)
 tty.setraw(fd)
 attrs=termios.tcgetattr(fd)
 speed=getattr(termios,'B'+str(baud),None)
 if speed is None: raise ValueError('unsupported baud rate '+str(baud))
 attrs[2]=(attrs[2]&~(termios.PARENB|termios.CSTOPB|termios.CSIZE))|termios.CS8|termios.CLOCAL|termios.CREAD
 attrs[4]=speed;attrs[5]=speed
 termios.tcsetattr(fd,termios.TCSANOW,attrs)
 termios.tcflush(fd,termios.TCIFLUSH)
 emit('READY')
 while True:
  ready,_,_=select.select([fd,sys.stdin.buffer],[],[])
  if fd in ready:
   try: os.read(fd,65536)
   except BlockingIOError: pass
  if sys.stdin.buffer in ready:
   if sys.stdin.buffer.readline().strip()!=b'ARM': raise RuntimeError('reader was not armed')
   break
 deadline=time.monotonic()+timeout; data=bytearray(); pattern=re.compile(pattern_text)
 while time.monotonic()<deadline:
  ready,_,_=select.select([fd],[],[],min(0.2,max(0,deadline-time.monotonic())))
  if fd in ready:
   try: data.extend(os.read(fd,65536))
   except BlockingIOError: pass
   if pattern.search(data.decode('utf-8','replace')):
    emit('MATCH',data=base64.b64encode(data).decode('ascii'));sys.exit(0)
 emit('TIMEOUT',data=base64.b64encode(data).decode('ascii'));sys.exit(2)
except Exception as exc:
 emit('ERROR',message=str(exc));sys.exit(3)
finally:
 if fd is not None: os.close(fd)
'''


def _read_event(process, timeout):
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    if not selector.select(timeout):
        raise AssertionError("remote serial reader did not respond before timeout")
    line = process.stdout.readline()
    if not line:
        diagnostic = b"" if process.stderr is None else process.stderr.read()
        raise AssertionError("remote serial reader exited: " + diagnostic.decode(errors="replace"))
    return json.loads(line)


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class RealOpenOcdFlashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = os.environ.get("ZRO_REAL_FLASH_FIXTURES")
        if not fixture_path:
            raise unittest.SkipTest("ZRO_REAL_FLASH_FIXTURES is not configured")
        try:
            document = json.loads(Path(fixture_path).read_text())
            fixtures = list(document["fixtures"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"invalid real-flash fixture file {fixture_path}: {error}"
            ) from error
        if not fixtures:
            raise unittest.SkipTest("real-flash fixture file configures no fixtures")
        cls.fixtures = fixtures

    def test_configured_targets_flash_and_emit_fresh_serial_output(self):
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.get("id")):
                self._run_fixture(fixture)

    def _run_fixture(self, fixture):
        required = (
            "ssh_command",
            "host",
            "build_dir",
            "config_path",
            "serial_device",
            "serial_baud",
            "expected_pattern",
            "serial_timeout",
        )
        missing = [key for key in required if key not in fixture]
        if missing:
            self.fail(f"fixture {fixture.get('id')} is missing: {', '.join(missing)}")
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        encoded = base64.b64encode(SERIAL_READER.encode()).decode("ascii")
        remote_command = " ".join(
            (
                "python3",
                "-c",
                shlex.quote(f"import base64;exec(base64.b64decode('{encoded}'))"),
                shlex.quote(str(fixture["serial_device"])),
                shlex.quote(str(fixture["serial_baud"])),
                shlex.quote(str(fixture["expected_pattern"])),
                shlex.quote(str(fixture["serial_timeout"])),
            )
        )
        reader = ssh.popen(fixture["host"], remote_command, "-o", "ControlMaster=no")
        try:
            ready = _read_event(reader, 15)
            self.assertEqual(ready["type"], "READY", ready)
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()

            west = fixture.get("west") or shutil.which("west")
            if not west:
                self.fail("fixture has no west executable and west is not on PATH")
            command = [
                str(west),
                "flash",
                "-d",
                str(fixture["build_dir"]),
                "-r",
                "remote-openocd",
                "--no-rebuild",
            ]
            runner_args = fixture.get("runner_args", [])
            if runner_args:
                command.extend(("--", *map(str, runner_args)))
            environment = os.environ.copy()
            environment.pop("ZEPHYR_REMOTE_OPENOCD_RECORD", None)
            environment.update(
                {
                    "EXTRA_ZEPHYR_MODULES": str(ROOT),
                    "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture["config_path"]),
                }
            )
            environment.update(
                {str(key): str(value) for key, value in fixture.get("environment", {}).items()}
            )
            flash = subprocess.run(
                command,
                cwd=fixture.get("workspace"),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=float(fixture["serial_timeout"]) + 180,
            )
            event = _read_event(reader, float(fixture["serial_timeout"]) + 2)
            captured = base64.b64decode(event.get("data", "")).decode("utf-8", "replace")
            self.assertEqual(flash.returncode, 0, flash.stdout + "\nserial:\n" + captured)
            self.assertEqual(event["type"], "MATCH", f"serial oracle failed: {event}\n{captured}")
            session = re.search(
                r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)", flash.stdout
            )
            self.assertIsNotNone(session, flash.stdout)
            assert session is not None
            address = ipaddress.ip_address(session.group(3))
            self.assertIn(address, ipaddress.ip_network("127.64.0.0/10"))
            if fixture.get("assert_openocd_bindto"):
                self.assertIn(f"bindto name: {address}", flash.stdout)
            cleanup = ssh.run(
                fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
            )
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr.decode("utf-8", "replace"))
            for pattern in fixture.get("expected_flash_patterns", []):
                self.assertRegex(flash.stdout, pattern)
            self.assertEqual(reader.wait(timeout=5), 0)
        finally:
            _stop(reader)


if __name__ == "__main__":
    unittest.main()
