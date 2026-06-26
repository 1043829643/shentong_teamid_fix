#!/usr/bin/env python3
"""Local web UI for Dota2 same-roster team ID detection.

Run this server locally, then open the printed URL. The browser talks only to
this local process; the StarRocks password stays in the Python process through
the STARROCKS_PASSWORD environment variable.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from detect_same_roster_team_ids import (
    FieldMapping,
    connect,
    discover_candidates,
    run_detection,
    run_player_track,
    search_player_candidates,
    write_csv,
)


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "same_roster_visualizer.html"
DEFAULT_OUTPUT = BASE_DIR / "same_roster_different_team_ids.csv"
DEFAULT_MANUAL_RECORDS = BASE_DIR / "manual_team_id_records.csv"
MANUAL_RECORD_FIELDS = [
    "group_id",
    "roster",
    "league_id",
    "league_name",
    "team_id",
    "team_name",
    "note",
]


def server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Dota2 roster visualizer locally.")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local web server host")
    parser.add_argument("--listen-port", type=int, default=8000, help="Local web server port")
    parser.add_argument("--starrocks-host", default="47.86.96.51", help="StarRocks FE MySQL host")
    parser.add_argument("--starrocks-port", type=int, default=9030, help="StarRocks FE MySQL port")
    parser.add_argument("--user", default="dota2_reader", help="StarRocks username")
    parser.add_argument("--password-env", default="STARROCKS_PASSWORD", help="Password environment variable name")
    parser.add_argument("--database", default="dota2_stats", help="Database/schema used for candidate discovery")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="CSV output path")
    parser.add_argument("--manual-records", default=str(DEFAULT_MANUAL_RECORDS), help="Manual CSV record path")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--read-timeout", type=int, default=300)
    parser.add_argument("--write-timeout", type=int, default=300)
    return parser.parse_args()


def detection_args(args: argparse.Namespace, **overrides: Any) -> SimpleNamespace:
    values = {
        "host": args.starrocks_host,
        "port": args.starrocks_port,
        "user": args.user,
        "password": None,
        "password_env": args.password_env,
        "database": args.database,
        "table": None,
        "league_col": None,
        "team_col": None,
        "match_col": None,
        "player_col": None,
        "time_col": None,
        "league_id": None,
        "start_time": None,
        "end_time": None,
        "detection_mode": "same_league",
        "max_diff": 0,
        "output": args.output,
        "limit": None,
        "max_candidates": 20,
        "list_candidates": False,
        "yes": True,
        "print_sql": False,
        "connect_timeout": args.connect_timeout,
        "read_timeout": args.read_timeout,
        "write_timeout": args.write_timeout,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def mapping_from_candidate(candidate: dict[str, Any]) -> FieldMapping:
    return FieldMapping(
        database=str(candidate["database"]),
        table=str(candidate["table"]),
        league_col=str(candidate["league_col"]),
        team_col=str(candidate["team_col"]),
        match_col=str(candidate["match_col"]),
        player_col=str(candidate["player_col"]),
        time_col=candidate.get("time_col") or None,
        score=int(candidate.get("score") or 0),
    )


def read_manual_records(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        return [{field: row.get(field, "") for field in MANUAL_RECORD_FIELDS} for row in reader]


def write_manual_records(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=MANUAL_RECORD_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
    except PermissionError as error:
        raise PermissionError(
            f"无法写入 {path.name}：文件可能正在被 Excel / WPS 等程序打开并锁定。"
            f"请先关闭该 CSV 文件，再回到网页点击保存。（原始错误：{error}）"
        ) from error


class RosterHandler(SimpleHTTPRequestHandler):
    app_args: argparse.Namespace

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_file(HTML_FILE)
                return
            if parsed.path == "/api/candidates":
                query = parse_qs(parsed.query)
                database = query.get("database", [self.app_args.database])[0] or None
                self.handle_candidates(database)
                return
            if parsed.path == "/api/report.csv":
                self.send_file(Path(self.app_args.output))
                return
            if parsed.path == "/api/manual-records":
                self.handle_manual_records()
                return
            if parsed.path == "/api/player-candidates":
                query = parse_qs(parsed.query)
                self.handle_player_candidates((query.get("q", [""])[0] or "").strip())
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/detect":
                self.handle_detect()
                return
            if parsed.path == "/api/manual-records":
                self.handle_save_manual_record()
                return
            if parsed.path == "/api/manual-records/save-all":
                self.handle_save_all_manual_records()
                return
            if parsed.path == "/api/manual-records/delete":
                self.handle_delete_manual_record()
                return
            if parsed.path == "/api/player-track":
                self.handle_player_track()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_candidates(self, database: str | None) -> None:
        connection = connect(detection_args(self.app_args, database=database))
        try:
            candidates = discover_candidates(connection, database)
            self.send_json({"candidates": [asdict(candidate) for candidate in candidates[:50]]})
        finally:
            connection.close()

    def handle_detect(self) -> None:
        body = self.read_json_body()
        candidate_payload = body.get("candidate")
        if not isinstance(candidate_payload, dict):
            self.send_json({"error": "缺少候选表信息，请先点击“读取候选表”。"}, HTTPStatus.BAD_REQUEST)
            return

        limit = body.get("limit")
        try:
            limit_value = int(limit) if str(limit or "").strip() else None
        except ValueError:
            self.send_json({"error": "limit 必须是数字。"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            max_diff = int(body.get("max_diff") or 0)
        except (TypeError, ValueError):
            max_diff = 0
        if max_diff not in (0, 1, 2):
            self.send_json({"error": "max_diff 只能是 0、1 或 2。"}, HTTPStatus.BAD_REQUEST)
            return

        mapping = mapping_from_candidate(candidate_payload)
        args = detection_args(
            self.app_args,
            database=mapping.database,
            league_id=str(body.get("league_id")).strip() if body.get("league_id") else None,
            start_time=str(body.get("start_time")).strip() if body.get("start_time") else None,
            end_time=str(body.get("end_time")).strip() if body.get("end_time") else None,
            detection_mode=str(body.get("detection_mode") or "same_league"),
            max_diff=max_diff,
            limit=limit_value,
        )

        connection = connect(args)
        try:
            rows = run_detection(connection, mapping, args)
            write_csv(self.app_args.output, rows)
            self.send_json(
                {
                    "rows": jsonable(rows),
                    "output": os.path.abspath(self.app_args.output),
                    "mapping": asdict(mapping),
                }
            )
        finally:
            connection.close()

    def handle_player_candidates(self, query: str) -> None:
        if not query:
            self.send_json({"candidates": []})
            return
        args = detection_args(self.app_args)
        connection = connect(args)
        try:
            self.send_json({"candidates": search_player_candidates(connection, args, query)})
        finally:
            connection.close()

    def handle_player_track(self) -> None:
        body = self.read_json_body()
        steamid = str(body.get("steamid") or "").strip()
        if not steamid.isdigit():
            self.send_json({"error": "请输入有效的 steamid（纯数字）。"}, HTTPStatus.BAD_REQUEST)
            return
        args = detection_args(
            self.app_args,
            start_time=str(body.get("start_time")).strip() if body.get("start_time") else None,
            end_time=str(body.get("end_time")).strip() if body.get("end_time") else None,
        )
        connection = connect(args)
        try:
            self.send_json(jsonable(run_player_track(connection, args, steamid)))
        finally:
            connection.close()

    def handle_manual_records(self) -> None:
        self.send_json(
            {
                "records": read_manual_records(Path(self.app_args.manual_records)),
                "path": os.path.abspath(self.app_args.manual_records),
            }
        )

    def handle_save_manual_record(self) -> None:
        body = self.read_json_body()
        record = {field: str(body.get(field) or "") for field in MANUAL_RECORD_FIELDS}
        if not record["league_id"] or not record["team_id"]:
            self.send_json({"error": "至少需要 league_id 和 team_id。"}, HTTPStatus.BAD_REQUEST)
            return

        path = Path(self.app_args.manual_records)
        records = read_manual_records(path)
        records.append(record)
        write_manual_records(path, records)
        self.send_json({"records": records, "path": os.path.abspath(path)})

    def handle_delete_manual_record(self) -> None:
        body = self.read_json_body()
        try:
            index = int(body.get("index"))
        except (TypeError, ValueError):
            self.send_json({"error": "删除记录需要有效 index。"}, HTTPStatus.BAD_REQUEST)
            return

        path = Path(self.app_args.manual_records)
        records = read_manual_records(path)
        if index < 0 or index >= len(records):
            self.send_json({"error": "index 超出范围。"}, HTTPStatus.BAD_REQUEST)
            return

        del records[index]
        write_manual_records(path, records)
        self.send_json({"records": records, "path": os.path.abspath(path)})

    def handle_save_all_manual_records(self) -> None:
        body = self.read_json_body()
        incoming_records = body.get("records")
        if not isinstance(incoming_records, list):
            self.send_json({"error": "需要 records 数组。"}, HTTPStatus.BAD_REQUEST)
            return

        records = [
            {field: str(record.get(field) or "") for field in MANUAL_RECORD_FIELDS}
            for record in incoming_records
            if isinstance(record, dict)
        ]
        path = Path(self.app_args.manual_records)
        write_manual_records(path, records)
        self.send_json({"records": records, "path": os.path.abspath(path)})

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    args = server_args()
    RosterHandler.app_args = args
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), RosterHandler)
    url = f"http://{args.listen_host}:{args.listen_port}/"
    print(f"可视化网页已启动：{url}")
    print(f"请确认已设置环境变量 {args.password_env}。按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
