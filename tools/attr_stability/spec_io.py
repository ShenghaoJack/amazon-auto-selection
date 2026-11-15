"""Validated local attribute writes and contracts between annotation steps."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from tools.attr_stability.catalog import (
    resolve_spec_path, catalog_dir_of, catalog_meta, parse_cat, ATTR_POOL_FILE,
    CONFIRMED_SPEC_FILE, CONFIRMED_RULES_FILE, CONFIRMED_SPEC_POLICY,
)
from tools.attr_stability.spec import SPEC_STATUS_PENDING, cat_rules_stem, resolve_rules_file

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def spec_lock(cat_root):
    """Serialize the Web process and workflow subprocesses for this CAT."""
    root = Path(cat_root).resolve()
    key = str(root).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "attribute-spec.lock").open("a+b") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            if os.name == "nt":
                import msvcrt
                def acquire():
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(fh, fcntl.LOCK_UN)
            deadline = time.monotonic() + 15
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ValueError("属性集正在被其他进程保存，请稍后重试") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                release()


def validate_attr_spec(spec, *, allow_empty=False):
    if not isinstance(spec, dict) or not isinstance(spec.get("attributes"), list):
        raise ValueError("属性集必须是对象，attributes 必须是列表")
    if not spec["attributes"] and not allow_empty:
        raise ValueError("属性集为空，至少需要一个属性")
    names = set()
    for entry in spec["attributes"]:
        if not isinstance(entry, dict):
            raise ValueError("每个属性必须是对象")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("属性名不能为空或含首尾空格")
        if name in names:
            raise ValueError(f"属性名重复: {name}")
        names.add(name)
        for field in ("hint", "hint_short"):
            if field in entry and not isinstance(entry[field], str):
                raise ValueError(f"{name} 的 {field} 必须是字符串")
    return spec


def load_attr_spec(path, *, allow_empty=False):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return validate_attr_spec(json.load(fh), allow_empty=allow_empty)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"属性集读取或校验失败 {path}: {exc}") from exc


def spec_revision(spec):
    if spec is None:
        return ""
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def own_spec_path(cat_root):
    root = Path(cat_root)
    return root / f"{root.name}-attribute-spec.json"


def _stage_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        return tmp
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


def write_files_atomic(files):
    """Stage writes first; None means delete. Restore writes/deletions if commit fails."""
    originals = {p: p.read_bytes() if p.exists() else None for p in files}
    staged, replaced = {}, []
    try:
        for path, content in files.items():
            if content is not None:
                staged[path] = _stage_bytes(path, content)
        for path, content in files.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(staged[path], path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            old = originals[path]
            if old is None:
                path.unlink(missing_ok=True)
            else:
                rollback = _stage_bytes(path, old)
                try:
                    os.replace(rollback, path)
                finally:
                    rollback.unlink(missing_ok=True)
        raise
    finally:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json_atomic(path, value):
    write_files_atomic({Path(path): json_bytes(value)})


def assert_rule_alignment(spec, rules):
    validate_attr_spec(spec)
    validate_attr_spec(rules)
    expected = {a["name"] for a in spec["attributes"]}
    actual = {a["name"] for a in rules["attributes"]}
    if actual != expected:
        raise ValueError("属性集与规则不一致: 缺少 %s，多余 %s；请重新保存并确认属性集"
                         % (sorted(expected - actual), sorted(actual - expected)))


def commit_attr_spec(cat_root, draft, *, expected_revision=None):
    """保存本运行草稿；人工确认时同步替换唯一共享基准，失败则整体回滚。"""
    root = Path(cat_root).resolve()
    shared = catalog_dir_of(root.parent, parse_cat(root.name)["类目"])
    with spec_lock(root), spec_lock(shared):
        source = resolve_spec_path(root.parent, root.name)
        previous = load_attr_spec(source, allow_empty=True) if source.exists() else None
        if expected_revision is not None and expected_revision != spec_revision(previous):
            raise ValueError("属性集已被其他操作更新，请重新加载后再保存")
        spec = copy.deepcopy(validate_attr_spec(draft))
        spec["cat"] = root.name
        own = own_spec_path(root)
        if previous and source.resolve() != own.resolve():
            spec.setdefault("_inherited_from", source.name)
        rules_source = resolve_rules_file(str(root))
        if rules_source:
            with open(rules_source, encoding="utf-8-sig") as fh:
                rules = json.load(fh)
            validate_attr_spec(rules, allow_empty=True)
            rp = Path(rules_source).resolve()
            target_rules = rp if rp.parent in (root, root / "categories") else root / rp.name
        else:
            rules = {"category": root.name, "attributes": []}
            target_rules = root / (cat_rules_stem(root.name) + ".rules.json")
        existing = {a["name"]: a for a in rules["attributes"]}
        names = [a["name"] for a in spec["attributes"]]
        added = [n for n in names if n not in existing]
        removed = [n for n in existing if n not in names]
        rules["attributes"] = [existing.get(n, {"name": n, "rule": "enum", "params": {}})
                               for n in names]
        if spec.get("status") == SPEC_STATUS_PENDING or added or removed:
            spec["definition_revision"] = uuid.uuid4().hex
            rules.pop("vocab_status", None)
        else:
            spec["definition_revision"] = (previous or {}).get("definition_revision") or uuid.uuid4().hex
        assert_rule_alignment(spec, rules)
        if spec.get("status") == "confirmed":
            spec["confirmed_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
            spec["_confirmed_from_cat"] = root.name
        else:
            spec.pop("confirmed_at", None)
            spec.pop("_confirmed_from_cat", None)
        files = {target_rules: json_bytes(rules), own: json_bytes(spec)}
        if spec.get("status") == "confirmed":
            files.update(_confirmed_catalog_files(root, shared, spec, rules))
        write_files_atomic(files)
        return {"spec": spec, "path": str(own), "rules_path": str(target_rules),
                "added": added, "removed": removed, "revision": spec_revision(spec)}


def _catalog_meta_for_write(root, shared):
    """写入前严格读取元数据，避免坏文件被软回退覆盖而丢失人工排除项。"""
    meta = catalog_meta(root.parent, parse_cat(root.name)["类目"])
    path = shared / "_catalog.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("必须为对象")
        except (OSError, ValueError) as exc:
            raise ValueError("共享类目元数据损坏，拒绝覆盖: %s" % path) from exc
        meta.update(raw)
    return meta


def _confirmed_catalog_files(root, shared, spec, rules):
    """一次确认替换唯一共享基准；旧提取池和旧属性集不再作为历史版本累积。"""
    meta = _catalog_meta_for_write(root, shared)
    meta.update({"属性集策略": CONFIRMED_SPEC_POLICY, "属性集": CONFIRMED_SPEC_FILE,
                 "规则": CONFIRMED_RULES_FILE, "属性集来源": root.name,
                 "属性集确认时间": spec["confirmed_at"]})
    files = {shared / CONFIRMED_SPEC_FILE: json_bytes(spec),
             shared / CONFIRMED_RULES_FILE: json_bytes(rules)}
    for path in shared.glob("*-attribute-spec.json"):
        if path.name != CONFIRMED_SPEC_FILE and path.is_file():
            files[path] = None
    pool = shared / ATTR_POOL_FILE
    if pool.exists():
        files[pool] = None
    files[shared / "_catalog.json"] = json_bytes(meta)
    return files


def reset_attr_spec(cat_root):
    """删除本运行产物；仅撤回由本运行发布的共享基准，绝不删除其他运行的确认集。"""
    root = Path(cat_root).resolve()
    shared = catalog_dir_of(root.parent, parse_cat(root.name)["类目"])
    with spec_lock(root), spec_lock(shared):
        own = own_spec_path(root)
        files = {own: None} if own.exists() else {}
        meta = _catalog_meta_for_write(root, shared)
        meta_changed = False
        if (meta.get("属性集策略") == CONFIRMED_SPEC_POLICY
                and meta.get("属性集来源") == root.name):
            for name in (CONFIRMED_SPEC_FILE, CONFIRMED_RULES_FILE):
                if (shared / name).exists():
                    files[shared / name] = None
            meta.update({"属性集": "", "规则": "", "属性集来源": "", "属性集确认时间": ""})
            meta_changed = True
        # 兼容旧布局：仅清除能明确证明属于本运行的副本。
        for path in shared.glob("*-attribute-spec.json"):
            if path in files or path.name == CONFIRMED_SPEC_FILE:
                continue
            exact_owner = path.name == root.name + "-attribute-spec.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                if not exact_owner:
                    continue
                value = {}
            if exact_owner or (isinstance(value, dict) and value.get("cat") == root.name):
                files[path] = None
                if meta.get("属性集") == path.name:
                    meta.pop("属性集", None)
                    meta_changed = True
        pool = shared / ATTR_POOL_FILE
        if pool.exists():
            files[pool] = None
        removed = [str(path) for path, content in files.items() if content is None and path.exists()]
        if meta_changed:
            files[shared / "_catalog.json"] = json_bytes(meta)
        write_files_atomic(files)
        return removed


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows_digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


def make_annotation_contract(spec, rows, *, source_path, input_mode="auto"):
    input_mode = str(input_mode).strip().lower()
    if input_mode not in ("auto", "snapshot"):
        raise ValueError("输入模式必须是 auto 或 snapshot")
    if not spec.get("definition_revision"):
        raise ValueError("属性集缺少定义版本，请重新运行步骤17")
    return {"version": 1, "completed": False, "definition_revision": spec["definition_revision"], "spec_sha256": spec_revision(spec),
            "input_sha256": rows_digest(rows), "input_mode": input_mode,
            "source_sha256": file_digest(source_path) if input_mode == "auto" else None}


def verify_annotation_contract(spec, rules, result, rows, *, source_path):
    assert_rule_alignment(spec, rules)
    contract = result.get("annotation_contract")
    if not isinstance(contract, dict) or contract.get("version") != 1:
        raise ValueError("步骤17结果缺少有效版本记录，请重新运行步骤17")
    if contract.get("completed") is not True:
        raise ValueError("步骤17尚未完成，拒绝使用中间结果")
    if not spec.get("definition_revision") or contract.get("definition_revision") != spec["definition_revision"]:
        raise ValueError("步骤17结果的属性集版本已过期，请重新运行步骤17")
    if contract.get("spec_sha256") != spec_revision(spec):
        raise ValueError("属性集内容已更新，请重新运行步骤17")
    if not isinstance(rows, list) or not rows or contract.get("input_sha256") != rows_digest(rows):
        raise ValueError("步骤17输入快照与结果不匹配，请重新运行步骤17")
    if contract.get("input_mode") not in ("auto", "snapshot"):
        raise ValueError("步骤17结果的输入模式不合法")
    if contract["input_mode"] == "auto" and contract.get("source_sha256") != file_digest(source_path):
        raise ValueError("上游输入已更新，请重新运行步骤17")
    incoming = [r.get("xl_row") for r in rows if isinstance(r, dict)]
    delivered = result.get("deliverable")
    if not isinstance(delivered, list) or not delivered:
        raise ValueError("步骤17交付为空，请重新运行步骤17")
    outgoing = [r.get("xl_row") for r in delivered if isinstance(r, dict)]
    if (len(incoming) != len(rows) or len(outgoing) != len(delivered)
            or any(type(rid) is not int for rid in incoming + outgoing)
            or len(set(incoming)) != len(incoming) or len(set(outgoing)) != len(outgoing)
            or set(incoming) != set(outgoing)):
        raise ValueError("步骤17交付行号缺失、重复或多余")
    names = [a["name"] for a in spec["attributes"]]
    for row in delivered:
        attrs = row.get("attributes")
        if not isinstance(attrs, dict) or any(n not in attrs or n + "_来源" not in attrs for n in names):
            raise ValueError(f"步骤17交付缺少必填属性或来源键，行 {row['xl_row']}")
