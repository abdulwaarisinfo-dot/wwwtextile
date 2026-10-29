# Live demo guide (fictional data only)

Goal: run the ERP on **your computer**, give your client a temporary `https://…` link, let them click through everything, then switch it off.
Nothing here is a production deployment. All companies, people, vehicles and amounts are invented.

## 0. What you need
* Python 3.11+ (developed and tested on 3.13) · Chrome, Edge or Firefox on the computer that runs the demo.
* Optional, for the *Document Reader* only: `tesseract-ocr` (everything else works without it; the demo guide shows it as "Unavailable" if missing).
* A tunnel program for the public link: **cloudflared** (free, no account for quick tunnels).

## 1. Start the demo (3 commands)
```bash
cd wwtextile
python -m pip install -r requirements.txt
python deploy/run_demo.py --reset          # use "python3" on macOS/Linux
```
First run creates `.demo.env` (private, never shown by the app) with a random secret key, a **client password** and a **separate admin password**,
generates about 8 weeks of fictional factory history, and prints the logins:

```
Client logins : ceo, manager1, manager2, store1, store2, prod1, prod2, gate1, gate2, fin1
Client password (share this): …
Admin login   : admin / …   (keep private)
Local address : http://127.0.0.1:8000   health: /healthz
```
Stop it with Ctrl+C. Starting it again **keeps** everything the client entered; `--reset` wipes it and regenerates fresh data (`--seed 7` gives a different story).
Use `--local` instead of nothing/`--reset` only if you will browse over plain `http://127.0.0.1` with Safari (it relaxes the Secure-cookie rule; never use it for the shared link).

## 2. Make the public link
In a **second terminal**:
```bash
# install once:  Windows: winget install --id Cloudflare.cloudflared   ·  macOS: brew install cloudflared   ·  Linux: see developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads
cloudflared tunnel --url http://127.0.0.1:8000
```
It prints a link like `https://random-words.trycloudflare.com`. That is the link to send. It works only while both terminals are running and your computer is awake.
No installer? This works with the `ssh` that ships with Windows 10+, macOS and Linux: `ssh -R 80:127.0.0.1:8000 nokey@localhost.run` (prints an `https://….lhr.life` link).
*(Both tunnel commands could not be run in the development sandbox; they are standard but unverified here, so do step 3 before you send anything.)*

## 3. Verify before you share
```bash
python deploy/verify_demo.py https://random-words.trycloudflare.com --client-password "<client password>" --admin-password "<admin password>" --workflow
```
Passwords are in `.demo.env`. It checks the health endpoint, headers, that anonymous visitors and the wrong/default passwords are rejected, that client users cannot reach admin pages,
that `.demo.env`, the database and source files are not downloadable, that key pages open, **that the admin account cannot sign in through the public link**, and (with `--workflow`) runs the whole factory flow
(receive → issue → record result/reconcile losses → finished stock → dispatch → approve → gate pass → exit → invoice). `--workflow` writes one `VERIFY-…` lot into the demo; run `--reset` afterwards for a clean slate.
Quick manual check: open `<link>/healthz` → `ok`.

## 4. What to send the client
* The link, the **client password**, and the usernames above (start with `ceo`, then `store1` → `manager1` → `prod1` → `manager2` → `gate1` → `fin1` for the factory flow).
* Tell them to open **Demo guide** in the sidebar: it lists the step-by-step flow, what each user can do, and which capabilities are real vs not connected.
* Use a private/second browser window to switch users (one sign-in per browser profile).

## 4a. The built-in guide (no AI key needed)
- **Dashboard → "Needs you now":** a ranked list (red = urgent, amber = soon) built from live records, plus a flow strip (Receive → Process → Reconcile → Approve → Gate pass → Exit) showing where work is sitting. Each line says why it is listed and links to the records.
- **Guide button (bottom right, every page):** *Needs you* (same list, with the badge showing urgent count), *This page* (what the page is for, when to use it, steps, what to do next) and *Ask* (quick question to the assistant).
- If the signed-in role cannot perform an action, the line says who acts on it and the button becomes "View".
- This is fixed rules over your data. It is **not** an AI model and does not judge business decisions; say so if the client asks.

## 4a2. Urdu and Roman Urdu
- **Without an AI key:** the rule engine understands everyday Urdu (script) and Roman Urdu for its main questions: *kitna stock hai, aaj ka haal batao, kitna nuqsan hua, kaunsi job late hai, kis se paise lene hain, hum ne kis ko paise dene hain, cash kitna hai, multihead ka haal, purana stock, sab se bara customer, gaari kahan hai, alerts dikhao*, and the same in Urdu script. This is a phrase table, so other wordings fail; it then says so in Roman Urdu or Urdu instead of guessing. The answer opens with an Urdu heading, but **the detail is still English** (numbers and names are identical to the English answer).
- **With an AI key:** the model is told to reply in the user's own language (English, Urdu script or Roman Urdu), so free Urdu works. Not tested against a live provider.
- **Voice:** choose "Urdu (Pakistan)" in the speech-language list on the Assistant page. Recognition and spoken replies depend on your browser and installed voices; not verified here.
- The Guide panel and dashboard text are English only.

## 4b. Connect a real AI model (optional, recommended for a client demo)
Without a key the assistant is a **rule engine**: it recognises about 25 kinds of question and answers from live data, but it will not understand free phrasing.
With a key, a real language model reads the question, decides which of the ERP's read-only tools to call (stock, losses, payables/receivables, Multihead, gate, dispatch, alerts, drafts…), and
explains the result. It still cannot read the database directly, cannot see anything the signed-in user may not see, and can only create **drafts**. Add these lines to `.demo.env` (keep it private) and restart:
```
WW_AI_PROVIDER=openai          # or: anthropic
WW_AI_API_KEY=<your key>
WW_AI_MODEL=<a chat model from your account that supports tool calling>
WW_AI_MAX_PER_HOUR=60          # per user; over the cap the rule engine answers and says so (protects your API bill)
# optional server voice (otherwise Chrome/Edge's own speech recognition is used):
WW_STT_PROVIDER=openai  WW_STT_API_KEY=<key>  WW_STT_MODEL=whisper-1
WW_TTS_PROVIDER=openai  WW_TTS_API_KEY=<key>  WW_TTS_MODEL=tts-1  WW_TTS_VOICE=alloy
```
The start-up banner then says `AI model: CONNECTED`, and the **Demo guide** shows it as connected. If the provider is down or the key is wrong, the answer falls back to the rule engine with a visible notice.
**Honest caveat:** this connection could not be tested against a live provider from the development sandbox (only against local fake servers), so run a few questions yourself before the client call. Questions sent to the model leave your machine; they contain fictional demo data only.

## 5. What is real and what is not
| Real (runs here) | Not connected / not built |
|---|---|
| All ERP workflows, validation, maker-checker approvals, ledgers, audit trail, reports, CSV/print, alerts, dashboard charts | Language-model assistant (needs `WW_AI_*` keys; without them only the rule-based engine answers and says so) |
| Rule-based assistant over live data; drafts only, never posts | Server speech-to-text / text-to-speech (needs keys). Browser voice input uses your browser's own recogniser (Chrome/Edge; English; audio may go to the browser vendor) |
| Document Reader (OCR on this machine, if tesseract is installed) – proposes a goods-receipt *draft* | WhatsApp / email / SMS, bank feeds, payment gateways, FBR/tax invoicing, barcode/weighbridge/RFID/CCTV |
The same table is shown live in the app under **Demo guide**.

## 6. Reset and regenerate
* From the terminal: `python deploy/run_demo.py --reset` (or `python -m wwtextile.cli reset-demo --yes --seed 11` with the env from `.demo.env`).
* From the browser: sign in as `admin` **directly at http://127.0.0.1:8000** → Demo guide → Reset (type `RESET`). Everyone is signed out.

## 7. Security model of the shared demo
* Public mode is the default (`--local` turns it off): secure + HttpOnly cookies, 32-byte random secret, per-address sign-in throttle, `noindex` headers and `robots.txt`, `127.0.0.1`-only bind (reachable only through your tunnel).
* The client password is random and different from the admin one. The published default password is rejected. Passwords are never rendered by the app.
* **Admin functions are not exposed publicly:** the `admin` account refuses to sign in through any proxy/tunnel (forwarding headers present), even with the right password, and refused attempts cannot lock it. Client roles have no user-management, settings or reset rights. Override only deliberately with `WW_DEMO_ADMIN_REMOTE=1`.
* The sign-in page is public by necessity: anyone with the link can *try* to sign in. Keep the link private, stop the tunnel when done, and rotate by deleting `.demo.env` and restarting.
* Not suitable for real data: SQLite single node, no backups, no TLS of its own (the tunnel provides it), no external security audit.

## 8. Alternative: a small server instead of your laptop
Same commands on any Ubuntu VPS (`python3 deploy/run_demo.py --reset` under `tmux`/systemd) with Caddy or nginx doing HTTPS in front (`deploy/nginx.conf`). Set `WW_FORWARDED_ALLOW_IPS` to the proxy address. On a VPS you cannot reach `127.0.0.1` from your browser, so use `ssh -L 8000:127.0.0.1:8000 you@server` to open the admin, or the CLI reset.
