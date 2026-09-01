# X Agent — Signal CRM

An API-first X.com prospect and conversation assistant. The counterpart of the
LinkedIn agent: it watches accounts, reads their public signal and your DMs
through the official X API, drafts short messages with Ollama, and only sends
through the API after you review and approve each one.

X's automation rules are stricter than LinkedIn's, so this agent **never
scripts the x.com website** and **never bulk-DMs people who did not engage with
you first**. All reads and writes go through the official X API v2.

## How it decides who to message

Relationship state is decided in `x_agent.py` (`classify_prospect`) from what
is actually recorded in the database — never by the LLM:

- **Discovered** — just added, nothing analyzed yet. Watch only.
- **Qualified** — profile + posts read, but no explicit intent signal yet.
  Watch and engage, but do **not** DM.
- **Initial** — an explicit intent signal is recorded (they mentioned you,
  replied to you, followed you back, DM'd you first, asked for info, etc.).
  A first DM may be drafted for review.
- **Awaiting** — your last DM is pending their reply. Wait.
- **Reply** / **goal_reply** — their last message or "goal done + they wrote
  back" needs your answer. A reply may be drafted for review.
- **Goal** — the app link was already sent, nothing new from them.
- **Eliminated** — you decided never to contact them (with optional reason).

The sweep review window shows one row per person who *needs* a message
(Send / Regenerate / Skip). Everyone else is skipped with a reason.

## First-time setup

1. Create an app at <https://developer.x.com> (read + DM write scopes need
   the appropriate app access level).
2. Generate your credentials:
   - `X_BEARER_TOKEN` — app-only Bearer token (public reads).
   - `X_API_KEY` + `X_API_SECRET` — consumer key/secret (OAuth 1.0a).
   - `X_ACCESS_TOKEN` + `X_ACCESS_SECRET` — user access token/secret
     (user-context reads and DMs; your own account must have DM access).
3. Start Chrome normally and sign in to your X account, and to allow DM reads
   the authenticated account must be able to see the conversations.
4. Start `Start X Agent.bat`.
5. Click **Set X credentials** and paste each value. The app calls
   `GET /2/users/me` to verify them.

Credentials are stored in the local `settings` table (or use the matching
environment variables).

## Daily workflow

1. **+ Add user by @handle** or **Discover from search** (searches recent
   posts and saves the unique authors as `Discovered`).
2. Select someone and use **Read signals** (profile + recent posts from the
   API) and **Fetch DMs** (your DM thread with them, if any).
3. Use **Generate first DM** (only allowed when `classify_prospect` sees an
   explicit intent signal) or **Generate reply**.
4. Review and edit the draft in the message box. **Send DM** only where intent
   is real.
5. Use **Save as sent** / **Save their reply** to keep the exact conversation
   stored, so state continues from X API outages.
6. Use **Run sweep** to refresh up to 20 accounts per pass with human pacing
   (20–45 s between accounts), and review one Send / Skip row per person who
   needs a message. Sweep sessions resume — accounts already processed in a
   session are not reprocessed.
7. Use **Open X profile** to check the real profile in your browser when you
   want extra context before sending.

## Colors

Blue = intent → first DM possible  
Orange = their reply / your turn  
Purple = goal reached + they wrote back  
Gray = eliminated

## Ollama

Defaults:

- URL: `http://127.0.0.1:11434`
- Model: `gemma4:31b-cloud`

Override them with `OLLAMA_URL` and `OLLAMA_MODEL` environment variables.

## X API notes

- Base URL: `https://api.x.com/2`.
- Public reads use the Bearer token; DMs and `/users/me` are signed with
  OAuth 1.0a HMAC-SHA1 (stdlib only, no SDK required).
- Requests pace themselves (1.2–3 s) and back off on HTTP 429.
- `send_message` verifies the conversation is opened with the exact handle
  you targeted before posting (defensive, mirrors the LinkedIn thread check).

## Data

- `x_agent.db` is local/private and ignored by Git.
- Signals, DMs, drafts, elimination decisions and sweep state all persist in
  the database and survive API outages.