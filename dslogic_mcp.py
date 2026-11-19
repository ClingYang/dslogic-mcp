# SPDX-License-Identifier: GPL-3.0-or-later
"""Local MCP tools for passive DSLogic U2Pro16 captures and 24C08 comparison."""

import argparse
import atexit
import json
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from compare_24c08_capture import compare_capture


ROOT = Path(__file__).resolve().parent
RATE = 2_000_000
MODEL = "DSLogic U2Pro16"


def open_analyzer(context):
    matches = [i for i, device in enumerate(context.list_devices()) if device.name == MODEL]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {MODEL}; found {len(matches)}. Check USB and release DSView.")
    return context.get_device(matches[0])


def save_waveform(result, output):
    import numpy as np

    channels = result.channel_indices
    if len(channels) < 2 or not result.raw_data or result.data_format != 0:
        raise RuntimeError("No usable DSLogic cross-channel samples")
    words = np.frombuffer(result.raw_data, dtype="<u8")
    if len(words) % len(channels):
        raise RuntimeError("Incomplete channel group in DSLogic sample stream")
    words = words.reshape(-1, len(channels))
    group_count = len(words)
    if not group_count:
        raise RuntimeError("Empty DSLogic capture")

    active_channels = []
    first = group_count
    last = 0
    for index, channel in enumerate(channels):
        line = words[:, index]
        change = (line != 0) & (line != np.iinfo(np.uint64).max)
        change[1:] |= line[1:] != line[:-1]
        locations = np.flatnonzero(change)
        if locations.size:
            active_channels.append(channel)
            first = min(first, int(locations[0]))
            last = max(last, int(locations[-1]))

    if len(active_channels) >= 2:
        margin = RATE // 64 // 20
        first = max(0, first - margin)
        last = min(group_count, last + margin + 1)
    else:
        first, last = max(0, group_count - RATE // 64), group_count

    sample_count = (last - first) * 64
    block_groups = (1 << 24) // 64
    block_count = (last - first + block_groups - 1) // block_groups
    header = "\n".join([
        "[version]", "version = 3", "[header]", "driver = DSLogic", "device mode = 0",
        "capturefile = data", f"total samples = {sample_count}", f"total probes = {len(channels)}",
        f"total blocks = {block_count}", "samplerate = 2 MHz", "trigger pos = 0",
        *(f"probe{channel} = {channel}" for channel in channels), "",
    ])
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("header", header)
        archive.writestr("decoders", "[]")
        archive.writestr("session", '{"Version":3,"Device":"DSLogic","DeviceMode":0}')
        for index, channel in enumerate(channels):
            for block, offset in enumerate(range(first, last, block_groups)):
                end = min(last, offset + block_groups)
                archive.writestr(f"L-{channel}/{block}", words[offset:end, index].tobytes())
    return {"sample_rate_hz": RATE, "saved_samples": sample_count, "active_channels": active_channels}


def capture_worker(directory, duration):
    from pydsview import Config, DSContext, TriggerConfig

    with DSContext() as context:
        device = open_analyzer(context)
        device.set_config(Config.OPERATION_MODE, 1)
        device.set_config(Config.VTH, 0.9)
        device.samplerate = RATE
        for channel in device.channels:
            device.enable_channel(channel.index, True)
        device.sample_count = RATE * duration
        TriggerConfig().reset()
        capture = device.start_capture()
        (directory / "ready").touch()
        deadline = time.monotonic() + duration + 4
        while not capture.is_done() and time.monotonic() < deadline:
            if (directory / "stop").exists():
                break
            time.sleep(0.05)
        if not capture.is_done():
            capture.stop()
        result = capture.wait(timeout=7)

    metadata = save_waveform(result, directory / "capture.dsl")
    (directory / "result.json").write_text(json.dumps(metadata), encoding="utf-8")


@dataclass
class Job:
    id: str
    temporary: tempfile.TemporaryDirectory
    process: subprocess.Popen

    @property
    def directory(self):
        return Path(self.temporary.name)


def serve(data_dir):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("dslogic", instructions=(
        "Use start_capture, wait for status 'capturing', then prompt the operator to press Read. "
        "Call finish_capture after they confirm, and compare_24c08 against the saved baseline."
    ))
    data_dir.mkdir(parents=True, exist_ok=True)
    scratch = data_dir / ".work"
    scratch.mkdir(exist_ok=True)
    command = [sys.executable, "-B", str(Path(__file__).resolve())]
    hide = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    lock = threading.Lock()
    job = None
    last = None

    def status():
        nonlocal job, last
        if job is None:
            return last or {"status": "idle"}
        code = job.process.poll()
        if code is None:
            return {"status": "capturing" if (job.directory / "ready").exists() else "starting",
                    "id": job.id}
        if code:
            tail = (job.directory / "worker.log").read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            last = {"status": "failed", "id": job.id, "detail": "\n".join(tail)}
        else:
            metadata = json.loads((job.directory / "result.json").read_text(encoding="utf-8"))
            destination = data_dir / f"dslogic-{job.id}.dsl"
            (job.directory / "capture.dsl").rename(destination)
            last = {"status": "saved", "id": job.id, "file": destination.name, **metadata}
        job.temporary.cleanup()
        job = None
        return last

    def stop_on_exit():
        if job is not None and job.process.poll() is None:
            (job.directory / "stop").touch()
            try:
                job.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.process.kill()
                job.process.wait()
        if job is not None:
            status()

    atexit.register(stop_on_exit)

    @mcp.tool()
    def probe_device() -> dict:
        """Open and identify the physical U2Pro16, then release the USB interface."""
        with lock:
            current = status()
            if current["status"] in {"starting", "capturing"}:
                return current
            try:
                run = subprocess.run(
                    command + ["probe"], stdin=subprocess.DEVNULL, capture_output=True,
                    encoding="utf-8", errors="replace", timeout=7, creationflags=hide,
                )
            except subprocess.TimeoutExpired as error:
                logs = (error.stderr or b"").decode("utf-8", errors="replace")
                raise RuntimeError("DSLogic probe timed out\n" + "\n".join(logs.splitlines()[-8:])) from error
            if run.returncode:
                raise RuntimeError("\n".join(run.stderr.splitlines()[-12:]))
            return json.loads(run.stdout)

    @mcp.tool()
    def start_capture(duration_seconds: int = 45) -> dict:
        """Arm passive capture on all digital channels for 2–45 seconds."""
        nonlocal job, last
        if not 2 <= duration_seconds <= 45:
            raise ValueError("duration_seconds must be between 2 and 45")
        with lock:
            current = status()
            if current["status"] in {"starting", "capturing"}:
                return current
            identifier = uuid4().hex[:12]
            temporary = tempfile.TemporaryDirectory(prefix="dslogic-", dir=scratch)
            directory = Path(temporary.name)
            try:
                (directory / "owner.md").write_text(f"DSLogic MCP capture {identifier}, active.\n", encoding="utf-8")
                with (directory / "worker.log").open("wb") as log:
                    process = subprocess.Popen(
                        command + ["worker", str(directory), str(duration_seconds)],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=hide,
                    )
            except Exception:
                temporary.cleanup()
                raise
            job = Job(identifier, temporary, process)
            last = None
            deadline = time.monotonic() + 4
            while not (directory / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            return status()

    @mcp.tool()
    def capture_status() -> dict:
        """Check capture progress or retrieve the name of the saved waveform."""
        with lock:
            return status()

    @mcp.tool()
    def finish_capture() -> dict:
        """Stop the active capture, save the waveform, and release the analyzer."""
        with lock:
            if job is not None and job.process.poll() is None:
                (job.directory / "stop").touch()
                try:
                    job.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    return status()
            return status()

    @mcp.tool()
    def compare_24c08(
        waveform: str, baseline: str,
        scl: int | None = None, sda: int | None = None,
    ) -> dict:
        """Compare 24C08 data in a saved waveform with a 1024-byte backup in the capture directory."""
        paths = []
        for name in (waveform, baseline):
            if not name or Path(name).name != name or Path(name).anchor:
                raise ValueError("Use plain file names in the capture directory")
            path = (data_dir / name).resolve(strict=True)
            if path.parent != data_dir or not path.is_file():
                raise ValueError("The file must be inside the capture directory")
            paths.append(path)
        report, _ = compare_capture(paths[0], paths[1].read_bytes(), scl, sda)
        return report

    mcp.run(transport="stdio")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    daemon = modes.add_parser("serve")
    daemon.add_argument("--data-dir", type=Path, default=ROOT / "captures")
    modes.add_parser("probe")
    worker = modes.add_parser("worker")
    worker.add_argument("directory", type=Path)
    worker.add_argument("duration", type=int)
    args = parser.parse_args()

    if args.mode == "serve":
        serve(args.data_dir.resolve())
    else:
        if args.mode == "worker":
            capture_worker(args.directory, args.duration)
        else:
            from pydsview import DSContext

            with DSContext() as context:
                device = open_analyzer(context)
                print(json.dumps({"model": MODEL, "digital_channels": [ch.index for ch in device.channels]}))


if __name__ == "__main__":
    main()
