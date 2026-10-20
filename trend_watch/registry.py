"""Adapter discovery, parameter union, origin ownership and failure isolation."""
import importlib
import importlib.util
import json
import pkgutil
from . import adapters
from .config import merge, number
from .http import HTTPContext


def discover(paths):
    registry, errors = {}, []
    for info in pkgutil.iter_modules(adapters.__path__):
        module = importlib.import_module("trend_watch.adapters." + info.name)
        if hasattr(module, "NAME"):
            module.ORIGIN = "built-in"
            registry[module.NAME] = module
    for path in sorted((paths.config / "adapters").glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location("tw_dropin_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            # Read source each run, even after an edit with the same mtime/size.
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            if not isinstance(module.NAME, str) or not module.NAME or not callable(module.fetch):
                raise ValueError("adapter needs NAME and fetch")
            interval = getattr(module, "MIN_INTERVAL_S", 900)
            if not number(interval) or interval < 0:
                raise ValueError("MIN_INTERVAL_S must be nonnegative")
            if module.NAME in registry:
                raise ValueError("duplicate adapter name " + module.NAME)
            module.ORIGIN = "drop-in"
            registry[module.NAME] = module
        except Exception as exc:
            errors.append("{}: {}".format(path, exc))
    return registry, errors


def merge_params(domains, cfg):
    merged, origins = {}, {}
    for domain in domains:
        for name, raw in domain["sources"].items():
            params = merge(cfg["adapters"].get(name, {}), raw)
            target = merged.setdefault(name, {})
            ownership = origins.setdefault(name, {})
            for key, value in params.items():
                if isinstance(value, list):
                    target.setdefault(key, [])
                    if not isinstance(target[key], list):
                        target[key] = [target[key]]
                    for v in value:
                        if v not in target[key]:
                            target[key].append(v)
                        ownership.setdefault(key, {}).setdefault(str(v), []).append(domain["name"])
                elif key == "geo":
                    target.setdefault(key, [])
                    if value not in target[key]:
                        target[key].append(value)
                    ownership.setdefault(key, {}).setdefault(str(value), []).append(domain["name"])
                else:
                    # Scalars use deterministic first-domain precedence; list parameters union.
                    target.setdefault(key, value)
    return merged, origins


def fetch_all(domains, cfg, registry, db, now, context_factory=HTTPContext):
    params, origins = merge_params(domains, cfg)
    items, statuses, errors = [], {}, []
    for name, values in params.items():
        module = registry[name]
        settings = dict(cfg["http"], min_interval_s=getattr(module, "MIN_INTERVAL_S", cfg["http"]["min_interval_s"]))
        settings = merge(settings, cfg["adapters"].get(name, {}))
        settings = merge(settings, {k: v for k, v in values.items() if k in cfg["http"] or k in ("min_interval_s", "request_interval_s")})
        previous = db.source(name)
        status = None
        if getattr(module, "NEEDS_KEY", False):
            status = "stubbed: " + str(module.NEEDS_KEY)
        elif previous and previous["cooldown_until"] > now:
            status = "cooldown"
        elif previous and previous["last_fetch"] is not None and now - previous["last_fetch"] < settings.get("min_interval_s", getattr(module, "MIN_INTERVAL_S", 900)):
            status = "rate-floor"
        if status:
            statuses[name] = status
            db.source_status(name, status)
            continue
        ctx = context_factory(settings, origins.get(name, {}))
        try:
            result = module.fetch(values, ctx)
            if not isinstance(result, list):
                raise ValueError("fetch must return a list")
            for row in result:
                if not valid_item(row):
                    ctx.errors.append("malformed item skipped")
                    continue
                items.append(dict(row, snippet=row.get("snippet") or "", extra=row.get("extra") or {}, platform=name))
            status = ctx.status
            if ctx.errors:
                errors.extend(name + ": " + e for e in ctx.errors)
                status = "partial" if result or ctx.status == "partial" else "error"
        except Exception as exc:
            status = "error"
            errors.append("{}: {}".format(name, type(exc).__name__))
        db.record_source(name, now, status, settings)
        statuses[name] = status
    return items, statuses, errors


def valid_item(row):
    """Malformed source data must never break downstream scoring or JSON output."""
    if not isinstance(row, dict):
        return False
    if not all(isinstance(row.get(k), str) and row[k].strip() for k in ("title", "url")):
        return False
    if row.get("score") is not None and not number(row["score"]):
        return False
    if row.get("rank") is not None and (type(row["rank"]) is not int or row["rank"] < 1):
        return False
    if row.get("snippet") is not None and not isinstance(row["snippet"], str):
        return False
    if row.get("extra") is not None and not isinstance(row["extra"], dict):
        return False
    hint = row.get("domain_hint", [])
    if not isinstance(hint, str) and not (isinstance(hint, list) and all(isinstance(v, str) for v in hint)):
        return False
    try:
        json.dumps(row, allow_nan=False)
    except (ValueError, TypeError):
        return False
    return True
