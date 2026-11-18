"""Reordena lo que ya está copiado según la organización elegida (por ejemplo, de «2025/2025-03» a «2025-03»).

- Solo MUEVE archivos dentro del mismo disco (instantáneo, sin copiar ni recomprimir): la calidad
  y el SHA-256 no cambian.
- Actualiza el catálogo, así la app sigue sabiendo qué ya copió (no vuelve a bajar nada).
- Mueve también la copia compatible (JPG/MP4) y la copia espejo, si existen.
- Opcionalmente borra los .json de metadatos que generó la app (nunca toca otros archivos).
- Al final elimina las carpetas que quedaron vacías.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from ..config import LAYOUT_FLAT, LAYOUT_MIRROR
from ..transfer.compat import COMPAT_DIR, compat_rel
from .filetimes import set_file_times
from .layout import folder_for_date, with_suffix_number


@dataclass
class ReorgReport:
    moved: int = 0
    already_ok: int = 0
    sidecars_removed: int = 0
    empty_dirs_removed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def _target_folder(capture_iso: Optional[str], remote_path: str, layout: str) -> str:
    if layout == LAYOUT_FLAT:
        return ""
    if layout == LAYOUT_MIRROR:
        if remote_path.startswith("icloud/"):
            return "iCloud"
        return remote_path.rsplit("/", 1)[0]
    d = None
    if capture_iso:
        try:
            d = datetime.fromisoformat(capture_iso)
        except ValueError:
            d = None
    return folder_for_date(d, layout)


def plan_moves(manifest, layout: str, with_dates: bool = False) -> list:
    """(remote_path, ruta actual, ruta nueva[, fecha]) para cada archivo que está en otra carpeta."""
    with manifest._lock:  # noqa: SLF001
        rows = manifest._con.execute(  # noqa: SLF001
            "SELECT f.remote_path, f.local_path, i.capture_date FROM files f "
            "LEFT JOIN items i ON i.key = f.item_key").fetchall()
    moves = []
    for remote, local, cap in rows:
        name = PurePosixPath(local).name
        folder = _target_folder(cap, remote, layout)
        new = f"{folder}/{name}" if folder else name
        if new != local:
            moves.append((remote, local, new, cap) if with_dates else (remote, local, new))
    return moves


def count_pending(manifest, layout: str) -> int:
    return len(plan_moves(manifest, layout))


def count_sidecars(manifest) -> int:
    root = manifest.root
    return sum(1 for f in manifest.all_files() if (root / (f["local_path"] + ".json")).exists())


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def reorganize(manifest, layout: str, remove_sidecars: bool, mirrors: list[str],
               progress: Callable[[int, int, str], None]) -> ReorgReport:
    rep = ReorgReport()
    root = manifest.root
    moves = plan_moves(manifest, layout, with_dates=True)
    touched_dirs: set[Path] = set()
    for i, (remote, old, new, cap) in enumerate(moves, start=1):
        progress(i, len(moves), PurePosixPath(old).name)
        src = root / old
        if not src.exists():
            rep.failed.append((old, "no existe en el disco"))
            continue
        # destino libre (si ya hay otro archivo con ese nombre, se agrega _2, _3…)
        target, n = new, 1
        while (root / target).exists() or (manifest.local_path_owner(target) not in (None, remote)):
            n += 1
            target = with_suffix_number(new, n)
        try:
            _move(src, root / target)
            touched_dirs.add(src.parent)
            if cap:
                try:
                    set_file_times(root / target, datetime.fromisoformat(cap))
                except (ValueError, OSError):
                    pass
            with manifest._lock:  # noqa: SLF001
                manifest._con.execute("UPDATE files SET local_path=? WHERE remote_path=?", (target, remote))  # noqa: SLF001
                manifest._con.commit()  # noqa: SLF001
            # copia compatible JPG/MP4
            oc, nc = compat_rel(old), compat_rel(target)
            if oc and nc and (root / oc).exists():
                _move(root / oc, root / nc)
                touched_dirs.add((root / oc).parent)
            # .json de metadatos de la app
            side = root / (old + ".json")
            if side.exists():
                if remove_sidecars:
                    side.unlink()
                    rep.sidecars_removed += 1
                else:
                    _move(side, root / (target + ".json"))
            # copias espejo
            for m in mirrors or []:
                ms = Path(m) / old
                if ms.exists():
                    _move(ms, Path(m) / target)
                    touched_dirs.add(ms.parent)
                    with manifest._lock:  # noqa: SLF001
                        manifest._con.execute("UPDATE mirrors SET local_path=? WHERE mirror_root=? AND local_path=?",  # noqa: SLF001
                                              (target, m, old))
                        manifest._con.commit()  # noqa: SLF001
            rep.moved += 1
        except OSError as e:
            rep.failed.append((old, str(e)))

    if remove_sidecars:   # .json que hayan quedado en archivos que no se movieron
        registered = {r["local_path"] for r in manifest.all_files()}
        for rel in registered:
            side = root / (rel + ".json")
            if side.exists():
                side.unlink()
                rep.sidecars_removed += 1

    # carpetas que quedaron vacías por la mudanza (solo esas y sus padres; nunca otras tuyas)
    bases = [root.resolve()] + [Path(m).resolve() for m in mirrors or []]
    for d in sorted(touched_dirs, key=lambda p: -len(p.parts)):
        d = d.resolve()
        while d not in bases and any(b in d.parents for b in bases) and ".vault" not in d.parts:
            try:
                if any(d.iterdir()):
                    break
                d.rmdir()
                rep.empty_dirs_removed += 1
            except OSError:
                break
            d = d.parent
    rep.already_ok = len(manifest.all_files()) - rep.moved - len(rep.failed)
    return rep


__all__ = ["reorganize", "count_pending", "plan_moves", "ReorgReport", "COMPAT_DIR"]
