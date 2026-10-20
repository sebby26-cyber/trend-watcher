import _isolation  # noqa: F401  (must precede trend_watch imports)
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trend_watch import packs
from trend_watch.config import Paths

NOW = 1800000000.0

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/packs"


def install_fixtures(folder):
    folder.mkdir(parents=True)
    (folder / "domains").mkdir()
    for name, domain, title, label, groups, terms, markers in (
        ("market", "market-crypto", "Market", "Crypto", [["bitcoin", "crypto"]],
         ["liquidations", "billion", "bitcoin"], ["billion", "plunge", "liquidat"]),
        ("technology", "technology-ai-models", "Technology", "AI models", [["openai"], ["model"]],
         ["openai", "open-weight", "model", "coding agent"], ["release", "launch", "open-weight", "coding", "agent"]),
    ):
        cfg = json.loads((EXAMPLES / "example.json").read_text())
        cfg.update(title=title, domains=[domain], labels={domain: label}, relevance={domain: groups},
                   strong_terms=terms, persona_markers=markers)
        cfg["outlets"] = {"tier1": ["reuters", "bloomberg", "coindesk", "the verge", "techcrunch"],
                          "tier2": ["yahoo finance", "engadget"]}
        cfg["nudge"]["action"] = name + "-trend-alert"
        if name == "technology":
            cfg.update(defer_to=["market"], finance_first_terms=["shares"], finance_domain_terms=["acquires"])
            cfg["tuning"].update(enabled=True, peer="editor")
        (folder / (name + ".json")).write_text(json.dumps(cfg))
        (folder / "domains" / (domain + ".json")).write_text(json.dumps({"name": domain}))


def make_state(state_dir, rows, run_ts=NOW):
    state_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(state_dir / "state.db"))
    con.executescript("""CREATE TABLE items (hash TEXT PRIMARY KEY, title_hash TEXT, title TEXT, url TEXT, platform TEXT,
        first_seen REAL, last_seen REAL, published REAL, domains TEXT, candidate_at REAL, llm_at REAL, data_json TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL, summary_json TEXT);""")
    con.execute("INSERT INTO runs(ts,summary_json) VALUES (?,?)", (run_ts, "{}"))
    for i, (title, platform, age_min, domains, extra) in enumerate(rows):
        pub = NOW - age_min * 60
        con.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("h%d" % i, "t%d" % i, title, "https://example.com/%d" % i, platform, pub, run_ts, pub,
                     json.dumps(domains), None, None, json.dumps({"extra": extra})))
    con.commit()
    con.close()


BREAKING = [
    ("Bitcoin plunges 9% as $2 billion in crypto liquidations hit leveraged traders - Reuters", "google_news", 40, ["market-crypto"], {}),
    ("Bitcoin crypto liquidations top $2 billion as leveraged traders hit - Bloomberg", "google_news", 35, ["market-crypto"], {}),
    ("Leveraged traders hit by $2 billion bitcoin liquidations - CoinDesk", "google_news", 20, ["market-crypto"], {}),
    ("Bitcoin liquidations: leveraged traders lose $2 billion - Yahoo Finance", "google_news", 15, ["market-crypto"], {}),
    ("Bitcoin (BTC) -4.0% 1h, -9.1% 24h", "coingecko_markets", 0, [], {"symbol": "BTC", "coin_id": "bitcoin", "pct_1h": -4.0, "pct_24h": -9.1}),
]


class MarketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.paths = Paths(base / "config", base / "state", base / "out")
        self.paths.config.mkdir(parents=True)
        install_fixtures(base / "packs")
        self.env = patch.dict(os.environ, {"TREND_WATCH_PACKS_DIR": str(base / "packs"), "HOME": str(base)}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_breaking_story_nudges_once_then_dedups(self):
        make_state(self.paths.state, BREAKING)
        out = packs.tick(self.paths, pack="market", now=NOW)
        self.assertEqual(out["eligible"], 1, out["top"])
        self.assertEqual(len(out["nudged"]), 1)
        self.assertEqual(out["nudged"][0]["status"], "outbox-only")
        text = Path(out["nudged"][0]["file"]).read_text()
        self.assertIn("Market trend alert — Crypto", text)
        self.assertIn("BTC -4.0% 1h", text)
        again = packs.tick(self.paths, pack="market", now=NOW + 1800)
        self.assertEqual(again["nudged"], [])
        self.assertTrue(any(g.startswith("dup-") for g in again["top"][0]["gates"]))

    def test_old_or_thin_story_is_held(self):
        rows = [(t, p, age + 300, d, e) for t, p, age, d, e in BREAKING[:4]]
        make_state(self.paths.state, rows)
        out = packs.tick(self.paths, pack="market", now=NOW)
        self.assertEqual(out["nudged"], [])
        make_state(Path(self.tmp.name) / "s2", BREAKING[:1])
        self.paths.state = Path(self.tmp.name) / "s2"
        self.assertEqual(packs.tick(self.paths, pack="market", now=NOW)["nudged"], [])

    def test_dry_run_writes_nothing(self):
        make_state(self.paths.state, BREAKING)
        out = packs.tick(self.paths, pack="market", now=NOW, dry_run=True)
        self.assertEqual(out["nudged"], [])
        self.assertEqual(list((self.paths.state / "market/outbox").iterdir()), [])

    def test_command_receives_file_and_failure_is_kept(self):
        make_state(self.paths.state, BREAKING)
        (self.paths.config / "market.json").write_text(json.dumps(
            {"nudge": {"command": [sys.executable, "-c", "import sys; sys.exit(3)", "{marker}", "{action}", "{file}"]}}))
        out = packs.tick(self.paths, pack="market", now=NOW)
        self.assertEqual(out["nudged"][0]["status"], "failed")
        self.assertTrue(Path(out["nudged"][0]["file"]).exists())
        # A failed hand-off does not count as nudged: the next run retries.
        (self.paths.config / "market.json").write_text(json.dumps(
            {"nudge": {"command": [sys.executable, "-c", "pass", "{file}"]}}))
        self.assertEqual(packs.tick(self.paths, pack="market", now=NOW + 60)["nudged"][0]["status"], "sent")


TECH = [
    ("OpenAI releases open-weight reasoning model for coding agents - Reuters", "google_news", 40, ["technology-ai-models"], {}),
    ("OpenAI open-weight reasoning model launch targets coding agents - The Verge", "google_news", 35, ["technology-ai-models"], {}),
    ("OpenAI launches open-weight reasoning model built for coding agents - TechCrunch", "google_news", 20, ["technology-ai-models"], {}),
    ("OpenAI open-weight reasoning model for coding agents is out - Engadget", "google_news", 15, ["technology-ai-models"], {}),
]


class TechnologyPackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.paths = Paths(base / "config", base / "state", base / "out")
        self.paths.config.mkdir(parents=True)
        install_fixtures(base / "packs")
        self.env = patch.dict(os.environ, {"TREND_WATCH_PACKS_DIR": str(base / "packs"), "HOME": str(base)}, clear=True)
        self.env.start()
        self.link = base / "link"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_tech_story_nudges_in_own_pack(self):
        make_state(self.paths.state, TECH)
        out = packs.tick(self.paths, now=NOW, pack="technology")
        self.assertEqual(len(out["nudged"]), 1, out["top"])
        f = Path(out["nudged"][0]["file"])
        self.assertEqual(f.parent.parent, self.paths.state / "technology")
        self.assertTrue(f.name.startswith("technology-alert-"))
        self.assertIn("Technology trend alert — AI models", f.read_text())
        self.assertTrue((self.paths.state / "technology" / "technology.db").exists())
        self.assertFalse((self.paths.state / "market").exists())

    def test_market_alert_wins(self):
        make_state(self.paths.state, TECH)
        root = packs.home(self.paths, "market")
        db = packs.own_db(root)
        db.execute("INSERT INTO nudges(ts,kind,status,domain,headline,score,signature,hashes,file,detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (NOW - 600, "alert", "sent", "market-ai-finance", "x", 0.8, "[]", json.dumps(["h0"]), "f", ""))
        db.commit()
        db.close()
        out = packs.tick(self.paths, now=NOW, pack="technology")
        self.assertEqual(out["nudged"], [])
        self.assertTrue(any(g.startswith("owned-by-market") for g in out["top"][0]["gates"]))

    def test_finance_first_is_held(self):
        rows = [(t.replace("OpenAI", "Nvidia shares jump as OpenAI"), p, a, d, e) for t, p, a, d, e in TECH]
        make_state(self.paths.state, rows)
        out = packs.tick(self.paths, now=NOW, pack="technology")
        self.assertEqual(out["nudged"], [])
        self.assertTrue(any(g.startswith("finance-first") for g in out["top"][0]["gates"]))

    def test_ma_is_held_on_any_headline(self):
        rows = list(TECH)
        t, pl, a, d, e = rows[-1]
        rows[-1] = ("Microsoft acquires coding agent startup behind OpenAI open-weight model - Engadget", pl, a, d, e)
        make_state(self.paths.state, rows)
        out = packs.tick(self.paths, now=NOW, pack="technology")
        self.assertEqual(out["nudged"], [])
        self.assertTrue(any(g.startswith("finance-first(acquires") for g in out["top"][0]["gates"]))

    def test_verdicts_tune_the_bar(self):
        make_state(self.paths.state, TECH)
        (self.paths.config / "technology.json").write_text(json.dumps(
            {"nudge": {"command": [sys.executable, "-c", "pass", "{file}"]}, "tuning": {"link_state": str(self.link)}}))
        out = packs.tick(self.paths, now=NOW, pack="technology")
        self.assertEqual(out["nudged"][0]["status"], "sent")
        pack = self.link / "lanes/editor/outbox/sent"
        pack.mkdir(parents=True)
        stamp = packs.datetime.fromtimestamp(NOW + 2, packs.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (pack / "000001.json").write_text(json.dumps({"action": "technology-trend-alert", "marker": "ALERT", "seq": 1,
                                                      "event_id": "host-000001", "emitted_at": stamp}))
        th = self.link / "threads/host-000001"
        th.mkdir(parents=True)
        (th / "transcript.jsonl").write_text(json.dumps({"dir": "peer", "marker": "DONE", "at": stamp,
                                                         "text": "Verdict: stale — covered yesterday."}) + "\n")
        again = packs.tick(self.paths, now=NOW + 1800, pack="technology")
        self.assertEqual(again["verdicts_harvested"], 1)
        self.assertEqual(again["verdicts"], {"stale": 1})
        self.assertAlmostEqual(again["bar"], 0.71)  # example threshold 0.70 + one step_up of 0.01


def test_discovery_precedence_and_xdg(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "out")
    paths.config.mkdir()
    repo = tmp_path / "repo"
    monkeypatch.setattr(packs, "ROOT", repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    roots = [tmp_path / "flag", tmp_path / "env1", tmp_path / "env2", tmp_path / "configured",
             xdg / "trend-watch/packs", repo / "packs"]
    for i, folder in enumerate(roots):
        folder.mkdir(parents=True)
        (folder / "sample.json").write_text('{}')
        (folder / ("unique%d.json" % i)).write_text('{}')
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", os.pathsep.join(map(str, roots[1:3])))
    (paths.config / "config.json").write_text(json.dumps({"packs_dir": str(roots[3])}))
    assert packs.directories(paths, [roots[0]]) == roots
    for folder in roots:
        assert packs.resolve(paths, "sample", [roots[0]]) == folder / "sample.json"
        (folder / "sample.json").unlink()
    assert len(packs.discover(paths, [roots[0]])) == len(roots)
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert packs.directories(paths)[-2] == tmp_path / "home/.config/trend-watch/packs"


def test_pack_domains_join_collector_with_local_precedence(tmp_path, monkeypatch):
    from trend_watch.config import load_config, load_domains, initialize
    from trend_watch.registry import discover
    import shutil
    folder = tmp_path / "packs"
    shutil.copytree(EXAMPLES, folder)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", str(folder))
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "out")
    initialize(paths)
    cfg = load_config(paths)
    registry, errors = discover(paths)
    assert not errors
    pack = packs.load(paths, "example")
    expected = folder / "domains/example-launches.json"
    assert pack["domain_files"] == [str(expected)]
    domains, errors = load_domains(paths, cfg, registry)
    assert not errors
    assert next(d for d in domains if d["name"] == "example-launches")["file"] == str(expected)
    local = paths.config / "domains/example-launches.json"
    local.write_text(json.dumps({"name": "example-launches", "keywords": ["custom"]}))
    domains, errors = load_domains(paths, cfg, registry)
    assert not errors
    assert next(d for d in domains if d["name"] == "example-launches")["keywords"] == ["custom"]
    local.rename(local.parent / "archive" / local.name)
    domains, errors = load_domains(paths, cfg, registry)
    assert not errors
    assert "example-launches" not in [d["name"] for d in domains]
    expected.unlink()
    shared = packs.domain_path(folder / "example.json", "ai-tech")
    assert shared == packs.ROOT / "seed/domains/ai-tech.json"


def test_live_pack_override_and_config_search_key(tmp_path, monkeypatch):
    from trend_watch.config import load_config
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "out")
    paths.config.mkdir()
    (paths.config / "config.json").write_text(json.dumps({"packs_dir": str(EXAMPLES)}))
    assert load_config(paths)["packs_dir"] == str(EXAMPLES)
    (paths.config / "example.json").write_text(json.dumps({"nudge": {"timeout_s": 12}, "labels": {"extra": "Extra"}}))
    cfg = packs.load(paths, "example")
    assert cfg["nudge"]["timeout_s"] == 12
    assert cfg["nudge"]["command"] == []
    assert cfg["labels"]["example-launches"] == "Space launches"
    assert cfg["labels"]["extra"] == "Extra"


def test_absent_or_disabled_reply_source_is_noop(tmp_path):
    cfg = packs.defaults(EXAMPLES / "example.json")
    db = packs.own_db(tmp_path)
    try:
        assert packs.harvest(db, cfg, NOW) == 0
        cfg["tuning"].update(enabled=True, link_state="", peer="reviewer")
        assert packs.harvest(db, cfg, NOW) == 0
        cfg["tuning"].update(enabled=False, link_state=str(tmp_path))
        assert packs.harvest(db, cfg, NOW) == 0
    finally:
        db.close()


def test_cli_list_report_harvest_and_missing_pack(tmp_path, monkeypatch, capsys):
    from trend_watch.cli import main
    monkeypatch.setenv("TW_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert main(["--packs-dir", str(EXAMPLES), "pack", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"example": str(EXAMPLES / "example.json")}
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "config").exists()
    assert main(["pack", "example", "report", "--hours", "2", "--packs-dir", str(EXAMPLES)]) == 0
    assert "Orbital Dispatch" in capsys.readouterr().out
    assert main(["--packs-dir", str(EXAMPLES), "pack", "example", "harvest"]) == 0
    assert "Verdicts harvested: 0" in capsys.readouterr().out
    assert main(["--packs-dir", str(EXAMPLES), "pack", "example", "run", "--dry-run"]) == 1
    assert "no shared state.db" in capsys.readouterr().err
    assert main(["pack", "missing", "run"]) == 1
    assert "pack not found" in capsys.readouterr().err


def test_alert_caps_test_nudge_and_first_report(tmp_path, monkeypatch):
    install_fixtures(tmp_path / "packs")
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "out")
    paths.config.mkdir()
    make_state(paths.state, BREAKING)
    override = paths.config / "market.json"
    override.write_text(json.dumps({"alert": {"max_nudges_per_24h": 0}, "nudge": {"first_report_after_h": 0.01}}))
    out = packs.tick(paths, "market", now=NOW)
    assert out["eligible"] == 1
    assert not out["nudged"]
    assert "cap reached" in out["note"]
    out = packs.tick(paths, "market", now=NOW + 60, test_nudge=True)
    assert out["nudged"][0]["kind"] == "test"
    assert "[TEST]" in Path(out["nudged"][0]["file"]).read_text()
    assert out["report"]["status"] == "outbox-only"
    assert "report" not in packs.tick(paths, "market", now=NOW + 120)


if __name__ == "__main__":
    unittest.main()


def _story_cfg(**alert):
    cfg = packs.merge(packs.PACK_DEFAULTS, json.loads((EXAMPLES / "example.json").read_text()))
    dom = "technology-ai-models"
    cfg.update(domains=[dom], relevance={dom: [["openai"], ["model"]]}, strong_terms=["openai", "model"],
               persona_markers=["launch"], finance_first=[], finance_domain=[])
    cfg["outlets"] = {"tier1": ["reuters"], "tier2": []}
    cfg["alert"].update(threshold=0.05, min_outlets=3)
    cfg["alert"].update(**alert)
    return cfg, dom


def _story(cfg, dom, rows):
    """rows: (outlet, age_min); every headline is a variant of one OpenAI model story."""
    items = [{"hash": "h%d" % i, "title": "OpenAI launches new reasoning model for coders today - " + outlet,
              "url": "https://example.com/%d" % i, "platform": "google_news", "first_seen": NOW - age * 60,
              "last_seen": NOW, "published": NOW - age * 60, "domains": [dom], "extra": {}, "live": True}
             for i, (outlet, age) in enumerate(rows)]
    stories = packs.cluster(items, cfg)
    assert len(stories) == 1
    return packs.evaluate(stories[0], [], [], cfg, NOW, [])


def test_age_from_coverage_ignores_an_early_outlier():
    rows = [("Blog One", 600), ("Wire Two", 50), ("Wire Three", 40), ("Wire Four", 30)]
    cfg, dom = _story_cfg()
    old = _story(cfg, dom, rows)
    assert old["age_min"] == 600 and any(g.startswith("stale>") for g in old["gates"])
    cfg, dom = _story_cfg(age_from="coverage")
    new = _story(cfg, dom, rows)
    # Coverage started when the third distinct outlet published within window.fresh_h (30 min ago).
    assert new["age_min"] == 30 and new["age_oldest_min"] == 600
    assert not any(g.startswith("stale>") for g in new["gates"])
    assert new["parts"]["novelty"] == 1.0


def test_age_from_coverage_keeps_the_true_age_of_an_old_covered_story():
    rows = [("Wire One", 900), ("Wire Two", 890), ("Wire Three", 880), ("Wire Four", 20), ("Wire Five", 10)]
    cfg, dom = _story_cfg(age_from="coverage")
    s = _story(cfg, dom, rows)
    assert s["age_min"] == 880
    assert any(g.startswith("stale>") for g in s["gates"])
    # Too few outlets ever: falls back to the oldest member.
    s = _story(cfg, dom, [("Wire One", 100), ("Wire Two", 10)])
    assert s["age_min"] == 100


def test_tier1_waiver_needs_enough_fresh_outlets_and_defaults_off():
    five = [("Outlet %s" % c, 30 + i) for i, c in enumerate("ABCDE")]
    cfg, dom = _story_cfg()
    assert any(g.startswith("coverage 5 outlets/0 tier1") for g in _story(cfg, dom, five)["gates"])
    cfg, dom = _story_cfg(tier1_waiver_min_outlets=5)
    assert not any(g.startswith("coverage") for g in _story(cfg, dom, five)["gates"])
    assert any(g.startswith("coverage 4 outlets/0 tier1") for g in _story(cfg, dom, five[:4])["gates"])
    cfg, dom = _story_cfg(tier1_waiver_min_outlets=0)
    assert any(g.startswith("coverage") for g in _story(cfg, dom, five)["gates"])
