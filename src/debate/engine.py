"""Движок обсуждения — конечный автомат, а не чат.

Этапы (`flow.phase` в state.json):

    start       критик делает независимый разбор (blind_first), автор пишет план v1
    debate      раунды: критик → (согласие? → plan_ready) → автор отвечает на пункты и правит план → критик …
                при исчерпании раундов или тупике (те же пункты отклонены дважды) — эскалация пользователю
    plan_ready  план показан пользователю: «го» → exec, комментарий → снова debate, выход → можно продолжить позже
    exec        worktree → исполнитель → ревьюер → правки → ревьюер … → итог
    done        завершено

Порядок ходов задаёт код: каждый ответ агента заканчивается JSON-блоком с вердиктом, и движок по нему решает,
кому ход. Пользователя зовут, только когда агент задал вопрос, нужен выбор при эскалации, нужно разрешение
на действие или готов план.

Ядро не знает про консоль: всё общение с пользователем идёт через объект UI (консольная реализация — cli.py).
Состояние сохраняется в state.json после каждого шага, поэтому обсуждение можно продолжить (`debate resume`)
с того места, где оно прервалось; прерванный посреди хода ход повторяется целиком.

Файлы обсуждения: <репо>/.debate/<дата-время>-<задача>/
    brief.md, transcript.md, plan.vN.md, plan.md, review.json, state.json, raw/ (промпты и сырые ответы CLI)
"""
from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import gitutil
from . import protocol as P
from .agents import Agent, AgentError, TurnResult

SCHEMA = 1  # версия формата state.json; старые форматы поднимаются в _upgrade_legacy()
PHASE_RU = {"start": "начало", "debate": "обсуждение", "plan_ready": "план ждёт решения",
            "exec": "исполнение", "done": "завершено"}


class UI(Protocol):
    """Всё, что движку нужно от интерфейса. Консоль — cli.ConsoleUI, тесты — сценарный UI, позже — веб."""

    def info(self, msg: str) -> None: ...                                    # короткое служебное сообщение
    def working(self, label: str) -> AbstractContextManager: ...             # индикатор «агент работает»
    def show_turn(self, label: str, role: str, data: dict, res: TurnResult) -> None: ...  # показать ход агента
    def choose(self, title: str, body: str, options: dict[str, str]) -> str: ...  # выбор из вариантов {клавиша: текст}
    def text(self, prompt: str) -> str: ...                                  # свободный ввод (пусто — пропустить)


def _n(x: int) -> str:
    """1234567 → «1 234 567»."""
    return f"{x:,}".replace(",", " ")


class Stopped(Exception):
    """Пользователь выбрал «остановить». Состояние уже сохранено, обсуждение можно продолжить."""


class Engine:
    """Одно обсуждение: новое (brief) или продолженное (resume_dir).

    role_overrides при продолжении отдаёт роль другому агенту ({"reviewer": "opencode"}); у него будет новая
    сессия, контекст он получает из промпта хода (план, дифф и т. п.).
    """

    def __init__(self, cfg: dict, repo: Path, ui: UI, *, brief: str | None = None, resume_dir: Path | None = None,
                 role_overrides: dict[str, str] | None = None):
        self.cfg, self.ui = cfg, ui
        timeout = int(cfg["debate"]["turn_timeout_min"] * 60)
        if resume_dir is not None:
            self.dir = Path(resume_dir).resolve()
            st = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
            if st.get("schema") != SCHEMA or "flow" not in st:
                st = _upgrade_legacy(self.dir, st)
            self.brief = (self.dir / "brief.md").read_text(encoding="utf-8").strip()
            self.slug, self.repo = st["slug"], Path(st["repo"])
            role_names = {r: s["agent"] for r, s in st["sessions"].items()}
        else:
            if not brief or not brief.strip():
                raise ValueError("Пустая задача")
            st = {}
            self.repo = repo.resolve()
            self.brief = brief.strip()
            self.slug = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{P.slugify(self.brief)}"
            self.dir = self.repo / cfg["output"]["dir"] / self.slug
            (self.dir / "raw").mkdir(parents=True, exist_ok=True)
            (self.dir / "brief.md").write_text(self.brief + "\n", encoding="utf-8")
            role_names = dict(cfg["roles"])

        self.roles: dict[str, Agent] = {}
        for role, name in role_names.items():
            if name not in cfg["agents"]:
                raise ValueError(f"Роль {role}: агент {name!r} не описан в [agents]")
            a = Agent.from_cfg(name, role, cfg["agents"][name], timeout, self.dir / "raw")
            if role in st.get("sessions", {}):
                s = st["sessions"][role]
                a.session_id, a.started, a.calls = s["session_id"], s["started"], s.get("calls", 0)
            self.roles[role] = a
        self.replaced: list[str] = []
        for role, name in (role_overrides or {}).items():
            if role not in self.roles:
                raise ValueError(f"Нет роли {role!r}, роли: {', '.join(self.roles)}")
            if name not in cfg["agents"]:
                raise ValueError(f"Роль {role}: агент {name!r} не описан в [agents]")
            prev = self.roles[role]
            if prev.name == name:
                continue
            # другой агент — новая сессия: история прошлого агента ему недоступна, контекст даёт промпт хода
            a = Agent.from_cfg(name, role, cfg["agents"][name], timeout, self.dir / "raw")
            a.calls = prev.calls
            self.roles[role] = a
            self.replaced.append(f"{role}: {prev.name} → {name}")

        # Состояние, которое переживает перезапуск (см. _save_state):
        #   notes           комментарии пользователя, ещё не доставленные агенту каждой роли;
        #   plan / version  текущий план и его версия; last_responses — ответы автора на пункты критика;
        #   minors          замечания, оставшиеся открытыми после согласия (уходят исполнителю);
        #   f (flow)        этап, раунд, следующий ход, разрешения пользователя, шаг исполнения.
        self.notes: dict[str, list[str]] = st.get("notes") or {r: [] for r in self.roles}
        self.plan: str = st.get("plan", "")
        self.version: int = st.get("version", 0)
        self.last_responses: list = st.get("last_responses", [])
        self.minors: list = st.get("minors", [])
        self.tokens: list[int] = st.get("tokens", [0, 0])
        self.f: dict = st.get("flow") or {"phase": "start", "blind_done": False}
        self.f.setdefault("grants", [])       # для обсуждения и ревью
        self.f.setdefault("grants_exec", [])  # для исполнения
        self.f.setdefault("declined", [])     # отклонённые пользователем действия: повторно о них не спрашиваем
        self._apply_grants()
        self.transcript = self.dir / "transcript.md"
        if resume_dir is None:
            self._t(f"# Обсуждение\n\nПапка: `{self.dir}`\n\n## Задача\n\n{self.brief}\n")
        else:
            self._t(f"\n---\n\n*Продолжение {datetime.now():%Y-%m-%d %H:%M}, этап: {PHASE_RU[self.f['phase']]}*\n")
            if self.replaced:
                self._t(f"\n*Смена агентов: {'; '.join(self.replaced)} (новые сессии)*\n")
        self._save_state()

    # ================= общий цикл =================
    def run(self) -> None:
        """Крутит автомат до завершения, выхода пользователя или остановки. Состояние сохраняется в любом случае."""
        try:
            while True:
                ph = self.f["phase"]
                if ph == "start":
                    self._start()
                elif ph == "debate":
                    self._rounds()
                elif ph == "plan_ready":
                    if not self._plan_ready():
                        break
                elif ph == "exec":
                    self._execute()
                else:
                    break
        except Stopped:
            self.ui.info("Остановлено пользователем.")
        finally:
            self._save_state()
            self.ui.info(f"Файлы обсуждения: {self.dir}")
            if any(self.tokens):
                self.ui.info(f"Токены за обсуждение: вход {_n(self.tokens[0])}, выход {_n(self.tokens[1])}")
            if self.f["phase"] != "done":
                self.ui.info(f"Продолжить: debate resume {self.slug}")

    # ================= обсуждение =================
    def _start(self) -> None:
        """Независимый разбор критика (если включён) и первая версия плана."""
        if self.cfg["debate"]["blind_first"] and not self.f.get("blind_done"):
            d = self._turn("critic", P.p_blind(self.brief))
            self._questions("critic", d, lambda: self._turn("critic", P.p_continue()))
            self.f["blind_done"] = True
            self._save_state()
        d = self._turn("proposer", P.p_propose(self.brief))
        d = self._questions("proposer", d, lambda: self._turn("proposer", P.p_continue()))
        self._set_plan(d)
        self._new_debate("critic")

    def _new_debate(self, first: str) -> None:
        """Начинает серию раундов. first: "critic" — после нового плана, "proposer_note" — после комментария."""
        self.f.update(phase="debate", next=first, round=0, prev_open=None,
                      max_rounds=int(self.cfg["debate"]["max_rounds"]))
        self._save_state()

    def _rounds(self) -> None:
        """Раунды обсуждения. f["next"] — чей следующий ход; сохраняется после каждого хода для resume.

        Согласие = у критика нет пунктов blocker/major (verdict agree, либо disagree только с minor).
        Эскалация: исчерпан max_rounds или автор второй раз подряд отклонил те же существенные пункты.
        """
        f = self.f
        while f["phase"] == "debate":
            nxt = f["next"]
            if nxt == "proposer_note":
                d = self._turn("proposer", P.p_revise_with_note())
                d = self._questions("proposer", d, lambda: self._turn("proposer", P.p_continue()))
                self._set_plan(d)
                f["next"] = "critic"

            elif nxt == "critic":
                c = self._turn("critic", P.p_critique(self.roles["proposer"].name, self.version,
                                                      self.plan, self.last_responses))
                c = self._questions("critic", c, lambda: self._turn("critic", P.p_continue()))
                f["round"] += 1  # после хода: прерванный ход при resume повторяется в том же раунде
                f["last_crit"] = c
                if not P.blocking_points(c):
                    # agree, либо disagree без существенных пунктов — согласие с замечаниями
                    self.minors = P.minor_points(c)
                    f.update(phase="plan_ready", outcome="consensus")
                    self._t(f"\n**Консенсус** на раунде {f['round']}, версия плана {self.version}.\n")
                elif f["round"] >= f["max_rounds"]:
                    f.update(next="escalate", after_escalate="proposer",
                             escalate_reason=f"За {f['round']} раундов не договорились.")
                else:
                    f["next"] = "proposer"

            elif nxt == "proposer":
                c = f["last_crit"]
                p = self._turn("proposer", P.p_respond(self.roles["critic"].name, c))
                p = self._questions("proposer", p, lambda: self._turn("proposer", P.p_continue()))
                self._set_plan(p)
                open_ids = sorted(str(x.get("id")) for x in P.blocking_points(c))
                rejected = {str(x.get("id")) for x in p.get("responses") or []
                            if str(x.get("decision")).lower() == "rejected"}
                if f.get("prev_open") is not None and open_ids == f["prev_open"] and set(open_ids) <= rejected:
                    f.update(next="escalate", after_escalate="critic",
                             escalate_reason="Тупик: автор снова отклонил те же существенные пункты.")
                else:
                    f["next"] = "critic"
                f["prev_open"] = open_ids

            elif nxt == "escalate":
                if self._escalate(f["last_crit"], f["escalate_reason"]) == "accept":
                    f.update(phase="plan_ready", outcome="user_accepted")
                else:
                    f["max_rounds"] = max(f["max_rounds"], f["round"] + 2)  # ещё два раунда после решения
                    f["next"] = f.get("after_escalate", "proposer")
            self._save_state()

    def _escalate(self, crit: dict, reason: str) -> str:
        """Показывает пользователю обе позиции. → "accept" (принять план автора) или "continue" (ещё 2 раунда)."""
        body = (f"{reason}\n\n## Позиция автора ({self.roles['proposer'].name}), план v{self.version}\n\n{self.plan}\n\n"
                f"## Ответы автора\n\n{P.fmt_responses(self.last_responses)}\n\n"
                f"## Открытые пункты критика ({self.roles['critic'].name})\n\n{crit.get('summary', '')}\n\n"
                f"{P.fmt_points(P.blocking_points(crit))}")
        self._t(f"\n### Эскалация пользователю\n\n{reason}\n")
        ch = self.ui.choose("Нужно ваше решение", body, {
            "p": "принять план автора как есть",
            "c": "поддержать критика — автор обязан исправить",
            "n": "своё решение (текстом)",
            "s": "остановить"})
        if ch == "s":
            raise Stopped
        if ch == "p":
            self._broadcast("Пользователь принял текущий план автора, несмотря на возражения критика.", ("proposer", "critic"))
            self.minors = P.blocking_points(crit) + P.minor_points(crit)
            return "accept"
        if ch == "c":
            ids = ", ".join(str(p.get("id")) for p in P.blocking_points(crit))
            self._broadcast(f"Пользователь поддерживает критика по пунктам {ids}. Автор должен исправить план.",
                            ("proposer", "critic"))
        else:
            note = self.ui.text("Ваше решение")
            if note:
                self._broadcast(note, ("proposer", "critic"))
        return "continue"

    def _plan_ready(self) -> bool:
        """False — пользователь вышел (план сохранён, можно продолжить позже)."""
        outcome = self.f.get("outcome", "consensus")
        self._write_plan(outcome)
        ch = self.ui.choose("План готов", self._plan_md(outcome),
                            {"g": "го — исполнять", "n": "комментарий — обсудить ещё", "q": "выйти (план сохранён)"})
        if ch == "q":
            return False
        if ch == "g":
            if self.cfg["exec"]["enabled"]:
                self.f.update(phase="exec", exec={"step": "setup"})
            else:
                self.ui.info("Исполнение отключено в конфиге ([exec] enabled = false).")
                self.f["phase"] = "done"
            self._save_state()
            return True
        note = self.ui.text("Ваш комментарий к плану")
        if note:
            self._broadcast(note, ("proposer", "critic"))
        self._new_debate("proposer_note")
        return True

    # ================= исполнение =================
    def _execute(self) -> None:
        """Исполнение по шагам ex["step"]: setup → execute → review ⇄ fix → final. Каждый шаг сохраняется."""
        ex_cfg = self.cfg["exec"]
        ex = self.f.setdefault("exec", {"step": "setup"})
        plan_text = (self.dir / "plan.md").read_text(encoding="utf-8")
        while True:
            step = ex["step"]
            wd = Path(ex["workdir"]) if ex.get("workdir") else self.repo

            if step == "setup":
                ex.update(workdir=str(self.repo), branch=None)
                if ex_cfg["use_worktree"] and gitutil.is_repo(self.repo):
                    try:
                        path, branch = gitutil.add_worktree(gitutil.toplevel(self.repo), self.slug)
                        ex.update(workdir=str(path), branch=branch)
                        self.ui.info(f"Создан worktree {path} (ветка {branch}). Файлы вне git (данные, .env, модели) "
                                     "там отсутствуют — при необходимости отключите worktree: "
                                     "[exec] use_worktree = false или debate run --in-place.")
                    except Exception as e:
                        if self.ui.choose("Не удалось создать worktree", str(e),
                                          {"i": "исполнять прямо в репозитории", "s": "остановить"}) == "s":
                            raise Stopped
                elif ex_cfg["use_worktree"]:
                    if self.ui.choose("Каталог не git-репозиторий", "Worktree создать нельзя.",
                                      {"i": "исполнять прямо в каталоге", "s": "остановить"}) == "s":
                        raise Stopped
                ex["step"] = "execute"

            elif step == "execute":
                e = self._turn("executor", P.p_exec(plan_text, str(wd)), mode="exec", cwd=wd)
                e = self._questions("executor", e,
                                    lambda: self._turn("executor", P.p_continue(), mode="exec", cwd=wd))
                ex.update(step="review", last_exec=e.get("summary", ""), review_round=0, reviews=[])

            elif step == "review":
                if not gitutil.is_repo(wd):
                    self.ui.info("Каталог не git-репозиторий — автоматическое ревью диффа пропущено.")
                    ex["step"] = "final"
                else:
                    stat, diff, trunc = gitutil.staged_diff(wd, int(ex_cfg["diff_char_limit"]))
                    if not diff.strip():
                        self.ui.info("Исполнитель не внёс изменений.")
                        ex["step"] = "final"
                    else:
                        rv = self._turn("reviewer", P.p_review(plan_text, ex.get("last_exec", ""), stat, diff, trunc),
                                        cwd=wd)
                        rv = self._questions("reviewer", rv,
                                             lambda: self._turn("reviewer", P.p_continue(), cwd=wd))
                        ex["reviews"].append(rv)
                        ex["verdict"] = str(rv.get("verdict"))
                        if ex["verdict"].lower() == "approve" and not P.blocking_points(rv):
                            ex["step"] = "final"
                        elif ex["review_round"] >= int(ex_cfg["max_review_rounds"]):
                            ex["step"] = "final"
                        else:
                            ex["step"] = "fix"

            elif step == "fix":
                e = self._turn("executor", P.p_fix(ex["reviews"][-1]), mode="exec", cwd=wd)
                e = self._questions("executor", e,
                                    lambda: self._turn("executor", P.p_continue(), mode="exec", cwd=wd))
                ex.update(step="review", last_exec=e.get("summary", ""), review_round=ex["review_round"] + 1)

            elif step == "final":
                (self.dir / "review.json").write_text(json.dumps(ex.get("reviews", []), ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
                stat = gitutil.git(["diff", "--cached", "--stat"], wd) if gitutil.is_repo(wd) else ""
                branch = ex.get("branch")
                how = (f"Проверьте и закоммитьте, затем слейте ветку `{branch}` в свою:\n\n"
                       f"```\ncd {wd}\ngit diff --cached\ngit commit -m '...'\n```" if branch else
                       "Изменения в рабочей копии репозитория (проиндексированы, не закоммичены).")
                self.f["phase"] = "done"
                self._save_state()
                self.ui.choose("Исполнение завершено",
                               f"Вердикт ревью: **{ex.get('verdict', '—')}**\n\n```\n{stat.strip() or '(нет изменений)'}\n```"
                               f"\n\n{how}", {"o": "ок"})
                return
            self._save_state()

    # ================= ход агента =================
    def _turn(self, role: str, prompt: str, mode: str = "debate", cwd: Path | None = None) -> dict:
        """Один ход агента роли `role`. Возвращает разобранный JSON-ответ (dict с verdict).

        По дороге: доставляет накопленные комментарии пользователя, обрабатывает Ctrl+C и ошибки CLI
        (повторить / дописать комментарий / остановить), спрашивает разрешения на отклонённые действия
        и повторяет ход с ними, продолжает оборванный ход, один раз просит повторить нечитаемый JSON.
        """
        agent = self.roles[role]
        label = f"{P.ROLE_RU[role]} · {agent.name}"
        full = P.notes_block(self.notes[role]) + prompt
        rules = P.system_rules(role, self.cfg["rules"].get("extra", ""))
        while True:
            try:
                with self.ui.working(label):
                    res = agent.ask(full, cwd=cwd or self.repo, rules=rules, mode=mode)
                break
            except KeyboardInterrupt:
                ch = self.ui.choose(f"Ход прерван: {label}", "Агент остановлен.",
                                    {"r": "повторить ход", "n": "добавить комментарий и повторить", "s": "остановить всё"})
            except AgentError as e:
                ch = self.ui.choose(f"Ошибка: {label}", str(e), {"r": "повторить ход", "s": "остановить всё"})
            if ch == "s":
                raise Stopped
            if ch == "n":
                note = self.ui.text("Комментарий для агента")
                if note:
                    full = P.notes_block([note]) + full
        self.notes[role] = []  # комментарии доставлены
        self._count(res)
        data = P.extract_json(res.text)
        for _ in range(3):
            granted = self._ask_grants(agent, label, res.denials, mode) if res.denials else None
            if granted:
                follow = P.p_after_grant(granted)
            elif res.stop == "cancelled" and data is None:
                # Grok в headless-режиме обрывает ход, если упёрся в действие, требующее одобрения
                d = res.denials[0] if res.denials else None
                reason = (f"не выполнено: {d['cmd'][:300]}" if d else
                          (res.stderr.splitlines()[0][:400] if res.stderr else "агент попытался выполнить запрещённое действие"))
                follow = P.p_resume_cancelled(reason)
            else:
                break
            try:
                with self.ui.working(f"{label}: продолжение"):
                    res = agent.ask(follow, cwd=cwd or self.repo, rules=rules, mode=mode)
            except AgentError as e:
                self.ui.info(f"{label}: продолжение не удалось: {e}")
                break
            self._count(res)
            data = P.extract_json(res.text) or (None if granted else data)
        if data is None:
            try:
                with self.ui.working(f"{label}: повторный запрос JSON"):
                    res2 = agent.ask(P.FIX_JSON, cwd=cwd or self.repo, rules=rules, mode=mode)
                self._count(res2)
                data = P.extract_json(res2.text)
            except AgentError:
                data = None
        if data is None:
            data = {"verdict": "unparsed", "summary": res.text[-3000:]}
            self.ui.info(f"{label}: не удалось разобрать JSON, ответ сохранён как текст.")
        self.ui.show_turn(label, role, data, res)
        self._log_turn(label, role, data, res)
        return data

    # ---------- разрешения ----------
    def _apply_grants(self) -> None:
        """Раздаёт агентам разрешения, выданные пользователем в этом обсуждении (отдельно по этапам)."""
        for a in self.roles.values():
            a.grants = list(self.f["grants"])
            a.grants_exec = list(self.f["grants_exec"])

    def _ask_grants(self, agent: Agent, label: str, denials: list[dict], mode: str = "debate") -> list[str] | None:
        """Показывает отклонённые команды и спрашивает, разрешить ли их. Возвращает новые шаблоны или None."""
        cmds = "\n".join(f"- `{d['tool']}`: {d['cmd'][:300]}{'…' if d.get('truncated') else ''}" for d in denials)
        self._t(f"\n> *{label}: отклонены команды:*\n" + "\n".join(f"> - {d['tool']}: {d['cmd'][:300]}" for d in denials) + "\n")
        new = [d for d in denials if f"{d['tool']}:{d['cmd']}" not in self.f["declined"]]
        if self.f.get("no_ask") or not new:
            self.ui.info(f"{label}: отклонено команд: {len(denials)} (без вопроса — вы выбрали не спрашивать)")
            return None
        allowed = set(agent.allowed_patterns(mode))
        sugg = [p for p in P.suggest_patterns(new) if p not in allowed]
        where = "исполнения" if mode == "exec" else "обсуждения"
        body = (f"{label} пытался выполнить команды, запрещённые на этапе {where}:\n\n{cmds}\n\n"
                f"Разрешение будет действовать до конца обсуждения только на этапе {where}.")
        if not sugg:
            body += ("\n\nПохоже, всё это уже разрешено по отдельности, а отказ из-за `cd …;` или склейки команд. "
                     "Разрешение не поможет; агенту передано правило не склеивать команды.")
        opts: dict[str, str] = {}
        if sugg:
            opts["w"] = "разрешить по шаблону: " + ", ".join(sugg)
        exact = [d for d in new if d["tool"] == "Bash" and not d.get("truncated")]
        if exact and len(exact) == len(new):
            opts["e"] = "разрешить ровно эти команды"
        opts.update({"t": "ввести свои шаблоны", "c": "продолжить без них",
                     "x": "продолжить без них и больше не спрашивать в этом обсуждении",
                     "s": "остановить (продолжить потом через debate resume)"})
        ch = self.ui.choose("Агенту нужно разрешение", body, opts)
        if ch == "s":
            raise Stopped
        if ch in ("c", "x"):
            self.f["declined"] += [f"{d['tool']}:{d['cmd']}" for d in new]
            if ch == "x":
                self.f["no_ask"] = True
            self._save_state()
            return None
        if ch == "w":
            pats = sugg
        elif ch == "e":
            pats = [f"Bash({d['cmd']})" for d in exact]
        else:
            raw = self.ui.text("Шаблоны через запятую или с новой строки, например: Bash(ssh:*), Bash(curl -s:*), WebFetch")
            pats = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
            if not pats:
                return None
        key = "grants_exec" if mode == "exec" else "grants"
        self.f[key] += [p for p in pats if p not in self.f[key]]
        self._apply_grants()
        where = "исполнителю" if mode == "exec" else "на этапе обсуждения и ревью"
        self._t(f"\n> **Пользователь разрешил ({where}):** {', '.join(pats)}\n")
        self._save_state()
        return pats

    def _count(self, res: TurnResult) -> None:
        """Суммирует токены, которые сообщил CLI (для итоговой строки и state.json)."""
        self.tokens[0] += res.tokens_in or 0
        self.tokens[1] += res.tokens_out or 0

    def _questions(self, role: str, data: dict, reask: Callable[[], dict]) -> dict:
        """Если агент задал вопросы — спрашивает пользователя и рассылает ответ участникам этапа.

        При verdict = need_user агент без ответа продолжать не может: после ответа его ход повторяется (reask),
        но не больше max_question_loops раз подряд.
        """
        loops = 0
        while True:
            qs = data.get("questions_for_user") or []
            need = str(data.get("verdict")).lower() == "need_user"
            if not qs and not need:
                return data
            if not qs:
                qs = [data.get("summary", "Агенту нужно ваше решение.")]
            label = f"{P.ROLE_RU[role]} · {self.roles[role].name}"
            ans = self.ui.text(f"{label} спрашивает:\n{P.fmt_questions(qs)}\n\nВаш ответ (пусто — «решайте сами»)")
            ans = ans or "Пользователь не ответил: решайте сами, выберите самый безопасный вариант."
            self._broadcast(f"Вопросы ({label}): {P.fmt_questions(qs)}\nОтвет пользователя: {ans}",
                            ("proposer", "critic") if role in ("proposer", "critic") else ("executor", "reviewer"))
            if not need:
                return data
            loops += 1
            if loops > int(self.cfg["debate"]["max_question_loops"]):
                return data
            data = reask()

    # ================= состояние и файлы =================
    def _broadcast(self, note: str, roles: tuple[str, ...]) -> None:
        """Кладёт комментарий пользователя в очередь агентам ролей `roles` (доставится с их следующим ходом)."""
        for r in roles:
            self.notes[r].append(note)
        self._t(f"\n> **Пользователь:** {note}\n")
        self._save_state()

    def _set_plan(self, d: dict) -> None:
        """Принимает план из ответа автора; версия растёт, только если текст плана изменился."""
        plan = d.get("plan")
        if isinstance(plan, str) and plan.strip():
            if plan.strip() != self.plan.strip():
                self.version += 1
            self.plan = plan.strip()
        elif not self.plan:
            self.plan = d.get("summary", "")
            self.version = 1
        self.last_responses = d.get("responses") or []
        (self.dir / f"plan.v{self.version}.md").write_text(self.plan + "\n", encoding="utf-8")

    def _plan_md(self, outcome: str) -> str:
        """Текст итогового plan.md: статус, план и открытые замечания критика."""
        status = ("согласован обоими агентами" if outcome == "consensus"
                  else "принят пользователем при открытых возражениях")
        s = f"# План (v{self.version})\n\nСтатус: {status}.\n\n{self.plan}\n"
        if self.minors:
            s += "\n## Замечания критика, оставшиеся открытыми\n\n" + P.fmt_points(self.minors) + "\n"
        return s

    def _write_plan(self, outcome: str) -> None:
        (self.dir / "plan.md").write_text(self._plan_md(outcome), encoding="utf-8")

    def _log_turn(self, label: str, role: str, data: dict, res: TurnResult) -> None:
        """Добавляет ход в transcript.md (человекочитаемая история обсуждения) и сохраняет состояние."""
        s = f"\n## {label}\n\n*вердикт:* `{data.get('verdict')}` · {res.duration_s:.0f} с\n\n{data.get('summary', '')}\n"
        if data.get("key_points"):
            s += "\n" + "\n".join(f"- {k}" for k in data["key_points"]) + "\n"
        if data.get("points"):
            s += "\n" + P.fmt_points(data["points"]) + "\n"
        if data.get("responses"):
            s += "\n" + P.fmt_responses(data["responses"]) + "\n"
        if data.get("questions_for_user"):
            s += "\nВопросы пользователю:\n" + P.fmt_questions(data["questions_for_user"]) + "\n"
        self._t(s)
        self._save_state()

    def _t(self, s: str) -> None:
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(s)

    def _save_state(self) -> None:
        """Атомарно (через временный файл) пишет state.json — всё, что нужно для debate resume."""
        state = {
            "schema": SCHEMA, "slug": self.slug, "repo": str(self.repo),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "flow": self.f, "plan": self.plan, "version": self.version,
            "last_responses": self.last_responses, "minors": self.minors, "notes": self.notes,
            "tokens": self.tokens,
            "sessions": {r: {"agent": a.name, "session_id": a.session_id, "started": a.started, "calls": a.calls}
                         for r, a in self.roles.items()},
        }
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "state.json")


# ================= старые обсуждения =================
def _upgrade_legacy(d: Path, st: dict) -> dict:
    """state.json версии до resume: восстанавливаем этап по файлам обсуждения."""
    raw = d / "raw"
    calls = {}
    for f in raw.glob("*-*-*.prompt.md") if raw.is_dir() else []:
        role, n = f.name.split("-")[0], f.name.split("-")[1]
        if n.isdigit():
            calls[role] = max(calls.get(role, 0), int(n))
    plan_md = d / "plan.md"
    version = st.get("version", 0)
    plan = ""
    pv = d / f"plan.v{version}.md"
    if pv.is_file():
        plan = pv.read_text(encoding="utf-8").strip()
    if plan_md.is_file():
        outcome = "consensus" if "согласован обоими" in plan_md.read_text(encoding="utf-8") else "user_accepted"
        flow = {"phase": "plan_ready", "outcome": outcome, "blind_done": True}
    elif version > 0:
        flow = {"phase": "debate", "next": "critic", "round": 0, "prev_open": None, "max_rounds": 4, "blind_done": True}
    else:
        flow = {"phase": "start", "blind_done": calls.get("critic", 0) > 0}
    for r, s in st.get("sessions", {}).items():
        s["calls"] = calls.get(r, 0)
    return {**st, "schema": SCHEMA, "flow": flow, "plan": plan, "version": version, "tokens": [0, 0]}


# ================= список обсуждений =================
def list_debates(repo: Path, out_dir: str) -> list[dict]:
    """Обсуждения репозитория для `debate list`, новые сверху."""
    rows = []
    base = repo.resolve() / out_dir
    if not base.is_dir():
        return rows
    for d in sorted(base.iterdir(), reverse=True):
        sf = d / "state.json"
        if not sf.is_file():
            continue
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        brief = (d / "brief.md").read_text(encoding="utf-8").strip() if (d / "brief.md").exists() else ""
        flow = st.get("flow") or {}
        legacy = st.get("schema") != SCHEMA
        if legacy:
            flow = _upgrade_legacy(d, json.loads(json.dumps(st)))["flow"]
        phase = flow.get("phase", "?")
        rows.append({
            "slug": d.name, "dir": d, "phase": phase,
            "phase_ru": PHASE_RU.get(phase, phase) + (" (старая версия)" if legacy else ""),
            "round": flow.get("round"), "version": st.get("version"), "updated": st.get("updated", ""),
            "brief": brief.splitlines()[0] if brief else "", "resumable": phase in PHASE_RU and phase != "done",
        })
    return rows
