"""A stand-in for ``codex app-server``: logs what it is sent, answers by mode.

Run as ``python fake_codex_app_server.py <mode> <log path> [args...]`` (the
tests wrap it in a shell script so the client launches it like the real
binary, with ``app-server`` as its argument). It logs its arguments, PATH and
pid, then every method it receives, one line each.

Modes:
  ok           answers id 1 and id 2; the reply carries one weekly window,
               two banked resets and the plan type at the top level
  realshape    as ok, with the plan type inside ``rateLimits`` (as the CLI
               answers today) and a null secondary window
  nocredits    as ok, without ``rateLimitResetCredits``
  emptylimits  an empty ``rateLimits``, two banked resets
  rpcerror     answers id 2 with a JSON-RPC error naming an email address
  noisy        before the reply: a log line, a rate-limit notification and a
               server request that reuses id 2
  hang         reads the first message and never answers
  exit         exits with status 3 before reading anything
  flood        writes two MiB of one JSON line instead of answering
  answer-then-hang-ignore-term
               answers like ok and logs ANSWERED, then ignores SIGTERM
               and stays alive after its input closes (only SIGKILL ends it)
"""

import json
import os
import signal
import sys
import time

mode = sys.argv[1]
log = open(sys.argv[2], "a", encoding="utf-8")
log.write("ARGS " + " ".join(sys.argv[3:]) + "\n")
log.write("PATH " + os.environ.get("PATH", "") + "\n")
log.write("PID " + str(os.getpid()) + "\n")
log.flush()

WEEKLY = {"usedPercent": 56, "windowDurationMins": 10080, "resetsAt": 1793774344}


def send(message):
    print(json.dumps(message), flush=True)


def reply_for(msg_id):
    if mode == "realshape":
        return {"rateLimits": {"limitId": "codex", "planType": "pro", "primary": WEEKLY, "secondary": None},
                "rateLimitResetCredits": {"availableCount": 2, "credits": None}}
    if mode == "nocredits":
        return {"rateLimits": {"primary": WEEKLY}, "planType": "pro"}
    if mode == "emptylimits":
        return {"rateLimits": {}, "rateLimitResetCredits": {"availableCount": 2}}
    return {"rateLimits": {"primary": WEEKLY}, "rateLimitResetCredits": {"availableCount": 2}, "planType": "pro"}


if mode == "exit":
    sys.exit(3)
if mode == "answer-then-hang-ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

for line in sys.stdin:
    msg = json.loads(line)
    log.write("METHOD " + str(msg.get("method", "")) + "\n")
    log.flush()
    if mode == "hang":
        time.sleep(60)
    if msg.get("method") == "initialize":
        send({"id": msg["id"], "result": {"userAgent": "fake", "platformOs": "macos"}})
    elif msg.get("method") == "account/rateLimits/read":
        if mode == "flood":
            sys.stdout.write('{"pad": "' + "x" * (2 * 1024 * 1024) + '"}\n')
            sys.stdout.flush()
            time.sleep(60)
        if mode == "noisy":
            print("not json, a log line", flush=True)
            send({"method": "account/rateLimits/updated", "params": {}})
            send({"id": msg["id"], "method": "item/tool/requestUserInput", "params": {}})
        if mode == "rpcerror":
            send({"id": msg["id"], "error": {"code": -32600, "message": "not logged in as sam@example.com"}})
        else:
            send({"id": msg["id"], "result": reply_for(msg["id"])})
            log.write("ANSWERED\n")
            log.flush()

if mode == "answer-then-hang-ignore-term":
    time.sleep(60)  # input closed; stays until killed
