"""Web API のレスポンスモデル（OpenAPI スキーマ用）。

web_ui.py の各エンドポイントに response_model として割り当てる。返却している
dict のキーを過不足なく宣言する（欠けると 500、未宣言キーは脱落するため）。
"""

from pydantic import BaseModel


# ---- meta ---------------------------------------------------------------

class VersionOut(BaseModel):
    version: str


class HealthChecks(BaseModel):
    db: bool
    ffmpeg: bool
    at: bool
    guide_age_sec: "int | None" = None


class HealthOut(BaseModel):
    status: str
    version: str
    checks: HealthChecks


# ---- 番組・放送局 -------------------------------------------------------

class StationOut(BaseModel):
    station_id: str
    name: str


class StationsOut(BaseModel):
    stations: "list[StationOut]"


class DatesOut(BaseModel):
    dates: "list[str]"


class ProgramOut(BaseModel):
    prog_id: str
    station_id: str
    station_name: str
    ftime: str
    duration: int
    title: str
    pfm: "str | None" = None
    url: "str | None" = None
    info: "str | None" = None
    image_url: "str | None" = None
    date: "str | None" = None


class ProgramsOut(BaseModel):
    date: str
    programs: "list[ProgramOut]"


class SearchOut(BaseModel):
    q: str
    scope: str
    aired: "list[str]"
    programs: "list[ProgramOut]"


class ProgramDetailOut(BaseModel):
    prog_id: str
    station_id: str
    station_name: str
    date: str
    ftime: str
    duration: int
    title: str
    pfm: "str | None" = None
    url: "str | None" = None
    info: "str | None" = None
    image_url: "str | None" = None


class CurrentProgramOut(BaseModel):
    prog_id: str
    station_id: str
    name: str
    ftime: str
    duration: int
    title: str
    pfm: "str | None" = None
    url: "str | None" = None
    image_url: "str | None" = None


# ---- 再生・音量 ---------------------------------------------------------

class NowPlayingOut(BaseModel):
    playing: bool
    station_id: "str | None" = None
    device: "str | None" = None
    volume: "float | None" = None


class PlaybackOut(NowPlayingOut):
    program: "CurrentProgramOut | None" = None
    station_name: "str | None" = None


class NowOut(BaseModel):
    programs: "list[CurrentProgramOut]"


class StationNowOut(BaseModel):
    station_id: str
    program: "CurrentProgramOut | None" = None


class VolumeOut(BaseModel):
    available: bool
    volume: "float | None" = None
    muted: "bool | None" = None
    device: "str | None" = None


class VolumeSetOut(BaseModel):
    status: str
    volume: "float | None" = None


class PlayOut(BaseModel):
    status: str
    station_id: "str | None" = None
    station_name: "str | None" = None
    type: "str | None" = None


class StatusMsg(BaseModel):
    status: str


# ---- 録音ジョブ ---------------------------------------------------------

class JobProgress(BaseModel):
    elapsed_sec: int
    total_sec: int
    percent: "int | None" = None


class JobOut(BaseModel):
    id: str
    station_id: str
    start_at: str
    duration_min: int
    prog_id: "str | None" = None
    title: "str | None" = None
    method: str
    output: str
    with_art: bool
    status: str
    started_at: "str | None" = None
    finished_at: "str | None" = None
    error: "str | None" = None
    reservation_id: "int | None" = None
    created_at: str
    progress: "JobProgress | None" = None
    size: int


class JobsOut(BaseModel):
    jobs: "list[JobOut]"


class StopJobOut(BaseModel):
    status: str
    job_id: str


# ---- 録音の作成（統一入口） ---------------------------------------------

class TargetView(BaseModel):
    station_id: str
    start_at: str
    duration_min: int
    prog_id: "str | None" = None
    title: "str | None" = None


class RecordingCreateResult(BaseModel):
    kind: str               # "job" | "reservation"
    method: str             # "timefree" | "live" | "scheduled"
    status: str
    target: TargetView
    output: str
    job_id: "str | None" = None
    reservation_id: "int | None" = None
    at_job_id: "str | None" = None


# ---- 予約 ---------------------------------------------------------------

class ReservationOut(BaseModel):
    id: int
    station_id: str
    start_at: str
    duration_min: int
    prog_id: "str | None" = None
    title: "str | None" = None
    with_art: int
    start_offset_sec: int
    end_offset_sec: int
    output: str
    at_job_id: "str | None" = None
    rule_id: "int | None" = None
    status: str
    created_at: str
    updated_at: str


class ReservationsOut(BaseModel):
    reservations: "list[ReservationOut]"


class CancelReservationOut(BaseModel):
    status: str
    reservation_id: int


# ---- 統合ステータス -----------------------------------------------------

class RecordingSummary(BaseModel):
    active: int
    job_ids: "list[str]"


class ReservationsSummary(BaseModel):
    count: int
    next: "ReservationOut | None" = None


class StatusOut(BaseModel):
    playback: NowPlayingOut
    recording: RecordingSummary
    reservations: ReservationsSummary
    version: str


# ---- ルール（シリーズ録画） ---------------------------------------------

class RuleOut(BaseModel):
    id: int
    query: str
    match_fields: str
    station_id: "str | None" = None
    weekday: "str | None" = None
    time_from: "str | None" = None
    time_to: "str | None" = None
    with_art: int
    start_offset_sec: int
    end_offset_sec: int
    enabled: int
    created_at: str
    updated_at: str


class RulesOut(BaseModel):
    rules: "list[RuleOut]"


class RuleCreateOut(BaseModel):
    id: int
    rule: RuleOut


class DeleteRuleOut(BaseModel):
    status: str
    rule_id: int


# ---- 保守 ---------------------------------------------------------------

class ReconcileCount(BaseModel):
    created: int
    pruned: int


class MaintenanceOut(BaseModel):
    guide: "int | None" = None
    synced: int
    rules: ReconcileCount
    guide_error: "str | None" = None


# ---- 録音ファイル -------------------------------------------------------

class RecordingFileOut(BaseModel):
    filename: str
    size: int
    mtime: int
    recording: bool
    prog: "ProgramDetailOut | None" = None


class RecordingsOut(BaseModel):
    recordings: "list[RecordingFileOut]"


# ---- 旧 API（後方互換・非推奨） -----------------------------------------

class RecordLegacyOut(BaseModel):
    status: str
    type: "str | None" = None
    output: "str | None" = None
    scheduled: "str | None" = None


class ScheduleLegacyOut(BaseModel):
    job_id: str
    scheduled: str
    station_id: str
    duration: str
    output: str
    prog: "ProgramDetailOut | None" = None


class SchedulesLegacyOut(BaseModel):
    schedules: "list[ScheduleLegacyOut]"
    error: "str | None" = None
