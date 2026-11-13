"""Протокол обсуждения: промпты для каждого этапа, схемы ответов и разбор JSON-вердиктов.

Все договорённости с агентами живут здесь. Каждый ответ агента обязан заканчиваться блоком ```json с полем
`verdict`; по нему движок (engine.py) решает, кому ход дальше. Первая строка каждого промпта — «Stage: <имя>»:
по ней легко найти нужный вызов в логах raw/ (и на неё же ориентируются тестовые агенты).
"""
from __future__ import annotations

import json
import re

ROLE_RU = {"proposer": "Автор плана", "critic": "Критик", "executor": "Исполнитель", "reviewer": "Ревьюер"}

# Уровни замечаний, которые блокируют согласие. minor не мешает консенсусу и передаётся исполнителю отдельным списком.
BLOCKING = ("blocker", "major")

# Заголовок раздела plan.md с открытыми minor-замечаниями. Его пишет engine.py, а p_exec/p_review на него ссылаются,
# поэтому он задан в одном месте.
OPEN_MINORS_HEADING = "Замечания критика, оставшиеся открытыми"

# ---------------------------------------------------------------------------------------------------------------
# Язык
# ---------------------------------------------------------------------------------------------------------------
# Промпты написаны по-английски: модели точнее держат английские инструкции (особенно формат JSON), а латиница
# занимает заметно меньше токенов, чем кириллица, — экономия на каждом ходе. На каком языке агенты пишут текст
# (планы, замечания, вопросы), задаёт настройка `[rules] language`.
LANGUAGES = {"ru": "Russian", "en": "English", "uk": "Ukrainian", "de": "German", "fr": "French", "es": "Spanish",
             "it": "Italian", "pt": "Portuguese", "pl": "Polish", "zh": "Chinese", "ja": "Japanese", "ko": "Korean"}


def language_name(code: str) -> str:
    """Код языка (`ru`) → название для промпта (`Russian`); неизвестное значение передаётся как есть."""
    code = (code or "ru").strip()
    return LANGUAGES.get(code.lower(), code)


# ---------------------------------------------------------------------------------------------------------------
# Системные правила (добавляются к системному промпту CLI или, если CLI так не умеет, — в первое сообщение)
# ---------------------------------------------------------------------------------------------------------------

COMMON_RULES = """\
You are one of two AI agents in a structured discussion. \
An orchestrator program runs the discussion: it passes your replies to the other agent and calls in the user when needed.
Rules:
- Write all prose (summaries, plans, points, questions) in {language}. Keep JSON keys and enum values exactly as in the schema.
- Ground every claim in facts from the repository: files, lines, configs, results of lightweight checks. Cite them (path:line).
- Do not agree out of politeness. Agreeing means you checked yourself and found no significant problems.
- Do not reopen points that are already closed, and do not restate the other agent.
- User comments are mandatory.
- Ask the user (questions_for_user) only if you cannot proceed without an answer or the decision is critical: \
deleting or overwriting data, heavy or long-running jobs, changing the goal or scope, production access, money.
- During the discussion, shell commands are limited to a read-only list (ls, cat, head, tail, wc, grep, rg, git status/log/diff/show, pdfinfo, pdffonts, file, stat, etc.; use your file-search tools instead of find). Do not start a command with `cd …;` and do not chain commands with `;` or `&&` — run separate commands with absolute paths, otherwise they are rejected. If a check needs a command outside the list (network, ssh, running scripts), just run it: the orchestrator will show it to the user and ask for permission.
- End EVERY reply with exactly one ```json block following the schema in the request. Text before the block is allowed but keep it short."""

ROLE_RULES = {
    "proposer": "Your role: plan author. You propose a solution and refine it based on the critique. "
                "During the discussion, do not modify files and do not run anything heavy.",
    "critic": "Your role: independent critic. Look for errors, gaps, wrong assumptions and risks; verify against the code "
              "and data, not the author's words. During the discussion, do not modify files and do not run anything heavy.",
    "executor": "Your role: executor. Implement the agreed plan precisely, without improvising. "
                "Do not commit: leave the changes in the working tree.",
    "reviewer": "Your role: reviewer. Check the executor's changes for conformance to the plan, correctness and risks. "
                "Do not modify files.",
}


def system_rules(role: str, extra: str = "", language: str = "ru") -> str:
    """Правила для роли + язык ответов + дополнительные правила проекта из `[rules] extra`."""
    s = COMMON_RULES.format(language=language_name(language)) + "\n\n" + ROLE_RULES[role]
    if extra.strip():
        s += "\n\nAdditional project rules:\n" + extra.strip()
    return s


# ---------------------------------------------------------------------------------------------------------------
# Схемы ответов. verdict = need_user или непустой questions_for_user — сигнал оркестратору позвать пользователя.
# ---------------------------------------------------------------------------------------------------------------
Q_FIELD = '"questions_for_user": [{"question": "...", "why_critical": "..."}]'

SCHEMA_PROPOSAL = f"""```json
{{"verdict": "proposal | need_user",
 "summary": "2-4 sentences: the essence of the plan",
 "plan": "the FULL plan in markdown: goal, steps, affected files, acceptance criteria, risks",
 {Q_FIELD}}}
```"""

SCHEMA_ANALYSIS = f"""```json
{{"verdict": "analysis | need_user",
 "summary": "2-4 sentences",
 "key_points": ["what matters, where the risks are, how you would solve it"],
 {Q_FIELD}}}
```"""

SCHEMA_CRITIQUE = f"""```json
{{"verdict": "agree | disagree | need_user",
 "summary": "2-4 sentences",
 "points": [{{"id": "C1", "severity": "blocker | major | minor", "claim": "what the problem is",
             "evidence": "path:line or check result", "suggestion": "how to fix it"}}],
 {Q_FIELD}}}
```
agree — no significant problems (only minor points allowed). disagree — at least one blocker or major.
Continue point numbering across rounds (C1, C2, …); a point that stays open keeps its id."""

SCHEMA_RESPONSE = f"""```json
{{"verdict": "revised | hold | need_user",
 "summary": "what you changed and why",
 "plan": "the FULL current version of the plan in markdown",
 "responses": [{{"id": "C1", "decision": "accepted | rejected", "why": "reasoning; for rejected — with evidence"}}],
 {Q_FIELD}}}
```
revised — the plan was changed; hold — the plan is unchanged, every point rejected with reasoning."""

SCHEMA_EXEC = f"""```json
{{"verdict": "done | blocked | need_user",
 "summary": "what was done",
 "changed_files": ["..."],
 "checks_run": ["which checks you ran and their results"],
 {Q_FIELD}}}
```"""

SCHEMA_REVIEW = f"""```json
{{"verdict": "approve | changes_requested | need_user",
 "summary": "2-4 sentences",
 "points": [{{"id": "R1", "severity": "blocker | major | minor", "claim": "...", "evidence": "path:line", "suggestion": "..."}}],
 {Q_FIELD}}}
```"""


# ---------------------------------------------------------------------------------------------------------------
# Промпты этапов. Первая строка каждого — «Stage: <имя>».
# ---------------------------------------------------------------------------------------------------------------
def p_blind(brief: str) -> str:
    """Критик разбирает задачу сам, ещё не видя плана: потом ему есть с чем сравнить, и он реже поддакивает."""
    return f"""Stage: independent_analysis

Task from the user:
<task>
{brief}
</task>

The other agent is preparing a plan right now. Without waiting for it, do your own independent analysis: study the \
repository, what matters here, where the risks are, how you would solve it. This is your baseline: later you will \
compare the author's plan against it.

Response schema:
{SCHEMA_ANALYSIS}"""


def p_propose(brief: str) -> str:
    """Автор готовит первую версию плана."""
    return f"""Stage: propose

Task from the user:
<task>
{brief}
</task>

Study the repository and propose a plan. An independent critic will review it.

Response schema:
{SCHEMA_PROPOSAL}"""


def p_critique(author: str, version: int, plan: str, responses: list | None) -> str:
    """Критик проверяет текущую версию плана; видит ответы автора на свои прошлые пункты."""
    resp = ""
    if responses:
        resp = "\n\nThe author's responses to your previous points:\n" + fmt_responses(responses)
    return f"""Stage: critique

The author ({author}) proposes a plan, version {version}:
<plan>
{plan}
</plan>{resp}

Verify the plan against the code and data and compare it with your independent analysis. If the author closed a point \
with good reason, do not raise it again.

Response schema:
{SCHEMA_CRITIQUE}"""


def p_respond(critic: str, crit: dict) -> str:
    """Автор отвечает на каждый существенный пункт: принять и исправить или отклонить с доказательствами."""
    return f"""Stage: respond

The critic ({critic}) replied:
{crit.get("summary", "")}

Points:
{fmt_points(crit.get("points", []))}

For each blocker/major point: accept it (and fix the plan) or reject it with evidence. \
minor — at your discretion. Return the full current version of the plan.

Response schema:
{SCHEMA_RESPONSE}"""


def p_revise_with_note() -> str:
    """Доработка плана по комментарию пользователя (сам комментарий идёт выше, блоком notes_block)."""
    return f"""Stage: respond

The user sent a comment on the plan (see above). Revise the plan accordingly and return the full current version.

Response schema:
{SCHEMA_RESPONSE}"""


def p_continue() -> str:
    """Продолжение после ответа пользователя на вопросы агента."""
    return """Stage: continue

The user answered the questions (see above). Continue taking the answer into account and reply using the same schema as last time."""


def p_after_grant(patterns: list[str]) -> str:
    """Повтор хода после того, как пользователь выдал разрешения на отклонённые команды."""
    plist = "\n".join(f"- {x}" for x in patterns)
    return f"""Stage: continue_after_grant

The user allowed commands matching these patterns for this discussion:
{plist}
Run the checks that failed because they were denied, and give an updated FULL reply using the schema required by this
turn's task (do not refer to your previous reply — the orchestrator only takes the new JSON block)."""


# Команды, у которых осмысленный шаблон разрешения — по двум словам (`git status`, `docker compose`), а не по одному:
# разрешать `git:*` целиком было бы слишком широко.
_TWO_WORD = {"git", "docker", "docker-compose", "kubectl", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "poetry",
             "systemctl", "apt", "cargo", "go"}


def split_raw(cmd: str) -> list[str]:
    """Склейку shell-команд → исходные тексты команд. Делит по `;`, `&&`, `||`, `|`, `&` и переводам строк,
    но не внутри кавычек и не после `\\` (иначе `rg 'a; b'` давал бы мусорные «команды» вроде `b'`)."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(cmd or ""):
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch == "\\" and i + 1 < len(cmd):
            cur += [ch, cmd[i + 1]]
            i += 1
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in ";|&\n":
            # `2>&1`, `>&2` — перенаправление, а не разделитель
            if ch == "&" and cur and cur[-1] in "<>":
                cur.append(ch)
            else:
                parts.append("".join(cur))
                cur = []
                if ch in "|&" and i + 1 < len(cmd) and cmd[i + 1] == ch:
                    i += 1
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [x.strip() for x in parts if x.strip()]


# Первое слово команды, для которого есть смысл предлагать шаблон: имя программы или путь к ней.
_CMD_WORD = re.compile(r"^[\w.+/-]+$")


def split_commands(cmd: str) -> list[list[str]]:
    """Склейку shell-команд (`a && b; c | d`) → список команд, каждая — список слов без присваиваний `X=1`.

    Перенаправления (`2>/dev/null`) остаются словами команды. Пропускаются только `cd`, `export`, `set`, `true`:
    они ничего не читают и не пишут, и CLI их обычно не проверяют. `echo` НЕ пропускается — его нет в списке
    разрешённых, и OpenCode/Claude отклоняют склейку именно из-за него. Части, первое слово которых не похоже
    на имя программы (обрывок строки, подстановка `$(…)`), отбрасываются — шаблон по ним бессмыслен.
    """
    import shlex
    out: list[list[str]] = []
    for part in split_raw(cmd):
        try:
            toks = shlex.split(part)
        except ValueError:
            toks = part.split()
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if toks and toks[0] not in ("cd", "export", "set", "true") and _CMD_WORD.match(toks[0]):
            out.append(toks)
    return out


def exact_patterns(denials: list[dict]) -> list[str]:
    """«Разрешить ровно эти команды»: каждая команда склейки — отдельным точным шаблоном `Bash(<команда>)`.

    Шаблон на всю склейку целиком бесполезен: CLI проверяют команды по одной и до него не доходят.
    """
    out: list[str] = []
    for d in denials:
        if d.get("tool") != "Bash":
            continue
        for part in split_raw(d.get("cmd", "")):
            # исходный текст команды, с кавычками: CLI сравнивают точный шаблон с ним
            if split_commands(part):
                pat = f"Bash({part})"
                if pat not in out:
                    out.append(pat)
    return out


def suggest_patterns(denials: list[dict]) -> list[str]:
    """Предлагает пользователю как можно более узкие шаблоны разрешений для отклонённых действий.

    Shell: по первому слову команды (`Bash(ssh:*)`), для git/docker/npm… — по двум (`Bash(git status:*)`);
    склейки через `&&`, `;`, `|` разбираются по частям (см. split_commands).
    Файлы вне рабочего каталога: только их папка (`Write(//tmp/x/**)`). Сайты: только домен.
    """
    out: list[str] = []
    for d in denials:
        if d.get("tool") != "Bash":
            tool, target = d.get("tool") or "", str(d.get("cmd", ""))
            if not tool:
                continue
            if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit") and target.startswith("/"):
                # файл вне рабочего каталога: разрешаем только его папку (// — абсолютный путь у Claude Code)
                parent = target.rsplit("/", 1)[0] or ""
                pat = f"{tool}(/{parent}/**)"
            elif tool == "WebFetch" and re.match(r"https?://", target):
                host = re.sub(r"^https?://([^/:]+).*", r"\1", target)
                pat = f"WebFetch(domain:{host})"
            elif tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"):
                # без абсолютного пути или адреса узкий шаблон не построить, а голое `Write` открыло бы запись
                # везде (так в одном из прогонов критик и ревьюер получили право писать файлы) — не предлагаем
                continue
            else:
                pat = tool
            if pat not in out:
                out.append(pat)
            continue
        for toks in split_commands(d.get("cmd", "")):
            prefix = " ".join(toks[:2]) if toks[0] in _TWO_WORD and len(toks) > 1 and not toks[1].startswith("-") else toks[0]
            pat = f"Bash({prefix}:*)"
            if pat not in out:
                out.append(pat)
    return out


def p_resume_cancelled(reason: str) -> str:
    """Продолжение хода, который CLI оборвал из-за запрещённого действия (так делает Grok в headless-режиме)."""
    return f"""Stage: continue_after_denial

Your previous turn stopped before producing a reply: {reason}
That action is not allowed at this stage. Continue without it: the read-only commands from the allowed list and your
file-reading tools are still available; anything that can only be verified with the denied action, mark in your reply
as unverified ("needs checking: …"). Then give a full reply using the schema required by this turn's task."""


# ---------------------------------------------------------------------------------------------------------------
# Запасные агенты
# ---------------------------------------------------------------------------------------------------------------
# Ошибки, после которых агент в ближайшее время не ответит: кончился лимит, не выполнен вход, CLI не найден.
# Тогда роль переходит к следующему агенту из цепочки ([roles] critic = ["grok", "opencode"]) без вопроса.
_UNAVAILABLE = re.compile(
    r"rate.?limit|usage.?limit|limit (?:reached|exceeded|hit)|quota|too many requests|\b429\b|"
    r"insufficient (?:credit|balance|quota|funds)|out of credits|credits? (?:exhausted|depleted)|exceeded your|"
    r"billing|subscription|not logged in|log ?in required|please (?:log|sign) ?in|unauthori[sz]ed|\b401\b|"
    r"invalid api key|authentication|not found|лимит|квот", re.I)


def is_unavailable(message: str) -> bool:
    """Похоже ли сообщение об ошибке агента на «лимит / нет входа / нет CLI» (а не на сбой конкретного хода)."""
    return bool(_UNAVAILABLE.search(message or ""))


def p_takeover(role: str, brief: str, plan: str, version: int, workdir: str | None = None) -> str:
    """Вводная для агента, который подхватил роль посреди обсуждения: истории прошлого агента у него нет."""
    where = f"\nWorking directory: {workdir}" if workdir else ""
    plan_block = f"\n\nCurrent plan (version {version}):\n<plan>\n{plan}\n</plan>" if plan else ""
    return f"""Context: you are taking over the role "{role}" in the middle of an ongoing discussion from another agent
(it became unavailable). You do not have its history; everything you need is below and in the task that follows.

Task from the user:
<task>
{brief}
</task>{plan_block}{where}

---

"""


# Если в ответе не нашлось разбираемого JSON-блока, агента один раз просят повторить только блок.
FIX_JSON = """Stage: fix_json

The orchestrator could not parse the JSON block in your previous reply. Repeat the reply as ONLY one valid ```json block using the same schema, with no text around it."""


def replan_note(user_note: str, exec_summary: str, review: dict | None) -> str:
    """Возврат плана на обсуждение с этапа исполнения: что уже сделано и что сказал ревьюер — автору и критику."""
    rv = ""
    if review:
        rv = f"\n\nLast review ({review.get('verdict', '?')}): {review.get('summary', '')}\n{fmt_points(review.get('points', []))}"
    note = user_note.strip() or "Revisit the plan in light of what execution and review revealed."
    return (f"The plan is back for discussion after execution. User's request: {note}\n\n"
            f"The executor has already implemented the current plan version in the working tree (changes are not "
            f"committed). Executor's last report: {exec_summary or '—'}{rv}\n\n"
            "Revise the plan where execution or review showed it is wrong or incomplete; keep what already works. "
            "The executor will then bring the existing changes in line with the new version.")


def p_exec_update(plan: str, workdir: str) -> str:
    """Исполнение после пересмотра плана: изменения прошлой версии уже в рабочем каталоге."""
    return f"""Stage: execute

The plan was revised after your previous implementation (new version below). Working directory: {workdir}.
Your earlier changes for the previous version are already in the working tree. Bring them in line with the new
version: change only what differs, do not redo what already matches. Work only inside the working directory.
Run heavy or long jobs only if the plan explicitly requires it. Do not commit.

<plan>
{plan}
</plan>

Response schema:
{SCHEMA_EXEC}"""


def p_exec(plan: str, workdir: str) -> str:
    """Исполнитель реализует итоговый plan.md в рабочем каталоге (репозиторий на ветке debate/… или worktree)."""
    return f"""Stage: execute

Implement the agreed plan. Working directory: {workdir}. Work only inside it.
Run heavy or long jobs only if the plan explicitly requires it. Do not commit.
If the plan has a section "{OPEN_MINORS_HEADING}" (the critic's remaining open points), either address each point
or explain in summary why you did not (for example, it is out of the plan's scope).

<plan>
{plan}
</plan>

Response schema:
{SCHEMA_EXEC}"""


def p_review(plan: str, exec_summary: str, stat: str, diff: str, truncated: bool) -> str:
    """Ревьюер получает план, отчёт исполнителя и дифф (длинный дифф обрезается, файлы доступны в каталоге)."""
    note = "\n(diff truncated — see the files in the working directory for the rest)" if truncated else ""
    return f"""Stage: review

The executor has implemented the plan. Review the changes: conformance to the plan, bugs, risks, anything missing.
The executor had to address the critic's open points from the plan (section "{OPEN_MINORS_HEADING}") or explain
in the report why not — check that too.

<plan>
{plan}
</plan>

Executor's report: {exec_summary}

Changed files:
{stat}

Diff:{note}
```diff
{diff}
```

Response schema:
{SCHEMA_REVIEW}"""


def p_fix(review: dict) -> str:
    """Исполнитель правит код по замечаниям ревьюера."""
    return f"""Stage: fix

The reviewer requested changes:
{review.get("summary", "")}

{fmt_points(review.get("points", []))}

Fix blocker/major points (minor — at your discretion). If you disagree with a point, explain in summary.

Response schema:
{SCHEMA_EXEC}"""


def notes_block(notes: list[str]) -> str:
    """Комментарии пользователя, ещё не доставленные агенту, — ставятся перед промптом его следующего хода."""
    if not notes:
        return ""
    return "User comments and decisions (mandatory):\n" + "\n".join(f"- {n}" for n in notes) + "\n\n"


# ---------------------------------------------------------------------------------------------------------------
# Разбор ответов
# ---------------------------------------------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> dict | None:
    """Последний корректный ```json-блок ответа; если блоков нет — последний JSON-объект с полем verdict."""
    if not text:
        return None
    for block in reversed(_JSON_BLOCK.findall(text)):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return normalize_points(obj)
        except Exception:
            pass
    dec = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return normalize_points(obj)
    return None


# Модели не всегда держат имена полей из схемы (OpenCode-ревьюер прислал `file`/`text` вместо `evidence`/`claim`).
# Синонимы приводятся к схеме, чтобы замечание не потерялось ни в таблице, ни в промпте исполнителю.
_POINT_ALIASES = {
    "claim": ("text", "description", "issue", "problem", "message", "comment", "summary", "title"),
    "evidence": ("file", "location", "where", "path", "ref", "proof"),
    "suggestion": ("fix", "recommendation", "proposal", "action", "how_to_fix"),
    "severity": ("level", "priority"),
}


def normalize_points(data: dict | None) -> dict | None:
    """Приводит поля замечаний к схеме (claim/evidence/suggestion/severity); исходные ключи не удаляются."""
    if not isinstance(data, dict) or not isinstance(data.get("points"), list):
        return data
    pts = []
    for pt in data["points"]:
        if isinstance(pt, str):
            pt = {"claim": pt}
        if not isinstance(pt, dict):
            continue
        for key, alts in _POINT_ALIASES.items():
            if not pt.get(key):
                for a in alts:
                    if pt.get(a):
                        pt[key] = pt[a]
                        break
        if isinstance(pt.get("severity"), str):
            pt["severity"] = pt["severity"].strip().lower()
        pts.append(pt)
    data["points"] = pts
    return data


def blocking_points(data: dict) -> list[dict]:
    """Замечания уровня blocker/major — пока они есть, согласия нет."""
    return [p for p in data.get("points") or [] if str(p.get("severity", "")).lower() in BLOCKING]


def minor_points(data: dict) -> list[dict]:
    return [p for p in data.get("points") or [] if str(p.get("severity", "")).lower() not in BLOCKING]


# ---------- форматирование для промптов и transcript.md ----------
def fmt_points(points: list) -> str:
    if not points:
        return "(none)"
    lines = []
    for p in points:
        lines.append(f"- [{p.get('id', '?')}] ({p.get('severity', '?')}) {p.get('claim', '')}")
        if p.get("evidence"):
            lines.append(f"  evidence: {p['evidence']}")
        if p.get("suggestion"):
            lines.append(f"  suggestion: {p['suggestion']}")
    return "\n".join(lines)


def fmt_responses(responses: list) -> str:
    return "\n".join(f"- [{r.get('id', '?')}] {r.get('decision', '?')}: {r.get('why', '')}" for r in responses) or "(none)"


def fmt_questions(qs: list) -> str:
    out = []
    for i, q in enumerate(qs, 1):
        if isinstance(q, str):
            out.append(f"{i}. {q}")
        else:
            out.append(f"{i}. {q.get('question', '')}" + (f"\n   why it matters: {q['why_critical']}" if q.get("why_critical") else ""))
    return "\n".join(out)


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(text: str, limit: int = 40) -> str:
    """Имя папки обсуждения из первой строки задачи: транслитерация, латиница, цифры и дефисы."""
    s = "".join(_TR.get(ch, ch) for ch in text.lower().split("\n")[0])
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:limit].rstrip("-")) or "task"
