#!/usr/bin/env python3
"""Имитация claude/grok CLI для проверки оркестратора без трат квоты.

FAKE_KIND=claude|grok|qwen|opencode, FAKE_STATE=<каталог для состояния>.
"""
import json
import os
import re
import sys
from pathlib import Path

kind = os.environ.get("FAKE_KIND", "claude")
state = Path(os.environ.get("FAKE_STATE", "/tmp/fake-state"))
state.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]


def opt(*names):
    for n in names:
        if n in argv:
            return argv[argv.index(n) + 1]
    return None


if kind == "claude":
    prompt = sys.stdin.read()
    sid = opt("--resume", "--session-id")
elif kind == "qwen":
    prompt = opt("-p") or sys.stdin.read()
    sid = opt("--resume", "--session-id")
elif kind == "opencode":
    prompt = argv[-1]
    sid = opt("--session") or "ses_" + os.urandom(6).hex()
    oc_cfg = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))
else:
    prompt = opt("-p", "--single") or Path(opt("--prompt-file")).read_text()
    sid = opt("-r", "-s")

if kind == "opencode" and os.environ.get("PWD"):
    os.chdir(os.environ["PWD"])  # как настоящий OpenCode: каталог проекта берётся из $PWD

exec_mode = any(x in argv for x in ("acceptEdits", "auto-edit", "debate-exec"))


def shell_allowed(cmd: str) -> bool:
    if kind == "opencode":
        bash = oc_cfg.get("permission", {}).get("bash", {})
        return any(v == "allow" and (cmd == k or (k.endswith("*") and cmd.startswith(k[:-1]))) for k, v in bash.items())
    if kind == "qwen":
        return f"run_shell_command({cmd})" in argv
    return f"Bash({cmd}:*)" in argv
stage = (re.search(r"Этап: (\w+)", prompt) or [None, "probe"])[1]
counter_file = state / f"{kind}-{stage}.n"
n = int(counter_file.read_text()) + 1 if counter_file.exists() else 1
counter_file.write_text(str(n))


def j(d):
    return "Кратко.\n```json\n" + json.dumps(d, ensure_ascii=False) + "\n```"


cancelled = False
denials = []
if stage == "independent_analysis" and n == 1 and os.environ.get("FAKE_CANCEL") and kind == "grok":
    cancelled = True
    text = "Изучаю репозиторий сам."
    sys.stderr.write("permission required: Execute `ls -la` was not run, as a headless run cannot ask for approval\n")
elif stage == "propose" and os.environ.get("FAKE_DENY") and kind == "claude":
    denials = [{"tool_name": "Bash", "tool_input": {"command": "ssh -o BatchMode=yes user@10.0.0.1 'docker ps'"}}]
    text = j({"verdict": "proposal", "summary": "План без стенда", "plan": "# План v1\n1. Без стенда"})
elif stage == "continue_after_grant":
    ok = any("Bash(ssh:*)" in a for a in argv)
    text = j({"verdict": "proposal", "summary": "После разрешения" if ok else "Разрешения нет",
              "plan": "# План v1\n1. Со стендом" if ok else "# План v1\n1. Без стенда"})
elif stage == "continue_after_denial":
    text = j({"verdict": "analysis", "summary": "Разбор после отказа", "key_points": ["ls не выполнен — непроверено"]})
elif stage == "propose" and n == 1 and os.environ.get("FAKE_BAD_JSON_FIRST"):
    text = "Вот мой план без JSON, извините."
elif stage == "fix_json":
    text = j({"verdict": "proposal", "summary": "План v1 (после fix_json)", "plan": "# План v1\n1. Сделать feature.txt"})
elif stage == "independent_analysis":
    text = j({"verdict": "analysis", "summary": "Мой разбор", "key_points": ["важно не сломать данные"]})
elif stage == "propose":
    text = j({"verdict": "proposal", "summary": "План v1", "plan": "# План v1\n1. Сделать feature.txt"})
elif stage == "critique":
    if n <= 1:
        text = j({"verdict": "disagree", "summary": "Нет тестов",
                  "points": [{"id": "C1", "severity": "blocker", "claim": "нет проверки", "evidence": "README.md:1"}],
                  "questions_for_user": [{"question": "Можно ли запускать тяжёлый прогон?", "why_critical": "память"}]})
    else:
        text = j({"verdict": "agree", "summary": "Ок",
                  "points": [{"id": "C2", "severity": "minor", "claim": "опечатка"}]})
elif stage == "respond":
    text = j({"verdict": "revised", "summary": "Добавил проверку",
              "plan": "# План v2\n1. Сделать feature.txt\n2. Проверить содержимое",
              "responses": [{"id": "C1", "decision": "accepted", "why": "справедливо"}]})
elif stage in ("execute", "fix"):
    if exec_mode:
        Path("feature.txt").write_text(f"done {stage}\n")
    text = j({"verdict": "done", "summary": f"Сделано ({stage})", "changed_files": ["feature.txt"], "checks_run": []})
elif stage == "review":
    if n == 1:
        text = j({"verdict": "changes_requested", "summary": "Нужна правка",
                  "points": [{"id": "R1", "severity": "major", "claim": "текст не тот"}]})
    else:
        text = j({"verdict": "approve", "summary": "Хорошо", "points": []})
elif stage == "continue":
    text = j({"verdict": "agree", "summary": "Учёл ответ", "points": []})
else:  # probe
    mem = state / f"{kind}-{sid}.mem"
    if "по памяти" in prompt:
        text = mem.read_text() if mem.exists() else "не помню"
    elif "probe-ok-42" in prompt:
        text = "probe-ok-42" if shell_allowed("echo") else "нет разрешения"
    elif "probe_write" in prompt:
        if exec_mode:
            Path("probe_write.txt").write_text("hello")
        text = "Не могу записать файл в режиме только чтения."
    else:
        readme = Path("README.md").read_text() if Path("README.md").exists() else ""
        num = "4817" if "4817" in readme else "?"
        mem.write_text(num)
        text = '```json\n{"verdict": "agree", "summary": "Тестовый проект, стенд ' + num + '"}\n```'

if kind == "claude":
    out = {"type": "result", "subtype": "success", "is_error": False, "result": text, "permission_denials": denials,
           "session_id": sid, "total_cost_usd": 0.01,
           "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 50}}
elif kind == "qwen":
    out = [{"type": "system", "subtype": "session_start", "session_id": sid, "model": "fake-qwen"},
           {"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
           {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid,
            "usage": {"input_tokens": 3, "output_tokens": 4}}]
elif kind == "opencode":
    ev = [{"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
          {"type": "text", "sessionID": sid, "part": {"type": "text", "text": text}},
          {"type": "step_finish", "sessionID": sid, "part": {"type": "step-finish",
                                                             "tokens": {"input": 7, "output": 8, "cache": {"read": 100, "write": 0}}}}]
    print("\n".join(json.dumps(e, ensure_ascii=False) for e in ev))
    sys.exit(0)
else:
    out = {"text": text, "stopReason": "cancelled" if cancelled else "end_turn", "sessionId": sid,
           "usage": {"input_tokens": 5, "output_tokens": 7}}
print(json.dumps(out, ensure_ascii=False))
