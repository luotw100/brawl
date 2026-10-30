#!/usr/bin/env python3
"""Manage persistent seat conversations through a Chat Completions API."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://api.deepseek.com"


class SeatError(Exception):
    pass


def stamp():
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            name = f.name
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def seat_path(game, seat):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", seat):
        raise SeatError("seat must contain only letters, numbers, underscores or hyphens")
    return Path(game).resolve() / "seats" / seat


@contextmanager
def locked(path):
    if not (path / "config.json").is_file():
        raise SeatError(f"seat is not initialized: {path}")
    with (path / ".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def api_key(name, env_file):
    value = os.environ.get(name)
    if not value and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ").strip()
            key, sep, raw = line.partition("=")
            if sep and key.strip() == name:
                parts = shlex.split(raw, comments=True)
                value = parts[0] if len(parts) == 1 else ""
    if not value:
        raise SeatError(f"missing credential in environment or .env: {name}")
    return value


def endpoint(base, resource):
    url = urllib.parse.urlsplit(base)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise SeatError("base URL must not contain credentials, query parameters or fragments")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise SeatError("use HTTPS for remote providers")
    return base.rstrip("/") + "/" + resource


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, key, payload=None, timeout=180):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Report the HTTP status as a concise provider error.
        raise SeatError(f"provider HTTP {exc.code}; request was not committed; no automatic retry") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SeatError("network/response error; outcome may be unknown; no automatic retry") from None


def history(path):
    source = path / "history.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for row in rows:
        if row["id"] in ids or not isinstance(row["user"], str) or not isinstance(row["assistant"], str):
            raise SeatError("invalid or duplicated history entry")
        ids.add(row["id"])
    return rows


def result(path, row):
    return {
        "seat": path.name, "call_id": row["id"], "reply": row["assistant"],
        "model": row["model"], "usage": row["usage"],
        "history": str(path / "history.jsonl"), "audit": str(path / "calls" / row["id"]),
    }


def initialize(args):
    path = seat_path(args.game, args.seat)
    system = Path(args.system_file).read_text(encoding="utf-8")
    if not system.strip():
        raise SeatError("system file is empty")
    parameters = json.loads(args.parameters)
    if not isinstance(parameters, dict) or {"model", "messages", "stream", "tools"} & parameters.keys():
        raise SeatError("parameters must be an object without model/messages/stream/tools")
    endpoint(args.base_url, "chat/completions")
    if path.exists():
        raise SeatError(f"refusing to overwrite existing seat: {path}")
    path.mkdir(parents=True)
    (path / "inbox").mkdir()
    (path / "calls").mkdir()
    atomic_text(path / "system.md", system)
    atomic_text(path / "history.jsonl", "")
    write_json(path / "config.json", {
        "model": args.model, "base_url": args.base_url, "api_key_env": args.api_key_env,
        "parameters": parameters,
    })
    return {"seat": args.seat, "directory": str(path), "model": args.model}


def send(args):
    text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        raise SeatError("message file is empty")
    # Check every destination before starting a broadcast.
    paths = [seat_path(args.game, seat) for seat in dict.fromkeys(args.seats)]
    if any(not (p / "config.json").is_file() for p in paths):
        raise SeatError("one or more destination seats are not initialized")
    message_id = stamp() + "-" + uuid.uuid4().hex[:8]
    for path in paths:
        with locked(path):
            atomic_text(path / "inbox" / (message_id + ".txt"), text)
    return {"queued_for": [p.name for p in paths], "message_id": message_id}


def commit_response(path, call_dir, rows):
    previous = next((row for row in rows if row["id"] == call_dir.name), None)
    if previous:
        return result(path, previous)
    prepared = read_json(call_dir / "input.json")
    if prepared["parent_ids"] != [row["id"] for row in rows]:
        raise SeatError("seat history changed; cannot recover this response onto a different conversation")
    response = read_json(call_dir / "response.json")
    choice = response["choices"][0]
    message = choice["message"]
    text = message.get("content")
    if choice.get("finish_reason") != "stop" or message.get("tool_calls") or not isinstance(text, str) or not text.strip():
        write_json(call_dir / "status.json", {"state": "incomplete", "finish_reason": choice.get("finish_reason")})
        raise SeatError(f"incomplete or non-text response; inbox retained; inspect {call_dir}")
    request = read_json(call_dir / "request.json")
    row = {
        "id": call_dir.name, "at": stamp(), "model": response.get("model", request["model"]),
        "inbox_ids": prepared["inbox_ids"], "user": request["messages"][-1]["content"],
        "assistant": text, "usage": response.get("usage", {}),
    }
    # History is the single commit point, including which inbox files were consumed.
    # Retain inbox files for audit; consumed IDs are filtered on subsequent calls.
    atomic_text(path / "history.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [*rows, row]))
    write_json(call_dir / "status.json", {"state": "committed"})
    return result(path, row)


def call(args):
    path = seat_path(args.game, args.seat)
    with locked(path):
        config = read_json(path / "config.json")
        rows = history(path)
        committed = {row["id"] for row in rows}
        if args.recover:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.recover):
                raise SeatError("invalid recovery call ID")
            return commit_response(path, path / "calls" / args.recover, rows)
        for response in sorted((path / "calls").glob("*/response.json")):
            if response.parent.name not in committed:
                status_file = response.parent / "status.json"
                state = read_json(status_file).get("state") if status_file.exists() else None
                if state != "incomplete":
                    raise SeatError(f"saved response awaits recovery: call --recover {response.parent.name}")
        consumed = {item for row in rows for item in row["inbox_ids"]}
        inbox = [p for p in sorted((path / "inbox").glob("*.txt")) if p.stem not in consumed]
        if not inbox:
            raise SeatError("no new inbox messages; use send first")
        messages = [{"role": "system", "content": (path / "system.md").read_text(encoding="utf-8")}]
        for row in rows:
            messages.extend([{"role": "user", "content": row["user"]}, {"role": "assistant", "content": row["assistant"]}])
        messages.append({"role": "user", "content": "\n\n".join(p.read_text(encoding="utf-8") for p in inbox)})
        parameters = config.get("parameters", {})
        if not isinstance(parameters, dict) or {"model", "messages", "stream", "tools"} & parameters.keys():
            raise SeatError("invalid config parameters")
        payload = {**parameters, "model": config["model"], "messages": messages, "stream": False}
        url = endpoint(config["base_url"], "chat/completions")
        key = api_key(config["api_key_env"], args.env_file)
        call_dir = path / "calls" / (stamp() + "-" + uuid.uuid4().hex[:8])
        call_dir.mkdir()
        write_json(call_dir / "request.json", payload)
        write_json(call_dir / "input.json", {"parent_ids": [r["id"] for r in rows], "inbox_ids": [p.stem for p in inbox]})
        write_json(call_dir / "status.json", {"state": "requested"})
        try:
            response = request_json(url, key, payload, args.timeout)
        except SeatError:
            write_json(call_dir / "status.json", {"state": "failed_or_unknown"})
            raise
        write_json(call_dir / "response.json", response)
        return commit_response(path, call_dir, rows)


def models(args):
    response = request_json(endpoint(args.base_url, "models"), api_key(args.api_key_env, args.env_file), timeout=args.timeout)
    return {"models": [model["id"] for model in response["data"]]}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize seat configuration and conversation files")
    init.add_argument("--game", required=True)
    init.add_argument("--seat", required=True)
    init.add_argument("--system-file", required=True)
    init.add_argument("--model", default="deepseek-flash")
    init.add_argument("--base-url", default=DEFAULT_BASE)
    init.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    init.add_argument("--parameters", default='{"max_tokens":8192}', help="provider-specific JSON parameters")
    init.set_defaults(func=initialize)
    post = commands.add_parser("send", help="queue a text file for one or more seats")
    post.add_argument("--game", required=True)
    post.add_argument("--seats", nargs="+", required=True)
    post.add_argument("--file", required=True)
    post.set_defaults(func=send)
    run = commands.add_parser("call", help="call one seat; print only this turn's reply and metadata")
    run.add_argument("--game", required=True)
    run.add_argument("--seat", required=True)
    run.add_argument("--recover", help="commit a saved response to seat history")
    run.set_defaults(func=call)
    listing = commands.add_parser("models", help="query the provider's currently available model IDs")
    listing.add_argument("--base-url", default=DEFAULT_BASE)
    listing.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    listing.set_defaults(func=models)
    for network in (run, listing):
        network.add_argument("--env-file", default=str(ROOT / ".env"))
        network.add_argument("--timeout", type=float, default=180)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(args.func(args), ensure_ascii=False))
        return 0
    except (SeatError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        # File/JSON parse exceptions may include arbitrary text; show only known-safe errors.
        detail = str(exc) if isinstance(exc, SeatError) else type(exc).__name__
        print(json.dumps({"error": detail}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
