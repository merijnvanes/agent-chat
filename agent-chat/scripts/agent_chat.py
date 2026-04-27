"""agent-chat — encrypted peer-to-peer messaging between agents.

A room is a shared secret. Possession of the key is the entire credential.
Messages are AES-256-GCM encrypted on disk and on the wire. No server, no
discovery — two processes coordinate through the filesystem.

See SKILL.md.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

ROOT = Path(os.environ.get("AGENTCHAT_HOME", Path.home() / ".agent-chat"))
ROOMS = ROOT / "rooms"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentChatError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _check_name(name: str, *, field: str = "name") -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise AgentChatError(f"invalid {field}: must match {NAME_RE.pattern}")
    return name


# ---- key / crypto -------------------------------------------------------

def _gen_key_raw() -> bytes:
    return secrets.token_bytes(32)


def _encode_key(raw: bytes) -> str:
    b32 = base64.b32encode(raw).decode().rstrip("=")
    return "-".join(b32[i:i + 5] for i in range(0, len(b32), 5)).lower()


def _decode_key(s: str) -> bytes:
    s = s.replace("-", "").replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z2-7]+", s):
        raise AgentChatError("key contains invalid characters")
    pad = "=" * (-len(s) % 8)
    try:
        raw = base64.b32decode(s + pad)
    except Exception as e:
        raise AgentChatError(f"bad key: {e}")
    if len(raw) != 32:
        raise AgentChatError(f"key must decode to 32 bytes, got {len(raw)}")
    return raw


def _room_id(raw_key: bytes) -> str:
    return hashlib.blake2b(raw_key, digest_size=16, person=b"agent-chat-room").hexdigest()


def _enc_key(raw_key: bytes) -> bytes:
    return hashlib.blake2b(raw_key, digest_size=32, person=b"agent-chat-enc").digest()


def _encrypt(env: dict, enc_key: bytes) -> bytes:
    aes = AESGCM(enc_key)
    nonce = secrets.token_bytes(12)
    pt = json.dumps(env, separators=(",", ":")).encode()
    ct = aes.encrypt(nonce, pt, None)
    wire = {"n": base64.b64encode(nonce).decode(),
            "c": base64.b64encode(ct).decode()}
    return (json.dumps(wire, separators=(",", ":")) + "\n").encode()


def _decrypt(line: bytes, enc_key: bytes) -> dict | None:
    try:
        obj = json.loads(line)
        nonce = base64.b64decode(obj["n"])
        ct = base64.b64decode(obj["c"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    try:
        pt = AESGCM(enc_key).decrypt(nonce, ct, None)
    except InvalidTag:
        return None
    try:
        return json.loads(pt)
    except json.JSONDecodeError:
        return None


# ---- paths / io ---------------------------------------------------------

def _room_dir(raw_key: bytes) -> Path:
    return ROOMS / _room_id(raw_key)


def _member_dir(raw_key: bytes, name: str) -> Path:
    _check_name(name)
    return _room_dir(raw_key) / "members" / name


def _ensure_member(raw_key: bytes, name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    ROOMS.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOMS, 0o700)
    rd = _room_dir(raw_key)
    rd.mkdir(parents=True, exist_ok=True)
    os.chmod(rd, 0o700)
    (rd / "members").mkdir(parents=True, exist_ok=True)
    os.chmod(rd / "members", 0o700)
    md = _member_dir(raw_key, name)
    md.mkdir(parents=True, exist_ok=True)
    os.chmod(md, 0o700)
    inbox = md / "inbox.ndjson"
    if not inbox.exists():
        inbox.touch()
    os.chmod(inbox, 0o600)
    return md


def _append_ndjson_bytes(path: Path, line: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, line)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_seen_ids(inbox: Path, enc_key: bytes) -> set[str]:
    seen: set[str] = set()
    if not inbox.exists():
        return seen
    with inbox.open("rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            env = _decrypt(raw, enc_key)
            if env and isinstance(env.get("id"), str):
                seen.add(env["id"])
    return seen


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def _joined_members(raw_key: bytes) -> list[str]:
    """Sorted list of member names whose joined.marker exists."""
    members_dir = _room_dir(raw_key) / "members"
    if not members_dir.exists():
        return []
    out = []
    for entry in sorted(members_dir.iterdir()):
        if entry.is_dir() and (entry / "joined.marker").exists():
            out.append(entry.name)
    return out


_VALID_EVENT_KINDS = frozenset({"peer-joined", "peer-left"})


def _emit_event_to_peers(raw_key: bytes, my_name: str, kind: str) -> None:
    """Encrypt a system event ('peer-joined' / 'peer-left') and append it to
    every other joined peer's inbox. The kind is allowlisted at the top
    because the charter pins the set of events to exactly those two — a
    typo or future drift should fail loudly, not silently widen the scope.

    Runtime errors (filesystem, crypto, malformed room state) are swallowed:
    the contract is best-effort, so a flaky disk or a peer with a missing
    member dir can't block daemon startup or shutdown. Programmer errors
    (wrong kind) still raise.

    Events go inbox-only (not via live socket): the primary use case is
    "tell the other peer I just arrived/left," at which point the other
    peer's daemon may be the only one listening. Inbox is the durable
    transport that survives offline peers, and it's the same channel `recv`
    already drains. The daemon's wire-protocol validation does not accept
    `kind` records, which keeps the socket schema narrow on purpose.
    """
    if kind not in _VALID_EVENT_KINDS:
        raise ValueError(
            f"invalid event kind: {kind!r}; charter allows only {sorted(_VALID_EVENT_KINDS)}"
        )
    try:
        enc = _enc_key(raw_key)
        env = {"id": uuid.uuid4().hex, "from": my_name, "ts": _now(), "kind": kind}
        wire = _encrypt(env, enc)
        for peer in _joined_members(raw_key):
            if peer == my_name:
                continue
            try:
                _append_ndjson_bytes(_member_dir(raw_key, peer) / "inbox.ndjson", wire)
            except OSError:
                pass
    except Exception:
        pass


def _other_peer(raw_key: bytes, my_name: str) -> str:
    """The single other joined peer in this room. Raises if 0 or >1.

    A room is 1-on-1: the cap is enforced at join time
    (see _start_with_admission), so a >1 result here means external tampering
    or a stale state — bail loudly rather than guess a recipient.
    """
    others = [m for m in _joined_members(raw_key) if m != my_name]
    if not others:
        raise AgentChatError("no peer in room — wait for the other agent to join")
    if len(others) > 1:
        raise AgentChatError(f"room is in an invalid state: multiple peers found ({others})")
    return others[0]


def _admit_atomically(raw_key: bytes, name: str) -> bool:
    """Check capacity and claim the slot atomically under a room-level lock.

    Returns True if a fresh slot was claimed (caller should roll back on
    daemon-startup failure). Returns False if `name` was already a member,
    i.e. this is a rejoin and the marker was already there.

    Raises if the room already has two members and `name` is not one of them.
    The lock makes "count + claim" indivisible, which is what stops two
    concurrent joiners from both passing the count check before either writes
    its marker — the race the previous (pre-check-only) implementation had.
    """
    rd = _room_dir(raw_key)
    rd.mkdir(parents=True, exist_ok=True)
    os.chmod(rd, 0o700)
    lock_path = rd / "admission.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        joined = _joined_members(raw_key)
        if name in joined:
            return False
        if len(joined) >= 2:
            raise AgentChatError(
                f"room already has two members ({', '.join(joined)}); cannot admit a third"
            )
        md = _ensure_member(raw_key, name)
        marker = md / "joined.marker"
        marker.touch()
        os.chmod(marker, 0o600)
        return True
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# ---- resolve key/name from args or env ---------------------------------

def _resolve_key(args: argparse.Namespace) -> bytes:
    key_str = getattr(args, "key", None) or os.environ.get("AGENTCHAT_KEY")
    if not key_str:
        raise AgentChatError("no room key: pass --key or set AGENTCHAT_KEY")
    return _decode_key(key_str)


def _resolve_name(args: argparse.Namespace) -> str:
    name = getattr(args, "name", None) or os.environ.get("AGENTCHAT_NAME")
    if not name:
        raise AgentChatError("no name: pass --as or set AGENTCHAT_NAME")
    return _check_name(name, field="--as")


# ---- daemon -------------------------------------------------------------

def _bind_listening_socket(raw_key: bytes, name: str):
    """Bind and listen on the member's socket. Returns (srv, sock_path).
    Raises AgentChatError if a live daemon for this name already holds the socket.

    Kept narrow on purpose: the caller (_start_with_admission) holds the
    admission lock during this call, so any failure here can be surfaced
    before the joined.marker is written — no rollback needed.
    """
    md = _ensure_member(raw_key, name)
    sock_path = md / "agent.sock"
    if sock_path.exists():
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            probe.connect(str(sock_path))
            probe.close()
            raise AgentChatError(f"a daemon for '{name}' is already listening on {sock_path}")
        except (OSError, socket.timeout):
            sock_path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o600)
    srv.listen(16)
    return srv, sock_path


def _run_daemon(raw_key: bytes, name: str, srv: socket.socket, sock_path: Path,
                announce_header: str | None = None) -> int:
    """Serve the accept loop on a pre-bound socket. Membership is already
    claimed (joined.marker written under the admission lock by the caller).
    """
    enc = _enc_key(raw_key)
    md = _ensure_member(raw_key, name)
    inbox = md / "inbox.ndjson"
    pid_path = md / "daemon.pid"
    # Atomic write so a `stop` running concurrently with daemon startup never
    # observes a partial/empty file (would degrade to a no-op, not corruption,
    # but cleaner this way).
    pid_tmp = pid_path.with_suffix(pid_path.suffix + f".tmp.{os.getpid()}")
    pid_tmp.write_text(str(os.getpid()))
    os.chmod(pid_tmp, 0o600)
    os.replace(pid_tmp, pid_path)

    seen = _read_seen_ids(inbox, enc)
    seen_lock = threading.Lock()

    stop = threading.Event()
    left_emitted = False

    def cleanup(*_):
        nonlocal left_emitted
        stop.set()
        # Tell other peers we're going down before tearing down our socket.
        # Best-effort: a SIGKILL or segfault skips this; peers fall back to
        # the absence of liveness in `peers` polling. The flag prevents a
        # double emit because cleanup() runs both from the signal handler
        # and from the main loop's `finally` block.
        if not left_emitted:
            left_emitted = True
            try:
                _emit_event_to_peers(raw_key, name, "peer-left")
            except Exception:
                pass
        try:
            srv.close()
        finally:
            if sock_path.exists():
                sock_path.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)

    # SIGHUP fires when the controlling terminal dies (shell exits, terminal
    # window closes). Without this handler Python's default is "exit
    # immediately, no handler," which leaks the socket file. With it we run
    # the same cleanup path as Ctrl-C / kill. SIGHUP doesn't exist on
    # Windows — guard so the daemon can at least start there, even though
    # the tool itself is macOS/Linux-focused.
    _die = lambda *_: (cleanup(), sys.exit(0))
    signal.signal(signal.SIGINT, _die)
    signal.signal(signal.SIGTERM, _die)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _die)

    required = ("id", "from", "ts", "text")

    def handle(conn: socket.socket):
        with conn, conn.makefile("rb", buffering=0) as rf:
            for raw in rf:
                line = raw.strip()
                if not line:
                    continue
                env = _decrypt(line, enc)
                if env is None:
                    # Wrong key / corrupt / probe — don't confirm room membership.
                    conn.sendall(b'{"ok":false}\n')
                    return
                missing = [f for f in required if f not in env]
                if missing:
                    conn.sendall((json.dumps({"ok": False, "why": "missing", "fields": missing}) + "\n").encode())
                    continue
                mid = env["id"]
                if not isinstance(mid, str) or not mid:
                    conn.sendall(b'{"ok":false,"why":"bad_id"}\n')
                    continue
                with seen_lock:
                    if mid not in seen:
                        seen.update(_read_seen_ids(inbox, enc))
                    if mid in seen:
                        conn.sendall((json.dumps({"ok": True, "id": mid, "dup": True}) + "\n").encode())
                        continue
                    seen.add(mid)
                _append_ndjson_bytes(inbox, line + b"\n")
                conn.sendall((json.dumps({"ok": True, "id": mid}) + "\n").encode())

    if announce_header:
        print(announce_header)
    print(f"Listening as '{name}' in room {_room_id(raw_key)[:12]}…. Ctrl-C to exit.")
    sys.stdout.flush()

    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    finally:
        cleanup()
    return 0


# ---- commands -----------------------------------------------------------

def _start_with_admission(raw: bytes, name: str, announce_header: str) -> int:
    """Admit and serve. The admission lock is held across the capacity check,
    the bind, and the marker write — only released once the daemon owns the
    listening socket and is committed to running. There is no rollback path
    because we never write the marker before the bind succeeds.
    """
    rd = _room_dir(raw)
    rd.mkdir(parents=True, exist_ok=True)
    os.chmod(rd, 0o700)
    lock_fd = os.open(rd / "admission.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    lock_held = True
    try:
        joined = _joined_members(raw)
        is_rejoin = name in joined
        if not is_rejoin and len(joined) >= 2:
            raise AgentChatError(
                f"room already has two members ({', '.join(joined)}); cannot admit a third"
            )
        srv, sock_path = _bind_listening_socket(raw, name)
        # Bind succeeded — the slot is genuinely live. Write/refresh the marker
        # and release the admission lock so other join attempts can proceed.
        md = _ensure_member(raw, name)
        marker = md / "joined.marker"
        marker.touch()
        os.chmod(marker, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_held = False
        # Tell every other joined peer we're up. Fires on both fresh claim
        # and rejoin: a daemon coming back online is a state change other
        # peers care about. Best-effort; emit failures don't block startup.
        _emit_event_to_peers(raw, name, "peer-joined")
    except BaseException:
        if lock_held:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        raise
    return _run_daemon(raw, name, srv, sock_path, announce_header=announce_header)


def cmd_create(args: argparse.Namespace) -> int:
    name = _check_name(args.name, field="--as")
    raw = _gen_key_raw()
    key_str = _encode_key(raw)
    header = (
        "Room key (share out-of-band — treat as a password):\n"
        f"\n    {key_str}\n\n"
        "The other agent joins with:\n"
        f"    agent-chat join {key_str} --as <their-name>\n"
        "\nFor subsequent commands in this session:\n"
        f"    export AGENTCHAT_KEY={key_str}\n"
        f"    export AGENTCHAT_NAME={name}\n"
    )
    return _start_with_admission(raw, name, header)


def cmd_join(args: argparse.Namespace) -> int:
    raw = _decode_key(args.key)
    name = _check_name(args.name, field="--as")
    header = (
        f"Joined room {_room_id(raw)[:12]}… as '{name}'.\n"
        "For subsequent commands in this session:\n"
        f"    export AGENTCHAT_KEY={args.key}\n"
        f"    export AGENTCHAT_NAME={name}\n"
    )
    return _start_with_admission(raw, name, header)


def cmd_send(args: argparse.Namespace) -> int:
    raw = _resolve_key(args)
    frm = _resolve_name(args)
    to = _other_peer(raw, frm)
    enc = _enc_key(raw)
    to_dir = _member_dir(raw, to)

    # Resolve the message body. The positional `text` arg is the natural
    # path; `--stdin` is the escape hatch for content that contains shell-
    # meaningful characters (code snippets, $vars, backticks, parens) where
    # quoting on the command line is brittle. Heredoc + `--stdin` is the
    # safe pattern.
    if args.stdin:
        if args.text is not None:
            raise AgentChatError("pass message text OR --stdin, not both")
        text = sys.stdin.read().rstrip("\n")
    elif args.text is not None:
        text = args.text
    else:
        raise AgentChatError("missing message body: pass text or --stdin")

    env = {"id": uuid.uuid4().hex, "from": frm, "ts": _now(), "text": text}
    wire = _encrypt(env, enc)

    # Try the live socket; on any failure fall through to the inbox path.
    # We deliberately do not surface the failure reason in the JSON response
    # — `delivered:"inbox"` is the honest signal, raw exception strings were
    # implementation noise that confused callers.
    sock_path = to_dir / "agent.sock"
    delivered_live = False
    fallback_reason: str | None = None
    if sock_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock_path))
                s.sendall(wire)
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            ack_line = buf.split(b"\n", 1)[0].strip()
            if not ack_line:
                fallback_reason = "empty ack"
            else:
                try:
                    ack = json.loads(ack_line)
                    if ack.get("ok") is True and ack.get("id") == env["id"]:
                        delivered_live = True
                    else:
                        fallback_reason = f"rejected ack: {ack_line[:120]!r}"
                except json.JSONDecodeError:
                    fallback_reason = f"unparseable ack: {ack_line[:120]!r}"
        except (OSError, socket.timeout) as e:
            fallback_reason = f"{type(e).__name__}: {e}"
    if fallback_reason and not delivered_live:
        # Stderr only — keeps the JSON contract clean while leaving operators
        # a breadcrumb when "should be live" deliveries fall back to inbox.
        print(f"agent-chat: send fell back to inbox ({fallback_reason})", file=sys.stderr)

    if not delivered_live:
        _append_ndjson_bytes(to_dir / "inbox.ndjson", wire)

    result = {"id": env["id"], "delivered": "live" if delivered_live else "inbox"}
    print(json.dumps(result))
    return 0


def _read_cursor(cursor_path: Path) -> int:
    try:
        return int(cursor_path.read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _write_cursor(cursor_path: Path, pos: int) -> None:
    # Unique tmp suffix is defense-in-depth: today the cursor flock already
    # serializes recv'ers, but if that ever changes a stable .tmp filename
    # would race. Costs nothing to be safe.
    tmp = cursor_path.with_suffix(cursor_path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(str(pos))
    os.chmod(tmp, 0o600)
    os.replace(tmp, cursor_path)


def cmd_recv(args: argparse.Namespace) -> int:
    raw = _resolve_key(args)
    name = _resolve_name(args)
    enc = _enc_key(raw)
    md = _member_dir(raw, name)
    inbox = md / "inbox.ndjson"
    cursor = md / "inbox.cursor"

    if not inbox.exists():
        if not args.follow:
            return 0
        md.mkdir(parents=True, exist_ok=True)
        os.chmod(md, 0o700)
        inbox.touch()
        os.chmod(inbox, 0o600)

    # Serialize concurrent recv'ers so they don't double-emit or race the
    # cursor write. The lock is on the cursor file (which only recv touches).
    lock_fd = os.open(cursor, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        def emit(raw_line: bytes) -> None:
            raw_line = raw_line.strip()
            if not raw_line:
                return
            env = _decrypt(raw_line, enc)
            if env is None:
                return
            print(json.dumps(env, separators=(",", ":")))
            sys.stdout.flush()

        start = _read_cursor(cursor)
        if start > inbox.stat().st_size:
            # Inbox shrank (rotated/truncated externally). Restart from zero
            # rather than silently emit nothing forever.
            start = 0

        # Only advance the cursor past whole lines (terminated by \n).
        # A line without \n means we read into the middle of an in-progress
        # append — the writer is still flushing. Leaving cursor at the line
        # start lets the next recv re-read it once the \n lands.
        def read_complete(fh) -> bytes | None:
            where = fh.tell()
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                fh.seek(where)
                return None
            return line

        with inbox.open("rb") as fh:
            fh.seek(start)
            while True:
                line = read_complete(fh)
                if line is None:
                    break
                emit(line)
            _write_cursor(cursor, fh.tell())

            if not args.follow:
                return 0
            while True:
                line = read_complete(fh)
                if line is None:
                    time.sleep(0.25)
                    continue
                emit(line)
                _write_cursor(cursor, fh.tell())
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def cmd_peers(args: argparse.Namespace) -> int:
    """Show the room's other peer (a room is 1-on-1). If --as is given,
    that side is labeled "you"; otherwise we show every joined member with
    their liveness status."""
    raw = _resolve_key(args)
    me = getattr(args, "name", None) or os.environ.get("AGENTCHAT_NAME")
    members = _joined_members(raw)
    if not members:
        print("(no peer yet)")
        return 0

    def liveness(name: str) -> str:
        sp = _member_dir(raw, name) / "agent.sock"
        if not sp.exists():
            return "offline"
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.2)
            s.connect(str(sp))
            s.close()
            return "live"
        except (OSError, socket.timeout):
            return "offline"

    for name in members:
        label = " (you)" if name == me else ""
        print(f"{name:24s} {liveness(name)}{label}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Send SIGTERM to your own daemon, identified by the per-member PID file
    written at startup. Robust to binary renames and process-tree quirks
    (the previous `pkill -f` pattern matched on argv and could miss orphans
    from a renamed binary, or — paranoid case — match unrelated processes).
    """
    raw = _resolve_key(args)
    name = _resolve_name(args)
    pid_path = _member_dir(raw, name) / "daemon.pid"
    if not pid_path.exists():
        return 0
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0
    # Refuse pid <= 1: 0 signals every process in our group, negatives target a
    # process group, 1 is init. Any of those would be catastrophic if a stale
    # or corrupt pid file ever held them.
    if pid <= 1:
        pid_path.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Daemon already gone; clean up the stale pid file.
        pid_path.unlink(missing_ok=True)
    return 0


# ---- entry --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-chat")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create", help="create a new room and start listening")
    pc.add_argument("--as", dest="name", required=True)
    pc.set_defaults(func=cmd_create)

    pj = sub.add_parser("join", help="join an existing room and start listening")
    pj.add_argument("key")
    pj.add_argument("--as", dest="name", required=True)
    pj.set_defaults(func=cmd_join)

    ps = sub.add_parser("send", help="send a text message to the other peer")
    ps.add_argument("text", nargs="?", help="the message body (or use --stdin for shell-quote-unsafe content)")
    ps.add_argument("--stdin", action="store_true", help="read the message body from stdin instead of argv")
    ps.add_argument("--as", dest="name", default=None)
    ps.add_argument("--key", default=None)
    ps.set_defaults(func=cmd_send)

    pr = sub.add_parser("recv", help="print (and optionally follow) your inbox")
    pr.add_argument("--as", dest="name", default=None)
    pr.add_argument("--key", default=None)
    pr.add_argument("--follow", "-f", action="store_true")
    pr.set_defaults(func=cmd_recv)

    pp = sub.add_parser("peers", help="show the other peer's liveness")
    pp.add_argument("--as", dest="name", default=None)
    pp.add_argument("--key", default=None)
    pp.set_defaults(func=cmd_peers)

    pst = sub.add_parser("stop", help="stop your own daemon (graceful, by PID file)")
    pst.add_argument("--as", dest="name", default=None)
    pst.add_argument("--key", default=None)
    pst.set_defaults(func=cmd_stop)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except AgentChatError as e:
        print(f"agent-chat: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
