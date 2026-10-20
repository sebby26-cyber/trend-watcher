"""One-command install, scheduler on/off and a plain-English status report.

Everything is derived from ``Path.home()`` and this checkout's location, so a
fresh ``git clone`` followed by ``bin/trend-watch setup`` is the whole install.
All external commands (launchctl, crontab) go through ``call`` so tests can
replace it.
"""
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .config import Paths, initialize

LABEL = "io.github.trend-watch"
MARKER = "# trend-watch-managed"
MINUTES = (7, 37)
SCHEDULE_TEXT = "runs at :07 and :37 every hour"
CHECKOUT = Path(__file__).resolve().parent.parent
SCRIPT = CHECKOUT / "bin" / "trend-watch"
LINK_CANDIDATES = [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


def call(args, input=None):
    """The single gateway to external commands (patched in tests)."""
    try:
        return subprocess.run(list(args), input=input, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(list(args), 127, "", str(exc))


def system_name(system=None):
    return system or ("darwin" if sys.platform == "darwin" else "linux")


def _home(home):
    return Path(home) if home is not None else Path.home()


def python_executable():
    return sys.executable or "/usr/bin/python3"


def scheduler_label():
    return os.environ.get("TREND_WATCH_LABEL") or LABEL


def custom_wrapper(home=None):
    value = os.environ.get("TREND_WATCH_WRAPPER")
    if not value:
        return None
    wrapper = Path(value).expanduser().resolve()
    return wrapper if wrapper.is_file() and os.access(str(wrapper), os.X_OK) else None


def mode_text(home=None):
    return "custom wrapper" if custom_wrapper(home) else "direct"


# ---------------------------------------------------------------- command on PATH

def _is_checkout_script(path):
    path = Path(path)
    return path.name == "trend-watch" and path.parent.name == "bin" and (path.parent.parent / "trend_watch" / "cli.py").is_file()


def install_link(home=None, candidates=None, path_env=None):
    """Symlink ``trend-watch`` into a PATH folder. Returns (link, message, on_path)."""
    home = _home(home)
    target = Path(os.path.realpath(SCRIPT))
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    on_path = {os.path.realpath(p) for p in path_env.split(os.pathsep) if p}
    folders = []
    for folder in (LINK_CANDIDATES if candidates is None else candidates):
        folder = Path(folder)
        try:
            if folder.is_dir() and os.path.realpath(folder) in on_path and os.access(str(folder), os.W_OK):
                folders.append(folder)
        except OSError:
            continue
    folders.append(home / ".local" / "bin")
    for folder in folders:
        link = folder / "trend-watch"
        try:
            if link.is_symlink():
                current = os.path.realpath(link)
                if current == str(target):
                    return link, "already installed", os.path.realpath(folder) in on_path
                if os.path.exists(link) and not _is_checkout_script(current):
                    continue  # someone else's command: leave it alone
                link.unlink()
                link.symlink_to(target)
                return link, "updated", os.path.realpath(folder) in on_path
            if os.path.lexists(link):
                continue  # never overwrite a regular file
            folder.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            return link, "installed", os.path.realpath(folder) in on_path
        except OSError:
            continue
    return None, "could not install a trend-watch command", False


# ---------------------------------------------------------------- macOS launchd

def plist_path(home=None):
    return _home(home) / "Library" / "LaunchAgents" / (scheduler_label() + ".plist")


def plist_bytes(paths, home=None):
    home = _home(home)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TW_REPO": str(CHECKOUT),
           "TW_CONFIG_DIR": str(paths.config), "TW_STATE_DIR": str(paths.state), "TW_OUT_DIR": str(paths.out)}
    for key in ("TREND_WATCH_PACKS_DIR", "XDG_CONFIG_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    wrapper = custom_wrapper(home)
    if wrapper:
        program = [str(wrapper)]
    else:
        program = [python_executable(), str(SCRIPT), "run"]
    logs = paths.state / "logs"
    data = {"Label": scheduler_label(), "ProgramArguments": program, "RunAtLoad": False,
            "StartCalendarInterval": [{"Minute": m} for m in MINUTES],
            "StandardOutPath": str(logs / "launchd.out.log"), "StandardErrorPath": str(logs / "launchd.err.log"),
            "EnvironmentVariables": env}
    return plistlib.dumps(data, sort_keys=True)


def write_plist(paths, home=None):
    """Write the LaunchAgent definition only when its content changed. Returns True if written."""
    path = plist_path(home)
    content = plist_bytes(paths, home)
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(str(tmp), str(path))
    return True


def domain():
    return "gui/{}".format(os.getuid())


def launchd_loaded():
    return call(["launchctl", "print", "{}/{}".format(domain(), scheduler_label())]).returncode == 0


def launchd_bootstrap(home=None, attempts=5):
    result = None
    for attempt in range(attempts):
        result = call(["launchctl", "bootstrap", domain(), str(plist_path(home))])
        if result.returncode == 0 or launchd_loaded():
            return True, ""
        if attempt + 1 < attempts:
            time.sleep(1)  # launchd may still be tearing down a just-booted-out job
    return False, (result.stderr or result.stdout or "").strip()


def launchd_bootout():
    call(["launchctl", "bootout", "{}/{}".format(domain(), scheduler_label())])
    return not launchd_loaded()


# ---------------------------------------------------------------- Linux cron

def cron_line(paths, home=None):
    home = _home(home)
    wrapper = custom_wrapper(home)
    env = {"TW_REPO": str(CHECKOUT), "TW_CONFIG_DIR": str(paths.config),
           "TW_STATE_DIR": str(paths.state), "TW_OUT_DIR": str(paths.out)}
    for key in ("TREND_WATCH_PACKS_DIR", "XDG_CONFIG_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if wrapper:
        command = [str(wrapper)]
    else:
        command = [python_executable(), str(SCRIPT), "run"]
    prefix = " ".join("{}={}".format(k, shlex.quote(v)) for k, v in env.items())
    log = shlex.quote(str(paths.state / "logs" / "cron.log"))
    return "{} * * * * {} {} >> {} 2>&1 {}".format(",".join(map(str, MINUTES)), prefix,
                                                     " ".join(shlex.quote(c) for c in command), log, MARKER)


def read_crontab():
    result = call(["crontab", "-l"])
    return result.stdout.splitlines() if result.returncode == 0 else []


def write_crontab(lines):
    text = "\n".join(lines) + "\n" if lines else ""
    result = call(["crontab", "-"], input=text)
    if result.returncode != 0:
        raise OSError("crontab update failed: " + (result.stderr or "").strip())


def cron_enabled():
    return any(line.rstrip().endswith(MARKER) for line in read_crontab())


def cron_set(paths, on, home=None):
    lines = [line for line in read_crontab() if not line.rstrip().endswith(MARKER)]
    if on:
        lines.append(cron_line(paths, home))
    write_crontab(lines)


# ---------------------------------------------------------------- commands

def scheduler_on(system=None):
    return launchd_loaded() if system_name(system) == "darwin" else cron_enabled()


def enable(paths, home=None, system=None, out=print):
    (paths.state / "logs").mkdir(parents=True, exist_ok=True)
    if system_name(system) == "darwin":
        if not plist_path(home).exists():
            write_plist(paths, home)
        if launchd_loaded():
            out("Scheduler: already on ({})".format(SCHEDULE_TEXT))
            return 0
        ok, error = launchd_bootstrap(home)
        if not ok:
            out("Scheduler: could not be turned on ({})".format(error or "launchctl bootstrap failed"))
            return 1
    else:
        cron_set(paths, True, home)
    out("Scheduler: on ({})".format(SCHEDULE_TEXT))
    return 0


def disable(paths, home=None, system=None, out=print):
    if system_name(system) == "darwin":
        if launchd_loaded() and not launchd_bootout():
            out("Scheduler: could not be turned off (launchctl bootout failed)")
            return 1
    else:
        cron_set(paths, False, home)
    out("Scheduler: off")
    return 0


def install_scheduler(paths, home=None, system=None):
    """Write the scheduler definition; reload it if it was already running."""
    if system_name(system) == "darwin":
        changed = write_plist(paths, home)
        if changed and launchd_loaded():
            launchd_bootout()
            launchd_bootstrap(home)
        return plist_path(home), changed
    record = paths.state / "cron-line.txt"
    line = cron_line(paths, home)
    changed = not record.exists() or record.read_text(encoding="utf-8").strip() != line
    record.write_text(line + "\n", encoding="utf-8")
    if cron_enabled():
        cron_set(paths, True, home)
    return record, changed


def setup(enable_after=True, home=None, system=None, candidates=None, path_env=None, out=print):
    home = _home(home)
    paths = Paths.resolve(home)
    initialize(paths)
    (paths.state / "logs").mkdir(parents=True, exist_ok=True)
    out("Settings:  " + str(paths.config))
    out("Data:      " + str(paths.state))
    out("Outputs:   " + str(paths.out))
    link, message, on_path = install_link(home, candidates, path_env)
    if link is None:
        out("Command:   " + message + "; run it as " + str(SCRIPT))
        command = str(SCRIPT)
    else:
        out("Command:   {} ({})".format(link, message))
        command = "trend-watch"
        if not on_path:
            out("           That folder is not on your PATH yet; until it is, type the full path: " + str(link))
            command = str(link)
    where, changed = install_scheduler(paths, home, system)
    out("Schedule:  {} ({}; mode: {})".format(where, "written" if changed else "unchanged", mode_text(home)))
    code = 0
    if enable_after:
        code = enable(paths, home, system, out)
    else:
        out("Scheduler: left as it was ({}); turn on with: {} enable".format("on" if scheduler_on(system) else "off", command))
    out("")
    out("Setup done. Everyday commands:")
    out("  {} status     - is it running, is Jev on, what happened last".format(command))
    out("  {} jev off    - stop paid Jev calls (jev on to resume)".format(command))
    out("  {} disable    - stop the schedule (enable to restart)".format(command))
    out("  {} run        - run once now".format(command))
    return code


def _local(ts):
    return time.strftime("%a %d %b %Y, %H:%M", time.localtime(ts))


def last_run(paths):
    path = paths.state / "last-run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        pass
    db = paths.state / "state.db"
    if db.exists():
        import sqlite3
        try:
            conn = sqlite3.connect(str(db))
            try:
                row = conn.execute("SELECT ts, summary_json FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            if row:
                return {"ts": row[0], "ok": True, "summary": json.loads(row[1]).get("summary", "")}
        except (sqlite3.Error, ValueError):
            pass
    return None


def jev_enabled(paths):
    try:
        data = json.loads((paths.config / "config.json").read_text(encoding="utf-8"))
        return bool(data.get("llm", {}).get("enabled", True))
    except (OSError, ValueError, AttributeError):
        return True


def budget_line(paths, cfg):
    now = time.time()
    db_path = paths.state / "state.db"
    from .state import State, day_at
    b = {"day": day_at(now), "calls": 0, "input_tokens": 0, "draft_calls": 0}
    if db_path.exists():
        db = State(db_path, migrate=False)  # status is read-only: no migration, no journal-mode change
        try:
            b = db.budget(now)
        finally:
            db.close()
    return "{}: calls {}/{}, tokens {}/{}, draft_calls {} (included in calls)".format(
        b["day"], b["calls"], cfg["budget"]["max_calls_per_day"], b["input_tokens"],
        cfg["budget"]["max_input_tokens_per_day"], b["draft_calls"])


def status(paths, home=None, system=None, out=print):
    from .config import load_config
    on = scheduler_on(system)
    out("Scheduler: {} ({}; mode: {})".format("on" if on else "off", SCHEDULE_TEXT if on else "turn on with: trend-watch enable", mode_text(home)))
    out("Jev: " + ("on" if jev_enabled(paths) else "off"))
    run = last_run(paths)
    if run:
        out("Last run: {} - {} - {}".format(_local(run["ts"]), "ok" if run.get("ok") else "FAILED", run.get("summary", "")))
        for label, key in (("Digest written", "digest"), ("Digest queued", "queued")):
            if run.get(key):
                out("{}: {}".format(label, run[key]))
    else:
        out("Last run: never")
    try:
        logs = [p for p in (paths.state / "logs").iterdir() if p.is_file()]
        if logs:
            newest = max(logs, key=lambda p: p.stat().st_mtime)
            out("Latest log: {} ({})".format(newest, _local(newest.stat().st_mtime)))
    except OSError:
        pass
    try:
        out("Today's Jev budget: " + budget_line(paths, load_config(paths)))
    except (ValueError, OSError) as exc:
        out("Today's Jev budget: unavailable ({})".format(exc))
    out("Outputs: " + str(paths.out))
    try:
        queued = sorted((paths.state / "outbox").rglob("*.md"))
    except OSError:
        queued = []
    if queued:
        out("Waiting in outbox: {} file(s) in {} - `trend-watch flush` moves them to the outputs folder".format(len(queued), paths.state / "outbox"))
    return 0


def record_run(paths, ok, summary, digest=None, queued=None, now=None):
    data = {"ts": time.time() if now is None else now, "ok": ok, "summary": summary}
    if digest:
        data["digest"] = str(digest)
    if queued:
        data["queued"] = str(queued)
    paths.state.mkdir(parents=True, exist_ok=True)
    path = paths.state / "last-run.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
