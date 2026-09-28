"""Worker runtimes: the one place that knows which CLI a fleet worker runs in.

The fleet (spawner, layout, bus, manager protocol, completion protocol) is
runtime-agnostic. Everything that differs between "a worker is Claude Code" and
"a worker is Cursor Agent" lives behind WorkerRuntime:

    launch(role, ctx, session)  -> the shell command cmux runs in the pane
    resume(role, ctx, session)  -> the shell command that relaunches it with context
    send(role, surface, ...)    -> verified dispatch via the bus's dispatch.py
    stop(surface)               -> ONE interrupt keypress, in that TUI's dialect

The runtime is resolved exactly once (make_runtime) from a WorkerConfig and
injected. Nothing else branches on "is this Cursor?".

Provider-independent state lives in .team/<fleet>/ (see fleet-worker.md). The
only runtime-specific state is session bookkeeping in <bus>/sessions.json and
the per-worker Cursor config dirs under <bus>/cursor/ -- neither is ever the
source of truth for task context.

Stdlib only; python3 >= 3.11.
"""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

WORKER_ROLES = ("worker-1", "worker-2", "worker-3", "worker-4")
RUNTIMES = ("claude", "cursor")

# Files on the bus that this module owns.
RUNTIME_FILE = "runtime.json"          # the CURRENT worker runtime profile
SESSIONS_FILE = "sessions.json"        # per-role, per-runtime session bookkeeping
HISTORY_FILE = "runtime-history.log"   # one line per boot: which runtime ran when
INSTRUCTIONS_FILE = "worker-instructions.md"  # copy of fleet-worker.md, inside the workspace


class RuntimeUnavailable(Exception):
    """The requested worker runtime cannot run as configured. Never fall back silently."""


@dataclass(frozen=True)
class WorkerConfig:
    """What the human asked for. Resolved once, then handed to make_runtime()."""

    runtime: str = "claude"        # "claude" | "cursor"
    model: str = "claude-opus"     # requested model FAMILY; each runtime maps it to a real id
    thinking: str = "max"          # requested reasoning level; each runtime reports what it got
    cursor_model: str | None = None  # explicit Cursor model id override (must be a Claude model)


@dataclass(frozen=True)
class FleetContext:
    fleet: str
    target: Path   # project the fleet works on (every pane's cwd)
    bus: Path      # <target>/.team/<fleet>
    assets: Path   # directory holding this file, fleet-worker.md, ...


@dataclass
class ModelSelection:
    """What a runtime WILL pass to its CLI, and how sure we are about it."""

    cli_model: str             # the exact --model argument
    display: str               # human-readable model name
    thinking: str              # human-readable reasoning setting
    thinking_verified: bool    # True only when the CLI itself confirmed the setting exists
    source: str                # how the selection was determined
    notes: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Boot prompt -- identical protocol for every runtime.
# --------------------------------------------------------------------------- #

def worker_boot_prompt(role: str, ctx: FleetContext, instructions: Path | None = None) -> str:
    """The first user turn every worker gets, whatever CLI it runs in.

    `instructions` is set when the runtime cannot load fleet-worker.md as a
    system prompt (Cursor has no public flag for that), so the worker is told
    to read the same file from disk instead. The protocol text is the same file
    either way.
    """
    parts = [
        f"You are {role} on fleet {ctx.fleet}.",
        f'Your shared bus is the directory .team/{ctx.fleet}/ under "{ctx.target}" '
        f'(absolute path: "{ctx.bus}"); your files there are {role}.md and {role}.done.',
    ]
    if instructions is not None:
        parts.append(
            f'Your standing fleet instructions are in the file "{instructions}". '
            "Read that whole file now and follow it exactly as if it were your system prompt; "
            "re-read it whenever you are unsure of the protocol or after your context has been compacted."
        )
    parts.append(
        "The bus is the source of truth, not your chat history: before replying, read "
        f"{role}.md (your own notes, possibly written by a previous run under a different CLI), "
        "manager.md and backlog.md if they exist."
    )
    parts.append(
        f"Then reply with one line: ready: {role}  -- if your notes show an unfinished task, "
        "append (unfinished: <that task in a few words>) -- and wait for the manager to assign work. "
        "Do not resume unfinished work until the manager tells you to."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# The abstraction
# --------------------------------------------------------------------------- #

class WorkerRuntime:
    """Base class. Subclasses fill in the CLI-specific parts."""

    name = "base"
    label = "worker"
    # Strings the TUI shows while a turn is in progress (lowercased, matched after
    # whitespace normalization by dispatch.py).
    working_signals: tuple[str, ...] = ()
    # ONE press of this key interrupts the current turn without exiting the app.
    interrupt_key = "escape"
    # ONE press of this key clears a non-empty input bar without exiting the app.
    clear_key = "ctrl+c"

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.selection: ModelSelection | None = None
        self.warnings: list[str] = []

    # -- lifecycle ---------------------------------------------------------- #
    def preflight(self, ctx: FleetContext) -> ModelSelection:
        """Check the CLI exists and resolve the model. Raise RuntimeUnavailable if not."""
        raise NotImplementedError

    def new_session(self, role: str, ctx: FleetContext) -> dict:
        """Create runtime-specific session bookkeeping for one worker launch."""
        raise NotImplementedError

    def launch(self, role: str, ctx: FleetContext, session: dict) -> str:
        """Shell command that boots this worker in its pane."""
        raise NotImplementedError

    def resume(self, role: str, ctx: FleetContext, session: dict) -> str:
        """Shell command that relaunches this worker with its private history.

        Private history is a convenience. A FRESH launch recovers everything
        that matters from the bus.
        """
        raise NotImplementedError

    def send(self, role: str, surface: str, message: str, ctx: FleetContext,
             runner=subprocess.run) -> int:
        """Deliver one single-line task through the bus's verified dispatcher.

        The manager does exactly this from its pane; the method exists so the
        orchestration layer never has to know the dispatcher's arguments.
        """
        argv = [sys.executable, str(ctx.bus / "dispatch.py"), "--surface", surface,
                "--clear", str(ctx.bus / f"{role}.done"), message]
        return runner(argv).returncode

    def stop(self, surface: str, runner=subprocess.run) -> int:
        """Interrupt the worker's current turn. Exactly one keypress -- never two:
        a second ctrl+c exits both TUIs, and the pane cannot be respawned."""
        return runner(["cmux", "send-key", "--surface", surface, self.interrupt_key]).returncode

    # -- reporting ---------------------------------------------------------- #
    def require_selection(self) -> ModelSelection:
        if self.selection is None:
            raise RuntimeError("preflight() has not run")
        return self.selection

    def profile(self) -> dict:
        """What gets written to <bus>/runtime.json (read by dispatch.py and by agents)."""
        sel = self.require_selection()
        return {
            "runtime": self.name,
            "label": self.label,
            "model": sel.display,
            "cli_model": sel.cli_model,
            "thinking": sel.thinking,
            "thinking_verified": sel.thinking_verified,
            "model_source": sel.source,
            "subagents": self.subagent_policy(),
            "working_signals": list(self.working_signals),
            "interrupt_key": self.interrupt_key,
            "clear_key": self.clear_key,
            "notes": list(sel.notes),
            "warnings": list(self.warnings),
            "booted_at": utc_now(),
        }

    def subagent_policy(self) -> str:
        raise NotImplementedError

    def summary_lines(self) -> list[str]:
        sel = self.require_selection()
        return [
            f"Workers: {self.label} × {len(WORKER_ROLES)}",
            f"Model: {sel.display}",
            f"Thinking: {sel.thinking}",
            f"Subagents: {self.subagent_policy()}",
        ]


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #

class ClaudeWorkerRuntime(WorkerRuntime):
    """Claude Code workers -- the original, default behavior.

    Model alias `opus` and `--effort max` are verified against `claude --help`
    (effort levels: low, medium, high, xhigh, max). The worker protocol loads as
    a real system prompt via --append-system-prompt-file.
    """

    name = "claude"
    label = "Claude Code"
    working_signals = ("esc to interrupt",)
    interrupt_key = "escape"
    clear_key = "ctrl+c"

    MODEL_ALIASES = {"claude-opus": "opus"}

    def __init__(self, config: WorkerConfig, binary: str = "claude"):
        super().__init__(config)
        self.binary = binary

    def preflight(self, ctx: FleetContext) -> ModelSelection:
        model = self.MODEL_ALIASES.get(self.config.model, self.config.model)
        self.selection = ModelSelection(
            cli_model=model,
            display=f"Claude {model.capitalize()} (alias '{model}')",
            thinking=f"{self.config.thinking} (--effort {self.config.thinking})",
            thinking_verified=True,
            source="claude --help: --model <alias>, --effort low|medium|high|xhigh|max",
        )
        return self.selection

    def new_session(self, role: str, ctx: FleetContext) -> dict:
        # A pinned session id makes resume unambiguous. Without it, `claude
        # --continue` in the shared cwd picks whichever of the FIVE fleet
        # sessions (manager + 4 workers) was most recent.
        return {"session_id": str(uuid.uuid4()), "launched_at": utc_now()}

    def _flags(self, ctx: FleetContext) -> list[str]:
        sel = self.require_selection()
        return [
            "--model", sel.cli_model,
            "--effort", self.config.thinking,
            "--dangerously-skip-permissions",
            "--append-system-prompt-file", str(ctx.assets / "fleet-worker.md"),
        ]

    def launch(self, role: str, ctx: FleetContext, session: dict) -> str:
        argv = [self.binary, *self._flags(ctx), "--session-id", session["session_id"],
                worker_boot_prompt(role, ctx)]
        return shlex.join(argv)

    def resume(self, role: str, ctx: FleetContext, session: dict) -> str:
        # Same flags as the launch: identity lives in launch flags, not the session.
        argv = [self.binary, "--resume", session["session_id"], *self._flags(ctx)]
        return f"cd {shlex.quote(str(ctx.target))} && {shlex.join(argv)}"

    def subagent_policy(self) -> str:
        return ("Claude models only (Claude Code has no non-Anthropic models). Built-in subagents "
                "may pick a smaller Claude model; set CLAUDE_CODE_SUBAGENT_MODEL=opus via --env-file "
                "to pin them.")


# --------------------------------------------------------------------------- #
# Cursor Agent
# --------------------------------------------------------------------------- #

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CLAUDE_FAMILY_RE = re.compile(r"claude|opus|sonnet|haiku", re.IGNORECASE)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

# Ranking for reasoning levels as Cursor names them. Unknown values are never
# ranked above known ones; if NO value is known we take the last one the CLI
# lists and say so rather than claiming "max".
THINKING_RANK = {
    "max": 100, "maximum": 100,
    "xhigh": 90, "x-high": 90, "extra-high": 90, "extra_high": 90, "very-high": 90, "very_high": 90,
    "true": 80, "on": 80, "enabled": 80,
    "high": 70,
    "medium": 50, "med": 50,
    "low": 30,
    "minimal": 20, "min": 20,
    "none": 0, "off": 0, "false": 0, "disabled": 0,
}


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:4])


def pick_highest_thinking(values: list[str]) -> tuple[str, bool]:
    """Return (value, known). known=False means no value was recognizable."""
    known = [v for v in values if v.lower() in THINKING_RANK]
    if known:
        return max(known, key=lambda v: THINKING_RANK[v.lower()]), True
    return values[-1], False


class CursorWorkerRuntime(WorkerRuntime):
    """Cursor Agent workers, pinned to Claude Opus at the highest reasoning level
    the installed CLI reports for it.

    Verified against cursor-agent 2026.09.26 (see README, "Cursor workers"):

    * `--model 'id[param=value]'` selects a model variant. Invalid ids or
      values are SILENTLY healed to the default variant, so we never pass a
      value the CLI did not first report as valid.
    * `cursor-agent acp` answers the `cursor/list_available_models` extension
      with every model and its parameter options; options in the
      "thought_level" category are the reasoning setting. That is the probe.
    * Older CLIs without that method: fall back to `--list-models`, pick the
      Opus id, and report thinking as NOT verified.
    * There is no public system-prompt flag, so the worker is told to read
      fleet-worker.md (copied onto the bus) as its first action.
    * CURSOR_CONFIG_DIR relocates cli-config.json and the chat store (auth and
      ~/.cursor/mcp.json are unaffected). Each worker gets its own dir, which
      (a) pins the Explore subagent to `inherit` without touching the user's
      global config and (b) makes `--continue` resume exactly that worker.
    """

    name = "cursor"
    label = "Cursor Agent"
    # "ctrl+c to stop" is the input bar's right-hand placeholder while a turn
    # runs; "Thinking…"/"Responding…" are the status line.
    working_signals = ("ctrl+c to stop", "thinking…", "responding…")
    # Cursor: ONE ctrl+c stops a running turn (a second, soon after, exits).
    interrupt_key = "ctrl+c"
    clear_key = "ctrl+c"

    BINARIES = ("cursor-agent", "agent")
    PROBE_TIMEOUT = 45.0

    def __init__(self, config: WorkerConfig, binary: str | None = None,
                 user_config_dir: Path | None = None):
        super().__init__(config)
        self._binary = binary
        self._user_config_dir = user_config_dir

    # -- discovery ---------------------------------------------------------- #
    @property
    def binary(self) -> str:
        if self._binary is None:
            self._binary = self.find_binary()
        return self._binary

    def find_binary(self) -> str:
        for name in self.BINARIES:
            path = shutil.which(name)
            if not path:
                continue
            if name == "agent":  # generic name: make sure it IS Cursor's
                helptext = subprocess.run([path, "--help"], capture_output=True, text=True).stdout
                if "Cursor Agent" not in helptext:
                    continue
            return path
        raise RuntimeUnavailable(
            "--cwa needs the Cursor CLI, but neither `cursor-agent` nor `agent` is on PATH.\n"
            "  install: curl https://cursor.com/install -fsS | bash   then: cursor-agent login"
        )

    def user_config_dir(self) -> Path:
        """Where the user's own cli-config.json lives (mirrors the CLI's lookup)."""
        if self._user_config_dir is not None:
            return self._user_config_dir
        env = os.environ.get("CURSOR_CONFIG_DIR", "").strip()
        if env:
            return Path(env)
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        if xdg:
            return Path(xdg) / "cursor"
        return Path.home() / ".cursor"

    # -- model probe -------------------------------------------------------- #
    def probe_models_acp(self, cwd: Path) -> list[dict] | None:
        """Ask `cursor-agent acp` for models + parameter options.

        Returns None when this CLI has no such method (caller falls back).
        Raises RuntimeUnavailable on auth failure or a dead CLI.
        """
        try:
            proc = subprocess.Popen([self.binary, "acp"], cwd=str(cwd), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RuntimeUnavailable(f"could not start `{self.binary} acp`: {exc}") from exc

        assert proc.stdin is not None and proc.stdout is not None  # both are PIPEs
        stdin, stdout = proc.stdin, proc.stdout
        deadline = time.monotonic() + self.PROBE_TIMEOUT
        fd = stdout.fileno()
        pending = b""  # raw bytes read but not yet split into lines

        def call(msg_id: int, method: str, params: dict) -> dict:
            nonlocal pending
            stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                                          "params": params}) + "\n").encode())
            stdin.flush()
            while True:
                # Drain complete lines first. Raw os.read + our own splitting:
                # select() on a BUFFERED reader misses lines already in its buffer.
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == msg_id and "method" not in msg:
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeUnavailable(
                        f"`{self.binary} acp` did not answer {method} within {self.PROBE_TIMEOUT:.0f}s")
                ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        raise RuntimeUnavailable(f"`{self.binary} acp` exited during {method}")
                    pending += chunk

        try:
            init = call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                          "clientInfo": {"name": "claude-fleet", "version": "1"}})
            if "error" in init:
                raise RuntimeUnavailable(f"cursor ACP initialize failed: {init['error']}")
            resp = call(2, "cursor/list_available_models", {})
        finally:
            proc.kill()
            proc.wait()
            stdin.close()
            stdout.close()

        err = resp.get("error")
        if err:
            text = json.dumps(err)
            if err.get("code") == -32601 or "Method not found" in text:
                return None
            if "uthenticat" in text:
                raise RuntimeUnavailable(
                    "Cursor CLI is not logged in, so it cannot list models.\n"
                    f"  run: {Path(self.binary).name} login   (or set CURSOR_API_KEY)")
            raise RuntimeUnavailable(f"Cursor model listing failed: {err.get('message', text)}")

        models = []
        for m in (resp.get("result") or {}).get("models") or []:
            options = []
            for opt in m.get("configOptions") or []:
                options.append({
                    "id": opt.get("id", ""),
                    "name": opt.get("name", ""),
                    "category": opt.get("category", ""),
                    "current": opt.get("currentValue"),
                    "values": [o.get("value") for o in opt.get("options") or [] if o.get("value")],
                })
            models.append({"id": m.get("value", ""), "name": m.get("name", ""), "options": options})
        return models

    def probe_models_list(self, cwd: Path) -> list[dict]:
        """Fallback: parse `cursor-agent --list-models` (ids + names, no parameters)."""
        try:
            r = subprocess.run([self.binary, "--list-models"], cwd=str(cwd), capture_output=True,
                               text=True, timeout=self.PROBE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeUnavailable("`cursor-agent --list-models` timed out") from exc
        out = ANSI_RE.sub("", (r.stdout or "") + "\n" + (r.stderr or ""))
        if r.returncode != 0:
            if "uthenticat" in out:
                raise RuntimeUnavailable(
                    "Cursor CLI is not logged in, so it cannot list models.\n"
                    f"  run: {Path(self.binary).name} login   (or set CURSOR_API_KEY)")
            raise RuntimeUnavailable(f"`cursor-agent --list-models` failed: {out.strip()[:300]}")
        models = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith(("Available models", "Tip:", "No models")):
                continue
            head, _, rest = line.partition(" - ")
            head = head.split()[0] if head.split() else ""
            if MODEL_ID_RE.match(head):
                name = re.sub(r"\s*\((current|default)(, (current|default))?\)\s*$", "", rest).strip()
                models.append({"id": head, "name": name, "options": []})
        return models

    # -- selection ---------------------------------------------------------- #
    def select_model(self, models: list[dict], verified_params: bool) -> ModelSelection:
        if not models:
            raise RuntimeUnavailable("Cursor reported no models for this account.")

        explicit = self.config.cursor_model
        if explicit:
            chosen = next((m for m in models if m["id"] == explicit), None)
            if chosen is None:
                raise RuntimeUnavailable(
                    f"--cursor-model {explicit!r} is not in this account's Cursor model list.\n"
                    f"  available Claude models: {self._claude_ids(models) or '(none)'}")
            if not CLAUDE_FAMILY_RE.search(chosen["id"] + " " + chosen["name"]):
                raise RuntimeUnavailable(
                    f"--cursor-model {explicit!r} is not a Claude model; Cursor workers are Claude-only.")
            if "opus" not in (chosen["id"] + " " + chosen["name"]).lower():
                self.warnings.append(f"--cursor-model {explicit} is a Claude model but not Opus.")
        else:
            wanted = self.config.model.replace("claude-", "")  # "opus"
            candidates = [m for m in models if wanted in (m["id"] + " " + m["name"]).lower()
                          and CLAUDE_FAMILY_RE.search(m["id"] + " " + m["name"])]
            if not candidates:
                raise RuntimeUnavailable(
                    f"No Claude {wanted.capitalize()} model is available to this Cursor account, and "
                    "Cursor workers never fall back to another model.\n"
                    f"  Claude models Cursor lists: {self._claude_ids(models) or '(none)'}\n"
                    "  pick one explicitly with --cursor-model <id>, or run without --cwa.")

            def rank(m: dict):
                text = (m["id"] + " " + m["name"]).lower()
                level = max((THINKING_RANK[t] for t in re.split(r"[^a-z]+", text) if t in THINKING_RANK),
                            default=-1)
                return (_version_key(m["id"]), "fast" not in text, "thinking" in text, level)

            chosen = max(candidates, key=rank)

        return self._thinking_for(chosen, verified_params)

    def _thinking_for(self, model: dict, verified_params: bool) -> ModelSelection:
        mid = model["id"]
        display = f"{model['name'] or mid} ({mid})" if model["name"] != mid else mid
        thought = [o for o in model["options"] if o["category"] == "thought_level" and o["values"]]
        if thought:
            opt = thought[0]
            value, known = pick_highest_thinking(opt["values"])
            listed = ", ".join(opt["values"])
            if not re.fullmatch(r"[A-Za-z0-9._-]+", opt["id"]) or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
                raise RuntimeUnavailable(f"unexpected Cursor parameter syntax: {opt['id']}={value}")
            thinking = (f"{value} -- the highest '{opt['name'] or opt['id']}' level Cursor reports for {mid} "
                        f"(options: {listed}); passed as [{opt['id']}={value}]")
            notes = []
            if not known:
                thinking = (f"{value} -- last listed '{opt['id']}' option for {mid} (options: {listed}); "
                            "none of the names are recognizable levels, so 'maximum' is NOT claimed")
                notes.append("thinking level chosen by list order, not by name")
            return ModelSelection(cli_model=f"{mid}[{opt['id']}={value}]", display=display,
                                  thinking=thinking, thinking_verified=known,
                                  source="cursor-agent acp: cursor/list_available_models", notes=notes)

        if verified_params:
            thinking = (f"model default -- Cursor reports no thinking/effort parameter for {mid}"
                        + ("; the id itself names a thinking variant" if "thinking" in mid.lower() else ""))
            return ModelSelection(cli_model=mid, display=display, thinking=thinking,
                                  thinking_verified=True,
                                  source="cursor-agent acp: cursor/list_available_models")

        thinking = ("NOT verified -- this Cursor CLI does not expose model parameters; "
                    + (f"'{mid}' is named as a thinking variant" if "thinking" in mid.lower()
                       else f"'{mid}' runs at its default reasoning level"))
        return ModelSelection(cli_model=mid, display=display, thinking=thinking, thinking_verified=False,
                              source="cursor-agent --list-models (fallback; no parameter info)",
                              notes=["upgrade the Cursor CLI (`cursor-agent update`) for verified thinking selection"])

    @staticmethod
    def _claude_ids(models: list[dict]) -> str:
        return ", ".join(m["id"] for m in models if CLAUDE_FAMILY_RE.search(m["id"] + " " + m["name"]))

    def preflight(self, ctx: FleetContext) -> ModelSelection:
        _ = self.binary  # raises RuntimeUnavailable if missing
        models = self.probe_models_acp(ctx.target)
        verified = models is not None
        if models is None:
            models = self.probe_models_list(ctx.target)
        self.selection = self.select_model(models, verified_params=verified)
        self.warnings.extend(scan_subagent_definitions(ctx.target))
        return self.selection

    # -- sessions ----------------------------------------------------------- #
    def config_dir(self, role: str, ctx: FleetContext) -> Path:
        return ctx.bus / "cursor" / role

    def new_session(self, role: str, ctx: FleetContext) -> dict:
        """Per-worker CURSOR_CONFIG_DIR with the user's cli-config.json plus one pin:
        the built-in Explore subagent inherits the worker's (Claude Opus) model
        instead of Cursor's default Explore model."""
        cfg_dir = self.config_dir(role, ctx)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        base: dict = {}
        src = self.user_config_dir() / "cli-config.json"
        if src.is_file():
            try:
                base = json.loads(src.read_text())
            except (OSError, json.JSONDecodeError):
                base = {}
        # Keep whatever the worker itself persisted last run (e.g. lastUsedModel),
        # then re-apply the user's settings and the pins on top.
        mine = cfg_dir / "cli-config.json"
        if mine.is_file():
            try:
                base = {**json.loads(mine.read_text()), **base}
            except (OSError, json.JSONDecodeError):
                pass
        base.setdefault("version", 1)
        base["exploreSubagentModel"] = "inherit"
        base["subagentModels"] = {**(base.get("subagentModels") or {}), "explore": "inherit"}
        mine.write_text(json.dumps(base, indent=2) + "\n")
        return {"config_dir": str(cfg_dir), "launched_at": utc_now()}

    def _argv(self, ctx: FleetContext, session: dict) -> list[str]:
        sel = self.require_selection()
        return [
            "env", f"CURSOR_CONFIG_DIR={session['config_dir']}",
            self.binary,
            "--model", sel.cli_model,
            "--force",         # run tools without prompting (parity with --dangerously-skip-permissions)
            "--trust",         # no workspace-trust dialog blocking the boot
            "--approve-mcps",  # no MCP approval dialog blocking the boot
            "--workspace", str(ctx.target),
        ]

    def launch(self, role: str, ctx: FleetContext, session: dict) -> str:
        prompt = worker_boot_prompt(role, ctx, instructions=ctx.bus / INSTRUCTIONS_FILE)
        return shlex.join([*self._argv(ctx, session), prompt])

    def resume(self, role: str, ctx: FleetContext, session: dict) -> str:
        # The chat store lives in this worker's own CURSOR_CONFIG_DIR, so
        # --continue can only mean this worker's latest chat.
        return f"cd {shlex.quote(str(ctx.target))} && {shlex.join([*self._argv(ctx, session), '--continue'])}"

    def subagent_policy(self) -> str:
        return ("custom subagents default to model 'inherit' (Claude Opus); built-in Explore pinned to "
                "'inherit' via per-worker cli-config.json; a per-call model argument chosen by the "
                "worker itself cannot be blocked by any CLI flag (prompt-level rule only)")


def scan_subagent_definitions(target: Path, home: Path | None = None) -> list[str]:
    """Warn about Cursor-visible subagent files that would NOT inherit the worker's model."""
    home = home or Path.home()
    dirs = [target / ".cursor" / "agents", target / ".claude" / "agents",
            home / ".cursor" / "agents", home / ".claude" / "agents"]
    warnings = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in (".md", ".mdc", ".markdown") or not f.is_file():
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
            if not m:
                continue
            fm = {}
            for line in m.group(1).splitlines():
                k, sep, v = line.partition(":")
                if sep:
                    fm[k.strip().lower()] = v.strip().strip("'\"")
            model = fm.get("model", "inherit") or "inherit"
            if fm.get("force-default-model", "").lower() == "true":
                warnings.append(f"subagent {f} sets force-default-model: true -> runs on Cursor's default model")
            elif model.lower() != "inherit" and not CLAUDE_FAMILY_RE.search(model):
                warnings.append(f"subagent {f} pins model: {model} (not a Claude model)")
            elif model.lower() != "inherit":
                warnings.append(f"subagent {f} pins model: {model} (Claude, but not the worker's Opus)")
    return warnings


# --------------------------------------------------------------------------- #
# Resolution + bus bookkeeping
# --------------------------------------------------------------------------- #

def worker_config(cwa: bool, cursor_model: str | None = None) -> WorkerConfig:
    """The only place the --cwa flag is interpreted."""
    return WorkerConfig(runtime="cursor" if cwa else "claude", cursor_model=cursor_model)


def make_runtime(config: WorkerConfig) -> WorkerRuntime:
    if config.runtime == "claude":
        return ClaudeWorkerRuntime(config)
    if config.runtime == "cursor":
        return CursorWorkerRuntime(config)
    raise ValueError(f"unknown worker runtime {config.runtime!r}; expected one of {RUNTIMES}")


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_boot(runtime: WorkerRuntime, ctx: FleetContext, sessions: dict[str, dict]) -> None:
    """Write runtime.json (current profile), merge sessions.json, append history.

    sessions.json keeps EVERY runtime's last session per role, so switching
    Claude -> Cursor -> Claude can still resume the older Claude session.
    """
    (ctx.bus / RUNTIME_FILE).write_text(json.dumps(runtime.profile(), indent=2) + "\n")

    path = ctx.bus / SESSIONS_FILE
    all_sessions = load_json(path)
    for role, session in sessions.items():
        entry = dict(session)
        entry["resume"] = runtime.resume(role, ctx, session)
        all_sessions.setdefault(role, {})[runtime.name] = entry
    all_sessions["current_runtime"] = runtime.name
    path.write_text(json.dumps(all_sessions, indent=2) + "\n")

    sel = runtime.require_selection()
    with (ctx.bus / HISTORY_FILE).open("a") as fh:
        fh.write(f"{utc_now()} workers={runtime.name} model={sel.cli_model}\n")
