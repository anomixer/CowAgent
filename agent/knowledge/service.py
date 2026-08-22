"""
Knowledge service for handling knowledge base operations.

Provides a unified interface for listing, reading, and graphing knowledge files,
callable from the web console, API, or CLI.

Knowledge file layout (under workspace_root):
    knowledge/index.md
    knowledge/log.md
    knowledge/<category>/<slug>.md
"""

import os
import re
import asyncio
import shutil
import threading
from pathlib import Path
from typing import Optional, Iterable
from urllib.parse import quote, unquote

from common.log import logger
from config import conf
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager


class KnowledgeService:
    """
    High-level service for knowledge base queries.
    Operates directly on the filesystem.
    """

    PROTECTED_FILES = {"index.md", "log.md"}
    INVALID_NAME_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
    IMPORT_EXTENSIONS = {".md", ".txt"}
    MAX_IMPORT_FILES = 100
    MAX_IMPORT_FILE_SIZE = 10 * 1024 * 1024
    MAX_IMPORT_TOTAL_SIZE = 200 * 1024 * 1024

    def __init__(self, workspace_root: str, memory_manager=None):
        self.workspace_root = os.path.abspath(workspace_root)
        self.knowledge_dir = os.path.join(self.workspace_root, "knowledge")
        self._memory_manager = memory_manager

    def _resolve_path(self, rel_path: str, *, kind: Optional[str] = None,
                      allow_missing: bool = True) -> tuple:
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("path is required")
        rel_path = rel_path.replace("\\", "/").strip("/")
        parts = rel_path.split("/")
        if any(not p or p in (".", "..") or self.INVALID_NAME_RE.search(p) for p in parts):
            raise ValueError("invalid path")
        if kind == "document" and not rel_path.lower().endswith(".md"):
            raise ValueError("document path must end with .md")

        root = Path(self.knowledge_dir).resolve()
        candidate = root.joinpath(*parts)
        # Resolve the nearest existing ancestor so a symlink cannot be used
        # to escape when the final destination does not exist yet.
        ancestor = candidate
        while not ancestor.exists() and ancestor != root:
            ancestor = ancestor.parent
        try:
            ancestor.resolve().relative_to(root)
        except ValueError:
            raise ValueError("path outside knowledge dir")
        if candidate.exists():
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                raise ValueError("path outside knowledge dir")
        elif not allow_missing:
            raise FileNotFoundError(f"path not found: {rel_path}")
        return rel_path, candidate

    def _ensure_not_protected(self, rel_path: str):
        if rel_path in self.PROTECTED_FILES:
            raise ValueError(f"protected knowledge file: {rel_path}")

    def _manager(self):
        if self._memory_manager is None:
            # Reuse the shared embedding provider selection so knowledge index
            # sync gets vectors too, instead of degrading to keyword-only.
            from agent.memory.embedding import create_default_embedding_provider
            embedding_provider = create_default_embedding_provider()
            self._memory_manager = MemoryManager(
                MemoryConfig(workspace_root=self.workspace_root),
                embedding_provider=embedding_provider,
            )
        return self._memory_manager

    @staticmethod
    def _run_sync(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        result = []
        error = []

        def runner():
            try:
                result.append(asyncio.run(coro))
            except Exception as exc:
                error.append(exc)

        thread = threading.Thread(target=runner)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0] if result else None

    def _sync_index(self, old_paths: Iterable[str], force: bool = False):
        old_paths = sorted(set(old_paths))
        if not old_paths and not force:
            return
        manager = self._manager()
        for rel_path in old_paths:
            manager.storage.delete_by_path(f"knowledge/{rel_path}")
        manager.mark_dirty()
        self._run_sync(manager.sync())

    @staticmethod
    def _extract_title(md_path: Path, fallback: str) -> str:
        """Read a markdown file's H1 title, falling back to the file stem."""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        return stripped[2:].strip() or fallback
        except Exception:
            pass
        return fallback

    def rebuild_index_md(self) -> bool:
        """Regenerate knowledge/index.md from the actual directory tree.

        Keeps the index in sync with real files so it never drifts or loses
        documents. Returns True when the file was (re)written.
        """
        root = Path(self.knowledge_dir)
        if not root.is_dir():
            return False

        def collect(dir_path: Path) -> list:
            # Return sorted (rel_path, title) tuples for *.md under dir_path,
            # excluding protected files at the knowledge root and dot files.
            entries = []
            for md in sorted(dir_path.rglob("*.md")):
                rel = md.relative_to(root).as_posix()
                if any(part.startswith(".") for part in md.relative_to(root).parts):
                    continue
                if rel in self.PROTECTED_FILES:
                    continue
                entries.append((rel, self._extract_title(md, md.stem)))
            return entries

        all_entries = collect(root)

        def link(rel: str) -> str:
            # Encode each path segment so spaces / special chars stay valid in
            # markdown links, while keeping the slashes between segments.
            encoded = "/".join(quote(part) for part in rel.split("/"))
            return f"./{encoded}"

        lines = ["# 知识库目录", ""]
        # Root-level documents first (no category dir).
        root_docs = [(rel, title) for rel, title in all_entries if "/" not in rel]
        for rel, title in root_docs:
            lines.append(f"- [{title}]({link(rel)})")
        if root_docs:
            lines.append("")

        # Group remaining documents by their top-level category.
        categories = {}
        for rel, title in all_entries:
            if "/" not in rel:
                continue
            category = rel.split("/", 1)[0]
            categories.setdefault(category, []).append((rel, title))

        for category in sorted(categories.keys()):
            lines.append(f"## {category}")
            for rel, title in categories[category]:
                lines.append(f"- [{title}]({link(rel)})")
            lines.append("")

        content = "\n".join(lines).rstrip() + "\n"
        index_path = root / "index.md"
        try:
            index_path.write_text(content, encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning(f"[KnowledgeService] Failed to rebuild index.md: {exc}")
            return False

    def _sanitize_document_name(self, filename: str) -> str:
        name = os.path.basename((filename or "").replace("\\", "/")).strip()
        if not name:
            raise ValueError("filename is required")
        stem, ext = os.path.splitext(name)
        if ext.lower() not in self.IMPORT_EXTENSIONS:
            raise ValueError(f"unsupported file type: {ext or name}")
        if not stem or stem in (".", "..") or self.INVALID_NAME_RE.search(stem):
            raise ValueError("invalid filename")
        safe_name = f"{stem}.md"
        self._ensure_not_protected(safe_name)
        return safe_name

    @staticmethod
    def _decode_document_content(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("document content is required")
        return bytes(content).decode("utf-8-sig", errors="replace")

    def _resolve_import_destination(self, target_category: str, filename: str,
                                    conflict_strategy: str) -> tuple:
        target_rel, target_full = self._resolve_path(target_category, kind="category")
        if not target_full.is_dir():
            raise FileNotFoundError(f"category not found: {target_rel}")

        safe_name = self._sanitize_document_name(filename)
        destination = target_full / safe_name
        rel_path = f"{target_rel}/{safe_name}"

        if destination.exists():
            if conflict_strategy == "skip":
                return rel_path, destination, "skip"
            if conflict_strategy == "rename":
                stem = destination.stem
                suffix = destination.suffix
                for index in range(1, 1000):
                    candidate = target_full / f"{stem}-{index}{suffix}"
                    if not candidate.exists():
                        candidate_rel = f"{target_rel}/{candidate.name}"
                        return candidate_rel, candidate, "write"
                raise FileExistsError(f"target already exists: {rel_path}")
            if conflict_strategy != "overwrite":
                raise ValueError("invalid conflict strategy")
        return rel_path, destination, "write"

    def create_document(self, path: str, content: str = "", overwrite: bool = False) -> dict:
        rel_path, full_path = self._resolve_path(path, kind="document")
        self._ensure_not_protected(rel_path)
        if len((content or "").encode("utf-8")) > self.MAX_IMPORT_FILE_SIZE:
            raise ValueError("file too large")
        if full_path.exists() and not overwrite:
            raise FileExistsError(f"target already exists: {rel_path}")
        old_paths = [rel_path] if full_path.exists() else []
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content or "", encoding="utf-8")
        # Keep index.md in sync before reindexing so it is indexed too.
        self.rebuild_index_md()
        self._sync_index(old_paths, force=True)
        return {"path": rel_path, "created": True, "overwritten": bool(old_paths)}

    def import_documents(self, target_category: str, files: Iterable[dict],
                         conflict_strategy: str = "skip") -> dict:
        if not isinstance(files, list):
            raise ValueError("files must be a list")
        if len(files) > self.MAX_IMPORT_FILES:
            raise ValueError(f"too many files: max {self.MAX_IMPORT_FILES}")
        results = []
        old_paths = []
        imported = skipped = failed = 0
        total_size = 0

        for item in files:
            filename = item.get("filename") if isinstance(item, dict) else None
            try:
                content_bytes = item.get("content") if isinstance(item, dict) else None
                size = len(content_bytes.encode("utf-8")) if isinstance(content_bytes, str) else len(content_bytes or b"")
                total_size += size
                if total_size > self.MAX_IMPORT_TOTAL_SIZE:
                    raise ValueError("import batch too large")
                if size > self.MAX_IMPORT_FILE_SIZE:
                    raise ValueError("file too large")
                rel_path, destination, mode = self._resolve_import_destination(
                    target_category, filename, conflict_strategy
                )
                if mode == "skip":
                    skipped += 1
                    results.append({"filename": filename, "path": rel_path, "status": "skipped",
                                    "reason": "target_exists"})
                    continue

                old_exists = destination.exists()
                content = self._decode_document_content(content_bytes)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                if old_exists:
                    old_paths.append(rel_path)
                imported += 1
                results.append({"filename": filename, "path": rel_path, "status": "imported",
                                "overwritten": old_exists})
            except Exception as exc:
                failed += 1
                results.append({"filename": filename or "", "status": "failed", "reason": str(exc)})

        if imported:
            # Keep index.md in sync before reindexing so it is indexed too.
            self.rebuild_index_md()
            self._sync_index(old_paths, force=True)
        return {"results": results, "imported": imported, "skipped": skipped, "failed": failed}

    def create_category(self, path: str) -> dict:
        rel_path, full_path = self._resolve_path(path, kind="category")
        if full_path.exists():
            return {"path": rel_path, "created": False, "reason": "already_exists"}
        full_path.mkdir(parents=True)
        return {"path": rel_path, "created": True}

    def rename_category(self, path: str, new_path: str) -> dict:
        old_rel, old_full = self._resolve_path(path, kind="category", allow_missing=False)
        new_rel, new_full = self._resolve_path(new_path, kind="category")
        if not old_full.is_dir():
            raise ValueError(f"not a category: {old_rel}")
        if new_full.exists():
            raise FileExistsError(f"target already exists: {new_rel}")
        old_documents = [str(p.relative_to(old_full)).replace(os.sep, "/")
                         for p in old_full.rglob("*.md") if p.is_file()]
        new_full.parent.mkdir(parents=True, exist_ok=True)
        try:
            old_full.rename(new_full)
        except FileNotFoundError:
            return {"old_path": old_rel, "path": new_rel, "moved": False, "reason": "not_found"}
        except FileExistsError:
            raise FileExistsError(f"target already exists: {new_rel}")
        old_paths = [f"{old_rel}/{p}" for p in old_documents]
        self._sync_index(old_paths)
        return {"old_path": old_rel, "path": new_rel, "moved_documents": len(old_documents)}

    def delete_category(self, path: str, confirm: bool = False) -> dict:
        rel_path, full_path = self._resolve_path(path, kind="category")
        if not full_path.exists():
            return {"path": rel_path, "deleted": False, "reason": "not_found"}
        if not full_path.is_dir():
            raise ValueError(f"not a category: {rel_path}")
        knowledge_root = Path(self.knowledge_dir).resolve()
        documents = [str(p.relative_to(knowledge_root)).replace(os.sep, "/")
                     for p in full_path.rglob("*.md") if p.is_file()]
        if any(p in self.PROTECTED_FILES for p in documents):
            raise ValueError("category contains protected knowledge files")
        if any(full_path.iterdir()) and not confirm:
            raise ValueError("category is not empty; confirmation is required")
        try:
            shutil.rmtree(full_path)
        except FileNotFoundError:
            return {"path": rel_path, "deleted": False, "reason": "not_found"}
        self._sync_index(documents)
        return {"path": rel_path, "deleted": True, "deleted_documents": len(documents)}

    def delete_documents(self, paths: Iterable[str]) -> dict:
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        results = []
        deleted = []
        for path in paths:
            rel_path, full_path = self._resolve_path(path, kind="document")
            self._ensure_not_protected(rel_path)
            if not full_path.exists():
                deleted.append(rel_path)
                results.append({"path": rel_path, "deleted": False, "reason": "not_found"})
                continue
            if not full_path.is_file():
                raise ValueError(f"not a document: {rel_path}")
            try:
                full_path.unlink()
                deleted.append(rel_path)
                results.append({"path": rel_path, "deleted": True})
            except FileNotFoundError:
                deleted.append(rel_path)
                results.append({"path": rel_path, "deleted": False, "reason": "not_found"})
        self._sync_index(deleted)
        return {"results": results, "deleted": sum(1 for item in results if item["deleted"])}

    def move_documents(self, paths: Iterable[str], target_category: str) -> dict:
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        target_rel, target_full = self._resolve_path(target_category, kind="category")
        if not target_full.is_dir():
            raise FileNotFoundError(f"category not found: {target_rel}")
        results = []
        moved_old_paths = []
        for path in paths:
            rel_path, full_path = self._resolve_path(path, kind="document")
            self._ensure_not_protected(rel_path)
            if not full_path.exists():
                results.append({"path": rel_path, "moved": False, "reason": "not_found"})
                continue
            destination = target_full / full_path.name
            new_rel = str(destination.relative_to(Path(self.knowledge_dir).resolve())).replace(os.sep, "/")
            if destination.exists():
                results.append({"path": rel_path, "moved": False, "reason": "target_exists",
                                "target": new_rel})
                continue
            try:
                os.link(full_path, destination)
                full_path.unlink()
                moved_old_paths.append(rel_path)
                results.append({"path": rel_path, "moved": True, "target": new_rel})
            except FileExistsError:
                results.append({"path": rel_path, "moved": False, "reason": "target_exists",
                                "target": new_rel})
            except FileNotFoundError:
                results.append({"path": rel_path, "moved": False, "reason": "not_found"})
        self._sync_index(moved_old_paths)
        return {"results": results, "moved": len(moved_old_paths)}

    # ------------------------------------------------------------------
    # list — directory tree with stats
    # ------------------------------------------------------------------
    def list_tree(self) -> dict:
        """
        Return the knowledge directory tree grouped by category,
        supporting arbitrarily nested sub-directories.

        Returns::

            {
                "tree": [
                    {
                        "dir": "concepts",
                        "files": [
                            {"name": "moe.md", "title": "MoE", "size": 1234},
                        ],
                        "children": []
                    },
                    {
                        "dir": "platform",
                        "files": [],
                        "children": [
                            {
                                "dir": "analysis",
                                "files": [{"name": "perf.md", ...}],
                                "children": []
                            }
                        ]
                    },
                ],
                "stats": {"pages": 15, "size": 32768},
                "enabled": true
            }
        """
        if not os.path.isdir(self.knowledge_dir):
            return {"tree": [], "stats": {"pages": 0, "size": 0}, "enabled": conf().get("knowledge", True)}

        stats = {"pages": 0, "size": 0}
        root_files, tree = self._scan_dir(self.knowledge_dir, stats, is_root=True)

        return {
            "root_files": root_files,
            "tree": tree,
            "stats": stats,
            "enabled": conf().get("knowledge", True),
        }

    def list_scoped_tree(self, user_id: int = 0, role: str = "admin") -> dict:
        """
        Return scoped knowledge trees separated into Personal and Team trees.
        Admin sees all team trees; regular users see personal tree + their teams' trees.
        """
        if not os.path.isdir(self.knowledge_dir):
            return {
                "personal_tree": {"root_files": [], "tree": [], "stats": {"pages": 0, "size": 0}},
                "team_trees": [],
                "enabled": conf().get("knowledge", True)
            }

        # 1. Personal Knowledge Tree (knowledge/users/{user_id}/)
        personal_dir = os.path.join(self.knowledge_dir, "users", str(user_id)) if user_id > 0 else self.knowledge_dir
        os.makedirs(personal_dir, exist_ok=True)
        p_stats = {"pages": 0, "size": 0}
        p_root_files, p_tree = self._scan_dir(personal_dir, p_stats, is_root=True)

        # 2. Team Knowledge Trees (knowledge/teams/{team_id}/)
        team_trees = []
        try:
            from channel.web.multiuser.db import get_multiuser_db
            db = get_multiuser_db()
            if db.user_count() > 0:
                teams = db.list_teams() if role == "admin" else db.list_user_teams(user_id)
                for t in teams:
                    t_id = t["id"]
                    t_dir = os.path.join(self.knowledge_dir, "teams", str(t_id))
                    os.makedirs(t_dir, exist_ok=True)
                    t_stats = {"pages": 0, "size": 0}
                    t_root_files, t_tree = self._scan_dir(t_dir, t_stats, is_root=True)
                    team_trees.append({
                        "team_id": t_id,
                        "team_name": t["name"],
                        "root_files": t_root_files,
                        "tree": t_tree,
                        "stats": t_stats
                    })
        except Exception as exc:
            logger.warning(f"[KnowledgeService] Failed to load team knowledge trees: {exc}")

        # 3. Knowledge shared with me (other users' personal KBs, per mu_kb_shares).
        # Surfaces as its own group so the recipient can browse/open it; the
        # read endpoint honours the same shares via check_read_access.
        shared_trees = []
        try:
            from channel.web.multiuser.db import get_multiuser_db
            db = get_multiuser_db()
            if db.user_count() > 0 and role != "admin":
                owner_ids = db.get_shared_user_ids(user_id)
                for owner_id in owner_ids:
                    if not owner_id or int(owner_id) == int(user_id):
                        continue
                    owner_dir = os.path.join(self.knowledge_dir, "users", str(owner_id))
                    if not os.path.isdir(owner_dir):
                        continue
                    owner_user = db.get_user_by_id(owner_id)
                    owner_name = (owner_user or {}).get("username") or f"user_{owner_id}"
                    s_stats = {"pages": 0, "size": 0}
                    s_root_files, s_tree = self._scan_dir(owner_dir, s_stats, is_root=True)
                    shared_trees.append({
                        "owner_id": int(owner_id),
                        "owner_name": owner_name,
                        "root_files": s_root_files,
                        "tree": s_tree,
                        "stats": s_stats
                    })
        except Exception as exc:
            logger.warning(f"[KnowledgeService] Failed to load shared knowledge trees: {exc}")

        # Fallback legacy tree scan
        legacy_stats = {"pages": 0, "size": 0}
        l_root_files, l_tree = self._scan_dir(self.knowledge_dir, legacy_stats, is_root=True)

        return {
            "root_files": l_root_files,
            "tree": l_tree,
            "stats": legacy_stats,
            "personal_tree": {
                "root_files": p_root_files,
                "tree": p_tree,
                "stats": p_stats
            },
            "team_trees": team_trees,
            "shared_trees": shared_trees,
            "enabled": conf().get("knowledge", True)
        }

    def _scan_dir(self, dir_path: str, stats: dict, is_root: bool = False) -> tuple:
        """
        Recursively scan a directory.

        :return: (files, children) where files is a list of .md file dicts
                 in this directory and children is a list of sub-directory nodes.
        """
        files = []
        children = []
        for name in sorted(os.listdir(dir_path)):
            if name.startswith("."):
                continue
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                sub_files, sub_children = self._scan_dir(full, stats)
                children.append({"dir": name, "files": sub_files, "children": sub_children})
            elif name.endswith(".md"):
                size = os.path.getsize(full)
                if not is_root:
                    stats["pages"] += 1
                    stats["size"] += size
                # Prefer the H1 heading as a readable title for normal docs.
                # System files (index.md / log.md) keep their filename so the
                # tree never hides what they actually are.
                title = name[:-3]
                if name not in self.PROTECTED_FILES:
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            first_line = f.readline().strip()
                        if first_line.startswith("# "):
                            title = first_line[2:].strip() or title
                    except Exception:
                        pass
                files.append({"name": name, "title": title, "size": size})
        return files, children

    # ------------------------------------------------------------------
    # read — single file content
    # ------------------------------------------------------------------
    def read_file(self, rel_path: str) -> dict:
        """
        Read a single knowledge markdown file.

        :param rel_path: Relative path within knowledge/, e.g. ``concepts/moe.md``
        :return: dict with ``content`` and ``path``
        :raises ValueError: if path is invalid or escapes knowledge dir
        :raises FileNotFoundError: if file does not exist
        """
        rel_path, full_path = self._resolve_path(rel_path, kind="document")
        if not full_path.is_file():
            raise FileNotFoundError(f"file not found: {rel_path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "path": rel_path}

    def check_read_access(self, rel_path: str, user_id: int = 0, role: str = "admin") -> bool:
        """
        Whether ``user_id`` may read the knowledge file at ``rel_path``.

        The list endpoint scopes what a user *sees* to their own personal
        tree, their teams' trees, and the shared root — but ``read_file`` is a
        raw path reader, so without this check any authenticated user could
        read another user's private ``users/<other>/...`` file by crafting the
        path (the list would hide it, the read would still serve it). This
        mirrors ``list_scoped_tree``'s scoping so the two agree.

        Allowed for a non-admin:
          - ``users/<own_id>/...``                       (own personal KB)
          - ``users/<owner>/...``  where ``owner`` shared with them (directly or via team)
          - ``teams/<tid>/...``   where the user is a member of team ``tid``
          - anything else at the knowledge root          (legacy / shared KB, visible to all)

        Admins and single-user mode (``role == "admin"``) are unrestricted,
        preserving the pre-multi-user behaviour.
        """
        if role == "admin":
            return True
        rel = rel_path.replace("\\", "/").strip("/")
        segs = [s for s in rel.split("/") if s]
        if not segs:
            return True
        first = segs[0]
        try:
            from channel.web.multiuser.db import get_multiuser_db
            db = get_multiuser_db()
        except Exception:
            # DB unavailable: fail safe for private namespaces, allow shared root.
            return first not in ("users", "teams")

        if first == "users" and len(segs) >= 2:
            owner = segs[1]
            if owner == str(user_id):
                return True
            if owner.isdigit():
                try:
                    shared = db.get_shared_user_ids(user_id)
                except Exception:
                    shared = []
                return int(owner) in shared
            return False

        if first == "teams" and len(segs) >= 2:
            tid = segs[1]
            if not tid.isdigit():
                return False
            try:
                return tid in [str(t) for t in db.get_user_team_ids(user_id)]
            except Exception:
                return False

        # Legacy / shared root knowledge (not under users/ or teams/): visible to all.
        return True

    def _allowed_namespaces(self, user_id: int) -> dict:
        """The caller's accessible knowledge namespaces: their own user id, the
        owners who shared a KB with them, and the team ids they belong to.
        Shared with ``build_graph``'s scoping so the two agree with
        ``check_read_access`` / ``list_scoped_tree``."""
        owners = {str(user_id)}
        teams = set()
        try:
            from channel.web.multiuser.db import get_multiuser_db
            db = get_multiuser_db()
            if db.user_count() > 0:
                for o in db.get_shared_user_ids(user_id):
                    if o and int(o) != int(user_id):
                        owners.add(str(o))
                for t in db.get_user_team_ids(user_id):
                    teams.add(str(t))
        except Exception:
            pass
        return {"owners": owners, "teams": teams}

    def _can_write(self, rel_path: str, user_id: int, role: str) -> bool:
        """Whether the caller may WRITE (create/delete/rename/move/import) at
        ``rel_path``. Writable scope is narrower than the read scope: only the
        caller's own personal KB and their teams (collaborative). Shared-with-me
        KBs are read-only, and the legacy shared root is left to admins — so a
        user cannot delete/rewrite another user's files via the action API.
        Admins and single-user (``role == "admin"``) are unrestricted."""
        if role == "admin":
            return True
        rel = rel_path.replace("\\", "/").strip("/")
        segs = [s for s in rel.split("/") if s]
        if not segs:
            return False  # writing at the shared root requires admin
        first = segs[0]
        if first == "users":
            return len(segs) >= 2 and segs[1] == str(user_id)
        if first == "teams":
            if not (len(segs) >= 2 and segs[1].isdigit()):
                return False
            try:
                from channel.web.multiuser.db import get_multiuser_db
                return segs[1] in [str(t) for t in get_multiuser_db().get_user_team_ids(user_id)]
            except Exception:
                return False
        return False



    # ------------------------------------------------------------------
    # graph — nodes and links for visualization
    # ------------------------------------------------------------------
    def build_graph(self, user_id: int = 0, role: str = "admin") -> dict:
        """
        Parse knowledge pages and extract cross-reference links.

        ``user_id``/``role`` scope the graph to what the caller may see (their
        own personal KB, their teams, KBs shared with them, plus the legacy
        shared root) — mirroring ``list_scoped_tree`` / ``check_read_access``.
        Without this, the graph leaked every user's KB *structure* (file names
        and links) to any authenticated caller. Admins / single-user are
        unrestricted.
        """
        knowledge_path = Path(self.knowledge_dir)
        if not knowledge_path.is_dir():
            return {"nodes": [], "links": []}

        # Precompute the caller's allowed namespaces (None = unrestricted).
        allowed = None
        if role != "admin":
            allowed = self._allowed_namespaces(user_id)

        def _ok(rel: str) -> bool:
            if allowed is None:
                return True
            rel = rel.replace("\\", "/").strip("/")
            parts = [s for s in rel.split("/") if s]
            if not parts:
                return True
            if parts[0] not in ("users", "teams"):
                return True  # legacy / shared root
            if len(parts) < 2:
                return False
            if parts[0] == "users":
                return parts[1] == str(user_id) or parts[1] in allowed["owners"]
            return parts[1] in allowed["teams"]

        nodes = {}
        links = []
        # Trailing "#anchor" is optional so section links still count as edges.
        link_re = re.compile(r'\[([^\]]*)\]\(([^)#]+\.md)(?:#[^)]*)?\)')

        for md_file in knowledge_path.rglob("*.md"):
            rel = str(md_file.relative_to(knowledge_path))
            if rel in ("index.md", "log.md"):
                continue
            if not _ok(rel):
                continue
            parts = rel.split("/")
            category = parts[0] if len(parts) > 1 else "root"
            title = md_file.stem.replace("-", " ").title()
            try:
                content = md_file.read_text(encoding="utf-8")
                first_line = content.strip().split("\n")[0]
                if first_line.startswith("# "):
                    title = first_line[2:].strip()
                for _, link_target in link_re.findall(content):
                    if "://" in link_target:
                        continue
                    # Links written by the index generator are percent-encoded;
                    # they must be decoded to match a path on disk.
                    resolved = (md_file.parent / unquote(link_target)).resolve()
                    try:
                        target_rel = str(resolved.relative_to(knowledge_path))
                    except ValueError:
                        continue
                    if target_rel != rel:
                        links.append({"source": rel, "target": target_rel})
            except Exception:
                pass
            nodes[rel] = {"id": rel, "label": title, "category": category}

        valid_ids = set(nodes.keys())
        links = [l for l in links if l["source"] in valid_ids and l["target"] in valid_ids]
        seen = set()
        deduped = []
        for l in links:
            key = tuple(sorted([l["source"], l["target"]]))
            if key not in seen:
                seen.add(key)
                deduped.append(l)

        return {"nodes": list(nodes.values()), "links": deduped}

    # ------------------------------------------------------------------
    # dispatch — single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a knowledge management action.

        :param action: ``list``, ``read``, or ``graph``
        :param payload: action-specific payload
        :return: protocol-compatible response dict
        """
        payload = payload or {}
        user_id = int(payload.pop("_user_id", 0) or 0)
        role = str(payload.pop("_role", "admin") or "admin")

        # Writable scope: non-admins may only write to their own personal KB or
        # their teams. Enforced once, here, for every mutating action below.
        WRITE_ACTIONS = {
            "create_category", "rename_category", "delete_category",
            "delete_documents", "move_documents", "create_document",
            "import_documents",
        }

        def _deny_write(rel: str) -> Optional[dict]:
            """Return a 403 response dict if the caller may not write to ``rel``."""
            if role != "admin" and not self._can_write(rel, user_id, role):
                return {"action": action, "code": 403,
                        "message": "無權限寫入此知識庫 / Not authorized to write here", "payload": None}
            return None
        try:
            if action == "list":
                result = self.list_tree()
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "read":
                path = payload.get("path")
                if not path:
                    return {"action": action, "code": 400, "message": "path is required", "payload": None}
                result = self.read_file(path)
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "graph":
                result = self.build_graph()
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "create_category":
                den = _deny_write(payload.get("path") or "")
                if den: return den
                result = self.create_category(payload.get("path"))
            elif action == "rename_category":
                for p in (payload.get("path"), payload.get("new_path")):
                    den = _deny_write(p or "")
                    if den: return den
                result = self.rename_category(payload.get("path"), payload.get("new_path"))
            elif action == "delete_category":
                den = _deny_write(payload.get("path") or "")
                if den: return den
                result = self.delete_category(payload.get("path"), payload.get("confirm", False))
            elif action == "delete_documents":
                for p in (payload.get("paths") or []):
                    den = _deny_write(p)
                    if den: return den
                result = self.delete_documents(payload.get("paths") or [])
            elif action == "move_documents":
                for p in (payload.get("paths") or []) + [payload.get("target_category")]:
                    den = _deny_write(p or "")
                    if den: return den
                result = self.move_documents(payload.get("paths") or [], payload.get("target_category"))
            elif action == "create_document":
                den = _deny_write(payload.get("path") or "")
                if den: return den
                result = self.create_document(payload.get("path"), payload.get("content", ""),
                                              payload.get("overwrite", False))
            elif action == "import_documents":
                den = _deny_write(payload.get("target_category") or "")
                if den: return den
                result = self.import_documents(
                    payload.get("target_category"),
                    payload.get("files") or [],
                    payload.get("conflict_strategy", "skip"),
                )
            else:
                return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
            return {"action": action, "code": 200, "message": "success", "payload": result}

        except ValueError as e:
            return {"action": action, "code": 403, "message": str(e), "payload": None}
        except FileNotFoundError as e:
            return {"action": action, "code": 404, "message": str(e), "payload": None}
        except FileExistsError as e:
            return {"action": action, "code": 409, "message": str(e), "payload": None}
        except Exception as e:
            logger.error(f"[KnowledgeService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}
