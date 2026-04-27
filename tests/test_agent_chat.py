"""Tests for agent-chat — minimal suite, charter-driven.

Run from anywhere:

    python tests/test_agent_chat.py

Maintainer-only — the skill does not load these at runtime.

These tests defend the charter properties that are observable at runtime:
end-to-end encryption (AEAD authenticity + plaintext-not-on-disk), 1-on-1
scope (under concurrent join races and on the sender side), drain semantics
of recv, and the security guard on cmd_stop. The other charter
non-negotiables — free, self-contained, lightweight — are properties of the
install/dependency story (requirements.txt is one line; no external
services), not of runtime behavior, and are defended by review rather than
by unit tests.

The set was selected by consulting two independent reviewers (one Codex via
codex exec, one Claude via claude -p) and reconciling their minimal-set
proposals.
"""

import argparse
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Make the production module importable from this tests/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-chat" / "scripts"))

# Force agent_chat to use a fresh, isolated home before importing it.
_TMP_HOME = Path(tempfile.mkdtemp(prefix="agentchat-test-"))
os.environ["AGENTCHAT_HOME"] = str(_TMP_HOME)

import agent_chat as ac  # noqa: E402


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _join_member(key_str: str, name: str) -> Path:
    """Mark a member as joined without running the daemon."""
    raw = ac._decode_key(key_str)
    md = ac._ensure_member(raw, name)
    (md / "joined.marker").touch()
    return md


def _capture_recv(key_str: str, name: str) -> list[dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ac.cmd_recv(_ns(key=key_str, name=name, follow=False))
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _send(key_str: str, frm: str, text: str) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ac.cmd_send(_ns(key=key_str, name=frm, text=text, stdin=False))
    return json.loads(buf.getvalue().strip())


def _race_worker(home: str, key_str: str, name: str, q):
    """Module-level so it survives multiprocessing spawn pickling."""
    os.environ["AGENTCHAT_HOME"] = home
    import importlib, agent_chat as _ac
    importlib.reload(_ac)
    try:
        claimed = _ac._admit_atomically(_ac._decode_key(key_str), name)
        q.put(("ok", name, claimed))
    except _ac.AgentChatError as e:
        q.put(("refused", name, str(e)))


class CharterCore(unittest.TestCase):
    """Five tests, one per charter property worth defending."""

    def setUp(self):
        self.key = ac._encode_key(ac._gen_key_raw())
        _join_member(self.key, "alice")
        _join_member(self.key, "bob")

    # --- correctness: drain semantics of recv ------------------------------

    def test_recv_returns_then_drains(self):
        """recv emits unseen messages once, then is empty until new ones arrive.
        Defends `correctness` — the core delivery contract."""
        _send(self.key, "alice", "first")
        _send(self.key, "alice", "second")

        msgs = _capture_recv(self.key, "bob")
        self.assertEqual([m["text"] for m in msgs], ["first", "second"])

        # Second recv must be empty — the cursor advanced past those lines.
        self.assertEqual(_capture_recv(self.key, "bob"), [])

    # --- scope + correctness: full E2E with implicit 1-on-1 routing --------

    def test_send_picks_the_other_peer_implicitly(self):
        """send takes no --to: in a 1-on-1 room there is exactly one recipient,
        and the message round-trips through encrypt → inbox → decrypt → recv.
        Defends `scope` (1-on-1) and `correctness` (E2E flow) together."""
        result = _send(self.key, "alice", "hi")
        self.assertEqual(result["delivered"], "inbox")
        msgs = _capture_recv(self.key, "bob")
        self.assertEqual([m["text"] for m in msgs], ["hi"])

    # --- security: encryption is real, AEAD rejects tampering --------------

    def test_disk_holds_ciphertext_and_tampering_is_dropped(self):
        """Two assertions on charter non-negotiable #2 (end-to-end encrypted):
        (a) plaintext never appears on disk; (b) a flipped byte inside the
        AES-GCM ciphertext causes recv to emit nothing. Defends `security`.

        The byte flip targets the *decoded* ciphertext bytes specifically
        (not the JSON or base64 wrapper) so a passing test proves AEAD
        authenticity rejected the record — not that an upstream parser
        choked. _decrypt collapses JSONDecodeError, b64 ValueError, and
        InvalidTag to None; without this targeting we cannot tell which one
        we hit."""
        plaintext = "secret-payload-xyz-do-not-leak"
        _send(self.key, "alice", plaintext)
        bob_inbox = ac._member_dir(ac._decode_key(self.key), "bob") / "inbox.ndjson"

        data = bob_inbox.read_bytes()
        self.assertNotIn(plaintext.encode(), data,
                         "plaintext leaked to disk — AES-GCM not actually applied")

        # Parse the wire record and mutate one byte of the actual ciphertext.
        # Re-encode and write back. This forces the rejection through
        # AESGCM.decrypt → InvalidTag, not through JSON/base64 upstream.
        obj = json.loads(data.rstrip(b"\n"))
        ct = bytearray(base64.b64decode(obj["c"]))
        ct[0] ^= 0x01
        obj["c"] = base64.b64encode(bytes(ct)).decode()
        bob_inbox.write_bytes((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        # Reset the read cursor so recv re-reads the (now tampered) line.
        (ac._member_dir(ac._decode_key(self.key), "bob") / "inbox.cursor").write_text("0")
        self.assertEqual(_capture_recv(self.key, "bob"), [],
                         "tampered ciphertext must not produce any output")

    # --- scope under race: 1-on-1 invariant survives concurrent joiners ----

    def test_concurrent_admissions_yield_at_most_two_members(self):
        """Race four spawn-mode subprocesses through _admit_atomically.
        Without the room lock each would observe joined < 2 and write its
        marker, producing four members. With the lock, exactly two succeed
        and the rest are refused. Defends `scope` (>2 peers is out of scope)
        under the only condition where the invariant actually breaks."""
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        names = ["w1", "w2", "w3", "w4"]
        # Use a fresh room so admission counts start from zero.
        race_key = ac._encode_key(ac._gen_key_raw())
        procs = [ctx.Process(target=_race_worker, args=(str(_TMP_HOME), race_key, n, q))
                 for n in names]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)
            self.assertIsNotNone(p.exitcode, "worker did not exit within timeout")
        results = [q.get(timeout=2) for _ in names]
        admitted = [r for r in results if r[0] == "ok"]
        refused = [r for r in results if r[0] == "refused"]
        self.assertEqual(len(admitted), 2, f"expected exactly 2 admitted, got {results}")
        self.assertEqual(len(refused), 2, f"expected exactly 2 refused, got {results}")
        self.assertEqual(len(ac._joined_members(ac._decode_key(race_key))), 2)

    # --- correctness: torn writes must not be skipped silently --------------

    def test_recv_does_not_consume_partial_line(self):
        """If a daemon crashes mid-append, the inbox can hold a line without
        a trailing \\n. recv must leave the cursor at the start of that
        partial line so the message is still readable once the writer
        completes. Defends `correctness` against silent message loss —
        not a subset of the drain test, since drain only sees whole lines."""
        _send(self.key, "alice", "complete")
        raw = ac._decode_key(self.key)
        bob_inbox = ac._member_dir(raw, "bob") / "inbox.ndjson"
        with bob_inbox.open("ab") as fh:
            fh.write(b'{"n":"AAAA","c":"BBBB"}')  # no trailing \n

        msgs = _capture_recv(self.key, "bob")
        self.assertEqual([m["text"] for m in msgs], ["complete"])
        cursor_pos = int((ac._member_dir(raw, "bob") / "inbox.cursor").read_text())
        self.assertLess(cursor_pos, bob_inbox.stat().st_size,
                        "cursor must not have advanced past the partial line")

    # --- correctness + scope: send must refuse when there is no peer -------

    def test_send_when_alone_in_room_fails(self):
        """The sender-side check that a peer exists. cmd_send raises rather
        than queue a message that nobody can ever read. Defends `correctness`
        on the path users hit most often (sending before the second peer has
        joined)."""
        # Fresh room with only alice — bob is not joined here.
        solo_key = ac._encode_key(ac._gen_key_raw())
        _join_member(solo_key, "alice")
        with self.assertRaises(ac.AgentChatError):
            _send(solo_key, "alice", "hi")

    # --- scope: system events route to other peers, not back to self ------

    def test_peer_events_route_to_other_peer_only(self):
        """`peer-joined` and `peer-left` are charter-scoped events that must
        land in the *other* peer's inbox and never echo back to the emitter.
        Defends the events amendment to the charter (`System events are in
        scope: peer-joined, peer-left`)."""
        raw = ac._decode_key(self.key)

        # Alice arrives. Event lands in bob's inbox, not alice's.
        ac._emit_event_to_peers(raw, "alice", "peer-joined")
        self.assertEqual(_capture_recv(self.key, "alice"), [],
                         "events must not echo back to the emitter")
        bob_msgs = _capture_recv(self.key, "bob")
        self.assertEqual(len(bob_msgs), 1)
        self.assertEqual(bob_msgs[0]["from"], "alice")
        self.assertEqual(bob_msgs[0]["kind"], "peer-joined")

        # Alice leaves. Same routing.
        ac._emit_event_to_peers(raw, "alice", "peer-left")
        bob_msgs = _capture_recv(self.key, "bob")
        self.assertEqual(len(bob_msgs), 1)
        self.assertEqual(bob_msgs[0]["from"], "alice")
        self.assertEqual(bob_msgs[0]["kind"], "peer-left")

        # Misuse: an arbitrary kind must raise rather than quietly widen the
        # charter-pinned set. This is what stops a future contributor from
        # adding a "typing" event by passing it through this helper.
        with self.assertRaises(ValueError):
            ac._emit_event_to_peers(raw, "alice", "typing")

    # --- correctness: --stdin escape hatch for shell-quote-unsafe bodies --

    def test_send_via_stdin_handles_shell_metachars(self):
        """A message body with parens, backticks, $vars, brackets, etc.,
        is passed safely via stdin instead of argv. Defends a real
        ergonomic failure observed in live testing where a code snippet
        as a positional arg blew up shell quoting."""
        body = "code: xs.append([f(i) for i in range(n)]) `backticks` $vars"
        buf = io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(body + "\n")
        try:
            with redirect_stdout(buf):
                ac.cmd_send(_ns(key=self.key, name="alice", text=None, stdin=True))
        finally:
            sys.stdin = saved_stdin

        result = json.loads(buf.getvalue().strip())
        self.assertEqual(result["delivered"], "inbox")
        msgs = _capture_recv(self.key, "bob")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], body)

    # --- security: stop refuses to signal pid <= 1 -------------------------

    def test_stop_refuses_pid_zero_one_or_negative(self):
        """A corrupt or stale PID file containing 0/1/negative would otherwise
        target the process group or init via os.kill. cmd_stop must drop them
        without signaling. Defends `security` against catastrophic local-state
        side effects (the only test in the suite tied to a real worst-case)."""
        import unittest.mock as _mock
        md = ac._member_dir(ac._decode_key(self.key), "alice")
        for bad in ("0", "1", "-1", "-12345"):
            (md / "daemon.pid").write_text(bad)
            with _mock.patch("agent_chat.os.kill") as mock_kill:
                rc = ac.cmd_stop(_ns(key=self.key, name="alice"))
            self.assertEqual(rc, 0)
            mock_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
