#!/usr/bin/env python3
"""
Unified watcher for both tracker types (absolutely right + prompt words).
Runs both watchers as separate subprocesses with health monitoring and auto-restart.
"""
import sys
import os
import subprocess
import signal
import time
from pathlib import Path

# Get the scripts directory
SCRIPTS_DIR = Path(__file__).parent.resolve()


class WatcherProcess:
    """Manages a watcher subprocess with auto-restart capability."""

    def __init__(self, name: str, script_path: Path, working_dir: Path = None):
        self.name = name
        self.script_path = script_path
        self.working_dir = working_dir or script_path.parent
        self.process: subprocess.Popen = None
        self.restart_count = 0
        self.max_restarts = 5
        self.restart_window = 60  # seconds
        self.restart_times = []

    def start(self):
        """Start the watcher subprocess."""
        if self.process and self.process.poll() is None:
            print(f"[{self.name}] Already running (PID {self.process.pid})")
            return

        # Build command with any passed arguments (like --secret)
        cmd = [sys.executable, str(self.script_path)]

        # Pass through relevant arguments
        for i, arg in enumerate(sys.argv[1:], 1):
            if arg in ("--secret", "--upload") or (
                i > 1 and sys.argv[i-1] in ("--secret", "--upload")
            ):
                cmd.append(arg)

        print(f"[{self.name}] Starting: {' '.join(cmd)}")
        print(f"[{self.name}] Working directory: {self.working_dir}")

        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.working_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )
        print(f"[{self.name}] Started with PID {self.process.pid}")

    def is_alive(self) -> bool:
        """Check if the subprocess is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def check_and_restart(self) -> bool:
        """Check if process died and restart if needed. Returns True if restarted."""
        if self.is_alive():
            return False

        # Process died - check restart limits
        exit_code = self.process.returncode
        print(f"[{self.name}] Process exited with code {exit_code}")

        # Clean up old restart times outside the window
        current_time = time.time()
        self.restart_times = [t for t in self.restart_times if current_time - t < self.restart_window]

        if len(self.restart_times) >= self.max_restarts:
            print(f"[{self.name}] Too many restarts ({self.max_restarts}) in {self.restart_window}s window. Giving up.")
            return False

        self.restart_times.append(current_time)
        self.restart_count += 1
        print(f"[{self.name}] Restarting... (attempt {self.restart_count})")
        self.start()
        return True

    def read_output(self) -> list:
        """Non-blocking read of any available output lines."""
        lines = []
        if self.process and self.process.stdout:
            # Use select or non-blocking read would be better, but for simplicity
            # we'll rely on line buffering and the poll in the main loop
            import select
            while True:
                # Check if there's data available to read
                ready, _, _ = select.select([self.process.stdout], [], [], 0)
                if not ready:
                    break
                line = self.process.stdout.readline()
                if line:
                    lines.append(line.rstrip())
                else:
                    break
        return lines

    def stop(self):
        """Stop the subprocess gracefully."""
        if self.process and self.process.poll() is None:
            print(f"[{self.name}] Stopping (PID {self.process.pid})...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"[{self.name}] Force killing...")
                self.process.kill()
                self.process.wait()
            print(f"[{self.name}] Stopped")


def main():
    """Main unified watcher orchestrator."""
    print("=" * 60)
    print("UNIFIED WATCHER - Running both trackers as subprocesses")
    print("=" * 60)
    print()

    # Define watchers with their script paths
    watchers = [
        WatcherProcess(
            name="ABSOLUTELY_RIGHT",
            script_path=SCRIPTS_DIR / "watcher.py",
            working_dir=SCRIPTS_DIR,
        ),
        WatcherProcess(
            name="PROMPT_WORDS",
            script_path=SCRIPTS_DIR / "prompt_words" / "watcher.py",
            working_dir=SCRIPTS_DIR / "prompt_words",
        ),
    ]

    # Handle graceful shutdown
    shutdown_requested = False

    def signal_handler(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\n" + "-" * 60)
        print("Shutdown requested, stopping all watchers...")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start all watchers
    for watcher in watchers:
        watcher.start()
        time.sleep(0.5)  # Slight delay to avoid output mixing

    print()
    print("All watchers started. Press Ctrl+C to stop.")
    print("-" * 60)
    print()

    # Main monitoring loop
    try:
        while not shutdown_requested:
            # Read and display output from each watcher
            for watcher in watchers:
                for line in watcher.read_output():
                    print(f"[{watcher.name}] {line}")

            # Check health and restart if needed
            for watcher in watchers:
                watcher.check_and_restart()

            # Small sleep to avoid busy waiting
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        # Stop all watchers
        print()
        for watcher in watchers:
            watcher.stop()
        print("-" * 60)
        print("All watchers stopped.")


if __name__ == "__main__":
    main()
