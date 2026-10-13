"""CIK-keyed watchlist with an explicit ambiguity policy.

Decisive identity is the SEC CIK reported by the source. Ticker and alias
matches against free text are recorded as *evidence only*; they never admit an
event into an intake lane on their own.

Normalization (documented so fixtures and code agree):
- Text is NFKC-normalized. Aliases match case-insensitively on word boundaries.
- Tickers match case-sensitively in upper case: as a cashtag (``$AAPL``), an
  exchange-qualified form (``NASDAQ:AAPL`` / ``NYSE: AAPL``), a parenthesized
  form (``(AAPL)``) or a bare token on word boundaries.
- A ticker flagged ``ambiguous_ticker`` (common words such as ON, IT, ALL, NOW,
  A) only matches as a cashtag or exchange-qualified form.
- CIKs are parsed strictly: 1-10 ASCII digits, otherwise malformed (never
  coerced). The integer is identity; the source string is preserved.
"""
import json
import re
import unicodedata
from .ids import uid

CIK = re.compile(r"^[0-9]{1,10}$")
EXCHANGES = ("NASDAQ", "NYSE", "NYSEAMERICAN", "AMEX", "OTC", "CBOE")


class WatchlistError(ValueError):
    pass


def parse_cik(value):
    """Strict CIK parse: returns (int_cik, source_repr) or raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("CIK must be digits")
    source = str(value) if isinstance(value, int) else value
    if not isinstance(source, str) or not CIK.match(source):
        raise ValueError("malformed CIK {!r}".format(value))
    number = int(source)
    if number <= 0:
        raise ValueError("CIK must be positive")
    return number, source


def nfkc(text):
    return unicodedata.normalize("NFKC", text or "")


class Watchlist:
    def __init__(self, entries, version):
        self.entries = entries
        self.version = version
        self.by_cik = {e["cik"]: e for e in entries}

    @classmethod
    def empty(cls):
        return cls([], uid("watchlist", "empty"))

    @classmethod
    def load(cls, path):
        if not path.exists():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatchlistError("{}: {}".format(path, exc))
        return cls.from_data(raw, str(path))

    @classmethod
    def from_data(cls, raw, label="watchlist"):
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            raise WatchlistError(label + ": expected an object with an 'entries' list")
        entries, seen = [], set()
        for n, entry in enumerate(raw["entries"]):
            where = "{} entry {}".format(label, n)
            if not isinstance(entry, dict):
                raise WatchlistError(where + ": must be an object")
            unknown = set(entry) - {"cik", "name", "tickers", "aliases", "ambiguous_ticker", "_help"}
            if unknown:
                raise WatchlistError(where + ": unknown keys " + ", ".join(sorted(unknown)))
            try:
                cik, source = parse_cik(entry.get("cik"))
            except ValueError as exc:
                raise WatchlistError(where + ": " + str(exc))
            if cik in seen:
                raise WatchlistError(where + ": duplicate CIK")
            seen.add(cik)
            tickers = entry.get("tickers", [])
            aliases = entry.get("aliases", [])
            if not all(isinstance(t, str) and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t) for t in tickers):
                raise WatchlistError(where + ": tickers must be upper-case symbols")
            if not all(isinstance(a, str) and a.strip() for a in aliases):
                raise WatchlistError(where + ": aliases must be nonempty strings")
            if not isinstance(entry.get("name", ""), str) or not isinstance(entry.get("ambiguous_ticker", False), bool):
                raise WatchlistError(where + ": name must be a string and ambiguous_ticker a boolean")
            entries.append({"cik": cik, "cik_source": source, "name": entry.get("name", ""), "tickers": list(tickers),
                            "aliases": [nfkc(a) for a in aliases], "ambiguous_ticker": entry.get("ambiguous_ticker", False)})
        canonical = [{k: e[k] for k in ("cik", "name", "tickers", "aliases", "ambiguous_ticker")} for e in sorted(entries, key=lambda e: e["cik"])]
        return cls(entries, uid("watchlist", json.dumps(canonical, sort_keys=True)))

    def decisive(self, cik):
        return self.by_cik.get(cik)

    def lexical(self, text):
        """Evidence-only matches: [(cik, kind, evidence)]. Never used to admit an event."""
        body = nfkc(text)
        found = []
        for entry in self.entries:
            for alias in entry["aliases"]:
                if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", body, flags=re.I):
                    found.append((entry["cik"], "alias", alias))
            for ticker in entry["tickers"]:
                strict = [r"\$" + re.escape(ticker) + r"(?![\w])",
                          r"(?<!\w)(?:" + "|".join(EXCHANGES) + r")\s*:\s*" + re.escape(ticker) + r"(?![\w])"]
                loose = [r"\(" + re.escape(ticker) + r"\)", r"(?<![\w$])" + re.escape(ticker) + r"(?![\w])"]
                patterns = strict if entry["ambiguous_ticker"] else strict + loose
                if any(re.search(p, body) for p in patterns):
                    found.append((entry["cik"], "ticker", ticker))
        return sorted(set(found))
