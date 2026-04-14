from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Sequence


PROJECT_NAME = "jupiter-sentinel"
PROJECT_VERSION = "3.0.0"
STARTUP_WAIT_SECONDS = 0.5
STOP_WAIT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.2
SERVICE_RETRY_SECONDS = 5.0
_MODULE_DIR = Path(__file__).resolve().parent


def _env_text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env_text(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def _env_optional_int(name: str) -> int | None:
    value = _env_text(name)
    if value is None:
        return None
    return int(value)


def _env_optional_float(name: str) -> float | None:
    value = _env_text(name)
    if value is None:
        return None
    return float(value)


def _env_optional_path(name: str) -> Path | None:
    value = _env_text(name)
    if value is None:
        return None
    return Path(value).expanduser()


def _env_choice(name: str, allowed: set[str]) -> str | None:
    value = _env_text(name)
    if value is None:
        return None
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {options}")
    return value


def _default_runtime_dir() -> Path:
    env_dir = os.environ.get("JUPITER_SENTINEL_RUNTIME_DIR", "").strip()
    if env_dir:
        path = Path(env_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    repo_dir = _MODULE_DIR / ".jupiter-sentinel"
    try:
        repo_dir.mkdir(parents=True, exist_ok=True)
        return repo_dir
    except OSError:
        fallback = Path.home() / ".jupiter-sentinel"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _runtime_paths() -> dict[str, Path]:
    runtime_dir = _default_runtime_dir()
    return {
        "runtime_dir": runtime_dir,
        "pid": runtime_dir / "autotrader.pid",
        "meta": runtime_dir / "autotrader.json",
        "log": runtime_dir / "autotrader.log",
        "state": runtime_dir / "state.json",
        "stop": runtime_dir / "stop.signal",
    }


def _load_metadata() -> dict[str, object]:
    meta_path = _runtime_paths()["meta"]
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_metadata(metadata: dict[str, object]) -> None:
    meta_path = _runtime_paths()["meta"]
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _cleanup_runtime_files() -> None:
    for key in ("pid", "meta", "stop"):
        path = _runtime_paths()[key]
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _read_pid() -> int | None:
    pid_path = _runtime_paths()["pid"]
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ensure_not_stale() -> int | None:
    pid = _read_pid()
    if pid is None:
        _cleanup_runtime_files()
        return None
    if not _is_process_running(pid):
        _cleanup_runtime_files()
        return None
    return pid


def _request_stop() -> None:
    _runtime_paths()["stop"].write_text("stop\n", encoding="utf-8")


def _stop_requested() -> bool:
    return _runtime_paths()["stop"].exists()


def _sleep_with_stop_check(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop_requested():
            return True
        time.sleep(min(POLL_INTERVAL_SECONDS, deadline - time.monotonic()))
    return _stop_requested()


def _default_state_file() -> Path:
    env_path = _env_optional_path("JUPITER_SENTINEL_STATE_FILE")
    return env_path if env_path is not None else _runtime_paths()["state"]


def _add_service_options(parser: argparse.ArgumentParser, *, include_foreground: bool) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        default=_env_bool("JUPITER_SENTINEL_LIVE", False),
        help="Execute real swaps instead of dry-run quotes.",
    )
    parser.add_argument(
        "--entry-amount-sol",
        type=float,
        default=_env_optional_float("JUPITER_SENTINEL_ENTRY_AMOUNT_SOL"),
        help="Target SOL amount per new position before risk sizing.",
    )
    parser.add_argument(
        "--enter-on",
        choices=["down", "up", "all"],
        default=_env_choice("JUPITER_SENTINEL_ENTER_ON", {"down", "up", "all"}),
        help="Which alert direction should trigger entries.",
    )
    parser.add_argument(
        "--max-open-positions",
        type=int,
        default=_env_optional_int("JUPITER_SENTINEL_MAX_OPEN_POSITIONS"),
        help="Optional cap on simultaneous open positions.",
    )
    parser.add_argument(
        "--interval-secs",
        type=int,
        default=_env_optional_int("JUPITER_SENTINEL_INTERVAL_SECS"),
        help="Loop sleep interval between cycles.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=_env_optional_int("JUPITER_SENTINEL_ITERATIONS"),
        help="Optional finite number of cycles for testing.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=_default_state_file(),
        help="Path to the persistent state JSON file.",
    )
    if include_foreground:
        parser.add_argument(
            "--foreground",
            action="store_true",
            default=_env_bool("JUPITER_SENTINEL_FOREGROUND", False),
            help="Run the service in the current terminal instead of as a background process.",
        )


def _service_argv(args: argparse.Namespace) -> list[str]:
    argv: list[str] = []
    if args.live:
        argv.append("--live")
    if args.entry_amount_sol is not None:
        argv.extend(["--entry-amount-sol", str(args.entry_amount_sol)])
    if args.enter_on is not None:
        argv.extend(["--enter-on", args.enter_on])
    if args.max_open_positions is not None:
        argv.extend(["--max-open-positions", str(args.max_open_positions)])
    if args.interval_secs is not None:
        argv.extend(["--interval-secs", str(args.interval_secs)])
    if args.iterations is not None:
        argv.extend(["--iterations", str(args.iterations)])
    argv.extend(["--state-file", str(Path(args.state_file).expanduser().resolve())])
    return argv


def _configure_service_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _start_health_server(trader: object) -> object | None:
    if not _env_bool("JUPITER_SENTINEL_HEALTHCHECK_ENABLED", False):
        return None

    host = _env_text("JUPITER_SENTINEL_HEALTH_HOST") or "0.0.0.0"
    port_value = _env_optional_int("JUPITER_SENTINEL_HEALTH_PORT")
    port = 8080 if port_value is None else port_value
    default_stale_after = max(getattr(trader, "scan_interval_secs", 60) * 3, 180)
    stale_after_value = _env_optional_float("JUPITER_SENTINEL_HEALTH_STALE_AFTER_SECS")
    stale_after_secs = (
        float(default_stale_after)
        if stale_after_value is None
        else stale_after_value
    )

    from src.service_health import BotHealthServer

    server = BotHealthServer(
        trader,
        host=host,
        port=port,
        stale_after_secs=stale_after_secs,
    )
    server.start()
    logging.info(
        "Healthcheck endpoint listening on http://%s:%s%s",
        host,
        server.port,
        server.health_path,
    )
    return server


def _run_autotrader_process(service_argv: list[str]) -> int:
    from src.autotrader import AutoTrader, build_arg_parser

    parsed = build_arg_parser().parse_args(service_argv)
    trader = AutoTrader(
        dry_run=not parsed.live,
        entry_amount_sol=parsed.entry_amount_sol,
        enter_on=parsed.enter_on,
        max_open_positions=parsed.max_open_positions,
        scan_interval_secs=parsed.interval_secs,
        state_path=parsed.state_file,
    )

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    health_server = None

    def handle_signal(sig: int, frame: object) -> None:
        del sig, frame
        trader.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        health_server = _start_health_server(trader)
        trader.run(max_iterations=parsed.iterations)
        return 0
    finally:
        if health_server is not None:
            health_server.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _run_service(args: argparse.Namespace) -> int:
    _configure_service_logging()
    service_argv = _service_argv(args)
    retry_forever = args.iterations is None

    while True:
        if _stop_requested():
            _cleanup_runtime_files()
            return 0
        try:
            return _run_autotrader_process(service_argv)
        except KeyboardInterrupt:
            return 130
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code == 0 or not retry_forever:
                return code
            logging.exception(
                "Autotrader exited unexpectedly with code %s. Retrying in %.1f seconds.",
                code,
                SERVICE_RETRY_SECONDS,
            )
        except Exception:
            if not retry_forever:
                logging.exception("Autotrader crashed during startup or runtime.")
                return 1
            logging.exception(
                "Autotrader crashed during startup or runtime. Retrying in %.1f seconds.",
                SERVICE_RETRY_SECONDS,
            )

        if _sleep_with_stop_check(SERVICE_RETRY_SECONDS):
            _cleanup_runtime_files()
            return 0


def _start_command(args: argparse.Namespace) -> int:
    existing_pid = _ensure_not_stale()
    if existing_pid is not None:
        print(f"{PROJECT_NAME} is already running with PID {existing_pid}.")
        return 0

    try:
        _runtime_paths()["stop"].unlink()
    except FileNotFoundError:
        pass

    if args.foreground:
        return _run_service(args)

    paths = _runtime_paths()
    log_path = paths["log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = [sys.executable, "-m", "jupiter_sentinel_cli", "_run-service", *_service_argv(args)]
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(_MODULE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )

    time.sleep(STARTUP_WAIT_SECONDS)
    if process.poll() is not None:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8")[-2000:]
        except OSError:
            pass
        print(f"Failed to start {PROJECT_NAME}; process exited with code {process.returncode}.")
        if tail:
            print("")
            print(tail.rstrip())
        return int(process.returncode or 1)

    paths["pid"].write_text(str(process.pid), encoding="utf-8")
    _write_metadata(
        {
            "pid": process.pid,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "log_file": str(log_path.resolve()),
            "state_file": str(Path(args.state_file).expanduser().resolve()),
            "mode": "live" if args.live else "dry-run",
            "command": command,
        }
    )

    print(f"Started {PROJECT_NAME} in background.")
    print(f"PID: {process.pid}")
    print(f"Log: {log_path.resolve()}")
    return 0


def _status_command() -> int:
    pid = _ensure_not_stale()
    metadata = _load_metadata()
    if pid is None:
        print(f"{PROJECT_NAME} is stopped.")
        return 0

    print(f"{PROJECT_NAME} is running.")
    print(f"PID: {pid}")
    started_at = metadata.get("started_at")
    if started_at:
        print(f"Started: {started_at}")
    mode = metadata.get("mode")
    if mode:
        print(f"Mode: {mode}")
    state_file = metadata.get("state_file")
    if state_file:
        print(f"State file: {state_file}")
    log_file = metadata.get("log_file")
    if log_file:
        print(f"Log file: {log_file}")
    return 0


def _stop_command() -> int:
    pid = _ensure_not_stale()
    if pid is None:
        print(f"{PROJECT_NAME} is not running.")
        return 0

    _request_stop()
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        pass
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _is_process_running(pid):
            _cleanup_runtime_files()
            print(f"Stopped {PROJECT_NAME} (PID {pid}).")
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)

    try:
        os.kill(pid, signal.SIGKILL)
    except PermissionError:
        print(
            f"Stop requested for {PROJECT_NAME} (PID {pid}), "
            "but the process could not be signaled in this environment."
        )
        return 1

    _cleanup_runtime_files()
    print(f"Force-stopped {PROJECT_NAME} (PID {pid}).")
    return 0


def _demo_command(args: argparse.Namespace) -> int:
    import demo as demo_module

    report = demo_module.run_demo()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(demo_module.render_report(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROJECT_NAME)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROJECT_VERSION}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the deterministic mock-backed demo.",
    )
    demo_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the demo payload as JSON.",
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Start the autonomous trader service.",
    )
    _add_service_options(start_parser, include_foreground=True)

    subparsers.add_parser(
        "status",
        help="Show whether the autonomous trader service is running.",
    )
    subparsers.add_parser(
        "stop",
        help="Stop the autonomous trader service.",
    )

    service_parser = subparsers.add_parser(
        "_run-service",
        help=argparse.SUPPRESS,
    )
    _add_service_options(service_parser, include_foreground=False)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if args.command == "demo":
        return _demo_command(args)
    if args.command == "start":
        return _start_command(args)
    if args.command == "status":
        return _status_command()
    if args.command == "stop":
        return _stop_command()
    if args.command == "_run-service":
        return _run_service(args)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
