"""File decay for the trend-watch state dir: ``trend-watch decay [--apply]``.

Policy: ``<config>/decay.json`` if present, else ``seed/decay.json``. Dry-run by
default: it only lists what each rule would remove and the bytes it would free.
``--apply`` removes the eligible entries and appends one JSON line per removal
(and one summary line) to ``<state>/logs/decay.jsonl``.

Scope: files and directories under the state dir only. Database rows are not this
module's job (State.prune handles them through config ``retention``). Every
resolved target must sit strictly inside the rule's directory, inside the state
dir, must not be a symlink, and must not be or contain a ``never`` path.
"""
import json
import shutil
import time
from pathlib import Path

from .config import ROOT
DAY = 86400


def load_policy(paths):
    for candidate in (paths.config / "decay.json", ROOT / "seed/decay.json"):
        if candidate.exists():
            policy = json.loads(candidate.read_text(encoding="utf-8"))
            validate(policy)
            return policy, candidate
    raise ValueError("no decay policy (seed/decay.json missing)")


def validate(policy):
    if not isinstance(policy.get("rules"), list):
        raise ValueError("decay policy: rules must be a list")
    for rule in policy["rules"]:
        for key in ("name", "path", "glob", "kind", "max_age_days", "keep_last"):
            if key not in rule:
                raise ValueError("decay rule {} lacks {}".format(rule.get("name", "?"), key))
        if rule["kind"] not in ("file", "dir"):
            raise ValueError("decay rule {}: kind must be file or dir".format(rule["name"]))
        rel = Path(rule["path"])
        if rel.is_absolute() or ".." in rel.parts or rule["path"] in ("", "."):
            raise ValueError("decay rule {}: path must be a subdirectory of the state dir".format(rule["name"]))
        if int(rule["max_age_days"]) < 1 or int(rule["keep_last"]) < 0:
            raise ValueError("decay rule {}: max_age_days >= 1 and keep_last >= 0".format(rule["name"]))


def size_of(path):
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.lstat().st_size
        except OSError:
            pass
    return total


def protected(target, state, never):
    for item in never:
        guard = (state / item).resolve()
        if target == guard or guard in target.parents or target in guard.parents:
            return True
    return False


def plan(paths, policy, now=None):
    """Return [(rule, [(path, bytes, mtime), ...]), ...] of eligible entries."""
    now = time.time() if now is None else now
    state = paths.state.resolve()
    never = policy.get("never", [])
    result = []
    for rule in policy["rules"]:
        base = state / rule["path"]
        picked = []
        if base.is_dir() and not base.is_symlink():
            entries = []
            for entry in base.glob(rule["glob"]):
                if entry.is_symlink():
                    continue
                if rule["kind"] == "file" and not entry.is_file():
                    continue
                if rule["kind"] == "dir" and not entry.is_dir():
                    continue
                entries.append((entry.lstat().st_mtime, entry))
            entries.sort(reverse=True)  # newest first
            for index, (mtime, entry) in enumerate(entries):
                if index < int(rule["keep_last"]):
                    continue
                if now - mtime <= int(rule["max_age_days"]) * DAY:
                    continue
                target = entry.resolve()
                if target.parent != base.resolve() or state not in target.parents:
                    continue
                if protected(target, state, never):
                    continue
                picked.append((entry, size_of(entry), mtime))
        result.append((rule, picked))
    return result


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.1f}{}".format(n, unit) if unit != "B" else "{}B".format(n)
        n /= 1024.0


def decay(paths, apply=False, now=None, out=print, sample=3):
    policy, source = load_policy(paths)
    now = time.time() if now is None else now
    planned = plan(paths, policy, now)
    mode = "apply" if apply else "dry-run"
    out("trend-watch decay ({}) policy={} state={}".format(mode, source, paths.state))
    log = None
    if apply:
        (paths.state / "logs").mkdir(parents=True, exist_ok=True)
        log = (paths.state / "logs" / "decay.jsonl").open("a", encoding="utf-8")
    total_n = total_b = 0
    try:
        for rule, picked in planned:
            freed = sum(b for _, b, _ in picked)
            total_n += len(picked)
            total_b += freed
            out("  {:<22} {:>5} entries {:>10}  (>{}d, keep_last {})".format(
                rule["name"], len(picked), human(freed), rule["max_age_days"], rule["keep_last"]))
            for entry, size, _ in picked[:sample]:
                out("      e.g. {} ({})".format(entry, human(size)))
            if apply:
                for entry, size, mtime in picked:
                    try:
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                        status = "removed"
                    except OSError as exc:
                        status = "error: {}".format(exc)
                    log.write(json.dumps({"ts": now, "rule": rule["name"], "path": str(entry),
                                          "bytes": size, "mtime": mtime, "status": status}) + "\n")
        out("  total {} entries {}".format(total_n, human(total_b)))
        if log is not None:
            log.write(json.dumps({"ts": now, "summary": True, "entries": total_n, "bytes": total_b}) + "\n")
    finally:
        if log is not None:
            log.close()
    return {"entries": total_n, "bytes": total_b, "applied": apply}
