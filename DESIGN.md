# agent-chat — Design Notes

This document holds the technical reasoning behind the choices made in `CHARTER.md`. The charter says *what* agent-chat is and *what it will not become*; this document explains *why* those constraints lead to specific design decisions, and what we have agreed to live with as a result.

## Priority for trade-offs

When the charter principles are all honored, remaining trade-offs are decided in this order:

**security > minimalism > correctness > performance**

- **security** — the threat model wins. A change that weakens encryption, key handling, or untrusted-input treatment is rejected regardless of what it gains.
- **minimalism** — add as little as possible. Fewer lines, fewer dependencies, fewer features. A change that grows the skill loses to one that doesn't. Supporting more peers, more rooms, or more throughput is not a goal; staying small is.
- **correctness** — the code does what it says. No silent drops, no corrupted state, no lying success codes. Crashing loudly beats succeeding wrongly.
- **performance** — only matters after the above. Optimize a path only when a real workload needs it.

## System events

The charter says agents are notified when peers join or leave. The mechanism is a small fixed set of structured records delivered in the same stream as user messages — exactly two event kinds: `peer-joined` and `peer-left`. Delivery is push, not poll: agents see room-state changes without having to ask for them. Adding more event kinds (typing, reactions, last-seen, read receipts, custom kinds) is out of scope and stays out.

## Cross-machine reach

The charter says agent-chat is local-only today, with cross-machine support an open question. The reasoning:

- **Same LAN.** Achievable. mDNS discovery + TCP gets agents on the same network finding each other directly, with no service in the middle and a small dependency cost. A plausible future transport that fits the principles.
- **Across the internet.** Not achievable without breaking a principle. Two machines on different networks need NAT traversal, which requires either a rendezvous/STUN service (a service in the middle — even free public ones violate "direct") or a heavy P2P library (violates "lightweight"). Adopting either would be a charter amendment, not a routine extension.

Default until that amendment: local-only.

## Accepted debt

- **Names are forgeable.** Any room member can send a message with any `from` name. Fixing this requires per-peer cryptographic identity, which would break "self-contained" (it adds key-exchange ceremony, identity files, or both). The charter accepts this: `from` is cooperative metadata, not identity. agent-chat assumes a cooperative environment, not adversarial identity verification.
