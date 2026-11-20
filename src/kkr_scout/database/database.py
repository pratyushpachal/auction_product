"""Portable SQLAlchemy Core schema and database connection."""
import os
from pathlib import Path

from sqlalchemy import Column, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, event, text

from ..common import PROJECT_ROOT

metadata = MetaData()
TABLES = {}


def table(name, fields, constraints=()):
    columns = []
    for field in fields:
        fname, kind, primary, nullable, *fk = field
        columns.append(Column(fname, kind, *(ForeignKey(fk[0]) for _ in [0] if fk), primary_key=primary, nullable=nullable))
    TABLES[name] = Table(name, metadata, *columns, *constraints)
    return TABLES[name]


S, T, I, F = String(255), Text, Integer, Float
table("pipeline_meta", [("key", S, True, False), ("value", T, False, False)])
table("matches", [
    ("match_id", S, True, False), ("source", S, False, False), ("source_match_id", S, False, False),
    ("source_file", T, False, False), ("source_sha256", String(64), False, False), ("data_version", S, False, False),
    ("competition", S, False, False), ("season", S, False, False), ("date", S, False, False), ("dates_json", T, False, False),
    ("gender", S, False, False), ("match_type", S, False, False), ("venue", S, False, True), ("venue_id", S, False, True), ("city", S, False, True),
    ("team1", S, False, False), ("team2", S, False, False), ("team1_canonical", S, False, False), ("team2_canonical", S, False, False),
    ("balls_per_over", I, False, False), ("scheduled_overs", F, False, True), ("toss_winner", S, False, True), ("toss_decision", S, False, True),
    ("winner", S, False, True), ("result", S, False, True), ("outcome_method", S, False, True), ("outcome_json", T, False, False), ("missing_json", T, False, False), ("info_json", T, False, False),
], (UniqueConstraint("source_file"), UniqueConstraint("source_sha256"), UniqueConstraint("source", "source_match_id")))
table("players", [("player_id", S, True, False), ("canonical_name", S, False, False), ("name_as_of", S, False, False), ("identity_status", S, False, False), ("state", S, False, True), ("batting_hand", S, False, True), ("bowling_type", S, False, True), ("dob", S, False, True), ("nationality", S, False, True)])
table("player_aliases", [("source_player_id", S, True, False), ("alias_name", S, True, False), ("player_id", S, False, False, "players.player_id"), ("first_seen", S, False, False), ("last_seen", S, False, False)])
table("match_players", [("match_id", S, True, False, "matches.match_id"), ("player_id", S, True, False, "players.player_id"), ("team", S, True, False), ("team_canonical", S, False, False), ("source_name", S, False, False)])
table("innings", [("match_id", S, True, False, "matches.match_id"), ("innings_no", I, True, False), ("batting_team", S, False, False), ("batting_team_canonical", S, False, False), ("bowling_team", S, False, False), ("is_super_over", I, False, False), ("total_runs", I, False, False), ("legal_balls", I, False, False), ("delivery_count", I, False, False), ("wickets", I, False, False), ("overs_text", S, False, False), ("penalty_pre", I, False, False), ("penalty_post", I, False, False), ("target_runs", I, False, True), ("target_overs", F, False, True), ("metadata_json", T, False, False)])
table("deliveries", [("match_id", S, True, False), ("innings_no", I, True, False), ("over", I, True, False), ("ball", I, True, False), ("delivery_no", I, False, False), ("actual_delivery", S, False, True), ("batter_id", S, False, False, "players.player_id"), ("non_striker_id", S, False, False, "players.player_id"), ("bowler_id", S, False, False, "players.player_id"), ("runs_bat", I, False, False), ("extras", I, False, False), ("total_runs", I, False, False), ("wides", I, False, False), ("noballs", I, False, False), ("byes", I, False, False), ("legbyes", I, False, False), ("penalty", I, False, False), ("is_legal", I, False, False), ("batter_ball", I, False, False), ("bowler_runs", I, False, False), ("is_boundary", I, False, False), ("over_band", S, False, False), ("extras_json", T, False, False)], (UniqueConstraint("match_id", "innings_no", "delivery_no"), ForeignKeyConstraint(["match_id", "innings_no"], ["innings.match_id", "innings.innings_no"])))
table("wickets", [("match_id", S, True, False), ("innings_no", I, True, False), ("over", I, True, False), ("ball", I, True, False), ("wicket_no", I, True, False), ("dismissed_player_id", S, False, False, "players.player_id"), ("wicket_type", S, False, False), ("counts_as_dismissal", I, False, False), ("credited_to_bowler", I, False, False), ("fielders_json", T, False, False)], (ForeignKeyConstraint(["match_id", "innings_no", "over", "ball"], ["deliveries.match_id", "deliveries.innings_no", "deliveries.over", "deliveries.ball"]),))
table("batting_scorecards", [("match_id", S, True, False, "matches.match_id"), ("innings_no", I, True, False), ("player_id", S, True, False, "players.player_id"), ("runs", I, False, True), ("balls", I, False, True), ("fours", I, False, True), ("sixes", I, False, True), ("dismissal", S, False, True), ("source_file", T, False, False)])
table("bowling_scorecards", [("match_id", S, True, False, "matches.match_id"), ("innings_no", I, True, False), ("player_id", S, True, False, "players.player_id"), ("legal_balls", I, False, True), ("runs_conceded", I, False, True), ("wickets", I, False, True), ("source_file", T, False, False)])
Index("idx_matches_competition_season", TABLES["matches"].c.competition, TABLES["matches"].c.season, TABLES["matches"].c.date)
Index("idx_deliveries_batter", TABLES["deliveries"].c.batter_id)
Index("idx_deliveries_bowler", TABLES["deliveries"].c.bowler_id)
Index("idx_wickets_player", TABLES["wickets"].c.dismissed_player_id)

_engines = {}


def get_engine(root: Path = PROJECT_ROOT):
    root = Path(root).resolve()
    default = root / "data/processed/kkr_scout.sqlite"
    url = os.environ.get("DATABASE_URL") or f"sqlite:///{default.as_posix()}"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url not in _engines:
        if url.startswith("sqlite"):
            default.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True)
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine, "connect")
            def sqlite_settings(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        _engines[url] = engine
    return _engines[url]


def create_schema(engine=None):
    engine = engine or get_engine()
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS regulation_deliveries"))
        conn.execute(text("""CREATE VIEW regulation_deliveries AS
            SELECT d.*, m.competition, m.season, m.date, m.balls_per_over,
                   i.batting_team_canonical AS batting_team, i.bowling_team
            FROM deliveries d JOIN matches m USING (match_id)
            JOIN innings i USING (match_id, innings_no)
            WHERE i.is_super_over=0 AND m.match_type='T20'"""))
    return engine
