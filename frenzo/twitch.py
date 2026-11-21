import time

import requests

API = "https://api.twitch.tv/helix"


class TwitchAPI:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires = 0

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

    def _get(self, path, params):
        for attempt in range(2):
            r = requests.get(
                f"{API}/{path}",
                params=params,
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"},
                timeout=20,
            )
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        return []

    def game_id(self, name):
        data = self._get("games", [("name", name)])
        return data[0]["id"] if data else None

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
            [("broadcaster_id", broadcaster_id), ("started_at", started_at_iso), ("first", "20")],
        )
