"""Полный цикл на фейковых агентах: python -m pytest tests (или python tests/test_flow.py)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from debate.config import DEFAULTS, deep_merge  # noqa: E402
from debate.engine import Engine, list_debates  # noqa: E402


class ScriptUI:
    def __init__(self, choices, texts):
        self.choices, self.texts, self.log = list(choices), list(texts), []

    def info(self, msg):
        self.log.append(("info", msg))

    def working(self, label):
        return nullcontext()

    def show_turn(self, label, role, data, res):
        self.log.append(("turn", label, data.get("verdict")))

    def choose(self, title, body, options):
        ch = self.choices.pop(0)
        assert ch in options, (title, ch, options)
        self.log.append(("choose", title, ch))
        return ch

    def text(self, prompt):
        t = self.texts.pop(0) if self.texts else ""
        self.log.append(("text", prompt[:60], t))
        return t


def make_wrapper(d: Path, kind: str, state: Path) -> str:
    w = d / f"fake-{kind}"
    # обёртка на Python, а не на sh: sh при старте переписывает $PWD на фактический cwd и скрыл бы ошибки с $PWD
    w.write_text(f"#!{sys.executable}\nimport os, sys\n"
                 f"os.environ.update(FAKE_KIND={kind!r}, FAKE_STATE={str(state)!r})\n"
                 f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'tests' / 'fake_agent.py')!r}, *sys.argv[1:]])\n")
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)


def setup(tmp: Path):
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi\n")
    (repo / ".gitignore").write_text(".debate/\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=repo, check=True)
    state = tmp / "state"
    cfg = deep_merge(DEFAULTS, {"agents": {"claude": {"cmd": make_wrapper(tmp, "claude", state)},
                                           "grok": {"cmd": make_wrapper(tmp, "grok", state)}}})
    return repo, cfg


def test_full_cycle():
    os.environ["FAKE_BAD_JSON_FIRST"] = "1"
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        # тексты: ответ на вопрос критика; выборы: план → го, итог → ок
        cfg["exec"]["use_worktree"] = True
        ui = ScriptUI(choices=["g", "o"], texts=["Тяжёлый прогон нельзя"])
        e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
        e.run()
        turns = [x for x in ui.log if x[0] == "turn"]
        print(*ui.log, sep="\n")
        assert ("turn", "Критик · grok", "analysis") in turns
        assert ("turn", "Автор плана · claude", "proposal") in turns   # после fix_json
        assert ("turn", "Критик · grok", "disagree") in turns
        assert ("turn", "Автор плана · claude", "revised") in turns
        assert ("turn", "Критик · grok", "agree") in turns
        assert ("turn", "Ревьюер · grok", "changes_requested") in turns
        assert ("turn", "Ревьюер · grok", "approve") in turns
        plan = (e.dir / "plan.md").read_text()
        assert "План v2" in plan and "C2" in plan and "согласован" in plan
        wt = repo.parent / f"repo-wt-{e.slug}"
        assert (wt / "feature.txt").read_text() == "done fix\n"
        assert not (repo / "feature.txt").exists()
        # ответ пользователя дошёл до автора в следующем промпте
        prompts = "".join(p.read_text() for p in (e.dir / "raw").glob("proposer-*.prompt.md"))
        assert "Тяжёлый прогон нельзя" in prompts
        # у grok сессия: первый вызов -s, дальше -r
        crit = sorted((e.dir / "raw").glob("critic-*.prompt.md"))
        assert '"-s"' in crit[0].read_text().splitlines()[0] and '"-r"' in crit[1].read_text().splitlines()[0]
    os.environ.pop("FAKE_BAD_JSON_FIRST")


def test_escalation_accept():
    """Критик всегда против → после max_rounds пользователь принимает план автора."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        cfg["debate"]["max_rounds"] = 1
        cfg["debate"]["blind_first"] = False
        # заставим критика всегда возражать: подменим счётчик через состояние
        state = tmp / "state"
        state.mkdir(exist_ok=True)
        (state / "grok-critique.n").write_text("-100")  # n<=1 при каждом вызове? нет: n растёт; задаём отрицательное
        ui = ScriptUI(choices=["p", "q"], texts=["ответ"])
        e = Engine(cfg, repo, ui, brief="Задача два")
        e.run()
        print(*ui.log, sep="\n")
        assert ("choose", "Нужно ваше решение", "p") in ui.log
        assert "принят пользователем" in (e.dir / "plan.md").read_text()


class Crash(Exception):
    pass


class CrashingUI(ScriptUI):
    """Падает на первом текстовом вводе — имитация закрытого терминала посреди обсуждения."""

    def text(self, prompt):
        raise Crash()


def test_resume_after_crash_and_quit():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        # 1) падение, когда критик задал вопрос (раунд 1)
        e1 = Engine(cfg, repo, CrashingUI([], []), brief="Задача с перерывом")
        try:
            e1.run()
        except Crash:
            pass
        st = json.loads((e1.dir / "state.json").read_text())
        assert st["flow"]["phase"] == "debate" and st["flow"]["next"] == "critic" and st["flow"]["round"] == 0
        crit_sid = st["sessions"]["critic"]["session_id"]

        rows = list_debates(repo, ".debate")
        assert rows[0]["resumable"] and rows[0]["phase"] == "debate"

        # 2) продолжение: ответ на вопрос, план готов → выходим (q)
        ui2 = ScriptUI(choices=["q"], texts=["нельзя"])
        e2 = Engine(cfg, repo, ui2, resume_dir=e1.dir)
        e2.run()
        print(*ui2.log, sep="\n")
        assert e2.f["phase"] == "plan_ready"
        assert e2.roles["critic"].session_id == crit_sid
        # критик продолжил ту же сессию (-r <тот же id>)
        last_crit = sorted((e1.dir / "raw").glob("critic-*.prompt.md"))[-1].read_text().splitlines()[0]
        assert '"-r"' in last_crit and crit_sid in last_crit

        # 3) ещё одно продолжение: го → исполнение до конца
        ui3 = ScriptUI(choices=["g", "o"], texts=[])
        e3 = Engine(cfg, repo, ui3, resume_dir=e1.dir)
        e3.run()
        assert e3.f["phase"] == "done"
        assert not list_debates(repo, ".debate")[0]["resumable"]
        # имена логов не перезаписались: нумерация вызовов продолжилась
        names = [p.name for p in (e1.dir / "raw").glob("critic-*.prompt.md")]
        assert len(names) == len(set(names)) >= 3


def test_grok_cancelled_turn_is_continued():
    os.environ["FAKE_CANCEL"] = "1"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            ui = ScriptUI(choices=["c", "q"], texts=["нельзя"])  # c — не разрешать ls, продолжить без него
            e = Engine(cfg, repo, ui, brief="Отмена хода")
            e.run()
            assert ("turn", "Критик · grok", "analysis") in ui.log
            prompts = [p.read_text() for p in sorted((e.dir / "raw").glob("critic-*.prompt.md"))]
            assert "continue_after_denial" in prompts[1] and "ls -la" in prompts[1]
            assert not any("fix_json" in p for p in prompts)
    finally:
        os.environ.pop("FAKE_CANCEL")


def test_grant_permission_mid_debate():
    os.environ["FAKE_DENY"] = "1"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            ui = ScriptUI(choices=["w", "q"], texts=["нельзя"])  # w — разрешить по шаблону Bash(ssh:*)
            e = Engine(cfg, repo, ui, brief="Нужен стенд")
            e.run()
            print(*ui.log, sep="\n")
            assert ("choose", "Агенту нужно разрешение", "w") in ui.log
            assert e.f["grants"] == ["Bash(ssh:*)"]
            assert "Со стендом" in (e.dir / "plan.v1.md").read_text()  # ответ после разрешения заменил первый
            # разрешение действует и для следующих вызовов, и у Grok (--allow)
            crit = sorted((e.dir / "raw").glob("critic-*.prompt.md"))[-1].read_text().splitlines()[0]
            assert '"--allow", "Bash(ssh:*)"' in crit
            st = json.loads((e.dir / "state.json").read_text())
            assert st["flow"]["grants"] == ["Bash(ssh:*)"]
            # после resume разрешение снова применяется
            e2 = Engine(cfg, repo, ScriptUI([], []), resume_dir=e.dir)
            assert e2.roles["proposer"].grants == ["Bash(ssh:*)"]
            argv, _, _ = e2.roles["proposer"].build("x", repo, "", "debate")
            i = argv.index("--allowedTools")
            assert argv[i + 1] == "Bash(ssh:*)" and "Read" in argv[i + 1:]
    finally:
        os.environ.pop("FAKE_DENY")


def test_qwen_and_opencode_adapters():
    """Полный цикл, где критик — Qwen Code, а ревьюер — OpenCode с выбранной моделью."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        state = tmp / "state"
        cfg["agents"]["qwen"]["cmd"] = make_wrapper(tmp, "qwen", state)
        cfg["agents"]["qwen"]["model"] = "qwen3-coder-plus"
        cfg["agents"]["opencode"]["cmd"] = make_wrapper(tmp, "opencode", state)
        cfg["agents"]["opencode"]["model"] = "deepseek/deepseek-chat"
        cfg["roles"].update(critic="qwen", reviewer="opencode")
        ui = ScriptUI(choices=["g", "o"], texts=["нельзя"])
        e = Engine(cfg, repo, ui, brief="Фича через qwen и opencode")
        e.run()
        print(*ui.log, sep="\n")
        assert ("turn", "Критик · qwen", "disagree") in ui.log
        assert ("turn", "Критик · qwen", "agree") in ui.log
        assert ("turn", "Ревьюер · opencode", "approve") in ui.log
        raw = e.dir / "raw"
        q = sorted(raw.glob("critic-*-qwen.prompt.md"))
        first, later = q[0].read_text().splitlines()[0], q[-1].read_text().splitlines()[0]
        assert '"--session-id"' in first and '"--resume"' in later and '"qwen3-coder-plus"' in first
        assert '"run_shell_command(git status)"' in first and '"plan"' in first
        oc = sorted(raw.glob("reviewer-*-opencode.prompt.md"))
        lines = oc[-1].read_text().splitlines()
        assert '"deepseek/deepseek-chat"' in lines[0] and '"--session"' in lines[0]
        env = json.loads(lines[1][len("<!-- env: "):-len(" -->")])
        oc_cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        bash = oc_cfg["permission"]["bash"]
        assert list(bash)[0] == "*" and bash["*"] == "deny" and bash["git status*"] == "allow"
        assert oc_cfg["permission"]["edit"] == "deny" and "debate" in oc_cfg["agent"]
        assert e.roles["reviewer"].session_id.startswith("ses_")
        assert e.tokens[0] > 0


def test_custom_agent_name_inherits_kind_defaults():
    from debate.config import fill_agent_defaults
    cfg = deep_merge(DEFAULTS, {"agents": {"deepseek": {"kind": "opencode", "model": "deepseek/deepseek-chat"}}})
    cfg = fill_agent_defaults(cfg)
    d = cfg["agents"]["deepseek"]
    assert d["model"] == "deepseek/deepseek-chat" and d["debate_args"] == ["--agent", "debate"]
    assert d["options"]["debate_permission"]["edit"] == "deny"
    from debate.agents import Agent
    assert "Bash(git status:*)" in Agent.from_cfg("deepseek", "critic", d, 60, None).allowed_patterns("debate")
    assert cfg["agents"]["opencode"]["model"] == ""  # стандартный агент не тронут


def test_exec_grants_do_not_leak_into_debate():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        e = Engine(cfg, repo, ScriptUI(["w"], []), brief="Права исполнителя")
        ex = e.roles["executor"]
        # у исполнителя по умолчанию есть команды чтения
        assert "Bash(grep:*)" in ex.allowed_patterns("exec")
        got = e._ask_grants(ex, "Исполнитель · claude", [{"tool": "Write", "cmd": "/tmp/x/a.sh"}], "exec")
        assert got == ["Write(//tmp/x/**)"] and e.f["grants_exec"] == got and e.f["grants"] == []
        argv, _, _ = ex.build("x", repo, "", "exec")
        assert "Write(//tmp/x/**)" in argv
        rv_argv, _, _ = e.roles["reviewer"].build("x", repo, "", "debate")
        assert "Write(//tmp/x/**)" not in rv_argv
        argv_debate, _, _ = ex.build("x", repo, "", "debate")
        assert "Write(//tmp/x/**)" not in argv_debate


def test_resume_with_role_switch():
    """Grok выбыл (лимит) — ревьюером на продолжении становится OpenCode с новой сессией."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        cfg["agents"]["opencode"]["cmd"] = make_wrapper(tmp, "opencode", tmp / "state")
        e1 = Engine(cfg, repo, ScriptUI(["q"], ["нельзя"]), brief="Смена ревьюера")
        e1.run()
        assert e1.f["phase"] == "plan_ready" and e1.roles["reviewer"].name == "grok"
        ui = ScriptUI(["g", "o"], [])
        e2 = Engine(cfg, repo, ui, resume_dir=e1.dir, role_overrides={"reviewer": "opencode"})
        assert e2.replaced == ["reviewer: grok → opencode"] and not e2.roles["reviewer"].started
        assert e2.roles["critic"].name == "grok" and e2.roles["critic"].started  # остальные роли не тронуты
        e2.run()
        assert ("turn", "Ревьюер · opencode", "approve") in ui.log
        st = json.loads((e1.dir / "state.json").read_text())
        assert st["sessions"]["reviewer"]["agent"] == "opencode"
        assert "Смена агентов: reviewer: grok → opencode" in (e1.dir / "transcript.md").read_text()


def test_layered_config(monkeypatch=None):
    """Глобальный → debate.toml → .debate/config.toml → --config; allow_* дописываются, остальное заменяется."""
    import debate.config as C
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        g = tmp / "global.toml"
        g.write_text('[roles]\ncritic = "qwen"\n[agents.claude]\nallow_exec = ["Bash(docker:*)"]\n'
                     '[debate]\nmax_rounds = 6\n[agents.opencode]\nmodel = "kimi/k2"\n')
        repo = tmp / "repo"
        (repo / ".debate").mkdir(parents=True)
        (repo / "debate.toml").write_text('[debate]\nmax_rounds = 5\n')
        (repo / ".debate" / "config.toml").write_text(
            '[agents.claude]\nallow_exec = ["Bash(dotnet test:*)"]\n[exec]\nuse_worktree = false\n'
            '[agents.deepseek]\nkind = "opencode"\n[roles]\nreviewer = "deepseek"\n')
        old = C.GLOBAL_CONFIG
        C.GLOBAL_CONFIG = g
        try:
            cfg, used, prov = C.load_config(None, repo, with_provenance=True)
            extra = tmp / "extra.toml"
            extra.write_text('[debate]\nmax_rounds = 9\n')
            cfg2, used2 = C.load_config(extra, repo)
        finally:
            C.GLOBAL_CONFIG = old
        assert [p.name for p in used] == ["global.toml", "debate.toml", "config.toml"]
        assert cfg["roles"]["critic"] == "qwen" and cfg["roles"]["reviewer"] == "deepseek"
        assert cfg["debate"]["max_rounds"] == 5                      # репозиторий перекрыл глобальный
        assert cfg["agents"]["claude"]["allow_exec"] == ["Bash(docker:*)", "Bash(dotnet test:*)"]  # дописалось
        assert cfg["exec"]["use_worktree"] is False
        assert cfg["agents"]["deepseek"]["model"] == "kimi/k2"      # унаследовал вид opencode с учётом глобального
        assert prov[("debate", "max_rounds")] == "debate.toml"
        assert "+" in prov[("agents", "claude", "allow_exec")]
        assert cfg2["debate"]["max_rounds"] == 9 and len(used2) == 4
        from debate.agents import Agent
        ex = Agent.from_cfg("claude", "executor", cfg["agents"]["claude"], 60, None)
        argv, _, _ = ex.build("x", repo, "", "exec")
        i = argv.index("--allowedTools")
        assert "Bash(docker:*)" in argv[i:] and "Bash(dotnet test:*)" in argv[i:] and "Bash(grep:*)" in argv[i:]


def test_agent_gets_pwd_of_workdir():
    """OpenCode берёт каталог проекта из $PWD: исполнитель в worktree должен писать в worktree, а не в репозиторий."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        cfg["agents"]["opencode"]["cmd"] = make_wrapper(tmp, "opencode", tmp / "state")
        cfg["roles"]["executor"] = "opencode"
        cfg["exec"]["use_worktree"] = True
        os.environ["PWD"] = str(repo)  # как будто debate запущен из репозитория
        ui = ScriptUI(choices=["g", "o"], texts=["нельзя"])
        e = Engine(cfg, repo, ui, brief="Исполнение в worktree через opencode")
        e.run()
        wt = repo.parent / f"repo-wt-{e.slug}"
        assert (wt / "feature.txt").exists() and not (repo / "feature.txt").exists()


def test_legacy_state_resume():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        d = repo / ".debate" / "20261112-061408-old"
        (d / "raw").mkdir(parents=True)
        (d / "brief.md").write_text("Старая задача\n")
        (d / "plan.v2.md").write_text("# План v2\n")
        (d / "plan.md").write_text("# План (v2)\n\nСтатус: согласован обоими агентами.\n")
        for n in ("critic-001-grok", "critic-002-grok", "proposer-001-claude"):
            (d / "raw" / f"{n}.prompt.md").write_text("x")
        (d / "state.json").write_text(json.dumps({"slug": d.name, "repo": str(repo), "version": 2, "cost_usd": 1.0,
            "sessions": {r: {"agent": a, "session_id": f"sid-{r}", "started": r in ("proposer", "critic")}
                         for r, a in (("proposer", "claude"), ("critic", "grok"), ("executor", "claude"), ("reviewer", "grok"))}}))
        rows = list_debates(repo, ".debate")
        assert rows[0]["phase"] == "plan_ready" and rows[0]["resumable"] and "старая версия" in rows[0]["phase_ru"]
        ui = ScriptUI(choices=["g", "o"], texts=[])
        e = Engine(cfg, repo, ui, resume_dir=d)
        assert e.roles["critic"].calls == 2 and e.roles["critic"].session_id == "sid-critic"
        assert e.plan == "# План v2"
        e.run()
        assert e.f["phase"] == "done"


def test_language_setting():
    """Промпты на английском, язык ответов агентов задаётся [rules] language и доходит до системных правил."""
    from debate import protocol as P
    assert "in Russian" in P.system_rules("critic")
    assert "in English" in P.system_rules("critic", language="en")
    assert "in Klingon" in P.system_rules("critic", language="Klingon")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        cfg = deep_merge(cfg, {"rules": {"language": "en"}, "exec": {"enabled": False}})
        ui = ScriptUI(choices=["q"], texts=[])
        e = Engine(cfg, repo, ui, brief="Task")
        e.run()
        first = sorted((e.dir / "raw").glob("critic-*.prompt.md"))[0].read_text()
        assert "Stage: independent_analysis" in first and "in English" in first


def test_chained_denial_patterns_and_no_repeat_ask():
    """Склейка с echo: echo предлагается к разрешению; «ровно эти» — по командам; повторный отказ не спрашивается."""
    from debate import protocol as P
    d = [{"tool": "Bash", "cmd": 'ls /x 2>/dev/null | head -n 3; echo "---"; cat /x/p.json'}]
    assert "Bash(echo:*)" in P.suggest_patterns(d)
    assert P.exact_patterns(d) == ["Bash(ls /x 2>/dev/null)", "Bash(head -n 3)", 'Bash(echo "---")', "Bash(cat /x/p.json)"]
    os.environ["FAKE_DENY"] = os.environ["FAKE_DENY_REPEAT"] = "1"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            ui = ScriptUI(choices=["w", "q"], texts=["нельзя"])
            e = Engine(cfg, repo, ui, brief="Нужен стенд")
            e.run()
            asks = [x for x in ui.log if x[:2] == ("choose", "Агенту нужно разрешение")]
            assert len(asks) == 1, asks
            assert any(x[0] == "info" and "снова получил отказ" in x[1] for x in ui.log)
    finally:
        os.environ.pop("FAKE_DENY"); os.environ.pop("FAKE_DENY_REPEAT")


def test_review_limit_escalates_to_user():
    """Лимит кругов ревью исчерпан с blocker → меню пользователю; поля замечаний не по схеме приводятся к схеме."""
    os.environ["FAKE_REVIEW_NEVER"] = "1"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            cfg = deep_merge(cfg, {"exec": {"max_review_rounds": 1}})
            # план → го; лимит → ещё круг; лимит снова → принять; итог → ок
            ui = ScriptUI(choices=["g", "f", "a", "o"], texts=["Тяжёлый прогон нельзя"])
            e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
            e.run()
            limits = [x for x in ui.log if x[:2] == ("choose", "Ревью не пройдено")]
            assert len(limits) == 2, limits
            rv = e.f["exec"]["reviews"][-1]["points"][0]
            assert rv["claim"] == "поправка не внесена" and rv["evidence"] == "feature.txt:1" and rv["severity"] == "blocker"
            assert e.f["phase"] == "done" and e.f["exec"]["accepted"]
            assert not [r for r in list_debates(repo, ".debate") if r["resumable"]]
            # результат старой версии: завершено без одобрения и без «принять» → resume открывает меню
            e.f["exec"].pop("accepted")
            e._save_state()
            assert [r for r in list_debates(repo, ".debate") if r["resumable"]]
            ui2 = ScriptUI(choices=["a", "o"], texts=[])
            Engine(cfg, repo, ui2, resume_dir=e.dir).run()
            assert ("choose", "Ревью не пройдено") in [x[:2] for x in ui2.log]
    finally:
        os.environ.pop("FAKE_REVIEW_NEVER")


def git_out(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_in_place_branch_and_dirty_tree():
    """По умолчанию — прямо в репозитории на ветке debate/<имя>; незакоммиченное пользователя — в stash по выбору."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        (repo / "README.md").write_text("моя незакоммиченная правка\n")
        (repo / "notes.txt").write_text("новый файл пользователя\n")
        base = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
        # план → го; грязное дерево → спрятать; итог → ок
        ui = ScriptUI(choices=["g", "h", "o"], texts=["нельзя"])
        e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
        e.run()
        ex = e.f["exec"]
        assert ex["branch"] == f"debate/{e.slug}" and ex["base_branch"] == base and ex["stashed"]
        assert git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == f"debate/{e.slug}"
        assert (repo / "feature.txt").read_text() == "done fix\n"           # исполнитель работал в репозитории
        assert not (repo.parent / f"repo-wt-{e.slug}").exists()             # worktree не создавался
        assert (repo / "README.md").read_text() == "hi\n" and not (repo / "notes.txt").exists()
        assert "debate: перед исполнением" in git_out(repo, "stash", "list")
        staged = git_out(repo, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["feature.txt"], staged                            # ни правок пользователя, ни .debate
        final = [x for x in ui.log if x[:2] == ("choose", "Исполнение завершено")]
        assert final
        assert e._finish_hint(ex, repo).count("git stash pop") == 1


def test_in_place_with_unignored_debate_dir():
    """.debate не в .gitignore: служебные файлы не считаются «незакоммиченным» и не попадают в дифф."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        (repo / ".gitignore").write_text("")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "no ignore"], cwd=repo, check=True)
        ui = ScriptUI(choices=["g", "o"], texts=["нельзя"])   # меню «незакоммиченные изменения» не должно появиться
        e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
        e.run()
        staged = git_out(repo, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["feature.txt"], staged


def test_legacy_worktree_exec_resume():
    """Обсуждение старой версии, исполнявшееся в worktree (без флага worktree в state): resume не трогает ветки репо."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, cfg = setup(tmp)
        cfg["exec"]["use_worktree"] = True
        ui = ScriptUI(choices=["g", "o"], texts=["нельзя"])
        e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
        e.run()
        e.f["exec"].pop("worktree")
        e.f["exec"]["step"] = "review"
        e.f["phase"] = "exec"
        e._save_state()
        base = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
        cfg["exec"]["use_worktree"] = False
        ui2 = ScriptUI(choices=["o"], texts=[])
        e2 = Engine(cfg, repo, ui2, resume_dir=e.dir)
        e2.run()
        assert ("choose", "Открыта другая ветка") not in [x[:2] for x in ui2.log]
        assert git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == base
        assert "git worktree remove" in e2._finish_hint(e2.f["exec"], Path(e2.f["exec"]["workdir"]))


def test_fallback_chain_on_limit():
    """critic/reviewer = [grok, opencode]: у grok лимит → роль сама уходит opencode с вводной; grok больше не зовут."""
    os.environ["FAKE_LIMIT"] = "grok"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            cfg["agents"]["opencode"]["cmd"] = make_wrapper(tmp, "opencode", tmp / "state")
            cfg["roles"].update(critic=["grok", "opencode"], reviewer=["grok", "opencode"])
            ui = ScriptUI(choices=["g", "o"], texts=["нельзя"])
            e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
            e.run()
            assert e.f["phase"] == "done"
            assert e.roles["critic"].name == "opencode" and e.roles["reviewer"].name == "opencode"
            assert e.f["exhausted"] == ["grok"] and not e.f["takeover"]
            assert not [x for x in ui.log if x[0] == "choose" and x[1].startswith("Ошибка")]   # без вопросов
            raw = e.dir / "raw"
            assert len(list(raw.glob("*-grok.prompt.md"))) == 1                # grok попробовали один раз
            first_oc = sorted(raw.glob("critic-*-opencode.prompt.md"))[0].read_text()
            assert "taking over the role" in first_oc and "Сделать фичу feature" in first_oc
            first_rv = sorted(raw.glob("reviewer-*-opencode.prompt.md"))[0].read_text()
            assert "taking over the role" in first_rv
            assert "Смена агента" in (e.dir / "transcript.md").read_text()
    finally:
        os.environ.pop("FAKE_LIMIT")


def test_quoted_separators_and_no_bare_write():
    """Разделители внутри кавычек не режут команду; голое `Write` в подсказках не появляется."""
    from debate import protocol as P
    assert P.split_raw("grep -e 'a; b|c' f && echo \"x;y\" 2>&1 | head") == ["grep -e 'a; b|c' f", 'echo "x;y" 2>&1', "head"]
    assert P.suggest_patterns([{"tool": "Bash", "cmd": "rg 'class\": x; y' src"}]) == ["Bash(rg:*)"]
    assert P.suggest_patterns([{"tool": "Write", "cmd": "notes.md"}]) == []
    assert P.suggest_patterns([{"tool": "Write", "cmd": "/tmp/x/a.txt"}]) == ["Write(//tmp/x/**)"]
    assert P.is_unavailable("Error: usage limit reached") and not P.is_unavailable("syntax error in reply")


def test_suggest_and_write_roles():
    """Раздача ролей: критик — другой агент; запись [roles] не трогает остальные разделы."""
    from debate.config import suggest_roles, write_roles
    r = suggest_roles(["grok", "claude", "opencode"])
    assert r["proposer"] == ["claude", "opencode"] and r["critic"] == ["grok", "opencode"]
    assert r["executor"] == r["proposer"] and r["reviewer"] == r["critic"]
    assert suggest_roles(["opencode"])["critic"] == ["opencode"]
    assert suggest_roles(["claude", "qwen"])["critic"] == ["qwen"]
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "c.toml"
        f.write_text('[roles]\ncritic = "x"\n\n[rules]\nlanguage = "en"\n')
        write_roles(f, r)
        import tomllib
        data = tomllib.loads(f.read_text())
        assert data["rules"]["language"] == "en" and data["roles"]["critic"] == ["grok", "opencode"]
        assert data["roles"]["proposer"] == ["claude", "opencode"]


def test_init_command_with_fakes():
    """debate init --here -y: находит CLI (обёртки), проверяет вход и пишет роли в .debate/config.toml."""
    from debate.cli import main
    import tomllib
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _ = setup(tmp)
        cfgf = repo / ".debate" / "config.toml"
        cfgf.parent.mkdir()
        cfgf.write_text(f'[agents.claude]\ncmd = "{make_wrapper(tmp, "claude", tmp / "state")}"\n'
                        f'[agents.opencode]\ncmd = "{make_wrapper(tmp, "opencode", tmp / "state")}"\n'
                        '[agents.grok]\ncmd = "/nonexistent/grok"\n[agents.qwen]\ncmd = "/nonexistent/qwen"\n')
        assert main(["init", "--here", "-y", "--repo", str(repo)]) == 0
        data = tomllib.loads(cfgf.read_text())
        assert data["roles"] == {"proposer": "claude", "critic": "opencode", "executor": "claude", "reviewer": "opencode"}
        assert data["agents"]["claude"]["cmd"].endswith("claude")


def test_replan_from_execution():
    """Ревью не пройдено → «вернуть план на обсуждение»: автор и критик пересматривают, исполнитель доводит то же."""
    os.environ["FAKE_REVIEW_NEVER"] = "1"
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, cfg = setup(tmp)
            cfg = deep_merge(cfg, {"exec": {"max_review_rounds": 0}})
            ui = ScriptUI(choices=["g", "p", "g", "a", "o"], texts=["Тяжёлый прогон нельзя", "дело в плане"])
            e = Engine(cfg, repo, ui, brief="Сделать фичу feature")
            e.run()
            assert e.f["phase"] == "done", e.f["phase"]
            raw = e.dir / "raw"
            ex_prompts = [p.read_text() for p in sorted(raw.glob("executor-*.prompt.md"))]
            assert any("The plan was revised after your previous implementation" in p for p in ex_prompts)
            prop = [p.read_text() for p in sorted(raw.glob("proposer-*.prompt.md"))]
            assert any("back for discussion after execution" in p and "дело в плане" in p for p in prop)
            branches = git_out(repo, "branch", "--list", "debate/*").split()
            assert len([b for b in branches if b.startswith("debate/")]) == 1   # ветка та же, новой не создано
            assert "Пересмотр плана после исполнения" in (e.dir / "transcript.md").read_text()
    finally:
        os.environ.pop("FAKE_REVIEW_NEVER")


if __name__ == "__main__":
    test_replan_from_execution()
    test_suggest_and_write_roles()
    test_init_command_with_fakes()
    test_fallback_chain_on_limit()
    test_quoted_separators_and_no_bare_write()
    test_legacy_worktree_exec_resume()
    test_in_place_branch_and_dirty_tree()
    test_in_place_with_unignored_debate_dir()
    test_review_limit_escalates_to_user()
    test_chained_denial_patterns_and_no_repeat_ask()
    test_language_setting()
    test_agent_gets_pwd_of_workdir()
    test_layered_config()
    test_resume_with_role_switch()
    test_exec_grants_do_not_leak_into_debate()
    test_custom_agent_name_inherits_kind_defaults()
    test_qwen_and_opencode_adapters()
    test_grant_permission_mid_debate()
    test_grok_cancelled_turn_is_continued()
    test_legacy_state_resume()
    test_full_cycle()
    test_escalation_accept()
    test_resume_after_crash_and_quit()
    print("OK")
