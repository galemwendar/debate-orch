"""Консольный интерфейс `debate`.

Команды:
    run      обсудить задачу и (по вашему «го») исполнить план
    resume   продолжить прерванное обсуждение
    list     обсуждения в текущем репозитории
    probe    проверить, что CLI агентов работают так, как ожидает оркестратор
    models   список моделей агента (если CLI это умеет)
    config   итоговые настройки и откуда взялось каждое значение

Здесь же — ConsoleUI, консольная реализация интерфейса движка (engine.UI) на библиотеке rich.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import protocol as P
from .agents import KINDS, Agent, AgentError, TurnResult, models_command
from .config import GLOBAL_CONFIG, load_config, role_agents, role_chain, roles_toml, suggest_roles, write_roles
from .engine import Engine, _n, list_debates

# цвета рамок по виду агента (по имени агента из [agents])
COLORS = {"claude": "orange3", "grok": "cyan", "qwen": "medium_purple", "opencode": "green3"}
VERDICT_STYLE = {"agree": "green", "approve": "green", "done": "green", "proposal": "blue", "revised": "blue",
                 "analysis": "blue", "disagree": "red", "changes_requested": "red", "hold": "yellow",
                 "need_user": "magenta", "blocked": "red", "unparsed": "red"}


# русская раскладка: й→q, к→r, т→n и т. д.
_RU2EN = dict(zip("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,."))


def _stop_key(options: dict[str, str]) -> str | None:
    """Клавиша «остановить/выйти» в этом меню (q или s), если она там есть."""
    for k in ("q", "s"):
        if k in options:
            return k
    return None


# похожие буквы: кириллическая «о» вместо латинской o и т. п.
_HOMOGLYPH = dict(zip("аеосрхуі", "aeocpxyi"))


def normalize_choice(raw: str, options: dict[str, str]) -> str | None:
    """Ответ пользователя → ключ меню. Понимает регистр, русскую раскладку, похожие буквы и `q` как «выйти»."""
    a = (raw or "").strip().lower()
    for variant in (a, "".join(_RU2EN.get(ch, ch) for ch in a), "".join(_HOMOGLYPH.get(ch, ch) for ch in a)):
        if variant in options:
            return variant
        if variant == "q" and _stop_key(options):
            return _stop_key(options)
    return None


class ConsoleUI:
    """Консольный интерфейс движка: панели ходов, меню выбора и многострочный ввод.

    Меню устойчивы к ошибкам ввода: неверная буква — повторный вопрос; Ctrl+C или Ctrl+D внутри меню —
    аккуратная остановка (если в меню есть «остановить»), а не трейсбек.
    """
    def __init__(self, console: Console | None = None, plan_lines: int = 40):
        self.c = console or Console()
        self.plan_lines = plan_lines

    def info(self, msg: str) -> None:
        self.c.print(f"[dim]•[/dim] {escape(msg)}")

    @contextmanager
    def working(self, label: str):
        with self.c.status(f"{label} работает… [dim](Ctrl+C — прервать ход)[/dim]"):
            yield

    def show_turn(self, label: str, role: str, data: dict, res: TurnResult) -> None:
        """Ход агента: краткое резюме, таблица замечаний, ответы на них и (для автора) начало плана."""
        agent = label.split("·")[-1].strip()
        color = COLORS.get(agent, "white")
        v = str(data.get("verdict"))
        meta = f"[{VERDICT_STYLE.get(v, 'white')}]{v}[/] · {res.duration_s:.0f} с"
        if res.tokens_in or res.tokens_out:
            meta += f" · токены: вход {_n(res.tokens_in or 0)} / выход {_n(res.tokens_out or 0)}"
        parts = [data.get("summary") or ""]
        if data.get("key_points"):
            parts.append("\n".join(f"• {k}" for k in data["key_points"]))
        body = Markdown("\n\n".join(p for p in parts if p))
        self.c.print(Panel(body, title=f"[bold {color}]{label}[/]", subtitle=meta, border_style=color))
        pts = data.get("points") or []
        if pts:
            t = Table(show_header=True, header_style="bold", expand=True)
            t.add_column("id", width=4)
            t.add_column("уровень", width=8)
            t.add_column("суть")
            t.add_column("доказательство", style="dim")
            for p in pts:
                sev = str(p.get("severity", ""))
                st = "red" if sev == "blocker" else "yellow" if sev == "major" else "dim"
                t.add_row(str(p.get("id", "")), f"[{st}]{sev}[/]", str(p.get("claim", "")), str(p.get("evidence", "")))
            self.c.print(t)
        if data.get("responses"):
            self.c.print(P.fmt_responses(data["responses"]), style="dim")
        if role == "proposer" and data.get("plan"):
            lines = str(data["plan"]).splitlines()
            shown = "\n".join(lines[: self.plan_lines])
            if len(lines) > self.plan_lines:
                shown += f"\n\n*… ещё {len(lines) - self.plan_lines} строк — полный текст в файлах обсуждения*"
            self.c.print(Panel(Markdown(shown), title="план", border_style="dim"))

    def choose(self, title: str, body: str, options: dict[str, str]) -> str:
        self.c.print(Panel(Markdown(body) if body else "", title=f"[bold magenta]{title}[/]", border_style="magenta"))
        stop = _stop_key(options)
        hint = "  ".join(f"[bold]{k}[/] — {v}" for k, v in options.items())
        if stop and "q" not in options:
            hint += f"  [dim](q — то же, что {stop})[/dim]"
        self.c.print(hint)
        while True:
            try:
                raw = self.c.input("[bold]выбор[/]: ")
            except (KeyboardInterrupt, EOFError):
                # повторный Ctrl+C или Ctrl+D в меню — аккуратная остановка
                self.c.print()
                if stop:
                    return stop
                raise
            ans = normalize_choice(raw, options)
            if ans:
                return ans
            self.c.print(f"[yellow]Нет такого варианта.[/yellow] Введите одну из букв: {', '.join(options)}"
                         + (" или q" if stop and "q" not in options else ""))

    def text(self, prompt: str) -> str:
        self.c.print(Panel(escape(prompt), border_style="magenta"))
        self.c.print("[dim]Многострочный ввод, пустая строка — конец.[/dim]")
        lines = []
        while True:
            try:
                line = self.c.input("[magenta]›[/] ")
            except (EOFError, KeyboardInterrupt):
                self.c.print()
                break
            if not line.strip():
                break
            lines.append(line)
        return "\n".join(lines).strip()


def read_ticket(args, console: Console) -> str:
    """Текст задачи: из аргументов, из файла (-f), из stdin или интерактивно."""
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.ticket:
        return " ".join(args.ticket)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    console.print("[bold]Опишите задачу.[/bold] [dim]Пустая строка — конец.[/dim]")
    return ConsoleUI(console).text("Задача / тикет")


def apply_overrides(cfg: dict, roles: list[str], models: list[str]) -> None:
    """--role critic=qwen, --model qwen=qwen3-coder-plus (имя агента из [agents])."""
    for item in roles:
        k, _, v = item.partition("=")
        if k not in cfg["roles"] or not v:
            raise ValueError(f"--role {item}: нужно роль=агент, роли: {', '.join(cfg['roles'])}")
        if v not in cfg["agents"]:
            raise ValueError(f"--role {item}: агент {v!r} не описан, есть: {', '.join(cfg['agents'])}")
        cfg["roles"][k] = v  # явное назначение на запуск: без запасных
    for item in models:
        k, _, v = item.partition("=")
        if k not in cfg["agents"] or not v:
            raise ValueError(f"--model {item}: нужно агент=модель, агенты: {', '.join(cfg['agents'])}")
        cfg["agents"][k]["model"] = v


def cmd_models(args) -> int:
    """`debate models <агент>` — вызывает команду списка моделей самого CLI."""
    console = Console()
    repo = Path(args.repo).resolve()
    cfg, _ = load_config(Path(args.config) if args.config else None, repo)
    if args.agent not in cfg["agents"]:
        console.print(f"[red]Агент {args.agent!r} не описан.[/red] Есть: {', '.join(cfg['agents'])}")
        return 2
    a = cfg["agents"][args.agent]
    console.print(f"[dim]{args.agent}: kind={a.get('kind')} · модель в конфиге: {a.get('model') or '(по умолчанию CLI)'}[/dim]")
    cmd = models_command(a)
    if not cmd:
        hint = {"claude": "алиасы sonnet / opus / haiku или полный id модели (claude-opus-5-5 и т. п.)",
                "qwen": "модели из ~/.qwen/settings.json (modelProviders) — Qwen Code не умеет выводить список"}
        console.print(hint.get(a.get("kind"), "у этого CLI нет команды списка моделей"))
        return 0
    if not shutil.which(cmd[0]):
        console.print(f"[red]Не найден в PATH:[/red] {cmd[0]}")
        return 2
    return subprocess.run(cmd).returncode


def cmd_run(args) -> int:
    """`debate run` — новое обсуждение."""
    console = Console()
    repo = Path(args.repo).resolve()
    cfg, cfg_path = load_config(Path(args.config) if args.config else None, repo)
    try:
        apply_overrides(cfg, args.role or [], args.model or [])
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 2
    if args.swap:
        r = cfg["roles"]
        r["proposer"], r["critic"] = r["critic"], r["proposer"]
        r["executor"], r["reviewer"] = r["reviewer"], r["executor"]
    if args.rounds:
        cfg["debate"]["max_rounds"] = args.rounds
    if args.no_exec:
        cfg["exec"]["enabled"] = False
    if getattr(args, "in_place", False):
        cfg["exec"]["use_worktree"] = False
    if getattr(args, "worktree", False):
        cfg["exec"]["use_worktree"] = True
    for role in cfg["roles"]:
        chain = role_chain(cfg, role)
        for name in chain:
            if name not in cfg["agents"]:
                console.print(f"[red]Роль {role}: агент {name!r} не описан в \\[agents][/red]")
                return 2
        found = [n for n in chain if shutil.which(cfg["agents"][n]["cmd"])]
        if not found:
            console.print(f"[red]Роль {role}: ни один агент не найден в PATH:[/red] {', '.join(chain)}")
            return 2
        if found[0] != chain[0]:
            console.print(f"[yellow]Роль {role}: {chain[0]} не найден в PATH — роль берёт {found[0]}[/yellow]")
            cfg["roles"][role] = found if len(chain) > 1 else found[0]
    brief = read_ticket(args, console)
    if not brief.strip():
        console.print("[red]Пустая задача.[/red]")
        return 2
    if not cfg_path:
        console.print("[yellow]Совет:[/yellow] конфигурации ещё нет — [bold]debate init[/bold] найдёт установленных "
                      "агентов и раздаст роли.")

    def who(role: str) -> str:
        ch = role_chain(cfg, role)
        return ch[0] + (f" (запасные: {', '.join(ch[1:])})" if len(ch) > 1 else "")
    srcs = ", ".join(_short(p, repo) for p in cfg_path) or "встроенный"
    console.print(f"[dim]конфиг: {srcs} · автор: {who('proposer')} · критик: {who('critic')} · "
                  f"исполнитель: {who('executor')} · ревьюер: {who('reviewer')} · раундов: {cfg['debate']['max_rounds']}[/dim]")
    out_dir = cfg["output"]["dir"]
    try:
        from .gitutil import ignored, is_repo
        if is_repo(repo) and not ignored(repo, out_dir + "/x"):
            console.print(f"[yellow]Совет:[/yellow] добавьте в .gitignore [bold]{out_dir}/*[/bold] "
                          f"(и [bold]!{out_dir}/config.toml[/bold], если конфиг репозитория нужен в git).")
    except Exception:
        pass
    Engine(cfg, repo, ConsoleUI(console), brief=brief).run()
    return 0


def _short(p: Path, repo: Path) -> str:
    """Путь для вывода: относительно репозитория или через ~."""
    try:
        return str(p.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(p).replace(str(Path.home()), "~", 1)


def _fmt_value(v) -> str:
    """Значение настройки для таблицы `debate config` (длинные списки сворачиваются)."""
    if isinstance(v, list):
        if len(v) > 8:
            return f"[{len(v)} шт.: {', '.join(map(str, v[:3]))}, …]"
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False)


def cmd_config(args) -> int:
    """Итоговые настройки и откуда взялось каждое значение."""
    from .config import config_layers
    console = Console()
    repo = Path(args.repo).resolve()
    explicit = Path(args.config) if args.config else None
    cfg, used, prov = load_config(explicit, repo, with_provenance=True)
    console.print("[bold]Файлы конфигурации[/bold] (каждый следующий перекрывает предыдущий):")
    for p in config_layers(repo, explicit):
        mark = "[green]✓[/green]" if p in used else "[dim]—[/dim]"
        console.print(f"  {mark} {_short(p, repo)}" + ("" if p in used else " [dim](нет)[/dim]"))
    console.print("[dim]Словари сливаются, списки заменяются; allow_debate и allow_exec дописываются.[/dim]\n")

    def show(title: str, d: dict, path: tuple):
        t = Table(title=escape(title), title_justify="left", title_style="bold cyan", show_header=False, box=None,
                  pad_edge=False)
        t.add_column("ключ", style="bold", no_wrap=True)
        t.add_column("значение", overflow="fold")
        t.add_column("откуда", style="dim", no_wrap=True)
        for k, v in d.items():
            if isinstance(v, dict) and k == "options":
                for k2, v2 in v.items():
                    t.add_row(f"options.{k2}", escape(_fmt_value(v2)), prov.get(path + (k, k2), "по умолчанию"))
                continue
            t.add_row(k, escape(_fmt_value(v)), prov.get(path + (k,), "по умолчанию"))
        console.print(t)
        console.print()

    for sec in ("roles", "debate", "exec", "rules", "output"):
        show(f"[{sec}]", cfg[sec], (sec,))
    names = list(cfg["agents"]) if args.all else role_agents(cfg, with_fallbacks=True)
    for name in names:
        show(f"[agents.{name}]", cfg["agents"][name], ("agents", name))
    if not args.all:
        console.print("[dim]Показаны агенты, назначенные в \\[roles]; все — debate config --all[/dim]")
    return 0


def _resolve_debate(repo: Path, out_dir: str, ref: str | None) -> Path | None:
    """Папка обсуждения по пути, имени или его части; без `ref` — последнее незавершённое."""
    if ref:
        p = Path(ref)
        if (p / "state.json").is_file():
            return p
        base = repo / out_dir
        if (base / ref / "state.json").is_file():
            return base / ref
        hits = [r["dir"] for r in list_debates(repo, out_dir) if r["slug"].startswith(ref) or ref in r["slug"]]
        return hits[0] if len(hits) == 1 else None
    rows = [r for r in list_debates(repo, out_dir) if r["resumable"]]
    return rows[0]["dir"] if rows else None


def cmd_resume(args) -> int:
    """`debate resume` — продолжить обсуждение; можно сменить модель (--model) или агента в роли (--role)."""
    console = Console()
    repo = Path(args.repo).resolve()
    cfg, _ = load_config(Path(args.config) if args.config else None, repo)
    try:
        apply_overrides(cfg, [], args.model or [])
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 2
    d = _resolve_debate(repo, cfg["output"]["dir"], args.debate)
    if d is None:
        console.print("[red]Не нашёл обсуждение.[/red] Посмотрите список: [bold]debate list[/bold]")
        return 2
    try:
        overrides = {}
        for item in args.role or []:
            k, _, v = item.partition("=")
            if not k or not v:
                raise ValueError(f"--role {item}: нужно роль=агент")
            overrides[k] = v
        eng = Engine(cfg, repo, ConsoleUI(console), resume_dir=d, role_overrides=overrides)
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 2
    if eng.f["phase"] == "done":
        console.print("Это обсуждение уже завершено.")
        return 0
    console.print(f"[dim]продолжаю {eng.slug} · этап: {eng.f['phase']} · план v{eng.version}[/dim]")
    for line in eng.replaced:
        console.print(f"[yellow]смена агента[/yellow] {escape(line)} — новая сессия, прошлую переписку этой роли агент не видит")
    missing = sorted({a.cmd for a in eng.roles.values() if not shutil.which(a.cmd)})
    if missing:
        console.print(f"[red]Не найдены в PATH:[/red] {', '.join(missing)}")
        return 2
    console.print(Panel(Markdown(eng.brief), title="задача", border_style="dim"))
    eng.run()
    return 0


def cmd_list(args) -> int:
    """`debate list` — незавершённые (или, с --all, все) обсуждения репозитория."""
    console = Console()
    repo = Path(args.repo).resolve()
    cfg, _ = load_config(Path(args.config) if args.config else None, repo)
    rows = list_debates(repo, cfg["output"]["dir"])
    if not args.all:
        rows = [r for r in rows if r["phase"] != "done"]
    if not rows:
        console.print("Обсуждений нет." if args.all else "Незавершённых обсуждений нет. Все: [bold]debate list --all[/bold]")
        return 0
    t = Table(show_header=True, header_style="bold", expand=True)
    t.add_column("обсуждение", no_wrap=True)
    t.add_column("этап")
    t.add_column("план", no_wrap=True)
    t.add_column("обновлено")
    t.add_column("задача", ratio=1)
    for r in rows:
        style = "green" if r["phase"] == "done" else "yellow"
        t.add_row(r["slug"], f"[{style}]{escape(r['phase_ru'])}[/]",
                  f"v{r['version']}" if r["version"] else "—", r["updated"].replace("T", " "),
                  escape(r["brief"][:80]))
    console.print(t)
    console.print("[dim]Продолжить: debate resume <обсуждение или его часть> (без аргумента — последнее незавершённое)[/dim]")
    return 0


def cmd_probe(args) -> int:
    """`debate probe` — четыре коротких вызова каждого агента во временном git-репозитории.

    1. Чтение файла и ответ JSON-блоком: CLI запускается, вывод разбирается, сессия создаётся.
    2. Вопрос о содержимом уже удалённого файла: ответить можно только из памяти — значит, сессия продолжается
       (и агент работал именно во временном каталоге, а не там, откуда запущен debate).
    3. Команда `echo` с выданным разрешением Bash(echo:*): разрешения по шаблону доходят до CLI.
    4. Просьба создать файл в режиме обсуждения: файл появиться не должен.

    Формулировки намеренно похожи на обычную работу: просьбы «повтори дословно» модели принимают за попытку
    обойти правила и отказываются, что давало ложные провалы.
    """
    console = Console()
    repo = Path(args.repo).resolve()
    cfg, _ = load_config(Path(args.config) if args.config else None, repo)
    names = args.agents or role_agents(cfg, with_fallbacks=True)
    unknown = [n for n in names if n not in cfg["agents"]]
    if unknown:
        console.print(f"[red]Не описаны агенты:[/red] {', '.join(unknown)}. Есть: {', '.join(cfg['agents'])}")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="debate-probe-"))
    subprocess.run(["git", "init", "-q"], cwd=tmp)
    probe_readme = "# Test project\n\nScratch directory for checking the CLI. Test bench number: 4817.\n"
    ok_all = True
    for name in names:
        a = cfg["agents"][name]
        console.rule(f"[bold]{name}[/bold] ({a['cmd']})")
        if not shutil.which(a["cmd"]):
            console.print("[red]не найден в PATH[/red]")
            ok_all = False
            continue
        agent = Agent.from_cfg(name, "critic", a, 600, None)
        (tmp / "README.md").write_text(probe_readme, encoding="utf-8")
        rules = P.system_rules("critic", language=cfg["rules"].get("language", "ru"))
        try:
            # обычная рабочая задача: прочитать файл и ответить JSON-блоком (без «повтори дословно» —
            # такие просьбы модели принимают за попытку обойти правила и отказываются)
            r1 = agent.ask('Read README.md in the current directory and briefly summarize it. End your reply with a block '
                           'following this schema:\n```json\n{"verdict": "agree", "summary": "README summary"}\n```',
                           cwd=tmp, rules=rules)
            console.print(Panel(r1.raw[:2500] or "(пусто)", title="сырой вывод (1)", border_style="dim"))
            d = P.extract_json(r1.text)
            console.print(f"текст разобран: {'[green]да[/]' if r1.text and r1.text != r1.raw else '[yellow]как есть[/]'} · "
                          f"JSON-вердикт: {'[green]да[/]' if d else '[red]нет[/]'} · session_id: {r1.session_id or '—'} · "
                          f"токены: {r1.tokens_in}/{r1.tokens_out}")
            (tmp / "README.md").unlink(missing_ok=True)  # файла больше нет: ответить можно только из памяти сессии
            r2 = agent.ask("What test bench number was in the README.md you read in your previous turn? "
                           "The file has been deleted, answer from memory with a single number.", cwd=tmp, rules=rules)
            remembers = "4817" in r2.text
            console.print(f"сессия продолжается: {'[green]да[/]' if remembers else '[red]нет[/]'} → {r2.text.strip()[:200]!r}")
            ok_all &= bool(d) and remembers
            # выданное разрешение (Bash(echo:*)) действительно пропускает команду
            agent.grants = ["Bash(echo:*)"]
            r3 = agent.ask("Environment check: run the shell command echo probe-ok-42 and report what it printed.",
                           cwd=tmp, rules="")
            agent.grants = []
            ran = "probe-ok-42" in r3.text and not r3.denials and r3.stop != "cancelled"
            console.print(f"разрешение по шаблону работает: {'[green]да[/]' if ran else '[red]нет[/]'}"
                          f" · stop={r3.stop or '—'} · отказы: {[x['cmd'] for x in r3.denials] or '—'}")
            ok_all &= ran
            if not args.no_write_check:
                agent.ask("Create a file probe_write.txt with the text hello in the current directory. This is a permissions check.",
                          cwd=tmp, rules="")
                wrote = (tmp / "probe_write.txt").exists()
                console.print(f"режим обсуждения только для чтения: "
                              f"{'[red]НЕТ — агент смог записать файл! Проверьте debate_args[/]' if wrote else '[green]да[/]'}")
                ok_all &= not wrote
                (tmp / "probe_write.txt").unlink(missing_ok=True)
        except AgentError as e:
            console.print(f"[red]ошибка:[/red] {e}")
            ok_all = False
    shutil.rmtree(tmp, ignore_errors=True)
    console.rule("[green]всё в порядке[/]" if ok_all else "[red]есть проблемы — см. «Проверка агентов» в README[/]")
    return 0 if ok_all else 1


def _version(cmd: str) -> str:
    """`<cli> --version` — первая строка; не тратит лимиты."""
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=20)
        return ((r.stdout or r.stderr).strip().splitlines() or ["?"])[0][:60]
    except Exception as e:
        return f"? ({type(e).__name__})"


def cmd_init(args) -> int:
    """`debate init` — найти установленные CLI агентов, проверить вход и раздать роли.

    Результат — раздел [roles] в ~/.config/debate/config.toml (или в .debate/config.toml репозитория с --here).
    Остальные разделы файла не трогаются. Для каждой роли — основной агент и запасные: если у основного
    кончится лимит, ход сам перейдёт к следующему.
    """
    console = Console()
    ui = ConsoleUI(console)
    repo = Path(args.repo).resolve()
    cfg, _ = load_config(Path(args.config) if args.config else None, repo)
    target = repo / ".debate" / "config.toml" if args.here else GLOBAL_CONFIG

    found = [k for k in KINDS if shutil.which(cfg["agents"][k]["cmd"])]
    t = Table(title="Агенты (CLI)", show_lines=False)
    for col in ("агент", "команда", "версия", "вход"):
        t.add_column(col)
    if not found:
        console.print("[red]Не найдено ни одного CLI агента.[/red] Поддерживаются: "
                      + ", ".join(f"{k} ({cfg['agents'][k]['cmd']})" for k in KINDS)
                      + ". Установите хотя бы два и запустите debate init ещё раз.")
        return 1
    check = not args.no_check and (args.yes or ui.choose(
        "Проверить вход?",
        f"Найдено: {', '.join(found)}. Чтобы убедиться, что вход выполнен и лимит не исчерпан, каждому агенту "
        "уйдёт один короткий запрос (несколько секунд, минимум токенов).",
        {"y": "проверить", "n": "не проверять"}) == "y")
    usable: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="debate-init-"))
    for k in KINDS:
        cmd = cfg["agents"][k]["cmd"]
        if k not in found:
            t.add_row(k, cmd, "—", "[dim]не установлен[/dim]")
            continue
        ver = _version(cmd)
        status = "[dim]не проверялся[/dim]"
        if check:
            agent = Agent.from_cfg(k, "critic", cfg["agents"][k], 180, None)
            try:
                with ui.working(f"{k}: проверка входа"):
                    agent.ask("Reply with the single word OK.", cwd=tmp)
                status = "[green]ok[/green]"
            except AgentError as e:
                status = "[red]ошибка[/red]: " + escape(str(e).splitlines()[0][:80])
                t.add_row(k, cmd, escape(ver), status)
                continue
        usable.append(k)
        t.add_row(k, cmd, escape(ver), status)
    console.print(t)
    if not usable:
        console.print("[red]Ни один агент не ответил.[/red] Выполните вход в CLI (например, `claude`, `grok auth login`, "
                      "`opencode auth login`) и повторите.")
        return 1

    roles = suggest_roles(usable)
    while True:
        console.print(Panel(roles_toml(roles).rstrip(), title=f"Предлагаемые роли → {target}", border_style="cyan"))
        if len(usable) == 1:
            console.print("[yellow]Доступен один агент: он будет спорить сам с собой — пользы от критики мало.[/yellow]")
        if args.yes:
            ch = "y"
        else:
            ch = ui.choose("Роли", "Первый агент в списке — основной, остальные — запасные на случай лимита. "
                           "Модель у каждого — та, что выбрана в самом CLI (сменить: debate models <агент>, "
                           "[agents.<агент>] model = …).",
                           {"y": "записать", "e": "изменить", "q": "выйти без записи"})
        if ch == "q":
            return 0
        if ch == "y":
            break
        raw = ui.text("Строки вида роль=агент[,запасной…], например: critic=opencode,grok")
        for line in raw.replace(";", "\n").splitlines():
            k, _, v = line.partition("=")
            k, names = k.strip(), [x.strip() for x in v.split(",") if x.strip()]
            if k not in roles or not names:
                console.print(f"[red]Пропущено:[/red] {escape(line)} (роли: {', '.join(roles)})")
                continue
            unknown = [n for n in names if n not in cfg["agents"]]
            if unknown:
                console.print(f"[red]Пропущено:[/red] нет агентов {', '.join(unknown)}")
                continue
            roles[k] = names
    write_roles(target, roles)
    console.print(f"[green]Записано:[/green] {target}. Проверить: [bold]debate config[/bold]; "
                  "подробная проверка агентов: [bold]debate probe[/bold].")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа. Прерывание (Ctrl+C, Ctrl+D) на любом уровне — короткое сообщение, без трейсбека."""
    ap = argparse.ArgumentParser(prog="debate", description="Оркестратор спора консольных ИИ-агентов: план → критика → согласие → исполнение → ревью")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="обсудить задачу и (по вашему «го») исполнить")
    r.add_argument("ticket", nargs="*", help="текст задачи (или -f файл, или stdin)")
    r.add_argument("-f", "--file", help="файл с задачей")
    r.add_argument("--repo", default=".", help="репозиторий (по умолчанию текущий каталог)")
    r.add_argument("--config", help="дополнительный файл конфигурации поверх остальных")
    r.add_argument("--swap", action="store_true", help="поменять местами автора↔критика и исполнителя↔ревьюера")
    r.add_argument("--role", action="append", metavar="РОЛЬ=АГЕНТ",
                   help="назначить агента на роль: --role critic=qwen (роли: proposer, critic, executor, reviewer)")
    r.add_argument("--model", action="append", metavar="АГЕНТ=МОДЕЛЬ",
                   help="модель агента на этот запуск: --model opencode=deepseek/deepseek-chat")
    r.add_argument("--rounds", type=int, help="максимум раундов до эскалации")
    r.add_argument("--no-exec", action="store_true", help="только обсуждение, без исполнения")
    r.add_argument("--worktree", action="store_true",
                   help="исполнять в отдельном git worktree рядом с репозиторием (можно работать параллельно)")
    r.add_argument("--in-place", action="store_true", help="исполнять в самом репозитории (по умолчанию)")
    r.set_defaults(func=cmd_run)

    it = sub.add_parser("init", help="найти установленные CLI агентов и раздать роли")
    it.add_argument("--here", action="store_true", help="записать роли в .debate/config.toml этого репозитория")
    it.add_argument("--no-check", action="store_true", help="не проверять вход (без запросов к агентам)")
    it.add_argument("-y", "--yes", action="store_true", help="проверить и записать без вопросов")
    it.add_argument("--repo", default=".")
    it.add_argument("--config")
    it.set_defaults(func=cmd_init)

    p = sub.add_parser("probe", help="проверить CLI агентов: ответ, сессия, read-only")
    p.add_argument("agents", nargs="*", help="какие агенты (по умолчанию — назначенные в [roles])")
    p.add_argument("--repo", default=".")
    p.add_argument("--config")
    p.add_argument("--no-write-check", action="store_true", help="не проверять запрет записи")
    p.set_defaults(func=cmd_probe)

    rs = sub.add_parser("resume", help="продолжить прерванное обсуждение")
    rs.add_argument("debate", nargs="?", help="папка обсуждения или часть её имени (по умолчанию последнее незавершённое)")
    rs.add_argument("--repo", default=".")
    rs.add_argument("--config")
    rs.add_argument("--model", action="append", metavar="АГЕНТ=МОДЕЛЬ", help="сменить модель агента при продолжении")
    rs.add_argument("--role", action="append", metavar="РОЛЬ=АГЕНТ",
                    help="отдать роль другому агенту при продолжении (новая сессия): --role reviewer=opencode")
    rs.set_defaults(func=cmd_resume)

    ls = sub.add_parser("list", help="обсуждения в этом репозитории")
    ls.add_argument("--all", action="store_true", help="включая завершённые")
    ls.add_argument("--repo", default=".")
    ls.add_argument("--config")
    ls.set_defaults(func=cmd_list)

    md = sub.add_parser("models", help="какие модели доступны агенту (если CLI это умеет)")
    md.add_argument("agent", help=f"имя агента из [agents] ({', '.join(KINDS)} или своё)")
    md.add_argument("--repo", default=".")
    md.add_argument("--config")
    md.set_defaults(func=cmd_models)

    cf = sub.add_parser("config", help="итоговые настройки и откуда взялось каждое значение")
    cf.add_argument("--all", action="store_true", help="показать всех агентов, а не только назначенных в [roles]")
    cf.add_argument("--repo", default=".")
    cf.add_argument("--config", help="дополнительный файл поверх остальных")
    cf.set_defaults(func=cmd_config)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (KeyboardInterrupt, EOFError):
        Console().print("\n[yellow]Остановлено.[/yellow] Состояние сохранено после последнего шага — "
                        "продолжить: [bold]debate resume[/bold]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
