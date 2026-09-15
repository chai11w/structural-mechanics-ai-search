"""Writer-owned, immutable content objects shared by version directory entries.

Only this module creates object links. Candidate workers may read/link seeded
objects, but must never open a shared file for writing. Existing independent
versions remain readable and are not rewritten during migration.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4

MARKER = '.bank-object-store.json'
HASH = re.compile(r'[0-9a-f]{64}')
# NTFS allows at most 1,024 names per file. Leave room for concurrent links and
# roll to another identical object only when a segment reaches this threshold.
MAX_OBJECT_LINKS = 900


def object_store(directory):
    from .bank_publication import reject_links, PublicationError
    directory = Path(directory).absolute()
    for parent in (directory, *directory.parents):
        marker = parent / MARKER
        if not marker.exists():
            continue
        reject_links(marker)
        if marker.stat().st_size > 8192:
            raise PublicationError('invalid-object-store')
        value = json.loads(marker.read_bytes())
        if set(value) != {'schema', 'objects'} or value['schema'] != 1:
            raise PublicationError('invalid-object-store')
        objects = reject_links(Path(value['objects']))
        if objects.name != 'objects' or not objects.is_dir():
            raise PublicationError('invalid-object-store')
        owner_marker = reject_links(objects.parent / MARKER)
        if json.loads(owner_marker.read_bytes()) != value:
            raise PublicationError('invalid-object-store')
        return objects
    return None


def initialize_objects(root, private):
    from .bank_publication import atomic_json, reject_links, PublicationError
    if root.stat().st_dev != private.stat().st_dev:
        raise PublicationError('incremental-storage-requires-same-volume')
    objects = reject_links(root / 'objects')
    objects.mkdir(exist_ok=True)
    value = {'schema': 1, 'objects': str(objects)}
    for directory in (root, private):
        marker = directory / MARKER
        if marker.exists():
            reject_links(marker)
            if json.loads(marker.read_bytes()) != value:
                raise PublicationError('object-store-binding-changed')
        else:
            atomic_json(marker, value)
    return objects


def object_path(objects, sha256, segment=0):
    from .bank_publication import PublicationError
    if not HASH.fullmatch(sha256):
        raise PublicationError('invalid-object-digest')
    # The binding validates the object root and its ancestors once per operation.
    # Inspect the two new components, without repeatedly resolving that same chain.
    target = objects / sha256[:2] / (sha256 + (f'.{segment}' if segment else ''))
    for path in (target.parent, target):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PublicationError('linked-path')
    return target


def available_object(objects, sha256):
    segment = 0
    while True:
        target = object_path(objects, sha256, segment)
        if not target.exists() or target.stat().st_nlink < MAX_OBJECT_LINKS:
            return target
        segment += 1


def shared_file(path, sha256, objects=None):
    objects = objects or object_store(path.parent)
    if objects is None:
        return False
    target = object_path(objects, sha256)
    segment = 0
    while target.is_file():
        if os.path.samefile(path, target):
            return True
        segment += 1
        target = object_path(objects, sha256, segment)
    return False


def ensure_object(source, item, objects, *, segment=None):
    from .bank_publication import file_digest, PublicationError, sync_directory
    target = (available_object(objects, item['sha256']) if segment is None
              else object_path(objects, item['sha256'], segment))
    if target.exists():
        # Callers have just verified the source against the frozen manifest.
        # If it is this very object, rereading it before linking adds no evidence;
        # the destination is independently verified after all links are created.
        if target.stat().st_size == item['size'] and os.path.samefile(source, target):
            return target
        if target.stat().st_size != item['size'] or file_digest(target) != item['sha256']:
            raise PublicationError('object-integrity-failed')
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / ('.pending-' + uuid4().hex)
    try:
        with source.open('rb') as inp, temporary.open('xb') as out:
            shutil.copyfileobj(inp, out)
            out.flush(); os.fsync(out.fileno())
        if temporary.stat().st_size != item['size'] or file_digest(temporary) != item['sha256']:
            raise PublicationError('object-integrity-failed')
        try:
            # Create-only: a duplicate-content race may reuse, never overwrite,
            # an existing immutable object.
            os.link(temporary, target)
        except FileExistsError:
            if target.stat().st_size != item['size'] or file_digest(target) != item['sha256']:
                raise PublicationError('object-integrity-failed')
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(target.parent)
    return target


def sync_current(root, version):
    """A fixed human-facing directory alias; API readers still pin active.json."""
    from .bank_publication import reject_links, PublicationError
    if not HASH.fullmatch(version):
        raise PublicationError('invalid-version')
    target = reject_links(root / 'versions' / version)
    if not target.is_dir():
        raise PublicationError('current-version-missing')
    current = root / 'current'
    linked = lambda p: p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
    if current.exists() or linked(current):
        if not linked(current):
            raise PublicationError('current-entry-is-not-managed')
        prior = current.resolve()
        if prior.parent != root / 'versions' or not HASH.fullmatch(prior.name):
            raise PublicationError('current-entry-is-not-managed')
        if prior == target:
            return
    temporary = root / ('.current-' + uuid4().hex)
    retired = root / ('.previous-current-' + uuid4().hex)
    def remove_alias(path):
        if linked(path):
            os.rmdir(path) if os.name == 'nt' else path.unlink()
    try:
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(temporary))
            if linked(current):
                current.rename(retired)
            try:
                temporary.rename(current)
            except Exception:
                if linked(retired):
                    retired.rename(current)
                raise
        else:
            temporary.symlink_to(target, target_is_directory=True)
            os.replace(temporary, current)
    finally:
        remove_alias(temporary)
        remove_alias(retired)


def seed_objects(source, version):
    from .bank_publication import verify_bundle, PublicationError, require_storage_space
    objects = object_store(source)
    if objects is None:
        raise PublicationError('object-store-required')
    manifest = verify_bundle(source, version)
    require_storage_space([(objects, missing_bytes(manifest, objects))])
    unique = {item['sha256']: item for item in manifest['files']}
    counts = Counter(item['sha256'] for item in manifest['files'])
    def seed_item(item):
        remaining = counts[item['sha256']]
        segment = 0
        while remaining > 0:
            target = ensure_object(source / item['path'], item, objects, segment=segment)
            remaining -= max(0, MAX_OBJECT_LINKS - target.stat().st_nlink)
            segment += 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(seed_item, unique.values()))
    return objects


def missing_bytes(manifest, objects):
    unique = {item['sha256']: item for item in manifest['files']}
    return sum(item['size'] for item in unique.values() if not available_object(objects, item['sha256']).exists())


def clone_bank(source, destination):
    """An isolated worker links only preseeded immutable media; indexes are private."""
    from .bank_publication import verify_bundle, PublicationError
    objects = object_store(source)
    manifest = verify_bundle(source, source.name)
    if objects is None:
        raise PublicationError('object-store-required')
    for bank in ('main', 'symbolic'):
        (destination / bank).mkdir()
    for item in manifest['files']:
        if item['path'] == 'registry.json':
            continue
        target = destination / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == '.xlsx':
            shutil.copyfile(source / item['path'], target)
        else:
            obj = available_object(objects, item['sha256'])
            if not obj.is_file() or obj.stat().st_size != item['size']:
                raise PublicationError('unseeded-object')
            os.link(obj, target)
