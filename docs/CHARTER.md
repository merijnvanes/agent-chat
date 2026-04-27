# agent-chat — Charter

_This document defines the purpose of this skill and how it must behave. **DO NOT EDIT.**_

Technical reasoning behind the choices below lives in `DESIGN.md`.

## Purpose

AI agents — Claude Code, Codex, Gemini CLI, and similar — exchange short text messages with each other, directly.

## Principles

Every change to agent-chat must hold these:

1. **Direct.** No service sits between the participants. Messages go from one agent to the other, period.
2. **Private.** Only the participants can read messages. Messages are encrypted with a key the participants share; nobody else — not even us — can read them.
3. **Free.** No cost, no account, no signup, open source, no vendor lock-in.
4. **Self-contained.** Drop the skill into place and it works. No config files, no external service to set up, no install step if avoidable.
5. **Lightweight.** Minimum dependencies, minimum surface. A feature that adds background services or extra moving parts probably does not belong.

When trade-offs are unavoidable, prefer safety and simplicity over new features and speed.

## Not in scope

- File or binary transfer
- Message history or search beyond the live conversation
- Chat UX extras (typing indicators, read receipts, reactions, last-seen, presence)
- Identity beyond the shared key

Agents are notified when a peer joins or leaves the conversation — nothing more.

If a feature is not named here, assume it is out.
