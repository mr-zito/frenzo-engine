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
