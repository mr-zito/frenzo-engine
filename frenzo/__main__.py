import argparse
import os
import shutil
import sys

from .config import load_config, placeholder


def check(cfg_path):
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print(("  OK      " if good else "  MISSING ") + text)

    print("Frenzo setup check\n")
    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (need 3.10+)")
    line(bool(shutil.which("ffmpeg")), "ffmpeg")
    line(bool(shutil.which("ffprobe")), "ffprobe")
    for mod, name in (("streamlink", "streamlink (recording)"), ("faster_whisper", "faster-whisper (captions)"),
                      ("anthropic", "anthropic (Claude)"), ("yt_dlp", "yt-dlp (YouTube)"),
                      ("websocket", "websocket-client (Kick chat)"), ("cv2", "opencv (finds the webcam)"), ("requests", "requests"),
                      ("yaml", "pyyaml")):
        try:
            __import__(mod)
            line(True, name)
        except ImportError:
            line(False, f"{name}  -> pip install -r requirements.txt")
    try:
        cfg = load_config(cfg_path)
    except SystemExit as e:
        line(False, str(e))
        return
    from .ai import _find_claude_code
    has_key = not placeholder(cfg["anthropic"]["api_key"])
    cc = _find_claude_code()
    line(has_key or bool(cc), "Claude: " + ("API key" if has_key else f"Claude Code with your plan ({cc})" if cc
                                            else "no API key and Claude Code not found"))
    plats = {s["platform"] for s in cfg["streamers"]}
    line(bool(cfg["streamers"]), "streamers: " + (", ".join(s["key"] for s in cfg["streamers"]) or "none"))
    if "twitch" in plats:
        good = not placeholder(cfg["twitch"]["client_id"]) and not placeholder(cfg["twitch"]["client_secret"])
        line(True, "Twitch keys in config.yaml" if good else "Twitch: keyless mode (no keys needed)")
        if good:
            try:
                from .twitch import TwitchAPI
                gid = TwitchAPI(cfg["twitch"]["client_id"], cfg["twitch"]["client_secret"]).game_id(cfg["game"]["name"])
                line(bool(gid), f"Twitch login works, category '{cfg['game']['name']}' found")
            except Exception as e:
                line(False, f"Twitch login: {e}")
    if "kick" in plats:
        good = not placeholder(cfg["kick"]["client_id"]) and not placeholder(cfg["kick"]["client_secret"])
        line(True, "Kick keys in config.yaml" if good else "Kick: keyless mode (no keys needed)")
        if good:
            try:
                from .platforms import KickPlatform
                KickPlatform(cfg)._auth()
                line(True, "Kick login works")
            except Exception as e:
                line(False, f"Kick login: {e}")
        for s in cfg["streamers"]:
            if s["platform"] == "kick" and not s.get("chatroom_id"):
                print(f"  TIP     kick:{s['login']} has no chatroom_id: add it for chat-based moments (README step 5)")
    print("\nAll good - run the engine!" if ok else "\nFix the MISSING lines, then run this check again.")


def main():
    p = argparse.ArgumentParser(prog="frenzo", description="Frenzo live-stream clipping engine")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="watch streamers and make clips while they are live (default)")
    sub.add_parser("check", help="check that everything is installed and configured")
    t = sub.add_parser("test", help="make clips from a video file you already have")
    t.add_argument("video")
    t.add_argument("--at", type=float, action="append", help="moment time in seconds (repeatable)")
    t.add_argument("--top", type=int, default=3, help="number of loud moments to try if --at not given")
    t.add_argument("--streamer", default="teststreamer")
    t.add_argument("--no-ai", action="store_true", help="skip Claude, use the simple loudness rule")
    v = sub.add_parser("vods", help="make clips from streams that already ended (past broadcasts)")
    v.add_argument("--hours", type=float, default=36, help="look this far back (default 36 h)")
    v.add_argument("--top", type=int, default=0, help="max clips per past stream (default 0 = no limit)")
    v.add_argument("--url", action="append", help="clip this past-stream/video URL instead (repeatable)")
    v.add_argument("--streamer", help="only this streamer (or the folder name for --url clips)")
    v.add_argument("--any-game", action="store_true", help="don't require the game in the stream title")
    v.add_argument("--no-ai", action="store_true", help="skip Claude, use the simple loudness rule")
    v.add_argument("--report", help="(tests) write moments.csv + report.json here; clips go to <report>/clips")
    r = sub.add_parser("fix", help="re-render existing clips (from their raw.mp4) in the current style")
    r.add_argument("--streamer", help="only this streamer's clips")
    e = sub.add_parser("reedit", help="ask Claude to re-edit one clip folder")
    e.add_argument("folder")
    e.add_argument("--note", help="what you want changed, in your own words")
    e.add_argument("--start", type=float, help="only use raw.mp4 from this second")
    e.add_argument("--end", type=float, help="... to this second")
    e.add_argument("--context", help="what really happens in the clip, written by you")
    c = sub.add_parser("recut", help="re-render one clip folder with your own start/end (no Claude)")
    c.add_argument("folder")
    c.add_argument("--start", type=float, required=True, help="start, seconds into raw.mp4")
    c.add_argument("--end", type=float, required=True, help="end, seconds into raw.mp4")
    c.add_argument("--title")
    c.add_argument("--highlight")
    c.add_argument("--emoji")
    c.add_argument("--focus", type=float, help="0-1: horizontal centre of the gameplay crop")
    c.add_argument("--cam", help="webcam box x,y,w,h (fractions 0-1) or 'none'")
    c.add_argument("--keep-cuts", action="store_true", help="keep the clip's jump cuts (start/end unchanged)")
    k = sub.add_parser("cutrec", help="make a clip from a recording, from your own start/end")
    k.add_argument("path")
    k.add_argument("--start", type=float, required=True)
    k.add_argument("--end", type=float, required=True)
    k.add_argument("--mode", choices=["manual", "claude"], default="manual")
    k.add_argument("--title")
    k.add_argument("--highlight")
    k.add_argument("--emoji")
    k.add_argument("--context")
    k.add_argument("--no-learn", action="store_true", help="don't turn this clip into a learning example")
    ct = sub.add_parser("cut", help="cut clips yourself from a past stream Frenzo downloaded (or a stream file)")
    ct.add_argument("stream", help="the kept stream's id (see the app, or Frenzo Clips/.streams) or a video file")
    ct.add_argument("ranges", nargs="+", help="start-end, e.g. 1:23:45-1:24:30 or 5025-5070 (several allowed)")
    ct.add_argument("--note", help="what happens / what you want, in your own words")
    ct.add_argument("--no-judge", action="store_true", help="no score: Claude only makes the edit plan")
    ct.add_argument("--as-is", action="store_true", help="no Claude at all: your range as it is (needs --title)")
    ct.add_argument("--title")
    ct.add_argument("--streamer", help="whose settings to use (default: the stream's streamer)")
    hv = sub.add_parser("harvest", help="edit a streamer's best viewer Twitch clips Frenzo hasn't made yet")
    hv.add_argument("streamer")
    g = hv.add_mutually_exclusive_group()
    g.add_argument("--since", help="look back this far, e.g. 24h (default), 90m, 2d")
    g.add_argument("--last-stream", action="store_true", help="the streamer's last broadcast")
    g.add_argument("--from", dest="t_from", help="start date/time, e.g. 2026-11-21T10:00")
    hv.add_argument("--to", dest="t_to", help="end date/time (with --from)")
    hv.add_argument("--top", type=int, help="how many moments (default 10)")
    hv.add_argument("--min-clippers", type=int, help="at least this many different clippers (default 2)")
    hv.add_argument("--min-views", type=int, help="at least this many views")
    hv.add_argument("--only", help="(app) comma-separated moment keys to make")
    hv.add_argument("--list", action="store_true", help="only list the ranked moments, make nothing")
    hv.add_argument("--json", action="store_true", help="(app) print the listing as JSON")
    hv.add_argument("--report", help="(tests) write harvest_report.json here; clips go to <report>/clips")
    ln = sub.add_parser("learn", help="learn from your hand-made, deleted, retitled and re-timed clips")
    ln.add_argument("--measure", help="(internal) measure a clip you cut by hand and save it as an example")
    ln.add_argument("--dry-run", action="store_true", help="ask Claude and show the result, save nothing")
    ln.add_argument("--examples", help="use this examples.jsonl instead of the real one (tests)")
    ln.add_argument("--force", action="store_true", help="run even with fewer than 3 examples")
    m = sub.add_parser("merge", help="merge two consecutive hourly recordings into one")
    m.add_argument("a")
    m.add_argument("b")
    a = sub.add_parser("app", help="open the Frenzo app (live, past streams and clips in one window)")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--no-window", action="store_true", help="only start the app server")
    a.add_argument("--tray", action="store_true", help="(kept for Frenzo.exe; the tray icon is always on)")
    a.add_argument("--no-tray", action="store_true", help="no icon next to the clock")
    b = sub.add_parser("build-exe", help="build Frenzo.exe, the desktop app window (Windows)")
    b.add_argument("--if-needed", action="store_true", help="only if missing or out of date")
    sub.add_parser("shortcut", help="put a Frenzo shortcut on the Desktop (Windows)")
    args = p.parse_args()
    from .sfx import ensure_folders
    ensure_folders()

    if args.cmd == "check":
        return check(args.config)
    if args.cmd == "test":
        from .offline import run_test
        cfg = load_config(args.config, required=False)
        return run_test(cfg, args.video, args.at, args.top, args.streamer, args.no_ai)
    if args.cmd == "vods":
        from .vods import run_vods
        cfg = load_config(args.config)
        if args.report:                          # test run: clips go next to the report, nothing else changes
            cfg["storage"]["output_dir"] = os.path.join(os.path.abspath(args.report), "clips")
        return run_vods(cfg, args.hours, args.top, args.url, args.streamer, args.any_game, args.no_ai,
                        report_dir=os.path.abspath(args.report) if args.report else None)
    if args.cmd == "fix":
        from .redo import run_redo
        return run_redo(load_config(args.config), args.streamer)
    if args.cmd == "reedit":
        from .redo import reedit_one
        return reedit_one(load_config(args.config), args.folder, args.note, args.start, args.end, args.context)
    if args.cmd == "recut":
        from .redo import recut_one
        cam = args.cam
        if cam and cam != "none":
            x, y, w, h = (float(v) for v in cam.split(","))
            cam = {"x": x, "y": y, "w": w, "h": h}
        return recut_one(load_config(args.config), args.folder, args.start, args.end, args.title,
                         args.highlight, args.emoji, args.focus, cam, args.keep_cuts)
    if args.cmd == "cutrec":
        from .recordings import cut_clip
        cfg = load_config(args.config)
        folder = cut_clip(cfg, args.path, args.start, args.end, args.mode, args.title,
                          args.highlight, args.emoji, args.context)
        if folder and not args.no_learn:
            # a moment you cut by hand = a moment the engine missed: measure it for learning, in the background
            from .config import ROOT
            from .learning import measure_in_background
            from .util import log
            cfg_path = args.config if os.path.isabs(args.config) else str(ROOT / args.config)
            try:
                measure_in_background(cfg, cfg_path, folder)
                log("learning: measuring this moment in the background (it becomes a lesson for the engine)", "learn")
            except Exception as e:
                log(f"learning: couldn't start measuring ({str(e)[:100]})", "learn")
        return
    if args.cmd == "cut":
        from .streams import cut, parse_ranges
        cfg = load_config(args.config)
        try:
            ranges = parse_ranges(args.ranges)
        except ValueError as e:
            raise SystemExit(f"ERROR: {e}")
        if args.as_is and not args.title:
            raise SystemExit("ERROR: --as-is needs --title")
        folders = cut(cfg, args.stream, ranges, note=args.note, no_judge=args.no_judge,
                      mode="manual" if args.as_is else "claude", title=args.title, streamer=args.streamer)
        from .util import log
        log(f"{len(folders)} of {len(ranges)} cut(s) made", "cut")
        return
    if args.cmd == "harvest":
        from .harvest import harvest
        cfg = load_config(args.config)
        report = {} if (args.report or args.json) else None
        if args.report:
            cfg["storage"]["output_dir"] = os.path.join(os.path.abspath(args.report), "clips")
        harvest(cfg, args.streamer, since=args.since, last_stream=args.last_stream, t_from=args.t_from, t_to=args.t_to,
                top=args.top, min_clippers=args.min_clippers, min_views=args.min_views,
                only=[k for k in (args.only or "").split(",") if k], report=report, dry_run=args.list or args.json)
        if args.json:
            import json as _json
            print("HARVEST_JSON " + _json.dumps(report, ensure_ascii=False))
        if args.report:
            import json as _json
            os.makedirs(args.report, exist_ok=True)
            with open(os.path.join(args.report, "harvest_report.json"), "w", encoding="utf-8") as f:
                _json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        return
    if args.cmd == "learn":
        from .learning import main as learn_main
        learn_main(load_config(args.config), args.config, measure=args.measure, dry_run=args.dry_run,
                   examples_file=args.examples, force=args.force)
        return
    if args.cmd == "merge":
        from .recordings import merge
        cfg = load_config(args.config)
        merge(cfg["storage"]["recordings_dir"], args.a, args.b)
        return
    if args.cmd == "app":
        from .app.server import run_app
        return run_app(args.config, args.port, window=not args.no_window, tray=not args.no_tray)
    if args.cmd == "build-exe":
        from .app.server import build_exe
        return build_exe(args.if_needed)
    if args.cmd == "shortcut":
        from .app.server import make_shortcut
        return make_shortcut()
    from .engine import run_live
    run_live(load_config(args.config))


if __name__ == "__main__":
    main()
