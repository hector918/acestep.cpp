#!/usr/bin/env python3
# testgen.py: two-stage generation test against ace-server, with per-phase
# timing. Run several rounds back-to-back (-n 2) to verify warm-cache
# behavior: under --offload-vae only the VAE reloads between songs, so
# round 2+ should be dramatically faster than a cold STRICT-mode run.
#
# Stdlib only, runs on the docker host (the runtime container has no python).
#
# Usage:
#   python3 testgen.py                              # default song, 1 round
#   python3 testgen.py -n 2                         # two rounds, compare times
#   python3 testgen.py -c "<caption>" -l "<lyrics>" -d 60 -o /tmp/song
#   python3 testgen.py -b http://127.0.0.1:8085     # server base URL

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CAPTION = ("Operatic, mezzo-soprano, ethereal, orchestral, harp, "
                   "strings, slow tempo, cinematic, space ambient")
DEFAULT_LYRICS = """[Verse 1]
(mezzo-soprano, gentle harp, string section)
在寂静的星空下 我听见你的呼唤
穿过银河的尘埃 落在我的窗前

[Chorus]
(full orchestra, ethereal children's choir, organ)
多想能陪你走过每一个太阳日
欢迎宇航员平安回家"""


def api_json(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def wait_job(base, jid, label, timeout=1800):
    """Poll job status; fetch the result body once status flips to done.
    Fails fast on failed/cancelled instead of waiting out the timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = api_json(base, f"/job?id={jid}")["status"]
        except urllib.error.HTTPError as e:
            sys.exit(f"[{label}] job {jid} vanished (HTTP {e.code}) — "
                     "evicted from the 32-entry pool or server restarted")
        if st == "done":
            with urllib.request.urlopen(f"{base}/job?id={jid}&result=1",
                                        timeout=300) as r:
                return r.read(), time.time() - t0
        if st in ("failed", "cancelled"):
            sys.exit(f"[{label}] job {jid} {st} — check: docker logs acestep-cpp")
        time.sleep(1)
    sys.exit(f"[{label}] job {jid} timed out after {timeout}s")


def extract_mp3(body):
    for part in body.split(b"--ace-batch-boundary"):
        if b"audio/mpeg" in part:
            return part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    return None


def one_round(base, args, rnd):
    print(f"\n=== Round {rnd} ===")
    t_round = time.time()

    lm_req = {
        "caption": args.caption,
        "lyrics": args.lyrics,
        "duration": args.duration,
        "vocal_language": args.language,
        "lm_mode": "generate",
    }
    jid = api_json(base, "/lm", lm_req)["id"]
    print(f"[LM]    job {jid}")
    body, t_lm = wait_job(base, jid, "LM")
    songs = json.loads(body)
    print(f"[LM]    done in {t_lm:.1f}s "
          f"(bpm={songs[0].get('bpm')}, dur={songs[0].get('duration')}s, "
          f"key={songs[0].get('keyscale')!r})")

    synth_req = songs[0]
    synth_req["output_format"] = "mp3"
    jid = api_json(base, "/synth", synth_req)["id"]
    print(f"[Synth] job {jid}")
    body, t_synth = wait_job(base, jid, "Synth")

    mp3 = extract_mp3(body)
    if not mp3:
        sys.exit("[Synth] no audio/mpeg part in multipart response")
    out = f"{args.output}_{rnd}.mp3"
    with open(out, "wb") as f:
        f.write(mp3)

    total = time.time() - t_round
    print(f"[Synth] done in {t_synth:.1f}s")
    print(f"[Round {rnd}] total {total:.1f}s  ->  {out} ({len(mp3)//1024} KB)")
    return t_lm, t_synth, total


def main():
    ap = argparse.ArgumentParser(description="ace-server generation test")
    ap.add_argument("-b", "--base", default="http://127.0.0.1:8085")
    ap.add_argument("-c", "--caption", default=DEFAULT_CAPTION)
    ap.add_argument("-l", "--lyrics", default=DEFAULT_LYRICS)
    ap.add_argument("-d", "--duration", type=int, default=60)
    ap.add_argument("-g", "--language", default="zh")
    ap.add_argument("-n", "--rounds", type=int, default=1)
    ap.add_argument("-o", "--output", default="song",
                    help="output prefix, files land as <prefix>_<round>.mp3")
    args = ap.parse_args()

    health = api_json(args.base, "/health")
    if health.get("status") != "ok":
        sys.exit(f"server unhealthy: {health}")
    print(f"server ok at {args.base}")

    results = []
    for rnd in range(1, args.rounds + 1):
        results.append(one_round(args.base, args, rnd))

    if len(results) > 1:
        print("\n=== Summary (warm-cache check) ===")
        for i, (t_lm, t_synth, total) in enumerate(results, 1):
            print(f"round {i}: LM {t_lm:6.1f}s | synth {t_synth:6.1f}s "
                  f"| total {total:6.1f}s")
        d = results[0][2] - results[1][2]
        print(f"round 2 vs round 1: {d:+.1f}s "
              "(offload-vae warm: expect round 2 to only pay ~3s VAE load)")


if __name__ == "__main__":
    main()
