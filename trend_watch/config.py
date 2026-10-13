"""Fresh configuration, path resolution and independently validated domains."""
import copy
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Paths:
    config: Path
    state: Path
    out: Path

    @classmethod
    def resolve(cls, home=None):
        """Explicit TW_* roots, then TREND_WATCH_HOME, then XDG defaults.

        TREND_WATCH_HOME supplies config/, the state root itself, and out/.
        Without it, output defaults to the resolved state root's out/ directory.
        """
        home = Path(home) if home is not None else Path.home()

        def env_path(key, fallback=None):
            return Path(os.environ[key]).expanduser() if os.environ.get(key) else fallback

        common = env_path("TREND_WATCH_HOME")
        config = env_path("TW_CONFIG_DIR", common / "config" if common else
                          env_path("XDG_CONFIG_HOME", home / ".config") / "trend-watch")
        state = env_path("TW_STATE_DIR", common or
                         env_path("XDG_DATA_HOME", home / ".local/share") / "trend-watch")
        out = env_path("TW_OUT_DIR", (common or state) / "out")
        return cls(config, state, out)


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def defaults():
    return json.loads((ROOT / "seed/config.json").read_text(encoding="utf-8"))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_shape(data, template, path="config"):
    if not isinstance(data, dict):
        raise ValueError(path + " must be an object")
    for key, value in data.items():
        if key == "_help":
            continue
        label = path + "." + key
        if key not in template:
            raise ValueError("unknown key " + label)
        expected = template[key]
        if isinstance(expected, dict):
            # Option/template maps and adapter parameters are intentionally extensible.
            if key in ("sources", "adapters", "reasons", "angles", "templates", "tones", "exempt_fields"):
                if not isinstance(value, dict):
                    raise ValueError(label + " must be an object")
            else:
                validate_shape(value, expected, label)
        elif isinstance(expected, bool):
            if type(value) is not bool:
                raise ValueError(label + " must be boolean")
        elif number(expected):
            integer_keys = {"max_candidates", "max_per_day", "max_calls_per_day",
                            "max_input_tokens_per_day", "retries", "failure_threshold",
                            "legacy_reserve_calls", "max_bytes", "max_decompressed_bytes", "max_elements",
                            "max_depth", "max_admissions_per_day", "baseline_days", "min_history_days",
                            "min_trip_count", "absolute_ceiling", "max_ambiguous_attempts",
                            "estimated_input_tokens_per_call", "min_shadow_days", "min_spot_checks",
                            "min_digits", "max_digits"}
            if not number(value) or value < 0 or (key in integer_keys and type(value) is not int):
                raise ValueError(label + " must be a nonnegative " + type(expected).__name__)
        elif isinstance(expected, list):
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(label + " must be a list of nonempty strings")
        elif not isinstance(value, str):
            raise ValueError(label + " must be a string")


def validate_settings(cfg):
    if not 0 <= cfg["thresholds"]["candidate_score"] <= 1:
        raise ValueError("candidate_score must be in [0,1]")
    if not 0 <= cfg["llm"]["min_confidence"] <= 1:
        raise ValueError("min_confidence must be in [0,1]")
    if cfg["freshness"]["hard_max_age_h"] < cfg["freshness"]["max_age_h"]:
        raise ValueError("hard_max_age_h must be >= max_age_h")
    if sum(cfg["weights"].values()) <= 0:
        raise ValueError("weights must have a positive sum")
    for key in ("reasons", "angles"):
        mapping = cfg["llm"][key]
        if not mapping or len(mapping) > 255 or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise ValueError(key + " must contain 1..255 string options")
    if cfg["drafts"]["backend"] not in ("template", "external"):
        raise ValueError("unknown drafter backend")
    if any(p not in ("x", "threads", "linkedin", "blog") for p in cfg["drafts"]["platforms"]):
        raise ValueError("unknown draft platform")
    for p in cfg["drafts"]["platforms"]:
        options = cfg["drafts"]["templates"].get(p)
        if not isinstance(options, dict) or not options or len(options) > 255 or not all(isinstance(v, str) for v in options.values()):
            raise ValueError("missing/invalid draft templates: " + p)
    tones = cfg["drafts"]["tones"]
    if not tones or len(tones) > 255 or not all(isinstance(v, str) for v in tones.values()):
        raise ValueError("invalid draft tones")
    for params in [cfg["http"]] + list(cfg.get("adapters", {}).values()) + list(cfg["sources"].values()):
        if not isinstance(params, dict):
            raise ValueError("source parameters and adapter defaults must be objects")
        for k in ("timeout_s", "min_interval_s", "request_interval_s", "retries", "backoff_s", "failure_threshold", "cooldown_s", "max_cooldown_s", "limit", "days"):
            if k in params and (not number(params[k]) or params[k] < 0):
                raise ValueError("invalid adapter setting " + k)
        for k in ("limit", "retries", "failure_threshold"):
            if k in params and type(params[k]) is not int:
                raise ValueError(k + " must be an integer")
        if "timeout_s" in params and params["timeout_s"] <= 0:
            raise ValueError("timeout_s must be positive")
        if "user_agent" in params and (not isinstance(params["user_agent"], str) or not params["user_agent"].strip()):
            raise ValueError("user_agent must be a nonempty string")
        for k in ("subreddits", "listings", "queries", "topics", "categories", "channel_ids", "playlist_ids"):
            if k in params and (not isinstance(params[k], list) or not all(isinstance(v, str) and v for v in params[k])):
                raise ValueError(k + " must be a list of nonempty strings")
        if "geo" in params and not (isinstance(params["geo"], str) or isinstance(params["geo"], list) and all(isinstance(v, str) for v in params["geo"])):
            raise ValueError("geo must be a string or list of strings")


EXEMPT_TYPES = ("edgar-accession-dashed", "digest-uid", "hex-digest", "url")


def validate_r1(cfg):
    """R1 sections: intake adapters, the investment@shadow consumer, budgets and the tripwire."""
    from urllib.parse import urlsplit
    edgar = cfg["intake"]["sec_edgar"]
    hosts = edgar["allowed_hosts"]
    if not hosts or any(h != h.lower() or not (h == "sec.gov" or h.endswith(".sec.gov")) for h in hosts):
        raise ValueError("intake.sec_edgar.allowed_hosts must be lowercase sec.gov hosts")
    feed = urlsplit(edgar["feed_url"])
    if feed.scheme != "https" or (feed.hostname or "") not in hosts:
        raise ValueError("intake.sec_edgar.feed_url must be https on an allowed host")
    if edgar["request_interval_s"] < 0.1:
        raise ValueError("intake.sec_edgar.request_interval_s must be >= 0.1 (SEC fair access: <= 10 requests/second)")
    if not edgar["user_agent"].strip() or edgar["timeout_s"] <= 0:
        raise ValueError("intake.sec_edgar needs a user_agent and a positive timeout_s")
    for key in ("max_bytes", "max_decompressed_bytes", "max_elements", "max_depth"):
        if edgar[key] < 1:
            raise ValueError("intake.sec_edgar." + key + " must be positive")
    inv = cfg["investment"]
    if inv["env"] != "shadow":
        raise ValueError("investment.env must be 'shadow' in R1 (production rendering is an owner decision)")
    if not inv["watchlist_path"].strip():
        raise ValueError("investment.watchlist_path must be nonempty")
    if inv["circuit_breaker"]["multiplier"] < 1 or inv["circuit_breaker"]["baseline_days"] < 1:
        raise ValueError("investment.circuit_breaker multiplier and baseline_days must be >= 1")
    if not 1 <= inv["materiality"]["max_ambiguous_attempts"] <= 2:
        raise ValueError("investment.materiality.max_ambiguous_attempts must be 1 or 2")
    if not 0 <= inv["readiness"]["min_review_precision"] <= 1:
        raise ValueError("investment.readiness.min_review_precision must be in [0,1]")
    trip = cfg["tripwire"]
    if not 1 <= trip["min_digits"] <= trip["max_digits"]:
        raise ValueError("tripwire digits must satisfy 1 <= min_digits <= max_digits")
    for name, kind in trip["exempt_fields"].items():
        if kind not in EXEMPT_TYPES:
            raise ValueError("tripwire.exempt_fields.{} has unknown type {}".format(name, kind))
        if name in trip["never_exempt"]:
            raise ValueError("tripwire field {} cannot be both exempt and never_exempt".format(name))


def load_config(paths):
    base = defaults()
    path = paths.config / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    validate_shape(raw, base)
    cfg = merge(base, raw)
    validate_settings(cfg)
    validate_r1(cfg)
    return cfg


DOMAIN_KEYS = ("enabled", "keywords", "exclude", "sources", "lanes", "tickers", "thresholds",
               "drafts", "freshness", "weights", "max_candidates")


def load_domains(paths, cfg, registry, archived=False, packs_dirs=None):
    folder = paths.config / "domains"
    if archived:
        folder = folder / "archive"
    domains, errors, names = [], [], set()
    template = {k: cfg[k] for k in DOMAIN_KEYS}
    template["name"] = ""
    files = {p.stem: p for p in sorted(folder.glob("*.json"))}
    if not archived:
        from . import packs
        for name, pack_file in packs.discover(paths, packs_dirs).items():
            try:
                pack = packs.load(paths, name, packs_dirs)
                if not pack["enabled"]:
                    continue
                for filename in pack["domain_files"]:
                    path = Path(filename)
                    # Live domain edits and explicit retirement retain precedence.
                    if not (folder / "archive" / path.name).exists():
                        files.setdefault(path.stem, path)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                errors.append("{}: {}".format(pack_file, exc))
    for path in sorted(files.values(), key=lambda p: p.name):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            validate_shape(raw, template, path.name)
            domain = merge({k: cfg[k] for k in DOMAIN_KEYS}, raw)
            domain["name"] = raw.get("name", path.stem)
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", domain["name"]) or domain["name"] == "none":
                raise ValueError("name must be a slug other than 'none'")
            if domain["name"] in names:
                raise ValueError("duplicate domain name " + domain["name"])
            if any(l not in ("content", "trading") for l in domain["lanes"]):
                raise ValueError("lanes must contain content/trading")
            for name, params in domain["sources"].items():
                if name not in registry:
                    raise ValueError("unknown adapter " + name)
                if not isinstance(params, dict):
                    raise ValueError("source parameters must be objects")
            validate_settings(merge(cfg, domain))
            names.add(domain["name"])
            domain["file"] = str(path)
            domains.append(domain)
        except (ValueError, OSError, TypeError, OverflowError, RecursionError) as exc:
            errors.append("{}: {}".format(path, exc))
    return domains, errors


def initialize(paths):
    for folder in (paths.config, paths.config / "domains/archive", paths.config / "adapters"):
        folder.mkdir(parents=True, exist_ok=True)
    for src, dest in [(ROOT / "seed/config.json", paths.config / "config.json")] + [
            (p, paths.config / "domains" / p.name) for p in sorted((ROOT / "seed/domains").glob("*.json"))]:
        # A retired seed stays retired across repeated installs.
        if dest.parent.name == "domains" and (dest.parent / "archive" / dest.name).exists():
            continue
        try:
            with dest.open("x", encoding="utf-8") as stream:
                stream.write(src.read_text(encoding="utf-8"))
        except FileExistsError:
            pass
