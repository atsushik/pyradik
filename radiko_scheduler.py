"""保守処理：番組表更新 → 予約の時刻同期 → ルールのリコンサイル。

サーバ内のバックグラウンドループから run_maintenance() が呼ばれる（同期関数
なので呼び出し側で to_thread する）。`at` ジョブの組み替えのみ担当し、録音の
発火自体は atd に任せる（再起動耐性のため）。
"""

from datetime import datetime, timedelta

import radiko_guide
import radiko_recording
import radiko_state


def sync_reservations():
    """prog_id 付き予約を番組表と突き合わせ、消滅は削除・時刻変更は at を再作成。"""
    changed = 0
    for r in radiko_state.list_reservations(status="scheduled"):
        if not r["prog_id"]:
            continue  # 固定予約（prog_id 無）は触らない
        p = radiko_recording.lookup_program(r["prog_id"])
        if p is None:
            # prog_id が番組表から消えた → 自動削除
            radiko_recording.cancel_at_job(r["at_job_id"])
            radiko_state.delete_reservation(r["id"])
            changed += 1
            continue
        new_start = datetime.strptime(f"{p['date']}{p['ftime']}", "%Y%m%d%H%M")
        new_dur = int(p["duration"])
        old_start = datetime.fromisoformat(r["start_at"])
        if new_start == old_start and new_dur == r["duration_min"]:
            continue
        # 開始/長さが変わった → at は変更不可なので取消＋再作成
        radiko_recording.cancel_at_job(r["at_job_id"])
        fire = new_start + timedelta(seconds=r["start_offset_sec"])
        at_id = radiko_recording.create_at_job(r["id"], fire)
        radiko_state.update_reservation(
            r["id"], start_at=new_start.isoformat(), duration_min=new_dur,
            at_job_id=at_id, title=p["title"],
        )
        changed += 1
    return changed


def reconcile_rules():
    """有効ルールごとに、今後の一致番組を予約として ensure し、不一致の未来予約を削除する。

    予約の同一性は (rule_id, prog_id) で判定。新しい回は prog_id が増えるので追加、
    時刻変更は sync_reservations 側が prog_id アンカーで追従する。
    """
    now = datetime.now()
    created = pruned = 0
    for rule in radiko_state.list_rules(enabled_only=True):
        matches = radiko_recording.find_programs(
            rule["query"], match_fields=rule["match_fields"], station_id=rule["station_id"],
            weekday=rule["weekday"], time_from=rule["time_from"], time_to=rule["time_to"],
            after=now,  # 未来の回のみ予約対象
        )
        match_progids = {m["prog_id"] for m in matches}

        for m in matches:
            if radiko_state.find_reservation(rule["id"], m["prog_id"]):
                continue  # 既にこの回の予約あり
            t = radiko_recording.resolve_target({
                "prog_id": m["prog_id"], "with_art": bool(rule["with_art"]),
                "start_offset_sec": rule["start_offset_sec"],
                "end_offset_sec": rule["end_offset_sec"],
            })
            try:
                radiko_recording.schedule_reservation(t, rule_id=rule["id"])
                created += 1
            except Exception:
                pass

        # このルール由来の未来予約で、もう一致しないものを削除
        for r in radiko_state.list_reservations(status="scheduled", rule_id=rule["id"]):
            if datetime.fromisoformat(r["start_at"]) > now and r["prog_id"] not in match_progids:
                radiko_recording.cancel_at_job(r["at_job_id"])
                radiko_state.delete_reservation(r["id"])
                pruned += 1

    return {"created": created, "pruned": pruned}


def run_maintenance(refresh=True):
    """番組表更新 → 予約同期 → ルールリコンサイルを順に実行する。"""
    result = {"guide": None, "synced": 0, "rules": 0}
    if refresh:
        try:
            result["guide"] = radiko_guide.refresh_guide()
        except Exception as e:
            result["guide_error"] = str(e)
    result["synced"] = sync_reservations()
    result["rules"] = reconcile_rules()
    return result
