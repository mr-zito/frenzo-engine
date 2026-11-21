"""Twitch Helix API (needs the free app keys in config.yaml: twitch.client_id / client_secret)."""
import threading
import time

import requests

API = "https://api.twitch.tv/helix"


class TwitchAPI:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires = 0
        self._lock = threading.Lock()
        self._games = {}

    def _auth(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Twitch login failed ({r.status_code}). Check client_id / client_secret in config.yaml."
            )
        data = r.json()
        self._token = data["access_token"]
        self._expires = time.time() + data.get("expires_in", 3600)
        return self._token

    def _request(self, path, params):
        """One GET: returns the whole JSON body. Retries once on 401 (expired token) and waits on 429
        (rate limit: Twitch says when the bucket refills in the Ratelimit-Reset header)."""
        for attempt in range(5):
            with self._lock:
                r = requests.get(
                    f"{API}/{path}",
                    params=params,
                    headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"},
                    timeout=20,
                )
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 429:
                reset = r.headers.get("Ratelimit-Reset")
                try:
                    wait = max(1.0, min(60.0, float(reset) - time.time()))
                except (TypeError, ValueError):
                    wait = 2.0 * (attempt + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            remaining = r.headers.get("Ratelimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                time.sleep(1.0)                     # nearly out of points: slow down a little
            return r.json()
        return {}

    def _get(self, path, params):
        return self._request(path, params).get("data", [])

    def _get_all(self, path, params, max_pages=10):
        """Follow the pagination cursor (100 items a page)."""
        out, cursor = [], None
        for _ in range(max_pages):
            p = list(params) + ([("after", cursor)] if cursor else [])
            body = self._request(path, p)
            out += body.get("data", [])
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not body.get("data"):
                break
        return out

    def game_id(self, name):
        if name not in self._games:
            data = self._get("games", [("name", name)])
            self._games[name] = data[0]["id"] if data else None
        return self._games[name]

    def users(self, logins):
        out = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for u in self._get("users", [("login", l) for l in chunk]):
                out[u["login"].lower()] = u["id"]
        return out

    def live_streams(self, logins):
        out = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for s in self._get("streams", [("user_login", l) for l in chunk] + [("first", "100")]):
                out[s["user_login"].lower()] = s
        return out

    def clips_since(self, broadcaster_id, started_at_iso):
        return self._get(
            "clips",
            [("broadcaster_id", broadcaster_id), ("started_at", started_at_iso), ("first", "100")],
        )

    def clips_between(self, broadcaster_id, started_at_iso, ended_at_iso, max_pages=10):
        """Every clip of a channel created in [started_at, ended_at]. Keep the range tight: Twitch can miss
        clips when the range is very wide."""
        return self._get_all("clips", [("broadcaster_id", broadcaster_id), ("started_at", started_at_iso),
                                       ("ended_at", ended_at_iso), ("first", "100")], max_pages)

    def clips_by_ids(self, ids):
        """Fresh data (views) for known clips, 100 ids per call."""
        out = []
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 100):
            out += self._get("clips", [("id", c) for c in ids[i : i + 100]])
        return out

    def videos(self, user_id, kind="archive", first=10):
        """Past broadcasts (VODs) of a channel, newest first."""
        return self._get("videos", [("user_id", user_id), ("type", kind), ("first", str(first))])

    def video(self, video_id):
        data = self._get("videos", [("id", video_id)])
        return data[0] if data else None


class TwitchPublic:
    """Keyless: Twitch's public web API (GraphQL, the same one twitch.tv, streamlink and yt-dlp use), for when no
    app keys are in config.yaml. Same methods as TwitchAPI for clips, videos and users, with Helix-shaped answers.
    Unofficial: it can change without notice (then the viewer-clip signal just goes quiet)."""
    URL = "https://gql.twitch.tv/gql"
    CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"          # twitch.tv's own public web client id
    CLIP_FIELDS = ("id slug title createdAt durationSeconds viewCount isFeatured curator { id login } "
                   "videoOffsetSeconds video { id } game { id name } broadcaster { id login }")

    def __init__(self):
        self._lock = threading.Lock()
        self._games = {}

    def _q(self, query, variables=None):
        for attempt in range(5):
            with self._lock:
                r = requests.post(self.URL, json={"query": query, "variables": variables or {}},
                                  headers={"Client-Id": self.CLIENT_ID}, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("errors") and not body.get("data"):
                raise RuntimeError(str(body["errors"])[:200])
            return body.get("data") or {}
        return {}

    @staticmethod
    def _ts(iso):
        import datetime as dt
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()

    @staticmethod
    def _helix(n, broadcaster_id=""):
        """A GraphQL clip node -> the same dict shape as Helix Get Clips (id = the clip's slug)."""
        cur, vid, game = n.get("curator") or {}, n.get("video") or {}, n.get("game") or {}
        slug = n.get("slug") or n.get("id")
        return {"id": slug, "url": f"https://clips.twitch.tv/{slug}", "creator_id": str(cur.get("id") or ""),
                "creator_name": cur.get("login") or "", "title": n.get("title") or "", "created_at": n.get("createdAt"),
                "duration": n.get("durationSeconds") or 30, "view_count": n.get("viewCount") or 0,
                "is_featured": bool(n.get("isFeatured")), "video_id": str(vid.get("id") or ""),
                "vod_offset": n.get("videoOffsetSeconds"), "game_id": str(game.get("id") or ""),
                "game_name": game.get("name") or "",
                "broadcaster_id": str((n.get("broadcaster") or {}).get("id") or broadcaster_id)}

    def users(self, logins):
        out = {}
        for l in logins:
            u = self._q("query($l:String!){ user(login:$l){ id } }", {"l": l}).get("user")
            if u:
                out[l.lower()] = u["id"]
        return out

    def game_id(self, name):
        if name not in self._games:
            g = self._q("query($n:String!){ game(name:$n){ id } }", {"n": name}).get("game")
            self._games[name] = g["id"] if g else None
        return self._games[name]

    def _clips(self, broadcaster_id, since_ts, until_ts=None, max_pages=20):
        """Clips made in [since_ts, until_ts]. LIMITS of the keyless API: clips are only listed MOST VIEWED first
        (newest-first is refused) and only the first page of 100 is served (later pages need a browser integrity
        token). So the top 100 of each period that covers the range (day, week, month) are combined and filtered
        by time: the most-viewed clips are found, little-watched fresh ones may not be (official keys see all)."""
        age = time.time() - since_ts
        periods = ["LAST_DAY", "LAST_WEEK", "LAST_MONTH", "ALL_TIME"]
        first = 0 if age < 23 * 3600 else 1 if age < 6.9 * 86400 else 2 if age < 29 * 86400 else 3
        q = ("query($id:ID!,$p:ClipsPeriod){ user(id:$id){ clips(first:100, criteria:"
             "{period:$p, sort:VIEWS_DESC}){ edges { node { " + self.CLIP_FIELDS + " } } } } }")
        out, seen = [], set()
        for period in periods[first:first + 2]:
            data = self._q(q, {"id": str(broadcaster_id), "p": period}).get("user") or {}
            for e in ((data.get("clips") or {}).get("edges")) or []:
                c = self._helix(e["node"], broadcaster_id)
                ts = self._ts(c["created_at"])
                if ts >= since_ts and (until_ts is None or ts <= until_ts) and c["id"] not in seen:
                    seen.add(c["id"])
                    out.append(c)
        return out

    def clips_since(self, broadcaster_id, started_at_iso, max_pages=8):
        return self._clips(broadcaster_id, self._ts(started_at_iso), max_pages=max_pages)

    def clips_between(self, broadcaster_id, started_at_iso, ended_at_iso, max_pages=20):
        return self._clips(broadcaster_id, self._ts(started_at_iso), self._ts(ended_at_iso), max_pages)

    def clips_by_ids(self, ids):
        out = []
        ids = [s for s in dict.fromkeys(ids) if s and all(ch.isalnum() or ch in "-_" for ch in s)]
        for i in range(0, len(ids), 25):                     # several clips per request (aliases)
            chunk = ids[i:i + 25]
            q = "query{ " + " ".join('c%d: clip(slug:"%s"){ %s }' % (j, s, self.CLIP_FIELDS)
                                     for j, s in enumerate(chunk)) + " }"
            data = self._q(q)
            out += [self._helix(n) for n in data.values() if n]
        return out

    def video(self, video_id):
        v = self._q("query($id:ID!){ video(id:$id){ id createdAt lengthSeconds owner { id login } game { id name } } }",
                    {"id": str(video_id)}).get("video")
        if not v:
            return None
        s = int(v.get("lengthSeconds") or 0)
        return {"id": v["id"], "user_id": str((v.get("owner") or {}).get("id") or ""), "created_at": v.get("createdAt"),
                "duration": f"{s // 3600}h{s % 3600 // 60}m{s % 60}s", "game_id": str((v.get("game") or {}).get("id") or "")}

    def videos(self, user_id, kind="archive", first=10):
        q = ("query($id:ID!,$n:Int){ user(id:$id){ videos(first:$n, type:ARCHIVE, sort:TIME){ edges { node { id title "
             "createdAt lengthSeconds game { id name } } } } } }")
        u = self._q(q, {"id": str(user_id), "n": first}).get("user") or {}
        out = []
        for e in ((u.get("videos") or {}).get("edges")) or []:
            n = e["node"]
            s = int(n.get("lengthSeconds") or 0)
            out.append({"id": n["id"], "user_id": str(user_id), "title": n.get("title") or "",
                        "created_at": n.get("createdAt"), "duration": f"{s // 3600}h{s % 3600 // 60}m{s % 60}s",
                        "game_id": str((n.get("game") or {}).get("id") or ""),
                        "url": f"https://www.twitch.tv/videos/{n['id']}"})
        return out


def api_for(cfg):
    """The official API when keys are set, else the keyless public one (twitch.public_api: false turns it off)."""
    from .config import placeholder
    tw = cfg.get("twitch") or {}
    if not placeholder(tw.get("client_id")) and not placeholder(tw.get("client_secret")):
        return TwitchAPI(tw["client_id"], tw["client_secret"])
    if tw.get("public_api", True):
        return TwitchPublic()
    return None
