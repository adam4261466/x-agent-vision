# X Agent — Signal CRM (browser-based)

A browser-based X.com prospect and conversation assistant, the counterpart of
the LinkedIn agent. It watches X accounts, reads their public profile, recent
posts and your DM threads straight from the live X web pages in the **agent
Chrome**, drafts short messages with Ollama, and — after you review each one —
types the approved draft into the DM composer for you to send yourself.

X's automation rules are strict and the paid API is not required: this agent
**never scripts bulk actions**, never sends on its own, and only drafts a
first DM when the CRM records an explicit user intent signal.

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
(`Type into composer` / Regenerate / Skip / `Confirm sent`). Everyone else is
skipped with a reason.

## First-time setup

1. Start **Start X Agent.bat**. If the agent Chrome is not running it is
   launched automatically on port `9223` with its own profile
   (`~/.agent-x-chrome-profile`).
2. When the window appears, go to `https://x.com/` in the **agent Chrome** and
   log in to your X account once. The agent Chrome is separate from your
   everyday browser, so use the real account you operate DMs from.
3. In the CRM, set your app/product link (**Set app link**) — it marks the
   "goal reached" state when it appears in an outbound message.

You can reuse one running agent Chrome across sessions; the app attaches over
CDP (`http://127.0.0.1:9223`, override with `X_CDP_URL`).

## Daily workflow

1. **+ Add user by @handle** — adds locally (status `Discovered`).
   **Discover from search** — the agent opens X search for your query, reads
   recent tweets, and adds the unique authors as `Discovered`.
2. Select someone and use **Read signals** (profile + recent posts from the
   live page) and **Fetch DMs** (your DM thread with them, if any).
3. Use **Generate first DM** (only allowed when `classify_prospect` sees an
   explicit intent signal) or **Generate reply**.
4. Review and edit the draft in the message box.
5. **Type into composer** opens their DM in the agent Chrome and types the
   draft into the message box. **You press the actual Send button in X**, then
   click **Confirm sent** in the CRM to record the message and update state.
   (On X, users who have not opted into DMs — or who never engaged you — will
   block the send with the platform's own error; the CRM's intent gating keeps
   first DMs to people who engaged with you first.)
6. Use **Run sweep** to read up to 20 accounts per pass with human pacing
   (20–45 s between accounts) and review one row per person who needs a
   message. Sweep sessions resume — accounts already processed in a session
   are not reprocessed.

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

## Browser connection

The agent attaches to its own Chrome via Chrome DevTools Protocol (CDP),
defaulting to:

`http://127.0.0.1:9223`

Override with `X_CDP_URL` if you use another remote-debugging endpoint.

## Reading model

- Profiles and timelines are read from the pages the logged-in agent Chrome
  can already see; retweets and promoted tweets are skipped.
- DM senders are attributed deterministically from the DOM (their messages
  carry the participant avatar, yours align right) — the browser never
  guesses, the app decides.
- A DM conversation is only used after the opened thread verifies it belongs
  to `@handle` (refuses to read or type into the wrong conversation).

## Data

- `x_agent.db` is local/private and ignored by Git.
- Signals, DMs, drafts, elimination decisions and sweep state all persist in
  the database, so an interrupted sweep or a browser hiccup loses nothing.