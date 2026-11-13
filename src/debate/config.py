"""Конфигурация оркестратора.

Настройки собираются слоями, каждый следующий перекрывает предыдущий:

    встроенные значения (DEFAULTS)
      → ~/.config/debate/config.toml        общие предпочтения пользователя
      → <репо>/debate.toml                  старое место, читается для совместимости
      → <репо>/.debate/config.toml          настройки конкретного репозитория
      → --config <файл>                     разовый файл поверх всего
      → флаги --role / --model              применяются в cli.py

Итоговые значения и источник каждого показывает `debate config`.
"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------------------------------------------
# Права «только чтение»
# ---------------------------------------------------------------------------------------------------------------
# Единый формат шаблонов во всём проекте — синтаксис Claude Code: `Bash(<префикс команды>:*)`, `Bash(<команда>)`,
# имена инструментов (`Read`, `WebFetch`…). Адаптеры других CLI переводят его в свой синтаксис (см. agents.py).
#
# В неинтерактивном режиме всё, что не разрешено явно, отклоняется. Сознательно НЕ включены команды, которые
# умеют писать или удалять файлы, даже если обычно их используют для чтения: find (-delete, -exec), sort -o,
# tree -o, git branch -D. Для поиска файлов у агентов есть собственные инструменты (Glob/Grep и аналоги).
CLAUDE_READONLY_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
    "Bash(git ls-files:*)", "Bash(git rev-parse:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(grep:*)", "Bash(rg:*)",
    "Bash(pdfinfo:*)", "Bash(pdffonts:*)", "Bash(file:*)", "Bash(stat:*)", "Bash(du:*)",
    "Bash(jq:*)", "Bash(diff:*)", "Bash(which:*)", "Bash(pwd)",
    "Bash(realpath:*)", "Bash(readlink:*)", "Bash(basename:*)", "Bash(dirname:*)",
]

# Только shell-шаблоны — их понимают все адаптеры.
READONLY_BASH = [p for p in CLAUDE_READONLY_TOOLS if p.startswith("Bash(")]

# Grok Build принимает шаблоны Claude Code через повторяемый флаг --allow (проверяется `debate probe`).
GROK_READONLY_ALLOW = [arg for p in READONLY_BASH for arg in ("--allow", p)]

# ---------------------------------------------------------------------------------------------------------------
# Значения по умолчанию
# ---------------------------------------------------------------------------------------------------------------
# Ключи агента:
#   kind          вид CLI: claude | grok | qwen | opencode (определяет, как строится командная строка);
#   cmd           исполняемый файл;
#   model         модель; пусто — флаг модели не передаётся и CLI берёт свою модель по умолчанию;
#   debate_args   аргументы на этапах обсуждения и ревью (только чтение);
#   exec_args     аргументы на этапе исполнения (правки файлов разрешены);
#   allow_debate  дополнительные разрешения к стандартным — ДОПИСЫВАЮТСЯ из всех слоёв конфигурации;
#   allow_exec    то же для этапа исполнения;
#   extra_args    произвольные аргументы, добавляются в конец командной строки;
#   options       настройки конкретного адаптера.
DEFAULTS: dict = {
    "agents": {
        "claude": {
            "kind": "claude",
            "cmd": "claude",
            "model": "",
            # обсуждение и ревью: явный список разрешённого плюс явный запрет правок
            "debate_args": ["--allowedTools", *CLAUDE_READONLY_TOOLS,
                            "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit"],
            # исполнение: правки без подтверждения и команды чтения; остальной shell — по разрешению пользователя
            "exec_args": ["--permission-mode", "acceptEdits", "--allowedTools", *CLAUDE_READONLY_TOOLS],
            "extra_args": [],
            "allow_debate": [],
            "allow_exec": [],
        },
        "grok": {
            "kind": "grok",
            "cmd": "grok",
            "model": "",
            "debate_args": ["--permission-mode", "plan", *GROK_READONLY_ALLOW],
            "exec_args": ["--permission-mode", "acceptEdits", *GROK_READONLY_ALLOW],
            "extra_args": [],
            "allow_debate": [],
            "allow_exec": [],
        },
        # Qwen Code: модель — любая из настроенных в ~/.qwen/settings.json (API-ключ, Coding Plan, локальная).
        # Команды чтения адаптер добавляет сам; в allow_* — только ваши добавки.
        "qwen": {
            "kind": "qwen",
            "cmd": "qwen",
            "model": "",
            "debate_args": ["--approval-mode", "plan"],
            "exec_args": ["--approval-mode", "auto-edit"],
            "allow_debate": [],
            "allow_exec": [],
            "extra_args": [],
            # как передаются разрешения: --allowed-tools run_shell_command(<префикс команды>)
            "options": {"grant_flag": "--allowed-tools", "grant_format": "run_shell_command({cmd})"},
        },
        # OpenCode: модель в виде provider/model (список — `debate models opencode`).
        # Права передаются конфигом через OPENCODE_CONFIG_CONTENT: shell по умолчанию запрещён,
        # команды чтения и ваши добавки разрешаются поверх. Агенты `debate`/`debate-exec` создаются там же.
        "opencode": {
            "kind": "opencode",
            "cmd": "opencode",
            "model": "",
            "debate_args": ["--agent", "debate"],
            "exec_args": ["--agent", "debate-exec"],
            "allow_debate": [],
            "allow_exec": [],
            "extra_args": [],
            # права в формате opencode.json (ключ `permission`), без bash — его собирает адаптер
            "options": {"debate_permission": {"edit": "deny", "webfetch": "deny"},
                        "exec_permission": {"edit": "allow", "webfetch": "deny"}},
        },
    },
    # Кто какую роль играет. Значения — имена из [agents]; можно завести своих агентов:
    #   [agents.deepseek]
    #   kind = "opencode"
    #   model = "deepseek/deepseek-chat"
    "roles": {
        "proposer": "claude",   # автор плана
        "critic": "grok",       # критик
        "executor": "claude",   # исполнитель согласованного плана
        "reviewer": "grok",     # ревьюер диффа исполнителя
    },
    "debate": {
        "max_rounds": 4,          # раундов «критика → ответ» до эскалации пользователю
        "blind_first": True,      # критик сначала делает свой разбор, не видя плана (защита от поддакивания)
        "turn_timeout_min": 30,   # таймаут одного хода агента
        "max_question_loops": 3,  # сколько раз подряд агент может вернуть need_user
    },
    "exec": {
        "enabled": True,
        "use_worktree": True,     # исполнять в отдельном git worktree (ветка debate/<имя обсуждения>)
        "max_review_rounds": 2,   # циклов «ревью → правки» после первого исполнения
        "diff_char_limit": 60000, # дифф длиннее обрезается в промпте ревьюера
    },
    "rules": {
        "language": "ru",         # на каком языке агенты пишут планы, замечания и вопросы (ru, en, de… или название)
        "extra": "",              # дополнительные правила, добавляются всем агентам во всех ролях
    },
    "output": {"dir": ".debate"}, # каталог обсуждений внутри репозитория
}

# Списки по этим ключам при наложении слоёв ДОПИСЫВАЮТСЯ, а не заменяются: так в конфиге репозитория можно
# добавить одно право, не копируя стандартный список. Все остальные списки заменяются целиком — иначе было бы
# невозможно убрать что-то из значений по умолчанию.
ADDITIVE = {"allow_debate", "allow_exec"}

GLOBAL_CONFIG = Path.home() / ".config" / "debate" / "config.toml"


def deep_merge(base: dict, over: dict, src: str | None = None, prov: dict | None = None,
               path: tuple = ()) -> dict:
    """Накладывает `over` на копию `base`.

    Словари сливаются рекурсивно, списки заменяются (кроме ключей из ADDITIVE — они дописываются без повторов).
    Если передан `prov`, в него записывается, из какого источника `src` пришло каждое значение:
    ключ — путь вида ("agents", "claude", "model").
    """
    out = copy.deepcopy(base)
    for k, v in over.items():
        p = path + (k,)
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v, src, prov, p)
            continue
        if k in ADDITIVE and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(dict.fromkeys(out[k] + v))
        else:
            out[k] = copy.deepcopy(v)
        if prov is not None and src:
            _mark(prov, p, v, src, append=k in ADDITIVE)
    return out


def _mark(prov: dict, path: tuple, value, src: str, append: bool = False) -> None:
    """Отмечает источник значения; для дописываемых списков источники перечисляются через «+»."""
    if isinstance(value, dict):
        for k, v in value.items():
            _mark(prov, path + (k,), v, src)
    elif append and path in prov:
        prov[path] = f"{prov[path]} + {src}"
    else:
        prov[path] = src


def config_layers(repo: Path, explicit: Path | None = None) -> list[Path]:
    """Файлы конфигурации в порядке наложения. Отсутствующие файлы просто пропускаются."""
    layers = [GLOBAL_CONFIG,
              repo / "debate.toml",               # старое место, читается для совместимости
              repo / ".debate" / "config.toml"]   # конфиг репозитория
    if explicit:
        layers.append(explicit)
    return layers


def fill_agent_defaults(cfg: dict) -> dict:
    """Агент со своим именем (`[agents.deepseek] kind = "opencode"`) наследует настройки своего вида.

    Наследование идёт от уже собранной конфигурации вида, то есть с учётом правок из всех слоёв:
    если в общем конфиге задана модель для `opencode`, её получит и `deepseek`, пока не укажет свою.
    """
    for name, a in list(cfg["agents"].items()):
        kind = a.get("kind", name)
        if name in DEFAULTS["agents"] or kind not in cfg["agents"] or kind == name:
            continue
        cfg["agents"][name] = deep_merge(cfg["agents"][kind], a)
    return cfg


def load_config(path: Path | None, repo: Path, with_provenance: bool = False):
    """Собирает конфигурацию для репозитория `repo`.

    Возвращает (cfg, [использованные файлы]), а при with_provenance — ещё и словарь источников значений.
    Явно указанный файл (`--config`) обязан существовать; остальные слои необязательны.
    """
    if path and not path.is_file():
        raise FileNotFoundError(f"Конфиг не найден: {path}")
    cfg = copy.deepcopy(DEFAULTS)
    prov: dict = {}
    used: list[Path] = []
    for p in config_layers(repo, path):
        if not p.is_file():
            continue
        with p.open("rb") as f:
            data = tomllib.load(f)
        cfg = deep_merge(cfg, data, _label(p, repo), prov)
        used.append(p)
    cfg = fill_agent_defaults(cfg)
    return (cfg, used, prov) if with_provenance else (cfg, used)


def _label(p: Path, repo: Path) -> str:
    """Короткое имя файла для вывода: путь от корня репозитория или ~/… для общего конфига."""
    try:
        if p == GLOBAL_CONFIG:
            return "~/.config/debate/config.toml"
        return str(p.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(p)
