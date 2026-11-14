#!/usr/bin/env python3
"""The sentiment worker as a Windows service: install, remove, control, run.

    install [--interval SECONDS] [--python PATH] [--name NAME] [--no-start]
    uninstall [--purge]
    start | stop | restart | status
    run

The service runs one pass of ``sentiment_worker.run()`` every ``--interval`` seconds
(default 300, the cadence the scheduled task used).  The wait between passes is an event,
so a stop request ends the service as soon as the current pass returns - not after the
whole interval.

Why a service instead of the scheduled task: a task is invisible between runs, and a pass
that dies leaves nothing to see.  A service is in services.msc, can be started and stopped,
and its state is the SCM's.  The scheduled task may stay enabled or be removed; the worker's
own instance lock keeps two passes from overlapping either way.

Why ctypes and not pywin32: this machine has no pywin32, and Strata's own service
(Strata\\Scripts\\Strata_Service.py) already talks to the SCM this way, so the same approach
is used here and nothing extra is installed.

What it does about Strata: nothing special, and that is deliberate.  ``sentiment_worker.run``
checks Strata before it scores, logs ``strata_unavailable`` and leaves the files marked, so a
model server that stops and starts under the service simply costs passes, not files.

The service is DEMAND_START and declares NO dependency on Strata.  It waits on its own for the
model to answer (see above), so the two are independent in both directions: Strata can be stopped
or restarted while this runs, and McTrader's start scripts start Strata first, then this.  A
dependency would make the SCM refuse to stop Strata while this service runs - which is exactly
the thing that must not happen, because "stop the service, load a different model" is a normal
thing to do in the middle of the day.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVICE_NAME = "SentimentWorker"
DISPLAY_NAME = "Strata Sentiment Worker"
DESCRIPTION = ("Scores the marked sentiment files in D:\\temp\\McTrader\\Sentiment with the "
               "local Strata model, one pass every few minutes.")
DEPENDENCY = ""                             # none: Strata is stopped and restarted independently
DEFAULT_INTERVAL = 300.0
#: While Strata is away there is nothing to do but wait: no pass is attempted (a pass could
#: only log that the model is missing), and the service checks back on this slower cadence
#: rather than the working interval.  One line is written when it goes away and one when it
#: comes back - not one per check.
STRATA_RETRY_S = 300.0
#: How often the queue is looked at while Strata is away. The model is not probed that often;
#: this is what lets a file dropped during a quiet spell be noticed in half a minute instead of
#: after the old blind 300 s wait (which looked like the worker never saw it).
STRATA_QUEUE_POLL_S = 30.0
#: No fresh work for this long, and a Strata this worker started is stopped again. Only a
#: service this worker started is ever stopped: an instance from a shortcut is not ours.
STRATA_IDLE_STOP_S = 120.0
#: How often the idle timer is checked while a pass waits for its next look.
STRATA_IDLE_POLL_S = 15.0

ROOT = Path(os.path.abspath(__file__)).parent
SERVICE_SCRIPT = Path(os.path.abspath(__file__))
LOG_DIR = ROOT / "logs"


def _default_python() -> Path:
    """The interpreter the scheduled task used, if it is still there."""
    for candidate in (Path(r"C:\Users\AndrewBot\AppData\Local\Programs\Python\Python312\python.exe"),
                      Path(sys.executable)):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _service_root() -> Path:
    return ROOT


# ------------------------------------------------------------------------------------- logging

def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [sentiment-service] {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"service-{time.strftime('%Y-%m-%d')}.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, file=sys.stderr, flush=True)      # a console run shows it; a service has no console
    except Exception:                                 # noqa: BLE001
        pass


class ServiceError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------------------- Win32

if os.name == "nt":
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    SC_MANAGER_CONNECT = 0x0001
    SC_MANAGER_ALL_ACCESS = 0xF003F
    SERVICE_QUERY_STATUS = 0x0004
    SERVICE_START = 0x0010
    SERVICE_STOP = 0x0020
    SERVICE_DELETE = 0x10000
    SERVICE_ALL_ACCESS = 0xF01FF
    SERVICE_WIN32_OWN_PROCESS = 0x00000010
    SERVICE_DEMAND_START = 0x00000003
    SERVICE_ERROR_NORMAL = 0x00000001
    SERVICE_NO_CHANGE = 0xFFFFFFFF
    SERVICE_CONTROL_STOP = 0x00000001
    SERVICE_CONTROL_SHUTDOWN = 0x00000005
    SERVICE_STOPPED = 0x00000001
    SERVICE_START_PENDING = 0x00000002
    SERVICE_RUNNING = 0x00000004
    SERVICE_ACCEPT_STOP = 0x00000001
    SERVICE_ACCEPT_SHUTDOWN = 0x00000004
    SC_STATUS_PROCESS_INFO = 0
    SERVICE_CONFIG_DESCRIPTION = 1
    ERROR_SERVICE_DOES_NOT_EXIST = 1060
    ERROR_SERVICE_ALREADY_RUNNING = 1056
    ERROR_SERVICE_NOT_ACTIVE = 1062
    ERROR_SERVICE_EXISTS = 1073
    ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

    STATE_NAMES = {1: "stopped", 2: "starting", 3: "stopping", 4: "running",
                   5: "continuing", 6: "pausing", 7: "paused"}

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    class SERVICE_STATUS_PROCESS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                    ("dwServiceFlags", wintypes.DWORD)]

    class SERVICE_DESCRIPTIONW(ctypes.Structure):
        _fields_ = [("lpDescription", wintypes.LPWSTR)]

    LPHANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.c_void_p, ctypes.c_void_p)
    LPSERVICE_MAIN_FUNCTIONW = ctypes.WINFUNCTYPE(None, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.LPWSTR))

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", LPSERVICE_MAIN_FUNCTIONW)]

    advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenSCManagerW.restype = wintypes.HANDLE
    advapi32.CreateServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
    advapi32.CreateServiceW.restype = wintypes.HANDLE
    advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenServiceW.restype = wintypes.HANDLE
    advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    advapi32.CloseServiceHandle.restype = wintypes.BOOL
    advapi32.QueryServiceStatusEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
    advapi32.ChangeServiceConfig2W.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    advapi32.ChangeServiceConfig2W.restype = wintypes.BOOL
    advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.StartServiceW.restype = wintypes.BOOL
    advapi32.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.ControlService.restype = wintypes.BOOL
    advapi32.DeleteService.argtypes = [wintypes.HANDLE]
    advapi32.DeleteService.restype = wintypes.BOOL
    advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, LPHANDLER_FUNCTION_EX,
                                                       ctypes.c_void_p]
    advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.SetServiceStatus.restype = wintypes.BOOL
    advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
    advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    shell32.IsUserAnAdmin.argtypes = []
    shell32.IsUserAnAdmin.restype = wintypes.BOOL


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:                                     # noqa: BLE001
        return False


def _word(code: int) -> str:
    try:
        return f"{ctypes.WinError(code)} (error {code})"
    except Exception:                                     # noqa: BLE001
        return f"error {code}"


def _open_scm(access: int = SC_MANAGER_CONNECT):
    handle = advapi32.OpenSCManagerW(None, None, access)
    if not handle:
        raise ServiceError("Cannot open the Windows service manager. Run this elevated "
                           f"({_word(ctypes.get_last_error())}).", 5)
    return handle


def _query_status(handle) -> SERVICE_STATUS_PROCESS:
    info = SERVICE_STATUS_PROCESS()
    needed = wintypes.DWORD()
    if not advapi32.QueryServiceStatusEx(handle, SC_STATUS_PROCESS_INFO, ctypes.byref(info),
                                         ctypes.sizeof(info), ctypes.byref(needed)):
        raise ServiceError(f"Cannot read the service state ({_word(ctypes.get_last_error())}).", 5)
    return info


def service_status(name: str = SERVICE_NAME) -> dict:
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_QUERY_STATUS)
        if not service:
            code = ctypes.get_last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return {"installed": False, "state": "not installed", "pid": 0}
            raise ServiceError(f"Cannot open service '{name}' ({_word(code)}).", 5)
        info = _query_status(service)
        return {"installed": True, "state": STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState)),
                "pid": info.dwProcessId}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def _wait_state(service, wanted: int, timeout: float) -> SERVICE_STATUS_PROCESS:
    deadline = time.monotonic() + max(0.5, timeout)
    info = _query_status(service)
    while time.monotonic() < deadline:
        if info.dwCurrentState == wanted or (wanted != SERVICE_STOPPED and
                                             info.dwCurrentState == SERVICE_STOPPED):
            return info
        time.sleep(0.25)
        info = _query_status(service)
    return info


def start_service(name: str = SERVICE_NAME, timeout: float = 60) -> dict:
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_START | SERVICE_QUERY_STATUS)
        if not service:
            raise ServiceError(f"Service '{name}' is not installed ({_word(ctypes.get_last_error())}).", 5)
        if not advapi32.StartServiceW(service, 0, None):
            code = ctypes.get_last_error()
            if code != ERROR_SERVICE_ALREADY_RUNNING:
                raise ServiceError(f"Cannot start '{name}' ({_word(code)}).", 5)
        info = _wait_state(service, SERVICE_RUNNING, timeout)
        return {"state": STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState)),
                "pid": info.dwProcessId}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def stop_service(name: str = SERVICE_NAME, timeout: float = 900) -> dict:
    """A stop waits for the pass in flight, which can be minutes: the worker scores files."""
    scm = _open_scm()
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not service:
            raise ServiceError(f"Service '{name}' is not installed ({_word(ctypes.get_last_error())}).", 5)
        status = SERVICE_STATUS()
        if not advapi32.ControlService(service, SERVICE_CONTROL_STOP, ctypes.byref(status)):
            code = ctypes.get_last_error()
            if code == ERROR_SERVICE_NOT_ACTIVE:
                return {"state": "stopped", "pid": 0}
            raise ServiceError(f"Cannot stop '{name}' ({_word(code)}).", 5)
        info = _wait_state(service, SERVICE_STOPPED, timeout)
        state = STATE_NAMES.get(info.dwCurrentState, str(info.dwCurrentState))
        if state != "stopped":
            raise ServiceError(f"Service '{name}' did not stop within {int(timeout)} s "
                               f"(state: {state}); the pass in flight may still be scoring.", 5)
        return {"state": state, "pid": 0}
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def ensure_strata() -> dict:
    """Make sure a Strata is answering, without ever competing with one that is loading.

    The rule - port first, then a fresh starting flag means wait, and only then start - lives
    in ``strata.ensure``.  This function is what it may call to start one: service control is
    this module's job, not the worker's, and the worker is handed it as a callable so it never
    imports this module (which imports the worker).
    """
    import strata  # noqa: PLC0415 - imported where it is used, like the worker is

    return strata.ensure(start=lambda: start_service("Strata"), log=log, wait_seconds=180.0)


def delete_service(name: str = SERVICE_NAME) -> None:
    scm = _open_scm(SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = advapi32.OpenServiceW(scm, name, SERVICE_DELETE | SERVICE_QUERY_STATUS)
        if not service:
            code = ctypes.get_last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return
            raise ServiceError(f"Cannot open '{name}' for removal ({_word(code)}).", 5)
        if not advapi32.DeleteService(service):
            code = ctypes.get_last_error()
            if code != ERROR_SERVICE_DOES_NOT_EXIST:
                raise ServiceError(f"Cannot remove '{name}' ({_word(code)}).", 5)
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


def register_service(name: str, bin_path: str) -> str:
    scm = _open_scm(SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = advapi32.CreateServiceW(scm, name, DISPLAY_NAME, SERVICE_ALL_ACCESS,
                                          SERVICE_WIN32_OWN_PROCESS, SERVICE_DEMAND_START,
                                          SERVICE_ERROR_NORMAL, bin_path, None, None, None, None, None)
        if not service:
            code = ctypes.get_last_error()
            if code != ERROR_SERVICE_EXISTS:
                raise ServiceError(f"Cannot create service '{name}' ({_word(code)}).", 5)
            raise ServiceError(f"A service named '{name}' already exists. Use uninstall first, "
                               f"or --name to pick another.", 5)
        description = SERVICE_DESCRIPTIONW(DESCRIPTION)
        advapi32.ChangeServiceConfig2W(service, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(description))
        return "created"
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(scm)


# ------------------------------------------------------------------------------------ supervisor

class Supervisor:
    """One pass every interval, interruptible between passes."""

    def __init__(self, name: str, interval: float):
        self.name = name
        self.interval = max(30.0, float(interval))
        self.handle = None
        self.stop_event = threading.Event()

    # -- SCM reporting --
    def _report(self, state: int, accepted: int = 0, checkpoint: int = 0, wait_hint: int = 0) -> None:
        if not self.handle or os.name != "nt":
            return
        status = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accepted, 0, 0, checkpoint, wait_hint)
        advapi32.SetServiceStatus(self.handle, ctypes.byref(status))

    def report_running(self) -> None:
        self._report(SERVICE_RUNNING, accepted=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)

    def report_stop_pending(self, wait_hint: int) -> None:
        self._report(3, checkpoint=1, wait_hint=max(1000, wait_hint))

    def report_stopped(self) -> None:
        self._report(SERVICE_STOPPED)

    def control(self, command: int) -> int:
        if command in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.stop_event.set()
            self.report_stop_pending(600000)          # a pass can be minutes of scoring
        return 0

    # -- the body --
    def _import_worker(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import sentiment_worker                          # noqa: PLC0415 - only under the SCM or `run`
        return sentiment_worker

    def _pending_paths(self):
        """The marked files waiting now, as paths, or [] when they cannot be listed."""
        try:
            import config as worker_config                # noqa: PLC0415 - imported with the worker
            import store                                  # noqa: PLC0415
            return list(store.discover(Path(worker_config.SENTIMENT_DIR)))
        except Exception as exc:                          # noqa: BLE001 - a count must never stop the loop
            log(f"cannot list the queued files: {exc.__class__.__name__}: {exc}")
            return []

    def _fresh_work(self, worker):
        """Whether the queue holds anything a model could actually change.

        A file that failed on the previous pass is not fresh work: the same bytes cannot
        pass the check that just rejected them, and waking a 15 GB model for them is what
        the two stuck files did 1,309 times.
        """
        pending = self._pending_paths()
        if not pending:
            return False
        try:
            return bool(worker.fresh_work(pending, worker.store.load_failures()))
        except Exception as exc:                          # noqa: BLE001 - an unknown queue is not work
            log(f"cannot judge the queue: {exc.__class__.__name__}: {exc}")
            return False

    def _pending_names(self):
        """The marked files waiting now, by name, or None when they cannot be listed."""
        try:
            import config as worker_config                # noqa: PLC0415 - imported with the worker
            import store                                  # noqa: PLC0415
            return {Path(p).name for p in store.discover(Path(worker_config.SENTIMENT_DIR))}
        except Exception as exc:                          # noqa: BLE001 - a count must never stop the loop
            log(f"cannot count the queued files: {exc.__class__.__name__}: {exc}")
            return None

    def _strata_up(self, worker):
        """(is Strata answering, why not) - never raises."""
        try:
            result = worker.strata_is_up()
            if isinstance(result, tuple):
                return bool(result[0]), (str(result[1]) if len(result) > 1 else "")
            return bool(result), ""
        except Exception as exc:                          # noqa: BLE001 - an unanswered probe is "down"
            return False, f"{exc.__class__.__name__}: {exc}"

    def _strata_idle_expired(self, started_here: bool, idle_since) -> bool:
        """Whether a Strata this worker started has been idle long enough to stop."""
        return (started_here and idle_since is not None
                and (time.monotonic() - idle_since) >= STRATA_IDLE_STOP_S)

    def _stop_strata(self) -> None:
        log(f"no fresh work for {int(STRATA_IDLE_STOP_S)}s; stopping the Strata service this worker started")
        try:
            stop_service("Strata")
        except ServiceError as exc:
            log(f"could not stop Strata: {exc}")

    def run(self) -> None:
        os.chdir(ROOT)
        try:
            worker = self._import_worker()
        except Exception as exc:                         # noqa: BLE001 - a broken checkout is retried
            log(f"cannot import sentiment_worker from {ROOT}: {exc.__class__.__name__}: {exc}")
            return
        log(f"service starting: interval={int(self.interval)}s root={ROOT} python={sys.executable}")
        previous = None
        strata_away = False
        started_here = False          # whether this worker started the Strata service
        idle_since = None             # monotonic time at which fresh work last existed
        while not self.stop_event.is_set():
            # Fresh work decides everything below: whether a stopped Strata is started, and
            # whether one this worker started has earned a rest.
            fresh = self._fresh_work(worker)
            if fresh:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()

            if self._strata_idle_expired(started_here, idle_since):
                self._stop_strata()
                started_here = False
                strata_away = True

            up, detail = self._strata_up(worker)
            if not up and fresh:
                # There is work a model could change and nothing is listening. The gate waits
                # for a Strata that is already loading and starts the service only when nothing
                # is happening at all, so this never competes with a load in flight.
                outcome = ensure_strata()
                log(f"strata was down and there is fresh work: {outcome.get('action')}")
                if outcome.get("action") == "started":
                    started_here = True
                up, detail = self._strata_up(worker)
            if not up:
                if not strata_away:
                    # The queue is looked at far more often than the model is probed: a file
                    # that landed while Strata was away used to wait for the next 300 s check,
                    # which looked exactly like the worker never noticing it.
                    log(f"strata is not answering ({detail}); checking the queue every {int(STRATA_QUEUE_POLL_S)}s")
                    strata_away = True
                if self.stop_event.wait(STRATA_QUEUE_POLL_S):
                    break
                continue
            if strata_away:
                log("strata is answering again; resuming passes")
                strata_away = False
            started = time.monotonic()
            try:
                # stop_event is handed in so a stop lands between files instead of after the
                # whole pass: the pass used to run to its end, which is what "Stop Pending"
                # meant on 2026-11-11 (297 s).
                code = worker.run(should_stop=self.stop_event.is_set, ensure_strata=ensure_strata)
            except BaseException as exc:                 # noqa: BLE001 - a watchdog must not die
                log(f"pass raised {exc.__class__.__name__}: {exc}")
                code = 1
            elapsed = time.monotonic() - started
            pending = self._pending_names()
            if pending and pending != previous:
                # Work was found and the queue moved: go straight back in and keep going
                # until it is empty.  The comparison is what makes this safe - a file that
                # cannot be scored (a schema breach, say) stays marked for ever, and
                # "keep going while anything is queued" would hot-loop on it.
                previous = pending
                wait_s = 5.0
                log(f"pass exit={code} in {elapsed:.1f}s; {len(pending)} still queued, working on: 5s")
            else:
                previous = pending
                if pending:
                    # Name them, so the log says WHICH files are stuck and where the reason is.
                    # Nothing is moved out of the queue: the queue is the record, and a file that
                    # cannot be scored staying in it is the point.
                    named = ", ".join(sorted(pending)[:5])
                    more = "" if len(pending) <= 5 else f" and {len(pending) - 5} more"
                    state = (f"{len(pending)} stuck (unchanged): {named}{more} - kept in the queue, "
                             f"state/failures.json says why")
                else:
                    state = "queue empty"
                # Start-to-start, so a pass that took most of the window does not push the
                # next look a further interval out.  A floor keeps a fast pass from spinning.
                wait_s = max(5.0, self.interval - elapsed)
                log(f"pass exit={code} in {elapsed:.1f}s; {state}; next pass in {int(wait_s)}s")
            wait_until = time.monotonic() + wait_s
            while not self.stop_event.is_set():
                if self._strata_idle_expired(started_here, idle_since):
                    self._stop_strata()
                    started_here = False
                    break
                left = wait_until - time.monotonic()
                if left <= 0:
                    break
                if self.stop_event.wait(min(left, STRATA_IDLE_POLL_S)):
                    break
        log("stop requested; exiting after the pass in flight")

    def main(self) -> None:
        if os.name != "nt":
            return
        self.handle = advapi32.RegisterServiceCtrlHandlerExW(self.name, _HANDLER_REF, None)
        if not self.handle:
            return
        self._report(SERVICE_START_PENDING, checkpoint=1, wait_hint=10000)
        self.report_running()
        try:
            self.run()
        finally:
            self.report_stopped()
            log("service stopped")


_HANDLER_REF = None
_MAIN_REF = None


def run_service(name: str, interval: float) -> int:
    if os.name != "nt":
        raise ServiceError("The sentiment worker service is for Windows only.", 2)
    global _HANDLER_REF, _MAIN_REF
    supervisor = Supervisor(name, interval)

    @LPHANDLER_FUNCTION_EX
    def on_control(command, event_type, event_data, context):
        return supervisor.control(command)

    @LPSERVICE_MAIN_FUNCTIONW
    def on_main(argc, argv):
        supervisor.main()

    _HANDLER_REF, _MAIN_REF = on_control, on_main
    table = (SERVICE_TABLE_ENTRYW * 2)()
    table[0].lpServiceName = name
    table[0].lpServiceProc = on_main
    table[1].lpServiceName = None
    table[1].lpServiceProc = LPSERVICE_MAIN_FUNCTIONW()
    if not advapi32.StartServiceCtrlDispatcherW(table):
        code = ctypes.get_last_error()
        if code == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise ServiceError("'run' is started by the Windows Service Control Manager, not by hand. "
                               "Use: service.py start", 5)
        raise ServiceError(f"cannot connect to the service manager: {_word(code)}", 5)
    return 0


# -------------------------------------------------------------------------------------- commands

def command_install(args) -> int:
    if not is_admin():
        raise ServiceError("Administrator rights are needed to install the service. Run service.ps1 "
                           "-Action Install, which asks for them.", 5)
    python = Path(args.python) if args.python else _default_python()
    if not python.is_file():
        raise ServiceError(f"no Python at {python}", 2)
    interval = max(30.0, float(args.interval))
    bin_path = f'"{python}" "{SERVICE_SCRIPT}" run --interval {int(interval)} --name {args.name}'
    register_service(args.name, bin_path)
    # Always write the dependency list, even when it is empty.  `sc config depend=` is the
    # only way to clear one, and a service that was once installed with `depend= Strata`
    # keeps it forever otherwise: the SCM then starts Strata before this service, which made
    # "start the worker" look like "the worker woke the model".  Reinstalling is the repair.
    subprocess.run(["sc.exe", "config", args.name, "depend=", DEPENDENCY],
                   capture_output=True, timeout=15, check=False)
    # A pass that dies should come back by itself: the worker tolerates Strata being down.
    subprocess.run(["sc.exe", "failure", args.name, "reset=", "86400",
                    "actions=", "restart/5000/restart/30000/restart/60000"],
                   capture_output=True, timeout=15, check=False)
    log(f"installed '{args.name}': {bin_path}")
    print(f"Service '{args.name}' created (Demand"
          + (f", depends on {DEPENDENCY})." if DEPENDENCY else ", no dependency)."))
    print(f"  python   {python}")
    print(f"  interval {int(interval)}s")
    print(f"  logs     {LOG_DIR}")
    if args.no_start:
        return 0
    result = start_service(args.name, timeout=60)
    print(f"Service '{args.name}' is {result['state']} (process {result['pid']}).")
    return 0


def command_uninstall(args) -> int:
    if not is_admin():
        raise ServiceError("Administrator rights are needed to remove the service.", 5)
    status = service_status(args.name)
    if not status["installed"]:
        print(f"Service '{args.name}' is not installed.")
    else:
        if status["state"] != "stopped":
            print(f"Stopping '{args.name}' first ...")
            stop_service(args.name)
        delete_service(args.name)
        print(f"Service '{args.name}' removed.")
    if args.purge:
        for path in (LOG_DIR,):
            if path.exists():
                import shutil
                shutil.rmtree(path, ignore_errors=True)
                print(f"removed {path}")
    return 0


def command_start(args) -> int:
    if not is_admin():
        raise ServiceError("Administrator rights are needed to start the service.", 5)
    result = start_service(args.name, timeout=60)
    print(f"Service '{args.name}' is {result['state']} (process {result['pid']}).")
    return 0


def command_stop(args) -> int:
    if not is_admin():
        raise ServiceError("Administrator rights are needed to stop the service.", 5)
    print("Stopping ... a pass in flight is allowed to finish (that can take minutes).")
    result = stop_service(args.name)
    print(f"Service '{args.name}' is {result['state']}.")
    return 0


def command_restart(args) -> int:
    command_stop(args)
    return command_start(args)


def command_status(args) -> int:
    status = service_status(args.name)
    if not status["installed"]:
        print(f"Service '{args.name}' is not installed.")
        print("Install it with: service.ps1 -Action Install")
        return 0
    words = status["state"] + (f" (process {status['pid']})" if status["pid"] else "")
    print(f"Service   {args.name}: {words}")
    print(f"Root      {ROOT}")
    print(f"Logs      {LOG_DIR}")
    newest = sorted(LOG_DIR.glob("worker-*.log"), key=lambda p: p.stat().st_mtime) if LOG_DIR.is_dir() else []
    if newest:
        print(f"Latest    {newest[-1].name}")
    return 0


def command_run(args) -> int:
    return run_service(args.name, args.interval)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="service.py",
                                 description="The sentiment worker as a Windows service.")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, text in (("install", "register the service (Administrator)"),
                       ("uninstall", "stop and remove it (Administrator)"),
                       ("start", "start it (Administrator)"),
                       ("stop", "stop it, letting the pass in flight finish (Administrator)"),
                       ("restart", "stop then start (Administrator)"),
                       ("status", "say what the service is doing"),
                       ("run", "the Service Control Manager entry point")):
        item = sub.add_parser(name, help=text)
        item.add_argument("--name", default=SERVICE_NAME)
        if name == "install":
            item.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                              help="seconds between passes (default 300)")
            item.add_argument("--python", help="the interpreter the service runs (default: the "
                                               "one the scheduled task used)")
            item.add_argument("--no-start", action="store_true", help="register but do not start")
        if name == "uninstall":
            item.add_argument("--purge", action="store_true", help="also delete the logs")
        if name == "run":
            item.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return command_run(args)
        handlers = {"install": command_install, "uninstall": command_uninstall,
                    "start": command_start, "stop": command_stop, "restart": command_restart,
                    "status": command_status}
        return handlers[args.command](args)
    except ServiceError as exc:
        print(f"sentiment service: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"sentiment service: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
