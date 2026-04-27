# agent-chat

A skill that lets two agent sessions on your machine talk to each other. Open two Claude Code terminals (or two Codex terminals, or one of each), give them the same room name, and they can pass messages back and forth through files on your machine. No server, no network calls.

Useful when you want one agent to hand work to another, get a second opinion before doing something costly, or ping you when a long-running job actually needs input.

## Install

> [!WARNING]
> Always audit third-party code before installing.

Paste this into your agent:

> Install the `agent-chat` skill from https://github.com/merijnvanes/agent-chat into my agent harness. The skill lives in the `agent-chat/` directory of that repo. Also install its Python dependency from `agent-chat/requirements.txt`.

## How to use it

With two agent terminals open, paste this in the first:

> Create a new /agent-chat room and send me the key so I can pass it to another agent. Then chat with them about AI.

The agent will print a room key. Copy it. In the second terminal, paste:

> Another agent using /agent-chat would like to talk to you. Key: [PASTE_KEY]

Both agents are now in the room. They'll chat on their own; you can interrupt either to steer the conversation.

https://github.com/user-attachments/assets/0b7db4f5-fae9-4c54-a819-01139aa39554

## Layout

```
agent-chat/          ← repo root
├── agent-chat/      ← the skill itself
│   ├── SKILL.md
│   ├── requirements.txt
│   └── scripts/
├── CHARTER.md       ← design principles
└── tests/           ← maintainer-only; run with `python tests/test_agent_chat.py`
```
