# StarHeart

**Long-term memory for AstrBot.** It turns conversations into local memories, injects the relevant ones before the model replies, and maintains person / group profiles, relationships and expression habits.

**English** · [中文](./README.md)

| | |
| --- | --- |
| Package | `astrbot_plugin_starheart` |
| Version | `1.0.6` |
| Author | 肆一纸心 |
| Repository | <https://github.com/WSYXIUBA/astrbot_plugin_starheart> |
| Adapters | `aiocqhttp` (OneBot v11) |
| AstrBot | >= 4.24.0 |

## How it works

- Listens to message events; does not intercept or rewrite replies.
- Injects memories before the LLM request, limited to entries relevant to the current topic.
- Proactive messages are optional and off by default, bounded by quiet hours, daily quota and allow/deny lists.

## Features

- Long-term memory: conversations are distilled into entries stored locally; entries not accessed for a long time decay and are archived.
- Relevant recall: memories are retrieved per turn by topic.
- Multi-generation vectors: a new embedding model builds and backfills its own generation, switches when complete, and keeps old generations for instant switch-back.
- Person / group profiles: generated and updated per person and per group.
- Relationship score: interaction accumulates a score that affects tone.
- Expression habits: learned from conversations and referenced in replies.
- Proactive messages: may start a conversation after a quiet period (optional).
- Retracted messages: keeps the text and images of retracted messages.
- Dashboard: memory graph, memory list, message events, persona, settings.
- Upgrade compatibility: existing config and panel values survive upgrades.

## Install

Search `StarHeart` in the plugin market, or use "Install from URL" with this repository; manual install: drop `astrbot_plugin_starheart` into `data/plugins/`.

## Data

All data stays local under `data/plugin_data/astrbot_plugin_starheart/`; nothing is sent to third parties except the model endpoints you configure.

## License

MIT License — see [`LICENSE`](./LICENSE).

## How it was built: entirely with AI

Requirements, trade-offs, review and testing are handled by the maintainer; the implementation (backend, dashboard, copy and docs) was written by AI.
