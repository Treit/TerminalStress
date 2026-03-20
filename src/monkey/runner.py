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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import psutil
from pywinauto.application import Application

from .actions import ACTION_PROFILES, FocusError, build_action_catalog, pick_action, set_target_hwnd
from .watchdog import Watchdog, find_wt_process

# Logging setup
LOG_DIR = Path(__file__).parent.parent / "monkey_logs"
DUMP_DIR = Path(__file__).parent.parent.parent / "crashdumps"


def setup_logging(log_dir: Path, instance_id: int | None = None) -> Path:
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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


def launch_wt(profile: str | None = None) -> tuple[Application, any, int]:
    """
    Launch a new Windows Terminal instance and connect to it.
    Returns (app, window, pid).

    Args:
        profile: WT profile name to use (e.g. "Command Prompt"). None = default.
    """
    import subprocess
    cmd = ["wt.exe"]
    if profile:
        cmd += ["-p", profile]
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
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
    wt_profile: str | None = None,
    action_profile: str = "default",
):
    """
    Main monkey test loop.

    Args:
        duration_seconds: How long to run (0 = forever).
        seed: Random seed for reproducibility.
        health_check_interval: Seconds between health snapshots.
        auto_launch: Whether to launch WT if not running.
        memory_threshold_mb: Memory threshold for leak warnings.
        wt_profile: WT profile to use when launching (e.g. "Command Prompt").
        action_profile: Bias action selection toward specific WT subsystems.
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
            app, win, pid = launch_wt(profile=wt_profile)
        else:
            raise

    logger.info(f"Connected to Windows Terminal (PID {pid})")

    # Track all PIDs and crashes for post-run analysis
    all_pids = [pid]
    crash_events = []
    hang_events = []

    # Initialize watchdog
    watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
    watchdog.set_hwnd(win.handle)
    set_target_hwnd(win.handle, pid)

    # Take initial snapshot
    snap = watchdog.take_snapshot()
    logger.info(
        f"Initial health: RSS={snap.memory_rss_mb:.1f}MB, "
        f"responding={snap.is_responding}"
    )

    # Build action catalog
    catalog = build_action_catalog(action_profile=action_profile)
    total_weight = sum(a.weight for a in catalog)
    logger.info(
        f"Action catalog: {len(catalog)} actions, total weight={total_weight}, "
        f"profile={action_profile}"
    )

    # Stats
    action_counts: dict[str, int] = {}
    action_errors: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    total_actions = 0
    start_time = time.time()
    last_health_check = start_time
    recent_actions: deque[str] = deque(maxlen=6)
    recent_tags: deque[str] = deque(maxlen=12)

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

    def _capture_context(current_action=None) -> dict:
        actions = list(recent_actions)
        tags = list(dict.fromkeys(recent_tags))
        if current_action is not None:
            actions = (actions + [current_action.name])[-6:]
            tags = list(dict.fromkeys(tags + list(current_action.tags)))
        return {
            "recent_actions": actions,
            "recent_tags": tags,
        }

    try:
        while running:
            elapsed = time.time() - start_time
            if duration_seconds > 0 and elapsed >= duration_seconds:
                logger.info("Duration reached, stopping.")
                break

            # Pick and execute a random action
            action = pick_action(
                catalog,
                recent_actions=tuple(recent_actions),
                recent_tags=tuple(recent_tags),
            )
            total_actions += 1
            action_counts[action.name] = action_counts.get(action.name, 0) + 1

            try:
                logger.info(f"[{total_actions}] {action.name}")
                action.func(win)
                recent_actions.append(action.name)
                for tag in action.tags:
                    recent_tags.append(tag)
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except FocusError as e:
                if "External foreground window" in str(e):
                    logger.info(f"[{total_actions}] {action.name} paused: {e}")
                else:
                    logger.debug(f"[{total_actions}] {action.name} skipped ({e})")
                total_actions -= 1
                remaining = action_counts.get(action.name, 0) - 1
                if remaining > 0:
                    action_counts[action.name] = remaining
                else:
                    action_counts.pop(action.name, None)
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
                        crash_events.append(
                            {
                                "time": time.time(),
                                "pid": pid,
                                "exit_code": exit_code,
                                "last_action": action.name,
                                "total_actions": total_actions,
                                **_capture_context(action),
                            }
                        )
                        if not auto_launch:
                            break
                    # Try to reconnect or relaunch
                    logger.info("Attempting to reconnect to Windows Terminal...")
                    time.sleep(2)
                    try:
                        app, win, pid = connect_to_wt()
                        watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                        watchdog.set_hwnd(win.handle)
                        set_target_hwnd(win.handle, pid)
                        if pid not in all_pids:
                            all_pids.append(pid)
                        logger.info(f"Reconnected to Windows Terminal (PID {pid})")
                    except RuntimeError:
                        if auto_launch:
                            logger.info("Launching new Windows Terminal instance...")
                            try:
                                app, win, pid = launch_wt(profile=wt_profile)
                                watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                                watchdog.set_hwnd(win.handle)
                                set_target_hwnd(win.handle, pid)
                                if pid not in all_pids:
                                    all_pids.append(pid)
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
                        crash_events.append(
                            {
                                "time": time.time(),
                                "pid": pid,
                                "exit_code": exit_code,
                                "last_action": "health_check",
                                "total_actions": total_actions,
                                **_capture_context(),
                            }
                        )
                        if not auto_launch:
                            break
                    logger.info("Attempting to reconnect to Windows Terminal...")
                    time.sleep(2)
                    try:
                        app, win, pid = connect_to_wt()
                        watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                        watchdog.set_hwnd(win.handle)
                        set_target_hwnd(win.handle, pid)
                        if pid not in all_pids:
                            all_pids.append(pid)
                        logger.info(f"Reconnected to Windows Terminal (PID {pid})")
                        continue
                    except RuntimeError:
                        if auto_launch:
                            logger.info("Launching new Windows Terminal instance...")
                            try:
                                app, win, pid = launch_wt(profile=wt_profile)
                                watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                                watchdog.set_hwnd(win.handle)
                                set_target_hwnd(win.handle, pid)
                                if pid not in all_pids:
                                    all_pids.append(pid)
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
                        # Capture crash dump before killing the hung process
                        dump_path = watchdog.capture_dump(DUMP_DIR)
                        if dump_path:
                            logger.info(f"Hang dump saved: {dump_path}")
                        else:
                            logger.warning("Failed to capture hang dump")

                        watchdog.kill_process()
                        logger.info(f"Killed hung process PID {pid}")

                        hang_events.append(
                            {
                                "time": time.time(),
                                "pid": pid,
                                "last_action": recent_actions[-1] if recent_actions else None,
                                "total_actions": total_actions,
                                "dump_path": dump_path,
                                **_capture_context(),
                            }
                        )

                        if not auto_launch:
                            break

                        # Try to reconnect or relaunch
                        logger.info("Attempting to reconnect to Windows Terminal...")
                        time.sleep(2)
                        try:
                            app, win, pid = connect_to_wt()
                            watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                            watchdog.set_hwnd(win.handle)
                            set_target_hwnd(win.handle, pid)
                            if pid not in all_pids:
                                all_pids.append(pid)
                            logger.info(f"Reconnected to Windows Terminal (PID {pid})")
                        except RuntimeError:
                            if auto_launch:
                                logger.info("Launching new Windows Terminal instance...")
                                try:
                                    app, win, pid = launch_wt(profile=wt_profile)
                                    watchdog = Watchdog(pid, memory_threshold_mb=memory_threshold_mb)
                                    watchdog.set_hwnd(win.handle)
                                    set_target_hwnd(win.handle, pid)
                                    if pid not in all_pids:
                                        all_pids.append(pid)
                                    logger.info(f"Launched and connected to Windows Terminal (PID {pid})")
                                except Exception:
                                    logger.error("Failed to launch Windows Terminal. Stopping.")
                                    break
                            else:
                                logger.error("No Windows Terminal instance found. Stopping.")
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
                    set_target_hwnd(win.handle, pid)
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
    summary["action_profile"] = action_profile
    summary["seed"] = seed
    summary["all_pids"] = all_pids
    summary["crash_events"] = crash_events
    summary["hang_events"] = hang_events
    summary["tag_counts"] = tag_counts
    summary["total_crashes"] = len(crash_events)

    logger.info("=" * 72)
    logger.info("MONKEY TEST COMPLETE")
    logger.info(f"Duration: {duration:.1f}s ({duration / 60:.1f}m)")
    logger.info(f"Total actions: {total_actions}")
    logger.info(f"Crash detected: {summary['crash_detected']}")
    logger.info(f"Total crashes (with recovery): {len(crash_events)}")
    logger.info(f"All PIDs observed: {all_pids}")
    logger.info(f"Hang count: {summary['hang_count']}")
    if tag_counts:
        logger.info(
            "Code-path coverage: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(tag_counts.items(), key=lambda x: -x[1])
            )
        )
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
    summary_file = LOG_DIR / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
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
  python -m monkey.runner --action-profile scroll-race
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
        "--wt-profile",
        type=str,
        default=None,
        help='WT profile to launch (e.g. "Command Prompt" to skip shell profile loading)',
    )
    parser.add_argument(
        "--action-profile",
        type=str,
        choices=sorted(ACTION_PROFILES.keys()),
        default="default",
        help="Bias action selection toward a specific set of WT code paths",
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
            if args.wt_profile:
                cmd += ["--wt-profile", args.wt_profile]
            cmd += ["--action-profile", args.action_profile]
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
        wt_profile=args.wt_profile,
        action_profile=args.action_profile,
    )

    # Exit with error code if crash or hang was detected
    if summary.get("crash_detected") or summary.get("hang_count", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
