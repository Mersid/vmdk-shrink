"""Host process streaming with deadlines independent of output activity."""

from __future__ import annotations

import codecs
import os
import queue
import subprocess
import threading
import time


def stream_process(
    argv: list[str], *, timeout: float = 7200.0, chunk_cb: object = None
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    stdout = proc.stdout
    messages: queue.Queue[bytes | BaseException | None] = queue.Queue()

    def read() -> None:
        try:
            while chunk := os.read(stdout.fileno(), 65536):
                messages.put(chunk)
        except (OSError, ValueError) as exc:
            messages.put(exc)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
    output: list[str] = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout, output="".join(output))
            try:
                item = messages.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            text = decoder.decode(item or b"", final=item is None)
            if text:
                output.append(text)
                if callable(chunk_cb):
                    chunk_cb(text)
            if item is None:
                break
        rc = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, rc, "".join(output), "")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(timeout=1)
        proc.stdout.close()
