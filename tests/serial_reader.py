# SPDX-License-Identifier: Apache-2.0

"""Standard-library serial observation support for external tests."""

from __future__ import annotations

import base64
import json
import selectors
import shlex
import subprocess

SERIAL_READER_SOURCE = r'''import base64,json,os,re,select,sys,termios,time
device,baud_text,data_bits_text,parity,stop_bits_text,flow,pattern_text,timeout_text=sys.argv[1:]
baud=int(baud_text); data_bits=int(data_bits_text); stop_bits=int(stop_bits_text)
timeout=float(timeout_text)
def emit(kind,**fields):
 print(json.dumps({'type':kind,**fields},separators=(',',':')),flush=True)
fd=None
try:
 fd=os.open(device,os.O_RDONLY|os.O_NOCTTY|os.O_NONBLOCK)
 attrs=termios.tcgetattr(fd)
 speed=getattr(termios,'B'+str(baud),None)
 if speed is None: raise ValueError('unsupported baud rate '+str(baud))
 attrs[0]=0; attrs[1]=0; attrs[3]=0
 attrs[2]=(attrs[2]&~(termios.PARENB|termios.PARODD|termios.CSTOPB|termios.CSIZE)|termios.CLOCAL|termios.CREAD)
 attrs[2]|={5:termios.CS5,6:termios.CS6,7:termios.CS7,8:termios.CS8}[data_bits]
 if parity=='even': attrs[2]|=termios.PARENB
 elif parity=='odd': attrs[2]|=termios.PARENB|termios.PARODD
 if stop_bits==2: attrs[2]|=termios.CSTOPB
 if flow=='hardware': attrs[2]|=getattr(termios,'CRTSCTS',0)
 if flow=='software': attrs[0]|=termios.IXON|termios.IXOFF
 attrs[4]=speed;attrs[5]=speed
 attrs[6][termios.VMIN]=0;attrs[6][termios.VTIME]=1
 termios.tcsetattr(fd,termios.TCSANOW,attrs); termios.tcflush(fd,termios.TCIFLUSH)
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


def read_event(process: subprocess.Popen[str], timeout: float) -> dict[str, object]:
    """Read one JSON event from the remote reader."""
    if process.stdout is None:
        raise AssertionError("remote serial reader has no stdout")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    if not selector.select(timeout):
        raise AssertionError("remote serial reader did not respond before timeout")
    line = process.stdout.readline()
    if not line:
        diagnostic = b"" if process.stderr is None else process.stderr.read()
        raise AssertionError("remote serial reader exited: " + diagnostic.decode(errors="replace"))
    return json.loads(line)


def stop_reader(process: subprocess.Popen[str]) -> None:
    """Terminate a reader and close all of its pipes."""
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


def remote_serial_reader_command(
    device: str,
    baud: int,
    pattern: str,
    timeout: float,
    *,
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: int = 1,
    flow_control: str = "none",
) -> str:
    """Return a shell-safe remote ``python3 -c`` serial-reader command."""
    encoded = base64.b64encode(SERIAL_READER_SOURCE.encode()).decode("ascii")
    args = (
        device,
        str(baud),
        str(data_bits),
        parity,
        str(stop_bits),
        flow_control,
        pattern,
        str(timeout),
    )
    return " ".join(
        (
            "python3",
            "-c",
            shlex.quote(f"import base64;exec(base64.b64decode('{encoded}'))"),
            *(shlex.quote(value) for value in args),
        )
    )
