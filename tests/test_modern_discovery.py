import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_saves import db, export, importer, paths


def create_db(path: Path, modern: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    if modern:
        conn.execute(
            """CREATE TABLE composerHeaders (
                composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER,
                lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER,
                recency INTEGER, checkpointAt INTEGER, value TEXT,
                subagentTypeName TEXT
            )"""
        )
    conn.commit()
    conn.close()


def put_json(path: Path, key: str, value: dict, table: str = "cursorDiskKV") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def add_modern_header(
    path: Path,
    composer_id: str,
    workspace_id: str,
    *,
    archived: bool = False,
    subagent: bool = False,
) -> None:
    header = {
        "type": "head",
        "composerId": composer_id,
        "workspaceIdentifier": {"id": workspace_id},
        "createdAt": 100,
        "lastUpdatedAt": 200,
        "isArchived": archived,
    }
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            composer_id,
            workspace_id,
            100,
            200,
            int(archived),
            int(subagent),
            200,
            None,
            json.dumps(header),
            "task" if subagent else "",
        ),
    )
    conn.commit()
    conn.close()


def add_composer_data(path: Path, composer_id: str, name: str) -> None:
    put_json(
        path,
        f"composerData:{composer_id}",
        {
            "composerId": composer_id,
            "name": name,
            "createdAt": 100,
            "lastUpdatedAt": 200,
            "unifiedMode": "agent",
            "fullConversationHeadersOnly": [{"bubbleId": "bubble-1"}],
        },
    )


class ModernDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.global_db = self.root / "globalStorage" / "state.vscdb"
        self.workspace_storage = self.root / "workspaceStorage"
        create_db(self.global_db, modern=True)
        paths.invalidate_headers_cache()

        self.patches = [
            patch.object(paths, "get_global_db_path", return_value=self.global_db),
            patch.object(paths, "get_workspace_storage_dir", return_value=self.workspace_storage),
            patch.object(paths, "get_project_identifier", return_value="project"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        paths.invalidate_headers_cache()
        self.temp.cleanup()

    def make_workspace(self, workspace_id: str, data: dict) -> Path:
        ws_dir = self.workspace_storage / workspace_id
        ws_dir.mkdir(parents=True)
        (ws_dir / "workspace.json").write_text(json.dumps(data))
        create_db(ws_dir / "state.vscdb")
        return ws_dir

    def test_modern_top_level_discovery_deduplicates_legacy_and_excludes_subagents(self) -> None:
        active_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        ws1 = self.make_workspace(
            "workspace-one",
            {
                "workspace": (
                    "vscode-remote://ssh-remote%2B"
                    "7b22686f73744e616d65223a22616c772d6665746368657272227d"
                    "/Users/alw/fetcherr/fetcherr-remote.code-workspace"
                )
            },
        )
        ws2 = self.make_workspace("workspace-two", {"folder": "file:///tmp/other"})
        add_modern_header(self.global_db, active_id, ws1.name)
        add_modern_header(self.global_db, "archived", ws1.name, archived=True)
        add_modern_header(self.global_db, "subagent", ws1.name, subagent=True)
        add_modern_header(self.global_db, "other", ws2.name)
        for cid in (active_id, "archived", "subagent", "other"):
            add_composer_data(self.global_db, cid, cid.title())
        put_json(self.global_db, f"bubbleId:{active_id}:bubble-1", {"text": "first"})
        put_json(self.global_db, f"bubbleId:{active_id}:bubble-2", {"text": "second"})

        put_json(
            self.global_db,
            "composer.composerHeaders",
            {
                "allComposers": [
                    {"composerId": active_id, "workspaceIdentifier": {"id": ws1.name}},
                    {"composerId": "legacy-only", "workspaceIdentifier": {"id": ws1.name}},
                ]
            },
            table="ItemTable",
        )
        add_composer_data(self.global_db, "legacy-only", "Legacy")

        self.assertEqual(
            set(paths.get_workspace_composer_ids(ws1 / "state.vscdb")),
            {active_id, "archived", "legacy-only"},
        )
        self.assertEqual(paths.get_workspace_composer_ids(ws2 / "state.vscdb"), ["other"])

        conversations = export.list_conversations("/unused", workspace_dir=ws1)
        by_id = {item["id"]: item for item in conversations}
        self.assertEqual(set(by_id), {active_id, "archived", "legacy-only"})
        self.assertFalse(by_id[active_id]["archived"])
        self.assertEqual(by_id[active_id]["messageCount"], 2)
        self.assertTrue(by_id["archived"]["archived"])
        self.assertEqual(by_id["archived"]["name"], "Archived")

        workspaces = paths.list_workspaces_with_conversations()
        by_workspace = {item["workspace_dir"].name: item for item in workspaces}
        self.assertEqual(by_workspace[ws1.name]["conversations"], 3)
        self.assertEqual(by_workspace[ws1.name]["type"], "ssh")
        self.assertEqual(by_workspace[ws1.name]["host"], "alw-fetcherr")
        self.assertEqual(by_workspace[ws2.name]["conversations"], 1)

    def test_archived_snapshot_round_trip_preserves_data_and_header(self) -> None:
        source_ws = self.make_workspace("source", {"folder": "file:///source/project"})
        add_modern_header(self.global_db, "archived", source_ws.name, archived=True)
        add_composer_data(self.global_db, "archived", "Archived chat")
        put_json(self.global_db, "bubbleId:archived:bubble-1", {"text": "hello"})
        put_json(self.global_db, "checkpointId:archived:checkpoint-1", {"path": "/source/project"})

        header = paths._build_global_headers_map()[source_ws.name][0]
        snapshot = export.export_conversation(
            "/source/project", "archived", composer_header=header
        )
        self.assertTrue(snapshot["composerHeader"]["isArchived"])
        self.assertEqual(snapshot["bubbleEntries"]["bubble-1"]["text"], "hello")
        self.assertIn("checkpoint-1", snapshot["checkpoints"])

        snapshot_path = self.root / "archived.json.gz"
        with gzip.open(snapshot_path, "wt", encoding="utf-8") as stream:
            json.dump(snapshot, stream)

        destination_db = self.root / "destination" / "state.vscdb"
        create_db(destination_db, modern=True)
        destination_ws = self.root / "destination-workspaces" / "target"
        destination_ws.mkdir(parents=True)
        (destination_ws / "workspace.json").write_text(
            json.dumps({"folder": "file:///destination/project"})
        )
        create_db(destination_ws / "state.vscdb")

        with patch.object(paths, "get_global_db_path", return_value=destination_db):
            self.assertTrue(
                importer.import_snapshot(
                    snapshot_path,
                    "/destination/project",
                    target_workspace_dir=destination_ws,
                    skip_backup=True,
                )
            )

        with db.CursorDB(destination_db) as cdb:
            restored = cdb.get_composer_headers()
            self.assertEqual(len(restored), 1)
            self.assertTrue(restored[0]["isArchived"])
            self.assertEqual(
                cdb.get_json("bubbleId:archived:bubble-1")["text"], "hello"
            )
            self.assertIsNotNone(cdb.get_json("checkpointId:archived:checkpoint-1"))


class LegacyDiscoveryTests(unittest.TestCase):
    def test_database_without_modern_table_keeps_legacy_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            global_db = root / "global" / "state.vscdb"
            ws_dir = root / "workspaces" / "legacy-workspace"
            create_db(global_db)
            create_db(ws_dir / "state.vscdb")
            put_json(
                ws_dir / "state.vscdb",
                "composer.composerData",
                {
                    "allComposers": [
                        {"composerId": "legacy", "name": "Legacy chat", "createdAt": 1}
                    ]
                },
                table="ItemTable",
            )
            add_composer_data(global_db, "legacy", "Legacy chat")

            with patch.object(paths, "get_global_db_path", return_value=global_db):
                paths.invalidate_headers_cache()
                self.assertEqual(
                    paths.get_workspace_composer_ids(ws_dir / "state.vscdb"),
                    ["legacy"],
                )
                conversations = export.get_workspace_conversations(
                    "/legacy", workspace_dir=ws_dir
                )
                self.assertEqual([item["composerId"] for item in conversations], ["legacy"])
                paths.invalidate_headers_cache()


if __name__ == "__main__":
    unittest.main()
