"""Visual X.com prospect and conversation CRM.

Mirrors the LinkedIn agent GUI: a searchable prospect list, per-user signal
panel (profile + posts + DMs), an editable proposal box, Send/Regenerate/Skip,
and a sweep that produces one review row per actionable person.

Design invariant (same as LinkedIn): LLM proposes -> human reviews -> action
occurs -> result is persisted. DMs are typed into the composer of the agent
Chrome and the human presses the final Send; the agent never sends on its own.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

import x_agent as core
from x_browser import BrowserSession, search_recent_users, send_message

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_gui_debug.log")


def _dbg(msg: str):
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        thread = threading.current_thread().name
        line = f"[{ts}] [x_gui] [{thread}] {msg}"
        print(line, file=sys.stderr, flush=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# Sweep review window: what action is being proposed for a person.
SWEEP_BADGE_TEXT = {
    "initial": "First DM",
    "reply": "Reply",
    "goal_reply": "Goal + reply",
}
SWEEP_BADGE_COLOR = {
    "initial": "#2d7ff9",
    "reply": "#f2994a",
    "goal_reply": "#9b51e0",
}

_STAGE_LABEL = {
    "eliminated": "Eliminated",
    "goal": "Goal done",
    "goal_reply": "Goal + reply",
    "awaiting": "Waiting for reply",
    "reply": "They replied",
    "initial": "Intent - first DM",
    "qualified": "Qualified (watching)",
    "discovered": "Discovered",
}


class _SweepRow:
    """One proposal in the sweep review window."""

    def __init__(self, parent, app, proposal: dict) -> None:
        self.app = app
        self.user = proposal["user"]
        self.stage = proposal["stage"]
        self.text = proposal["text"]
        self.done = False
        self._sent_recorded = False

        frame = ttk.LabelFrame(parent, padding=8)
        frame.pack(fill="x", padx=6, pady=4)
        frame.columnconfigure(1, weight=1)

        self.badge = tk.Label(
            frame,
            text=SWEEP_BADGE_TEXT.get(self.stage, self.stage),
            bg=SWEEP_BADGE_COLOR.get(self.stage, "#888"),
            fg="white",
            padx=8,
            pady=2,
            font=("Segoe UI", 9, "bold"),
        )
        self.badge.grid(row=0, column=0, sticky="nw", padx=(0, 8))

        handle = f"@{self.user['username']}"
        name = self.user["display_name"] or self.user["username"]
        ttk.Label(frame, text=f"{name}  ({handle})", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=1, columnspan=2, sticky="w"
        )
        bio = (self.user["bio"] or "").replace("\n", " ")[:140]
        ttk.Label(frame, text=bio[:100], foreground="#888", font=("Segoe UI", 8), wraplength=700).grid(
            row=1, column=1, columnspan=2, sticky="w"
        )

        self.msg = tk.Text(frame, height=5, wrap="word", font=("Segoe UI", 10))
        self.msg.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        self.msg.insert("1.0", self.text)
        self.msg.configure(state="normal")

        btnbar = ttk.Frame(frame)
        btnbar.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.send_btn = ttk.Button(btnbar, text="Type into composer", command=self.send)
        self.send_btn.pack(side="left")
        self.reg_btn = ttk.Button(btnbar, text="↻ Regenerate", command=self.regenerate)
        self.reg_btn.pack(side="left", padx=6)
        self.skip_btn = ttk.Button(btnbar, text="Skip", command=self.skip)
        self.skip_btn.pack(side="left", padx=6)
        self.confirm_btn = ttk.Button(btnbar, text="✓ Confirm sent", command=self.confirm_sent)
        self.confirm_btn.pack(side="left", padx=6)
        self.status = tk.Label(btnbar, text="generated", foreground="#888")
        self.status.pack(side="left", padx=10)

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for b in (self.send_btn, self.skip_btn, self.reg_btn, self.confirm_btn):
            try:
                b.configure(state=state)
            except Exception:
                pass

    def is_pending(self) -> bool:
        return not self.done

    def _user_id(self) -> int:
        return int(self.user["id"])

    def _username(self) -> str:
        return str(self.user["username"]).lstrip("@")

    def regenerate(self) -> None:
        if self.done:
            return
        self.done = True
        self._set_running(True)
        self.status.configure(text="regenerating…", foreground="#f2994a")
        uid = self._user_id()
        stage = self.stage

        def worker() -> None:
            try:
                text = core.generate_initial(uid) if stage == "initial" else core.generate_reply(uid)
                self.app.after(0, lambda text=text: self._mark_updated(text))
            except Exception as exc:
                err = str(exc)
                _dbg(f"sweep regenerate FAILED @{self._username()}: {type(exc).__name__}: {exc}")
                self.app.after(0, lambda err=err: self._mark_regen_fail(err))

        threading.Thread(target=worker, daemon=True).start()

    def _mark_updated(self, text: str) -> None:
        self.text = text
        self.msg.configure(state="normal")
        self.msg.delete("1.0", "end")
        self.msg.insert("1.0", text)
        self.done = False
        self._set_running(False)
        self.status.configure(text="regenerated", foreground="#27ae60")

    def _mark_regen_fail(self, err: str) -> None:
        self.done = False
        self._set_running(False)
        self.status.configure(text=f"regen failed — {err[:50]}", foreground="#e74c3c")

    def send(self) -> None:
        if self.done:
            return
        text = self.msg.get("1.0", "end-1c").strip()
        if not text:
            self.status.configure(text="message is empty — edit it first", foreground="#e74c3c")
            return
        self.text = text
        self.done = True
        self._set_running(True)
        self.status.configure(text="typing into composer…", foreground="#f2994a")
        username = self._username()
        uid = self._user_id()

        def worker() -> None:
            try:
                echoed = send_message(username, text)
                self.app.after(0, lambda echoed=echoed: self._mark_typed(echoed))
            except Exception as exc:
                err = str(exc)
                _dbg(f"sweep send(compose) FAILED @{username}: {type(exc).__name__}: {exc}")
                self.app.after(0, lambda err=err: self._mark_fail(err))

        threading.Thread(target=worker, daemon=True).start()

    def confirm_sent(self) -> None:
        """Record the DM as sent after the human clicks Send in X."""
        if self.done and self._sent_recorded:
            return
        text = self.msg.get("1.0", "end-1c").strip()
        if not text:
            self.status.configure(text="nothing to record", foreground="#e74c3c")
            return
        self._sent_recorded = True
        try:
            core.record_sent(self._user_id(), text)
            core.add_event(self._user_id(), "DM_SENT", text[:200])
        except Exception as exc:
            _dbg(f"confirm_sent record FAILED: {exc}")
            self.status.configure(text=f"record failed — {str(exc)[:50]}", foreground="#e74c3c")
            self._sent_recorded = False
            return
        self.status.configure(text="✓ recorded as sent", foreground="#27ae60")

    def skip(self) -> None:
        if self.done:
            return
        self.done = True
        self._set_running(True)
        self.status.configure(text="skipped", foreground="#8a94a6")

    def _mark_typed(self, echoed: str) -> None:
        self.status.configure(
            text="typed — press Send in X now, then 'Confirm sent'",
            foreground="#2d7ff9")

    def _mark_fail(self, err: str) -> None:
        _dbg(f"_SweepRow _mark_fail: {err[:200]}")
        self.done = False
        self._set_running(False)
        self.status.configure(text=f"failed — {err[:60]}", foreground="#e74c3c")


class XAgentApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("X Agent — Signal CRM")
        self.geometry("1450x900")
        self.minsize(1150, 720)
        self.selected_id: int | None = None
        self.rows = []
        self._sweep_running = False
        self._build()
        self.refresh_all()

    # --- UI construction ----------------------------------------------------
    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        side = ttk.Frame(self, padding=12)
        side.grid(row=0, column=0, sticky="nsew")
        side.columnconfigure(0, weight=1)

        ttk.Label(side, text="X Agent", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(side, text="Signal CRM — watch, engage, dm only on intent", font=("Segoe UI", 10)).grid(
            row=1, column=0, sticky="w", pady=(0, 10)
        )

        ttk.Button(side, text="+ Add user by @handle", command=self.add_by_handle).grid(
            row=2, column=0, sticky="ew", pady=(0, 6)
        )
        ttk.Button(side, text="🔎 Discover from search", command=self.discover_search).grid(
            row=3, column=0, sticky="ew", pady=(0, 6)
        )
        self.sweep_btn = ttk.Button(side, text="▶ Run sweep", command=self.run_sweep)
        self.sweep_btn.grid(row=4, column=0, sticky="ew", pady=(0, 6), ipady=4)
        ttk.Button(side, text="Refresh", command=self.refresh_all).grid(
            row=5, column=0, sticky="ew", pady=(0, 10)
        )

        ttk.Button(side, text="Set app link", command=self.ask_set_link).grid(
            row=6, column=0, sticky="ew", pady=(0, 10)
        )

        ttk.Label(
            side,
            text=("Run sweep reads each user's profile + recent posts + DMs from "
                  "the agent Chrome, figures out who needs a message, and shows a "
                  "row per person.\n\n"
                  "Type into composer = opens the DM in the agent Chrome and "
                  "types the approved draft into the message box. YOU press the "
                  "final Send in X, then click 'Confirm sent' to record it.\n\n"
                  "Users without an explicit intent signal (mention, request, "
                  "follow-back) are WATCHED, never messaged — X forbids "
                  "unsolicited DMs. First DMs are only drafted for users who "
                  "engaged with you first."),
            justify="left",
            wraplength=220,
            foreground="#555",
            font=("Segoe UI", 9),
        ).grid(row=8, column=0, sticky="ew")

        main = ttk.Frame(self, padding=(4, 12, 12, 12))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        main.rowconfigure(6, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        search = ttk.Entry(top, textvariable=self.search_var, font=("Segoe UI", 11))
        search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        search.bind("<KeyRelease>", lambda _event: self.refresh_list())
        self.count_var = tk.StringVar(value="0 users")
        ttk.Label(top, textvariable=self.count_var).grid(row=0, column=1)

        self.title_var = tk.StringVar(value="Select a user")
        ttk.Label(main, textvariable=self.title_var, font=("Segoe UI", 17, "bold")).grid(
            row=1, column=0, sticky="w", pady=(12, 2)
        )

        list_frame = ttk.Frame(main)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, activestyle="dotbox", font=("Segoe UI", 10), exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self.select_user)

        details = ttk.LabelFrame(main, text="Selected user", padding=10)
        details.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        details.columnconfigure(0, weight=1)
        self.info_var = tk.StringVar(value="Select someone from the list")
        ttk.Label(details, textvariable=self.info_var, font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        self.action_var = tk.StringVar()
        ttk.Label(details, textvariable=self.action_var, font=("Segoe UI", 10, "bold")).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.posts_var = tk.StringVar(value=" — ")
        ttk.Label(details, textvariable=self.posts_var, font=("Segoe UI", 9), foreground="#555",
                  justify="left", wraplength=1080).grid(row=2, column=0, sticky="w", pady=(4, 0))

        actions = ttk.Frame(main)
        actions.grid(row=5, column=0, sticky="ew", pady=8)
        self.open_btn = ttk.Button(actions, text="Open X profile", command=self.open_profile, state="disabled")
        self.open_btn.pack(side="left", padx=(0, 5))
        self.signals_btn = ttk.Button(actions, text="Read signals (profile+posts)", command=self.read_signals, state="disabled")
        self.signals_btn.pack(side="left", padx=5)
        self.dms_btn = ttk.Button(actions, text="Fetch DMs", command=self.fetch_dms, state="disabled")
        self.dms_btn.pack(side="left", padx=5)
        self.initial_btn = ttk.Button(actions, text="Generate first DM", command=self.generate_initial, state="disabled")
        self.initial_btn.pack(side="left", padx=5)
        self.reply_btn = ttk.Button(actions, text="Generate reply", command=self.generate_reply, state="disabled")
        self.reply_btn.pack(side="left", padx=5)

        conversation = ttk.LabelFrame(main, text="DM history", padding=8)
        conversation.grid(row=6, column=0, sticky="nsew")
        conversation.columnconfigure(0, weight=1)
        conversation.rowconfigure(0, weight=1)
        self.history = tk.Text(conversation, height=10, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.history.grid(row=0, column=0, sticky="nsew")

        composer = ttk.LabelFrame(main, text="Draft / exact message input", padding=8)
        composer.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        composer.columnconfigure(0, weight=1)
        self.editor = tk.Text(composer, height=7, wrap="word", font=("Segoe UI", 10))
        self.editor.grid(row=0, column=0, sticky="ew")
        bar = ttk.Frame(composer)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="Save as sent", command=lambda: self.save_message("outbound")).pack(side="left")
        ttk.Button(bar, text="Save their reply", command=lambda: self.save_message("inbound")).pack(side="left", padx=8)
        ttk.Button(bar, text="Clear", command=self.clear_editor).pack(side="left")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var, relief="sunken", anchor="w").grid(
            row=8, column=0, sticky="ew", pady=(8, 0)
        )

    # --- list / selection -----------------------------------------------------
    def _state(self, row) -> tuple[str, str]:
        return core.classify_prospect(row)

    def refresh_list(self) -> None:
        query = self.search_var.get().strip()
        source = core.search_users(query=query, limit=100000)
        self.rows = list(source)
        self.listbox.delete(0, "end")
        for index, row in enumerate(self.rows):
            handle = f"@{row['username']}"
            name = (row["display_name"] or "").strip()
            stage = _STAGE_LABEL.get(row["status"], row["status"])
            line = f"{name or handle}  {handle[:30]}  — {stage}"
            if row["eliminated"]:
                line += "  (eliminated)"
            self.listbox.insert("end", line)
        self.count_var.set(f"{len(self.rows)} users")

    def refresh_all(self) -> None:
        self.refresh_list()
        self.refresh_selection()

    def select_user(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.selected_id = int(self.rows[selection[0]]["id"])
            self.refresh_selection()

    def refresh_selection(self) -> None:
        if self.selected_id is None:
            self._clear_selected_ui()
            return
        row = core.get_user(self.selected_id)
        if row is None:
            self.selected_id = None
            self._clear_selected_ui()
            return
        self._clear_selected_ui()
        self.selected_id = int(row["id"])

        stage, action = self._state(row)
        handle = f"@{row['username']}"
        name = row["display_name"] or row["username"]
        verified = "✓" if row["verified"] else ""
        bio = (row["bio"] or "No bio.").replace("\n", " ")
        metrics = (f"{row['followers_count'] or 0} followers · {row['following_count'] or 0} following · "
                   f"{row['post_count'] or 0} posts")
        self.title_var.set(f"{name} {verified}  ({handle})")
        self.action_var.set(action)
        self.info_var.set(f"{metrics}\n{bio}")
        posts = core.format_recent_posts(self.selected_id, max_posts=6).replace("\n", "  |  ")
        self.posts_var.set(f"Recent posts: {posts[:800]}")

        self._update_buttons(row)
        self._show_history()
        self.clear_editor()

    def _update_buttons(self, row) -> None:
        for button in (self.open_btn, self.signals_btn, self.dms_btn):
            button.configure(state="normal")
        has_inbound = any(m["direction"] == "inbound" for m in core.get_messages(self.selected_id)) if self.selected_id else False
        self.reply_btn.configure(state="normal" if has_inbound else "disabled")
        self.initial_btn.configure(state="normal")

    def _clear_selected_ui(self) -> None:
        self.title_var.set("Select a user")
        self.info_var.set("Select someone from the list")
        self.action_var.set("")
        self.posts_var.set("")
        for button in (self.open_btn, self.signals_btn, self.dms_btn, self.initial_btn, self.reply_btn):
            button.configure(state="disabled")
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.insert("end", "No user selected.\n")
        self.history.configure(state="disabled")
        self.clear_editor()

    def _show_history(self) -> None:
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        rows = core.get_messages(self.selected_id) if self.selected_id else []
        if not rows:
            self.history.insert("end", "No DMs recorded yet.\n")
        for row in rows:
            who = "YOU" if row["direction"] == "outbound" else "THEM"
            self.history.insert("end", f"{who} · {row['created_at']}\n{row['content']}\n\n")
        self.history.configure(state="disabled")

    def _require(self) -> int:
        if self.selected_id is None:
            raise ValueError("Select a user first")
        return self.selected_id

    # --- user actions ----------------------------------------------------------
    def open_profile(self) -> None:
        try:
            row = core.get_user(self._require())
            if row is None:
                raise ValueError("User not found")
            core.open_profile(row["username"])
            self.status_var.set("Profile opened in your browser.")
        except Exception as exc:
            messagebox.showerror("Open X profile", str(exc))

    def add_by_handle(self) -> None:
        _dbg("add_by_handle clicked")
        handle = simpledialog.askstring(
            "Add user by @handle", "X handle (with or without @):", parent=self)
        if not handle:
            return
        handle = handle.strip().lstrip("@")
        _dbg(f"add_by_handle handle={handle}")
        self._set_busy(True, f"Adding @{handle} locally…")

        def worker() -> None:
            try:
                uid = core.add_user(handle)
                self.after(0, lambda uid=uid: self._profile_done(
                    uid, f"@{handle} added locally. Click 'Read signals' to capture "
                         "profile + posts from the browser."))
            except Exception as exc:
                error = str(exc)
                _dbg(f"add_by_handle FAILED: {exc}")
                self.after(0, lambda error=error: self._profile_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def discover_search(self) -> None:
        _dbg("discover_search clicked")
        query = simpledialog.askstring(
            "Discover from search", "Search real posts for (e.g. 'pdf extraction pain' or a topic):", parent=self)
        if not query:
            return
        self._set_busy(True, f"Searching X: {query[:40]}…")

        def worker() -> None:
            try:
                found = search_recent_users(query, count=40)
                added = 0
                for u in found:
                    u2 = dict(u)
                    u2["source"] = "search"
                    try:
                        core.upsert_user(u2)
                        added += 1
                    except Exception as exc:
                        _dbg(f"discover upsert failed: {exc}")
                self.after(0, lambda added=added: self._profile_done(None,
                    f"Discovery: found {added} new user(s) for {query!r}. Select one and 'Read signals'."))
                self.after(0, lambda: self.refresh_list())
            except Exception as exc:
                error = str(exc)
                _dbg(f"discover_search FAILED: {exc}")
                self.after(0, lambda error=error: self._profile_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def read_signals(self) -> None:
        try:
            uid = self._require()
            row = core.get_user(uid)
        except Exception as exc:
            messagebox.showerror("Read signals", str(exc))
            return
        self._set_busy(True, "Reading profile + recent posts…")

        def worker() -> None:
            try:
                res = core.read_signals_live(row["username"], user_id=uid)
                text = (f"@{row['username']}: profile + {res['new_posts']} new post(s) captured. "
                        f"{res['metrics']['followers']} followers.")
                self.after(0, lambda text=text: self._profile_done(uid, text))
            except Exception as exc:
                error = str(exc)
                _dbg(f"read_signals FAILED: {exc}")
                self.after(0, lambda error=error: self._profile_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_dms(self) -> None:
        try:
            uid = self._require()
            row = core.get_user(uid)
        except Exception as exc:
            messagebox.showerror("Fetch DMs", str(exc))
            return
        self._set_busy(True, "Fetching DMs…")

        def worker() -> None:
            try:
                res = core.read_dms_live(uid, row["username"])
                self.after(0, lambda res=res: self._dms_done(uid, res))
            except Exception as exc:
                error = str(exc)
                _dbg(f"fetch_dms FAILED: {exc}")
                self.after(0, lambda error=error: self._profile_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def _dms_done(self, uid: int, res: dict) -> None:
        self._set_busy(False, f"Fetched DMs: {len(res['messages'])} message(s), {res['new']} new saved.")
        self.refresh_selection()

    def generate_initial(self) -> None:
        try:
            uid = self._require()
            text = core.generate_initial(uid)
            self._fill_editor(text)
            self._set_busy(False, "First DM draft generated — review and send only if intent is real.")
        except Exception as exc:
            messagebox.showerror("Generate first DM", str(exc))

    def generate_reply(self) -> None:
        try:
            uid = self._require()
            text = core.generate_reply(uid)
            self._fill_editor(text)
            self._set_busy(False, "Reply draft generated.")
        except Exception as exc:
            messagebox.showerror("Generate reply", str(exc))

    def save_message(self, direction: str) -> None:
        try:
            uid = self._require()
        except Exception as exc:
            messagebox.showerror("Save message", str(exc))
            return
        text = self.editor.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Save message", "Message is empty.")
            return
        try:
            core.add_message(uid, direction, text)
            self.refresh_selection()
            self.status_var.set("Saved to DM history.")
        except Exception as exc:
            messagebox.showerror("Save message", str(exc))

    def clear_editor(self) -> None:
        self.editor.delete("1.0", "end")
        self.editor.configure(state="normal")

    def _fill_editor(self, text: str) -> None:
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)

    def _profile_done(self, uid: int | None, msg: str) -> None:
        self._set_busy(False, msg)
        if uid is not None:
            self.selected_id = int(uid)
        self.refresh_all()

    def _profile_error(self, error: str) -> None:
        self._set_busy(False, "Error.")
        messagebox.showerror("X Agent", error)

    def _set_busy(self, busy: bool, msg: str) -> None:
        self.status_var.set(msg)
        self._sweep_running = busy
        try:
            self.sweep_btn.configure(state="disabled" if busy else "normal")
        except Exception:
            pass

    # --- settings ---------------------------------------------------------------
    def ask_set_link(self) -> None:
        current = core.app_url()
        url = simpledialog.askstring("Set app link", "App/product URL (marks the goal when sent):",
                                     initialvalue=current, parent=self)
        if url:
            core.set_setting("app_url", url.strip())
            self.status_var.set("App link saved.")
        else:
            self.status_var.set("App link unchanged.")

    # --- sweep ------------------------------------------------------------------
    def run_sweep(self) -> None:
        _dbg("run_sweep clicked")
        if self._sweep_running:
            return
        self._sweep_running = True
        self._set_busy(True, "Sweep: reading signals…")

        def worker() -> None:
            _dbg("run_sweep worker START")
            sess = BrowserSession()
            try:
                groups = self._sweep_read(sess)
                sess.close()
                self.after(0, lambda g=groups: self._sweep_show(g))
            except Exception as exc:
                _dbg(f"run_sweep FAILED: {type(exc).__name__}: {exc}")
                try:
                    sess.close()
                except Exception:
                    pass
                error = str(exc)
                self.after(0, lambda error=error: self._sweep_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def _sweep_read(self, sess: BrowserSession) -> dict[str, list]:
        """Human-paced, resumable sweep pass (mirrors the LinkedIn sweep):
        read signals + DMs for up to 20 users from the browser, classify, and
        return groups."""
        MAX_PER_SESSION = 20
        sweep_id = core.start_sweep_session(max_per_session=MAX_PER_SESSION)
        work = core.get_sweep_queue(sweep_id, limit=MAX_PER_SESSION)
        _dbg(f"sweep session id={sweep_id} queue={len(work)}")
        if not work:
            self._set_busy(False, "Nothing pending in this session.")
            return {}

        successes = 0
        first_error = ""
        blocked: set[str] = set()
        n = len(work)
        self.after(0, lambda n=n: self._sweep_status(f"Sweep session: checking {n} account(s) this pass…"))

        for i, r in enumerate(work):
            username = r["username"]
            uid = int(r["id"])
            self.after(0, lambda i=i, n=n, u=username: self._sweep_status(
                f"Sweep {i + 1}/{n}: @{u}…"))
            try:
                core.read_signals_live(username, user_id=uid, session=sess)
                core.read_dms_live(uid, username, session=sess)
                successes += 1
                core.mark_swept(uid, sweep_id, "read")
            except Exception as exc:
                _dbg(f"sweep read FAILED @{username}: {type(exc).__name__}: {exc}")
                if not first_error:
                    first_error = str(exc)
                blocked.add(username)
                core.mark_swept(uid, sweep_id, "blocked")

            if i < n - 1:
                delay = random.uniform(25, 60)
                _dbg(f"sweep: sleeping {delay:.0f}s before next user")
                self.after(0, lambda d=delay: self._sweep_status(f"Sweep: pausing {d:.0f}s…"))
                time.sleep(delay)

        if successes == 0 and blocked:
            self._set_busy(False, f"Sweep pass: everything failed. First error: {first_error[:120]}")

        groups: dict[str, list] = {}
        for r in work:
            stage, _ = core.classify_prospect(r)
            groups.setdefault(stage, []).append(r)

        summary = core.sweep_session_summary(sweep_id)
        remaining = summary.get("pending", 0)
        _dbg(f"sweep read done: {summary}")
        if remaining:
            self.after(0, lambda r=remaining: self._sweep_status(
                f"Sweep pass done — {r} remaining for next run."))
        return groups

    def _sweep_preview(self, groups: dict[str, list]) -> list[dict]:
        previews: list[dict] = []
        for stage in ("initial", "reply", "goal_reply"):
            for r in groups.get(stage, []):
                uid = int(r["id"])
                try:
                    text = core.generate_initial(uid) if stage == "initial" else core.generate_reply(uid)
                except Exception as exc:
                    _dbg(f"sweep preview FAILED uid={uid}: {exc}")
                    text = f"[draft generation failed: {exc}]"
                previews.append({"stage": stage, "user": r, "text": text})
        return previews

    def _sweep_show(self, groups: dict[str, list]) -> None:
        _dbg("_sweep_show opening review window")
        self._sweep_running = False
        self._set_busy(False, "Sweep done. Review the rows and 'Type into composer' per person.")

        win = tk.Toplevel(self)
        win.title("Sweep result — semi-auto steps")
        win.geometry("1020x780")

        needs = {s: len(groups.get(s, [])) for s in ("initial", "reply", "goal_reply")}
        waiting = {s: len(groups.get(s, [])) for s in ("awaiting", "goal", "qualified", "discovered")}
        parts = []
        if needs["initial"]:
            parts.append(f"{needs['initial']} with intent need a first DM")
        if needs["reply"]:
            parts.append(f"{needs['reply']} need a reply")
        if needs["goal_reply"]:
            parts.append(f"{needs['goal_reply']} goal reached + wrote back")
        if waiting["awaiting"]:
            parts.append(f"{waiting['awaiting']} waiting on their reply (skipped)")
        if waiting["goal"]:
            parts.append(f"{waiting['goal']} goal done (skipped)")
        if waiting["qualified"] + waiting["discovered"]:
            parts.append(f"{waiting['qualified'] + waiting['discovered']} watching — no intent signal yet (skipped)")
        summary = ", ".join(parts) if parts else "Nothing actionable right now."
        ttk.Label(win, text=summary, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            anchor="w", padx=12, pady=(10, 2))

        ttk.Label(win, text=("Each person below has a draft you can edit. 'Type into composer' opens their DM "
                             "in the agent Chrome and types the draft into the message box - you press the final "
                             "Send in X, then click 'Confirm sent' to record it. Skip = leave them for later."),
                  font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(2, 4))

        canvas = tk.Canvas(win)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=4)
        vsb.pack(side="left", fill="y", padx=(0, 10), pady=4)

        rows = [_SweepRow(body, self, p) for p in previews]
        if not rows:
            ttk.Label(body, text="Everyone is either waiting, watching, or already done — nothing to do this pass.",
                      ).pack(anchor="w", padx=10, pady=20)

    def _sweep_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _sweep_error(self, error: str) -> None:
        self._sweep_running = False
        self._set_busy(False, "Sweep failed.")
        messagebox.showerror("Sweep failed", error)


def main() -> None:
    core.init_db()
    app = XAgentApp()
    app.mainloop()


if __name__ == "__main__":
    main()