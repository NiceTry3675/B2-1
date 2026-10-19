"""백업과 복원 (보너스 5.1).

- ``backup``: 모든 데이터 파일을 ``<data-dir>/backups/backup-YYYYMMDD-HHMMSS.zip`` 하나로 묶는다.
  zip도 임시 파일에 만든 뒤 교체하므로, 만드는 도중 실패해도 깨진 zip이 남지 않는다.
- ``backup restore``: 복원하기 직전에 현재 상태를 자동으로 백업한다. 복원 자체도 되돌릴 수 있다.
- zip 안에서는 이미 알고 있는 데이터 파일 이름만 꺼낸다. ``../../etc/passwd`` 같은 경로가
  들어 있어도 데이터 폴더 밖으로 쓰지 않는다 (zip slip 방지).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .decorators import timed
from .errors import NotFoundError, ValidationError
from .services import Clock
from .storage import AtomicWriter, SequenceStore

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "backup-"
BACKUP_SUFFIX = ".zip"
MANIFEST_NAME = "manifest.json"
_NAME_RE = re.compile(r"backup-(\d{8}-\d{6})(?:-(\d+))?\.zip")
LIST_HINT = "'backup list'로 복원할 수 있는 백업 파일을 확인하세요."


def _newest_first_key(path: Path) -> Tuple[str, int]:
    """backup-20261018-235501-2.zip → ("20261018-235501", 2). 같은 초에 만든 백업도 나중 것이 먼저 온다."""
    match = _NAME_RE.fullmatch(path.name)
    if match is None:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 1))


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size: int
    files: List[str]


@dataclass(frozen=True)
class RestoreReport:
    restored_from: Path
    safety_backup: BackupInfo
    files: List[str]


class BackupService:
    def __init__(
        self,
        data_dir: Path,
        file_names: Sequence[str],
        *,
        sequences: Optional[SequenceStore] = None,
        sequence_keys: Sequence[str] = (),
        now: Clock = dt.datetime.now,
    ) -> None:
        self._data_dir = data_dir
        self._file_names = list(file_names)
        self._sequences = sequences
        self._sequence_keys = list(sequence_keys)
        self._now = now

    @property
    def backup_dir(self) -> Path:
        return self._data_dir / BACKUP_DIR_NAME

    @timed
    def create(self) -> BackupInfo:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        moment = self._now()
        path = self._unique_path(moment.strftime("%Y%m%d-%H%M%S"))
        included = [name for name in self._file_names if (self._data_dir / name).is_file()]
        manifest = {"app_version": __version__, "created_at": moment.isoformat(timespec="seconds"), "files": included}
        with AtomicWriter(path) as writer:
            with zipfile.ZipFile(writer.stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in included:
                    archive.write(self._data_dir / name, arcname=name)
                archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        return BackupInfo(path, path.stat().st_size, included)

    def list_backups(self) -> List[BackupInfo]:
        if not self.backup_dir.is_dir():
            return []
        infos: List[BackupInfo] = []
        candidates = sorted(self.backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"), key=_newest_first_key, reverse=True)
        for path in candidates:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
                with zipfile.ZipFile(path) as archive:
                    files = [name for name in archive.namelist() if name in self._file_names]
            except (zipfile.BadZipFile, OSError):
                files = []  # 손상되었거나 읽을 수 없는 백업도 목록에는 보여 준다 (파일 0개).
                size = path.stat().st_size if path.exists() else 0
            infos.append(BackupInfo(path, size, files))
        return infos

    @timed
    def restore(self, name: str) -> RestoreReport:
        source = self._resolve(name)
        if not zipfile.is_zipfile(source):
            raise ValidationError(f"올바른 백업 파일(zip)이 아닙니다: {source.name}", hint=LIST_HINT)
        with zipfile.ZipFile(source) as archive:
            try:
                broken = archive.testzip()  # 각 파일의 압축을 풀어 보며 CRC를 검사한다.
            except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
                raise ValidationError(f"백업 파일이 손상되었습니다 ({exc}).", hint="다른 백업 파일로 복원하세요.") from None
            if broken is not None:
                raise ValidationError(f"백업 파일이 손상되었습니다 (손상된 항목: {broken}).", hint="다른 백업 파일로 복원하세요.")
            members = [member for member in archive.namelist() if member in self._file_names]
            if not members:
                raise ValidationError(f"백업에 복원할 데이터 파일이 없습니다: {source.name}", hint=LIST_HINT)

            safety = self.create()  # 복원하기 전에 지금 상태를 백업해 둔다.
            issued = self._issued_ids()
            for member in members:
                with AtomicWriter(self._data_dir / member) as writer, archive.open(member) as data:
                    shutil.copyfileobj(data, writer.stream)
        self._keep_issued_ids(issued)
        return RestoreReport(source, safety, members)

    def _issued_ids(self) -> Dict[str, int]:
        if self._sequences is None:
            return {}
        return {key: self._sequences.current(key) for key in self._sequence_keys}

    def _keep_issued_ids(self, issued: Dict[str, int]) -> None:
        """예전 meta.jsonl이 복원돼도 '지금까지 발급한 최대 id'는 낮아지지 않게 한다.

        그렇지 않으면 복원 뒤 새 거래가, 복원 직전에 있던(안전 백업에만 남은) 거래와 같은 id를 받는다.
        """
        if self._sequences is None:
            return
        for key, value in issued.items():
            if value:
                self._sequences.commit(key, value)

    def _resolve(self, name: str) -> Path:
        candidate = Path(name)
        if not candidate.is_absolute() and len(candidate.parts) == 1:
            candidate = self.backup_dir / candidate  # 파일 이름만 주면 백업 폴더에서 찾는다.
        if not candidate.is_file():
            raise NotFoundError(f"백업 파일을 찾을 수 없습니다: {name}", hint=LIST_HINT)
        return candidate

    def _unique_path(self, stamp: str) -> Path:
        path = self.backup_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
        counter = 2
        while path.exists():  # 같은 초에 두 번 백업하면 -2, -3을 붙인다.
            path = self.backup_dir / f"{BACKUP_PREFIX}{stamp}-{counter}{BACKUP_SUFFIX}"
            counter += 1
        return path
