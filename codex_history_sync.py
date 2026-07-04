#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time


# Table blocks that are shared across all providers and therefore union-merged.
# NOTE: `[model_providers.*]` is intentionally excluded — those blocks are
# per-provider identity (base_url, bearer token, wire_api), not shared config.
# Union-merging them would copy a relay's `[model_providers.custom]` into the
# official provider and re-route it away from OpenAI.
BLOCK_PREFIXES = (
    "[projects.",
    "[plugins.",
    "[mcp_servers.",
    "[marketplaces.",
)


def utc_iso_from_epoch(seconds: int | float) -> str:
    return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def rollout_id_from_name(path: Path) -> str:
    stem = path.stem
    parts = stem.split("-")
    if len(parts) >= 6:
        return "-".join(parts[-5:])
    return stem


def clean_title(text: str | None) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value:
        return "Untitled session"
    if len(value) > 140:
        return value[:140].rstrip() + "..."
    return value


def text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def backup_sqlite(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = connect_sqlite_readonly(src)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return True


def connect_sqlite_readonly(path: Path, timeout: float = 5) -> sqlite3.Connection:
    """Open a local SQLite file read-only, with fallbacks for flaky URI handling."""
    attempts = [
        (f"file:{path}?mode=ro", True),
        (f"file:{path}?mode=ro&immutable=1", True),
        (str(path), False),
    ]
    last_error: sqlite3.Error | None = None
    for target, use_uri in attempts:
        try:
            con = sqlite3.connect(target, uri=use_uri, timeout=timeout)
            try:
                con.execute("PRAGMA schema_version").fetchone()
            except sqlite3.Error:
                con.close()
                raise
            return con
        except sqlite3.Error as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def table_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    headers: list[int] = []
    for index, line in enumerate(lines):
        if re.match(r"^\s*\[[^\]]+\]\s*$", line):
            headers.append(index)
    blocks: dict[str, str] = {}
    for block_index, start in enumerate(headers):
        end = headers[block_index + 1] if block_index + 1 < len(headers) else len(lines)
        header = lines[start].strip()
        if header.startswith(BLOCK_PREFIXES):
            blocks[header] = "\n".join(lines[start:end]).rstrip() + "\n"
    return blocks


def replace_or_prepend_scalar(text: str, key: str, value: str) -> str:
    pattern = rf'(?m)^\s*{re.escape(key)}\s*=.*$'
    replacement = f'{key} = "{value}"'
    if re.search(pattern, text):
        return re.sub(pattern, replacement, text, count=1)
    prefix = replacement + "\n"
    return prefix + text if text else prefix


def scalar_value(text: str, key: str) -> str | None:
    pattern = rf'(?m)^\s*{re.escape(key)}\s*=\s*"([^"]+)"\s*$'
    match = re.search(pattern, text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def ensure_history_save_all(text: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "[history]":
            start = index
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n[history]\npersistence = \"save-all\"\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^\s*\[.+\]\s*$", lines[index]):
            end = index
            break
    for index in range(start + 1, end):
        if re.match(r"^\s*persistence\s*=", lines[index]):
            lines[index] = 'persistence = "save-all"'
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, 'persistence = "save-all"')
    return "\n".join(lines) + "\n"


def append_missing_blocks(text: str, block_union: dict[str, str]) -> str:
    existing = set(table_blocks(text).keys())
    missing = [block for header, block in block_union.items() if header not in existing]
    if not missing:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + "\n".join(missing)


def strip_model_provider_routing(text: str) -> str:
    """Remove every trace of custom model_provider routing from a config.

    Drops the `model_provider = "..."` scalar, the empty `[model_providers]`
    header, and every `[model_providers.<key>]` block (header + its key=value
    lines). What's left relies on Codex's built-in openai provider — i.e. the
    ChatGPT OAuth path. Used by `set-official` to make a provider official.
    """
    lines = text.splitlines()
    out: list[str] = []
    skipping_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[model_providers]":
            continue
        if re.match(r"^\[model_providers\.[^\]]+\]\s*$", stripped):
            skipping_block = True
            continue
        if skipping_block:
            if re.match(r"^\[.+\]\s*$", stripped):
                skipping_block = False
                out.append(line)
            # else: a key=value (or blank) line inside the dropped block — skip
            continue
        if re.match(r'^\s*model_provider\s*=', line):
            continue
        out.append(line)
    result = "\n".join(out)
    if text.endswith("\n") and result:
        result += "\n"
    return result


def normalize_config_text(text: str, sqlite_home: Path, block_union: dict[str, str]) -> str:
    """Union-merge shared blocks into a provider config WITHOUT touching identity.

    Identity keys (model_provider, model_providers.* blocks, base_url,
    disable_response_storage, model) are left exactly as each provider defines
    them — so the official `default` can stay on OpenAI while relays stay on
    `custom`. Only shared infrastructure (sqlite_home, [history]) and the union
    of shared table blocks (mcp_servers, marketplaces, plugins, projects, ...)
    are normalized.
    """
    updated = text or ""
    updated = ensure_history_save_all(updated)
    updated = replace_or_prepend_scalar(updated, "sqlite_home", str(sqlite_home))
    updated = append_missing_blocks(updated, block_union)
    return updated


class CodexHistorySync:
    def __init__(
        self,
        codex_home: Path,
        cc_switch_home: Path,
        sqlite_home: Path | None,
        dry_run: bool,
        verbose: bool,
    ) -> None:
        self.codex_home = codex_home.expanduser().resolve()
        self.cc_switch_home = cc_switch_home.expanduser().resolve()
        self.sqlite_home = (sqlite_home or (self.codex_home / "sqlite")).expanduser().resolve()
        self.cc_switch_db = self.cc_switch_home / "cc-switch.db"
        self.cc_switch_settings = self.cc_switch_home / "settings.json"
        self.live_config = self.codex_home / "config.toml"
        self.session_index = self.codex_home / "session_index.jsonl"
        self.state_dbs = self._state_db_candidates()
        self.dry_run = dry_run
        self.verbose = verbose

    def _state_db_candidates(self) -> list[Path]:
        candidates = [
            self.sqlite_home / "state_5.sqlite",
            self.codex_home / "state_5.sqlite",
        ]
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key not in seen:
                unique.append(path)
                seen.add(key)
        return unique

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)

    def backup_root(self) -> Path:
        return self.codex_home / "backups" / "codex-history-sync-tool"

    def create_backup(self) -> str | None:
        backup_dir = self.backup_root() / time.strftime("%Y%m%d_%H%M%S")
        if self.dry_run:
            return str(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        files_to_copy = [
            self.live_config,
            self.session_index,
            self.cc_switch_settings,
        ]
        for src in files_to_copy:
            if src.exists():
                target = backup_dir / src.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        for db_path in [self.cc_switch_db, *self.state_dbs]:
            if db_path.exists():
                target = backup_dir / db_path.relative_to(db_path.parent.parent if db_path.parent.name == "sqlite" else db_path.parent)
                backup_sqlite(db_path, target)
        return str(backup_dir)

    def gather_block_union(self) -> dict[str, str]:
        union: dict[str, str] = {}
        texts: list[str] = []
        if self.live_config.exists():
            texts.append(self.live_config.read_text(encoding="utf-8"))
        if self.cc_switch_db.exists():
            con = connect_sqlite_readonly(self.cc_switch_db, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                for row in con.execute("select settings_config from providers where app_type='codex'"):
                    try:
                        cfg = json.loads(row["settings_config"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    text = cfg.get("config")
                    if isinstance(text, str):
                        texts.append(text)
            finally:
                con.close()
        for text in texts:
            union.update(table_blocks(text))
        return union

    def official_provider_ids(self) -> set[str]:
        ids = {"default", "codex-official"}
        if not self.cc_switch_db.exists():
            return ids
        con = connect_sqlite_readonly(self.cc_switch_db, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute("select id, coalesce(category, '') as category from providers where app_type='codex'"):
                if (row["category"] or "").strip().lower() == "official":
                    ids.add(row["id"])
        finally:
            con.close()
        return ids

    def current_provider_is_official(self) -> bool:
        provider_id = self.current_provider_id()
        return bool(provider_id and provider_id in self.official_provider_ids())

    def current_history_bucket(self) -> str:
        if self.current_provider_is_official():
            return "openai"
        if self.live_config.exists():
            value = scalar_value(self.live_config.read_text(encoding="utf-8"), "model_provider")
            if value:
                return value
        return "custom"

    def normalize_settings_json(self) -> dict[str, object]:
        result = {"changed": False, "path": str(self.cc_switch_settings)}
        if not self.cc_switch_settings.exists():
            result["missing"] = True
            return result
        data = json.loads(self.cc_switch_settings.read_text(encoding="utf-8"))
        changed = False
        for key, value in {
            "unifyCodexSessionHistory": True,
            "preserveCodexOfficialAuthOnSwitch": True,
        }.items():
            if data.get(key) != value:
                data[key] = value
                changed = True
        result["changed"] = changed
        if changed and not self.dry_run:
            self.cc_switch_settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    def normalize_live_config(self, block_union: dict[str, str]) -> dict[str, object]:
        result = {"changed": False, "path": str(self.live_config)}
        if not self.live_config.exists():
            result["missing"] = True
            return result
        old_text = self.live_config.read_text(encoding="utf-8")
        new_text = old_text
        if self.current_provider_is_official():
            new_text = strip_model_provider_routing(new_text)
        new_text = normalize_config_text(new_text, self.sqlite_home, block_union)
        changed = new_text != old_text
        result["changed"] = changed
        if changed and not self.dry_run:
            self.live_config.write_text(new_text, encoding="utf-8")
        return result

    def normalize_common_config(self, block_union: dict[str, str]) -> dict[str, object]:
        result = {"changed": False, "key": "common_config_codex"}
        if not self.cc_switch_db.exists():
            result["missing"] = True
            return result
        con = sqlite3.connect(str(self.cc_switch_db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        try:
            row = con.execute("select value from settings where key='common_config_codex'").fetchone()
            if row is None:
                result["missing"] = True
                return result
            old_text = row["value"] or ""
            new_text = normalize_config_text(strip_model_provider_routing(old_text), self.sqlite_home, block_union)
            result["changed"] = new_text != old_text
            if result["changed"] and not self.dry_run:
                con.execute("update settings set value=? where key='common_config_codex'", (new_text,))
                con.commit()
        finally:
            con.close()
        return result

    def normalize_provider_templates(self, block_union: dict[str, str]) -> dict[str, object]:
        result = {"providers": 0, "changed": 0, "updated_ids": []}
        if not self.cc_switch_db.exists():
            result["missing"] = True
            return result
        official_ids = self.official_provider_ids()
        con = sqlite3.connect(str(self.cc_switch_db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        changed = 0
        updated_ids: list[str] = []
        try:
            rows = list(con.execute("select id, settings_config from providers where app_type='codex'"))
            result["providers"] = len(rows)
            for row in rows:
                try:
                    cfg = json.loads(row["settings_config"] or "{}")
                except json.JSONDecodeError:
                    continue
                old_text = cfg.get("config", "")
                if not isinstance(old_text, str):
                    old_text = ""
                new_text = old_text
                if row["id"] in official_ids:
                    new_text = strip_model_provider_routing(new_text)
                new_text = normalize_config_text(new_text, self.sqlite_home, block_union)
                if new_text == old_text:
                    continue
                changed += 1
                updated_ids.append(row["id"])
                if not self.dry_run:
                    cfg["config"] = new_text
                    con.execute(
                        "update providers set settings_config=? where id=?",
                        (json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), row["id"]),
                    )
            if not self.dry_run:
                con.commit()
        finally:
            con.close()
        result["changed"] = changed
        result["updated_ids"] = updated_ids
        return result

    def iter_rollouts(self) -> list[tuple[Path, int]]:
        roots = [
            (self.codex_home / "sessions", 0),
            (self.codex_home / "archived_sessions", 1),
        ]
        rollouts: list[tuple[Path, int]] = []
        for root, archived in roots:
            if not root.exists():
                continue
            for path in root.rglob("rollout-*.jsonl"):
                rollouts.append((path, archived))
        return rollouts

    def read_session_meta_line(self, path: Path, max_lines: int = 20) -> tuple[int, str, dict[str, object]] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= max_lines:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "session_meta":
                        return index, line, obj
        except OSError:
            return None
        return None

    def replace_line_streaming(self, path: Path, target_index: int, new_line: str) -> bool:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with path.open("r", encoding="utf-8") as src, tmp_path.open("w", encoding="utf-8", newline="") as dst:
                for index, line in enumerate(src):
                    dst.write(new_line if index == target_index else line)
            if not self.dry_run:
                os.replace(tmp_path, path)
            else:
                tmp_path.unlink(missing_ok=True)
            return True
        except OSError:
            tmp_path.unlink(missing_ok=True)
            return False

    def normalize_rollouts(self, backup_dir: str | None, target_model_provider: str) -> dict[str, object]:
        result = {"changed": 0, "scanned": 0, "model_provider_changed": 0}
        backup_path = Path(backup_dir) / "rollout_session_meta_backup.jsonl" if backup_dir else None
        backup_handle = None
        if backup_path and not self.dry_run:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_handle = backup_path.open("a", encoding="utf-8", newline="\n")
        try:
            for path, _archived in self.iter_rollouts():
                result["scanned"] += 1
                meta_line = self.read_session_meta_line(path)
                if not meta_line:
                    continue
                line_index, line, obj = meta_line
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                file_rollout_id = rollout_id_from_name(path)
                id_changed = payload.get("id") != file_rollout_id
                provider_changed = payload.get("model_provider") != target_model_provider
                if not id_changed and not provider_changed:
                    continue
                if backup_handle is not None:
                    backup_handle.write(
                        json.dumps(
                            {
                                "path": str(path),
                                "line_index": line_index,
                                "original_line": line.rstrip("\r\n"),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                payload["id"] = file_rollout_id
                payload["model_provider"] = target_model_provider
                obj["payload"] = payload
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                new_line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + newline
                if self.replace_line_streaming(path, line_index, new_line):
                    result["changed"] += 1
                    if provider_changed:
                        result["model_provider_changed"] += 1
        finally:
            if backup_handle is not None:
                backup_handle.close()
        return result

    def parse_rollout(self, path: Path) -> dict[str, object]:
        meta: dict[str, object] = {}
        title = ""
        first_ts: float | None = None
        last_ts: float | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = parse_iso_timestamp(obj.get("timestamp"))
                    if timestamp is not None:
                        first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
                        last_ts = timestamp if last_ts is None else max(last_ts, timestamp)
                    item_type = obj.get("type")
                    payload = obj.get("payload") or {}
                    if item_type == "session_meta" and isinstance(payload, dict):
                        meta.update(payload)
                    elif item_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message" and not title:
                        title = clean_title(str(payload.get("message", "")))
                    elif item_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user" and not title:
                        title = clean_title(text_from_content(payload.get("content")))
        except OSError:
            pass
        stat = path.stat()
        created_at = first_ts or parse_iso_timestamp(str(meta.get("timestamp") or "")) or stat.st_ctime
        updated_at = max([value for value in (last_ts, stat.st_mtime) if value is not None], default=stat.st_mtime)
        return {
            "id": rollout_id_from_name(path),
            "title": title or "Untitled session",
            "created_at": int(created_at),
            "updated_at": int(updated_at),
            "cwd": meta.get("cwd"),
            "source": meta.get("source") or "vscode",
            "thread_source": meta.get("thread_source"),
            "model": meta.get("model"),
            "model_provider": meta.get("model_provider"),
            "reasoning_effort": meta.get("reasoning_effort"),
            "rollout_path": str(path),
        }

    def load_current_index_titles(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        if not self.session_index.exists():
            return titles
        try:
            with self.session_index.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rollout_id = obj.get("id")
                    title = obj.get("thread_name")
                    if isinstance(rollout_id, str) and isinstance(title, str) and title.strip():
                        titles[rollout_id] = title.strip()
        except OSError:
            return titles
        return titles

    def normalize_state_db(self, path: Path, index_titles: dict[str, str], target_model_provider: str) -> dict[str, object]:
        result = {
            "path": str(path),
            "missing": not path.exists(),
            "provider_rows_updated": 0,
            "title_rows_updated": 0,
            "inserted": 0,
            "integrity": "missing",
        }
        if not path.exists():
            return result
        con = sqlite3.connect(str(path), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        try:
            existing = {
                row["id"]: row
                for row in con.execute("select id, title, model_provider from threads")
            }

            before = con.total_changes
            con.execute(
                "update threads set model_provider=? where model_provider<>?",
                (target_model_provider, target_model_provider),
            )
            result["provider_rows_updated"] += con.total_changes - before

            for rollout_id, title in index_titles.items():
                row = existing.get(rollout_id)
                current_title = (row["title"] or "") if row is not None else ""
                if row is None or current_title == title:
                    continue
                before = con.total_changes
                con.execute(
                    "update threads set title=?, first_user_message=?, preview=? where id=?",
                    (title, title, title, rollout_id),
                )
                result["title_rows_updated"] += con.total_changes - before

            for rollout_path, archived in self.iter_rollouts():
                rollout_id = rollout_id_from_name(rollout_path)
                if rollout_id in existing:
                    continue
                info = self.parse_rollout(rollout_path)
                before = con.total_changes
                con.execute(
                    """
                    insert or ignore into threads
                    (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                     sandbox_policy, approval_mode, tokens_used, has_user_event, archived, archived_at,
                     cli_version, first_user_message, model, reasoning_effort, created_at_ms,
                     updated_at_ms, thread_source, preview)
                    values (?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, 1, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        info["id"],
                        info["rollout_path"],
                        info["created_at"],
                        info["updated_at"],
                        info["source"],
                        target_model_provider,
                        info["cwd"],
                        info["title"],
                        archived,
                        info["updated_at"] if archived else None,
                        info["title"],
                        info["model"],
                        info["reasoning_effort"],
                        int(info["created_at"]) * 1000,
                        int(info["updated_at"]) * 1000,
                        info["thread_source"],
                        info["title"],
                    ),
                )
                result["inserted"] += con.total_changes - before
                existing[rollout_id] = {"id": rollout_id, "title": info["title"]}

            if self.dry_run:
                con.rollback()
            else:
                con.commit()
            result["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        result["missing"] = False
        return result

    def rebuild_session_index(self, index_titles: dict[str, str]) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        state_rows: dict[str, dict[str, object]] = {}
        preferred_db = next((path for path in self.state_dbs if path.exists()), None)
        if preferred_db:
            con = connect_sqlite_readonly(preferred_db, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                for row in con.execute("select id, title, updated_at from threads"):
                    state_rows[row["id"]] = {
                        "id": row["id"],
                        "title": row["title"] or "Untitled session",
                        "updated_at": int(row["updated_at"] or 0),
                    }
            finally:
                con.close()

        seen: set[str] = set()
        for rollout_path, _archived in self.iter_rollouts():
            rollout_id = rollout_id_from_name(rollout_path)
            if rollout_id in seen:
                continue
            seen.add(rollout_id)
            if rollout_id in state_rows:
                row = dict(state_rows[rollout_id])
                if rollout_id in index_titles:
                    row["title"] = index_titles[rollout_id]
                rows.append(row)
                continue
            info = self.parse_rollout(rollout_path)
            rows.append(
                {
                    "id": info["id"],
                    "title": index_titles.get(str(info["id"])) or info["title"],
                    "updated_at": int(info["updated_at"]),
                }
            )

        rows.sort(key=lambda row: (int(row["updated_at"]), str(row["id"])))
        result = {"path": str(self.session_index), "entries": len(rows), "changed": True}
        if self.dry_run:
            return result
        self.session_index.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.session_index.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        {
                            "id": row["id"],
                            "thread_name": row["title"] or "Untitled session",
                            "updated_at": utc_iso_from_epoch(int(row["updated_at"])),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        os.replace(tmp_path, self.session_index)
        return result

    def current_provider_id(self) -> str | None:
        if not self.cc_switch_settings.exists():
            return None
        try:
            data = json.loads(self.cc_switch_settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        value = data.get("currentProviderCodex")
        return value if isinstance(value, str) and value else None

    def collect_status(self) -> dict[str, object]:
        status: dict[str, object] = {
            "codex_home": str(self.codex_home),
            "sqlite_home": str(self.sqlite_home),
            "current_provider_id": self.current_provider_id(),
            "current_history_bucket": self.current_history_bucket(),
            "state_dbs": [],
        }
        for db_path in self.state_dbs:
            entry: dict[str, object] = {"path": str(db_path), "exists": db_path.exists()}
            if db_path.exists():
                con = connect_sqlite_readonly(db_path, timeout=5)
                con.row_factory = sqlite3.Row
                try:
                    rows = list(con.execute("select model_provider, count(*) as cnt from threads group by model_provider order by model_provider"))
                finally:
                    con.close()
                entry["providers"] = {row["model_provider"]: row["cnt"] for row in rows}
            status["state_dbs"].append(entry)
        return status

    def run_sync(self) -> dict[str, object]:
        self.sqlite_home.mkdir(parents=True, exist_ok=True)
        backup_dir = self.create_backup()
        block_union = self.gather_block_union()
        current_index_titles = self.load_current_index_titles()
        target_model_provider = self.current_history_bucket()
        settings_report = self.normalize_settings_json()
        common_report = self.normalize_common_config(block_union)
        provider_report = self.normalize_provider_templates(block_union)
        live_config_report = self.normalize_live_config(block_union)
        rollout_report = self.normalize_rollouts(backup_dir, target_model_provider)
        state_reports = [self.normalize_state_db(path, current_index_titles, target_model_provider) for path in self.state_dbs]
        index_report = self.rebuild_session_index(current_index_titles)
        return {
            "backup_dir": backup_dir,
            "current_provider_id": self.current_provider_id(),
            "target_model_provider": target_model_provider,
            "settings": settings_report,
            "common_config": common_report,
            "provider_templates": provider_report,
            "live_config": live_config_report,
            "rollouts": rollout_report,
            "state_dbs": state_reports,
            "session_index": index_report,
        }

    def set_official(self, provider_id: str) -> dict[str, object]:
        """Strip custom model_provider routing from one provider → official OpenAI.

        Removes `model_provider = "..."`, the empty `[model_providers]` header,
        and every `[model_providers.<key>]` block from the named provider's
        config in cc-switch.db, so it falls back to Codex's built-in openai
        provider (ChatGPT OAuth). If that provider is the currently active
        one, the live `~/.codex/config.toml` is fixed the same way. A backup
        is taken first via create_backup().
        """
        result: dict[str, object] = {
            "provider_id": provider_id,
            "found": False,
            "changed": False,
            "live_config_changed": False,
        }
        if not self.cc_switch_db.exists():
            result["missing_db"] = True
            return result
        result["backup_dir"] = self.create_backup()
        con = sqlite3.connect(str(self.cc_switch_db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        try:
            row = con.execute(
                "select id, settings_config from providers where id=? and app_type='codex'",
                (provider_id,),
            ).fetchone()
            if row is None:
                return result
            result["found"] = True
            try:
                cfg = json.loads(row["settings_config"] or "{}")
            except json.JSONDecodeError:
                cfg = {}
            old_text = cfg.get("config", "")
            if not isinstance(old_text, str):
                old_text = ""
            new_text = strip_model_provider_routing(old_text)
            if new_text != old_text:
                result["changed"] = True
                result["diff"] = "\n".join(
                    difflib.unified_diff(
                        old_text.splitlines(),
                        new_text.splitlines(),
                        lineterm="",
                        fromfile=f"{provider_id} (before)",
                        tofile=f"{provider_id} (after)",
                        n=1,
                    )
                )
                if not self.dry_run:
                    cfg["config"] = new_text
                    con.execute(
                        "update providers set settings_config=? where id=? and app_type='codex'",
                        (json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), provider_id),
                    )
                    con.commit()
            # If this is the active provider, fix the live config.toml too.
            if self.current_provider_id() == provider_id and self.live_config.exists():
                live_old = self.live_config.read_text(encoding="utf-8")
                live_new = strip_model_provider_routing(live_old)
                if live_new != live_old:
                    result["live_config_changed"] = True
                    if not self.dry_run:
                        self.live_config.write_text(live_new, encoding="utf-8")
        finally:
            con.close()
        return result


def build_parser() -> argparse.ArgumentParser:
    default_codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    default_cc_switch_home = Path.home() / ".cc-switch"

    parser = argparse.ArgumentParser(
        description="Union all local Codex history, then retag it to the currently active cc-switch provider bucket.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home,
        help=f"Codex home directory. Default: {default_codex_home}",
    )
    parser.add_argument(
        "--cc-switch-home",
        type=Path,
        default=default_cc_switch_home,
        help=f"cc-switch home directory. Default: {default_cc_switch_home}",
    )
    parser.add_argument(
        "--sqlite-home",
        type=Path,
        default=None,
        help="Canonical sqlite_home to write into Codex configs. Default: <codex-home>/sqlite",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print final JSON report.",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)
    subparsers.add_parser(
        "sync",
        help="Run one sync pass: union all local history, then retag rollout/state history to the active provider bucket.",
    )

    watch_parser = subparsers.add_parser(
        "watch",
        help="Poll cc-switch and re-run union+retag sync after provider changes.",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds. Default: 2.0",
    )

    subparsers.add_parser("status", help="Print current status only.")

    set_official_parser = subparsers.add_parser(
        "set-official",
        help="Strip model_provider + [model_providers.*] routing from a provider so it uses official OpenAI.",
    )
    set_official_parser.add_argument(
        "provider_id",
        help="cc-switch codex provider id to make official (e.g. 'default').",
    )
    return parser


def run_watch(syncer: CodexHistorySync, interval: float) -> int:
    last_provider = None
    while True:
        current_provider = syncer.current_provider_id()
        if current_provider != last_provider:
            report = syncer.run_sync()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            last_provider = current_provider
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    syncer = CodexHistorySync(
        codex_home=args.codex_home,
        cc_switch_home=args.cc_switch_home,
        sqlite_home=args.sqlite_home,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )

    command = args.command or "sync"
    if command == "status":
        print(json.dumps(syncer.collect_status(), ensure_ascii=False, indent=2))
        return 0
    if command == "watch":
        return run_watch(syncer, args.interval)
    if command == "set-official":
        report = syncer.set_official(args.provider_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    report = syncer.run_sync()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
