"""Live chat readers for Twitch, Kick and YouTube. All read-only, no account needed."""
import collections
import glob
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time

from .util import log


class BaseChat(threading.Thread):
    """Keeps the last 15 minutes of messages as (wall_time, user, text)."""

    def __init__(self, name, keep_seconds=900):
        super().__init__(daemon=True)
        self.name_tag = name
        self.keep_seconds = keep_seconds
        self.messages = collections.deque()
        self.lock = threading.Lock()
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def snapshot(self, t_from, t_to):
        with self.lock:
            return [m for m in self.messages if t_from <= m[0] <= t_to]

    def count_between(self, t_from, t_to, weight_fn=None):
        msgs = self.snapshot(t_from, t_to)
        return sum(weight_fn(m[2]) for m in msgs) if weight_fn else len(msgs)

    def _add(self, user, text, when=None):
        now = time.time()
        with self.lock:
            self.messages.append((when or now, user, text))
            while self.messages and self.messages[0][0] < now - self.keep_seconds:
                self.messages.popleft()

    def run(self):
        backoff = 2
        while not self._halt.is_set():
            try:
                self._session()
                backoff = 2
            except Exception as e:
                if self._halt.is_set():
                    break
                log(f"chat disconnected ({e}); reconnecting in {backoff}s", self.name_tag)
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self):
        raise NotImplementedError


class TwitchChat(BaseChat):
    def __init__(self, channel, name):
        super().__init__(name)
        self.channel = channel.lower()

    def _session(self):
        sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        sock.settimeout(1.0)
        sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.channel}\r\n".encode())
        buf, last_data = b"", time.time()
        try:
            while not self._halt.is_set():
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    if time.time() - last_data > 360:
                        raise ConnectionError("chat silent for 6 minutes")
                    continue
                if not chunk:
                    raise ConnectionError("connection closed")
                last_data = time.time()
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    line = line.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                    elif " PRIVMSG " in line and line.startswith(":"):
                        try:
                            prefix, rest = line[1:].split(" PRIVMSG ", 1)
                            self._add(prefix.split("!", 1)[0], rest.split(" :", 1)[1])
                        except (IndexError, ValueError):
                            pass
        finally:
            sock.close()


KICK_PUSHER = ("wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
               "?protocol=7&client=js&version=8.4.0&flash=false")


class KickChat(BaseChat):
    """Kick chat through the public Pusher websocket the kick.com website itself uses."""

    def __init__(self, chatroom_id, name):
        super().__init__(name)
        self.chatroom_id = chatroom_id

    def _session(self):
        import websocket  # websocket-client
        ws = websocket.create_connection(KICK_PUSHER, timeout=30)
        ws.settimeout(1.0)
        ws.send(json.dumps({"event": "pusher:subscribe",
                            "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"}}))
        last_data = time.time()
        try:
            while not self._halt.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.time() - last_data > 300:
                        raise ConnectionError("chat silent for 5 minutes")
                    continue
                if not raw:
                    raise ConnectionError("connection closed")
                last_data = time.time()
                msg = json.loads(raw)
                event = msg.get("event", "")
                if event == "pusher:ping":
                    ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif event.endswith("ChatMessageEvent"):
                    data = msg.get("data")
                    data = json.loads(data) if isinstance(data, str) else (data or {})
                    user = (data.get("sender") or {}).get("username", "?")
                    self._add(user, str(data.get("content", "")))
        finally:
            ws.close()


class YouTubeChat(BaseChat):
    """YouTube live chat, read by yt-dlp into a file that we follow as it grows."""

    def __init__(self, video_id, name, work_dir):
        super().__init__(name)
        self.video_id = video_id
        self.work_dir = work_dir
        self.proc = None

    def stop(self):
        super().stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _session(self):
        os.makedirs(self.work_dir, exist_ok=True)
        base = os.path.join(self.work_dir, f"ytchat_{self.video_id}")
        for old in glob.glob(base + "*"):
            try:
                os.remove(old)
            except OSError:
                pass
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download",
             "--write-subs", "--sub-langs", "live_chat", "-o", base,
             f"https://www.youtube.com/watch?v={self.video_id}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        handle, path, pos, partial = None, None, 0, ""
        try:
            while not self._halt.is_set():
                if handle is None:
                    found = glob.glob(base + "*live_chat*")
                    if found:
                        path = found[0]
                        handle = open(path, "r", encoding="utf-8", errors="replace")
                    elif self.proc.poll() is not None:
                        raise ConnectionError("yt-dlp could not open the live chat")
                    else:
                        self._halt.wait(1)
                        continue
                chunk = handle.read()
                if chunk:
                    partial += chunk
                    *lines, partial = partial.split("\n")
                    for line in lines:
                        self._parse(line)
                else:
                    if self.proc.poll() is not None:
                        raise ConnectionError("live chat ended")
                    self._halt.wait(1)
        finally:
            if handle:
                handle.close()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def _parse(self, line):
        if not line.strip():
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        for r in _find_key(obj, "liveChatTextMessageRenderer"):
            runs = (r.get("message") or {}).get("runs", [])
            text = "".join(x.get("text") or (x.get("emoji", {}).get("shortcuts") or [""])[0] for x in runs)
            user = (r.get("authorName") or {}).get("simpleText", "?")
            ts = r.get("timestampUsec")
            self._add(user, text, int(ts) / 1e6 if ts else None)


def _find_key(obj, key):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key and isinstance(v, dict):
                    yield v
                else:
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
