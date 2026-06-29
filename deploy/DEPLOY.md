# Deploy polmkt-arb to an always-on VM (paper/detection — no live trading)

Goal: run the detector + Telegram bot + nightly summary 24/7 on a cheap Linux VM,
so edge alerts, paper results, and readiness data accumulate even when your Mac is
off. **This runs in paper/detection mode only — it places no real orders.**

Total time ~15 minutes. You need: a cloud account and your local `.env` + Kalshi key.

---

## 1. Create the VM (cheapest easy option)

**DigitalOcean** (easiest UI) — or Hetzner (~$1 cheaper):
- Create a Droplet → **Ubuntu 24.04 LTS** → **Basic / Regular, $6/mo (1 GB / 1 vCPU)**
  (the $4 512 MB works too; 1 GB is safer headroom).
- **Region: a US datacenter** (e.g. New York / San Francisco) — correct for Polymarket US.
- Auth: add your SSH key (recommended) or set a password.
- Note the VM's IP. Default login user: `root` (DigitalOcean) — commands below assume it.

## 2. Copy the code + secrets from your Mac

From your Mac (NOT on the VM), in a local terminal:

```bash
rsync -av --exclude .venv --exclude .git --exclude '__pycache__' \
  --exclude '*.db*' --exclude '*.log' --exclude 'scripts/_demo_out' \
  ~/PolMkt/polmkt-arb/  root@<VM-IP>:~/polmkt-arb/
```

This copies the code **and** your `.env` + `secrets/kalshi_rsa_private.pem`
(rsync ignores `.gitignore`, so secrets come along — they never touch git).

## 3. Run setup on the VM

```bash
ssh root@<VM-IP>
cd ~/polmkt-arb
sudo bash deploy/setup.sh
```

That installs dependencies and starts four auto-restarting services:
`polmkt-detector` (sports edge watch), `polmkt-bot` (Telegram control),
`polmkt-dashboard` (localhost), and a nightly `polmkt-daily.timer` summary.

## 4. Verify

- You should get a Telegram **“▶️ started”** ping and a **“🤖 command bot online”** message.
- Text your bot **`/status`** — it should reply.
- Logs: `journalctl -u polmkt-detector -f`

---

## Day-to-day

| Want to… | Do this |
|---|---|
| Stop all new orders instantly | text **`/halt`** (or `touch ~/polmkt-arb/HALT`) |
| Resume | text **`/resume`** |
| Check status / PnL / positions | text `/status` `/pnl` `/positions` |
| View the dashboard | `ssh -L 8787:127.0.0.1:8787 root@<VM-IP>` then open http://127.0.0.1:8787 |
| See live logs | `journalctl -u polmkt-detector -f` |
| Update after code changes | re-`rsync` (step 2), then `cd ~/polmkt-arb && sudo bash deploy/setup.sh` |
| Restart a service | `systemctl restart polmkt-detector` |

The dashboard stays bound to localhost (not exposed to the internet) — view it
through the SSH tunnel above. Don't open port 8787 publicly; it has no auth.

## Cost
~$6/mo (DigitalOcean 1 GB) or ~$4/mo (Hetzner CX22 / DO 512 MB). Paper mode only,
so no trading risk while it gathers data.

## Going live later (NOT yet)
Live order placement isn't built. When it is, arming additionally requires
`ARMED=true`, `--live`, funded balances, and a non-empty verified `mappings.yaml`
— and you should prove it with a 1-contract trade first. Until then this is a
read-only watcher that texts you.
