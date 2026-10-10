"""
agent.py — the conversation loop: sends your message (plus a bit of context)
to a local Ollama model, and if the model wants to use a tool (read a file,
run a command, etc.) it executes it via tools.py and feeds the result back,
looping until the model gives you a final plain-text answer.
"""

import json
import requests

import tools

SYSTEM_PROMPT = (
    "You are Shiv, a helpful desktop companion running locally on the user's "
    "own Windows computer. You can read/search/write files, open files and "
    "apps, run shell commands, and remove files (moved to a trash folder, "
    "never permanently deleted). Running a command or removing something "
    "always asks the user to confirm first — that confirmation happens "
    "outside of you, so if a tool result says the user declined, just accept "
    "that and don't retry the same thing.\n\n"
    "Be direct and concise. When a task needs a tool, call it — don't ask "
    "the user to do something manually that you have a tool for. Only use "
    "run_command for things the other tools genuinely can't do. Always "
    "explain briefly what you did after taking an action."
)


class ShivAgent:
    def __init__(self, config: dict, context_watcher, confirm_fn):
        self.config = config
        self.context_watcher = context_watcher
        self.confirm_fn = confirm_fn
        self.history = []  # list of {"role": ..., "content": ...} dicts

    def _call_ollama(self, messages: list) -> dict:
        url = self.config["ollama_url"].rstrip("/") + "/api/chat"
        payload = {
            "model": self.config["model"],
            "messages": messages,
            "tools": tools.TOOLS,
            "stream": False,
        }
        resp = requests.post(url, json=payload, timeout=120)
        if not resp.ok:
            # Surface Ollama's actual error body instead of a bare status code.
            raise RuntimeError(
                f"Ollama returned {resp.status_code} {resp.reason} — {resp.text[:2000]}"
            )
        return resp.json()

    def handle_message(self, user_text: str) -> str:
        ctx = self.context_watcher.snapshot() if self.context_watcher else {}
        context_note = (
            f"[Current context — active window: {ctx.get('active_window')!r}; "
            f"clipboard preview: {ctx.get('clipboard_preview')!r}]"
        )

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.history)
        messages.append({"role": "system", "content": context_note})
        messages.append({"role": "user", "content": user_text})

        self.history.append({"role": "user", "content": user_text})

        max_steps = self.config.get("max_agent_steps", 6)
        for _ in range(max_steps):
            try:
                data = self._call_ollama(messages)
            except requests.exceptions.ConnectionError:
                return (
                    "I can't reach Ollama at "
                    f"{self.config['ollama_url']} — is it installed and running? "
                    "(Try opening a terminal and running `ollama serve`, or just "
                    "start the Ollama app.)"
                )
            except Exception as e:
                return f"Something went wrong talking to the model: {e}"

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                final_text = msg.get("content", "").strip() or "(no response)"
                self.history.append({"role": "assistant", "content": final_text})
                return final_text

            # Model wants to use one or more tools — run them and continue the loop.
            messages.append(msg)
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                raw_args = fn.get("arguments", {})
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")

                if name == "write_file" and self.config.get("confirm_before_overwrite", True):
                    result = tools.dispatch(name, args, confirm=self.confirm_fn)
                elif name == "run_command" and self.config.get("confirm_before_run_command", True):
                    result = tools.dispatch(name, args, confirm=self.confirm_fn)
                elif name == "move_to_trash" and self.config.get("confirm_before_delete", True):
                    result = tools.dispatch(name, args, confirm=self.confirm_fn)
                else:
                    result = tools.dispatch(name, args, confirm=None)

                messages.append({
                    "role": "tool",
                    "content": json.dumps(result),
                })

        return "I went through a few steps without reaching a final answer — try rephrasing, or ask me to try one thing at a time."
