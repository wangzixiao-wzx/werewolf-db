# -*- coding: utf-8 -*-
"""把 werewolf.db 导出成纯前端可用的 data.js（GitHub Pages 静态只读查询站的数据源）。

用法：
  python export_static.py                    # 默认读 ../werewolf-stats/werewolf.db
  python export_static.py --db <path>        # 指定数据库
  python export_static.py --anonymize        # 玩家昵称脱敏成「玩家01…」（公开更稳妥）
  python export_static.py --out <path>       # 指定输出文件

每次本地录入新对局后重跑一次，再 git push 即可更新线上数据。
只导出查询所需的表；merge_log / name_pair 等内部维护表不公开。
"""
import argparse
import datetime
import json
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "..", "werewolf-stats", "werewolf.db"))
DEFAULT_OUT = os.path.join(HERE, "data.js")


def build(db_path, anonymize=False):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    factions = {
        r["id"]: {"name": r["name"], "color": r["color"]}
        for r in con.execute("SELECT id, name, color FROM faction")
    }

    def fname(fid):
        return (factions.get(fid) or {}).get("name", "未知")

    roles = [
        {"id": r["id"], "name": r["name"], "faction_id": r["faction_id"],
         "faction": fname(r["faction_id"]), "note": r["note"] or ""}
        for r in con.execute("SELECT id, name, faction_id, note FROM role ORDER BY id")
    ]
    role_map = {r["id"]: r for r in roles}

    alias_map = {}
    for r in con.execute("SELECT player_id, alias FROM player_alias"):
        alias_map.setdefault(r["player_id"], []).append(r["alias"])
    players = [
        {"id": r["id"], "name": r["name"],
         "aliases": alias_map.get(r["id"], []), "note": r["note"] or ""}
        for r in con.execute("SELECT id, name, note FROM player ORDER BY id")
    ]
    player_map = {p["id"]: p for p in players}

    # 脱敏：主名与别名 -> 玩家NN；技能日志 / 备注里出现的名字一并替换
    name_map = {}
    if anonymize:
        labels = {p["id"]: "玩家%02d" % i for i, p in enumerate(players, 1)}
        for p in players:
            name_map[p["name"]] = labels[p["id"]]
            for a in p["aliases"]:
                name_map.setdefault(a, labels[p["id"]])
        # 历史合并旧名 / 名字对：这些名字可能残留在备注或技能日志里。
        # 单字与纯数字跳过（在中文文本和 JSON 数字里无法可靠区分，会误伤）。
        old = set()
        for r in con.execute("SELECT DISTINCT from_name FROM merge_log WHERE from_name <> ''"):
            old.add(r["from_name"] or "")
        for r in con.execute("SELECT a, b FROM name_pair"):
            old.add(r["a"] or ""); old.add(r["b"] or "")
        for n in old:
            n = n.strip()
            if len(n) >= 2 and not n.isdigit() and n not in name_map:
                name_map[n] = "玩家00"
        for p in players:
            p["name"] = labels[p["id"]]
            p["aliases"] = []
            p["note"] = ""

    def nm(s):
        s = s or ""
        if name_map:
            for k in sorted(name_map, key=len, reverse=True):
                s = s.replace(k, name_map[k])
        return s

    # board.note 里混有录入来源（源文件名 / 内部字段名），这些不属于版型规则，
    # 也不适合公开；保留前面的规则说明，剔除内部片段。
    INTERNAL_NOTE = re.compile(r"\.txt|记录约定|录入约定|board_role|game_player|"
                               r"导入补录|由源文档整理|批量导入|20\d\d-\d\d-\d\d")

    def clean_board_note(s):
        s = (s or "").strip()
        s = re.sub(r"(来源|批量导入)[:：][^；;。]*\.txt[；;，,\s]*", "", s)
        s = re.split(r"记录约定[:：]|录入约定[:：]|数据库约定[:：]", s)[0]
        s = s.strip(" ；;，,")
        return "" if INTERNAL_NOTE.search(s) else s

    # 个别历史规则值里混了数据库字段名（例如「final_faction_id 标为狼人阵营」），
    # 这是录入约定而非版型规则；公开快照改成中文描述，不改动主库原文。
    def clean_rule_value(s):
        return (s or "").replace("final_faction_id", "最终阵营")

    boards = []
    for b in con.execute("SELECT id, name, player_count, judge_count, note FROM board ORDER BY id"):
        comp = [
            {"name": r["name"], "faction": fname(r["faction_id"]), "cnt": r["cnt"]}
            for r in con.execute(
                """SELECT r.name, r.faction_id, br.cnt FROM board_role br
                   JOIN role r ON r.id = br.role_id WHERE br.board_id = ? ORDER BY br.id""",
                (b["id"],))
        ]
        rules = [
            # note 全是录入来源备注（由文档导入 / 导入补录 / 内部字段名），
            # 无规则信息且带内部痕迹，公开快照一律不导出，只保留空列占位。
            {"role": r["rn"] or "", "key": r["rule_key"] or "",
             "value": clean_rule_value(r["rule_value"]), "note": ""}
            for r in con.execute(
                """SELECT rr.rule_key, rr.rule_value, rr.note, r.name rn
                   FROM board_rule rr LEFT JOIN role r ON r.id = rr.role_id
                   WHERE rr.board_id = ? ORDER BY rr.id""", (b["id"],))
        ]
        boards.append({
            "id": b["id"], "name": b["name"], "player_count": b["player_count"],
            "judge_count": b["judge_count"], "note": clean_board_note(b["note"]),
            "comp": comp, "rules": rules,
        })
    board_map = {b["id"]: b["name"] for b in boards}

    games = []
    for g in con.execute(
            "SELECT id, played_at, board_id, player_count, winner_faction_id, win_type, note "
            "FROM game ORDER BY id"):
        seats = []
        for gp in con.execute(
                """SELECT gp.*, r.name role_name, r.faction_id role_fid
                   FROM game_player gp LEFT JOIN role r ON r.id = gp.role_id
                   WHERE gp.game_id = ? ORDER BY gp.seat""", (g["id"],)):
            eff = gp["final_faction_id"] if gp["final_faction_id"] is not None else gp["role_fid"]
            pname = (player_map.get(gp["player_id"]) or {}).get("name", "?")
            seats.append({
                "seat": gp["seat"], "player": nm(pname), "player_id": gp["player_id"],
                "role": gp["role_name"] or "?", "faction_id": gp["role_fid"],
                "eff_faction_id": eff, "faction": fname(eff),
                "survived": gp["survived"], "death_round": gp["death_round"] or 0,
                "death_cause": gp["death_cause"] or "",
                "sheriff": gp["is_sheriff"], "mvp": gp["is_mvp"],
                "note": nm(gp["note"] or ""),
            })
        skills = [
            {"round": s["round_no"], "actor": nm(s["actor_name"] or ""),
             "action": s["action"] or "", "target": nm(s["target_name"] or ""),
             "result": nm(s["result"] or "")}
            for s in con.execute(
                "SELECT round_no, actor_name, action, target_name, result "
                "FROM skill_log WHERE game_id = ? ORDER BY round_no, id", (g["id"],))
        ]
        games.append({
            "id": g["id"], "played_at": g["played_at"] or "",
            "board_id": g["board_id"], "board": board_map.get(g["board_id"], "?"),
            "player_count": g["player_count"], "winner": g["winner_faction_id"],
            "win_type": g["win_type"] or "", "note": nm(g["note"] or ""),
            "seats": seats, "skills": skills,
        })

    con.close()
    data = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "anonymized": anonymize,
        "counts": {
            "games": len(games), "players": len(players), "boards": len(boards),
            "roles": len(roles), "seats": sum(len(x["seats"]) for x in games),
            "skills": sum(len(x["skills"]) for x in games),
        },
        "factions": factions, "roles": roles, "players": players,
        "boards": boards, "games": games,
    }
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--anonymize", action="store_true")
    a = ap.parse_args()
    if not os.path.isfile(a.db):
        raise SystemExit("找不到数据库：%s" % a.db)
    data = build(a.db, a.anonymize)
    js = "window.WW_DATA = " + json.dumps(
        data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(js)
    c = data["counts"]
    print("已导出 %s  (%.1f KB)" % (a.out, len(js.encode("utf-8")) / 1024))
    print("  快照时间 %s | 脱敏=%s" % (data["generated"], data["anonymized"]))
    print("  对局 %d / 玩家 %d / 板子 %d / 身份 %d / 座位 %d / 技能日志 %d"
          % (c["games"], c["players"], c["boards"], c["roles"], c["seats"], c["skills"]))


if __name__ == "__main__":
    main()
