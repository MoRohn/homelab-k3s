"""Exact sports results from ESPN's public site API: no key, ~0.3 s, structured scores.

`lookup(text, when, tz)` finds the teams a question names, then reads each team's schedule (or, when a
name is shared by many teams, the scoreboard for the day asked about) and returns the matching games as
evidence lines: final scores, live status, or the next scheduled game. It returns None rather than
guess: then web search answers instead.

Only team ids, league names and a date leave the box (`site.api.espn.com`), never the question.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from lif.common import log

LOG = log.get("lif.web.sports")
BASE = "https://site.api.espn.com/apis/site/v2/sports"

# key → (sport, league path, label, words that point at it)
LEAGUES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "college-football": ("football", "college-football", "college football",
                         ("college football", "cfb", "ncaa football", "ncaaf", "bowl game", "ap poll", "top 25")),
    "nfl": ("football", "nfl", "NFL", ("nfl", "super bowl", "pro football")),
    "nba": ("basketball", "nba", "NBA", ("nba", "nba finals")),
    "wnba": ("basketball", "wnba", "WNBA", ("wnba",)),
    "mens-college-basketball": ("basketball", "mens-college-basketball", "men's college basketball",
                                ("college basketball", "march madness", "ncaa basketball", "ncaab", "final four")),
    "mlb": ("baseball", "mlb", "MLB", ("mlb", "world series", "baseball")),
    "nhl": ("hockey", "nhl", "NHL", ("nhl", "stanley cup", "hockey")),
    "mls": ("soccer", "usa.1", "MLS", ("mls",)),
    "epl": ("soccer", "eng.1", "Premier League", ("premier league", "epl")),
}
_SPORT_HINTS = {"football": ("college-football", "nfl"), "basketball": ("nba", "mens-college-basketball", "wnba"),
                "soccer": ("mls", "epl"), "baseball": ("mlb",), "hockey": ("nhl",)}
# "Who's the best team", "standings", "AP poll", "best record": a league table answers, not a schedule.
_TABLE = re.compile(r"(?i)\b(?:standings?|best|top (?:team|teams|\d+|ten|25)|ranked|rankings?|ranks?|poll"
                    r"|(?:number|no\.?) ?(?:1|one)|#1|first place|leading|leads? the|best in|undefeated|unbeaten"
                    r"|playoff (?:picture|race|seed)|seed(?:ing)?|who(?:'s| is) the best)\b")
COLLEGE = ("college-football", "mens-college-basketball")
DIRECTORY_TTL = 24 * 3600
SCHEDULE_TTL = 60                      # live games change by the minute
MAX_CANDIDATES = 4


@dataclass
class Team:
    league: str
    id: str
    display: str
    aliases: tuple[str, ...]


@dataclass
class Game:
    league: str
    id: str
    when: datetime
    state: str                          # pre | in | post
    status: str                         # "Final", "Q3 5:12", "Sat, Oct 10 3:30 PM EDT"
    home: tuple[str, str | None, bool | None]       # (name, score, winner)
    away: tuple[str, str | None, bool | None]
    url: str = ""
    team_ids: tuple[str, ...] = ()

    def line(self, tz: ZoneInfo) -> str:
        local = self.when.astimezone(tz)
        day = f"{local:%a} {local:%B} {local.day}, {local.year}, {local:%-I:%M %p %Z}"
        (hn, hs, hw), (an, as_, aw) = self.home, self.away
        if self.state == "pre":
            return f"SCHEDULED ({LEAGUES[self.league][2]}): {an} at {hn}, {day}."
        score = f"{an} {as_}, {hn} {hs} ({an} at {hn})"
        if self.state == "in":
            return f"IN PROGRESS ({LEAGUES[self.league][2]}, {self.status}): {score}. Started {day}."
        winner = an if aw else hn if hw else None
        result = f" {winner} won." if winner else " Tied." if hs == as_ else ""
        return f"FINAL ({LEAGUES[self.league][2]}, {day}): {score}.{result}"


@dataclass
class SportsResult:
    lines: list[str]
    sources: list[dict]                 # parallel to lines: the {title, url} each line came from
    teams: list[str] = field(default_factory=list)


class SportsFeed:
    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 2.0):
        self.client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "lif-web/1"})
        self.timeout = timeout
        self._dirs: dict[str, tuple[float, list[Team]]] = {}
        self._json: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── HTTP with a short cache ───────────────────────────────────────────────────────────────

    async def _get(self, url: str, ttl: float) -> dict | None:
        hit = self._json.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            r = await self.client.get(url, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            LOG.warning("espn fetch failed", extra={"fields": {"url": url[:120], "err": type(e).__name__}})
            return hit[1] if hit else None
        self._json[url] = (time.time(), data)
        if len(self._json) > 512:
            for k in sorted(self._json, key=lambda k: self._json[k][0])[:128]:
                self._json.pop(k, None)
        return data

    async def directory(self, league: str) -> list[Team]:
        hit = self._dirs.get(league)
        if hit and time.time() - hit[0] < DIRECTORY_TTL:
            return hit[1]
        lock = self._locks.setdefault(league, asyncio.Lock())
        async with lock:
            hit = self._dirs.get(league)
            if hit and time.time() - hit[0] < DIRECTORY_TTL:
                return hit[1]
            sport, path, *_ = LEAGUES[league]
            data = await self._get(f"{BASE}/{sport}/{path}/teams?limit=1000", DIRECTORY_TTL)
            teams = _parse_directory(league, data) if data else []
            if teams:
                self._dirs[league] = (time.time(), teams)
            return teams or (hit[1] if hit else [])

    async def prewarm(self) -> None:
        await asyncio.gather(*(self.directory(lg) for lg in LEAGUES), return_exceptions=True)

    # ── lookup ────────────────────────────────────────────────────────────────────────────────

    async def lookup(self, text: str, now: datetime, target: date | None = None,
                     span: tuple[date, date] | None = None) -> SportsResult | None:
        tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo("UTC")
        leagues = leagues_for(text)
        dirs = await asyncio.gather(*(self.directory(lg) for lg in leagues))
        cands = match_teams(text, [t for d in dirs for t in d])
        tables: SportsResult | None = None
        if _TABLE.search(text) and len(leagues) < len(LEAGUES):       # a league or sport was named
            named = sorted({t.league for t in cands}) or leagues
            tables = await self._tables(named[:2], cands, now)
        if not cands or len(cands) > MAX_CANDIDATES and span is None and target is None:
            return tables
        games = await self._games(text, cands, now, tz, target, span)
        if games is None:
            return tables
        if tables:
            games.lines = tables.lines + games.lines
            games.sources = tables.sources + games.sources
        return games

    async def _games(self, text: str, cands: list[Team], now: datetime, tz: ZoneInfo, target: date | None,
                     span: tuple[date, date] | None) -> SportsResult | None:
        if span is None and target is not None:
            span = (target, target)
        if len(cands) > MAX_CANDIDATES:
            if span is None:
                return None              # "the Tigers" with no day: too many teams to guess
            games = await self._scoreboard_games(cands, span, tz)
        else:
            games = await self._schedule_games(cands, span, now, tz)
        if not games:
            return None
        # Several teams named ("Beavers vs Pitt"): keep games between them when there are any.
        ids = {t.id for t in cands}
        both = [g for g in games if len(ids & set(g.team_ids)) >= 2]
        games = both or games
        seen, out = set(), []
        for g in sorted(games, key=lambda g: g.when):
            if g.id not in seen:
                seen.add(g.id)
                out.append(g)
        out = out[:6]
        return SportsResult([g.line(tz) for g in out],
                            [{"title": f"ESPN: {g.away[0]} at {g.home[0]}", "url": g.url} for g in out],
                            [t.display for t in cands])

    async def _tables(self, leagues: list[str], named: list[Team], now: datetime) -> SportsResult | None:
        """Rankings (college: the playoff ranking when published, else the AP poll) or standings (pro)."""
        lines, sources = [], []
        ids = {t.id for t in named}

        async def one(lg: str) -> None:
            sport, path, label, _ = LEAGUES[lg]
            if lg in COLLEGE:
                data = await self._get(f"{BASE}/{sport}/{path}/rankings", 3600)
                polls = (data or {}).get("rankings") or []
                poll = next((p for p in polls if "playoff" in str(p.get("name", "")).lower()), None) or next(
                    (p for p in polls if str(p.get("name", "")).startswith("AP")), polls[0] if polls else None)
                if not poll:
                    return
                ranks = poll.get("ranks") or []
                top = "; ".join(f"{r.get('current')}. {(r.get('team') or {}).get('location') or '?'}"
                                f" ({r.get('recordSummary') or '?'})" for r in ranks[:10])
                src = {"title": f"ESPN: {poll.get('name')}", "url": f"https://www.espn.com/{path}/rankings"}
                lines.append(f"RANKING ({poll.get('headline') or poll.get('name')}): {top}.")
                sources.append(src)
                ranked = {str((r.get('team') or {}).get('id')): r for r in ranks}
                for t in named:
                    if t.league == lg:
                        r = ranked.get(t.id)
                        lines.append(f"{t.display}: " + (f"No. {r.get('current')} in {poll.get('name')} "
                                                         f"({r.get('recordSummary')})." if r else
                                                         f"not ranked in the top {len(ranks)} of {poll.get('name')}."))
                        sources.append(src)
                return
            data = await self._get(f"https://site.api.espn.com/apis/v2/sports/{sport}/{path}/standings", 3600)
            rows = []
            stack = [data or {}]
            while stack:                                   # conferences → divisions → entries
                node = stack.pop()
                stack.extend(node.get("children") or [])
                for e in (node.get("standings") or {}).get("entries") or []:
                    st = {x.get("name"): x for x in e.get("stats") or []}
                    rows.append((float((st.get("winPercent") or {}).get("value") or 0),
                                 float((st.get("pointDifferential") or st.get("differential") or {}).get("value") or 0),
                                 (e.get("team") or {}).get("displayName") or "?", str((e.get("team") or {}).get("id")),
                                 (st.get("overall") or {}).get("displayValue")
                                 or f"{(st.get('wins') or {}).get('displayValue', '?')}-{(st.get('losses') or {}).get('displayValue', '?')}",
                                 node.get("name") or ""))
            if not rows:
                return
            rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
            top = "; ".join(f"{r[2]} {r[4]}" for r in rows[:8])
            src = {"title": f"ESPN: {label} standings", "url": f"https://www.espn.com/{path}/standings"}
            lines.append(f"STANDINGS ({label}, best records as of {now:%B} {now.day}, {now.year}): {top}.")
            sources.append(src)
            for i, r in enumerate(rows):
                if r[3] in ids:
                    lines.append(f"{r[2]}: {r[4]} ({r[5]}), {i + 1} of {len(rows)} by record.")
                    sources.append(src)
        await asyncio.gather(*(one(lg) for lg in leagues))
        return SportsResult(lines, sources) if lines else None

    async def _schedule_games(self, teams: list[Team], span: tuple[date, date] | None, now: datetime,
                              tz: ZoneInfo) -> list[Game]:
        async def one(t: Team) -> list[Game]:
            sport, path, *_ = LEAGUES[t.league]
            data = await self._get(f"{BASE}/{sport}/{path}/teams/{t.id}/schedule", SCHEDULE_TTL)
            games = _parse_events(t.league, (data or {}).get("events") or [])
            if span is not None:
                return [g for g in games if span[0] <= g.when.astimezone(tz).date() <= span[1]]
            # No day named: the game in progress, else the latest result and the next game.
            live = [g for g in games if g.state == "in"]
            done = [g for g in games if g.state == "post" and g.when <= now]
            nxt = [g for g in games if g.state == "pre" and g.when >= now - timedelta(hours=6)]
            return live + done[-1:] + nxt[:1]
        got = await asyncio.gather(*(one(t) for t in teams))
        games = [g for gs in got for g in gs]
        if span is not None and not games:
            # The day asked about had no game: say what the nearest ones were instead of nothing.
            return await self._schedule_games(teams, None, now, tz)
        return games

    async def _scoreboard_games(self, teams: list[Team], span: tuple[date, date], tz: ZoneInfo) -> list[Game]:
        ids_by_league: dict[str, set[str]] = {}
        for t in teams:
            ids_by_league.setdefault(t.league, set()).add(t.id)
        days = [span[0] + timedelta(days=i) for i in range((span[1] - span[0]).days + 1)][:7]

        async def one(league: str, d: date) -> list[Game]:
            sport, path, *_ = LEAGUES[league]
            groups = "&groups=80" if league == "college-football" else ""
            data = await self._get(f"{BASE}/{sport}/{path}/scoreboard?dates={d:%Y%m%d}&limit=400{groups}",
                                   SCHEDULE_TTL)
            return [g for g in _parse_events(league, (data or {}).get("events") or [])
                    if ids_by_league[league] & set(g.team_ids)]
        got = await asyncio.gather(*(one(lg, d) for lg in ids_by_league for d in days))
        games = [g for gs in got for g in gs]
        return games if len(games) <= MAX_CANDIDATES else []


# ── parsing and matching (pure; tested directly) ───────────────────────────────────────────────

def leagues_for(text: str) -> list[str]:
    t = text.lower()
    named = [k for k, v in LEAGUES.items() if any(re.search(rf"\b{re.escape(w)}\b", t) for w in v[3])]
    if named:
        return named
    for word, keys in _SPORT_HINTS.items():
        if re.search(rf"\b{word}\b", t):
            return list(keys)
    return list(LEAGUES)


def _parse_directory(league: str, data: dict) -> list[Team]:
    out = []
    for sp in data.get("sports") or []:
        for lg in sp.get("leagues") or []:
            for item in lg.get("teams") or []:
                t = item.get("team") or {}
                names = {t.get("displayName"), t.get("shortDisplayName"), t.get("location"), t.get("name"),
                         t.get("nickname"), f"{t.get('location', '')} {t.get('name', '')}".strip()}
                aliases = tuple(sorted({n.lower() for n in names if n and len(n) >= 3}, key=len, reverse=True))
                if t.get("id") and aliases:
                    out.append(Team(league, str(t["id"]), t.get("displayName") or aliases[0], aliases))
    return out


# Ordinary words that are also team names: matched only when the question capitalises them.
_COMMON = {"heat", "magic", "jazz", "thunder", "sun", "suns", "fire", "wild", "kings", "giants", "jets", "bears",
           "lions", "eagles", "rams", "chiefs", "cardinals", "rockets", "nets", "bulls", "hawks", "sky", "storm",
           "fever", "dream", "spurs", "united", "city", "rays", "reds", "twins", "royals", "pirates", "angels",
           "athletics", "stars", "lightning", "flames", "kraken", "blues", "sharks", "union", "galaxy", "packers",
           "texans", "ducks", "state", "tech", "miami", "washington", "kansas city", "new york", "los angeles",
           "chicago", "boston", "texas", "florida", "georgia", "carolina", "arizona", "utah", "phoenix", "orlando",
           "indiana", "minnesota", "tennessee", "detroit", "houston", "dallas", "denver", "seattle", "portland",
           "charlotte", "atlanta", "cleveland", "toronto", "philadelphia", "pittsburgh", "san diego", "san jose",
           "las vegas", "vegas", "golden", "army", "navy", "air force", "liberty", "rice", "temple", "duke"}


def match_teams(text: str, teams: list[Team]) -> list[Team]:
    """Longest alias first; a matched span is consumed, so "Oregon State" never also matches "Oregon".
    Teams sharing the very same alias on the same span ("Tigers") are all kept: the caller disambiguates."""
    low = text.lower()
    taken = [False] * len(low)
    claimed: dict[tuple[int, int], str] = {}
    hits: dict[tuple[str, str], Team] = {}
    for alias, team in sorted(((a, t) for t in teams for a in t.aliases), key=lambda x: len(x[0]), reverse=True):
        for m in re.finditer(rf"\b{re.escape(alias)}\b", low):
            span = (m.start(), m.end())
            if span in claimed:
                if claimed[span] != alias:
                    continue
            elif any(taken[span[0]:span[1]]) or (alias in _COMMON and not text[m.start()].isupper()):
                continue
            claimed[span] = alias
            for i in range(*span):
                taken[i] = True
            hits[(team.league, team.id)] = team
    return list(hits.values())


def _score(c: dict) -> str | None:
    s = c.get("score")
    if isinstance(s, dict):
        return s.get("displayValue") or (str(int(s["value"])) if s.get("value") is not None else None)
    return str(s) if s not in (None, "") else None


def _parse_events(league: str, events: list[dict]) -> list[Game]:
    out = []
    for e in events:
        try:
            c = (e.get("competitions") or [{}])[0]
            comp = c.get("competitors") or []
            home = next(x for x in comp if x.get("homeAway") == "home")
            away = next(x for x in comp if x.get("homeAway") == "away")
            st = (c.get("status") or e.get("status") or {}).get("type") or {}
            when = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        except (StopIteration, KeyError, ValueError):
            continue
        url = next((ln.get("href") for ln in e.get("links") or []
                    if "summary" in (ln.get("rel") or []) and str(ln.get("href", "")).startswith("https://")), "")

        def side(x: dict) -> tuple[str, str | None, bool | None]:
            return ((x.get("team") or {}).get("displayName") or "?", _score(x), x.get("winner"))
        out.append(Game(league, str(e.get("id")), when, st.get("state") or "pre",
                        st.get("shortDetail") or st.get("description") or "", side(home), side(away), url,
                        tuple(str((x.get("team") or {}).get("id") or x.get("id")) for x in comp)))
    return out
