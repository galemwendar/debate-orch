"""Адаптеры консольных агентов: запуск в неинтерактивном режиме и разбор их ответа.

Поддерживаемые виды (kind): claude (Claude Code), grok (Grok Build), qwen (Qwen Code), opencode (OpenCode).

Общая схема одного хода:
    Agent.build()  → командная строка, stdin и переменные окружения для конкретного CLI;
    Agent.ask()    → запуск процесса, таймаут, Ctrl+C, лог в raw/;
    parse_output() → текст ответа, id сессии, токены, причина остановки и отказы в правах.

Сессия у каждой роли своя: первый вызов создаёт её (свой id или id, выданный CLI), следующие продолжают,
поэтому агенту передаётся только новая реплика, а не вся история обсуждения.

Разрешения везде задаются в одном виде — шаблонами Claude Code (`Bash(git status:*)`, `Bash(pwd)`, `WebFetch`,
`Write(//tmp/x/**)`); каждый адаптер переводит их в синтаксис своего CLI.
"""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import READONLY_BASH

ARG_LIMIT = 100_000  # промпт длиннее этого передаётся файлом или через stdin, а не аргументом командной строки
KINDS = ("claude", "grok", "qwen", "opencode")


class AgentError(RuntimeError):
    """CLI завершился с ошибкой, по таймауту или вернул ответ с признаком ошибки."""


@dataclass
class TurnResult:
    """Результат одного вызова агента, приведённый к общему виду для всех CLI."""

    text: str                 # итоговый текст ответа (в нём ищется JSON-блок с вердиктом)
    raw: str                  # сырой stdout CLI — для логов и отладки
    session_id: str | None    # id сессии, если CLI его сообщил
    tokens_in: int | None     # входные токены с учётом кэша, если CLI их сообщил
    tokens_out: int | None
    duration_s: float
    is_error: bool = False
    stop: str = ""      # причина остановки хода (end_turn, cancelled, …)
    stderr: str = ""
    denials: list[dict] = field(default_factory=list)  # [{"tool": "Bash", "cmd": "...", "truncated": bool}]


def bash_prefix(pattern: str) -> tuple[str, bool] | None:
    """'Bash(git status:*)' → ('git status', True); 'Bash(pwd)' → ('pwd', False); не Bash → None."""
    m = re.fullmatch(r"Bash\((.*)\)", pattern.strip())
    if not m:
        return None
    body = m.group(1)
    if body.endswith(":*"):
        return body[:-2], True
    return body, False


@dataclass
class Agent:
    """Участник обсуждения в одной роли (автор, критик, исполнитель или ревьюер).

    Один и тот же CLI может играть несколько ролей — у каждой роли свой объект Agent и своя сессия,
    чтобы, например, исполнитель не видел переписку автора и работал строго по итоговому плану.
    """

    name: str          # ключ из [agents]: claude / grok / qwen / opencode / любое своё имя
    kind: str          # claude | grok | qwen | opencode — определяет синтаксис CLI
    cmd: str           # исполняемый файл
    role: str          # proposer | critic | executor | reviewer
    model: str = ""    # пусто — флаг модели не передаётся, CLI берёт свою модель по умолчанию
    debate_args: list[str] = field(default_factory=list)   # аргументы на этапах обсуждения и ревью
    exec_args: list[str] = field(default_factory=list)     # аргументы на этапе исполнения
    extra_args: list[str] = field(default_factory=list)    # дописываются в конец командной строки
    allow_debate: list[str] = field(default_factory=list)  # добавочные разрешения из конфига (обсуждение и ревью)
    allow_exec: list[str] = field(default_factory=list)    # добавочные разрешения из конфига (исполнение)
    options: dict = field(default_factory=dict)            # настройки адаптера (см. config.DEFAULTS)
    timeout_s: int = 1800
    log_dir: Path | None = None
    grants: list[str] = field(default_factory=list)       # разрешения пользователя для обсуждения и ревью
    grants_exec: list[str] = field(default_factory=list)  # разрешения пользователя для исполнения
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started: bool = False   # сессия уже создана: следующие вызовы её продолжают
    calls: int = 0          # счётчик вызовов — для имён файлов в raw/

    @classmethod
    def from_cfg(cls, name: str, role: str, a: dict, timeout_s: int, log_dir: Path | None) -> "Agent":
        kind = a.get("kind", name)
        if kind not in KINDS:
            raise ValueError(f"Неизвестный kind агента {name!r}: {kind!r} (нужно одно из: {', '.join(KINDS)})")
        return cls(name=name, kind=kind, cmd=a.get("cmd", kind), role=role, model=a.get("model", ""),
                   debate_args=list(a.get("debate_args", [])), exec_args=list(a.get("exec_args", [])),
                   extra_args=list(a.get("extra_args", [])),
                   allow_debate=list(a.get("allow_debate", [])), allow_exec=list(a.get("allow_exec", [])),
                   options=dict(a.get("options", {})), timeout_s=timeout_s, log_dir=log_dir)

    def reset_session(self) -> None:
        """Начать новую сессию (прошлая история роли агенту будет недоступна)."""
        self.session_id = str(uuid.uuid4())
        self.started = False

    def allowed_patterns(self, mode: str) -> list[str]:
        """Всё, что разрешено без вопроса на этапе: стандартное + добавки из конфига + выданное пользователем."""
        args = self.exec_args if mode == "exec" else self.debate_args
        pats = [x for x in args if x.startswith("Bash(") or x in ("Read", "Grep", "Glob")]
        if self.kind in ("qwen", "opencode"):
            pats += READONLY_BASH
        return list(dict.fromkeys(pats + self._extra(mode)))

    def _extra(self, mode: str) -> list[str]:
        """Добавки из конфига (allow_*) и разрешения пользователя — то, что адаптер передаёт поверх своих аргументов."""
        cfg_allow = self.allow_exec if mode == "exec" else self.allow_debate
        return list(dict.fromkeys(cfg_allow + self._grants(mode)))

    def _grants(self, mode: str) -> list[str]:
        return self.grants_exec if mode == "exec" else self.grants

    # ---------- построение команды ----------
    def build(self, prompt: str, cwd: Path, rules: str, mode: str) -> tuple[list[str], str | None, dict]:
        """Строит запуск для этапа `mode` ("debate" — обсуждение и ревью, "exec" — исполнение).

        Возвращает (argv, текст для stdin или None, дополнительные переменные окружения).
        """
        mode_args = list(self.exec_args if mode == "exec" else self.debate_args)
        return getattr(self, f"_build_{self.kind}")(prompt, cwd, rules, mode, mode_args)

    def _build_claude(self, prompt, cwd, rules, mode, mode_args):
        """Claude Code: `claude -p`, промпт через stdin, правила — в дополнение к системному промпту.

        Новая сессия создаётся с нашим id (--session-id), дальше продолжается через --resume.
        Добавочные разрешения вставляются в тот же список --allowedTools, что и стандартные.
        """
        grants = self._extra(mode)
        if grants:
            if "--allowedTools" in mode_args:
                i = mode_args.index("--allowedTools") + 1
                mode_args[i:i] = grants
            else:
                mode_args += ["--allowedTools", *grants]
        c = [self.cmd, "-p", "--output-format", "json"]
        c += ["--resume", self.session_id] if self.started else ["--session-id", self.session_id]
        if self.model:
            c += ["--model", self.model]
        if rules:
            c += ["--append-system-prompt", rules]
        c += mode_args + self.extra_args
        return c, prompt, {}  # промпт через stdin

    def _build_grok(self, prompt, cwd, rules, mode, mode_args):
        """Grok Build: `grok -p`, сессия -s <id> / -r <id>, правила через --rules, разрешения через --allow.

        Особенность: на действие без разрешения Grok в headless-режиме не отвечает отказом, а обрывает ход
        (stopReason: cancelled) и пишет команду в stderr — это разбирает parse_stderr_denials().
        """
        c = [self.cmd, "--output-format", "json", "--cwd", str(cwd)]
        c += ["-r", self.session_id] if self.started else ["-s", self.session_id]
        if self.model:
            c += ["-m", self.model]
        if rules:
            c += ["--rules", rules]
        c += mode_args + self.extra_args
        for g in self._extra(mode):
            c += ["--allow", g]
        if len(prompt) > ARG_LIMIT and self.log_dir:
            c += ["--prompt-file", str(self._prompt_file(prompt))]
        else:
            c += ["-p", prompt]
        return c, None, {}

    def _build_qwen(self, prompt, cwd, rules, mode, mode_args):
        """Qwen Code: `qwen -p`, сессия --session-id / --resume, режимы --approval-mode.

        Разрешения на shell передаются как --allowed-tools run_shell_command(<префикс>); флаг и формат
        можно поменять в [agents.<имя>.options], если ваша версия CLI их называет иначе.
        """
        c = [self.cmd, "--output-format", "json"]
        c += ["--resume", self.session_id] if self.started else ["--session-id", self.session_id]
        if self.model:
            c += ["-m", self.model]
        if rules:
            c += ["--append-system-prompt", rules]
        c += mode_args + self.extra_args
        flag = self.options.get("grant_flag", "--allowed-tools")
        fmt_prefix = self.options.get("grant_format", "run_shell_command({cmd})")
        tool_map = self.options.get("tool_map", {})
        allow = READONLY_BASH + self._extra(mode)
        for p in dict.fromkeys(allow):
            bp = bash_prefix(p)
            if bp:
                c += [flag, fmt_prefix.format(cmd=bp[0])]
            elif p in tool_map:
                c += [flag, tool_map[p]]
        if len(prompt) > ARG_LIMIT:
            return c, prompt, {}  # длинный промпт через stdin
        c += ["-p", prompt]
        return c, None, {}

    def _build_opencode(self, prompt, cwd, rules, mode, mode_args):
        """OpenCode: `opencode run --format json` (поток событий), сессия --session <id из первого ответа>.

        Права и собственные агенты передаются конфигом через переменную OPENCODE_CONFIG_CONTENT,
        поэтому ваш opencode.json не меняется.
        """
        c = [self.cmd, "run", "--format", "json"]
        if self.started:
            c += ["--session", self.session_id]
        if self.model:
            c += ["-m", self.model]
        c += mode_args + self.extra_args
        # системного промпта у `opencode run` нет — правила идут в начало первого сообщения сессии
        if rules and not self.started:
            prompt = f"<rules>\n{rules}\n</rules>\n\n{prompt}"
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(self._opencode_config(mode), ensure_ascii=False)}
        if len(prompt) > ARG_LIMIT and self.log_dir:
            c += ["--file", str(self._prompt_file(prompt)), "Задание этого хода — в приложенном файле. Выполни его."]
        else:
            c += [prompt]
        return c, None, env

    def _opencode_config(self, mode: str) -> dict:
        """Права OpenCode: всё, что не разрешено явно, запрещено (без «ask», которому в run некому ответить)."""
        key = "exec_permission" if mode == "exec" else "debate_permission"
        perm = copy.deepcopy(self.options.get(key) or {})
        bash = {"*": "deny"}
        if isinstance(perm.get("bash"), dict):
            bash.update(perm["bash"])
        elif isinstance(perm.get("bash"), str):
            bash["*"] = perm["bash"]
        allow = READONLY_BASH + self._extra(mode)
        for p in allow:
            bp = bash_prefix(p)
            if bp:
                bash[f"{bp[0]}*" if bp[1] else bp[0]] = "allow"
            elif p == "WebFetch":
                perm["webfetch"] = "allow"
            elif p in ("Edit", "Write"):
                perm["edit"] = "allow"
        perm["bash"] = bash
        # свои агенты: права встроенных plan/build (bash: ask) перекрыли бы глобальные
        agents = {"debate": {"mode": "primary", "description": "Обсуждение: только чтение", "permission": perm},
                  "debate-exec": {"mode": "primary", "description": "Исполнение плана", "permission": perm}}
        return {"$schema": "https://opencode.ai/config.json", "permission": perm, "agent": agents}

    def _prompt_file(self, prompt: str) -> Path:
        """Сохраняет длинный промпт в raw/ — для CLI, которые умеют читать задание из файла."""
        pf = self.log_dir / f"{self.role}-{self.calls + 1:03d}.prompt.txt"
        pf.write_text(prompt, encoding="utf-8")
        return pf

    # ---------- вызов ----------
    def ask(self, prompt: str, *, cwd: Path, rules: str = "", mode: str = "debate") -> TurnResult:
        """Один ход агента в каталоге `cwd`. Бросает AgentError при ошибке, KeyboardInterrupt — при Ctrl+C."""
        argv, stdin, extra_env = self.build(prompt, cwd, rules, mode)
        self.calls += 1
        n = self.calls
        t0 = time.monotonic()
        # PWD обязательно: OpenCode (и, возможно, другие CLI на Node/Bun) определяют каталог проекта по $PWD,
        # а не по фактическому cwd процесса — без этого агент работал бы в каталоге, откуда запущен debate
        env = {**os.environ, "PWD": str(cwd), **extra_env}
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), text=True, encoding="utf-8", errors="replace", env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,  # Ctrl+C обрабатываем сами и гасим всю группу
            )
        except FileNotFoundError:
            raise AgentError(f"{self.name}: команда {self.cmd!r} не найдена (not found)")
        try:
            out, err = proc.communicate(stdin, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise AgentError(f"{self.name}: превышен таймаут {self.timeout_s // 60} мин")
        except KeyboardInterrupt:
            _kill(proc)
            raise
        dt = time.monotonic() - t0
        self._log(n, prompt, argv, out, err, extra_env)
        if proc.returncode != 0:
            parsed = parse_output(out, self.kind) if out and out.strip()[:1] in "{[" else None
            if parsed and parsed.text and parsed.text != parsed.raw:
                msg = parsed.text.strip()[:1500]
            else:
                msg = (err or out or "").strip()[-1500:]
            raise AgentError(f"{self.name} завершился с кодом {proc.returncode}: {msg}")
        res = parse_output(out, self.kind)
        res.duration_s = dt
        res.stderr = (err or "").strip()
        res.denials += parse_stderr_denials(res.stderr)
        if res.session_id and res.session_id != self.session_id:
            self.session_id = res.session_id
        self.started = True
        if res.is_error:
            raise AgentError(f"{self.name} вернул ошибку: {res.text[:1500]}")
        return res

    def _log(self, n: int, prompt: str, argv: list[str], out: str, err: str, env: dict | None = None) -> None:
        """Пишет в raw/ промпт (с командной строкой в комментарии), сырой ответ и stderr.

        Внимание: сюда попадает всё, что пользователь передал агентам, — включая пароли, если их написали в чат.
        """
        if not self.log_dir:
            return
        base = self.log_dir / f"{self.role}-{n:03d}-{self.name}"
        shown = [a if len(a) < 300 else a[:300] + "…" for a in argv]
        head = f"<!-- {json.dumps(shown, ensure_ascii=False)} -->\n"
        if env:
            head += f"<!-- env: {json.dumps(env, ensure_ascii=False)} -->\n"
        base.with_suffix(".prompt.md").write_text(f"{head}\n{prompt}", encoding="utf-8")
        base.with_suffix(".out.json").write_text(out or "", encoding="utf-8")
        if err and err.strip():
            base.with_suffix(".stderr.txt").write_text(err, encoding="utf-8")


def _kill(proc: subprocess.Popen) -> None:
    """Останавливает агента вместе с дочерними процессами (процесс запущен в своей группе)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


# ---------- разбор вывода ----------
# Форматы JSON у CLI разные и не всегда задокументированы, поэтому разбор намеренно гибкий: ищем знакомые ключи
# на любом уровне вложенности. Для OpenCode (поток событий) — отдельный разборщик.
TEXT_KEYS = ("result", "response", "text", "content", "message", "output", "answer")
SID_KEYS = ("session_id", "sessionId", "sessionID", "session")
IN_KEYS = ("input_tokens", "inputTokens", "prompt_tokens")
CACHE_KEYS = (("cache_read_input_tokens",), ("cache_creation_input_tokens",), ("cachedInputTokens", "cached_tokens"))
OUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens")


def parse_output(out: str, kind: str = "") -> TurnResult:
    """Приводит вывод CLI к TurnResult.

    Claude Code и Grok отдают один JSON-объект, Qwen Code — массив сообщений с итоговым {"type": "result"}.
    Отказы в правах Claude Code перечисляет в `permission_denials`; у Grok они приходят через stderr.
    """
    if kind == "opencode":
        return parse_opencode(out)
    raw = out or ""
    s = raw.strip()
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        # возможно, несколько JSON-строк (стриминг) — берём последнюю разбираемую
        for line in reversed(s.splitlines()):
            try:
                obj = json.loads(line)
                break
            except Exception:
                continue
    if obj is None:
        return TurnResult(text=raw, raw=raw, session_id=None, tokens_in=None, tokens_out=None, duration_s=0)
    # Qwen Code: массив сообщений, итог — в последнем {"type": "result"}
    result = obj
    if isinstance(obj, list):
        results = [x for x in obj if isinstance(x, dict) and x.get("type") == "result"]
        result = results[-1] if results else obj
    text = _find_text(result) or _find_text(obj) or ""
    sid = _find_key(result, SID_KEYS) or _find_key(obj, SID_KEYS)
    tin = _find_key(result, IN_KEYS)
    tout = _find_key(result, OUT_KEYS)
    if isinstance(tin, int):
        for keys in CACHE_KEYS:
            c = _find_key(result, keys)
            if isinstance(c, int):
                tin += c
    is_err = bool(isinstance(result, dict) and (result.get("is_error") is True or result.get("subtype") == "error"))
    stop = _find_key(result, ("stopReason", "stop_reason", "terminal_reason"))
    denials = []
    for d in (result.get("permission_denials") if isinstance(result, dict) else None) or []:
        if not isinstance(d, dict):
            continue
        inp = d.get("tool_input") or {}
        cmd = inp.get("command") or inp.get("file_path") or inp.get("url") or json.dumps(inp, ensure_ascii=False)[:300]
        denials.append({"tool": _norm_tool(str(d.get("tool_name", "?"))), "cmd": str(cmd), "truncated": False})
    return TurnResult(text=text if text else raw, raw=raw,
                      session_id=sid if isinstance(sid, str) else None,
                      tokens_in=tin if isinstance(tin, int) else None,
                      tokens_out=tout if isinstance(tout, int) else None,
                      duration_s=0, is_error=is_err, stop=str(stop or ""), denials=denials)


_DENY_WORDS = re.compile(r"denied|not allowed|permission|rejected|запрещ", re.I)


def parse_opencode(out: str) -> TurnResult:
    """`opencode run --format json`: по JSON-событию на строку. Текст — в событиях type=text (part.text)."""
    raw = out or ""
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    texts, denials = [], []
    sid, tin, tout, err_msg = None, 0, 0, ""
    has_tokens = False
    for ev in events:
        sid = sid or ev.get("sessionID") or ev.get("sessionId") or ev.get("session_id")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        typ = ev.get("type", "")
        if typ == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else None
        if tok:
            has_tokens = True
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            tin += int(tok.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
            tout += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if state.get("status") == "error" and _DENY_WORDS.search(str(state.get("error", ""))):
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            cmd = inp.get("command") or inp.get("filePath") or inp.get("url") or json.dumps(inp, ensure_ascii=False)[:300]
            denials.append({"tool": _norm_tool(str(part.get("tool", "?"))), "cmd": str(cmd), "truncated": False})
        if typ == "error":
            e = ev.get("error")
            err_msg = (e.get("data", {}).get("message") or e.get("message") or json.dumps(e, ensure_ascii=False)
                       if isinstance(e, dict) else str(e or ev))
    text = "\n".join(texts)
    return TurnResult(text=text or err_msg or raw, raw=raw, session_id=sid if isinstance(sid, str) else None,
                      tokens_in=tin if has_tokens else None, tokens_out=tout if has_tokens else None,
                      duration_s=0, is_error=bool(err_msg and not texts), denials=denials)


_STDERR_DENIAL = re.compile(r"permission required: (\w+) `(.*?)` was not run", re.S)
_TOOL_MAP = {"Execute": "Bash", "Write": "Write", "Edit": "Edit", "Fetch": "WebFetch",
             "bash": "Bash", "run_shell_command": "Bash", "shell": "Bash", "edit": "Edit", "write": "Write",
             "write_file": "Write", "replace": "Edit", "webfetch": "WebFetch", "web_fetch": "WebFetch"}


def _norm_tool(name: str) -> str:
    """Имя инструмента конкретного CLI → имя в терминах Claude Code (Bash, Edit, Write, WebFetch…)."""
    return _TOOL_MAP.get(name, name)


def parse_stderr_denials(stderr: str) -> list[dict]:
    """Grok пишет в stderr: permission required: Execute `<команда>` was not run, as a headless run …"""
    out = []
    for tool, cmd in _STDERR_DENIAL.findall(stderr or ""):
        cmd = cmd.strip()
        trunc = cmd.endswith("…")
        out.append({"tool": _norm_tool(tool), "cmd": cmd.rstrip("…").strip(), "truncated": trunc})
    return out


def _find_text(o) -> str | None:
    """Текст ответа: первое непустое строковое поле из TEXT_KEYS, при необходимости — вглубь структуры."""
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        for k in TEXT_KEYS:
            v = o.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for k in TEXT_KEYS:
            if k in o:
                t = _find_text(o[k])
                if t:
                    return t
        return None
    if isinstance(o, list):
        # список сообщений: последний ответ ассистента
        for item in reversed(o):
            if isinstance(item, dict) and item.get("role") not in (None, "assistant"):
                continue
            t = _find_text(item)
            if t:
                return t
        texts = [x.get("text") for x in o if isinstance(x, dict) and isinstance(x.get("text"), str)]
        return "\n".join(texts) if texts else None
    return None


def _find_key(o, keys):
    """Первое скалярное значение любого из `keys` на любой глубине (для списков — с конца)."""
    if isinstance(o, dict):
        for k in keys:
            if k in o and not isinstance(o[k], (dict, list)):
                return o[k]
        for v in o.values():
            r = _find_key(v, keys)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in reversed(o):
            r = _find_key(v, keys)
            if r is not None:
                return r
    return None


# ---------- список моделей ----------
MODELS_CMD = {"grok": ["models"], "opencode": ["models"]}


def models_command(agent_cfg: dict) -> list[str] | None:
    """Команда, выводящая список моделей агента, или None, если CLI такого не умеет."""
    kind = agent_cfg.get("kind")
    opt = (agent_cfg.get("options") or {}).get("models_cmd")
    if opt:
        return [agent_cfg.get("cmd", kind), *opt]
    if kind in MODELS_CMD:
        return [agent_cfg.get("cmd", kind), *MODELS_CMD[kind]]
    return None
