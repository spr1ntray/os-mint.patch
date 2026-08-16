# OS MINT

Soft Hub patch: OpenSea WL mint by slug. Robinhood only. Public skipped.

## Install

```text
dist/os-mint-1.0.3.softhub.zip
```

Soft Hub → Patches → drop the zip → Prepare if needed.

Paste one or more slugs / OpenSea URLs into **Коллекции**. One NFT per wallet, then stop. If public opens and nothing minted: «Клеймить было нечего».

## Build

```bash
python3 /Users/sprintray/codex_soft/soft-hub/scripts/build_plugin.py \
  /Users/sprintray/grok_soft/kuporh_mint \
  /Users/sprintray/grok_soft/kuporh_dist/os-mint-1.0.3.softhub.zip
```
