# Uploaded data inventory

Every numeric JSON upload was opened and its nested keys inspected before the loader was written. Raw files stay in their uploaded locations. Counts include super overs and interrupted innings.

| Competition | Seasons | Tier | Grain | Match files | Innings | Delivery rows | Legal balls | Dates |
|---|---|---|---|---:|---:|---:|---:|---|
| Indian Premier League | 2007/08, 2009, 2009/10, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020/21, 2021, 2022, 2023, 2024, 2025, 2026 | A | one delivery event | 441 | 901 | 103976 | 100220 | 2008-04-18 to 2026-05-24 |
| Syed Mushtaq Ali Trophy | 2015/16, 2017/18, 2018/19, 2019/20, 2020/21, 2021/22, 2022/23, 2023/24, 2024/25 | A | one delivery event | 56 | 112 | 13085 | 12660 | 2016-01-02 to 2024-12-15 |

Tier A means ball-by-ball. There are no Tier B scorecard uploads.

README.txt is the Cricsheet archive listing and provenance note. LICENSE.txt contains the Open Data Commons Attribution License. The pasted request and ROADMAP are specifications, not sporting data.

Source structure

- Match: meta.data_version, created and revision; info.dates, event.name, season, match_type, balls_per_over, players, registry.people, teams, venue, toss and outcome.
- Innings: team and overs; optional super_over, powerplays, target, absent_hurt and miscounted_overs fields remain in metadata.
- Delivery: batter, non_striker, bowler, runs.batter/extras/total; optional extras and wickets. Runs include event extras.
- Player grain: roster name linked to registry.people source ID. Officials also occur in registry.people; only roster and delivery participants enter players.
- 1013 innings start at over 0. Source overs are zero-indexed; delivery position is event order within an over, including illegal deliveries.
- 0 roster-name occurrences lack registry IDs. Fuzzy resolution remains available for later uploads, with a review bucket and no low-confidence merge.

Optional inputs

No uploaded squad, purse, slot limit, overseas limit, price table or player attributes were found. Do not infer these from historical match rosters. Historical rosters are match participants only.

The per-file byte hash, version, competition, season, grain and counts are in upload_manifest.csv. All observed nested field names are in inventory.json.
