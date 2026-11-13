"""Небольшие обёртки над git: ветка или worktree для исполнителя, проверка незакоммиченного, дифф для ревьюера."""
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


def _excl(exclude: list[str]) -> list[str]:
    """pathspec «всё, кроме»: `. :(exclude).debate` — служебные файлы обсуждения не трогаем."""
    return ["--", ".", *[f":(exclude){x}" for x in exclude]] if exclude else []


def current_branch(path: Path) -> str | None:
    """Текущая ветка; None — если HEAD отсоединён."""
    name = git(["rev-parse", "--abbrev-ref", "HEAD"], path).strip()
    return None if name == "HEAD" else name


def has_branch(path: Path, branch: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          cwd=str(path), capture_output=True).returncode == 0


def dirty(path: Path, exclude: list[str] | None = None) -> str:
    """`git status --short` без служебных путей; пусто — рабочее дерево чистое."""
    return git(["status", "--short", "--untracked-files=all", *_excl(exclude or [])], path).strip()


def stash(path: Path, message: str, exclude: list[str] | None = None) -> None:
    """Прячет все незакоммиченные изменения, включая новые файлы (`git stash -u`), кроме служебных путей."""
    git(["stash", "push", "-u", "-m", message, *_excl(exclude or [])], path)


def switch_branch(path: Path, branch: str) -> bool:
    """Переключает рабочую копию на ветку `branch`, создавая её от HEAD, если её нет. True — ветка создана."""
    if has_branch(path, branch):
        git(["switch", branch], path)
        return False
    git(["switch", "-c", branch], path)
    return True


def staged_diff(path: Path, limit: int, exclude: list[str] | None = None) -> tuple[str, str, bool]:
    """Индексирует все изменения (включая новые файлы) и возвращает (stat, diff, был ли дифф обрезан).

    Индексация нужна, чтобы новые файлы исполнителя попали в дифф для ревьюера. Коммит не делается —
    его делает пользователь после проверки. `exclude` — служебные пути (каталог .debate), их не индексируем.
    Перед исполнением в репозитории оркестратор проверяет, что незакоммиченных изменений нет, — иначе
    в индекс попали бы и ваши правки.
    """
    git(["add", "-A", *_excl(exclude or [])], path)
    stat = git(["diff", "--cached", "--stat"], path)
    diff = git(["diff", "--cached"], path)
    truncated = len(diff) > limit
    return stat, diff[:limit], truncated


def ignored(repo: Path, rel: str) -> bool:
    """Исключён ли путь через .gitignore."""
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(repo))
    return r.returncode == 0
