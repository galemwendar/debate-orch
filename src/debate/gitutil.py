"""Небольшие обёртки над git: проверка репозитория, worktree для исполнителя и дифф для ревьюера."""
from __future__ import annotations

import subprocess
from pathlib import Path


def git(args: list[str], cwd: Path) -> str:
    """Запускает git в каталоге `cwd` и возвращает stdout; при ошибке — RuntimeError с текстом stderr."""
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def is_repo(path: Path) -> bool:
    """Находится ли `path` внутри рабочего дерева git."""
    try:
        return git(["rev-parse", "--is-inside-work-tree"], path).strip() == "true"
    except Exception:
        return False


def toplevel(path: Path) -> Path:
    """Корень репозитория, в котором лежит `path`."""
    return Path(git(["rev-parse", "--show-toplevel"], path).strip())


def add_worktree(repo: Path, slug: str) -> tuple[Path, str]:
    """Создаёт worktree рядом с репозиторием (`<репо>-wt-<slug>`) на новой ветке `debate/<slug>` от HEAD.

    Исполнитель работает там, не трогая вашу рабочую копию. Важно: worktree строится от последнего коммита —
    незакоммиченных правок и файлов вне git (данные, .env, модели) в нём нет.
    """
    branch = f"debate/{slug}"
    path = repo.parent / f"{repo.name}-wt-{slug}"
    git(["worktree", "add", "-b", branch, str(path), "HEAD"], repo)
    return path, branch


def staged_diff(path: Path, limit: int) -> tuple[str, str, bool]:
    """Индексирует все изменения (включая новые файлы) и возвращает (stat, diff, был ли дифф обрезан).

    Индексация нужна, чтобы новые файлы исполнителя попали в дифф для ревьюера. Коммит не делается —
    его делает пользователь после проверки. При исполнении без worktree (--in-place) в индекс попадут
    и ваши собственные незакоммиченные изменения.
    """
    git(["add", "-A"], path)
    stat = git(["diff", "--cached", "--stat"], path)
    diff = git(["diff", "--cached"], path)
    truncated = len(diff) > limit
    return stat, diff[:limit], truncated


def ignored(repo: Path, rel: str) -> bool:
    """Исключён ли путь через .gitignore."""
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(repo))
    return r.returncode == 0
