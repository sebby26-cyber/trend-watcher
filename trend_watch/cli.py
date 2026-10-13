"""Repository-local CLI; no installation or third-party packages needed."""
import argparse
import json
import re
import sys
import time
from .config import Paths, initialize, load_config, load_domains
from .outputs import flush
from .pipeline import run
from .registry import discover
from .state import State, day_at
from .lock import LockBusy, state_lock
from . import setup as installer

# Exit code when another trend-watch process holds the state lock (EX_TEMPFAIL).
LOCK_BUSY = 75
# Commands that never write state, config or the scheduler.
READ_ONLY = {"status", "scan", "replay", "readiness", "records"}
# Commands meant for the scheduler fail fast instead of queueing behind a running tick.
FAIL_FAST = {"run", "tick", "ingest", "resolve", "annotate", "consume", "render"}


def parser():
    root = argparse.ArgumentParser(prog="trend-watch")
    root.add_argument("--packs-dir", action="append", default=[], help="pack search directory (repeatable)")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "flush", "budget", "enable", "disable", "status"):
        commands.add_parser(name)
    commands.add_parser("setup").add_argument("--no-enable", action="store_true", help="install everything but leave the scheduler as it is")
    tick = commands.add_parser("run")
    tick.add_argument("--domain")
    for flag in ("no-llm", "no-drafts", "dry-run", "verbose"):
        tick.add_argument("--" + flag, action="store_true")
    tick.add_argument("--max-candidates", type=int)
    domains = commands.add_parser("domains").add_subparsers(dest="operation", required=True)
    for op in ("list", "validate", "add", "archive"):
        sub = domains.add_parser(op)
        if op in ("add", "archive"):
            sub.add_argument("name")
    commands.add_parser("jev").add_argument("state", nargs="?", choices=("on", "off", "status"), default="status")
    sources = commands.add_parser("sources").add_subparsers(dest="operation", required=True)
    sources.add_parser("list")
    for name, text in (("tick", "ingest -> resolve -> annotate -> consume -> render under one lock"),
                       ("ingest", "fetch primary-source intake feeds into immutable observations"),
                       ("resolve", "observations -> events/revisions, watchlist match, intake admission"),
                       ("annotate", "bounded Jev questions for ambiguous filings (attempt state machine)"),
                       ("render", "regenerate the shadow Markdown report under the state directory")):
        commands.add_parser(name, help=text)
    consume = commands.add_parser("consume", help="evaluate admitted events for one consumer")
    consume.add_argument("name")
    consume.add_argument("--replay-batch", default="", help="force new evaluation requests tagged with this batch id")
    ack = commands.add_parser("ack", help="append an acknowledgement to a record (by record UID)")
    ack.add_argument("record_uid")
    ack.add_argument("--actor", default="owner")
    ack.add_argument("--note")
    ack.add_argument("--verdict", choices=("correct", "incorrect"))
    ack.add_argument("--correct", dest="corrected", choices=("REVIEW", "IGNORE", "INCOMPLETE"))
    ack.add_argument("--env")
    commands.add_parser("records", help="list active records (read-only)").add_argument("--all", action="store_true")
    scan = commands.add_parser("scan", help="digit-run tripwire over shadow outputs, feeds and R1 tables")
    scan.add_argument("paths", nargs="*")
    scan.add_argument("--legacy", action="store_true", help="also scan legacy state.db tables")
    replay = commands.add_parser("replay", help="offline fixture replay of investment@shadow (no network, no live Jev)")
    replay.add_argument("--fixtures")
    replay.add_argument("--json", action="store_true")
    commands.add_parser("readiness", help="readiness gate for production rendering (read-only)")
    commands.add_parser("migrate", help="apply pending forward-only state.db migrations (backup first)")
    commands.add_parser("backup", help="online backup of state.db to <state>/backups").add_argument("--dest")
    commands.add_parser("restore", help="archive the current state.db, then restore a backup").add_argument("backup_file")
    pack = commands.add_parser("pack", help="list or evaluate optional alert packs")
    pack.add_argument("name", help="pack name, or list")
    pack.add_argument("operation", nargs="?", choices=("run", "report", "harvest"))
    pack.add_argument("--packs-dir", action="append", default=argparse.SUPPRESS)
    pack.add_argument("--dry-run", action="store_true")
    pack.add_argument("--test-nudge", action="store_true")
    pack.add_argument("--json", action="store_true")
    pack.add_argument("--hours", type=float, default=24)
    return root


def mutating(args):
    if args.command in READ_ONLY:
        return False
    if args.command == "jev":
        return args.state != "status"
    if args.command == "domains":
        return args.operation in ("add", "archive")
    return True


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        paths = Paths.resolve()
        if args.command == "pack":
            return pack_command(args, paths)
        if not mutating(args):
            return dispatch(args, paths)
        # One shared bootstrap lock for every mutating entry point (including ack,
        # migrations, backup/restore, config toggles and scheduler changes).
        with state_lock(paths.state, wait_s=0 if args.command in FAIL_FAST else 30):
            return dispatch(args, paths)
    except LockBusy as exc:
        print("trend-watch: busy: {}".format(exc), file=sys.stderr)
        return LOCK_BUSY
    except (ValueError, OSError) as exc:
        print("trend-watch: " + str(exc), file=sys.stderr)
        return 1


def dispatch(args, paths):
    if args.command == "setup":
        return installer.setup(enable_after=not args.no_enable)
    paths.state.mkdir(parents=True, exist_ok=True)
    if args.command == "init":
        initialize(paths)
        print("Initialized " + str(paths.config))
        return 0
    if not (paths.config / "config.json").exists():
        initialize(paths)  # a fresh machine works without a separate init step
    if args.command == "enable":
        return installer.enable(paths)
    if args.command == "disable":
        return installer.disable(paths)
    if args.command == "status":
        return installer.status(paths)
    if args.command == "run":
        try:
            summary = run(paths, domain=args.domain, no_llm=args.no_llm, no_drafts=args.no_drafts, dry_run=args.dry_run, max_candidates=args.max_candidates, verbose=args.verbose, packs_dirs=args.packs_dir)
        except Exception as exc:
            if not args.dry_run:
                installer.record_run(paths, False, "{}: {}".format(type(exc).__name__, exc))
            raise
        if not args.dry_run:
            record_success(paths, summary)
        return 0
    if args.command == "jev":
        path = paths.config / "config.json"
        data = json.loads(path.read_text())
        llm = data.setdefault("llm", {})
        if args.state != "status":
            llm["enabled"] = args.state == "on"
            path.write_text(json.dumps(data, indent=2) + "\n")
        print("jev " + ("on" if llm.get("enabled", True) else "off"))
        return 0
    if args.command == "migrate":
        db = State(paths.state / "state.db")
        try:
            before, after, saved = db.migration
            print("state.db schema v{} -> v{}{}".format(before, after, "; pre-migration backup " + str(saved) if saved else ""))
        finally:
            db.close()
        return 0
    if args.command == "backup":
        from .backup import backup
        print(backup(paths, args.dest))
        return 0
    if args.command == "restore":
        from .backup import restore
        archived = restore(paths, args.backup_file)
        print("restored {}{}".format(args.backup_file, "; previous state archived at " + str(archived) if archived else ""))
        return 0
    if args.command in ENGINE_COMMANDS:
        return engine_command(args, paths)
    if args.command == "flush":
        moved, errors = flush(paths)
        print("Flushed {} queued files".format(moved))
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    cfg = load_config(paths)
    registry, registry_errors = discover(paths)
    domains, errors = load_domains(paths, cfg, registry, packs_dirs=args.packs_dir)
    errors = registry_errors + errors
    if args.command == "budget":
        db = State(paths.state / "state.db")
        try:
            b = db.budget(time.time())
            print("{}: calls {}/{}, tokens {}/{}, draft_calls {} (included in calls)".format(b["day"], b["calls"], cfg["budget"]["max_calls_per_day"], b["input_tokens"], cfg["budget"]["max_input_tokens_per_day"], b["draft_calls"]))
        finally:
            db.close()
    elif args.command == "sources":
        db = State(paths.state / "state.db")
        try:
            for name, adapter in sorted(registry.items()):
                row = db.source(name)
                interval = cfg["adapters"].get(name, {}).get("min_interval_s", getattr(adapter, "MIN_INTERVAL_S", 900))
                print("{}\t{}\t{}\tmin_interval={}s\t{}".format(name, adapter.ORIGIN, getattr(adapter, "NEEDS_KEY", False) or "free", interval, row["last_status"] if row else "never fetched"))
        finally:
            db.close()
    elif args.operation == "validate":
        archived, archived_errors = load_domains(paths, cfg, registry, archived=True)
        errors.extend(archived_errors)
        print("{} active-directory and {} archived valid domains".format(len(domains), len(archived)))
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    elif args.operation == "list":
        for label, rows in (("Domains", domains), ("Archived", load_domains(paths, cfg, registry, archived=True)[0])):
            print(label + ":")
            for d in rows:
                print("{}\tenabled={}\t{}\t{}\t{}".format(d["name"], d["enabled"], ",".join(d["sources"]), ",".join(d["lanes"]), d["file"]))
    elif args.operation == "add":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.name) or args.name == "none":
            raise ValueError("name must be a slug other than 'none'")
        if any(d["name"] == args.name for d in domains):
            raise ValueError("domain name already exists")
        target = paths.config / "domains" / (args.name + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        starter = {"_help": ["Edit keywords and sources; all omitted keys inherit config.json.", "Archive with: trend-watch domains archive " + args.name],
                   "name": args.name, "enabled": True, "keywords": [args.name], "sources": {"hackernews": {}}, "lanes": ["content"]}
        with target.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(starter, indent=2) + "\n")
        print(target)
    elif args.operation == "archive":
        matches = [d for d in domains if d["name"] == args.name]
        from pathlib import Path
        target = paths.config / "domains" / (args.name + ".json")
        if matches:
            source = Path(matches[0]["file"])
        elif re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.name) and target.is_file():
            source = target  # Also allow retiring an invalid file by its stem.
        else:
            raise ValueError("domain not found: " + args.name)
        destination = paths.config / "domains/archive" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("archive already exists: " + str(destination))
        source.rename(destination)
        print(destination)
    for error in errors:
        print(error, file=sys.stderr)
    return 0


ENGINE_COMMANDS = {"tick", "ingest", "resolve", "annotate", "consume", "render", "ack", "records", "scan", "replay", "readiness"}


def engine_command(args, paths):
    from . import engine
    from .consumers import REGISTRY
    if args.command == "replay":
        from .replay import format_report, run_replay
        report = run_replay(args.fixtures)
        print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2) if args.json else format_report(report))
        return 0 if report["passed"] else 1
    cfg = load_config(paths)
    if args.command == "scan":
        from pathlib import Path
        from .tripwire import scan_paths
        targets = [Path(p) for p in args.paths] or [paths.state / "shadow", paths.state / "feeds", paths.state / "state.db"]
        hits = scan_paths(cfg, files=[t for t in targets if t.suffix != ".db"], databases=[t for t in targets if t.suffix == ".db"], legacy=args.legacy)
        for hit in hits:
            print("{where}: {digits}-digit run".format(**hit))
        print("tripwire: {} hit(s)".format(len(hits)))
        return int(bool(hits))
    if args.command == "readiness":
        from .readiness import readiness
        ready, lines = readiness(paths, cfg)
        print("\n".join(lines))
        return 0 if ready else 1
    readonly = args.command == "records"
    if readonly and not (paths.state / "state.db").exists():
        print("(no records)")
        return 0
    db = State(paths.state / "state.db", migrate=not readonly)
    try:
        if readonly:
            if not db.has_table("records"):
                print("(no records)")
                return 0
            query = "SELECT r.uid, r.consumer, r.env, r.decision, r.reason_code, r.created_at, r.body_json FROM records r"
            if not args.all:
                query += " JOIN record_heads h ON h.record_id=r.id"
            for row in db.execute(query + " ORDER BY r.id"):
                body = json.loads(row["body_json"])
                print("\t".join([row["uid"], row["consumer"] + "@" + row["env"], row["decision"], row["reason_code"], row["created_at"],
                                 "{} {} {}".format(body.get("cik"), body.get("form"), body.get("accession"))]))
            return 0
        if args.command == "ack":
            from .records import ack
            message, _ = ack(db, args.record_uid, args.actor, time.time(), args.note, args.verdict, args.corrected, args.env)
            print(message)
            return 0
        ctx = engine.Context(paths, cfg)
        if args.command == "tick":
            results = engine.tick(db, ctx)
        elif args.command == "consume":
            if args.name not in REGISTRY:
                raise ValueError("unknown consumer {} (known: {})".format(args.name, ", ".join(sorted(REGISTRY))))
            ctx.replay_batch = args.replay_batch
            results = engine.run_named(db, ctx, "consume", args.name)
        else:
            results = engine.run_named(db, ctx, args.command)
        failed = False
        for stage, (status, counts) in results.items():
            detail = ", ".join("{}={}".format(k, v) for k, v in sorted(counts.items()))
            print("{}: {}{}".format(stage, status, " (" + detail + ")" if detail else ""))
            failed = failed or status == "failed"
        return 2 if failed else 0
    finally:
        db.close()


def record_success(paths, summary):
    digest = queued = None
    if summary["candidates"]:
        relative = "digests/" + day_at(summary.get("ts", time.time())) + ".md"
        if any(w.startswith("WARN: output queued in outbox: " + relative) for w in summary["warnings"]):
            queued = paths.state / "outbox" / relative
        else:
            digest = paths.out / relative
    text = summary["summary"]
    if summary["errors"]:
        text += " ({} source errors)".format(len(summary["errors"]))
    if summary.get("no_llm_reason"):
        text += "; no Jev: " + summary["no_llm_reason"]
    installer.record_run(paths, True, text, digest=digest, queued=queued)


def pack_command(args, paths):
    from . import packs
    if args.name == "list":
        if args.operation:
            raise ValueError("pack list takes no operation")
        found = packs.discover(paths, args.packs_dir)
        if args.json:
            print(json.dumps({name: str(path) for name, path in found.items()}, indent=2))
        else:
            for name, path in found.items():
                print("{}\t{}".format(name, path))
            if not found:
                print("No packs found.")
        return 0
    if not args.operation:
        raise ValueError("specify pack <name> run, report or harvest")
    try:
        if args.operation == "run":
            out = packs.tick(paths, args.name, dry_run=args.dry_run, test_nudge=args.test_nudge,
                             packs_dirs=args.packs_dir)
            if args.json:
                print(json.dumps(out, indent=2, sort_keys=True))
            else:
                for story in out.get("top", []):
                    print("{score:.3f} | {domain} | {headline} | {gates}".format(**story))
                for nudge in out.get("nudged", []):
                    print("nudge {kind} {status}: {headline} ({file})".format(**nudge))
                print("{}: {} stories, {} eligible, {} nudged, bar {}{}".format(
                    args.name, out.get("stories", 0), out.get("eligible", 0), len(out["nudged"]),
                    out.get("bar", "-"), " (" + out["note"] + ")" if out.get("note") else ""))
            return 0
        cfg = packs.load(paths, args.name, args.packs_dir)
        root = packs.home(paths, args.name)
        with packs.locked(root):
            db = packs.own_db(root)
            try:
                now = time.time()
                if args.operation == "report":
                    print(packs.report_text(db, cfg, now - args.hours * 3600, now), end="")
                else:
                    print("Verdicts harvested: {}".format(packs.harvest(db, cfg, now)))
            finally:
                db.close()
    except BlockingIOError:
        print("trend-watch pack: busy (another process holds the pack lock)", file=sys.stderr)
        return LOCK_BUSY
    return 0
