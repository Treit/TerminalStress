"""
Main runner for the Windows Terminal monkey stress tester.

Orchestrates the action loop, watchdog monitoring, and logging.
"""

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from pywinauto.application import Application

from .actions import build_action_catalog, pick_action, FocusError, set_target_hwnd, FocusError
from .watchdog import Watchdog, find_wt_process

# Logging setup
LOG_DIR = Path(__file__).parent.parent / "monkey_logs"


def setup_logging(log_dir: Path, instance_id: int | None = None) -> Path:
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_inst{instance_id}" if instance_id is not None else ""
    log_file = log_dir / f"monkey_{timestamp}{suffix}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def connect_to_wt() -> tuple[Application, any, int]:
    """
    Connect to a running Windows Terminal instance.
    Returns (app, window, pid).
    """
    proc = find_wt_process()
    if not proc:
        raise RuntimeError(
            "Windows Terminal is not running. Please start it first."
        )

    pid = proc.pid
    try:
        app = Application(backend="uia").connect(
            class_name="CASCADIA_HOSTING_WINDOW_CLASS", timeout=10
        )
    except Exception as e:
        # Fallback: connect by process ID
        logger = logging.getLogger("monkey")
        logger.warning(f"connect by class_name failed ({e}), trying by PID {pid}")
        app = Application(backend="uia").connect(process=pid, timeout=10)
    win = app.top_window().wrapper_object()
    return app, win, pid


def launch_wt() -> tuple[Application, any, int]:
    """
    Launch a new Windows Terminal instance and connect to it.
    Returns (app, window, pid).
    """
    import subprocess
    subprocess.Popen(["wt.exe"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    # WT launcher stub exits quickly; the real process takes a moment to start
    for _ in range(10):
        time.sleep(1)
        try:
            return connect_to_wt()
        except RuntimeError:
            continue
    raise RuntimeError("Failed to launch and connect to Windows Terminal.")


def run_monkey(
    duration_seconds: int = 300,
    seed: int | None = None,
    health_check_interval: int = 10,
    auto_launch: bool = False,
    memory_threshold_mb: float = 2048.0,
):
    """
    Main monkey test loop.

    Args:
        duration_seconds: How long to run (0 = forever).
        seed: Random seed for reproducibility.
        health_check_interval: Seconds between health snapshots.
        auto_launch: Whether to launch WT if not running.
        memory_threshold_mb: Memory threshold for leak warnings.
    """
    logger = logging.getLogger("monkey")

    if seed is not None:
        random.seed(seed)
        logger.info(f"Random seed: {seed}")
    else:
        seed = random.randint(0, 2**32 - 1)
        random.seed(seed)
        logger.info(f"Random seed (auto): {seed}")

    # Connect or launch
    try:
        app, win, pid = connect_to_wt()
    except RuntimeError:
        if auto_launch:
            logger.info("Launching Windows Terminal...")
            app, win, pid = launch_wt()
        else:
            raise

    logger.info(f"Connected to Windows Terminal (PID {pid})")

    # Initialize watchdog
    watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
    watchdog.set_hwnd(win.handle)
    set_target_hwnd(win.handle)

    # Take initial snapshot
    snap = watchdog.take_snapshot()
    logger.info(
        f"Initial health: RSS={snap.memory_rss_mb:.1f}MB, "
        f"responding={snap.is_responding}"
    )

    # Build action catalog
    catalog = build_action_catalog()
    total_weight = sum(a.weight for a in catalog)
    logger.info(
        f"Action catalog: {len(catalog)} actions, total weight={total_weight}"
    )

    # Stats
    action_counts: dict[str, int] = {}
    action_errors: dict[str, int] = {}
    total_actions = 0
    start_time = time.time()
    last_health_check = start_time

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        logger.info(f"Received signal {signum}, shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info(
        f"Starting monkey test (duration={'forever' if duration_seconds == 0 else f'{duration_seconds}s'})"
    )
    logger.info("=" * 72)

    try:
        while running:
            elapsed = time.time() - start_time
            if duration_seconds > 0 and elapsed >= duration_seconds:
                logger.info("Duration reached, stopping.")
                break

            # Pick and execute a random action
            action = pick_action(catalog)
            total_actions += 1
            action_counts[action.name] = action_counts.get(action.name, 0) + 1

            try:
                logger.info(f"[{total_actions}] {action.name}")
                action.func(win)
            except FocusError:
                logger.debug(f"[{total_actions}] {action.name} skipped (WT not focused)")
                total_actions -= 1
                action_counts[action.name] = action_counts.get(action.name, 0) - 1
                _brief_sleep_ms = random.uniform(0.05, 0.2)
                time.sleep(_brief_sleep_ms)
                continue
            except Exception as e:
                action_errors[action.name] = action_errors.get(action.name, 0) + 1
                logger.warning(f"[{total_actions}] {action.name} FAILED: {e}")

                # Check if WT is still alive after an error
                if not watchdog.is_process_running():
                    exit_code = watchdog.get_exit_code()
                    if exit_code is not None and exit_code == 0:
                        logger.warning(
                            f"Windows Terminal exited normally (code 0) after action '{action.name}'. "
                            "This is likely the last tab/pane being closed, not a crash."
                        )
                    else:
                        logger.error(
                            f"Windows Terminal process CRASHED (exit code: {exit_code})!"
                        )
                        watchdog.state.crash_detected = True
                        break
                    # Try to reconnect or relaunch
                    logger.info("Attempting to reconnect to Windows Terminal...")
                    time.sleep(2)
                    try:
                        app, win, pid = connect_to_wt()
                        watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                        watchdog.set_hwnd(win.handle)
                        set_target_hwnd(win.handle)
                        logger.info(f"Reconnected to Windows Terminal (PID {pid})")
                    except RuntimeError:
                        if auto_launch:
                            logger.info("Launching new Windows Terminal instance...")
                            try:
                                app, win, pid = launch_wt()
                                watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                                watchdog.set_hwnd(win.handle)
                                set_target_hwnd(win.handle)
                                logger.info(f"Launched and connected to Windows Terminal (PID {pid})")
                            except Exception:
                                logger.error("Failed to launch Windows Terminal. Stopping.")
                                break
                        else:
                            logger.error("No Windows Terminal instance found. Stopping.")
                            break

            # Periodic health check
            now = time.time()
            if now - last_health_check >= health_check_interval:
                last_health_check = now

                # Check if WT is still running
                if not watchdog.is_process_running():
                    exit_code = watchdog.get_exit_code()
                    if exit_code is not None and exit_code == 0:
                        logger.warning("Windows Terminal exited normally (code 0) during health check.")
                    else:
                        logger.error(f"Windows Terminal process CRASHED (exit code: {exit_code})!")
                        break
                    logger.info("Attempting to reconnect to Windows Terminal...")
                    time.sleep(2)
                    try:
                        app, win, pid = connect_to_wt()
                        watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                        watchdog.set_hwnd(win.handle)
                        set_target_hwnd(win.handle)
                        logger.info(f"Reconnected to Windows Terminal (PID {pid})")
                        continue
                    except RuntimeError:
                        if auto_launch:
                            logger.info("Launching new Windows Terminal instance...")
                            try:
                                app, win, pid = launch_wt()
                                watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                                watchdog.set_hwnd(win.handle)
                                set_target_hwnd(win.handle)
                                logger.info(f"Launched and connected to Windows Terminal (PID {pid})")
                                continue
                            except Exception:
                                logger.error("Failed to launch Windows Terminal. Stopping.")
                                break
                        else:
                            logger.error("No Windows Terminal instance found. Stopping.")
                            break

                # Check if WT is responding
                snap = watchdog.take_snapshot()
                if not snap.is_responding:
                    logger.error(
                        f"Windows Terminal is NOT RESPONDING (hang detected)! "
                        f"Actions so far: {total_actions}"
                    )
                    # Continue to log a few more attempts to confirm
                    for i in range(3):
                        time.sleep(2)
                        snap = watchdog.take_snapshot()
                        if snap.is_responding:
                            logger.warning("Window recovered after hang.")
                            break
                    else:
                        logger.error(
                            "CONFIRMED HANG — Window did not recover after 3 retries."
                        )
                        break

                # Log health
                is_leaking, growth = watchdog.check_memory_leak()
                logger.info(
                    f"[HEALTH] actions={total_actions}, elapsed={elapsed:.0f}s, "
                    f"RSS={snap.memory_rss_mb:.1f}MB (growth={growth:+.1f}MB), "
                    f"CPU={snap.cpu_percent:.0f}%, responding={snap.is_responding}"
                )
                if is_leaking:
                    logger.warning(
                        f"POSSIBLE MEMORY LEAK: RSS={snap.memory_rss_mb:.1f}MB "
                        f"exceeds threshold of {watchdog.memory_threshold_mb}MB"
                    )

                # Reconnect window handle in case WT was restarted or tabs changed
                try:
                    win = app.top_window()
                    watchdog.set_hwnd(win.handle)
                    set_target_hwnd(win.handle)
                except Exception:
                    pass

            # Random delay between actions
            time.sleep(random.uniform(0.01, 0.15))

    except Exception as e:
        logger.error(f"Unexpected error in monkey loop: {e}", exc_info=True)

    # Final summary
    duration = time.time() - start_time
    summary = watchdog.get_summary()
    summary["total_actions"] = total_actions
    summary["action_counts"] = action_counts
    summary["action_errors"] = action_errors
    summary["seed"] = seed

    logger.info("=" * 72)
    logger.info("MONKEY TEST COMPLETE")
    logger.info(f"Duration: {duration:.1f}s ({duration / 60:.1f}m)")
    logger.info(f"Total actions: {total_actions}")
    logger.info(f"Crash detected: {summary['crash_detected']}")
    logger.info(f"Hang count: {summary['hang_count']}")
    logger.info(f"Memory: initial={summary['initial_rss_mb']}MB, peak={summary['peak_rss_mb']}MB, current={summary['current_rss_mb']}MB")
    logger.info(f"Seed: {seed} (use --seed {seed} to reproduce)")

    if action_errors:
        logger.info("Action errors:")
        for name, count in sorted(action_errors.items(), key=lambda x: -x[1]):
            logger.info(f"  {name}: {count}")

    logger.info("Top actions:")
    for name, count in sorted(action_counts.items(), key=lambda x: -x[1])[:10]:
        logger.info(f"  {name}: {count}")

    # Write JSON summary
    summary_file = LOG_DIR / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary written to {summary_file}")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Windows Terminal Monkey Stress Tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m monkey.runner                    # Run for 5 minutes
  python -m monkey.runner --duration 3600    # Run for 1 hour
  python -m monkey.runner --duration 0       # Run forever (Ctrl+C to stop)
  python -m monkey.runner --seed 12345       # Reproducible run
  python -m monkey.runner --launch           # Launch WT if not running
        """,
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Duration in seconds (0 = run forever, default: 300)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--health-interval",
        type=int,
        default=10,
        help="Seconds between health checks (default: 10)",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch Windows Terminal if not already running",
    )
    parser.add_argument(
        "--memory-threshold",
        type=float,
        default=2048.0,
        help="Memory threshold in MB for leak warnings (default: 2048)",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=1,
        help="Number of parallel monkey instances to run (default: 1)",
    )

    parser.add_argument(
        "--instance-id",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    if args.instances > 1:
        import subprocess as sp
        procs = []
        for i in range(args.instances):
            instance_seed = (args.seed or random.randint(0, 2**32 - 1)) + i
            cmd = [
                sys.executable, "-m", "monkey.runner",
                "--duration", str(args.duration),
                "--seed", str(instance_seed),
                "--health-interval", str(args.health_interval),
                "--memory-threshold", str(args.memory_threshold),
                "--instance-id", str(i),
            ]
            if args.launch:
                cmd.append("--launch")
            print(f"Starting monkey instance {i+1}/{args.instances} (seed={instance_seed})")
            proc = sp.Popen(cmd, cwd=str(Path(__file__).parent.parent))
            procs.append(proc)
        for proc in procs:
            proc.wait()
        results = [p.returncode for p in procs]
        print(f"All instances finished. Exit codes: {results}")
        sys.exit(1 if any(r != 0 for r in results) else 0)

    log_file = setup_logging(LOG_DIR, instance_id=args.instance_id)
    logger = logging.getLogger("monkey")
    logger.info(f"Log file: {log_file}")

    summary = run_monkey(
        duration_seconds=args.duration,
        seed=args.seed,
        health_check_interval=args.health_interval,
        auto_launch=args.launch,
        memory_threshold_mb=args.memory_threshold,
    )

    # Exit with error code if crash or hang was detected
    if summary.get("crash_detected") or summary.get("hang_count", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
