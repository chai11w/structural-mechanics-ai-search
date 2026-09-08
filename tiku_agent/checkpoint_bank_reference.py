"""Logical references to current question-bank files; construction performs no I/O."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class BankReferenceV1:
    bank_id: str
    chapter: str
    relative_key: str
    lookup_mode: str = "current"

    def __post_init__(self):
        if not isinstance(self.bank_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.bank_id):
            raise ValueError("invalid bank id")
        if not isinstance(self.chapter, str) or not self.chapter.strip() or len(self.chapter) > 100:
            raise ValueError("invalid bank chapter")
        key = self.relative_key
        if not isinstance(key, str) or not key or len(key) > 1000:
            raise ValueError("invalid bank relative key")
        parts = key.split("/")
        if (PureWindowsPath(key).drive or PurePosixPath(key).is_absolute()
                or any(part in {"", ".", ".."} or part != part.strip() or part.endswith(".") for part in parts)
                or re.search(r'[\\:<>"|?*\x00-\x1f\x7f]', key)
                or "%" in key):
            raise ValueError("unsafe bank relative key")
        for part in parts:
            if re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part):
                raise ValueError("reserved bank filename")
        if PurePosixPath(key).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            raise ValueError("unsupported bank media")
        if self.lookup_mode != "current":
            raise ValueError("unsupported bank lookup mode")

    def to_dict(self):
        return dict(bank_id=self.bank_id, chapter=self.chapter,
                    relative_key=self.relative_key, lookup_mode=self.lookup_mode)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"bank_id", "chapter", "relative_key", "lookup_mode"}:
            raise ValueError("invalid bank reference")
        return cls(**dict(value))


class CheckpointBankCatalog:
    def __init__(self, roots: Mapping[str, str | Path]):
        self.roots = dict(roots)
        for key, root in self.roots.items():
            BankReferenceV1(key, "chapter", "image.png")
            path = Path(root)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("bank root must be absolute")
            self.roots[key] = path
        self.roots = MappingProxyType(self.roots)

    def reference(self, path: str | Path, *, chapter: str) -> BankReferenceV1:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("bank image must have an absolute source location")
        matches = [(root, key) for key, root in self.roots.items() if path.is_relative_to(root)]
        if not matches:
            raise ValueError("image is outside configured banks")
        root, key = max(matches, key=lambda item: len(item[0].parts))
        return BankReferenceV1(key, chapter, path.relative_to(root).as_posix())

    def read(self, reference: BankReferenceV1) -> bytes:
        # Revalidate paths on every read. Never repair or search for a replacement.
        from tiku_agent.checkpoint_store import _reject_linked_path, _inspect_image
        from tiku_agent.checkpoint_contract import MAX_ARTIFACT_BYTES

        root = self.roots.get(reference.bank_id)
        if root is None:
            raise ValueError("bank is not configured")
        target = root.joinpath(*reference.relative_key.split("/"))
        _reject_linked_path(target)
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("bank reference escaped its root")
        with target.open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
        _inspect_image(content)
        return content
