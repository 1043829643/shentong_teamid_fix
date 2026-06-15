#!/usr/bin/env python3
"""Detect same 5-player Dota2 rosters using different team IDs in one league.

The script connects to StarRocks through the MySQL protocol in read-only style:
it only reads information_schema and selected source tables, then writes a local
CSV report.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys", "_statistics_"}

LEAGUE_NAMES = ("league_id", "leagueid", "league")
TEAM_NAMES = ("team_id", "teamid", "team")
MATCH_NAMES = ("match_id", "matchid", "game_id", "gameid", "replay_id", "replayid")
PLAYER_NAMES = ("steamid", "steam_id", "account_id", "accountid", "player_id", "playerid")
TIME_NAMES = (
    "start_time",
    "starttime",
    "match_time",
    "matchtime",
    "game_time",
    "gametime",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class FieldMapping:
    database: str
    table: str
    league_col: str
    team_col: str
    match_col: str
    player_col: str
    time_col: str | None = None
    score: int = 0

    @property
    def qualified_table(self) -> str:
        return f"{quote_ident(self.database)}.{quote_ident(self.table)}"


def import_pymysql() -> Any:
    try:
        import pymysql  # type: ignore
    except ImportError:
        print(
            "缺少依赖 pymysql。请先运行：python -m pip install pymysql",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return pymysql


def quote_ident(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def normalize_name(name: str) -> str:
    return name.lower().replace("-", "_").replace(" ", "_")


def parse_time_filter(value: str | None, *, end_of_day: bool = False) -> int | None:
    if value is None or not str(value).strip():
        return None

    text = str(value).strip()
    if text.isdigit():
        return int(text)

    normalized = text.replace("T", " ")
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(normalized, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return int(parsed.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue

    raise ValueError(f"无法解析时间：{value}。请使用 YYYY-MM-DD、YYYY-MM-DD HH:MM:SS 或 Unix 秒。")


def pick_column(columns: Iterable[str], preferred_names: tuple[str, ...]) -> tuple[str | None, int]:
    normalized = {normalize_name(column): column for column in columns}

    for index, preferred in enumerate(preferred_names):
        if preferred in normalized:
            return normalized[preferred], 100 - index

    for column in columns:
        normalized_column = normalize_name(column)
        for index, preferred in enumerate(preferred_names):
            if normalized_column.endswith(preferred) or preferred in normalized_column:
                return column, 50 - index

    return None, 0


def connect(args: argparse.Namespace) -> Any:
    pymysql = import_pymysql()
    password = args.password or os.getenv(args.password_env)
    if password is None:
        if not sys.stdin.isatty():
            print(
                f"未提供密码。请设置环境变量 {args.password_env}，或使用 --password 传入。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        password = getpass.getpass(f"StarRocks password for {args.user}: ")

    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=args.read_timeout,
        write_timeout=args.write_timeout,
        connect_timeout=args.connect_timeout,
    )


def discover_candidates(connection: Any, only_database: str | None) -> list[FieldMapping]:
    params: list[Any] = []
    database_filter = ""
    if only_database:
        database_filter = "AND table_schema = %s"
        params.append(only_database)

    sql = f"""
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema NOT IN ({",".join(["%s"] * len(SYSTEM_SCHEMAS))})
          {database_filter}
        ORDER BY table_schema, table_name, ordinal_position
    """
    params = list(SYSTEM_SCHEMAS) + params

    tables: dict[tuple[str, str], list[str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            key = (row["table_schema"], row["table_name"])
            tables.setdefault(key, []).append(row["column_name"])

    candidates: list[FieldMapping] = []
    for (database, table), columns in tables.items():
        league_col, league_score = pick_column(columns, LEAGUE_NAMES)
        team_col, team_score = pick_column(columns, TEAM_NAMES)
        match_col, match_score = pick_column(columns, MATCH_NAMES)
        player_col, player_score = pick_column(columns, PLAYER_NAMES)
        time_col, time_score = pick_column(columns, TIME_NAMES)

        if league_col and team_col and match_col and player_col:
            candidates.append(
                FieldMapping(
                    database=database,
                    table=table,
                    league_col=league_col,
                    team_col=team_col,
                    match_col=match_col,
                    player_col=player_col,
                    time_col=time_col,
                    score=league_score + team_score + match_score + player_score + time_score,
                )
            )

    if only_database in (None, "dota2_analysis"):
        candidates.insert(
            0,
            FieldMapping(
                database="dota2_analysis",
                table="players__match_info",
                league_col="match_info.league_id",
                team_col="match_info.radiant_team_id/dire_team_id",
                match_col="players.match_id",
                player_col="players.steamid",
                time_col="match_info.end_time",
                score=1000,
            ),
        )

    return sorted(candidates, key=lambda item: item.score, reverse=True)


def resolve_mapping(args: argparse.Namespace, connection: Any) -> FieldMapping:
    if args.table:
        if not args.database:
            print("使用 --table 时也需要提供 --database。", file=sys.stderr)
            raise SystemExit(2)
        missing = [
            name
            for name, value in (
                ("--league-col", args.league_col),
                ("--team-col", args.team_col),
                ("--match-col", args.match_col),
                ("--player-col", args.player_col),
            )
            if not value
        ]
        if missing:
            print("手动指定表时缺少字段参数：" + ", ".join(missing), file=sys.stderr)
            raise SystemExit(2)
        return FieldMapping(
            database=args.database,
            table=args.table,
            league_col=args.league_col,
            team_col=args.team_col,
            match_col=args.match_col,
            player_col=args.player_col,
            time_col=args.time_col,
            score=999,
        )

    candidates = discover_candidates(connection, args.database)
    if not candidates:
        print("没有自动发现同时包含 league/team/match/player 字段的候选表。", file=sys.stderr)
        print("请用 --database --table --league-col --team-col --match-col --player-col 手动指定。", file=sys.stderr)
        raise SystemExit(1)

    print("自动发现候选表：")
    for index, candidate in enumerate(candidates[: args.max_candidates], start=1):
        print(
            f"{index}. {candidate.database}.{candidate.table} "
            f"(league={candidate.league_col}, team={candidate.team_col}, "
            f"match={candidate.match_col}, player={candidate.player_col}, "
            f"time={candidate.time_col or '-'}, score={candidate.score})"
        )

    if args.list_candidates:
        raise SystemExit(0)

    selected = candidates[0]
    if len(candidates) > 1 and not args.yes:
        print()
        print("发现多个候选表。默认不会猜测执行；请加 --yes 使用第 1 个，或用 --table 手动指定。")
        raise SystemExit(1)

    print(f"使用候选表：{selected.database}.{selected.table}")
    return selected


def build_detection_sql(mapping: FieldMapping, args: argparse.Namespace) -> tuple[str, list[Any]]:
    if mapping.database == "dota2_analysis" and mapping.table == "players__match_info":
        return build_dota2_analysis_detection_sql(args)

    league_expr = f"CAST({quote_ident(mapping.league_col)} AS VARCHAR)"
    team_expr = f"CAST({quote_ident(mapping.team_col)} AS VARCHAR)"
    match_expr = f"CAST({quote_ident(mapping.match_col)} AS VARCHAR)"
    player_expr = f"CAST({quote_ident(mapping.player_col)} AS VARCHAR)"
    time_select = ""
    time_output = ""
    if mapping.time_col:
        time_column = quote_ident(mapping.time_col)
        time_select = f", MIN({time_column}) AS first_seen, MAX({time_column}) AS last_seen"
        time_output = ", MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen"

    filters = [
        f"{quote_ident(mapping.league_col)} IS NOT NULL",
        f"{quote_ident(mapping.team_col)} IS NOT NULL",
        f"{quote_ident(mapping.match_col)} IS NOT NULL",
        f"{quote_ident(mapping.player_col)} IS NOT NULL",
    ]
    params: list[Any] = []
    if args.league_id is not None:
        filters.append(f"{quote_ident(mapping.league_col)} = %s")
        params.append(args.league_id)
    where_clause = " AND ".join(filters)
    limit_clause = " LIMIT %s" if args.limit else ""
    if args.limit:
        params.append(args.limit)

    # GROUP_CONCAT with ORDER BY is supported by MySQL-compatible StarRocks
    # versions and gives a stable roster_key for the same five players.
    sql = f"""
        WITH team_rosters AS (
            SELECT
                {league_expr} AS league_id,
                {match_expr} AS match_id,
                {team_expr} AS team_id,
                COUNT(DISTINCT {player_expr}) AS player_count,
                GROUP_CONCAT(DISTINCT {player_expr} ORDER BY {player_expr} SEPARATOR ',') AS roster_key
                {time_select}
            FROM {mapping.qualified_table}
            WHERE {where_clause}
            GROUP BY {league_expr}, {match_expr}, {team_expr}
            HAVING COUNT(DISTINCT {player_expr}) = 5
        ),
        anomalies AS (
            SELECT
                league_id,
                roster_key,
                COUNT(DISTINCT team_id) AS team_id_count,
                COUNT(*) AS roster_occurrences,
                GROUP_CONCAT(DISTINCT team_id ORDER BY team_id SEPARATOR ',') AS team_ids,
                GROUP_CONCAT(DISTINCT match_id ORDER BY match_id SEPARATOR ',') AS match_ids
                {time_output}
            FROM team_rosters
            GROUP BY league_id, roster_key
            HAVING COUNT(DISTINCT team_id) > 1
        )
        SELECT *
        FROM anomalies
        ORDER BY league_id, team_id_count DESC, roster_occurrences DESC, roster_key
        {limit_clause}
    """
    return sql, params


def build_dota2_analysis_detection_sql(args: argparse.Namespace) -> tuple[str, list[Any]]:
    filters = ["mi.league_id IS NOT NULL"]
    match_filters = [
        "match_id IS NOT NULL",
        "CAST(match_id AS VARCHAR) <> '0'",
        "league_id IS NOT NULL",
    ]
    params: list[Any] = []
    if args.league_id is not None:
        filters.append("mi.league_id = %s")
        params.append(args.league_id)

    start_time = parse_time_filter(getattr(args, "start_time", None))
    end_time = parse_time_filter(getattr(args, "end_time", None), end_of_day=True)
    if start_time is not None:
        match_filters.append("end_time >= %s")
        params.append(start_time)
    if end_time is not None:
        match_filters.append("end_time <= %s")
        params.append(end_time)

    limit_clause = " LIMIT %s" if args.limit else ""
    if args.limit:
        params.append(args.limit)

    detection_mode = getattr(args, "detection_mode", "same_league")
    if detection_mode == "cross_league":
        anomaly_sql = """
        anomalies AS (
            SELECT
                GROUP_CONCAT(DISTINCT tr.league_id ORDER BY tr.league_id SEPARATOR ',') AS league_id,
                GROUP_CONCAT(
                    DISTINCT CONCAT(tr.league_id, ' | ', COALESCE(ln.league_name, ''))
                    ORDER BY tr.league_id
                    SEPARATOR ';;'
                ) AS league_name,
                tr.roster_key,
                MAX(tr.roster_players) AS roster_players,
                COUNT(DISTINCT tr.league_id) AS league_count,
                COUNT(DISTINCT team_id) AS team_id_count,
                COUNT(*) AS roster_occurrences,
                GROUP_CONCAT(DISTINCT team_id ORDER BY team_id SEPARATOR ',') AS team_ids,
                GROUP_CONCAT(
                    DISTINCT CONCAT(team_id, ' | ', COALESCE(team_name, ''))
                    ORDER BY team_id
                    SEPARATOR ';;'
                ) AS team_id_names,
                GROUP_CONCAT(DISTINCT match_id ORDER BY match_id SEPARATOR ',') AS match_ids,
                MIN(first_seen) AS first_seen,
                MAX(last_seen) AS last_seen
            FROM team_rosters tr
            LEFT JOIN league_names ln
              ON tr.league_id = ln.league_id
            GROUP BY tr.roster_key
            HAVING COUNT(DISTINCT tr.league_id) > 1
               AND COUNT(DISTINCT team_id) > 1
        )
        """
    else:
        anomaly_sql = """
        anomalies AS (
            SELECT
                tr.league_id,
                MAX(ln.league_name) AS league_name,
                tr.roster_key,
                MAX(tr.roster_players) AS roster_players,
                1 AS league_count,
                COUNT(DISTINCT team_id) AS team_id_count,
                COUNT(*) AS roster_occurrences,
                GROUP_CONCAT(DISTINCT team_id ORDER BY team_id SEPARATOR ',') AS team_ids,
                GROUP_CONCAT(
                    DISTINCT CONCAT(team_id, ' | ', COALESCE(team_name, ''))
                    ORDER BY team_id
                    SEPARATOR ';;'
                ) AS team_id_names,
                GROUP_CONCAT(DISTINCT match_id ORDER BY match_id SEPARATOR ',') AS match_ids,
                MIN(first_seen) AS first_seen,
                MAX(last_seen) AS last_seen
            FROM team_rosters tr
            LEFT JOIN league_names ln
              ON tr.league_id = ln.league_id
            GROUP BY tr.league_id, tr.roster_key
            HAVING COUNT(DISTINCT team_id) > 1
        )
        """

    sql = f"""
        WITH league_names AS (
            SELECT CAST(league_id AS VARCHAR) AS league_id, MAX(league_name) AS league_name
            FROM (
                SELECT league_id, league_name FROM `dota2_analysis`.`pro_match_list`
                UNION ALL
                SELECT league_id, league_name FROM `dota2_analysis`.`pro_match_list_2`
                UNION ALL
                SELECT league_id, league_name FROM `dota2_analysis`.`match_info_upload`
            ) names
            WHERE league_id IS NOT NULL
              AND league_name IS NOT NULL
              AND league_name <> ''
            GROUP BY CAST(league_id AS VARCHAR)
        ),
        pro_players_dedup AS (
            SELECT CAST(steamid AS VARCHAR) AS steamid, MAX(name) AS player_name
            FROM `dota2_analysis`.`pro_players`
            WHERE steamid IS NOT NULL
            GROUP BY CAST(steamid AS VARCHAR)
        ),
        match_info_dedup AS (
            SELECT
                CAST(match_id AS VARCHAR) AS match_id,
                CAST(MAX(league_id) AS VARCHAR) AS league_id,
                CAST(MAX(radiant_team_id) AS VARCHAR) AS radiant_team_id,
                CAST(MAX(dire_team_id) AS VARCHAR) AS dire_team_id,
                MAX(radiant_team_tag) AS radiant_team_name,
                MAX(dire_team_tag) AS dire_team_name,
                MIN(end_time) AS first_seen,
                MAX(end_time) AS last_seen
            FROM `dota2_analysis`.`match_info`
            WHERE {" AND ".join(match_filters)}
            GROUP BY CAST(match_id AS VARCHAR)
        ),
        player_rows AS (
            SELECT
                mi.league_id,
                mi.match_id,
                CASE
                    WHEN p.team = 2 THEN mi.radiant_team_id
                    WHEN p.team = 3 THEN mi.dire_team_id
                    WHEN p.slot BETWEEN 0 AND 4 THEN mi.radiant_team_id
                    WHEN p.slot BETWEEN 5 AND 9 THEN mi.dire_team_id
                END AS team_id,
                CASE
                    WHEN p.team = 2 THEN mi.radiant_team_name
                    WHEN p.team = 3 THEN mi.dire_team_name
                    WHEN p.slot BETWEEN 0 AND 4 THEN mi.radiant_team_name
                    WHEN p.slot BETWEEN 5 AND 9 THEN mi.dire_team_name
                END AS team_name,
                CAST(p.steamid AS VARCHAR) AS player_id,
                pp.player_name,
                mi.first_seen,
                mi.last_seen
            FROM `dota2_analysis`.`players` p
            JOIN match_info_dedup mi
              ON CAST(p.match_id AS VARCHAR) = mi.match_id
            LEFT JOIN pro_players_dedup pp
              ON CAST(p.steamid AS VARCHAR) = pp.steamid
            WHERE p.steamid IS NOT NULL
              AND CAST(p.match_id AS VARCHAR) <> '0'
              AND {" AND ".join(filters)}
        ),
        team_rosters AS (
            SELECT
                league_id,
                match_id,
                CAST(team_id AS VARCHAR) AS team_id,
                MAX(team_name) AS team_name,
                COUNT(DISTINCT player_id) AS player_count,
                GROUP_CONCAT(DISTINCT player_id ORDER BY player_id SEPARATOR ',') AS roster_key,
                GROUP_CONCAT(
                    DISTINCT CONCAT(player_id, ' | ', COALESCE(player_name, ''))
                    ORDER BY player_id
                    SEPARATOR ';;'
                ) AS roster_players,
                MIN(first_seen) AS first_seen,
                MAX(last_seen) AS last_seen
            FROM player_rows
            WHERE team_id IS NOT NULL
              AND CAST(team_id AS VARCHAR) <> '0'
            GROUP BY league_id, match_id, CAST(team_id AS VARCHAR)
            HAVING COUNT(DISTINCT player_id) = 5
        ),
        {anomaly_sql}
        SELECT *
        FROM anomalies
        ORDER BY league_id, team_id_count DESC, roster_occurrences DESC, roster_key
        {limit_clause}
    """
    return sql, params


def run_detection(connection: Any, mapping: FieldMapping, args: argparse.Namespace) -> list[dict[str, Any]]:
    sql, params = build_detection_sql(mapping, args)
    if args.print_sql:
        print("将执行 SQL：")
        print(sql)
        print("参数：", params)

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "league_id",
        "league_name",
        "roster_key",
        "roster_players",
        "league_count",
        "team_id_count",
        "roster_occurrences",
        "team_ids",
        "team_id_names",
        "match_ids",
        "first_seen",
        "last_seen",
    ]
    available_fields = [field for field in fieldnames if rows and field in rows[0]]
    if not available_fields:
        available_fields = fieldnames[:6]

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=available_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: list[dict[str, Any]], output_path: str) -> None:
    if not rows:
        print("未发现同一联赛内同 5 人阵容对应多个 team_id 的情况。")
        print(f"已生成空报告：{output_path}")
        return

    league_count = len({row["league_id"] for row in rows})
    print(f"发现 {len(rows)} 条异常阵容组合，涉及 {league_count} 个联赛。")
    print(f"报告已导出：{output_path}")
    print()
    print("前 10 条：")
    for row in rows[:10]:
        print(
            f"- league_id={row.get('league_id')} "
            f"league_name={row.get('league_name') or '-'} "
            f"team_ids={row.get('team_id_names') or row.get('team_ids')} "
            f"matches={row.get('match_ids')} "
            f"roster={row.get('roster_players') or row.get('roster_key')}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find same five Dota2 player steamids using different team IDs in the same league."
    )
    parser.add_argument("--host", default="47.86.96.51", help="StarRocks FE MySQL host")
    parser.add_argument("--port", type=int, default=9030, help="StarRocks FE MySQL port")
    parser.add_argument("--user", default="dota2_reader", help="StarRocks username")
    parser.add_argument("--password", help="StarRocks password; prefer STARROCKS_PASSWORD env var")
    parser.add_argument("--password-env", default="STARROCKS_PASSWORD", help="Password environment variable name")
    parser.add_argument("--database", default="dota2_analysis", help="Database/schema name. Optional for auto discovery.")
    parser.add_argument("--table", help="Source table name. Requires manual column arguments.")
    parser.add_argument("--league-col", help="League ID column name")
    parser.add_argument("--team-col", help="Team ID column name")
    parser.add_argument("--match-col", help="Match/game ID column name")
    parser.add_argument("--player-col", help="Player steam/account ID column name")
    parser.add_argument("--time-col", help="Optional time column for first_seen/last_seen")
    parser.add_argument("--league-id", help="Limit detection to one league ID")
    parser.add_argument("--start-time", help="Only include matches with end_time >= this value")
    parser.add_argument("--end-time", help="Only include matches with end_time <= this value")
    parser.add_argument(
        "--detection-mode",
        choices=("same_league", "cross_league"),
        default="same_league",
        help="same_league: same roster uses multiple team IDs in one league; cross_league: same roster uses different teams across leagues",
    )
    parser.add_argument("--output", default="same_roster_different_team_ids.csv", help="CSV output path")
    parser.add_argument("--limit", type=int, help="Limit report rows")
    parser.add_argument("--max-candidates", type=int, default=20, help="Maximum auto-discovery candidates to print")
    parser.add_argument("--list-candidates", action="store_true", help="Only list candidate tables and exit")
    parser.add_argument("--yes", action="store_true", help="Use the top auto-discovery candidate when multiple exist")
    parser.add_argument("--print-sql", action="store_true", help="Print generated SQL before running")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--read-timeout", type=int, default=300)
    parser.add_argument("--write-timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args()
    connection = connect(args)
    try:
        mapping = resolve_mapping(args, connection)
        rows = run_detection(connection, mapping, args)
        write_csv(args.output, rows)
        print_summary(rows, args.output)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
