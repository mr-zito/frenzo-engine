"""Who is live right now, on Twitch, Kick and YouTube."""
import datetime as dt
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .chat import KickChat, TwitchChat, YouTubeChat
from .config import placeholder
from .twitch import TwitchAPI, TwitchPublic, api_for
from .util import log

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


@dataclass
class LiveInfo:
    platform: str
    login: str
    display: str
    url: str                 # what the recorder opens
    game: str
    title: str
    user_id: Optional[str] = None
    video_id: Optional[str] = None


def game_ok(cfg, scfg, *texts):
    """True if the stream is the game we want (or game filtering is off)."""
    if not cfg["game"]["only_when_playing"] or scfg.get("any_game"):
        return True
    hay = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in hay for k in cfg["game"]["keywords"])


def probe_streams(urls, workers=6):
    """Keyless live check with streamlink: {url: {"title","category","author"}} for channels that are live."""
    import concurrent.futures
    import streamlink

    def one(url):
        try:
            session = streamlink.Streamlink()
            _, plugin_cls, resolved = session.resolve_url(url)
            plugin = plugin_cls(session, resolved)
            if not plugin.streams():
                return url, None
            return url, {"title": plugin.get_title() or "", "category": plugin.get_category() or "",
                         "author": plugin.get_author() or ""}
        except Exception:
            return url, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, urls))


class _WrongGameLog:
    """Logs 'live but playing something else' once per change, so the window isn't flooded."""
    def __init__(self):
        self.seen = {}

    def note(self, key, game):
        if self.seen.get(key) != game:
            self.seen[key] = game
            log(f"live but not playing the game (category: {game or 'unknown'}) - waiting", key)


class TwitchPlatform:
    name = "twitch"

    def __init__(self, cfg):
        self.cfg = cfg
        tw = cfg["twitch"]
        self.keyless = placeholder(tw["client_id"]) or placeholder(tw["client_secret"])
        # clips: the official API with keys, else Twitch's public web API (twitch.public_api: false = off)
        self.api = api_for(cfg)
        self.ids = {}
        self.game_id = None
        self.wrong = _WrongGameLog()

    def setup(self, streamers):
        if self.keyless:
            if self.api is not None:
                try:
                    self.ids = self.api.users([s["login"] for s in streamers])
                except Exception as e:
                    log(f"couldn't look up the Twitch channels ({str(e)[:80]})", "twitch")
                log("no Twitch keys: keyless live checks; viewer clips from Twitch's public web API", "twitch")
            else:
                log("no Twitch keys: using keyless live checks (viewer-clip signal off)", "twitch")
            return
        self.game_id = self.api.game_id(self.cfg["game"]["name"])
        logins = [s["login"] for s in streamers]
        self.ids = self.api.users(logins)
        for l in logins:
            if l not in self.ids:
                log(f"WARNING: Twitch user '{l}' not found, check the spelling", "twitch")

    def check(self, streamers):
        if self.keyless:
            urls = {s["login"]: f"https://www.twitch.tv/{s['login']}" for s in streamers}
            res = probe_streams(list(urls.values()))
            out = {}
            for s in streamers:
                m = res.get(urls[s["login"]])
                if not m:
                    continue
                if game_ok(self.cfg, s, m["category"]) or (not m["category"] and game_ok(self.cfg, s, m["title"])):
                    out[s["login"]] = LiveInfo("twitch", s["login"], m["author"] or s["login"], urls[s["login"]],
                                               m["category"], m["title"], user_id=self.ids.get(s["login"]))
                else:
                    self.wrong.note(s["key"], m["category"])
            return out
        live = self.api.live_streams([s["login"] for s in streamers])
        out = {}
        for s in streamers:
            st = live.get(s["login"])
            if not st:
                continue
            right_game = (self.game_id and st.get("game_id") == self.game_id) or \
                game_ok(self.cfg, s, st.get("game_name"))
            if right_game:
                out[s["login"]] = LiveInfo("twitch", s["login"], st.get("user_name") or s["login"],
                                           f"https://www.twitch.tv/{s['login']}", st.get("game_name", ""),
                                           st.get("title", ""), user_id=self.ids.get(s["login"]))
        return out

    def chat(self, info, scfg):
        return TwitchChat(info.login, f"twitch:{info.login}")

    def new_clips(self, info, since_iso):
        if self.api is None or not info.user_id:
            return []
        out = []
        for c in self.api.clips_since(info.user_id, since_iso):
            created = dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
            out.append((c["id"], created, c.get("title", "")))
        return out

    def clips_raw(self, info, since_iso):
        """Get Clips items (dicts) made since since_iso: the viewer-clip evidence."""
        if self.api is None or not info.user_id:
            return []
        return self.api.clips_since(info.user_id, since_iso)

    def clips_by_ids(self, ids):
        return [] if self.api is None else self.api.clips_by_ids(ids)

    @property
    def public(self):
        return isinstance(self.api, TwitchPublic)


class KickPlatform:
    name = "kick"
    API = "https://api.kick.com/public/v1"

    def __init__(self, cfg):
        self.cfg = cfg
        k = cfg["kick"]
        self.keyless = placeholder(k["client_id"]) or placeholder(k["client_secret"])
        self.client_id, self.client_secret = k["client_id"], k["client_secret"]
        self._token, self._expires = None, 0
        self.wrong = _WrongGameLog()

    def _auth(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post("https://id.kick.com/oauth/token", data={
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.client_secret}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Kick login failed ({r.status_code}). Check the kick keys in config.yaml.")
        d = r.json()
        self._token, self._expires = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def setup(self, streamers):
        if self.keyless:
            log("no Kick keys: using keyless live checks", "kick")
            return
        self._auth()

    def channel(self, slug):
        r = requests.get(f"{self.API}/channels", params={"slug": slug},
                         headers={"Authorization": f"Bearer {self._auth()}"}, timeout=20)
        if r.status_code == 401:
            self._token = None
            return self.channel(slug)
        r.raise_for_status()
        data = r.json().get("data") or []
        return data[0] if data else None

    def check(self, streamers):
        out = {}
        if self.keyless:
            urls = {s["login"]: f"https://kick.com/{s['login']}" for s in streamers}
            res = probe_streams(list(urls.values()))
            for s in streamers:
                m = res.get(urls[s["login"]])
                if not m:
                    continue
                if game_ok(self.cfg, s, m["category"], m["title"]):
                    out[s["login"]] = LiveInfo("kick", s["login"], m["author"] or s["login"], urls[s["login"]],
                                               m["category"], m["title"])
                else:
                    self.wrong.note(s["key"], m["category"])
            return out
        for s in streamers:
            try:
                ch = self.channel(s["login"])
            except Exception as e:
                log(f"Kick check failed for {s['login']} ({e})", "kick")
                continue
            if not ch or not (ch.get("stream") or {}).get("is_live"):
                continue
            game = (ch.get("category") or {}).get("name", "")
            if game_ok(self.cfg, s, game):
                out[s["login"]] = LiveInfo("kick", s["login"], ch.get("slug") or s["login"],
                                           f"https://kick.com/{s['login']}", game, ch.get("stream_title", ""))
        return out

    def chat(self, info, scfg):
        room = scfg.get("chatroom_id") or self._find_chatroom(info.login)
        if not room:
            log("no Kick chatroom_id (see README) - using loudness to find moments instead", f"kick:{info.login}")
            return None
        return KickChat(room, f"kick:{info.login}")

    def _find_chatroom(self, slug):
        try:
            r = requests.get(f"https://kick.com/api/v2/channels/{slug}",
                             headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}, timeout=15)
            if r.ok:
                return (r.json().get("chatroom") or {}).get("id")
        except Exception:
            pass
        return None

    def new_clips(self, info, since_iso):
        return []


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class YouTubePlatform:
    name = "youtube"

    def __init__(self, cfg):
        self.cfg = cfg
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            raise SystemExit("ERROR: yt-dlp is not installed. Run: pip install -r requirements.txt")

    def setup(self, streamers):
        pass

    @staticmethod
    def live_url(login):
        login = login.strip()
        if login.startswith("http"):
            return login.rstrip("/") + ("" if login.rstrip("/").endswith("/live") else "/live")
        if re.fullmatch(r"UC[\w-]{22}", login):
            return f"https://www.youtube.com/channel/{login}/live"
        return f"https://www.youtube.com/@{login.lstrip('@')}/live"

    def check(self, streamers):
        import yt_dlp
        out = {}
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                "logger": _Quiet(), "socket_timeout": 20}
        from .util import youtube_cookies
        if youtube_cookies():
            opts["cookiefile"] = youtube_cookies()          # signed-in YouTube session, if you added one
        for s in streamers:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.live_url(s["raw_login"]), download=False)
            except Exception:
                continue  # not live (yt-dlp raises when a channel has no live stream)
            if not info or info.get("live_status") != "is_live" or not info.get("id"):
                continue
            title = info.get("title", "")
            if game_ok(self.cfg, s, title, " ".join(info.get("tags") or [])):
                out[s["login"]] = LiveInfo(
                    "youtube", s["login"], info.get("channel") or info.get("uploader") or s["login"],
                    f"https://www.youtube.com/watch?v={info['id']}", "", title, video_id=info["id"])
        return out

    def chat(self, info, scfg):
        return YouTubeChat(info.video_id, f"youtube:{info.login}", self.cfg["storage"]["recordings_dir"])

    def new_clips(self, info, since_iso):
        return []


PLATFORMS = {"twitch": TwitchPlatform, "kick": KickPlatform, "youtube": YouTubePlatform}
