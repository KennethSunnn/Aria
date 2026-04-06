# Third-Party Notices

This project includes or derives configuration/personality assets from third-party open-source projects.

## agency-agents

- Project: [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)
- **Preferred local path** (not committed; clone or extract here): `third_party/agency-agents/` — use the same nested layout as upstream (for example `third_party/agency-agents/agency-agents-main/`).
- **Legacy path** (still supported if you already have it): `agency-agents-main/agency-agents-main` at the repository root.
- License: MIT
- Upstream copyright notice:
  - `Copyright (c) 2025 AgentLand Contributors`

### How ARIA uses it

- ARIA uses agent profile markdown files as source material for personality routing and prompt injection.
- ARIA-specific integration and runtime logic are implemented in this repository (for example: `aria_manager.py`, `runtime/`, and `config/agent_personality_map.json`).

### Compliance notes

- The upstream MIT license text is preserved in your local checkout, for example:
  - `third_party/agency-agents/agency-agents-main/LICENSE`, or
  - `agency-agents-main/agency-agents-main/LICENSE`
- Per MIT terms, the upstream copyright and permission notice are retained.

