"""録音ジョブの実行本体。

即時録音はサーバが `python radiko_jobrunner.py <job_id>` を detached 起動し、
予約発火は at が `python radiko_jobrunner.py --reservation <res_id>` を実行する。
いずれも jobs 行の状態（recording→completed/failed）を更新するため、サーバと
疎結合で、再起動を跨いでも状態が正しく残る。
"""

import argparse
import math
import sys
from datetime import datetime, timedelta

import radiko_config
import radiko_recorder
import radiko_recording
import radiko_state


def _record(job) -> bool:
    start = datetime.fromisoformat(job["start_at"])
    client = radiko_recorder.RadikoClient()
    try:
        if job["method"] == "timefree":
            ft = start.strftime("%Y%m%d%H%M%S")
            return client.record_timefree(job["station_id"], ft, job["duration_min"], job["output"])
        # live: 番組終了までの残り時間を実時間でキャプチャ（即時・予約発火の両方を1式で扱う）
        end = start + timedelta(minutes=job["duration_min"])
        remaining_sec = (end - datetime.now()).total_seconds()
        minutes = max(1, math.ceil(remaining_sec / 60))
        return client.record_live(job["station_id"], minutes, job["output"])
    finally:
        client.logout()


def _embed_art(job):
    start = datetime.fromisoformat(job["start_at"])
    src = radiko_recording.lookup_art_source(
        job["station_id"], start.strftime("%Y%m%d"), start.strftime("%H%M")
    )
    if not src:
        return
    url, _title, image_url = src
    if not (url or image_url):
        return
    import radiko_cli  # fetch_art / embed_cover_art は DB 非依存
    art = radiko_cli.fetch_art(image_url, url)
    if art:
        image_bytes, ext = art
        radiko_cli.embed_cover_art(job["output"], image_bytes, ext)


def run_job(job_id) -> int:
    job = radiko_state.get_job(job_id)
    if not job:
        print(f"job {job_id} が見つかりません", file=sys.stderr)
        return 1
    radiko_state.set_job_status(job_id, "recording")
    try:
        ok = _record(job)
    except Exception as e:
        radiko_state.set_job_status(job_id, "failed", str(e))
        return 1
    if not ok:
        radiko_state.set_job_status(job_id, "failed", "recorder returned non-zero")
        return 1
    if job["with_art"]:
        try:
            _embed_art(job)
        except Exception:
            pass  # アート埋め込み失敗は録音成功を覆さない
    radiko_state.set_job_status(job_id, "completed")
    return 0


def run_reservation(res_id) -> int:
    res = radiko_state.get_reservation(res_id)
    if not res:
        print(f"reservation {res_id} が見つかりません", file=sys.stderr)
        return 1
    # end_offset を duration に織り込んで live ジョブを materialize（start_offset は at 側で適用済み）
    duration = res["duration_min"] + math.ceil(res["end_offset_sec"] / 60)
    job_id = radiko_state.create_job(
        res["station_id"], res["start_at"], duration, "live", res["output"],
        prog_id=res["prog_id"], title=res["title"], with_art=bool(res["with_art"]),
        reservation_id=res_id,
    )
    radiko_state.update_reservation(res_id, status="fired")
    return run_job(job_id)


def main(argv=None):
    p = argparse.ArgumentParser(description="radiko 録音ジョブ実行")
    p.add_argument("job_id", nargs="?", help="実行する job の id")
    p.add_argument("--reservation", type=int, help="この予約を materialize して実行")
    args = p.parse_args(argv)
    if args.reservation is not None:
        return run_reservation(args.reservation)
    if args.job_id:
        return run_job(args.job_id)
    p.error("job_id か --reservation を指定してください")


if __name__ == "__main__":
    sys.exit(main())
